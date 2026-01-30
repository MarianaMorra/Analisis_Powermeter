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
I_MIN_ACTIVA = 5.0  # A

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
def insertar_evento_analiticas_plegadoras(
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

# Cierre de alarma por falta de datos       
def cerrar_alarma_falta_datos(conn, id_activo, fecha_fin):
    query = """
        UPDATE alarmas
        SET fecha_fin = %s
        WHERE id_activo = %s
          AND fecha_fin IS NULL
          AND id_lista_alarma = (
              SELECT id FROM listas_alarmas
              WHERE nombre = 'falta_datos'
          );
    """
    with conn.cursor() as cur:
        cur.execute(query, (fecha_fin, id_activo))
  
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
    tolerancia_rel,
    tiempo_estable_s
):
    """
    Consolida eventos por fase en eventos de máquina.

    - Un evento NO se cierra por ausencia de datos.
    - Mientras no haya datos suficientes para evaluar estabilidad,
      el evento queda abierto (fin = None).
    - El cierre solo ocurre cuando reaparecen datos y se cumple
      el criterio de estabilidad.
    """

    df = df.sort_values("temporal_placa").reset_index(drop=True)
    times = df["temporal_placa"].to_numpy()

    eventos_fase = sorted(eventos_fase, key=lambda x: x["inicio"])
    eventos_maquina = []
    actual = None

    col = f"corriente_{fase}"

    # delta temporal típico (cuando hay datos)
    dt_medio = df["temporal_placa"].diff().median().total_seconds()

    for ev in eventos_fase:

        if actual is None:
            actual = {
                "inicio": ev["inicio"],
                "fin": ev["fin"],
                "fases": {ev["fase"]}
            }
            continue

        # superposición → mismo evento
        if actual["fin"] is None or ev["inicio"] <= actual["fin"]:
            actual["fin"] = (
                max(actual["fin"], ev["fin"])
                if actual["fin"] is not None
                else ev["fin"]
            )
            actual["fases"].add(ev["fase"])
            continue

        # ---- NO hay superposición: intentar cerrar ----

        t_ini = actual["fin"]
        t_fin = actual["fin"] + pd.Timedelta(minutes=10)

        i_ini = times.searchsorted(t_ini, side="left")
        i_fin = times.searchsorted(t_fin, side="right")

        df_ev = df.iloc[i_ini:i_fin].copy()

        # ---- NO HAY DATOS → NO SE CIERRA ----
        if df_ev.empty:
            actual["fin"] = None
            continue

        # promedio móvil
        df_ev["media"] = df_ev[col].rolling(
            window=ventana_media_s,
            min_periods=ventana_media_s
        ).mean()

        df_ev["estable"] = (
            (df_ev[col] - df_ev["media"]).abs()
            <= df_ev["media"] * tolerancia_rel
        )

        contador = 0
        fin_estable = None

        for i in range(len(df_ev)):
            if df_ev["estable"].iloc[i]:
                contador += 1
                if contador * dt_medio >= tiempo_estable_s:
                    fin_estable = df_ev["temporal_placa"].iloc[i]
                    break
            else:
                contador = 0

        if fin_estable:
            # cierre real del evento
            actual["fin"] = fin_estable
            eventos_maquina.append(actual)

            actual = {
                "inicio": ev["inicio"],
                "fin": ev["fin"],
                "fases": {ev["fase"]}
            }
        else:
            # no hay estabilidad → sigue el mismo evento
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

# Obtencion de datos para alarmas
def obtener_configuracion_alarmas():
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
    with conn.cursor(cursor_factory=DictCursor) as cur:
        cur.execute(query,(id_activo, fase))
        resultado = cur.fetchone()
        if resultado and all(resultado[campo] is not None for campo in resultado.keys()):
                return resultado
        return None    

