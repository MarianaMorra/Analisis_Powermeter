import os
import glob
import json
import plotly.graph_objects as go
from datetime import datetime
import matplotlib
import pandas as pd
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ..libreria_eventos import generar_ventanas
from ..libreria_eventos import delta_corriente_por_ventana
from ..libreria_eventos import detectar_eventos_simples
from ..libreria_eventos import unir_eventos_por_fase
from ..libreria_eventos import consolidar_eventos_maquina
from ..libreria_eventos import recortar_rango_operativo_por_corriente

# --------------------- VARIABLES CONFIGURABLES -----------------------

CONFIG= {
    "win_s": 60,
    "ventana_media_s": 30,
    "tolerancia_rel": 0.05,
    "umbral_delta": 10.0,
    "tiempo_estable_s": 20,
    "fase_recorte": "r",
    "umbral_corriente_recorte": 2.0,
    "min_muestras_recorte": 2,
}

def mostrar_config():
    print("\n--- Config actual ---")
    for k, v in CONFIG.items():
        print(f"{k}: {v}")

def cargar_ultimo_config_maquina(nombre_maquina: str) -> bool:
    os.makedirs("salidas", exist_ok=True)

    maq = nombre_maquina.replace(" ", "_")
    patron = os.path.join("salidas", f"config_eventos_{maq}_*.json")
    archivos = glob.glob(patron)

    if not archivos:
        print(f"No hay config guardada para {nombre_maquina}. Se usa la CONFIG actual.")
        return False

    # más reciente por fecha de modificación
    ultimo = max(archivos, key=os.path.getmtime)

    with open(ultimo, "r", encoding="utf-8") as f:
        data = json.load(f)

    CONFIG.update(data)
    print(f"Configuración cargada para {nombre_maquina} desde: {ultimo}")
    return True

