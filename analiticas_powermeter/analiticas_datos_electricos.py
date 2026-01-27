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
ID_PLEGADORA_ELECTRICO = 3
ID_EVENTO_ELECTRICO = 1

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
    return (
        3,      # tiempo_ejecucion
        60,     # win_s
        3,      # ventana_media_s
        3,      # tolerancia_rel
        2,      # umbral_delta
        120,    # tiempo_estable_s
        3600    # duracion_max_evento
    )    

   # query = """
    #    SELECT win_s, ventana_media_s, tolerancia_rel, umbral_delta, tiempo_estable_s, duracion_max_evento
    #   FROM configuracion_plegadora
    #    ORDER BY ID DESC LIMIT 1;
    #"""
    #with conn.cursor(cursor_factory=DictCursor) as cur:
    #    cur.execute(query)
    #    resultado = cur.fetchone()
    #    if resultado and all(resultado[campo] is not None for campo in resultado.keys()):
    #            return resultado
    #    return None

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
    WHERE "numero_serie" = %s
    AND "temporal_placa" BETWEEN %s AND %s
    ORDER BY "temporal_placa"
    LIMIT 10000;
    """
    with conn.cursor() as cur:
        cur.execute(query, (numero_serie, desde, hasta))
        rows = cur.fetchall()   # ✅ ACÁ

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
    observacion,
    numero_serie,
    desde,
    hasta
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
        VALUES (%s, %s, %s, %s, %s, %s);
    """

    fecha = datetime.now(pytz.UTC)

    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                id_analitica,
                numero_serie,
                desde,
                hasta,
                observacion,
                fecha
            )
        )

# Insertar analiticas de plegadora con sus pasos asociados
def upsert_analiticas_plegadoras(
    conn,
    numero_serie,
    inicio,
    fin,
    duracion,
    valor
):
    """
    Inserta o actualiza un evento eléctrico de plegadora.

    - Si existe un evento abierto (fin IS NULL), lo actualiza.
    - Si no existe, inserta un nuevo evento.
    """

    with conn.cursor() as cur:
        # 1. Buscar evento abierto para la plegadora
        cur.execute("""
            SELECT id, inicio, valor
            FROM analiticas_plegadoras
            WHERE numero_serie = %s
              AND fin IS NULL
            ORDER BY inicio DESC
            LIMIT 1
        """, (numero_serie,))

        row = cur.fetchone()

        if row:
            # 2. Evento abierto → UPDATE
            id_evento, inicio_evento, valor_actual = row

            # Recalcular duración total desde el inicio real
            if fin is not None:
                nueva_duracion = int((fin - inicio_evento).total_seconds())
            else:
                nueva_duracion = duracion

            # Mantener el pico máximo
            nuevo_valor = (
                max(valor_actual, valor)
                if valor_actual is not None and valor is not None
                else valor or valor_actual
            )

            cur.execute("""
                UPDATE analiticas_plegadoras
                SET
                    fin = %s,
                    duracion = %s,
                    valor = %s
                WHERE id = %s
            """, (
                fin,
                nueva_duracion,
                nuevo_valor,
                id_evento
            ))

            return id_evento

        else:
            # 3. No hay evento abierto → INSERT
            cur.execute("""
                INSERT INTO analiticas_plegadoras (
                    numero_serie,
                    inicio,
                    fin,
                    duracion,
                    valor
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
            """, (
                numero_serie,
                inicio,
                fin,
                duracion,
                valor
            ))

            return cur.fetchone()[0]

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
def delta_corriente_por_ventana(grupos, fase="r"):
    col = f"corriente_{fase}"

    df_agg = grupos[col].agg(
        i_max="max",
        i_min="min"
    ).reset_index()

    df_agg["delta_corriente"] = df_agg["i_max"] - df_agg["i_min"]

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
        return [], None

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

            # 2) Placas con analítica habilitada
            placas = obtener_placas_habilitadas(
                conn,
                ID_PLEGADORA_ELECTRICO
            )

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
                    hasta
                )

                if desde >= hasta:
                    continue

                # 5) Obtener datos eléctricos
                df = obtener_datos_desde_hasta(
                    conn,
                    numero_serie,
                    desde,
                    hasta
                )

                if df.empty:
                    continue

                # 6) Detectar eventos eléctricos
                eventos_cerrados  = calcular_analiticas(
                    df_m=df,
                    win_s=win_s,
                    ventana_media_s=ventana_media_s,
                    tolerancia_rel=tolerancia_rel,
                    umbral_delta=umbral_delta,
                    tiempo_estable_s=tiempo_estable_s
                )

                cantidad_eventos = 0
                fecha_hasta = desde

                # 7.1) Persistir eventos cerrados
                for ev in eventos_cerrados:
                    inicio = ev["inicio"]
                    fin = ev["fin"]

                    duracion = int((fin - inicio).total_seconds())
                    if duracion <= 0 or duracion > duracion_max_evento:
                        continue

                    valor = ev.get("valor") or len(ev["fases"])

                    upsert_analiticas_plegadoras(
                        conn=conn,
                        numero_serie=numero_serie,
                        inicio=inicio,
                        fin=fin,
                        duracion=duracion,
                        valor=None
                    )

                    cantidad_eventos += 1
                    fecha_hasta = max(fecha_hasta, fin)

                # 7.2) Persistir evento abierto (si existe)
                #if evento_abierto:
                #    inicio = evento_abierto["inicio"]
                #    fin = None
                #    duracion = int((hasta - inicio).total_seconds())
                #    valor = evento_abierto.get("valor") or len(evento_abierto["fases"])

                #    if duracion > 0 and duracion <= duracion_max_evento:
                    #    upsert_analiticas_plegadoras(
                        #    conn=conn,
                        #    numero_serie=numero_serie,
                        #    inicio=inicio,
                        #    fin=fin,
                        #    duracion=duracion,
                        #   valor=valor
                        #)

                # 8) Registrar observación de ejecución
                insertar_observacion_analitica(
                    conn,
                    id_analitica=ID_PLEGADORA_ELECTRICO,
                    observacion=f"Eventos eléctricos detectados: {cantidad_eventos}",
                    numero_serie=numero_serie,
                    desde=desde,
                    hasta=fecha_hasta
                )

            # 9) Commit único
            conn.commit()

            logger.info(
                f"Esperando {tiempo_ejecucion} segundos antes de la próxima ejecución..."
            )
            time.sleep(int(float(tiempo_ejecucion)))

        except Exception as e:
            conn.rollback()
            logger.error(f"Error en el proceso: {e}")
            logger.info("Esperando 30 minutos antes de la próxima ejecución...")
            time.sleep(1800)

        finally:
            conn.close()
