import pandas as pd
from datetime import timedelta
import psycopg2
import os
import logging
from psycopg2.extras import DictCursor
from datetime import datetime, timedelta
import time
import pytz
import sys

# Cargar las variables de entorno
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USR = os.environ.get("DB_USR", "db_analiticas")
DB_PASS = os.environ.get("DB_PASS", "AnaliticasDB25")
DB_NAME = os.environ.get("DB_NAME", "dbSQLPlataforma")
NAME_ANALIITICAS = os.environ.get("NAME_ANALITICAS", "PLEGADORA_ELECTRICO")
stdout_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[stdout_handler],
)

logger = logging.getLogger(__name__)

# Constantes
def obtener_id_analitica(conn, nombre_analitica):
    query = """
    SELECT id
    FROM analiticas
    WHERE nombre = %s
      AND fecha_baja IS NULL
    """
    with conn.cursor() as cur:
        cur.execute(query, (nombre_analitica,))
        row = cur.fetchone()

    if not row:
        raise ValueError(f"No se encontró analítica: {nombre_analitica}")

    return row[0]

# Conexión a la base de datos.
def connect_db():
    try:
        connection = psycopg2.connect(
            host=DB_HOST, database=DB_NAME, user=DB_USR, password=DB_PASS
        )
        logger.info("Conexión a la base de datos establecida.")
        return connection
    except psycopg2.OperationalError as e:
        logger.error(f"Error al conectar a la base de datos: {e}")
        return None

# Obtiene las placas con la analítica de bobinas habilitada
def obtener_placas_habilitadas(conn, id_analitica):
    query = """
    SELECT d.id_dispositivo, d.numero_serie, da.fecha_alta, da.fecha_baja
    FROM dispositivos_analiticas da
    JOIN dispositivos d ON da.id_dispositivo = d.id_dispositivo
    WHERE da.id_analitica = %s
      AND (da.fecha_baja IS NULL) AND (d.fecha_baja IS NULL);
    """
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(query, (id_analitica,))
        return cur.fetchall()

# Obtener configuracon de la analitica de bobinas
def obtener_configuracion_analitica(conn, numero_serie):
    query = """
    SELECT tiempo_ejecucion, win_s, ventana_media_s, tolerancia_rel, umbral_delta, tiempo_estable_s, duracion_max_evento
    FROM configuracion_plegadora
    WHERE numero_serie = %s
    ORDER BY fecha_actualizacion DESC, id DESC
    LIMIT 1;
    """
    with conn.cursor() as cur:
        cur.execute(query, (numero_serie,))
        row = cur.fetchone()

    if not row:
        raise RuntimeError(
            f"No existe configuración activa para la plegadora {numero_serie}"
        )
    return row

# obener cual fue la ultima fecha analizada 
def obtener_ultimo_hasta_ejecutado(conn, numero_serie, id_analitica, desde, hasta):
    query = """
    SELECT MAX(hasta) 
    FROM observaciones_analiticas 
    WHERE id_analitica = %s AND numero_serie = %s
    AND hasta BETWEEN %s AND %s;
    """
    with conn.cursor() as cur:
        cur.execute(query, (id_analitica, numero_serie, desde, hasta))
        ultimo_hasta = cur.fetchone()[0]

    return ultimo_hasta if ultimo_hasta else desde

def obtener_datos_desde_hasta(conn, numero_serie, desde, hasta):
    query = """
    SELECT
        "temporal_placa",
        "corriente_r",
        "corriente_s",
        "corriente_t"
    FROM data_instantanea
    WHERE numero_serie = %s
    AND temporal_placa BETWEEN %s AND %s
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
        ]
    )

    df["temporal_placa"] = pd.to_datetime(df["temporal_placa"], utc=True)
    df["corriente_r"] = pd.to_numeric(df["corriente_r"], errors="coerce")
    df["corriente_s"] = pd.to_numeric(df["corriente_s"], errors="coerce")
    df["corriente_t"] = pd.to_numeric(df["corriente_t"], errors="coerce")

    return df
    
# Inserta una observación en la tabla de observaciones_analiticas
def insertar_observacion_analitica(
    conn,
    id_analitica,
    numero_serie,
    desde,
    hasta,
    observacion
):
    query = """
        INSERT INTO observaciones_analiticas (
            id_analitica,
            numero_serie,
            desde,
            hasta,
            observacion,
            fecha
        )
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP);
    """

    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                id_analitica,
                numero_serie,
                desde,
                hasta,
                observacion
            )
        )


# Insertar eventos de plegadora en analiticas_plegadoras
def upsert_analiticas_plegadoras(
    conn,
    numero_serie,
    inicio,
    fin,
    duracion,
    valor
):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO analiticas_plegadoras (
                numero_serie,
                inicio,
                fin,
                duracion,
                valor,
                temporal_server
            )
            VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """, (
            numero_serie,
            inicio,
            fin,
            duracion,
            valor
        ))

