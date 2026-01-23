import os
import csv
import sys
import logging
from datetime import datetime, timedelta

import pytz
from sklearn.cluster import KMeans

# =========================================================
# LOGGING
# =========================================================
stdout_handler = logging.StreamHandler(sys.stdout)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[stdout_handler],
)
logger = logging.getLogger(__name__)

# =========================================================
# PARÁMETROS GLOBALES
# =========================================================
umbral_pico = 0.4
ventana_pico = 2

fases = {
    "r": {"i": "corriente_r", "p": "potencia_a_r"},
    "s": {"i": "corriente_s", "p": "potencia_a_s"},
    "t": {"i": "corriente_t", "p": "potencia_a_t"},
}

claves_features = ["media", "std", "pendiente", "energia"]

# =========================================================
# CARGA DE DATOS
# =========================================================
def cargar_data_desde_csv(path_csv, limit=None):
    data = []

    with open(path_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if limit is not None and i >= limit:
                break

            data.append({
                "timestamp": datetime.fromisoformat(row["temporal_placa"]),
                "corriente_r": float(row["corriente_r"]),
                "corriente_s": float(row["corriente_s"]),
                "corriente_t": float(row["corriente_t"]),
                "potencia_a_r": float(row["potencia_a_r"]),
                "potencia_a_s": float(row["potencia_a_s"]),
                "potencia_a_t": float(row["potencia_a_t"]),
            })

            if i % 50000 == 0 and i > 0:
                print(f"{i} filas cargadas...")

    return data
# =========================================================
# VENTANAS TEMPORALES
# =========================================================
def generar_ventanas(data, duracion_s, paso_s=None, key_ts="timestamp"):
    if not data:
        return []

    if paso_s is None:
        paso_s = duracion_s

    ventanas = []
    dur = timedelta(seconds=duracion_s)
    paso = timedelta(seconds=paso_s)

    inicio = data[0][key_ts]
    fin_total = data[-1][key_ts]

    j0 = 0
    while inicio <= fin_total:
        fin = inicio + dur

        while j0 < len(data) and data[j0][key_ts] < inicio:
            j0 += 1

        j = j0
        ventana = []
        while j < len(data) and data[j][key_ts] < fin:
            ventana.append(data[j])
            j += 1

        if ventana:
            ventanas.append(ventana)

        inicio += paso

    return ventanas

# =========================================================
# FEATURES
# =========================================================
def extraer_features_por_fase(ventana):
    if len(ventana) < 2:
        return []

    features = []
    timestamps = [m["timestamp"] for m in ventana]

    dt = [0.0]
    for i in range(1, len(timestamps)):
        dt.append((timestamps[i] - timestamps[i-1]).total_seconds())

    for fase, cols in fases.items():
        segI = [m[cols["i"]] for m in ventana]
        segP = [m[cols["p"]] for m in ventana]

        energia = sum(p * d for p, d in zip(segP, dt))
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

# =========================================================
# NORMALIZACIÓN
# =========================================================
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

# =========================================================
# CLUSTERING
# =========================================================
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

# =========================================================
# ETIQUETADO DE CLUSTERS
# =========================================================
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

# =========================================================
# ESTADO GLOBAL
# =========================================================
def estado_global_maquina(r, s, t):
    estados = [r, s, t]
    if any(e is None or e == "desconocido" for e in estados):
        return "indeterminado"
    if estados.count("apagado") == 3:
        return "apagado"
    if estados.count("reposo_operativo") == 3:
        return "reposo_operativo"
    if estados.count("trabajo_efectivo") >= 2:
        return "trabajo_efectivo"
    return "indeterminado"

# =========================================================
# ANALÍTICA PRINCIPAL
# =========================================================
def calcular_analiticas(data, n_estable, pausa_min, ventana_anti_apagado, ventana_pico_inicio):

    try:
        ventanas = generar_ventanas(data, duracion_s=5)[:200]

        rows = []
        for v in ventanas:
            rows.extend(extraer_features_por_fase(v))
        if not rows:
            return []

        rows_norm = normalizar_features_por_fase(rows, claves_features)
        rows_clust = clusterizar_por_fase(rows_norm, claves_features)

        # Resumen y estados
        resumen = {}
        for r in rows_clust:
            if r["cluster"] is None:
                continue
            resumen.setdefault(r["fase"], {}).setdefault(r["cluster"], {"media": [], "energia": []})
            resumen[r["fase"]][r["cluster"]]["media"].append(r["media"])
            resumen[r["fase"]][r["cluster"]]["energia"].append(r["energia"])

        mapa = {}
        for fase, clusters in resumen.items():
            stats = {
                c: {
                    "media": sum(v["media"]) / len(v["media"]),
                    "energia": sum(v["energia"]) / len(v["energia"]),
                }
                for c, v in clusters.items()
            }
            mapa[fase] = etiquetar_clusters(stats)

        rows_estado = []
        for r in rows_clust:
            r2 = r.copy()
            r2["estado"] = mapa.get(r["fase"], {}).get(r["cluster"], "desconocido")
            rows_estado.append(r2)

        por_t = {}
        for r in rows_estado:
            por_t.setdefault(r["t_inicio"], {})[f"estado_{r['fase']}"] = r["estado"]

        estados_finales = []
        for t, e in sorted(por_t.items()):
            estados_finales.append({
                "t_inicio": t,
                "estado_global": estado_global_maquina(
                    e.get("estado_r"), e.get("estado_s"), e.get("estado_t")
                )
            })

        return estados_finales

    except Exception:
        logger.exception("Error en analítica")
        return []
    
# ============================================================
# EXPORTAR DATOS A .CSV
# ============================================================
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

# =========================================================
# MAIN
# =========================================================
if __name__ == "__main__":

    CSV_PATH = r"C:\Users\HP Spectre X360\Desktop\MARIANA\COLLOQUIA_2025\Analisis_Powermeter\data\PLE1\12-1-2026\12-1-2026_PLE1.csv"

    print("== Iniciando analítica powermeter ==")
    data = cargar_data_desde_csv(CSV_PATH, limit=100000)

    eventos = calcular_analiticas(
        data,
        n_estable=16,
        pausa_min=13,
        ventana_anti_apagado=60,
        ventana_pico_inicio=100,
    )

    print(f"Registros finales: {len(eventos)}")
    
    if eventos:
        exportar_eventos_a_csv(eventos, r"C:\Users\HP Spectre X360\Desktop\MARIANA\COLLOQUIA_2025\Analisis_Powermeter\eventos_powermeter.csv")


