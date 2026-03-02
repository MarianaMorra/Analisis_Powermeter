import os
import sys
import logging
from datetime import date, timedelta, datetime
import psycopg2
import pandas as pd
import numpy as np

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USR = os.environ.get("DB_USR", "db_analiticas")
DB_PASS = os.environ.get("DB_PASS", "AnaliticasDB25")
DB_NAME = os.environ.get("DB_NAME", "dbSQLPlataforma")

# Constantes
I_MIN_ACTIVA = 5.0  # A mínima para considerar actividad

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)

# Conexion con la DB
def connect_db():
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USR,
            password=DB_PASS,
        )
        logger.info("Conectado a la base de datos")
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Error de conexión DB: {e}")
        return None

# Obtengo los datos
def obtener_datos(conn, numero_serie, desde, hasta):
    query = """
        SELECT
            temporal_placa,
            corriente_r,
            corriente_s,
            corriente_t
        FROM data_instantanea
        WHERE numero_serie = %s
          AND temporal_placa >= %s
          AND temporal_placa <  %s
        ORDER BY temporal_placa
    """

    with conn.cursor() as cur:
        cur.execute(query, (numero_serie, desde, hasta))
        rows = cur.fetchall()

    df = pd.DataFrame(
        rows,
        columns=[
            "temporal_placa",
            "corriente_r",
            "corriente_s",
            "corriente_t",
        ],
    )

    if df.empty:
        return df

    df["temporal_placa"] = pd.to_datetime(df["temporal_placa"], utc=True)
    for c in ["corriente_r", "corriente_s", "corriente_t"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    return df

# Obtengo id_activo mediante numero_serie
def obtener_id_activo(conn, numero_serie):
    query = """
        SELECT a.id_activo
        FROM dispositivos d
        JOIN activos_dispositivos ad
          ON ad.id_dispositivo = d.id_dispositivo
        JOIN activos a
          ON a.id_activo = ad.id_activo
        WHERE d.numero_serie = %s
          AND ad.fecha_baja IS NULL
    """

    with conn.cursor() as cur:
        cur.execute(query, (numero_serie,))
        row = cur.fetchone()

    return row[0] if row else None

# Guardo la media diaria en su tabla correspondiente
def insertar_acumulado_diario(conn, id_activo, fase, fecha_carga, i_media):
    query = """
        INSERT INTO acumulado_corriente
            (id_activo, fase, fecha_carga, i_media)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id_activo, fase, fecha_carga)
        DO UPDATE SET i_media = EXCLUDED.i_media
    """

    with conn.cursor() as cur:
        cur.execute(
            query,
            (int(id_activo), fase, fecha_carga, float(i_media)),
        )

# Obtengo la media diaria de corriente por fase y por maquina
def procesar_acumulado_diario(df, conn, id_activo):
    if df.empty:
        logger.warning("No hay datos para procesar")
        return

    df = df.sort_values("temporal_placa")
    df["fecha"] = df["temporal_placa"].dt.timestamp

    for fecha, grupo in df.groupby("fecha"):
        for col, fase in [
            ("corriente_r", "R"),
            ("corriente_s", "S"),
            ("corriente_t", "T"),
        ]:
            media = grupo[col].mean()

            if media is None or np.isnan(media):
                continue

            if media < I_MIN_ACTIVA:
                continue

            insertar_acumulado_diario(
                conn,
                id_activo,
                fase,
                fecha,
                media,
            )

# Calculo el percentil 90 de manera mensual
def calcular_p90_mensual():
    hoy = date.today()
    primer_dia_mes = hoy.replace(day=1)
    ultimo_dia_mes_anterior = primer_dia_mes - timedelta(days=1)

    mes_inicio = ultimo_dia_mes_anterior.replace(day=1)
    mes_fin = primer_dia_mes

    logger.info(f"Calculando P90 mensual {mes_inicio:%Y-%m}")

    conn = connect_db()
    if not conn:
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO historico_media_mensual (
                    id_activo,
                    fase,
                    media_p90,
                    fecha_carga
                )
                SELECT
                    id_activo,
                    fase,
                    percentile_cont(0.9)
                        WITHIN GROUP (ORDER BY i_media),
                    now()
                FROM acumulado_corriente
                WHERE fecha >= %s
                AND fecha <  %s
                GROUP BY id_activo, fase
                ON CONFLICT (id_activo, fase)
                DO UPDATE
                SET
                    media_p90  = EXCLUDED.media_p90,
                    fecha_carga = EXCLUDED.fecha_carga;
                """, 
                (mes_inicio, mes_fin)   
            ) 
        conn.commit()
        logger.info("P90 mensual calculado correctamente")

    except Exception:
        conn.rollback()
        logger.exception("Error en cálculo mensual")

    finally:
        conn.close()

# Funcion Principal
if __name__ == "__main__":

    if len(sys.argv) < 2:
        logger.error("Uso: python calcular_media_historica.py [diario|mensual]")
        sys.exit(1)

    modo = sys.argv[1]

    if modo == "mensual":
        calcular_p90_mensual()
        sys.exit(0)

    if modo == "diario":
        if len(sys.argv) != 5:
            logger.error(
                "Uso diario: python calcular_media_historica.py diario <numero_serie> <desde> <hasta>"
            )
            sys.exit(1)

        numero_serie = sys.argv[2]
        desde = pd.to_datetime(sys.argv[3], utc=True)
        hasta = pd.to_datetime(sys.argv[4], utc=True)

        conn = connect_db()
        if not conn:
            sys.exit(1)

        try:
            df = obtener_datos(conn, numero_serie, desde, hasta)
            id_activo = obtener_id_activo(conn, numero_serie)

            if id_activo is None:
                raise ValueError("No se encontró id_activo para el numero_serie")

            procesar_acumulado_diario(df, conn, id_activo)
            conn.commit()

            logger.info("Acumulado diario procesado OK")

        except Exception:
            conn.rollback()
            logger.exception("Error en proceso diario")
            sys.exit(1)

        finally:
            conn.close()

        sys.exit(0)

    logger.error(f"Modo inválido: {modo}")
    sys.exit(1)