# Insertar alarmas de evento
def insertar_alarma_evento(
    conn,
    causa,
    fecha_inicio,
    fecha_fin,
    nivel_nombre,
    estado_nombre,
    lista_nombre,
    numero_serie
):
    query = """
        INSERT INTO alarmas (
            id_activo,
            id_nivel_alarma,
            id_estado_alarma,
            id_lista_alarma,
            causa,
            fecha_inicio,
            fecha_fin
        )
        SELECT
            va.id_activo,
            na.id,
            ea.id,
            la.id,
            %s,
            %s,
            %s
        FROM vista_activo_por_serie_y_fecha va
        JOIN niveles_alarmas na ON na.nombre = %s
        JOIN estados_alarmas ea ON ea.nombre = %s
        JOIN listas_alarmas la ON la.nombre = %s
        WHERE va.numero_serie = %s
          AND va.fecha_alta <= %s
          AND (va.fecha_baja IS NULL OR %s <= va.fecha_baja)
    """

    with conn.cursor() as cur:
        cur.execute(query, (
            causa,
            fecha_inicio,
            fecha_fin,
            nivel_nombre,
            estado_nombre,
            lista_nombre,
            numero_serie,
            fecha_inicio,
            fecha_inicio
        ))
     
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
    tolerancia_rel,     # ±10% respecto a la media
    tiempo_estable_s    # 2 minutos
):
    """
    Consolida eventos por fase en eventos de máquina.
    El evento termina cuando la corriente se mantiene estable
    alrededor de su valor medio durante al menos tiempo_estable_s.
    """
    # ordenar una sola vez para poder indexar rápido por tiempo
    df = df.sort_values("temporal_placa").reset_index(drop=True)

    # vector de tiempos para buscar índices (binary search)
    times = df["temporal_placa"].to_numpy()

    eventos_fase = sorted(eventos_fase, key=lambda x: x["inicio"])
    eventos_maquina = []
    actual = None

    col = f"corriente_{fase}"

    for ev in eventos_fase:

        if actual is None:
            actual = {
                "inicio": ev["inicio"],
                "fin": ev["fin"],
                "fases": {ev["fase"]}
            }
            continue

        # superposición → mismo evento
        if ev["inicio"] <= actual["fin"]:
            actual["fin"] = max(actual["fin"], ev["fin"])
            actual["fases"].add(ev["fase"])
            continue

        # ---- NO hay superposición: evaluar si el evento anterior terminó ----

        # recortar DF al rango candidato
        t_ini = actual["fin"]
        t_fin = actual["fin"] + pd.Timedelta(minutes=5)

        i_ini = times.searchsorted(t_ini, side="left")
        i_fin = times.searchsorted(t_fin, side="right")

        df_ev = df.iloc[i_ini:i_fin].copy()

        if df_ev.empty:
            # sin datos → cerramos conservadoramente
            eventos_maquina.append(actual)
            actual = {
                "inicio": ev["inicio"],
                "fin": ev["fin"],
                "fases": {ev["fase"]}
            }
            continue

        # promedio móvil
        df_ev["media"] = df_ev[col].rolling(
            window=ventana_media_s,
            min_periods=ventana_media_s
        ).mean()

        # condición de estabilidad
        df_ev["estable"] = (
            (df_ev[col] - df_ev["media"]).abs()
            <= df_ev["media"] * tolerancia_rel
        )

        # cálculo del delta temporal medio
        dt = df_ev["temporal_placa"].diff().median().total_seconds()

        contador = 0
        fin_estable = None

        for i in range(len(df_ev)):
            if df_ev["estable"].iloc[i]:
                contador += 1
                if contador * dt >= tiempo_estable_s:
                    fin_estable = df_ev["temporal_placa"].iloc[i]
                    break
            else:
                contador = 0

        if fin_estable:
            # el evento terminó realmente
            actual["fin"] = fin_estable
            eventos_maquina.append(actual)

            # abrir nuevo evento
            actual = {
                "inicio": ev["inicio"],
                "fin": ev["fin"],
                "fases": {ev["fase"]}
            }
        else:
            # no hubo estabilidad → seguimos en el mismo evento
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

# Calcular los eventos de las plegadoras a partir de la observacion de la corriente
def calcular_analiticas(
    df_m,
    win_s,
    ventana_media_s,
    tolerancia_rel,
    umbral_delta,
    tiempo_estable_s
):
    if df_m.empty:
        return []
      
    df_m = recortar_rango_operativo_por_corriente(
            df_m,
            fase="r",               # o la fase que prefieras como referencia
            umbral_corriente=umbral_delta,  # coherente con tu detección
            min_muestras=2          # para picos, 2 suele andar mejor que 3
        )

    if df_m.empty:
        return []
        
    ventanas = generar_ventanas(df_m, win_s)

    deltas = {
        "R": delta_corriente_por_ventana(ventanas, fase="r"),
        "S": delta_corriente_por_ventana(ventanas, fase="s"),
        "T": delta_corriente_por_ventana(ventanas, fase="t"),
    }

    eventos_por_fase = {}

    for fase, df_delta in deltas.items():
        registros = [
            {
                "t_inicio": row.temporal_placa,
                "delta_corriente": row.delta_corriente
            }
            for row in df_delta.itertuples(index=False)
        ]

        eventos_por_fase[fase] = detectar_eventos_simples(
            registros,
            umbral_delta
        )

    eventos_fase = unir_eventos_por_fase(
        eventos_por_fase["R"],
        eventos_por_fase["S"],
        eventos_por_fase["T"]
    )

    eventos_cerrados= consolidar_eventos_maquina(
        eventos_fase=eventos_fase,
        df=df_m,
        fase='r',
        ventana_media_s=ventana_media_s,
        tolerancia_rel=tolerancia_rel,
        tiempo_estable_s=tiempo_estable_s
    )

    return eventos_cerrados

