import json
import ssl
import paho.mqtt.client as mqtt
from psycopg2.extras import DictCursor
import psycopg2
import datetime as dt
import pytz
import os
import logging
import threading
import queue
import sys
import math

# Cargar las variables de entorno
MQTT_USR = os.environ.get("MQTT_USR", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
DB_HOST = os.environ.get("DB_HOST", "")
DB_USR = os.environ.get("DB_USR", "")
DB_PASS = os.environ.get("DB_PASS", "")
DB_NAME = os.environ.get("DB_NAME", "")
FP_MUESTRAS_N = int(os.environ.get("FP_MUESTRAS_N", 300))

stdout_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[stdout_handler],
)

logger = logging.getLogger(__name__)

# ---------------------------
# UMBRALES DE VARIACIÓN
# ---------------------------
UMB_TENSION = 0.5      
UMB_CORRIENTE = 0.5   
UMB_POTENCIA_A = 0.5
UMB_POTENCIA_R = 0.5
FP_UMBRAL = 0.95

# Crear cola de comunicacion entre hilos
data_queue = queue.Queue()
fp_null_tracker = {}  
fp_alarmas_activas = {}
ultimo_operario = {}
connection = None
stop_processing = False

# Configuración de la base de datos
def connect_db():
    global connection
    try:
        connection = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USR,
            password=DB_PASS
        )
        logger.info("Conexión a la base de datos establecida.")
    except psycopg2.OperationalError as e:
        logger.error(f"Error al conectar a la base de datos: {e}")
        connection = None

# Lista de alarmas
def obtener_alarma_plegadora():
    global connection
    global fp_alarmas_activas

    try:
        if connection is None or connection.closed:
            connect_db()
            if connection is None:
                logger.error("No se pudo conectar a la base de datos para obtener alarmas activas.")
                return

        with connection.cursor() as cur:
            # Traer todas las alarmas activas
            cur.execute("""
                SELECT a.id, a.id_activo, l.nombre, a.fecha_inicio
                FROM alarmas a
                JOIN listas_alarmas l ON l.id = a.id_lista_alarma
                WHERE a.id_estado_alarma IN (
                    SELECT id FROM estados_alarmas WHERE nombre = 'activa' OR nombre = 'reconocida'
                )
            """)
            filas = cur.fetchall()

            for id_alarma, id_activo, lista_nombre, fecha_inicio in filas:
                # Buscar el número de serie desde la vista
                cur.execute("""
                    SELECT numero_serie
                    FROM vista_activo_por_serie_y_fecha
                    WHERE id_activo = %s
                      AND fecha_alta <= %s
                      AND (fecha_baja IS NULL OR %s <= fecha_baja)
                    LIMIT 1
                """, (id_activo, fecha_inicio, fecha_inicio))

                row = cur.fetchone()
                numero_serie = row[0] if row else None

                if not numero_serie:
                    logger.warning(f"No se encontró número de serie para activo {id_activo} en {fecha_inicio}")
                    continue

                if lista_nombre.lower() == "bajo_fp":
                    fp_alarmas_activas[numero_serie] = {
                        "id_alarma": id_alarma,
                        "id_activo": id_activo
                    }
                    logger.info(f"Alarma FP activa encontrada: {numero_serie} (id: {id_alarma}, activo: {id_activo})")

    except Exception as e:
        logger.error(f"Error al obtener alarmas activas: {e}")

