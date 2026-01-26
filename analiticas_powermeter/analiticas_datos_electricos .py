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
def obtener_configuracion_analitica(conn):
    query = """
        SELECT win_s, ventana_media_s, tolerancia_rel, umbral_delta, tiempo_estable_s, duracion_max_evento
        FROM configuracion_plegadora
        ORDER BY ID DESC LIMIT 1;
    """
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(query)
        resultado = cur.fetchone()
        if resultado and all(resultado[campo] is not None for campo in resultado.keys()):
                return resultado
        return None

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
            SELECT id, id_estado, inicio, fin
            FROM data_procesada
            WHERE numero_serie=%s AND inicio>%s AND fin<%s AND fin IS NOT NULL
            ORDER BY inicio;
            """
    with conn.cursor() as cur:
        cur.execute(query, (numero_serie, desde, hasta))
        return cur.fetchall()
    
# Inserta una observación en la tabla de observaciones_analiticas
def insertar_observacion_analitica(
    conn, id_analitica, observacion, numero_serie, desde, hasta
):
    query = """
    INSERT INTO observaciones_analiticas (id_analitica, observacion, fecha, numero_serie, desde, hasta) 
    VALUES (%s, %s, CURRENT_TIMESTAMP, %s, %s, %s);
    """
    with conn.cursor() as cur:
        cur.execute(query, (id_analitica, observacion, numero_serie, desde, hasta))
    conn.commit()

# Insertar pasos en bobinas
def insertar_paso_evento(
    conn,
    id_evento,
    nombre,
    valor,
    id_inicio=None,
    observacion=None
):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pasos_eventos (
                id_analitica_evento,
                nombre,
                valor_paso,
                id_inicio,
                observacion
            )
            VALUES (%s, %s, %s, %s, %s)
        """, (
            id_evento,
            nombre,
            valor,
            id_inicio,
            observacion
        ))

# Insertar analiticas de bobiba con sus pasos asociados
def upsert_analitica_evento(
    conn,
    id_tipo,
    identificador_activo,
    inicio,
    fin,
    valor,
    id_inicio=None,
    id_fin=None
):
    """
    Inserta un evento si no hay uno abierto.
    Actualiza el evento abierto si ya existe.
    """

    with conn.cursor() as cur:
        # Buscar evento abierto
        cur.execute("""
            SELECT id, inicio
            FROM analiticas_eventos
            WHERE identificador_activo = %s
              AND id_tipo = %s
              AND fin IS NULL
            ORDER BY inicio DESC
            LIMIT 1
        """, (identificador_activo, id_tipo))

        row = cur.fetchone()

        if row:
            # Ya hay evento abierto → UPDATE
            id_evento = row[0]

            cur.execute("""
                UPDATE analiticas_eventos
                SET
                    fin = %s,
                    valor = %s,
                    id_fin = %s
                WHERE id = %s
            """, (fin, valor, id_fin, id_evento))

        else:
            # 3No hay evento abierto → INSERT
            cur.execute("""
                INSERT INTO analiticas_eventos (
                    identificador_activo,
                    id_tipo,
                    inicio,
                    fin,
                    valor,
                    id_inicio,
                    id_fin,
                    temporal_server
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                RETURNING id
            """, (
                identificador_activo,
                id_tipo,
                inicio,
                fin,
                valor,
                id_inicio,
                id_fin
            ))

            id_evento = cur.fetchone()[0]

    return id_evento

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

# Derivada de corriente
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
        df_ev = df[
            (df["temporal_placa"] >= actual["fin"]) &
            (df["temporal_placa"] <= actual["fin"] + timedelta(minutes=5))
        ].copy()

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
        eventos_fase=eventos_fase,
        df=df_m,
        fase="r",
        ventana_media_s=ventana_media_s,
        tolerancia_rel=tolerancia_rel,
        tiempo_estable_s=tiempo_estable_s
    )

    return eventos_maquina, eventos_por_fase

# Funcion principal
if __name__ == "__main__":

    zona_horaria = pytz.timezone("UTC")

    while True:
        conn = connect_db()
        if not conn:
            exit(1)

        try:
            # 1) Obtener configuración de la analítica
            configuracion = obtener_configuracion_analitica(conn)
            if not configuracion:
                logger.error(
                    "No se encontraron parámetros de configuración. "
                    "Esperando 30 minutos..."
                )
                time.sleep(1800)
                continue

            (
                tiempo_ejecucion,
                win_s,
                ventana_media_s,
                tolerancia_rel,
                umbral_delta,
                tiempo_estable_s,
                duracion_max_evento
            ) = configuracion

            # 2) Dispositivos con analítica habilitada
            placas = obtener_placas_habilitadas(
                conn,
                ID_PLEGADORA_ELECTRICO            )

            for placa in placas:
                numero_serie = placa["numero_serie"]

                # 3) Ventana válida de ejecución
                desde = placa["fecha_alta"]
                hasta = (
                    datetime.now(zona_horaria)
                    if placa["fecha_baja"] is None
                    else min(placa["fecha_baja"], datetime.now(zona_horaria))
                )

                # 4) Avance incremental
                desde = obtener_ultimo_hasta_ejecutado(
                    conn,
                    numero_serie,
                    ID_PLEGADORA_ELECTRICO,
                    desde,
                    hasta,
                )

                if desde >= hasta:
                    continue  # analítica encendida, pero sin datos nuevos

                # 5) Obtener datos eléctricos
                df = obtener_datos_desde_hasta(
                    conn,
                    numero_serie,
                    desde,
                    hasta,
                )

                if df.empty:
                    continue

                # 6) Detectar eventos eléctricos (SOLO DETECCIÓN)
                eventos_maquina, _ = calcular_analiticas(
                    df_m=df,
                    win_s=win_s,
                    ventana_media_s=ventana_media_s,
                    tolerancia_rel=tolerancia_rel,
                    umbral_delta=umbral_delta,
                    tiempo_estable_s=tiempo_estable_s
                )


                fecha_hasta = desde
                cantidad_eventos = 0

                # 7) Persistir eventos (SOLO EVENTOS CERRADOS)
                for ev in eventos_maquina:
                    inicio = ev["inicio"]
                    fin = ev["fin"]

                    duracion = (fin - inicio).total_seconds()
                    if duracion <= 0 or duracion > duracion_max_evento:
                        continue  # descartar ruidO

                    valor = len(ev["fases"])

                    upsert_analitica_evento(
                        conn=conn,
                        id_tipo=ID_EVENTO_ELECTRICO,
                        identificador_activo=numero_serie,
                        inicio=inicio,
                        fin=fin,
                        valor=valor
                    )

                    cantidad_eventos += 1
                    fecha_hasta = max(fecha_hasta, fin)

                # 8) Registrar observación de ejecución
                insertar_observacion_analitica(
                    conn,
                    ID_PLEGADORA_ELECTRICO,
                    f"Eventos eléctricos detectados: {cantidad_eventos}",
                    numero_serie,
                    desde,
                    fecha_hasta,
                )

            logger.info(
                f"Esperando {tiempo_ejecucion} segundos antes de la próxima ejecución..."
            )
            time.sleep(int(float(tiempo_ejecucion)))

        except Exception as e:
            logger.error(f"Error en el proceso: {e}")
            logger.info("Esperando 30 minutos antes de la próxima ejecución...")
            time.sleep(1800)

        finally:
            conn.close()