def exportar_config(nombre_maquina: str):
    os.makedirs("salidas", exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # por prolijidad mínima
    maq = nombre_maquina.replace(" ", "_")

    out_path = os.path.join("salidas", f"config_eventos_{maq}_{ts}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, indent=2)

    return out_path

def set_config(clave: str, valor: str):

    if clave not in CONFIG:
        raise KeyError(f"Parámetro inexistente: {clave}")

    actual = CONFIG[clave]
    t = type(actual)

    if t is bool:
        v = valor.strip().lower()
        if v in ("1", "true", "t", "si", "sí", "y", "yes"):
            CONFIG[clave] = True
        elif v in ("0", "false", "f", "no", "n"):
            CONFIG[clave] = False
        else:
            raise ValueError("Valor booleano inválido. Use true/false o 1/0.")
        return

    if t is int:
        CONFIG[clave] = int(valor)
        return

    if t is float:
        CONFIG[clave] = float(valor)
        return

    # strings u otros
    CONFIG[clave] = valor


def modificar_parametros_eventos_menu():
    # definición de tipos y validaciones mínimas
    campos = [
        ("win_s", int, "Tamaño de ventana (segundos)", 1, 24*3600),
        ("ventana_media_s", int, "Ventana media (muestras)", 1, 10**9),
        ("tolerancia_rel", float, "Tolerancia relativa (ej 0.05)", 0.0, 1.0),
        ("umbral_delta", float, "Umbral delta corriente", 0.0, None),
        ("tiempo_estable_s", int, "Tiempo estable para cierre (seg)", 0, 24*3600),
        ("fase_recorte", str, "Fase recorte (r/s/t)", None, None),
        ("umbral_corriente_recorte", float, "Umbral corriente recorte", 0.0, None),
        ("min_muestras_recorte", int, "Mínimo muestras recorte", 1, 10**9),
    ]

    while True:
        print("\n2. Modificar parametros")
        for i, (k, _, desc, _, _) in enumerate(campos, start=1):
            print(f"\t{i}. {k} = {CONFIG[k]}  | {desc}")
        print("\t0. Volver")

        op = input("> ").strip()
        if op == "0":
            break

        if not op.isdigit() or not (1 <= int(op) <= len(campos)):
            print("Opción inválida.")
            continue

        idx = int(op) - 1
        clave, tipo, desc, minimo, maximo = campos[idx]

        nuevo = input(f"Nuevo valor para {clave} ({desc}): ").strip()
        if nuevo == "":
            print("Sin cambios.")
            continue

        try:
            if tipo is int:
                val = int(nuevo)
                if minimo is not None and val < minimo:
                    raise ValueError(f"Debe ser >= {minimo}")
                if maximo is not None and val > maximo:
                    raise ValueError(f"Debe ser <= {maximo}")

            elif tipo is float:
                val = float(nuevo)
                if minimo is not None and val < minimo:
                    raise ValueError(f"Debe ser >= {minimo}")
                if maximo is not None and val > maximo:
                    raise ValueError(f"Debe ser <= {maximo}")

            else:  # str
                val = nuevo.strip().lower()
                if clave == "fase_recorte" and val not in ("r", "s", "t"):
                    raise ValueError("fase_recorte debe ser r, s o t")

            CONFIG[clave] = val
            print("OK.")

        except Exception as e:
            print(f"Valor inválido: {e}")

# --------------------- CARGA INFORMACION -----------------------------
PLEGADORAS = {
    "1": {"nombre": "Plegadora 1", "numero_serie": "28562F60A8D8"},
    "2": {"nombre": "Plegadora 7", "numero_serie": "28562F612CC4"},
}

def seleccionar_plegadora():
    while True:
        print("\nSeleccione máquina:")
        print("\t1. Plegadora 1 - nro. serie: 28562F60A8D8")
        print("\t2. Plegadora 7 - nro. serie: 28562F612CC4")
        op = input("> ").strip()

        if op in ("1", "2"):
            info = PLEGADORAS[op]
            return info["numero_serie"], info["nombre"]

        print("Opción inválida.")

def cargar_csv_instantanea(path_csv: str) -> tuple[pd.DataFrame, str, str]:
    """
    Carga CSV, pide máquina (1 o 7) y devuelve:
      (df_filtrado, numero_serie, nombre_plegadora)
    Requiere columnas:
      temporal_placa, corriente_r, corriente_s, corriente_t, numero_serie
    """

    df = pd.read_csv(path_csv)

    columnas_requeridas = {
        "temporal_placa", "corriente_r", "corriente_s", "corriente_t", "numero_serie"
    }
    faltan = columnas_requeridas - set(df.columns)
    if faltan:
        raise ValueError(f"Faltan columnas en el CSV: {sorted(faltan)}")

    df["temporal_placa"] = pd.to_datetime(df["temporal_placa"], utc=True, errors="coerce")
    df = df.dropna(subset=["temporal_placa"]).sort_values("temporal_placa").reset_index(drop=True)

    # elegir máquina
    serie, nombre = seleccionar_plegadora()

    df_i = df[df["numero_serie"].astype(str) == serie].copy()
    if df_i.empty:
        raise ValueError(f"El CSV no tiene datos para {nombre} ({serie}).")

    return df_i, serie, nombre

# --------------------- DETECCION EVENTOS -----------------------------
import pandas as pd

# Helpers
def deltas_a_dicts(df_delta: pd.DataFrame):
    """
    Convierte el df de deltas (con columna temporal_placa) a la estructura
    que espera detectar_eventos_simples: lista de dicts con t_inicio y delta_corriente.
    """
    if "temporal_placa" not in df_delta.columns:
        raise ValueError("df_delta debe tener columna 'temporal_placa'")

    out = []
    for _, row in df_delta.iterrows():
        out.append({
            "t_inicio": row["temporal_placa"],
            "delta_corriente": float(row["delta_corriente"]) if pd.notna(row["delta_corriente"]) else float("nan")
        })
    return out

def calcular_eventos_desde_df(
    df_m,
    win_s,
    ventana_media_s,
    tolerancia_rel,
    umbral_delta,
    tiempo_estable_s,
    umbral_corriente_recorte,
    min_muestras_recorte,
    *,
    fase_recorte="r",
):
    """
    Requiere df_m con:
      temporal_placa, corriente_r, corriente_s, corriente_t
    """

    if df_m is None or df_m.empty:
        return []

    # 0) Asegurar timestamp parseado y ordenado
    df_m = df_m.copy()
    df_m["temporal_placa"] = pd.to_datetime(df_m["temporal_placa"], utc=True, errors="coerce")
    df_m = df_m.dropna(subset=["temporal_placa"]).sort_values("temporal_placa").reset_index(drop=True)

    if df_m.empty:
        return []

    # 1) Recorte para evitar picos de encendido/apagado
    df_m = recortar_rango_operativo_por_corriente(
        df_m,
        fase=fase_recorte,
        umbral_corriente=umbral_corriente_recorte,
        min_muestras=min_muestras_recorte
    )

    if df_m.empty:
        return []

    # 2) Ventanas
    ventanas = generar_ventanas(df_m, win_s)

    # 3) Deltas por fase
    deltas = {
        "R": delta_corriente_por_ventana(ventanas, fase="r"),
        "S": delta_corriente_por_ventana(ventanas, fase="s"),
        "T": delta_corriente_por_ventana(ventanas, fase="t"),
    }

    # 4) Eventos simples por fase
    eventos_por_fase = {}
    for fase, df_delta in deltas.items():
        registros = [
            {"t_inicio": row.temporal_placa, "delta_corriente": row.delta_corriente}
            for row in df_delta.itertuples(index=False)
        ]
        eventos_por_fase[fase] = detectar_eventos_simples(registros, umbral_delta)

    # 5) Unir eventos de fases
    eventos_fase_lista = unir_eventos_por_fase(
        eventos_por_fase["R"],
        eventos_por_fase["S"],
        eventos_por_fase["T"]
    )

    if not eventos_fase_lista:
        return []

    # 6) Consolidar a eventos de máquina
    eventos_maquina = consolidar_eventos_maquina(
        eventos_fase=eventos_fase_lista,
        df=df_m,
        fase="r",  # fase usada para evaluar estabilidad
        ventana_media_s=ventana_media_s,
        tolerancia_rel=tolerancia_rel,
        tiempo_estable_s=tiempo_estable_s
    )

    return eventos_maquina

# --------------------- GRAFICA CORRIENTE - EVENTOS -------------------
def graficar_maquina(df_i: pd.DataFrame,
                             serie: str,
                             nombre: str,
                             df_ev: pd.DataFrame | None = None,
                             out_dir: str = "salidas"):

    os.makedirs(out_dir, exist_ok=True)

    if "temporal_placa" not in df_i.columns:
        raise ValueError("df_i debe tener columna temporal_placa")

    df = df_i.copy()
    df["temporal_placa"] = pd.to_datetime(df["temporal_placa"], utc=True, errors="coerce")
    df = df.dropna(subset=["temporal_placa"]).sort_values("temporal_placa")

    if df.empty:
        raise ValueError("df_i quedó vacío.")

    fig = go.Figure()

    for fase in ("r", "s", "t"):
        col = f"corriente_{fase}"
        if col not in df.columns:
            continue

        fig.add_trace(
            go.Scatter(
                x=df["temporal_placa"],
                y=df[col],
                mode="lines",
                name=f"Fase {fase.upper()}",
            )
        )

    # Eventos como bandas verticales
    if df_ev is not None and not df_ev.empty:
        eventos = df_ev.copy()
        eventos["inicio"] = pd.to_datetime(eventos["inicio"], utc=True, errors="coerce")
        eventos["fin"] = pd.to_datetime(eventos["fin"], utc=True, errors="coerce")
        eventos = eventos.dropna(subset=["inicio", "fin"])

        for _, ev in eventos.iterrows():
            fig.add_vrect(
                x0=ev["inicio"],
                x1=ev["fin"],
                fillcolor="red",
                opacity=0.2,
                line_width=0
            )

    fig.update_layout(
        title=f"{nombre} ({serie}) - Corrientes R/S/T",
        xaxis_title="Tiempo",
        yaxis_title="Corriente",
        hovermode="x unified"
    )

    out_path = os.path.join(out_dir, f"{serie}_corrientes.html")

    fig.write_html(out_path, auto_open=True)

    return out_path
