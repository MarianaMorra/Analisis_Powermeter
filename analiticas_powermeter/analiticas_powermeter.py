#import psycopg2
import os
import logging
#from psycopg2.extras import DictCursor
from datetime import datetime, timedelta
import sys
from sklearn.cluster import KMeans
#import random
#import time
import pytz
from datetime import datetime
import csv

# Cargar las variables de entorno
DB_HOST = os.environ.get("DB_HOST", "")
DB_USR = os.environ.get("DB_USR", "")
DB_PASS = os.environ.get("DB_PASS", "")
DB_NAME = os.environ.get("DB_NAME", "")
ID_ANALITICAS_POWERMETER = os.environ.get("ID_AB", "1")
stdout_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[stdout_handler],
)

logger = logging.getLogger(__name__)

# Parametros y constantes
umbral_pico = 0.5
ventana_pico = 5
fases = {
    "r": {
        "i": "corriente_r",
        "v": "tension_r",
        "p": "potencia_a_r",
        "q": "potencia_q_r"
    },
    "s": {
        "i": "corriente_s",
        "v": "tension_s",
        "p": "potencia_a_s",
        "q": "potencia_q_s"
    },
    "t": {
        "i": "corriente_t",
        "v": "tension_t",
        "p": "potencia_a_t",
        "q": "potencia_q_t"
    },
}

claves_features = ['media', 'std', 'pendiente', 'energia']

# Conexión a la base de datos.
# Obtiene las placas con la analítica de powermeter habilitada
# Obtener configuracon de la analitica del powermeter
# Obtener cual fue la ultima fecha analizada 
# Importar datos desde .csv (BORRAR UNA VEZ QUE ESTE ESTABLECIDA LA CONEXION CON LA BASE DE DATOS)
#==================================================================================================================================
import pandas as pd

def cargar_data_desde_csv(path_csv, max_filas=None):
    df = pd.read_csv(path_csv)

    # parsear timestamp sin asumir formato
    df['temporal_placa'] = pd.to_datetime(
        df['temporal_placa'],
        errors='coerce',
        dayfirst=True
    )

    # descartar filas inválidas
    df = df[df['temporal_placa'].notna()]

    if max_filas:
        df = df.iloc[:max_filas]

    # quedarnos solo con las columnas que importan
    cols = [
        'temporal_placa',
        'corriente_r', 'corriente_s', 'corriente_t',
        'potencia_a_r', 'potencia_a_s', 'potencia_a_t'
    ]
    df = df[cols]

    # convertir TODO a lista de dicts (rápido)
    data = [
        {
            "timestamp": r['temporal_placa'],
            "corriente_r": r['corriente_r'],
            "corriente_s": r['corriente_s'],
            "corriente_t": r['corriente_t'],
            "potencia_a_r": r['potencia_a_r'],
            "potencia_a_s": r['potencia_a_s'],
            "potencia_a_t": r['potencia_a_t'],
        }
        for r in df.to_dict(orient="records")
    ]

    return data

#==================================================================================================================================

def normalizar_timestamp(ts):

    if ts is None:
        return None

    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)

    if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)

    return ts

# Genera las ventanas de muestras para el clustering
from datetime import timedelta

def generar_ventanas(data, duracion_s, paso_s=None):
    if not data:
        return []

    if paso_s is None:
        paso_s = duracion_s

    dur = timedelta(seconds=duracion_s)
    paso = timedelta(seconds=paso_s)

    ventanas = []
    inicio = data[0]["timestamp"]
    fin_total = data[-1]["timestamp"]

    i = 0
    n = len(data)

    while inicio <= fin_total:
        fin = inicio + dur
        ventana = []

        while i < n and data[i]["timestamp"] < fin:
            ventana.append(data[i])
            i += 1

        if ventana:
            ventanas.append(ventana)

        inicio += paso

    return ventanas

