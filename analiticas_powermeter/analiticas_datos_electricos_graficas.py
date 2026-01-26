import pandas as pd
from datetime import timedelta

#-------------------------------------------------------------------------------------------------------------------------------

def cargar_data(path_csv, max_filas=None):
    df = pd.read_csv(path_csv)

    df["temporal_placa"] = pd.to_datetime(
        df["temporal_placa"],
        errors="coerce",
        dayfirst=True
    )

    df = df[df["temporal_placa"].notna()]

    if max_filas:
        df = df.iloc[:max_filas]

    df = df.sort_values("temporal_placa").reset_index(drop=True)

    return df

#-------------------------------------------------------------------------------------------------------------------------------

def generar_ventanas(df, win_s):
    ventanas = []
    t0 = df["temporal_placa"].iloc[0]
    t1 = df["temporal_placa"].iloc[-1]

    inicio = t0
    while inicio < t1:
        fin = inicio + timedelta(seconds=win_s)
        w = df[(df["temporal_placa"] >= inicio) & (df["temporal_placa"] < fin)]
        if len(w) > 0:
            ventanas.append(w)
        inicio = fin

    return ventanas
#-------------------------------------------------------------------------------------------------------------------------------

def delta_corriente_por_ventana(ventanas, fase="r"):
    out = []
    col = f"corriente_{fase}"

    for w in ventanas:
        delta = w[col].max() - w[col].min()
        out.append({
            "t_inicio": w["temporal_placa"].iloc[0],
            "delta_corriente": delta,
            "i_max": w[col].max(),
            "i_min": w[col].min(),
        })

    return out

#-------------------------------------------------------------------------------------------------------------------------------

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

#-------------------------------------------------------------------------------------------------------------------------------

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

#-------------------------------------------------------------------------------------------------------------------------------

def consolidar_eventos_maquina(eventos_fase, gap_max_s=60):
    """
    gap_max_s: tolerancia entre eventos (segundos)
    """
    eventos_fase = sorted(eventos_fase, key=lambda x: x["inicio"])

    eventos_maquina = []
    actual = None

    for ev in eventos_fase:
        if actual is None:
            actual = {
                "inicio": ev["inicio"],
                "fin": ev["fin"],
                "fases": {ev["fase"]}
            }
            continue

        gap = (ev["inicio"] - actual["fin"]).total_seconds()

        if ev["inicio"] <= actual["fin"] or gap <= gap_max_s:
            # mismo evento
            actual["fin"] = max(actual["fin"], ev["fin"])
            actual["fases"].add(ev["fase"])
        else:
            # evento nuevo
            eventos_maquina.append(actual)
            actual = {
                "inicio": ev["inicio"],
                "fin": ev["fin"],
                "fases": {ev["fase"]}
            }

    if actual:
        eventos_maquina.append(actual)

    return eventos_maquina

#-------------------------------------------------------------------------------------------------------------------------------

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

#-------------------------------------------------------------------------------------------------------------------------------

def calcular_analiticas(
    df_m,                 
    win_s=60,
    umbral_delta=2.0,
    gap_max_s=60
):
    if df_m.empty:
        return [], {}

    ventanas = generar_ventanas(df_m, win_s=win_s)

    deltas = {
        "R": delta_corriente_por_ventana(ventanas, fase="r"),
        "S": delta_corriente_por_ventana(ventanas, fase="s"),
        "T": delta_corriente_por_ventana(ventanas, fase="t"),
    }

    eventos_por_fase = {
        fase: detectar_eventos_simples(d, umbral_delta)
        for fase, d in deltas.items()
    }

    eventos_fase = unir_eventos_por_fase(
        eventos_por_fase["R"],
        eventos_por_fase["S"],
        eventos_por_fase["T"]
    )

    eventos_maquina = consolidar_eventos_maquina(
        eventos_fase,
        gap_max_s=gap_max_s
    )

    return eventos_maquina, eventos_por_fase

#-------------------------------------------------------------------------------------------------------------------------------
import plotly.graph_objects as go
import plotly.io as pio

pio.renderers.default = "browser"

def graficar_eventos_sobre_corriente(
    df_m,
    eventos_maquina,
    numero_serie,
    fases=("R", "S", "T")
):

    map_fase_col = {
        "R": "corriente_r",
        "S": "corriente_s",
        "T": "corriente_t",
    }

    colores_fase = {
        "R": "red",
        "S": "green",
        "T": "blue",
    }

    fig = go.Figure()
    trace_idx = {}

    # Trazas de corriente
    for fase in fases:
        visible = (fase == "R")
        col = map_fase_col[fase]

        fig.add_trace(
            go.Scatter(
                x=df_m["temporal_placa"],
                y=df_m[col],
                mode="lines",
                name=f"Fase {fase}",
                line=dict(color=colores_fase[fase]),
                visible=visible,
            )
        )

        trace_idx[fase] = len(fig.data) - 1

    # Shapes de eventos
    shapes_eventos = [
        dict(
            type="rect",
            xref="x",
            yref="paper",
            x0=ev["inicio"],
            x1=ev["fin"],
            y0=0,
            y1=1,
            fillcolor="grey",
            opacity=0.25,
            line_width=0,
        )
        for ev in eventos_maquina
    ]

    # Botones por fase
    botones = []
    for fase in fases:
        visibles = [False] * len(fig.data)
        visibles[trace_idx[fase]] = True

        botones.append(
            dict(
                label=f"Fase {fase}",
                method="update",
                args=[
                    {"visible": visibles},
                    {"shapes": shapes_eventos},
                ],
            )
        )

    fig.update_layout(
        title=f"{numero_serie} – Corriente por fase con eventos de máquina",
        xaxis_title="Tiempo",
        yaxis_title="Corriente [A]",
        template="plotly_white",
        shapes=shapes_eventos,
        updatemenus=[
            dict(
                buttons=botones,
                direction="down",
                x=1.08,
                y=1.1,
                showactive=True,
            )
        ],
    )

    fig.show()

#-------------------------------------------------------------------------------------------------------------------------------

if __name__ == "__main__":

    CSV_PATH = r"C:\Users\HP Spectre X360\Desktop\MARIANA\COLLOQUIA_2025\Analisis_Powermeter\Pruebas\12-1-2026_prueba\12-1-2026_PLE1.csv"

    df = cargar_data(CSV_PATH)

    for numero_serie in df["numero_serie"].unique():

        df_m = df[df["numero_serie"] == numero_serie].copy()

        df_m = recortar_rango_operativo_por_corriente(
            df_m,
            fase="r",
            umbral_corriente=5.0,
            min_muestras=3
        )
        eventos_maquina, eventos_por_fase = calcular_analiticas(
            df_m,
            win_s=60,
            umbral_delta=2.0,
            gap_max_s=60
        )

        print(f"\nPlegadora {numero_serie}")
        print("Eventos máquina:", len(eventos_maquina))
        print("Eventos por fase:", {k: len(v) for k, v in eventos_por_fase.items()})

        graficar_eventos_sobre_corriente(df_m, eventos_maquina, numero_serie)