# Insertar alarmas de evento
def insertar_alarma_plegadora(
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

# Obtener los trabajadores activos 
def  obtener_trabajadores_activos():
    global connection
    global ultimo_operario

    try:
        if connection is None or connection.closed:
            connect_db()
            if connection is None:
                logger.error("No se pudo conectar a la base de datos para obtener operarios activos.")
                return

        with connection.cursor() as cur:
             # Buscar operarios con fin NULL 
            cur.execute("""
                SELECT id, id_operario, inicio
                FROM registro_operarios
                WHERE fin IS NULL
                ORDER BY id DESC LIMIT 1        
            """)
            fila = cur.fetchone()

            if not fila:
                logger.info("No hay operarios activos en este momento.")
                return

            # Guardamos el último
            for id_registro, id_operario, inicio in fila:
                ultimo_operario = {
                  "id_operario": id_operario, 
                  "id": id_registro
                }
                logger.info(f"Operario activo: id_operario={id_operario}, desde={inicio}")
     
    except Exception as e:
        logger.error(f"Error al obtener operarios activos: {e}")

# Funcion que calcula el factor de potencia
def factor_potencia(        
        potencia_a_r, potencia_r_r,
        potencia_a_s, potencia_r_s,
        potencia_a_t, potencia_r_t
):
    p = potencia_a_r + potencia_a_s + potencia_a_t
    q = potencia_r_r + potencia_r_s + potencia_r_t

    if p is None or q is None:
        return None
    
    s = math.sqrt(p*p + q*q)

    if s == 0:
        return None
    return abs(p) / s

# Funcion super importante:
# Lee la cola que alimenta MQTT e inserta los datos en la DB y ademas gestiona las alarmas
def procesarEvento():
    global connection
    global data_queue
    global stop_processing

    while not stop_processing:
        try:
            newData = data_queue.get(timeout=1) 

            # Conectar a la base de datos
            if connection is None or connection.closed:
                connect_db()
                if connection is None:
                    return
                
            numero_serie = newData['numero_serie']
            temporal_placa = newData["temporal_placa"]
            temporal_server = newData['temporal_server']
            fases = newData["fases"]

            r = fases[0] if len(fases) >= 1 else None
            s = fases[1] if len(fases) >= 2 else None
            t = fases[2] if len(fases) >= 3 else None

            def get(field, fase):
                return fase[field] if fase else None

            tension_r = get("v", r)
            corriente_r = get("i", r)
            potencia_a_r = get("p", r)
            potencia_r_r = get("q", r)

            tension_s = get("v", s)
            corriente_s = get("i", s)
            potencia_a_s = get("p", s)
            potencia_r_s = get("q", s)

            tension_t = get("v", t)
            corriente_t = get("i", t)
            potencia_a_t = get("p", t)
            potencia_r_t = get("q", t)

            # ----------------------------------
            # 1) Alarma por bajo factor de potencia
            # ----------------------------------
            # Inicialización
            if numero_serie not in fp_up_tracker:
                fp_up_tracker[numero_serie] = 0
                fp_down_tracker[numero_serie] = 0

            # Evaluación FP
            if fp_maquina is not None and fp_maquina < FP_UMBRAL:
                fp_up_tracker[numero_serie] += 1
                fp_down_tracker[numero_serie] = 0
            else:
                fp_down_tracker[numero_serie] += 1
                fp_up_tracker[numero_serie] = 0

            # Disparo de alarma
            if (
                fp_up_tracker[numero_serie] >= FP_MUESTRAS_N
                and numero_serie not in fp_alarmas_activas
            ):
                id_alarma = insertar_alarma_plegadora(
                    connection,
                    causa=f"Factor de potencia inferior al mínimo durante {FP_MUESTRAS_N} muestras.",
                    fecha_inicio=newData['time'],
                    fecha_fin=None,
                    estado_nombre="activa",
                    lista_nombre="bajo_fp",
                    numero_serie=numero_serie
                )
                fp_alarmas_activas[numero_serie] = {"id_alarma": id_alarma}

            # Cierre de alarma por estabilización
            if (
                fp_down_tracker[numero_serie] >= FP_MUESTRAS_N
                and numero_serie in fp_alarmas_activas
            ):
                id_alarma = fp_alarmas_activas[numero_serie]["id_alarma"]
                with connection.cursor() as cur:
                    cur.execute("""
                        UPDATE alarmas
                        SET fecha_fin = %s,
                            id_estado_alarma = (
                                SELECT id FROM estados_alarmas WHERE nombre = 'inactiva'
                            )
                        WHERE id = %s
                    """, (newData['time'], id_alarma))
                connection.commit()

                del fp_alarmas_activas[numero_serie]
                fp_up_tracker[numero_serie] = 0
                fp_down_tracker[numero_serie] = 0
            # ----------------------------------
            # 2) Alarma por ...
            # ----------------------------------

            with connection.cursor() as cursor:
                # -----------------------------------------
                # Buscar último registro del número de serie
                # -----------------------------------------
                query_last = """
                    SELECT id,
                        tension_r, corriente_r, potencia_a_r, potencia_r_r,
                        tension_s, corriente_s, potencia_a_s, potencia_r_s,
                        tension_t, corriente_t, potencia_a_t, potencia_r_t
                    FROM data_instantanea
                    WHERE numero_serie = %s
                    ORDER BY id DESC
                    LIMIT 1
                """
                cursor.execute(query_last, (numero_serie,))
                last = cursor.fetchone()

                if last:

                    last_id = last[0]
                    last_vals = []
                    for v in last[1:]:
                        if v is None:
                            last_vals.append(None)
                        else:
                            last_vals.append(float(v))


                    new_vals = [
                        tension_r, corriente_r, potencia_a_r, potencia_r_r,
                        tension_s, corriente_s, potencia_a_s, potencia_r_s,
                        tension_t, corriente_t, potencia_a_t, potencia_r_t,
                    ]

                    # -----------------------------------------
                    # Comparación con umbrales
                    # -----------------------------------------
                    umbrales = [
                        UMB_TENSION, UMB_CORRIENTE, UMB_POTENCIA_A, UMB_POTENCIA_R,
                        UMB_TENSION, UMB_CORRIENTE, UMB_POTENCIA_A, UMB_POTENCIA_R,
                        UMB_TENSION, UMB_CORRIENTE, UMB_POTENCIA_A, UMB_POTENCIA_R,
                    ]                 

                    variacion = False
                    for old, new, umbral in zip(last_vals, new_vals, umbrales):
                        if old is None:
                            variacion = True
                            break
                          # Comparación de umbral (ambos cast a float)
                        if abs(float(new) - float(old)) > umbral:
                            variacion = True
                            break

                    if not variacion:
                        # No cambió más que el umbral → UPDATE solo del timestamp
                        cursor.execute(
                            "UPDATE data_instantanea SET update = %s WHERE id = %s",
                            (temporal_server, last_id,)
                        )
                        connection.commit()
                        continue
                
                # --------------------------------------------------
                # Si hubo variación → INSERT
                # --------------------------------------------------
                query_insert = """
                INSERT INTO data_instantanea(
                    numero_serie,
                    tension_r, corriente_r, potencia_a_r, potencia_r_r,
                    tension_s, corriente_s, potencia_a_s, potencia_r_s,
                    tension_t, corriente_t, potencia_a_t, potencia_r_t,
                    temporal_placa, temporal_server
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """

                cursor.execute(query_insert, (
                    numero_serie,
                    tension_r, corriente_r, potencia_a_r, potencia_r_r,
                    tension_s, corriente_s, potencia_a_s, potencia_r_s,
                    tension_t, corriente_t, potencia_a_t, potencia_r_t,
                    temporal_placa, temporal_server
                ))

                connection.commit()                    
                   
        except queue.Empty:
            continue
        except psycopg2.DatabaseError as e:
            if connection is not None and connection.closed == 0:
                connection.rollback()
            logger.error(f"Error al insertar datos: {e}")
        except Exception as e:
            logger.error(f"Error inesperado Hilo: {e}")

# Funcion que toma los datos del borker MQTT
def on_message(client, userdata, msg):
    global data_queue
    try:

        # Decodificar json
        payload_decoded = msg.payload.decode()   
        data_list = json.loads(payload_decoded)   

        current_date = dt.datetime.now(dt.timezone.utc)
        argentina_tz = pytz.timezone("America/Argentina/Buenos_Aires")
        current_date_argentina = current_date.astimezone(argentina_tz)
        
        aux = msg.topic
        aux = aux.split("/")

        for entry in data_list:
            data = {
                "numero_serie":  aux[1],
                "temporal_placa": entry["t"],
                "temporal_server": current_date_argentina.strftime('%Y-%m-%d %H:%M:%S'),
                "fases": entry["f"]
            }
            data_queue.put(data)
    except Exception as e:
        logger.error(f"Error inesperado: {e}")

# Funcion principal
def main():
    global stop_processing

    # Configurar el cliente MQTT
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,client_id="CA_pm_inst")
    client.on_message = on_message

    # Configurar credenciales del broker MQTT
    client.username_pw_set(MQTT_USR, MQTT_PASS)

    # Conectar al broker MQTT
    client.connect(MQTT_HOST, MQTT_PORT, 60)

    # Suscribirse al tema MQTT
    client.subscribe("pro5/+/inst/v1")

    logger.info("Conectado a MQTT y suscrito al tema")

    hilo_procesar = threading.Thread(target=procesarEvento)
    hilo_procesar.start()

    # Mantener el cliente MQTT en funcionamiento
    client.loop_forever()

    stop_processing = True

    hilo_procesar.join()

    if connection:
            connection.close()
            logger.info("Conexión a la base de datos cerrada.")