# Extrae features de interes por ventana: energia/media/varianza/desvio/pendiente    
def extraer_features_por_fase(ventana):
    if len(ventana) < 2:
        return []

    features = []
    timestamps = [m["timestamp"] for m in ventana]

    for fase, cols in fases.items():
        segI = [
            m[cols["i"]] for m in ventana
            if isinstance(m[cols["i"]], (int, float))
        ]
        segP = [
            m[cols["p"]] for m in ventana
            if isinstance(m[cols["p"]], (int, float))
        ]

        if len(segI) < 2 or len(segP) < 2:
            continue

        # tiempos
        dt = []
        for i in range(1, len(timestamps)):
            d = (timestamps[i] - timestamps[i-1]).total_seconds()
            if d > 0:
                dt.append(d)

        if not dt:
            continue

        energia = sum(p * d for p, d in zip(segP[1:], dt))
        media = sum(segI) / len(segI)
        var = sum((x - media) ** 2 for x in segI) / len(segI)
        std = var ** 0.5

        features.append({
            "t_inicio": timestamps[0],
            "fase": fase,
            "media": media,
            "std": std,
            "pendiente": segI[-1] - segI[0],
            "energia": energia,
        })

    return features


# Normaliza los features para que ninguno sesgue el clustering
def normalizar_features_por_fase(rows, claves):
    por_fase = {}
    for r in rows:
        por_fase.setdefault(r["fase"], []).append(r)

    rows_norm = []
    for fase, items in por_fase.items():
        stats = {}
        for k in claves:
            vals = [r[k] for r in items]
            mu = sum(vals) / len(vals)
            sigma = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5 or 1.0
            stats[k] = (mu, sigma)

        for r in items:
            r2 = r.copy()
            for k in claves:
                mu, sigma = stats[k]
                r2[k] = (r[k] - mu) / sigma
            rows_norm.append(r2)

    return rows_norm

# Genera el clustering de los datos. Utiliza los features normalizados
def clusterizar_por_fase(rows_norm, claves, n_clusters=4):
    por_fase = {}
    for r in rows_norm:
        por_fase.setdefault(r["fase"], []).append(r)

    out = []
    for fase, items in por_fase.items():
        if len(items) < n_clusters:
            for r in items:
                r2 = r.copy()
                r2["cluster"] = None
                out.append(r2)
            continue

        X = [[r[k] for k in claves] for r in items]
        labels = KMeans(n_clusters=n_clusters, random_state=42, n_init=10).fit_predict(X)

        for r, lbl in zip(items, labels):
            r2 = r.copy()
            r2["cluster"] = int(lbl)
            out.append(r2)

    return out

# Resumen por fase y cluster (PARA MODIFICAR NOMBRAMIENTO DE ESTADOS SI LAS CONDICIONES DE LAS MAQUINAS VARIAN)
def resumen_clusters_por_fase(rows_clusterizadas, claves_features):
    resumen = {}

    for r in rows_clusterizadas:
        fase = r["fase"]
        cluster = r["cluster"]

        if cluster is None:
            continue

        resumen.setdefault(fase, {})
        resumen[fase].setdefault(cluster, {
            "count": 0,
            "features": {k: [] for k in claves_features},
            "t_inicio": []
        })

        resumen[fase][cluster]["count"] += 1
        resumen[fase][cluster]["t_inicio"].append(r["t_inicio"])

        for k in claves_features:
            resumen[fase][cluster]["features"][k].append(r[k])

    return resumen

# Determina a que estado corresponde cada cluster
def etiquetar_clusters(stats, umbral_apagado_rel=0.1):
    medias = {c: v["media"] for c, v in stats.items()}
    energias = {c: v["energia"] for c, v in stats.items()}

    def norm(d):
        mn, mx = min(d.values()), max(d.values())
        return {k: 0.0 if mx == mn else (v - mn) / (mx - mn) for k, v in d.items()}

    m_n = norm(medias)
    e_n = norm(energias)

    score = {c: 0.5 * m_n[c] + 0.5 * e_n[c] for c in stats}
    orden = sorted(score.items(), key=lambda x: x[1])

    etiquetas = {}
    cmin, smin = orden[0]
    etiquetas[cmin] = "apagado" if smin <= umbral_apagado_rel else "reposo_operativo"
    etiquetas[orden[-1][0]] = "trabajo_efectivo"

    for c, _ in orden[1:-1]:
        etiquetas[c] = "reposo_operativo"

    return etiquetas

