import pandas as pd
import psycopg2
import matplotlib.pyplot as plt
from datetime import datetime
import pytz
import os
import matplotlib

matplotlib.use("Agg")  # backend sin UI

# conexión
conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "localhost"),
    database=os.environ.get("DB_NAME", "dbSQLPlataforma"),
    user=os.environ.get("DB_USR", "db_analiticas"),
    password=os.environ.get("DB_PASS", "AnaliticasDB25"),
)

numero_serie = "28562F60A8D8"
#numero_serie = "28562F612CC4"

desde = datetime(2026, 1, 9, 13, 30, tzinfo=pytz.UTC)
hasta = datetime(2026, 1, 9, 15, 30, tzinfo=pytz.UTC)

q_corriente = """
SELECT temporal_placa, corriente_r, corriente_s, corriente_t
FROM data_instantanea
WHERE numero_serie = %s
  AND temporal_placa BETWEEN %s AND %s
ORDER BY temporal_placa;
"""

df_i = pd.read_sql(q_corriente, conn, params=(numero_serie, desde, hasta))

q_eventos = """
SELECT inicio, fin
FROM analiticas_plegadoras
WHERE numero_serie = %s
  AND inicio < %s
  AND fin > %s
ORDER BY inicio;
"""

df_ev = pd.read_sql(q_eventos, conn, params=(numero_serie, hasta, desde))

plt.figure(figsize=(14,6))

plt.plot(df_i["temporal_placa"], df_i["corriente_r"], label="R", alpha=0.7)
plt.plot(df_i["temporal_placa"], df_i["corriente_s"], label="S", alpha=0.7)
plt.plot(df_i["temporal_placa"], df_i["corriente_t"], label="T", alpha=0.7)

for _, ev in df_ev.iterrows():
    plt.axvspan(
        ev["inicio"],
        ev["fin"],
        color="red",
        alpha=0.2
    )

plt.legend()
plt.title("Corriente vs tiempo con eventos detectados")
plt.xlabel("Tiempo")
plt.ylabel("Corriente")
plt.tight_layout()
plt.savefig("corriente_eventos_08_15_{}.png".format(numero_serie), dpi=150)