if __name__ == '__main__':
    main()

#---- FUNCIONES GENERALES ------------------------------------------------------------------------------------------------
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

# Obtener id_activo mediante el numero_serie
def obtener_id_activo_por_numero_serie(conn, numero_serie):
    query = """
        SELECT a.id_activo
        FROM dispositivos d
        JOIN activos_dispositivos ad
          ON ad.id_dispositivo = d.id_dispositivo
        JOIN activos a
          ON a.id_activo = ad.id_activo
        WHERE d.numero_serie = %s
          AND ad.fecha_baja IS NULL;
    """

    with conn.cursor() as cur:
        cur.execute(query, (numero_serie,))
        row = cur.fetchone()

    if row is None:
        return None

    return row[0]

#-------------------------------------------------------------------------------------------------------------------------
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

# Obtencion de datos para alarmas
def obtener_configuracion_alarmas(conn):
    query = """
    SELECT desequilibrio_warning, desequilibrio_critico, sobrecorriente_warning, sobrecorriente_critico, tiempo_picos_warning, tiempo_picos_critico, tiempo_sin_datos
    FROM configuracion_alarma
    ORDER BY fecha_actualizacion DESC LIMIT 1;
    """
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(query)
        resultado = cur.fetchone()
        if resultado and all(resultado[campo] is not None for campo in resultado.keys()):
                return resultado
        return None

