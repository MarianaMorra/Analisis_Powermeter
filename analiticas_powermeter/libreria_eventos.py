import pandas as pd

# Generacion de ventanas de datos
def generar_ventanas(df, win_s):
    df = df.sort_values("temporal_placa").set_index("temporal_placa")
    return df.groupby(pd.Grouper(freq=f"{win_s}s"))

# Derivada de corriente
def delta_corriente_por_ventana(grupos, fase):
    col = f"corriente_{fase}"

    df_agg = grupos[col].agg(
        i_max="max",
        i_min="min"
    ).reset_index()

  # para matar picos negativos
    baseline = df_agg["i_min"].rolling(
        window=3, min_periods=1
    ).median()

    df_agg["delta_corriente"] = df_agg["i_max"] - baseline

    return df_agg

# Deteccion de eventos 
def detectar_eventos_simples(deltas, umbral_delta):
    eventos = []
    en_evento = False
    inicio = None

    for d in deltas:
        activo = d["delta_corriente"] >= umbral_delta

        if activo and not en_evento:
            inicio = d["t_inicio"]
            en_evento = True

        elif (not activo) and en_evento:
            eventos.append({
                "inicio": inicio,
                "fin": d["t_inicio"]
            })
            en_evento = False
            inicio = None

    return eventos

# Une eventos por fase y tiempo
def unir_eventos_por_fase(eventos_r, eventos_s, eventos_t):
    eventos = []

    for ev in eventos_r:
        eventos.append({
            "inicio": ev["inicio"],
            "fin": ev["fin"],
            "fase": "R"
        })

    for ev in eventos_s:
        eventos.append({
            "inicio": ev["inicio"],
            "fin": ev["fin"],
            "fase": "S"
        })

    for ev in eventos_t:
        eventos.append({
            "inicio": ev["inicio"],
            "fin": ev["fin"],
            "fase": "T"
        })

    # ordenar por inicio
    eventos.sort(key=lambda x: x["inicio"])
    return eventos

# Determina si el evento fue global de la maquina, no por fase
def consolidar_eventos_maquina(
    eventos_fase,
    df,
    fase,
    ventana_media_s,
    tolerancia_rel,
    tiempo_estable_s
):
    """
    Consolida eventos por fase en eventos de máquina.

    - Un evento NO se cierra por ausencia de datos.
    - Mientras no haya datos suficientes para evaluar estabilidad,
      el evento queda abierto (fin = None).
    - El cierre solo ocurre cuando reaparecen datos y se cumple
      el criterio de estabilidad.
    """

    df = df.sort_values("temporal_placa").reset_index(drop=True)
    times = df["temporal_placa"].to_numpy()

    eventos_fase = sorted(eventos_fase, key=lambda x: x["inicio"])
    eventos_maquina = []
    actual = None

    col = f"corriente_{fase}"

    # delta temporal típico (cuando hay datos)
    dt_medio = df["temporal_placa"].diff().median().total_seconds()

    for ev in eventos_fase:

        if actual is None:
            actual = {
                "inicio": ev["inicio"],
                "fin": ev["fin"],
                "fases": {ev["fase"]}
            }
            continue

        # superposición → mismo evento
        if actual["fin"] is None or ev["inicio"] <= actual["fin"]:
            actual["fin"] = (
                max(actual["fin"], ev["fin"])
                if actual["fin"] is not None
                else ev["fin"]
            )
            actual["fases"].add(ev["fase"])
            continue

        # ---- NO hay superposición: intentar cerrar ----

        t_ini = actual["fin"]
        t_fin = actual["fin"] + pd.Timedelta(minutes=10)

        i_ini = times.searchsorted(t_ini, side="left")
        i_fin = times.searchsorted(t_fin, side="right")

        df_ev = df.iloc[i_ini:i_fin].copy()

        # ---- NO HAY DATOS → NO SE CIERRA ----
        if df_ev.empty:
            actual["fin"] = None
            continue

        # promedio móvil
        df_ev["media"] = df_ev[col].rolling(
            window=ventana_media_s,
            min_periods=ventana_media_s
        ).mean()

        df_ev["estable"] = (
            (df_ev[col] - df_ev["media"]).abs()
            <= df_ev["media"] * tolerancia_rel
        )

        contador = 0
        fin_estable = None

        for i in range(len(df_ev)):
            if df_ev["estable"].iloc[i]:
                contador += 1
                if contador * dt_medio >= tiempo_estable_s:
                    fin_estable = df_ev["temporal_placa"].iloc[i]
                    break
            else:
                contador = 0

        if fin_estable:
            # cierre real del evento
            actual["fin"] = fin_estable
            eventos_maquina.append(actual)

            actual = {
                "inicio": ev["inicio"],
                "fin": ev["fin"],
                "fases": {ev["fase"]}
            }
        else:
            # no hay estabilidad → sigue el mismo evento
            actual["fin"] = max(actual["fin"], ev["fin"])
            actual["fases"].add(ev["fase"])

    if actual:
        eventos_maquina.append(actual)

    return eventos_maquina

# Evitar que picos de arranque/ fin contaminen los verdaderos eventos de las plegadoras
def recortar_rango_operativo_por_corriente(
    df,
    fase="r",
    umbral_corriente=2.0,
    min_muestras=3
):
    col = f"corriente_{fase}"

    # índice donde empieza el trabajo real
    idx_ini = None
    count = 0
    for i, v in enumerate(df[col]):
        if v >= umbral_corriente:
            count += 1
            if count >= min_muestras:
                idx_ini = i - min_muestras + 1
                break
        else:
            count = 0

    # índice donde termina el trabajo real
    idx_fin = None
    count = 0
    for i in range(len(df) - 1, -1, -1):
        if df[col].iloc[i] >= umbral_corriente:
            count += 1
            if count >= min_muestras:
                idx_fin = i + min_muestras - 1
                break
        else:
            count = 0

    if idx_ini is None or idx_fin is None or idx_ini >= idx_fin:
        return df.copy()

    return df.iloc[idx_ini:idx_fin + 1].copy()