# Agrega columna 'estado'a la tabla rows_clusterizadas
def asignar_estado(rows_clusterizadas, mapa_estados):

    rows_con_estado = []

    for r in rows_clusterizadas:
        fase = r.get("fase")
        cluster = r.get("cluster")

        estado = mapa_estados.get(fase, {}).get(cluster, "desconocido")

        r_out = r.copy()
        r_out["estado"] = estado

        rows_con_estado.append(r_out)

    return rows_con_estado

# 
def estado_global_maquina(estado_R, estado_S, estado_T):

    estados = [estado_R, estado_S, estado_T]

    # Si falta alguna fase o hay algo desconocido
    if any(e is None or e == "desconocido" for e in estados):
        return "indeterminado"

    # Apagado real: todas apagadas
    if estados.count("apagado") == 3:
        return "apagado"

    # Reposo operativo: todas en reposo
    if estados.count("reposo_operativo") == 3:
        return "reposo_operativo"

    # Trabajo efectivo: al menos 2 fases trabajando
    if estados.count("trabajo_efectivo") >= 1:
        return "trabajo_efectivo"

    # Cualquier transitorio raro
    return "indeterminado"

# Funcion detectar_picos mediante corriente
def detectar_picos(signal, timestamps, umbral_pico, ventana):

    n = len(signal)
    picos = [False] * n

    dt = [None] * n
    dI_dt = [None] * n

    for i in range(1, n):
        delta_t = (timestamps[i] - timestamps[i-1]).total_seconds()
        if delta_t > 0:
            dt[i] = delta_t
            dI_dt[i] = (signal[i] - signal[i-1]) / delta_t

    for i in range(1, n - ventana):
        if dI_dt[i] is not None and dI_dt[i] > umbral_pico:
            entorno = signal[i:i + ventana + 1]
            if signal[i + 1] == max(entorno):
                picos[i + 1] = True

    return picos

# Funcion que detecta picos de corriente en cada fase
def detectar_picos_3_fases(
    data,
    umbral_pico,
    ventana_pico
):

    timestamps = [m["timestamp"] for m in data]

    signal_R = [m["corriente_r"] for m in data]
    signal_S = [m["corriente_s"] for m in data]
    signal_T = [m["corriente_t"] for m in data]

    picos_R = detectar_picos(
        signal_R, timestamps, umbral_pico, ventana_pico
    )
    picos_S = detectar_picos(
        signal_S, timestamps, umbral_pico, ventana_pico
    )
    picos_T = detectar_picos(
        signal_T, timestamps, umbral_pico, ventana_pico
    )

    return {
        "R": [timestamps[i] for i, v in enumerate(picos_R) if v],
        "S": [timestamps[i] for i, v in enumerate(picos_S) if v],
        "T": [timestamps[i] for i, v in enumerate(picos_T) if v],
    }

# Determinacion de inicio y fin validos de los datos. Omite los picos producto del arranque o apagado de la maquina
def determinar_rango_valido(estados_finales, n_estable):

    # Inicio valido
    contador = 0
    idx_inicio = None

    for i, r in enumerate(estados_finales):
        estado_estable = r["estado_global"] in ("apagado", "reposo_operativo")

        if estado_estable:
            contador += 1
            if contador >= n_estable:
                idx_inicio = i
                break
        else:
            contador = 0

    # Fin valido
    contador = 0
    idx_fin = None

    for i in range(len(estados_finales) - 1, -1, -1):
        r = estados_finales[i]
        estado_estable = r["estado_global"] in ("apagado", "reposo_operativo")

        if estado_estable:
            contador += 1
            if contador >= n_estable:
                idx_fin = i
                break
        else:
            contador = 0

    # Fallback
    if idx_inicio is None:
        idx_inicio = 0

    if idx_fin is None:
        idx_fin = len(estados_finales) - 1

    if idx_inicio >= idx_fin:
        raise ValueError("No se pudo determinar una zona válida de operación")

    return idx_inicio, idx_fin