# Obtener media historica de corriente
def obtener_media_historica(id_activo, fase):
    query = """
        SELECT media_p90
        FROM historico_media
        WHERE id_activo = %s AND fase = %s
        ORDER BY fecha_carga DESC LIMIT 1;
        """
    with connection.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(query,(id_activo, fase))
        resultado = cur.fetchone()
        if resultado and all(resultado[campo] is not None for campo in resultado.keys()):
                return resultado
        return None    

# Obtener el acumulado de corriente del dia
def obtener_acumulado_corriente(id_activo):
    query = """
        SELECT fase, i_media
        FROM acumulado_corriente
        WHERE id_activo = %s
        ORDER BY fecha DESC
        LIMIT 3;
    """
    with connection.cursor() as cur:
        cur.execute(query, (id_activo,))
        rows = cur.fetchall()

    return {fase: float(i_media) for fase, i_media in rows}

# Alarma por desequilibrio de fases
def alarma_desequilibrio_fases():

    return 

# Alarma por sobrecorriente: trifasica/monofasica
def alarma_sobrecorriente(
    numero_serie,
    id_activo,
    sobrecorriente_warning,
    sobrecorriente_critico
):
    # 1. Obtener P90 historico mensual por fase
    media_hist = {}
    for fase in ["R", "S", "T"]:
        res = obtener_media_historica(id_activo, fase)
        if not res:
            return
        media_hist[fase] = {
            "media_p90": res["media_p90"],
            "fecha_carga": res["fecha_carga"]
        }

    # 2. Obtener ultimo acumulado diario por fase
    acumulado = {}
    query = """
        SELECT fase, i_media, fecha
        FROM acumulado_corriente
        WHERE id_activo = %s
        ORDER BY fecha DESC
        LIMIT 3;
    """
    with connection.cursor() as cur:
        cur.execute(query, (id_activo,))
        for fase, i_media, fecha in cur.fetchall():
            acumulado[fase] = {
                "i_media": float(i_media),
                "fecha": fecha
            }

    if not all(f in acumulado for f in ["R", "S", "T"]):
        return

    # 3. Validacion temporal: mes acumulado = mes siguiente al ultimo historico
    def mes_siguiente(year, month):
        if month == 12:
            return year + 1, 1
        return year, month + 1

    for fase in ["R", "S", "T"]:
        fh = media_hist[fase]["fecha_carga"]
        fa = acumulado[fase]["fecha"]

        hist_ym = (fh.year, fh.month)
        acum_ym = (fa.year, fa.month)

        if acum_ym != mes_siguiente(*hist_ym):
            insertar_observacion_analitica(
                connection,
                #ID_PLEGADORA_ELECTRICO,
                numero_serie,
                fa,
                None,
                f"Inconsistencia mensual historico/acumulado en fase {fase}"
            )
            return

    # 4. Comparacion diaria contra P90 (por fase)
    fases_critico = []
    fases_warning = []

    for fase in ["R", "S", "T"]:
        i_media = acumulado[fase]["i_media"]
        p90 = media_hist[fase]["media_p90"]

        if i_media > p90 * (1 + sobrecorriente_critico):
            fases_critico.append(fase)
        elif i_media > p90 * (1 + sobrecorriente_warning):
            fases_warning.append(fase)

    # 5. Disparo de alarma (prioridad: critico > warning)
    fecha_evento = max(acumulado[f]["fecha"] for f in ["R", "S", "T"])

    if fases_critico:
        insertar_alarma_plegadora(
            conn=connection,
            causa=f"Sobrecorriente CRITICA diaria en fases: {', '.join(fases_critico)}",
            latitud=None,
            longitud=None,
            fecha_inicio=fecha_evento,
            fecha_fin=None,
            nivel_nombre="critico",
            estado_nombre="activa",
            lista_nombre="sobrecorriente",
            numero_serie=numero_serie,
        )
        return

    if fases_warning:
        insertar_alarma_plegadora(
            conn=connection,
            causa=f"Sobrecorriente WARNING diaria en fases: {', '.join(fases_warning)}",
            latitud=None,
            longitud=None,
            fecha_inicio=fecha_evento,
            fecha_fin=None,
            nivel_nombre="advertencia",
            estado_nombre="activa",
            lista_nombre="sobrecorriente",
            numero_serie=numero_serie,
        )


# Alarma por picos altos repetitivos
def alarma_picos_repetitivos():
    return
