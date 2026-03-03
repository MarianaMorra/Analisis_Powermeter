import pandas as pd
import numpy as np

# Generacion de ventanas de datos
def generar_ventanas(df, win_s):
    df = df.sort_values("temporal_placa").set_index("temporal_placa")
    return df.groupby(pd.Grouper(freq=f"{win_s}s"))

# Derivada de corriente
def delta_corriente_por_ventana(grupos, baseline_win, fase):
    col = f"corriente_{fase}"

    df_agg = grupos[col].agg(
        i_max="max",
        i_min="min"
    ).reset_index()

  # para matar picos negativos
    baseline = df_agg["i_min"].rolling(
        window=baseline_win, min_periods=1
    ).median()

    df_agg["delta_corriente"] = df_agg["i_max"] - baseline

    return df_agg

# Deteccion de eventos 
def detectar_eventos_simples(deltas, umbral_delta):
    """
    deltas: list[{"t_inicio": datetime, "delta_corriente": float}]
    Devuelve eventos: list[{"inicio": datetime, "fin": datetime|None}]
    Si el último evento queda activo al final, se guarda con fin=None.
    """
    eventos = []
    en_evento = False
    inicio = None

    for d in deltas:
        activo = d["delta_corriente"] >= umbral_delta

        if activo and not en_evento:
            inicio = d["t_inicio"]
            en_evento = True

        elif (not activo) and en_evento:
            eventos.append({"inicio": inicio, "fin": d["t_inicio"]})
            en_evento = False
            inicio = None

    # si termina activo, queda abierto
    if en_evento:
        eventos.append({"inicio": inicio, "fin": None})

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
    fase="r",
    ventana_media_s=60,
    tolerancia_rel=0.10,
    tiempo_estable_s=120,
    max_busqueda_estabilidad_s=600,   # antes era 10 min fijo
    max_gap_s=None,                  # opcional: si querés evitar "evento infinito"
):
    """
    Consolida eventos por fase en eventos de máquina.

    Reglas:
    - Si eventos se solapan (o el actual está abierto), se fusionan.
    - Si no se solapan:
        - se intenta "cerrar de verdad" verificando estabilidad de corriente
          en la fase elegida (fase="r"/"s"/"t") luego del fin provisorio.
        - si no hay datos suficientes, NO se cierra: fin=None (evento abierto).
    - Si el evento queda abierto (fin=None) y max_gap_s está definido,
      y el próximo evento empieza luego de ese gap, se fuerza nuevo evento
      (para que un corte de comunicación no te junte todo el día).
    """

    # ----------------- helpers -----------------
    def _max_fin(a, b):
        """max seguro: si cualquiera es None -> None (evento abierto)."""
        if a is None or b is None:
            return None
        return max(a, b)

    def _to_dt(x):
        return pd.to_datetime(x)

    def _ensure_datetime_col(dfx, colname="temporal_placa"):
        dfx = dfx.copy()
        dfx[colname] = pd.to_datetime(dfx[colname], errors="coerce")
        dfx = dfx.dropna(subset=[colname])
        return dfx

    # ----------------- sanitización df -----------------
    df = _ensure_datetime_col(df, "temporal_placa").sort_values("temporal_placa").reset_index(drop=True)
    if df.empty:
        return []

    col = f"corriente_{fase}"
    if col not in df.columns:
        raise ValueError(f"Falta la columna {col} en df")

    # índice temporal para rolling por tiempo real
    df_idx = df.set_index("temporal_placa", drop=False)

    # ----------------- dt típico robusto -----------------
    diffs = df["temporal_placa"].diff().dropna()
    if diffs.empty:
        dt_medio = None
    else:
        dt_medio = diffs.median().total_seconds()
        if not np.isfinite(dt_medio) or dt_medio <= 0:
            dt_medio = None

    # fallback: si no se puede estimar, asumimos 1s para no romper contador
    if dt_medio is None:
        dt_medio = 1.0

    # ----------------- sanitización eventos_fase -----------------
    eventos_fase = sorted(eventos_fase, key=lambda x: _to_dt(x["inicio"]))

    # normalizo inicio/fin a datetime o None
    norm = []
    for ev in eventos_fase:
        ini = _to_dt(ev["inicio"])
        fin = None if ev.get("fin") is None else _to_dt(ev["fin"])
        norm.append({"inicio": ini, "fin": fin, "fase": ev["fase"]})
    eventos_fase = norm

    eventos_maquina = []
    actual = None

    # ----------------- función de chequeo de estabilidad -----------------
    def _buscar_fin_estable(t_ini):
        """
        Busca un timestamp de cierre real a partir de t_ini,
        cuando la corriente vuelve a estar estable por tiempo_estable_s.
        Devuelve datetime o None.
        """
        if t_ini is None:
            return None

        t_ini = _to_dt(t_ini)
        t_fin = t_ini + pd.Timedelta(seconds=max_busqueda_estabilidad_s)

        # recorte por tiempo (esto evita searchsorted + problemas de dtype)
        df_ev = df_idx.loc[t_ini:t_fin].copy()
        if df_ev.empty:
            return None

        # rolling por TIEMPO REAL
        media = df_ev[col].rolling(f"{int(ventana_media_s)}s", min_periods=max(1, int(ventana_media_s/ max(dt_medio, 1e-6)))).mean()
        df_ev["media"] = media

        # si media es NaN al principio, estable = False
        df_ev["estable"] = False
        m = df_ev["media"]
        ok = m.notna() & ((df_ev[col] - m).abs() <= (m.abs() * tolerancia_rel))
        df_ev.loc[ok, "estable"] = True

        # contar estabilidad consecutiva (en tiempo, usando dt_medio)
        contador = 0
        for ts, est in zip(df_ev["temporal_placa"].to_numpy(), df_ev["estable"].to_numpy()):
            if est:
                contador += 1
                if contador * dt_medio >= tiempo_estable_s:
                    return pd.to_datetime(ts)
            else:
                contador = 0

        return None

    # ----------------- loop principal -----------------
    for ev in eventos_fase:

        if actual is None:
            actual = {"inicio": ev["inicio"], "fin": ev["fin"], "fases": {ev["fase"]}}
            continue

        # si actual está abierto y querés limitar el "gap infinito"
        if actual["fin"] is None and max_gap_s is not None:
            gap = (ev["inicio"] - actual["inicio"]).total_seconds()
            # ojo: esto usa gap desde inicio; alternativa: desde última evidencia.
            # si querés “desde último dato”, habría que guardar last_seen.
            if gap > max_gap_s:
                # cerramos como abierto (fin=None) y arrancamos otro
                eventos_maquina.append(actual)
                actual = {"inicio": ev["inicio"], "fin": ev["fin"], "fases": {ev["fase"]}}
                continue

        # ---- superposición: mismo evento ----
        if actual["fin"] is None or ev["inicio"] <= actual["fin"]:
            actual["fin"] = _max_fin(actual["fin"], ev["fin"])
            actual["fases"].add(ev["fase"])
            continue

        # ---- no hay superposición: intentar cerrar ----
        t_ini = actual["fin"]  # acá es datetime seguro (no None)
        fin_estable = _buscar_fin_estable(t_ini)

        if fin_estable is None:
            # No hay datos o no hubo estabilidad -> NO se cierra: queda abierto
            actual["fin"] = None
            actual["fases"].add(ev["fase"])
            # además, lo fusionamos extendiendo "fin" con el de ev si existiera, pero como fin=None, queda abierto.
            continue

        # Si hay fin_estable, se cierra el actual y se inicia uno nuevo
        actual["fin"] = fin_estable
        eventos_maquina.append(actual)

        actual = {"inicio": ev["inicio"], "fin": ev["fin"], "fases": {ev["fase"]}}

    # agregar el último
    if actual is not None:
        eventos_maquina.append(actual)

    # normalizar fases a lista ordenada para que no te explote JSON
    for ev in eventos_maquina:
        ev["fases"] = sorted(ev["fases"])

    return eventos_maquina

# Evitar que picos de arranque/ fin contaminen los verdaderos eventos de las plegadoras
def recortar_rango_operativo_por_corriente(
    df,
    fase,       # puede ser "r", "s", "t" o "media"
    umbral_corriente=2.0,
    min_muestras=3
):
    if fase == "media":
        col = "corriente_media"
        df = df.copy()
        df[col] = (
            df["corriente_r"] +
            df["corriente_s"] +
            df["corriente_t"]
        ) / 3
    else:
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