# Determinacion de EVENTOS
def detectar_eventos(estados_finales, idx_inicio, idx_fin, pausa_min, ventana_anti_apagado):
    eventos = []

    en_evento = False
    inicio = None
    contador_reposo = 0
    ultimo_reposo = None

    for i in range(idx_inicio, idx_fin + 1):
        r = estados_finales[i]

        apagado = r["estado_global"] == "apagado"
        reposo = r["estado_global"] == "reposo_operativo"
        trabajo = r["estado_global"] == "trabajo_efectivo"

        # Apagado corta evento
        if apagado:
            if en_evento:
                eventos.append({
                    "Fecha Inicio": inicio,
                    "Fecha Fin": r["t_inicio"]
                })
                en_evento = False
                inicio = None
            contador_reposo = 0
            ultimo_reposo = None
            continue

        # Guardar último reposo (fuera de evento)
        if reposo and not en_evento:
            ultimo_reposo = r["t_inicio"]

        # Inicio de evento
        if trabajo and not en_evento:
            #-------------------------------------------------------
            # Bloque anti-apagado
            t_actual = estados_finales[i]["t_inicio"]
            t_min = t_actual - timedelta(seconds=ventana_anti_apagado)

            hubo_apagado_reciente = False
            j = i - 1

            while j >= idx_inicio:
                t_j = estados_finales[j]["t_inicio"]

                # salimos si ya nos fuimos fuera de la ventana temporal
                if t_j < t_min:
                    break

                if estados_finales[j]["estado_global"] == "apagado":
                    hubo_apagado_reciente = True
                    break

                j -= 1

            if hubo_apagado_reciente:
                continue  # ignorar falso inicio de evento
            #-----------------------------------------------------------
            inicio = ultimo_reposo if ultimo_reposo else r["t_inicio"]
            en_evento = True
            contador_reposo = 0

        # Continuidad
        if en_evento and not reposo:
            contador_reposo = 0

        # Cierre normal
        if en_evento and reposo:
            contador_reposo += 1
            if contador_reposo >= pausa_min:
                fin = estados_finales[i - pausa_min]["t_inicio"]
                eventos.append({
                    "Fecha Inicio": inicio,
                    "Fecha Fin": fin
                })
                en_evento = False
                inicio = None
                contador_reposo = 0
                ultimo_reposo = r["t_inicio"]

    # Cerrar evento abierto
    if en_evento:
        eventos.append({
            "Fecha Inicio": inicio,
            "Fecha Fin": estados_finales[idx_fin]["t_inicio"]
        })

    return eventos

# Correccion del comienzo de los eventos mediante la deteccion de picos
from datetime import timedelta

def ajustar_inicio_eventos(eventos, picos, ventana_pico_inicio):
    eventos_ajustados = []

    for ev in eventos:
        t_ini = ev["Fecha Inicio"]
        t_min = t_ini - timedelta(seconds=ventana_pico_inicio)

        # picos previos dentro de la ventana
        picos_previos = [
            t for t in picos
            if t_min <= t < t_ini
        ]

        ev_out = ev.copy()

        if picos_previos:
            ev_out["Fecha Inicio Ajustada"] = max(picos_previos)
        else:
            ev_out["Fecha Inicio Ajustada"] = t_ini

        eventos_ajustados.append(ev_out)

    return eventos_ajustados