# Insertar fila en tabla auxiliar acumulado_corriente
def insertar_acumulado_corriente(id_activo, fase, ts_inicio, ts_fin, media_corriente):
    query = """
        INSERT INTO acumulado_corriente
            (id_activo, fase, ts_inicio, ts_fin, i_media)
        VALUES
            (%s, %s, %s, %s, %s);
    """

    with conn.cursor() as cur:
        cur.execute(
            query,
            (id_activo, fase, ts_inicio, ts_fin, media_corriente)
        )
    return True

# Calcula y devuelve la media de corriente por fase cada 5 minutos. Carga el valor en la tabla correspondiente
def procesar_data_cinco_minutos(
    df,
    id_activo,
    I_MIN_ACTIVA
):
    df = df.sort_values("temporal_placa")

    df["ventana_5m"] = df["temporal_placa"].dt.floor("5min")

    for ventana, grupo in df.groupby("ventana_5m"):
        ts_inicio = ventana
        ts_fin = ventana + timedelta(minutes=5)

        for fase_col, fase_nombre in [
            ("corriente_r", "R"),
            ("corriente_s", "S"),
            ("corriente_t", "T"),
        ]:
            media = grupo[fase_col].mean()

            if media < I_MIN_ACTIVA:
                continue

            insertar_acumulado_corriente(
                id_activo,
                fase_nombre,
                ts_inicio,
                ts_fin,
                media,
            )

# Alarma por desequilibrio de fases
def alarma_desequilibrio_fases():
    return 

# Alarma por sobrecorriente: trifasica/monofasica
def alarma_sobrecorriente():
    return

# Alarma por picos altos repetitivos
def alarma_picos_repetitivos():
    return