# Funcion principal
if __name__ == "__main__":

    zona_horaria = pytz.timezone("UTC")
    VENTANA_ANALISIS = timedelta(hours=1)

    while True:
        conn = connect_db()
        if not conn:
            exit(1)

        try:
            ID_PLEGADORA_ELECTRICO = obtener_id_analitica(
                conn, "PLEGADORA_ELECTRICO"
            )
            placas = obtener_placas_habilitadas(
                conn, ID_PLEGADORA_ELECTRICO
            )

            tiempo_sleep = 60  # fallback

            for placa in placas:
                numero_serie = placa["numero_serie"]

                # ---------------- RANGO BASE ----------------
                desde_base = placa["fecha_alta"]

                ahora = datetime.now(zona_horaria)
                hasta_teorico = (
                    ahora
                    if placa["fecha_baja"] is None
                    else min(placa["fecha_baja"], ahora)
                )

                # ---------------- CURSOR (UNA SOLA VEZ) ----------------
                desde = obtener_ultimo_hasta_ejecutado(
                    conn,
                    numero_serie,
                    ID_PLEGADORA_ELECTRICO,
                    desde_base,
                    hasta_teorico
                )

                # Ventana fija de 1 hora
                hasta = min(desde + VENTANA_ANALISIS, hasta_teorico)

                logger.info(
                    f"[RANGO] serie={numero_serie} desde={desde} hasta={hasta}"
                )

                if desde >= hasta:
                    logger.info(
                        f"[DEBUG] serie={numero_serie} sin rango para procesar"
                    )
                    continue

                # ---------------- DATOS ----------------
                df = obtener_datos_desde_hasta(
                    conn,
                    numero_serie,
                    desde,
                    hasta
                )

                logger.info(
                    f"[DEBUG] serie={numero_serie} filas_df={len(df)}"
                )

                if df.empty:
                    insertar_observacion_analitica(
                        conn,
                        ID_PLEGADORA_ELECTRICO,
                        numero_serie,
                        desde,
                        desde,
                        "Sin datos en el rango"
                    )
                    continue

                # ---------------- CONFIG ----------------
                configuracion = obtener_configuracion_analitica(
                    conn,
                    numero_serie
                )

                (
                    tiempo_ejecucion,
                    win_s,
                    ventana_media_s,
                    tolerancia_rel,
                    umbral_delta,
                    tiempo_estable_s,
                    duracion_max_evento
                ) = configuracion

                tiempo_sleep = int(float(tiempo_ejecucion))

                # ---------------- ANALITICA ----------------
                eventos_cerrados = calcular_analiticas(
                    df,
                    win_s,
                    ventana_media_s,
                    tolerancia_rel,
                    umbral_delta,
                    tiempo_estable_s
                )

                logger.info(
                    f"[DEBUG] serie={numero_serie} eventos_detectados={len(eventos_cerrados)}"
                )

                # ---------------- CURSOR REAL ----------------
                fecha_hasta = df["temporal_placa"].max()
                cantidad_eventos = 0

                for ev in eventos_cerrados:
                    inicio = ev["inicio"]
                    fin = ev["fin"]

                    duracion = int((fin - inicio).total_seconds())
                    if duracion <= 0 or duracion > duracion_max_evento:
                        continue

                    valor = ev.get("valor") or len(ev["fases"])

                    upsert_analiticas_plegadoras(
                        conn,
                        numero_serie,
                        inicio,
                        fin,
                        duracion,
                        valor
                    )

                    cantidad_eventos += 1
                    fecha_hasta = max(fecha_hasta, fin)

                # ---------------- OBSERVACION ----------------
                insertar_observacion_analitica(
                    conn,
                    ID_PLEGADORA_ELECTRICO,
                    numero_serie,
                    desde,
                    fecha_hasta,
                    f"Eventos eléctricos detectados: {cantidad_eventos}"
                )

            conn.commit()

            logger.info(
                f"Esperando {tiempo_sleep} segundos antes de la próxima ejecución..."
            )
            time.sleep(tiempo_sleep)

        except Exception as e:
            conn.rollback()
            logger.error(f"Error en el proceso: {e}")
            logger.info(
                "Esperando 30 minutos antes de la próxima ejecución..."
            )
            time.sleep(1800)

        finally:
            conn.close()