def exportar_eventos_a_csv(eventos, path_csv):
    if not eventos:
        print("No hay eventos para exportar")
        return

    # Tomamos las claves del primer evento
    fieldnames = eventos[0].keys()

    with open(path_csv, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for ev in eventos:
            # Convertir datetime a string ISO
            ev_out = {}
            for k, v in ev.items():
                if hasattr(v, "isoformat"):
                    ev_out[k] = v.isoformat()
                else:
                    ev_out[k] = v
            writer.writerow(ev_out)

    print(f"Eventos exportados a {path_csv}")
#================================================================================================================================
# Ejecuta el cálculo de analíticas para un dispositivo específico.
def calcular_analiticas(data, n_estable, pausa_min, ventana_anti_apagado, ventana_pico_inicio):

    win = 10
    eventos_ajustados = []

    try:
        # 1) Ventanas
        ventanas = generar_ventanas(data, win)
        ventanas = ventanas[:1000]  # LIMITADOR para performance

        # 2) Features crudos
        rows = []
        for ventana in ventanas:
            rows.extend(extraer_features_por_fase(ventana))

        if not rows:
            return []

        # 3) Normalizar
        rows_norm = normalizar_features_por_fase(rows, claves_features)

        # 4) Clustering
        rows_clusterizadas = clusterizar_por_fase(rows_norm, claves_features)

        # 5) Resumen
        resumen = resumen_clusters_por_fase(rows_clusterizadas, claves_features)

        # 6) Etiquetar clusters
        mapa_estados = {}
        for fase, clusters in resumen.items():
            stats = {
                cid: {
                    "media": sum(info["features"]["media"]) / info["count"],
                    "energia": sum(info["features"]["energia"]) / info["count"]
                }
                for cid, info in clusters.items()
            }
            mapa_estados[fase] = etiquetar_clusters(stats)

        # 7) Asignar estado
        rows_con_estado = asignar_estado(rows_clusterizadas, mapa_estados)

        # 8) Combinar fases
        por_tiempo = {}
        for r in rows_con_estado:
            t = r["t_inicio"]
            por_tiempo.setdefault(t, {})
            por_tiempo[t][f"estado_{r['fase']}"] = r["estado"]

        estados_finales = []
        for t, est in sorted(por_tiempo.items()):
            estado_R = est.get("estado_r")
            estado_S = est.get("estado_s")
            estado_T = est.get("estado_t")

            estados_finales.append({
                "t_inicio": t,
                "estado_R": estado_R,
                "estado_S": estado_S,
                "estado_T": estado_T,
                "estado_global": estado_global_maquina(
                    estado_R, estado_S, estado_T
                )
            })

        for r in estados_finales[:200]:  # mirá las primeras 20
            print(
                r["t_inicio"],
                r["estado_R"],
                r["estado_S"],
                r["estado_T"],
                "=>",
                r["estado_global"]
            )

        # 9) Rango válido
        idx_inicio, idx_fin = determinar_rango_valido(estados_finales, n_estable)

        # 10) Eventos
        eventos = detectar_eventos(
            estados_finales,
            idx_inicio,
            idx_fin,
            pausa_min,
            ventana_anti_apagado
        )

        # 11) Ajuste con picos
        picos_dict = detectar_picos_3_fases(data, umbral_pico, ventana_pico)
        picos = picos_dict["R"] + picos_dict["S"] + picos_dict["T"]

        eventos_ajustados = ajustar_inicio_eventos(
            eventos,
            picos,
            ventana_pico_inicio
        )

        return eventos_ajustados

    except Exception:
        logger.exception("Error al procesar los datos")
        return []

if __name__ == "__main__":

    ZONA_HORARIA = pytz.timezone("UTC")

    CSV_PATH = r"C:\Users\HP Spectre X360\Desktop\MARIANA\COLLOQUIA_2025\Analisis_Powermeter\Pruebas\12-1-2026_prueba\12-1-2026_PLE1.csv"

    pausa_min = 3
    ventana_anti_apagado = 13
    ventana_pico_inicio = 5
    n_estable = 5

    try:
        print("== Iniciando analítica powermeter ==")

        # 1) Cargar datos 
        print("Cargando datos desde CSV...")
        data = cargar_data_desde_csv(CSV_PATH)

        if not data:
            raise RuntimeError("No se cargaron datos")

        # 3) Ejecutar analítica principal
        print("Ejecutando analíticas...")
        eventos = calcular_analiticas( data, n_estable, pausa_min, ventana_anti_apagado, ventana_pico_inicio)
        # -----------------------------------------
        # RESULTADOS
        # -----------------------------------------
        print(f"Eventos detectados: {len(eventos)}")

        if eventos:
            exportar_eventos_a_csv(eventos, r"C:\Users\HP Spectre X360\Desktop\MARIANA\COLLOQUIA_2025\Analisis_Powermeter\eventos_powermeter.csv")

        print("== Fin de ejecución ==")

    except Exception as e:
        print("ERROR en ejecución:", str(e))