# Calcular los eventos de las plegadoras a partir de la observacion de la corriente
def calcular_analiticas(
    df_m,
    desde,
    hasta,
    win_s,
    ventana_media_s,
    tolerancia_rel,
    umbral_delta,
    tiempo_estable_s
):
    if df_m.empty:
        # 1) Si no hay datos, inserto observacion y disparo alarma
        insertar_observacion_analitica(conn, ID_PLEGADORA_ELECTRICO, numero_serie, desde, hasta, "Ausencia de datos nuevos")
        insertar_alarma_evento(
            conn = conn,
            causa ="Ausencia de datos nuevos",
            latitud = None,
            longitud = None,
            fecha_inicio = desde,
            fecha_fin = None,
            nivel_nombre="advertencia",
            estado_nombre="inactiva",
            lista_nombre="traslacion_medio",
            numero_serie=numero_serie,
        )
        return []
    
    # 2) Cierro alarma por falta de datos. Cargo la fecha de finalizacion como el timestamp del primer dato valido
    cerrar_alarma_falta_datos(
        conn,
        id_activo,
        fecha_fin=df_m["temporal_placa"].min()
    )

    # 3) Procesamos los datos raw cada 5 minutos para obtener las ventanas que generan la media historica
    #procesar_data_cinco_minutos(df_m, id_activo, I_MIN_ACTIVA)

    # 4) Ajustamos los datos para no detectar picos de encendido/apagado  
    df_m = recortar_rango_operativo_por_corriente(
            df_m,
            fase="r",               
            umbral_corriente=umbral_delta,  
            min_muestras=2          
        )

    if df_m.empty:
        return []

    # 5) Genero ventanas de datos cada 1 minuto    
    ventanas = generar_ventanas(df_m, win_s)

    # 6) Determino la variacion de corriente maxima dentro de esa ventana
    deltas = {
        "R": delta_corriente_por_ventana(ventanas, fase="r"),
        "S": delta_corriente_por_ventana(ventanas, fase="s"),
        "T": delta_corriente_por_ventana(ventanas, fase="t"),
    }

    eventos_por_fase = {}

    # 7) Detecto eventos en cada fase mediante la comparacion del delta con un threshold establecido
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

    # 8) Junto todos los eventos obtenidos
    eventos_fase = unir_eventos_por_fase(
        eventos_por_fase["R"],
        eventos_por_fase["S"],
        eventos_por_fase["T"]
    )

    # 9) Me fijo si los eventos se replican en las tres fases -> evento de maquina
    eventos_cerrados= consolidar_eventos_maquina(
        eventos_fase=eventos_fase,
        df=df_m,
        fase='r',
        ventana_media_s=ventana_media_s,
        tolerancia_rel=tolerancia_rel,
        tiempo_estable_s=tiempo_estable_s
    )

    # Vamos a verificar si se debe disparar una alarma
    # 10) Alarmas por desbalance de fases:
    
    # 11) Alarmas por sobrecorriente:
    
    # 12) Alarmas por picos repetidos:

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
            # Obtengo ID de la analitica asociada
            ID_PLEGADORA_ELECTRICO = obtener_id_analitica(
                conn, "PLEGADORA_ELECTRICO"
            )

            # Obtengo las placas que estan suscriptas a la analitica
            placas = obtener_placas_habilitadas(
                conn, ID_PLEGADORA_ELECTRICO
            )

            tiempo_sleep = 60  # fallback

            for placa in placas:
                numero_serie = placa["numero_serie"]

                # ------------------------------------------------
                desde_base = placa["fecha_alta"]

                ahora = datetime.now(zona_horaria)
                hasta_teorico = (
                    ahora
                    if placa["fecha_baja"] is None
                    else min(placa["fecha_baja"], ahora)
                )
                # ------------------------------------------------
                desde = obtener_ultimo_hasta_ejecutado(
                    conn,
                    numero_serie,
                    ID_PLEGADORA_ELECTRICO,
                    desde_base,
                    hasta_teorico
                )

                # Ventana fija de 1 hora
                hasta = min(desde + VENTANA_ANALISIS, hasta_teorico)

                if desde >= hasta:
                    continue
                
                # Cargamos los datos
                df = obtener_datos_desde_hasta(
                    conn,
                    numero_serie,
                    desde,
                    hasta
                )
                
                # Obtenemos id_activo mediante numero_serie
                id_activo = obtener_id_activo_por_numero_serie(conn, numero_serie)

                if id_activo is None:
                    continue

                # Obtenemos la configuracion necesaria para calcular las analitas (por maquina)
                configuracion = obtener_configuracion_analitica(conn, numero_serie)
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

                # Calculo de analiticas: eventos de corriente
                # NOTA: LE ESTOY CARGANDO DESDE Y HASTA PARA PROCESAR ALARMAS. ESTO NO PASABA EN ANALITICAS_BOBINAS
                eventos_cerrados = calcular_analiticas(df, desde, hasta, win_s, ventana_media_s, tolerancia_rel, umbral_delta, tiempo_estable_s)

                fecha_hasta = df["temporal_placa"].max()
                cantidad_eventos = 0

                # Verificacion de eventos calculados y carga de los mismos en la tabla analiticas_plegadoras
                for ev in eventos_cerrados:
                    inicio = ev["inicio"]
                    fin = ev["fin"]
                                        
                    # evento abierto
                    if fin is None:
                        continue

                    duracion = int((fin - inicio).total_seconds())
                    
                    logger.info(
                        f"EVENTO DURACION: {duracion}s (max={duracion_max_evento}s)"
                    )

                    if duracion <= 0 or duracion > duracion_max_evento:
                        continue

                    valor = ev.get("valor") or len(ev["fases"])

                    insertar_evento_analiticas_plegadoras(conn, numero_serie, inicio, fin, duracion, valor)

                    cantidad_eventos += 1
                    fecha_hasta = max(fecha_hasta, fin)

                logger.info(
                    f"Eventos detectados (cerrados): {len(eventos_cerrados)}"
                )

                # Insertar entrada en la tabla de observaciones
                insertar_observacion_analitica(
                    conn,
                    ID_PLEGADORA_ELECTRICO,
                    numero_serie,
                    desde,
                    fecha_hasta,
                    f"Eventos eléctricos detectados: {cantidad_eventos}"
                )

            # Comit general
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
