from flask import Flask, jsonify, request, redirect
import mercadopago
import os
import psycopg2
import psycopg2.extras
import uuid
import secrets
import requests
import threading
import time
from datetime import datetime, timedelta, timezone

# Hora de Argentina (UTC-3), como datetime "naive" para guardar/comparar
# fechas de forma consistente en todo el sistema.
AR_TZ = timezone(timedelta(hours=-3))
def ahora_ar():
    return datetime.now(AR_TZ).replace(tzinfo=None)

app = Flask(__name__)

MP_TOKEN      = os.environ.get("MP_ACCESS_TOKEN")
CLAVE_SECRETA = os.environ.get("CLAVE_SECRETA")
PRECIO        = float(os.environ.get("PRECIO", "500"))
DATABASE_URL  = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://")
USER_ID       = "178328412"
BASE_URL      = "https://web-production-94bbab.up.railway.app"

# OAuth marketplace (opcional: se activa cargando estas variables en Railway)
MP_CLIENT_ID     = os.environ.get("MP_CLIENT_ID")
MP_CLIENT_SECRET = os.environ.get("MP_CLIENT_SECRET")
FEE_PORCENTAJE   = float(os.environ.get("FEE_PORCENTAJE", "0"))  # tu comisión, ej. 10

# Reembolso automático: minutos que puede esperar una orden pagada sin que
# el equipo (offline) la ejecute, antes de devolverle el dinero al cliente
REEMBOLSO_MINUTOS = int(os.environ.get("REEMBOLSO_MINUTOS", "5"))

# Freno de fuerza bruta del PIN con que el cliente regala fichas desde su panel.
# Tras PIN_MAX_INTENTOS fallidos, el regalo queda bloqueado PIN_BLOQUEO_MIN
# minutos para ese cliente. Configurables en Railway.
PIN_MAX_INTENTOS = int((os.environ.get("PIN_MAX_INTENTOS") or "5").strip() or "5")
PIN_BLOQUEO_MIN  = int((os.environ.get("PIN_BLOQUEO_MIN") or "15").strip() or "15")

# ── Alertas por Telegram (equipo caido) ──
# Se setean en Railway (Variables). Sin ellas, las alertas no arrancan.
TELEGRAM_TOKEN   = (os.environ.get("TELEGRAM_TOKEN") or "").strip() or None
TELEGRAM_CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip() or None
# Sin latido por mas de esto (seg) -> se considera caido y avisa. 180 = 3 min
# (3 latidos perdidos) para no dar falsas alarmas por un latido salteado.
ALERTA_OFFLINE_S = int((os.environ.get("ALERTA_OFFLINE_S") or "180").strip() or "180")
# Cada cuanto se re-arma el QR de las cajas MQTT. La orden se muere a los ~8 min,
# asi que hay que re-armar antes. 240 = 4 min (2x de margen). Configurable en Railway.
REARME_SEGUNDOS = int((os.environ.get("REARME_SEGUNDOS") or "240").strip() or "240")

# ─── MQTT: activación push de cajas tipo "pulso" (ej. inflado) ───────
import ssl
import json as _json
try:
    import paho.mqtt.publish as _mqtt_publish
except ImportError:
    _mqtt_publish = None

MQTT_HOST = (os.environ.get("MQTT_HOST") or "").strip() or None
# Robusto: una variable vacía ("") no debe tumbar el arranque del backend.
try:
    MQTT_PORT = int((os.environ.get("MQTT_PORT") or "8883").strip() or "8883")
except ValueError:
    MQTT_PORT = 8883
MQTT_USER = (os.environ.get("MQTT_USER") or "").strip() or None
MQTT_PASS = os.environ.get("MQTT_PASS") or None

# ─── TIPO DE CAJA: ahora vive en la DB, no en el codigo ──────────────
# Hasta 09/2026 estos conjuntos eran literales en este archivo: dar de alta una
# maquina obligaba a editar app.py + git push + redeploy de Railway. Desde el
# panel /admin el tipo de cada caja es la columna 'tipo' de la tabla
# dispositivos, y el ESP fisico que la maneja es la columna 'esp_id'.
# Los literales de abajo quedan SOLO como semilla: init_db() los usa una unica
# vez para completar las cajas viejas que todavia tienen tipo NULL.
SEMILLA_MQTT   = {"inflado01", "aspiradora01", "soplado01", "aspiradora02", "soplado02", "villagas01"}
SEMILLA_PULSO  = {"inflado01", "villagas01"}
SEMILLA_FICHAS = {"villagas01"}
SEMILLA_GRUPOS = {                      # esp_id -> cajas que cuelgan de ese ESP
    "estacion01": {"aspiradora01": 0, "soplado01": 1},
    "estacion02": {"aspiradora02": 0, "soplado02": 1},
}

# Tipos validos y como se comporta cada uno.
TIPOS = {
    "pulso":     "Pulso — un disparo corto; la maquina corre su ciclo interno sola",
    "sostenida": "Sostenida — el rele queda cerrado el tiempo pagado (maestro + Nano)",
    "fichas":    "Expendedora de fichas — el cmd lleva 'cantidad' en vez de 'segundos'",
    "legacy":    "Vieja por polling HTTPS — sin MQTT (en retirada)",
}

# Cache chico de la tabla: __contains__ se llama seguido (webhook, subscriptor
# MQTT, paneles) y no queremos una consulta por llamada.
CACHE_TIPOS_S = 10
_cache_tipos = {"t": 0.0, "filas": {}}
_cache_tipos_lock = threading.Lock()

def _semilla_como_cache():
    """Las semillas con la forma que tiene el cache. Red de seguridad para
    cuando todavia no se pudo leer la DB ni una sola vez."""
    filas = {}
    for cid in SEMILLA_MQTT:
        if cid in SEMILLA_FICHAS:
            tipo = "fichas"
        elif cid in SEMILLA_PULSO:
            tipo = "pulso"
        else:
            tipo = "sostenida"
        filas[cid] = {"tipo": tipo, "esp_id": cid}
    for esp, cajas in SEMILLA_GRUPOS.items():
        for cid in cajas:
            if cid in filas:
                filas[cid]["esp_id"] = esp
    return filas

def _tipos_cache():
    ahora = time.time()
    with _cache_tipos_lock:
        if _cache_tipos["filas"] and (ahora - _cache_tipos["t"]) < CACHE_TIPOS_S:
            return _cache_tipos["filas"]
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, tipo, esp_id FROM dispositivos")
        filas = {r["id"]: {"tipo": (r["tipo"] or "legacy"), "esp_id": (r["esp_id"] or r["id"])}
                 for r in cur.fetchall()}
        cur.close()
        conn.close()
    except Exception as e:
        # La DB puede estar durmiendo un instante: seguimos con lo ultimo bueno
        # en vez de tratar a todas las cajas como legacy.
        print(f"[tipos] no pude leer dispositivos: {e}")
        if _cache_tipos["filas"]:
            return _cache_tipos["filas"]
        # Cache frio (recien deployado) + DB que no responde: devolver {} haria
        # que TODAS las cajas parezcan 'legacy', el pago no saldria por MQTT y
        # terminaria auto-reembolsado. Caemos a las semillas, que es exactamente
        # como se comportaba el backend cuando los tipos eran literales.
        # No se cachea: el proximo llamado vuelve a intentar contra la DB.
        print("[tipos] cache frio -> uso las semillas del codigo")
        return _semilla_como_cache()
    with _cache_tipos_lock:
        _cache_tipos["t"] = ahora
        _cache_tipos["filas"] = filas
    return filas

def invalidar_cache_tipos():
    """Llamar despues de crear/editar una caja para que el cambio pegue ya."""
    with _cache_tipos_lock:
        _cache_tipos["t"] = 0.0

class _CajasPorTipo:
    """Se usa igual que el set de antes (`x in ESTACIONES_MQTT`, `for x in ...`)
    pero los datos salen de la columna 'tipo'. Asi no hubo que tocar los ~20
    lugares del backend que consultaban estos conjuntos."""
    def __init__(self, tipos):
        self._tipos = set(tipos)
    def _ids(self):
        return {cid for cid, d in _tipos_cache().items() if d["tipo"] in self._tipos}
    def __contains__(self, caja):
        d = _tipos_cache().get(caja)
        return bool(d) and d["tipo"] in self._tipos
    def __iter__(self):
        return iter(self._ids())
    def __len__(self):
        return len(self._ids())
    def __repr__(self):
        return repr(sorted(self._ids()))

# Cajas que se activan por push MQTT, no por polling de /orden.
ESTACIONES_MQTT = _CajasPorTipo({"pulso", "sostenida", "fichas"})
# De esas, las de PULSO (un disparo instantaneo) se marcan completadas al toque.
# Las demas son de servicio SOSTENIDO: se marcan 'ejecutando' con inicio, para
# que la recuperacion tras corte de luz calcule el tiempo restante.
# Una expendedora de fichas tambien es un disparo instantaneo.
ESTACIONES_PULSO = _CajasPorTipo({"pulso", "fichas"})
# Expendedoras de fichas: en vez de "segundos" el cmd MQTT lleva "cantidad" de
# fichas; el campo "segundos" del dispositivo guarda cuantas fichas por pago.
ESTACIONES_FICHAS = _CajasPorTipo({"fichas"})

def cajas_hermanas(caja):
    """Cajas que comparten el mismo ESP fisico (columna esp_id): si una se cae,
    estan TODAS caidas. Se usa para cancelar los QR de todas al irse offline."""
    filas = _tipos_cache()
    esp = (filas.get(caja) or {}).get("esp_id") or caja
    hermanas = {cid for cid, d in filas.items() if (d.get("esp_id") or cid) == esp}
    return hermanas or {caja}

def publicar_activacion(caja_id, pago_id, segundos_override=None):
    """Publica la orden de activar al equipo por MQTT (TLS 8883). El ESP
    deduplica por pago_id. Devuelve True si el publish salió bien.
    segundos_override: si viene (solo lo usa /simular_pago para probar), manda
    ese tiempo en vez del de /config. En pagos reales queda None y manda el
    tiempo configurado, que es el que vale en produccion."""
    if not (_mqtt_publish and MQTT_HOST and MQTT_USER and MQTT_PASS):
        print("MQTT sin configurar (faltan env vars) — no publico")
        return False
    # Mandamos los segundos del conteo (editables en /config) para que el ESP
    # muestre el tiempo correcto sin re-flashear.
    if segundos_override is not None:
        segundos = int(segundos_override)
    else:
        disp = get_dispositivo(caja_id)
        segundos = int(disp["segundos"]) if disp and disp.get("segundos") else 90
    if caja_id in ESTACIONES_FICHAS:
        # expendedora: el campo "segundos" del disp guarda las fichas por pago
        payload = _json.dumps({"accion": "activar", "caja": caja_id, "pago_id": str(pago_id), "cantidad": segundos})
    else:
        payload = _json.dumps({"accion": "activar", "caja": caja_id, "pago_id": str(pago_id), "segundos": segundos})
    try:
        _mqtt_publish.single(
            topic=f"termopago/{caja_id}/cmd", payload=payload, qos=1, retain=False,
            hostname=MQTT_HOST, port=MQTT_PORT,
            auth={"username": MQTT_USER, "password": MQTT_PASS},
            tls={"tls_version": ssl.PROTOCOL_TLS_CLIENT}, keepalive=15,
            client_id="termopago-backend-pub")
        print(f"MQTT activar -> {caja_id} (pago {pago_id})")
        return True
    except Exception as e:
        print(f"Error publicando MQTT a {caja_id}: {e}")
        return False

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ordenes (
            id            TEXT PRIMARY KEY,
            dispositivo_id TEXT,
            segundos      INTEGER,
            estado        TEXT,
            fecha         TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT
        )
    """)
    cur.execute("ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS inicio TEXT")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dispositivos (
            id              TEXT PRIMARY KEY,
            nombre          TEXT,
            external_pos_id TEXT,
            precio          REAL,
            segundos        INTEGER,
            orden_qr_id     TEXT,
            ultimo_rearme   TEXT
        )
    """)
    # token_env: nombre de la variable de entorno con el Access Token del
    # dueño del dispositivo. NULL = usa MP_ACCESS_TOKEN (cuenta propia).
    cur.execute("ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS token_env TEXT")
    # cliente: alias del cliente conectado por OAuth (tabla clientes)
    cur.execute("ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS cliente TEXT")
    # ultimo_poll: última vez que el ESP32 del dispositivo consultó (para
    # detectar equipos sin conexión)
    cur.execute("ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS ultimo_poll TEXT")
    # monto: importe cobrado en cada orden (para estadísticas)
    cur.execute("ALTER TABLE ordenes ADD COLUMN IF NOT EXISTS monto REAL")
    # tipo / esp_id / canal: lo que antes eran los sets ESTACIONES_* y GRUPOS_ESP.
    #   tipo   -> 'pulso' | 'sostenida' | 'fichas' | 'legacy'
    #   esp_id -> ESP fisico que maneja la caja (varias cajas pueden compartirlo)
    #   canal  -> canal dentro de ese ESP (0/1) en los maestro-esclavo
    cur.execute("ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS tipo TEXT")
    cur.execute("ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS esp_id TEXT")
    cur.execute("ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS canal INTEGER")
    cur.execute("ALTER TABLE dispositivos ADD COLUMN IF NOT EXISTS creado TEXT")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clientes (
            alias         TEXT PRIMARY KEY,
            nombre        TEXT,
            mp_user_id    TEXT,
            access_token  TEXT,
            refresh_token TEXT,
            vence         TEXT
        )
    """)
    # panel_token: link secreto del panel propio de cada cliente
    cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS panel_token TEXT")
    # pin: clave corta del cliente para regalar fichas desde su panel
    cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS pin TEXT")
    # freno de fuerza bruta del PIN: intentos fallidos seguidos y, al pasarse,
    # hasta cuando queda bloqueado el regalo para ese cliente
    cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS pin_fallidos INTEGER")
    cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS pin_bloqueado_hasta TEXT")
    # cortes: registro de desconexiones (huecos > 30s en el polling del ESP32)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cortes (
            id             SERIAL PRIMARY KEY,
            dispositivo_id TEXT,
            fin            TEXT,
            duracion_seg   INTEGER
        )
    """)
    # reinicios: cada arranque del ESP reporta la causa del reinicio
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reinicios (
            id             SERIAL PRIMARY KEY,
            dispositivo_id TEXT,
            motivo         TEXT,
            fecha          TEXT
        )
    """)
    # Migración: el termo original, con su caja existente y la config del panel viejo
    cur.execute("SELECT valor FROM config WHERE clave='precio'")
    row = cur.fetchone()
    precio_ini = float(row["valor"]) if row else PRECIO
    cur.execute("SELECT valor FROM config WHERE clave='segundos'")
    row = cur.fetchone()
    segundos_ini = int(row["valor"]) if row else 1800
    cur.execute("SELECT valor FROM config WHERE clave='orden_qr_id'")
    row = cur.fetchone()
    orden_ini = row["valor"] if row else None
    cur.execute("""
        INSERT INTO dispositivos (id, nombre, external_pos_id, precio, segundos, orden_qr_id)
        VALUES ('termo_001', 'Agua caliente', 'default', %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
    """, (precio_ini, segundos_ini, orden_ini))

    # ── Backfill unico de tipo/esp_id/canal desde las semillas ──
    # Solo toca filas con la columna en NULL, asi que es idempotente: si despues
    # cambias un tipo desde /admin, este bloque no lo pisa en el proximo deploy.
    for cid in SEMILLA_FICHAS:
        cur.execute("UPDATE dispositivos SET tipo='fichas' WHERE id=%s AND tipo IS NULL", (cid,))
    for cid in SEMILLA_PULSO - SEMILLA_FICHAS:
        cur.execute("UPDATE dispositivos SET tipo='pulso' WHERE id=%s AND tipo IS NULL", (cid,))
    for cid in SEMILLA_MQTT - SEMILLA_PULSO:
        cur.execute("UPDATE dispositivos SET tipo='sostenida' WHERE id=%s AND tipo IS NULL", (cid,))
    # Lo que no estaba en ningun conjunto MQTT seguia por polling HTTPS.
    cur.execute("UPDATE dispositivos SET tipo='legacy' WHERE tipo IS NULL")
    for esp, cajas in SEMILLA_GRUPOS.items():
        for cid, canal in cajas.items():
            cur.execute("UPDATE dispositivos SET esp_id=%s, canal=%s WHERE id=%s AND esp_id IS NULL",
                        (esp, canal, cid))
    # Caja sola = su propio ESP.
    cur.execute("UPDATE dispositivos SET esp_id=id WHERE esp_id IS NULL")

    conn.commit()
    cur.close()
    conn.close()

init_db()

def mp_headers(token=None):
    return {
        "Authorization": f"Bearer {token or MP_TOKEN}",
        "Content-Type": "application/json"
    }

# ─── Clientes OAuth ──────────────────────────────────────────────

def get_cliente(alias):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clientes WHERE alias=%s", (alias,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def guardar_cliente(alias, campos):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO clientes (alias) VALUES (%s) ON CONFLICT (alias) DO NOTHING", (alias,))
    sets = ", ".join(f"{k}=%s" for k in campos)
    cur.execute(f"UPDATE clientes SET {sets} WHERE alias=%s", list(campos.values()) + [alias])
    conn.commit()
    cur.close()
    conn.close()

def token_cliente(alias):
    """Access Token de un cliente OAuth, renovándolo si está por vencer
    (MP los vence a los 180 días; renovamos con 7 días de margen)."""
    cli = get_cliente(alias)
    if not cli or not cli.get("access_token"):
        return None
    try:
        vence = datetime.fromisoformat(cli["vence"]) if cli.get("vence") else None
    except (ValueError, TypeError):
        vence = None
    if vence and (vence - ahora_ar()).total_seconds() > 7 * 86400:
        return cli["access_token"]
    if not (MP_CLIENT_ID and MP_CLIENT_SECRET and cli.get("refresh_token")):
        return cli["access_token"]
    try:
        r = requests.post("https://api.mercadopago.com/oauth/token", json={
            "client_id": MP_CLIENT_ID,
            "client_secret": MP_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": cli["refresh_token"]
        }, timeout=10)
        if r.status_code in (200, 201):
            t = r.json()
            vence_nuevo = (ahora_ar() + timedelta(seconds=t.get("expires_in", 15552000))).isoformat()
            guardar_cliente(alias, {
                "access_token": t["access_token"],
                "refresh_token": t.get("refresh_token", cli["refresh_token"]),
                "vence": vence_nuevo
            })
            print(f"Token renovado para cliente {alias}")
            return t["access_token"]
        print(f"Error renovando token de {alias}: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"Error renovando token de {alias}: {e}")
    return cli["access_token"]

def token_de(disp):
    """Access Token del dueño del dispositivo (cliente OAuth, cliente
    por variable de entorno, o cuenta propia)."""
    if disp and disp.get("cliente"):
        t = token_cliente(disp["cliente"])
        if t:
            return t
    if disp and disp.get("token_env"):
        return os.environ.get(disp["token_env"], MP_TOKEN)
    return MP_TOKEN

def tokens_conocidos():
    """Todos los tokens configurados (propio + clientes), para el webhook."""
    tokens = [MP_TOKEN]
    for disp in get_dispositivos():
        t = token_de(disp)
        if t and t not in tokens:
            tokens.append(t)
    return tokens

# ─── Dispositivos ────────────────────────────────────────────────

def get_dispositivo(disp_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM dispositivos WHERE id=%s", (disp_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def get_dispositivos():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM dispositivos ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def actualizar_dispositivo(disp_id, campos):
    sets = ", ".join(f"{k}=%s" for k in campos)
    valores = list(campos.values()) + [disp_id]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"UPDATE dispositivos SET {sets} WHERE id=%s", valores)
    conn.commit()
    cur.close()
    conn.close()

def insertar_orden(orden_id, dispositivo_id, segundos, monto=None):
    """Inserta una orden. El PK evita duplicados si MP notifica dos veces."""
    try:
        monto = float(monto) if monto is not None else None
    except (ValueError, TypeError):
        monto = None
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ordenes (id, dispositivo_id, segundos, estado, fecha, monto) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (orden_id, dispositivo_id, segundos, "pendiente", ahora_ar().isoformat(), monto)
    )
    conn.commit()
    cur.close()
    conn.close()

# ─── QR: cancelar y re-armar por dispositivo ─────────────────────

def cancelar_orden_qr(disp):
    """Cancela la orden activa del QR de un dispositivo (si hay una)."""
    if not disp.get("orden_qr_id"):
        return
    headers = mp_headers(token_de(disp))
    headers["X-Idempotency-Key"] = str(uuid.uuid4())
    try:
        r = requests.post(
            f"https://api.mercadopago.com/v1/orders/{disp['orden_qr_id']}/cancel",
            headers=headers, timeout=10
        )
        print(f"Cancelación {disp['id']}: {r.status_code}")
    except Exception as e:
        print(f"Error cancelando orden de {disp['id']}: {e}")

_user_id_cache = {}
def user_id_de(token):
    """user_id (collector) de la cuenta MP de ese token. Cacheado. Necesario
    para el endpoint Instore que limpia la orden de la caja."""
    if not token:
        return None
    if token in _user_id_cache:
        return _user_id_cache[token]
    try:
        r = requests.get("https://api.mercadopago.com/users/me", headers=mp_headers(token), timeout=10)
        if r.status_code == 200:
            uid = r.json().get("id")
            _user_id_cache[token] = uid
            return uid
    except Exception as e:
        print(f"user_id_de error: {e}")
    return None

def limpiar_orden_caja(disp):
    """Borra la orden que quedo pegada en la caja de MP (Instore DELETE). Tras un
    pago, la orden pagada sigue asociada al QR y muestra 'el cobro se esta
    registrando'; hay que eliminarla para que la caja acepte pagos nuevos."""
    try:
        uid = user_id_de(token_de(disp))
        pos = disp.get("external_pos_id")
        if uid and pos:
            r = requests.delete(f"https://api.mercadopago.com/mpmobile/instore/qr/{uid}/{pos}",
                                headers=mp_headers(token_de(disp)), timeout=10)
            print(f"Limpieza caja {disp['id']}: {r.status_code}")
    except Exception as e:
        print(f"Limpieza caja {disp.get('id')}: {e}")

def rearmar_qr(disp):
    """Carga la orden al QR de la caja del dispositivo, con su precio.
    Antes verifica si la orden anterior fue pagada sin que llegara el
    webhook (red de seguridad; el PK evita duplicados)."""
    anterior = disp.get("orden_qr_id")
    if anterior:
        try:
            r = requests.get(f"https://api.mercadopago.com/v1/orders/{anterior}", headers=mp_headers(token_de(disp)), timeout=10)
            if r.status_code == 200:
                estado = r.json().get("status")
                if estado == "processed":
                    o = r.json()
                    insertar_orden(f"ord_{anterior}", o.get("external_reference", disp["id"]), disp["segundos"], o.get("total_amount"))
                    print(f"Pago recuperado por verificación directa: {anterior}")
                elif estado == "created":
                    # sigue activa sin pagar: cancelarla para que la nueva
                    # no choque (renovación sin huecos)
                    cancelar_orden_qr(disp)
        except Exception as e:
            print(f"Error verificando orden anterior de {disp['id']}: {e}")

    headers = mp_headers(token_de(disp))
    headers["X-Idempotency-Key"] = str(uuid.uuid4())
    monto = f"{float(disp['precio']):.2f}"
    if disp["id"] in ESTACIONES_FICHAS:
        # Fichas: el concepto del pago que ve el cliente al escanear (ej "Ficha x 1")
        titulo = f"Ficha x {int(disp['segundos'])}"
    else:
        minutos = disp["segundos"] // 60
        titulo = f"{disp['nombre']} {minutos} minutos" if minutos >= 1 else disp["nombre"]
    orden = {
        "type": "qr",
        "external_reference": disp["id"],
        "description": titulo,
        "expiration_time": "PT15M",
        "total_amount": monto,
        "config": {"qr": {"external_pos_id": disp["external_pos_id"], "mode": "static"}},
        "transactions": {"payments": [{"amount": monto}]},
        "items": [{
            "title": titulo,
            "unit_price": monto,
            "quantity": 1,
            "unit_measure": "unit",
            "external_code": disp["id"].upper()[:30]
        }]
    }
    # Comisión marketplace: solo aplica a dispositivos de clientes OAuth
    if disp.get("cliente") and FEE_PORCENTAJE > 0:
        orden["marketplace_fee"] = f"{float(disp['precio']) * FEE_PORCENTAJE / 100:.2f}"
    ahora = ahora_ar().isoformat()
    # Limpiar la orden vieja pegada en la caja (si no, tras un pago el QR queda
    # en "registrando" y no acepta pagos nuevos aunque creemos una orden nueva).
    limpiar_orden_caja(disp)
    try:
        r = requests.post("https://api.mercadopago.com/v1/orders", json=orden, headers=headers, timeout=10)
        if r.status_code == 201:
            actualizar_dispositivo(disp["id"], {"orden_qr_id": r.json().get("id", ""), "ultimo_rearme": ahora})
            print(f"QR re-armado: {disp['id']}")
        else:
            # No se pudo crear la orden nueva. Para cuando llegamos aca la anterior
            # ya fue pagada o cancelada, o sea NO hay QR pagable. Marcamos el QR como
            # muerto (orden_qr_id=None, ultimo_rearme=None) para que el proximo
            # heartbeat (60s) REINTENTE enseguida, en vez de dejarlo roto 10 min.
            actualizar_dispositivo(disp["id"], {"orden_qr_id": None, "ultimo_rearme": None})
            print(f"Re-arme {disp['id']} FALLO ({r.status_code}): {r.text[:200]} -> reintenta al proximo heartbeat")
    except Exception as e:
        print(f"Error re-armando QR de {disp['id']}: {e}")

# ─── Rutas del ESP32 ────────────────────────────────────────────

# ─── Landing pública (termopago.com.ar) ──────────────────────────

@app.route("/")
def landing():
    ruta = os.path.join(os.path.dirname(__file__), "web", "index.html")
    try:
        with open(ruta, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "TermoPago", 200

@app.route("/health")
def health():
    """Chequeo para monitoreo externo (UptimeRobot). Devuelve 200 solo si el
    backend Y la base responden. Si la DB esta caida -> 503 -> el monitor avisa.
    Publico y liviano (SELECT 1), sin datos sensibles."""
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone(); cur.close(); conn.close()
        return "ok", 200
    except Exception as e:
        print(f"/health FALLO: {e}")
        return "db-error", 503

@app.route("/logo/<path:archivo>")
def logo_static(archivo):
    from flask import send_from_directory
    carpeta = os.path.join(os.path.dirname(__file__), "web", "logo")
    return send_from_directory(carpeta, archivo)

# ─── Rutas del ESP32 ─────────────────────────────────────────────

@app.route("/orden/<dispositivo_id>")
def consultar_orden(dispositivo_id):
    # re-armar el QR del dispositivo si nunca se armó o pasaron más de 10 min
    # (la orden vence a los 15: ventana de riesgo máxima si se corta la luz)
    disp = get_dispositivo(dispositivo_id)
    if disp:
        # detectar corte REAL: si el poll anterior fue hace más de 30s.
        # Pero NO contar el hueco si la máquina estuvo ANDANDO en ese lapso
        # (mientras corre su tiempo no pollea ese canal -> hueco normal, no corte).
        up = disp.get("ultimo_poll")
        if up:
            try:
                gap = (ahora_ar() - datetime.fromisoformat(up)).total_seconds()
                if gap > 30:
                    conn0 = get_db(); c0 = conn0.cursor()
                    # ¿hubo una orden que arrancó durante el hueco? -> estuvo andando
                    c0.execute("SELECT 1 FROM ordenes WHERE dispositivo_id=%s AND inicio IS NOT NULL AND inicio >= %s LIMIT 1",
                               (dispositivo_id, up))
                    estuvo_andando = c0.fetchone() is not None
                    if not estuvo_andando:
                        c0.execute("INSERT INTO cortes (dispositivo_id, fin, duracion_seg) VALUES (%s,%s,%s)",
                                   (dispositivo_id, ahora_ar().isoformat(), int(gap)))
                        conn0.commit()
                    c0.close(); conn0.close()
            except (ValueError, TypeError):
                pass
        # registrar que el equipo está vivo (para el reembolso automático)
        actualizar_dispositivo(dispositivo_id, {"ultimo_poll": ahora_ar().isoformat()})
        rearme = disp.get("ultimo_rearme")
        try:
            vencido = (not rearme) or (ahora_ar() - datetime.fromisoformat(rearme)).total_seconds() > 600
        except (ValueError, TypeError):
            vencido = True
        if vencido:
            rearmar_qr(disp)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM ordenes WHERE dispositivo_id=%s AND estado='pendiente' ORDER BY fecha ASC LIMIT 1",
        (dispositivo_id,)
    )
    orden = cur.fetchone()
    if orden:
        cur.execute(
            "UPDATE ordenes SET estado='ejecutando', inicio=%s WHERE id=%s",
            (ahora_ar().isoformat(), orden["id"])
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"encender": True, "segundos": orden["segundos"], "orden_id": orden["id"]})

    # Recuperación tras corte de luz / reinicio: si hay una orden en ejecución
    # que no se completó y todavía le queda tiempo, devolver el restante.
    cur.execute(
        "SELECT * FROM ordenes WHERE dispositivo_id=%s AND estado='ejecutando' AND inicio IS NOT NULL "
        "ORDER BY inicio DESC LIMIT 1",
        (dispositivo_id,)
    )
    ejecutando = cur.fetchone()
    cur.close()
    conn.close()
    if ejecutando:
        try:
            transcurrido = (ahora_ar() - datetime.fromisoformat(ejecutando["inicio"])).total_seconds()
            restante = int(ejecutando["segundos"] - transcurrido)
            if restante > 5:
                return jsonify({"encender": True, "segundos": restante, "orden_id": ejecutando["id"]})
        except (ValueError, TypeError):
            pass
    return jsonify({"encender": False})

@app.route("/completar/<orden_id>")
def completar_orden(orden_id):
    """El ESP32 avisa que terminó el servicio de una orden."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE ordenes SET estado='completada' WHERE id=%s", (orden_id,))
    conn.commit()
    cur.close()
    conn.close()
    return "ok"

# ─── Simulación de pago para pruebas ────────────────────────────

@app.route("/simular_pago/<clave>")
@app.route("/simular_pago/<clave>/<int:segundos>")
@app.route("/simular_pago/<clave>/<int:segundos>/<dispositivo_id>")
def simular_pago(clave, segundos=10, dispositivo_id="termo_001"):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    oid = str(uuid.uuid4())
    insertar_orden(oid, dispositivo_id, segundos)
    # Cajas MQTT: no pollean /orden, se activan por push. Pulso -> completada al
    # toque; sostenido -> ejecutando (para calcular el restante en recuperacion).
    if dispositivo_id in ESTACIONES_MQTT:
        # En simulacion mandamos el tiempo de la URL para poder probar distintos
        # valores sin tocar /config. Los pagos reales usan el tiempo de config.
        ok = publicar_activacion(dispositivo_id, oid, segundos_override=segundos)
        if ok:
            if dispositivo_id in ESTACIONES_PULSO:
                marcar_orden(oid, "completada")
            else:
                marcar_ejecutando(oid)
        return f"Pago simulado (MQTT) -> {dispositivo_id}: {'enviado' if ok else 'FALLO publish'}"
    return f"Pago simulado: {dispositivo_id}, {segundos} segundos"

# ─── Webhook MercadoPago ─────────────────────────────────────────

def segundos_de(dispositivo_id):
    disp = get_dispositivo(dispositivo_id)
    return disp["segundos"] if disp else 1800

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data:
        return "ok", 200

    topic = data.get("topic") or data.get("type")

    # Notificaciones de la Orders API (tema "orders" del panel de MP).
    # Puede venir de la cuenta propia o de la de un cliente: se prueba
    # cada token conocido hasta encontrar la orden.
    if topic in ("order", "orders", "topic_order") or (data.get("data", {}).get("id", "") or "").startswith("ORD"):
        order_id = data["data"]["id"]
        order = {}
        for token in tokens_conocidos():
            r = requests.get(f"https://api.mercadopago.com/v1/orders/{order_id}", headers=mp_headers(token))
            if r.status_code == 200:
                order = r.json()
                break
        if order.get("status") == "processed":
            dispositivo_id = order.get("external_reference", "termo_001")
            oid = f"ord_{order_id}"
            insertar_orden(oid, dispositivo_id, segundos_de(dispositivo_id), order.get("total_amount"))
            print(f"Pago QR aprobado para {dispositivo_id}")
            # Cajas MQTT: activar por push. Pulso (inflado) -> completada al toque
            # (instantaneo). Sostenido (aspiradora/soplado) -> ejecutando con
            # inicio, para que la recuperacion tras corte calcule el restante.
            # (El reembolso automatico solo toca 'pendiente', asi que ninguna de
            # las dos queda en riesgo.) Se pasa el id completo de la orden para
            # que coincida con el orden_id de /orden (dedup en el esclavo).
            if dispositivo_id in ESTACIONES_MQTT:
                if publicar_activacion(dispositivo_id, oid):
                    if dispositivo_id in ESTACIONES_PULSO:
                        marcar_orden(oid, "completada")
                    else:
                        marcar_ejecutando(oid)
            disp = get_dispositivo(dispositivo_id)
            if disp:
                rearmar_qr(disp)  # dejar el QR listo para el próximo cliente
        return "ok", 200

    # Formato IPN legacy (merchant_order)
    if topic == "merchant_order":
        if "resource" in data:
            url = data["resource"]
        else:
            url = f"https://api.mercadopago.com/merchant_orders/{data['data']['id']}"
        r = requests.get(url, headers=mp_headers())
        order = r.json()
        pagos_aprobados = [p for p in order.get("payments", []) if p["status"] == "approved"]
        if pagos_aprobados and order.get("order_status") == "paid":
            dispositivo_id = order.get("external_reference", "termo_001")
            insertar_orden(f"mo_{order['id']}", dispositivo_id, segundos_de(dispositivo_id), order.get("total_amount"))
            print(f"Pago QR (legacy) aprobado para {dispositivo_id}")
        return "ok", 200

    # Pagos de Checkout Pro
    if topic == "payment":
        sdk = mercadopago.SDK(MP_TOKEN)
        pago_id = data["data"]["id"]
        pago = sdk.payment().get(pago_id)["response"]
        if pago.get("status") == "approved":
            dispositivo_id = pago.get("metadata", {}).get("dispositivo_id", "termo_001")
            insertar_orden(f"pay_{pago_id}", dispositivo_id, segundos_de(dispositivo_id), pago.get("transaction_amount"))
            print(f"Pago checkout aprobado: {pago_id}")
        return "ok", 200

    return "ok", 200

# ─── OAuth marketplace: conexión de clientes con un click ────────

@app.route("/conectar_cliente/<clave>/<alias>/<nombre>")
def conectar_cliente(clave, alias, nombre):
    """Genera el link de autorización para mandarle al cliente.
    Ej: /conectar_cliente/CLAVE/lavadero/Lavadero-San-Martin"""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    if not (MP_CLIENT_ID and MP_CLIENT_SECRET):
        return jsonify({"error": "Faltan MP_CLIENT_ID y MP_CLIENT_SECRET en Railway"}), 400
    guardar_cliente(alias, {"nombre": nombre.replace("-", " ")})
    link = (
        "https://auth.mercadopago.com.ar/authorization"
        f"?client_id={MP_CLIENT_ID}&response_type=code&platform_id=mp"
        f"&state={alias}&redirect_uri={BASE_URL}/oauth_callback"
    )
    return jsonify({
        "cliente": alias,
        "link_para_el_cliente": link,
        "instrucciones": "Mandale este link al cliente. Lo abre, inicia sesión en su MP y acepta. Listo."
    })

@app.route("/oauth_callback")
def oauth_callback():
    """MP redirige acá cuando el cliente acepta la autorización."""
    code = request.args.get("code")
    alias = request.args.get("state")
    if not code or not alias:
        return "Faltan parámetros", 400
    r = requests.post("https://api.mercadopago.com/oauth/token", json={
        "client_id": MP_CLIENT_ID,
        "client_secret": MP_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": f"{BASE_URL}/oauth_callback"
    }, timeout=10)
    if r.status_code not in (200, 201):
        print(f"Error OAuth: {r.status_code} {r.text[:300]}")
        return "<h2>Hubo un problema al conectar la cuenta. Avisale a TermoPago.</h2>", 400
    t = r.json()
    vence = (ahora_ar() + timedelta(seconds=t.get("expires_in", 15552000))).isoformat()
    guardar_cliente(alias, {
        "mp_user_id": str(t.get("user_id", "")),
        "access_token": t["access_token"],
        "refresh_token": t.get("refresh_token", ""),
        "vence": vence
    })
    print(f"Cliente OAuth conectado: {alias} (user_id {t.get('user_id')})")
    return """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:sans-serif;text-align:center;margin-top:80px">
<h1>✅ ¡Cuenta conectada!</h1>
<p>Tu MercadoPago quedó vinculado a TermoPago.<br>Ya podés cerrar esta ventana.</p>
</body></html>"""

@app.route("/ver_clientes/<clave>")
def ver_clientes(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT alias, nombre, mp_user_id, vence, (access_token IS NOT NULL) AS conectado FROM clientes ORDER BY alias")
    filas = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(f) for f in filas])

@app.route("/oauth_estado/<clave>")
def oauth_estado(clave):
    """Diagnostico del marketplace OAuth: dice si las variables estan
    cargadas, que redirect_uri se usa, y el estado de cada cliente y sus
    cajas. NO expone secretos (solo si estan seteados o no)."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    redirect_uri = f"{BASE_URL}/oauth_callback"
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT alias, nombre, mp_user_id, vence, "
                "(access_token IS NOT NULL) AS conectado FROM clientes ORDER BY alias")
    filas = cur.fetchall()
    cur.close()
    conn.close()

    clientes = []
    for f in filas:
        dias = None
        try:
            if f.get("vence"):
                dias = round((datetime.fromisoformat(f["vence"]) - ahora_ar()).total_seconds() / 86400, 1)
        except (ValueError, TypeError):
            dias = None
        cajas = [d["id"] for d in get_dispositivos() if d.get("cliente") == f["alias"]]
        clientes.append({
            "alias": f["alias"],
            "nombre": f["nombre"],
            "mp_user_id": f["mp_user_id"],
            "conectado": bool(f["conectado"]),
            "dias_para_vencer": dias,
            "cajas": cajas,
        })

    listo = bool(MP_CLIENT_ID and MP_CLIENT_SECRET)
    return jsonify({
        "marketplace_listo": listo,
        "variables": {
            "MP_CLIENT_ID": "OK" if MP_CLIENT_ID else "FALTA",
            "MP_CLIENT_SECRET": "OK" if MP_CLIENT_SECRET else "FALTA",
            "FEE_PORCENTAJE": FEE_PORCENTAJE,
        },
        "redirect_uri_a_registrar_en_mp": redirect_uri,
        "clientes": clientes,
        "ayuda": "Registra el redirect_uri en Tus Integraciones -> tu app -> Redirect URIs. "
                 "Genera el link con /conectar_cliente/<clave>/<alias>/<nombre>.",
    })


# ─── Panel propio del cliente (link secreto, ve solo lo suyo) ─────

def get_cliente_por_token(token):
    if not token:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clientes WHERE panel_token=%s", (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


@app.route("/panel_link/<clave>/<alias>")
def panel_link(clave, alias):
    """Genera (o devuelve) el link secreto del panel de un cliente.
    Ej: /panel_link/CLAVE/villagas"""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    cli = get_cliente(alias)
    if not cli:
        return jsonify({"error": f"No existe el cliente '{alias}' (usar /conectar_cliente)"}), 404
    token = cli.get("panel_token")
    if not token:
        token = secrets.token_urlsafe(16)
        guardar_cliente(alias, {"panel_token": token})
    pin = cli.get("pin")
    if not pin:
        pin = f"{secrets.randbelow(10000):04d}"
        guardar_cliente(alias, {"pin": pin})
    return jsonify({
        "cliente": alias,
        "nombre": cli.get("nombre"),
        "panel": f"{BASE_URL}/panel/{token}",
        "pin_para_regalar": pin,
        "instrucciones": "Mandale el link al cliente (es privado). El PIN es para que pueda regalar fichas desde el panel."
    })


def regalar_ficha(cli, mis, pin_ingresado):
    """El cliente regala 1 ficha desde su panel, validando su PIN. Se registra
    como orden 'gift_' -> NO cuenta como venta, pero queda el rastro."""
    alias = cli["alias"]
    pin_real = (cli.get("pin") or "").strip()
    if not pin_real:
        return "No tenes PIN configurado todavia. Pediselo a TermoPago."

    # Freno de fuerza bruta: un PIN son 4 digitos = 10.000 combinaciones, o sea
    # nada para un script. Sin esto, cualquiera con el link del panel se saca
    # todas las fichas que quiera probando. El contador va en la DB (no en
    # memoria) para que un redeploy no le devuelva los intentos al atacante.
    ahora = ahora_ar()
    bloqueado = cli.get("pin_bloqueado_hasta")
    if bloqueado:
        try:
            hasta = datetime.fromisoformat(bloqueado)
        except (ValueError, TypeError):
            hasta = None
        if hasta and hasta > ahora:
            faltan = int((hasta - ahora).total_seconds() // 60) + 1
            return (f"Demasiados intentos con el PIN equivocado. "
                    f"Proba de nuevo en {faltan} minuto(s).")

    if (pin_ingresado or "").strip() != pin_real:
        fallidos = int(cli.get("pin_fallidos") or 0) + 1
        if fallidos >= PIN_MAX_INTENTOS:
            guardar_cliente(alias, {
                "pin_fallidos": 0,
                "pin_bloqueado_hasta": (ahora + timedelta(minutes=PIN_BLOQUEO_MIN)).isoformat(),
            })
            print(f"[PIN] {alias}: {PIN_MAX_INTENTOS} intentos fallidos -> bloqueado {PIN_BLOQUEO_MIN} min")
            return (f"PIN incorrecto. Por seguridad se bloqueo el regalo de fichas "
                    f"por {PIN_BLOQUEO_MIN} minutos.")
        guardar_cliente(alias, {"pin_fallidos": fallidos})
        restantes = PIN_MAX_INTENTOS - fallidos
        return (f"PIN incorrecto. No se regalo ninguna ficha. "
                f"Te queda(n) {restantes} intento(s) antes de que se bloquee.")

    # PIN correcto: se limpia el contador y cualquier bloqueo pendiente.
    if cli.get("pin_fallidos") or cli.get("pin_bloqueado_hasta"):
        guardar_cliente(alias, {"pin_fallidos": 0, "pin_bloqueado_hasta": None})

    fichas = [d for d in mis if d["id"] in ESTACIONES_FICHAS]
    if not fichas:
        return "No tenes una expendedora de fichas asignada."
    ahora = ahora_ar()
    regaladas = 0
    for d in fichas:
        up = d.get("ultimo_poll")
        try:
            seg = (ahora - datetime.fromisoformat(up)).total_seconds() if up else None
        except (ValueError, TypeError):
            seg = None
        if seg is None or seg > 600:
            return f"La maquina '{d['nombre']}' esta desconectada. Proba cuando vuelva a estar en linea."
        oid = "gift_" + uuid.uuid4().hex[:16]
        if publicar_activacion(d["id"], oid, segundos_override=1):
            insertar_orden(oid, d["id"], 1, 0)
            marcar_orden(oid, "regalada")
            regaladas += 1
    if regaladas:
        return f"🎁 Listo! Regalaste {regaladas} ficha(s). Ya sale de la maquina."
    return "No se pudo regalar (MQTT sin configurar o error de envio)."


@app.route("/panel/<token>", methods=["GET", "POST"])
def panel_cliente(token):
    """Panel propio del cliente: ve SOLO sus maquinas (estado, precio/tiempo,
    ventas e historial) y puede editar precio y tiempo. No usa la clave
    maestra ni ve datos de otros clientes."""
    cli = get_cliente_por_token(token)
    if not cli:
        return "<h2>Link invalido</h2>", 404
    alias = cli["alias"]
    nombre_cli = cli.get("nombre") or alias

    mis = [d for d in get_dispositivos() if d.get("cliente") == alias]

    mensaje = ""
    if request.method == "POST" and request.form.get("accion") == "regalar":
        mensaje = regalar_ficha(cli, mis, request.form.get("pin", ""))

    ahora = ahora_ar()

    tarjetas_estado = ""
    info_maquinas = ""
    for d in mis:
        up = d.get("ultimo_poll")
        try:
            seg = (ahora - datetime.fromisoformat(up)).total_seconds() if up else None
        except (ValueError, TypeError):
            seg = None
        if seg is None:
            color, txt = "#9e9e9e", "Nunca conecto"
        elif seg < 90:
            color, txt = "#2e7d32", "🟢 Conectado"
        elif seg < 600:
            color, txt = "#f9a825", "🟡 Intermitente"
        else:
            color, txt = "#c62828", "🔴 Caido"
        if seg is None:
            hace = "—"
        elif seg < 60:      hace = f"hace {int(seg)} seg"
        elif seg < 3600:    hace = f"hace {int(seg//60)} min"
        elif seg < 86400:   hace = f"hace {int(seg//3600)} h"
        else:               hace = f"hace {int(seg//86400)} dias"

        tarjetas_estado += (
            f'<div class="est"><div class="en">{d["nombre"]}</div>'
            f'<div style="color:{color};font-weight:600">{txt}</div>'
            f'<div class="eh">Ultimo contacto: {hace}</div></div>')

        if d["id"] in ESTACIONES_FICHAS:
            unidad = f'{int(d["segundos"])} ficha(s) por pago'
        elif d["id"] in ESTACIONES_MQTT:
            unidad = f'{int(d["segundos"])} seg de conteo'
        else:
            unidad = f'{d["segundos"] // 60} min'
        info_maquinas += (
            f'<div class="est"><div class="en">{d["nombre"]}</div>'
            f'<div class="eh">Precio: ${float(d["precio"]):g} · {unidad}</div></div>')

    if not mis:
        tarjetas_estado = '<div class="est">Todavia no tenes maquinas asignadas.</div>'

    mis_ids = tuple(d["id"] for d in mis)
    def nuevo(): return {"ventas": 0, "monto": 0.0}
    tot_hoy, tot_mes, tot_all = nuevo(), nuevo(), nuevo()
    por_dia = {}
    rows = []
    if mis_ids:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            r"""SELECT id, dispositivo_id, fecha, COALESCE(monto,0) AS monto, estado
                FROM ordenes WHERE dispositivo_id IN %s
                AND (id LIKE 'ord\_%%' OR id LIKE 'pay\_%%' OR id LIKE 'mo\_%%' OR id LIKE 'gift\_%%')
                ORDER BY fecha DESC""",
            (mis_ids,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        hoy = ahora.strftime("%Y-%m-%d")
        mes_actual = ahora.strftime("%Y-%m")
        for r in rows:
            if (r["id"] or "").startswith("gift_"):
                continue   # las regaladas se muestran en la lista pero NO facturan
            dia = (r["fecha"] or "")[:10]
            m = float(r["monto"] or 0)
            for dest in (por_dia.setdefault(dia, nuevo()), tot_all):
                dest["ventas"] += 1; dest["monto"] += m
            if dia == hoy:
                tot_hoy["ventas"] += 1; tot_hoy["monto"] += m
            if dia[:7] == mes_actual:
                tot_mes["ventas"] += 1; tot_mes["monto"] += m

    regaladas_mes = 0
    if mis_ids:
        conn = get_db(); cur = conn.cursor()
        cur.execute(r"""SELECT fecha FROM ordenes WHERE dispositivo_id IN %s AND id LIKE 'gift\_%%'""", (mis_ids,))
        for rg in cur.fetchall():
            if (rg["fecha"] or "")[:7] == ahora.strftime("%Y-%m"):
                regaladas_mes += 1
        cur.close(); conn.close()

    bloque_regalo = ""
    if any(d["id"] in ESTACIONES_FICHAS for d in mis):
        bloque_regalo = (
            '<h3>🎁 Regalar una ficha</h3>'
            '<form method="post">'
            '<input type="hidden" name="accion" value="regalar">'
            '<label>Tu PIN</label>'
            '<input type="text" inputmode="numeric" name="pin" placeholder="PIN" autocomplete="off">'
            '<button type="submit">Regalar 1 ficha</button>'
            '</form>'
            f'<p class="sub">Regalaste {regaladas_mes} ficha(s) este mes.</p>'
        )

    def tarjeta(t, d):
        return (f'<div class="card"><div class="ct">{t}</div>'
                f'<div class="cv">${d["monto"]:,.0f}</div>'
                f'<div class="cs">{d["ventas"]} ventas</div></div>')

    nombres = {d["id"]: d["nombre"] for d in mis}
    ventas_rows = [o for o in rows if not (o["id"] or "").startswith("gift_")][:25]
    regalo_rows = [o for o in rows if (o["id"] or "").startswith("gift_")][:25]

    def _dia_hora(o):
        f = o["fecha"] or ""
        dia = (f[8:10] + "/" + f[5:7]) if len(f) >= 10 else f
        hora = f[11:16] if len(f) >= 16 else ""
        return dia, hora

    filas_hist = ""
    for o in ventas_rows:
        dia, hora = _dia_hora(o)
        monto = f"${float(o['monto']):,.0f}" if o.get("monto") else "—"
        filas_hist += (f'<tr><td>{dia}</td><td><b>{hora}</b></td>'
                       f'<td>{nombres.get(o["dispositivo_id"], o["dispositivo_id"])}</td>'
                       f'<td style="text-align:right">{monto}</td></tr>')
    if not filas_hist:
        filas_hist = '<tr><td colspan="4">Sin ventas todavia</td></tr>'

    filas_regalos = ""
    for o in regalo_rows:
        dia, hora = _dia_hora(o)
        filas_regalos += (f'<tr><td>{dia}</td><td><b>{hora}</b></td>'
                          f'<td>{nombres.get(o["dispositivo_id"], o["dispositivo_id"])}</td></tr>')
    if not filas_regalos:
        filas_regalos = '<tr><td colspan="3">Sin regaladas todavia</td></tr>'

    filas_dia = ""
    for k in sorted(por_dia.keys(), reverse=True)[:30]:
        d = por_dia[k]
        filas_dia += f'<tr><td>{k}</td><td>{d["ventas"]}</td><td>${d["monto"]:,.0f}</td></tr>'
    if not filas_dia:
        filas_dia = '<tr><td colspan="3">Sin datos</td></tr>'

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TermoPago - {nombre_cli}</title>
<style>
  body {{ font-family: sans-serif; max-width: 640px; margin: 20px auto; padding: 0 14px; color:#222; }}
  h2 {{ margin-bottom: 2px; }}
  h3 {{ margin: 26px 0 8px; color:#1b4f72; }}
  .sub {{ color:#888; font-size:13px; margin-bottom:12px; }}
  .est {{ background:#f4f8fb; border:1px solid #dce6ee; border-radius:10px; padding:12px 14px; margin-top:10px; }}
  .en {{ font-weight:bold; }}
  .eh {{ color:#888; font-size:13px; margin-top:2px; }}
  .cards {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }}
  .card {{ flex:1; min-width:120px; background:#009ee3; color:white; border-radius:10px; padding:12px 14px; }}
  .ct {{ font-size:13px; opacity:.9; }} .cv {{ font-size:24px; font-weight:bold; margin:2px 0; }}
  .cs {{ font-size:12px; opacity:.9; }}
  fieldset {{ margin-top: 12px; border: 1px solid #ccc; border-radius: 8px; padding: 12px; }}
  legend {{ font-weight: bold; padding: 0 6px; }}
  label {{ display:block; margin-top:10px; font-size:14px; }}
  input {{ width:100%; padding:10px; font-size:17px; margin-top:4px; box-sizing:border-box; }}
  button {{ margin-top:16px; width:100%; padding:14px; font-size:17px; background:#009ee3;
           color:white; border:none; border-radius:6px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 7px 10px; text-align:left; font-size:14px; }}
  th {{ background:#eaf4fb; color:#1b4f72; }}
  tr:nth-child(even) td {{ background:#f7f9fb; }}
  .msg {{ margin-top:14px; font-size:15px; color:#1b7a2e; }}
</style></head><body>
<h2>👋 Hola, {nombre_cli}</h2>
<div class="sub">Panel de tus maquinas · hora de Argentina</div>
<p class="msg">{mensaje}</p>

<h3>📡 Estado de conexion</h3>
{tarjetas_estado}

<h3>📊 Ventas</h3>
<div class="cards">{tarjeta("Hoy", tot_hoy)}{tarjeta("Este mes", tot_mes)}{tarjeta("Historico", tot_all)}</div>

{bloque_regalo}

<h3>💲 Precio y cantidad</h3>
{info_maquinas}
<p class="sub">El precio y la cantidad de fichas los ajusta TermoPago. Si necesitás un cambio, avisanos.</p>

<h3>🧾 Ultimas ventas</h3>
<table><tr><th>Dia</th><th>Hora</th><th>Maquina</th><th style="text-align:right">Monto</th></tr>{filas_hist}</table>

<h3>🎁 Fichas regaladas</h3>
<table><tr><th>Dia</th><th>Hora</th><th>Maquina</th></tr>{filas_regalos}</table>

<h3>📅 Por dia (ultimos 30)</h3>
<table><tr><th>Dia</th><th>Ventas</th><th>Facturado</th></tr>{filas_dia}</table>

<p class="sub" style="margin-top:22px">Los montos se registran desde julio 2026; ventas anteriores pueden figurar en $0.</p>
</body></html>"""


# ─── Alta de dispositivos: crea la caja y el QR en MercadoPago ────

# ═══════════════════════════════════════════════════════════════════
#  PANEL DE ADMINISTRACION  /admin/<clave>
#  Alta de clientes y de sus maquinas sin tocar codigo ni redeployar.
# ═══════════════════════════════════════════════════════════════════

import html as _html
import io as _io
import zipfile as _zipfile
import re as _re

FIRMWARE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware")

# Que sketch le corresponde a cada tipo. Se toma la primera carpeta que exista,
# asi los nombres viejos siguen andando mientras se migra a los universales.
SKETCH_POR_TIPO = {
    "fichas":    ["termopago_fichas", "expendedora_villagas", "expendedora_fichas"],
    "pulso":     ["termopago_pulso", "inflado01"],
    "sostenida": ["termopago_sostenida", "estacion01_maestro_mqtt", "estacion02_maestro_mqtt"],
    "legacy":    [],
}

def sketch_de(tipo):
    """Carpeta de firmware que hay que flashear para ese tipo de caja."""
    for nombre in SKETCH_POR_TIPO.get(tipo, []):
        if os.path.isdir(os.path.join(FIRMWARE_DIR, nombre)):
            return nombre
    candidatos = SKETCH_POR_TIPO.get(tipo) or []
    return candidatos[0] if candidatos else None

def _esc(x):
    return _html.escape(str(x if x is not None else ""))

def _slug(x):
    """Deja solo lo que puede ir en un id de caja y en un topic MQTT."""
    return _re.sub(r"[^a-z0-9_]", "", (x or "").strip().lower().replace(" ", "_").replace("-", "_"))

def get_clientes():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM clientes ORDER BY alias")
    filas = cur.fetchall()
    cur.close()
    conn.close()
    return filas

def asegurar_panel(alias):
    """Devuelve (link_del_panel, pin) del cliente, creandolos si no existían."""
    cli = get_cliente(alias)
    if not cli:
        return None, None
    token = cli.get("panel_token")
    if not token:
        token = secrets.token_urlsafe(16)
        guardar_cliente(alias, {"panel_token": token})
    pin = cli.get("pin")
    if not pin:
        pin = f"{secrets.randbelow(10000):04d}"
        guardar_cliente(alias, {"pin": pin})
    return f"{BASE_URL}/panel/{token}", pin

def link_oauth(alias):
    if not (MP_CLIENT_ID and MP_CLIENT_SECRET):
        return None
    return ("https://auth.mercadopago.com.ar/authorization"
            f"?client_id={MP_CLIENT_ID}&response_type=code&platform_id=mp"
            f"&state={alias}&redirect_uri={BASE_URL}/oauth_callback")

def _estado_caja(disp, ahora):
    """(color, texto, hace) segun cuando fue el ultimo latido del equipo."""
    up = disp.get("ultimo_poll")
    try:
        seg = (ahora - datetime.fromisoformat(up)).total_seconds() if up else None
    except (ValueError, TypeError):
        seg = None
    if seg is None:
        return "#9e9e9e", "Nunca conectó", "—"
    if seg < 90:
        color, txt = "#2e7d32", "🟢 En línea"
    elif seg < 600:
        color, txt = "#f9a825", "🟡 Intermitente"
    else:
        color, txt = "#c62828", "🔴 Caído"
    if seg < 60:      hace = f"hace {int(seg)} s"
    elif seg < 3600:  hace = f"hace {int(seg // 60)} min"
    elif seg < 86400: hace = f"hace {int(seg // 3600)} h"
    else:             hace = f"hace {int(seg // 86400)} d"
    return color, txt, hace


def _admin_post(clave):
    """Procesa los formularios del panel. Devuelve el mensaje a mostrar."""
    accion = request.form.get("accion")

    if accion == "nuevo_cliente":
        alias = _slug(request.form.get("alias"))
        nombre = (request.form.get("nombre") or "").strip()
        if not alias:
            return "❌ Falta el alias (letras y números, sin espacios)."
        if get_cliente(alias):
            return f"❌ Ya existe un cliente con el alias '{alias}'."
        guardar_cliente(alias, {"nombre": nombre or alias})
        panel, pin = asegurar_panel(alias)
        return (f"✅ Cliente <b>{_esc(nombre or alias)}</b> creado. "
                f"Panel: <a href='{panel}'>{panel}</a> · PIN {pin}. "
                f"Ahora conectá su cuenta de MercadoPago y agregale las máquinas.")

    if accion == "nueva_maquina":
        alias = _slug(request.form.get("cliente"))
        disp_id = _slug(request.form.get("disp_id"))
        nombre = (request.form.get("nombre") or "").strip()
        tipo = request.form.get("tipo")
        if not disp_id:
            return "❌ Falta el ID de la máquina."
        if not nombre:
            return "❌ Falta el nombre de la máquina."
        if tipo not in TIPOS:
            return "❌ Tipo de máquina inválido."
        if get_dispositivo(disp_id):
            return f"❌ Ya existe una máquina con el ID '{disp_id}'."
        # De qué cuenta de MP cobra: OAuth del cliente, variable de Railway, o la propia.
        origen = (request.form.get("cuenta") or "").strip()
        if origen == "cliente" and alias:
            token_env = f"cliente_{alias}"
        elif origen.startswith("MP_"):
            token_env = origen
        else:
            token_env = None
        res = alta_dispositivo(
            disp_id, nombre, token_env,
            tipo=tipo,
            esp_id=_slug(request.form.get("esp_id")) or disp_id,
            canal=request.form.get("canal"),
            precio=request.form.get("precio"),
            valor=request.form.get("valor"),
        )
        if "error" in res:
            return f"❌ {_esc(res['error'])} <small>{_esc(res.get('detalle', ''))}</small>"
        # Si la caja es de un cliente OAuth, dejarla asociada aunque el alta haya
        # usado una variable de entorno (asi aparece en el panel del cliente).
        if alias and not res.get("cliente"):
            actualizar_dispositivo(disp_id, {"cliente": alias})
            invalidar_cache_tipos()
        return (f"✅ Máquina <b>{_esc(nombre)}</b> creada ({_esc(tipo)}). "
                f"<a href='/admin/{clave}/maquina/{disp_id}'>Ver cómo flashear el equipo →</a>")

    if accion == "editar_maquina":
        disp_id = request.form.get("disp_id")
        disp = get_dispositivo(disp_id)
        if not disp:
            return "❌ No existe esa máquina."
        campos = {}
        tipo = request.form.get("tipo")
        if tipo in TIPOS:
            campos["tipo"] = tipo
        esp = _slug(request.form.get("esp_id"))
        if esp:
            campos["esp_id"] = esp
        canal = request.form.get("canal")
        campos["canal"] = int(canal) if (canal or "").strip().isdigit() else None
        actualizar_dispositivo(disp_id, campos)
        invalidar_cache_tipos()
        return f"✅ Actualizada la máquina <b>{_esc(disp['nombre'])}</b>."

    if accion == "nuevo_pin":
        alias = request.form.get("alias")
        cli = get_cliente(alias)
        if not cli:
            return "❌ No existe ese cliente."
        pin = f"{secrets.randbelow(10000):04d}"
        # PIN nuevo = borrón y cuenta nueva: se levanta cualquier bloqueo por
        # intentos fallidos, así el cliente puede usarlo en el momento.
        guardar_cliente(alias, {"pin": pin, "pin_fallidos": 0, "pin_bloqueado_hasta": None})
        return (f"🔑 PIN nuevo de <b>{_esc(cli.get('nombre') or alias)}</b>: <b>{pin}</b>. "
                f"El anterior dejó de servir para regalar fichas — pasale este.")

    if accion == "nuevo_link":
        alias = request.form.get("alias")
        cli = get_cliente(alias)
        if not cli:
            return "❌ No existe ese cliente."
        token = secrets.token_urlsafe(16)
        guardar_cliente(alias, {"panel_token": token})
        nuevo = f"{BASE_URL}/panel/{token}"
        return (f"🔗 Link nuevo de <b>{_esc(cli.get('nombre') or alias)}</b>: "
                f"<a href='{nuevo}'>{nuevo}</a>. El anterior ya no abre nada, "
                f"así que mandale este.")

    if accion == "borrar_cliente":
        alias = request.form.get("alias")
        cli = get_cliente(alias)
        if not cli:
            return "❌ No existe ese cliente."
        # Guarda: un cliente con maquinas no se borra de una. Primero se dan de
        # baja las maquinas, asi nunca queda una caja huerfana cobrando a una
        # cuenta de MercadoPago que ya no esta asociada a nadie.
        suyas = [d for d in get_dispositivos() if d.get("cliente") == alias]
        if suyas:
            nombres = ", ".join(_esc(d["nombre"]) for d in suyas)
            return (f"❌ <b>{_esc(cli.get('nombre') or alias)}</b> todavía tiene "
                    f"{len(suyas)} máquina(s): {nombres}. Dalas de baja primero.")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM clientes WHERE alias=%s", (alias,))
        conn.commit()
        cur.close()
        conn.close()
        return (f"🗑️ Cliente <b>{_esc(cli.get('nombre') or alias)}</b> eliminado. "
                f"Su link de panel deja de funcionar. Si había conectado su "
                f"MercadoPago, la autorización sigue viva del lado de ellos: "
                f"para cortarla del todo, que la revoquen desde su cuenta.")

    if accion == "borrar_maquina":
        disp_id = request.form.get("disp_id")
        disp = get_dispositivo(disp_id)
        if not disp:
            return "❌ No existe esa máquina."
        cancelar_orden_qr(disp)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM dispositivos WHERE id=%s", (disp_id,))
        conn.commit()
        cur.close()
        conn.close()
        invalidar_cache_tipos()
        return (f"🗑️ Máquina <b>{_esc(disp['nombre'])}</b> dada de baja. "
                f"La caja sigue existiendo en MercadoPago (borrala desde ahí si no la vas a usar).")

    return ""


@app.route("/admin/<clave>", methods=["GET", "POST"])
def admin_panel(clave):
    """Panel de alta: clientes, sus máquinas y el firmware de cada equipo.
    Todo lo que antes se hacía a mano por URL o editando app.py."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    mensaje = _admin_post(clave) if request.method == "POST" else ""
    ahora = ahora_ar()
    dispositivos = get_dispositivos()
    clientes = get_clientes()

    # Cajas agrupadas por cliente (None = cajas propias de TermoPago)
    por_cliente = {}
    for d in dispositivos:
        por_cliente.setdefault(d.get("cliente") or "", []).append(d)

    opciones_tipo = "".join(
        f'<option value="{t}">{_esc(desc)}</option>' for t, desc in TIPOS.items() if t != "legacy")

    def tabla_maquinas(maquinas):
        if not maquinas:
            return '<p class="vacio">Todavía no tiene máquinas cargadas.</p>'
        filas = ""
        for d in maquinas:
            color, txt, hace = _estado_caja(d, ahora)
            tipo = d.get("tipo") or "legacy"
            unidad = (f'{int(d["segundos"])} ficha(s)' if tipo == "fichas"
                      else f'{int(d["segundos"])} s')
            esp = d.get("esp_id") or d["id"]
            canal = "" if d.get("canal") is None else f' · canal {d["canal"]}'
            filas += f"""
      <tr>
        <td><b>{_esc(d['nombre'])}</b><br><code>{_esc(d['id'])}</code></td>
        <td>{_esc(tipo)}<br><small class="mut">ESP: {_esc(esp)}{canal}</small></td>
        <td>${float(d['precio']):g}<br><small class="mut">{unidad}</small></td>
        <td style="color:{color}">{txt}<br><small class="mut">{hace}</small></td>
        <td class="acc">
          <a class="btn mini" href="/admin/{clave}/maquina/{d['id']}">Flashear</a>
          <a class="btn mini gris" href="/diag_caja/{clave}/{d['id']}">Diag</a>
          <form method="post" onsubmit="return confirm('Dar de baja {_esc(d['nombre'])}?')">
            <input type="hidden" name="accion" value="borrar_maquina">
            <input type="hidden" name="disp_id" value="{_esc(d['id'])}">
            <button class="btn mini rojo" type="submit">Baja</button>
          </form>
        </td>
      </tr>"""
        return f"""<table>
      <tr><th>Máquina</th><th>Tipo</th><th>Cobro</th><th>Estado</th><th></th></tr>
      {filas}
    </table>"""

    def form_maquina(alias, sugerido, tiene_oauth):
        opcion_cliente = ('<option value="cliente">Cuenta MP del cliente (OAuth)</option>'
                          if tiene_oauth else
                          '<option value="cliente" disabled>Cuenta MP del cliente — falta conectarla</option>')
        return f"""
    <details class="alta">
      <summary>+ Agregar máquina</summary>
      <form method="post" class="grid">
        <input type="hidden" name="accion" value="nueva_maquina">
        <input type="hidden" name="cliente" value="{_esc(alias)}">
        <label>ID de la máquina <small class="mut">(va en el topic MQTT)</small>
          <input name="disp_id" value="{_esc(sugerido)}" required></label>
        <label>Nombre visible
          <input name="nombre" placeholder="Aspiradora 1" required></label>
        <label>Tipo
          <select name="tipo">{opciones_tipo}</select></label>
        <label>ESP que la maneja <small class="mut">(repetir para 2 cajas en un mismo ESP)</small>
          <input name="esp_id" placeholder="{_esc(sugerido)}"></label>
        <label>Canal <small class="mut">(0 o 1, solo maestro-esclavo)</small>
          <input name="canal" type="number" min="0" max="3" placeholder=""></label>
        <label>Precio (ARS)
          <input name="precio" type="number" step="1" min="1" value="500"></label>
        <label>Segundos o fichas por pago
          <input name="valor" type="number" min="1" value="300"></label>
        <label>Cobra en
          <select name="cuenta">
            {opcion_cliente}
            <option value="">Cuenta propia de TermoPago</option>
          </select></label>
        <button class="btn" type="submit">Crear máquina y su QR</button>
      </form>
    </details>"""

    bloques = ""
    for cli in clientes:
        alias = cli["alias"]
        maquinas = por_cliente.get(alias, [])
        panel, pin = asegurar_panel(alias)
        tiene_oauth = bool(cli.get("access_token"))
        oauth = link_oauth(alias)
        if tiene_oauth:
            estado_mp = '<span class="ok">MercadoPago conectado</span>'
        elif oauth:
            estado_mp = f'<a class="btn mini" href="{oauth}" target="_blank">Conectar su MercadoPago</a>'
        else:
            estado_mp = '<span class="warn">Falta cargar MP_CLIENT_ID / MP_CLIENT_SECRET en Railway</span>'
        n = len(maquinas) + 1
        sugerido = f"{alias}{n:02d}"
        # La baja del cliente solo se ofrece cuando ya no le queda ninguna
        # maquina: evita borrar de un click a alguien que esta cobrando.
        if maquinas:
            baja_cli = ""
        else:
            baja_cli = f"""
      <form method="post" style="display:inline"
            onsubmit="return confirm('Eliminar el cliente {_esc(cli.get('nombre') or alias)}?')">
        <input type="hidden" name="accion" value="borrar_cliente">
        <input type="hidden" name="alias" value="{_esc(alias)}">
        <button class="btn mini rojo" type="submit">Eliminar cliente</button>
      </form>"""
        bloques += f"""
  <section class="cli">
    <h2>{_esc(cli.get('nombre') or alias)} <small class="mut">{_esc(alias)}</small></h2>
    <p class="meta">{estado_mp}
       · Panel del cliente: <a href="{panel}" target="_blank">{panel}</a>
       · PIN {_esc(pin)}
      <form method="post" style="display:inline"
            onsubmit="return confirm('Generar un PIN nuevo? El actual deja de servir.')">
        <input type="hidden" name="accion" value="nuevo_pin">
        <input type="hidden" name="alias" value="{_esc(alias)}">
        <button class="btn mini gris" type="submit">PIN nuevo</button>
      </form>
      <form method="post" style="display:inline"
            onsubmit="return confirm('Generar un link nuevo? El actual deja de abrir.')">
        <input type="hidden" name="accion" value="nuevo_link">
        <input type="hidden" name="alias" value="{_esc(alias)}">
        <button class="btn mini gris" type="submit">Link nuevo</button>
      </form>{baja_cli}</p>
    {tabla_maquinas(maquinas)}
    {form_maquina(alias, sugerido, tiene_oauth)}
  </section>"""

    propias = por_cliente.get("", [])
    if propias:
        bloques += f"""
  <section class="cli">
    <h2>TermoPago <small class="mut">máquinas propias</small></h2>
    {tabla_maquinas(propias)}
    {form_maquina("", "caja01", False)}
  </section>"""

    total_maq = len(dispositivos)
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TermoPago — Administración</title>
<style>
  :root {{ --azul:#009ee3; --borde:#e2e6ea; --texto:#1c2430; --mut:#6b7785; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color:var(--texto);
         background:#f6f8fa; margin:0; padding:24px 16px 64px; }}
  .wrap {{ max-width:900px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  h2 {{ font-size:18px; margin:0 0 6px; }}
  .mut {{ color:var(--mut); font-weight:400; }}
  .ok {{ color:#2e7d32; font-weight:600; }}
  .warn {{ color:#c62828; }}
  .meta {{ font-size:13px; color:var(--mut); margin:0 0 14px; word-break:break-all; }}
  .meta a {{ color:var(--azul); }}
  /* Un boton dentro de .meta tiene que seguir siendo blanco sobre azul:
     sin esto, '.meta a' le gana por especificidad a '.btn' y el texto
     desaparece contra el fondo. */
  .meta a.btn {{ color:#fff; }}
  section.cli, .caja {{ background:#fff; border:1px solid var(--borde); border-radius:10px;
                       padding:16px; margin-bottom:18px; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th {{ text-align:left; font-size:12px; text-transform:uppercase; color:var(--mut);
        border-bottom:1px solid var(--borde); padding:6px 8px 6px 0; }}
  td {{ padding:10px 8px 10px 0; border-bottom:1px solid var(--borde); vertical-align:top; }}
  td.acc {{ white-space:nowrap; text-align:right; }}
  td.acc form {{ display:inline; }}
  code {{ background:#f0f3f6; padding:1px 5px; border-radius:4px; font-size:12px; }}
  .btn {{ display:inline-block; background:var(--azul); color:#fff; border:none; border-radius:6px;
          padding:11px 16px; font-size:15px; text-decoration:none; cursor:pointer; }}
  .btn.mini {{ padding:5px 10px; font-size:12px; margin-left:4px; }}
  .btn.gris {{ background:#788; }}
  .btn.rojo {{ background:#c62828; }}
  details.alta {{ margin-top:14px; }}
  summary {{ cursor:pointer; color:var(--azul); font-size:14px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin-top:12px; }}
  .grid label {{ display:block; font-size:13px; }}
  .grid input, .grid select {{ width:100%; padding:9px; font-size:15px; margin-top:4px;
                               border:1px solid var(--borde); border-radius:6px; }}
  .grid button {{ grid-column:1/-1; }}
  .msg {{ background:#fff; border-left:4px solid var(--azul); padding:12px 14px; border-radius:6px;
          margin-bottom:18px; font-size:14px; }}
  .vacio {{ color:var(--mut); font-size:14px; margin:6px 0; }}
  .links a {{ color:var(--azul); font-size:13px; margin-right:14px; }}
  @media (max-width:560px) {{
    th:nth-child(3), td:nth-child(3) {{ display:none; }}
    td.acc {{ text-align:left; }}
  }}
</style></head><body><div class="wrap">
<h1>⚙️ TermoPago — Administración</h1>
<p class="meta">{len(clientes)} cliente(s) · {total_maq} máquina(s)</p>
{f'<div class="msg">{mensaje}</div>' if mensaje else ''}

<div class="caja">
  <h2>Nuevo cliente</h2>
  <form method="post" class="grid">
    <input type="hidden" name="accion" value="nuevo_cliente">
    <label>Alias <small class="mut">(corto, sin espacios: villagas)</small>
      <input name="alias" required></label>
    <label>Nombre del negocio
      <input name="nombre" placeholder="Estación Villagas"></label>
    <button class="btn" type="submit">Crear cliente</button>
  </form>
</div>

{bloques}

<p class="links">
  <a href="/config/{clave}">Precios y tiempos</a>
  <a href="/estado/{clave}">Estado de equipos</a>
  <a href="/estadisticas/{clave}">Estadísticas</a>
  <a href="/historial/{clave}">Historial</a>
  <a href="/cortes/{clave}">Cortes</a>
</p>
</div></body></html>"""


@app.route("/admin/<clave>/maquina/<disp_id>")
def admin_maquina(clave, disp_id):
    """Ficha de flasheo de un equipo: todo lo que hay que saber para dejarlo
    andando, sin abrir el código."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    disp = get_dispositivo(disp_id)
    if not disp:
        return "No existe esa máquina", 404

    tipo = disp.get("tipo") or "legacy"
    esp = disp.get("esp_id") or disp_id
    sketch = sketch_de(tipo)
    hermanas = sorted(cajas_hermanas(disp_id))
    color, txt, hace = _estado_caja(disp, ahora_ar())
    unidad = ("fichas por pago" if tipo == "fichas" else "segundos de servicio")

    if tipo == "legacy":
        aviso = ('<p class="warn">Esta caja es del sistema viejo por polling HTTPS. '
                 'Cambiale el tipo desde el panel antes de flashear un equipo MQTT.</p>')
    else:
        aviso = ""

    otras = ""
    if len(hermanas) > 1:
        lista = ", ".join(f"<code>{_esc(h)}</code>" for h in hermanas)
        otras = (f'<p class="nota">Este ESP (<code>{_esc(esp)}</code>) maneja {len(hermanas)} cajas: {lista}. '
                 f'Cargá todas en el portal, separadas por coma, en el mismo orden que los canales.</p>')

    ids_portal = ",".join(hermanas) if len(hermanas) > 1 else disp_id

    # QR fisico de la caja: lo genera MercadoPago al crearla y no cambia nunca
    # (el que rota es la ORDEN que cuelga de la caja, no la imagen). Lo pedimos
    # a MP en vivo para no guardar una URL que se puede vencer.
    qr_img = qr_pdf = None
    qr_error = ""
    try:
        _r = requests.get("https://api.mercadopago.com/pos",
                          params={"external_id": disp.get("external_pos_id")},
                          headers=mp_headers(token_de(disp)), timeout=10)
        _cajas = _r.json().get("results", []) if _r.status_code == 200 else []
        if _cajas:
            qr_img = _cajas[0].get("qr", {}).get("image")
            qr_pdf = _cajas[0].get("qr", {}).get("template_document")
        else:
            qr_error = (f"MercadoPago no devolvio ninguna caja con external_id "
                        f"'{_esc(disp.get('external_pos_id'))}' (respondio {_r.status_code}).")
    except Exception as _e:
        qr_error = f"No pude consultar MercadoPago ahora: {_esc(_e)}"

    if qr_img:
        bloque_qr = f"""
  <p class="mut">Este es el QR que va pegado en la máquina. No cambia nunca:
     lo que se renueva solo cada pocos minutos es la orden de cobro que cuelga
     de él, no la imagen.</p>
  <p><img src="{qr_img}" alt="QR de {_esc(disp['nombre'])}" class="qr"></p>
  <p>
    <a class="btn" href="{qr_img}" target="_blank">Abrir la imagen</a>
    {f'<a class="btn gris" href="{qr_pdf}" target="_blank">PDF para imprimir</a>' if qr_pdf else ''}
  </p>"""
    else:
        bloque_qr = (f'<p class="warn">{qr_error}</p>'
                     '<p class="mut">Probá de nuevo en un minuto. Si sigue igual, '
                     f'mirá <a href="/diag_caja/{clave}/{_esc(disp_id)}">el diagnóstico</a>: '
                     'suele ser que la caja quedó en otra cuenta de MercadoPago.</p>')

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flashear {_esc(disp['nombre'])}</title>
<style>
  body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background:#f6f8fa;
         color:#1c2430; margin:0; padding:24px 16px 64px; }}
  .wrap {{ max-width:720px; margin:0 auto; }}
  .caja {{ background:#fff; border:1px solid #e2e6ea; border-radius:10px; padding:18px; margin-bottom:18px; }}
  h1 {{ font-size:21px; margin:0 0 4px; }}
  h2 {{ font-size:16px; margin:0 0 10px; }}
  .mut {{ color:#6b7785; }}
  .warn {{ color:#c62828; }}
  code {{ background:#f0f3f6; padding:2px 6px; border-radius:4px; }}
  .grande {{ display:block; font-size:24px; font-weight:700; letter-spacing:.5px;
             background:#f0f3f6; border:1px dashed #b9c2cc; border-radius:8px;
             padding:14px; margin:10px 0; text-align:center; word-break:break-all; }}
  ol {{ padding-left:20px; line-height:1.75; }}
  dl {{ display:grid; grid-template-columns:auto 1fr; gap:6px 14px; font-size:14px; margin:0; }}
  dt {{ color:#6b7785; }}
  dd {{ margin:0; }}
  .btn {{ display:inline-block; background:#009ee3; color:#fff; border-radius:6px;
          padding:11px 16px; text-decoration:none; }}
  .nota {{ font-size:14px; background:#fff8e1; border-left:4px solid #f9a825;
           padding:10px 12px; border-radius:6px; }}
  a.volver {{ color:#009ee3; font-size:14px; }}
  .qr {{ width:240px; max-width:100%; height:auto; border:1px solid #e2e6ea;
         border-radius:8px; background:#fff; padding:8px; }}
</style></head><body><div class="wrap">
<p><a class="volver" href="/admin/{clave}">← Volver al panel</a></p>
<h1>{_esc(disp['nombre'])}</h1>
<p class="mut">{txt} · último contacto {hace}</p>
{aviso}

<div class="caja">
  <h2>1 · Abrí este sketch en el Arduino IDE</h2>
  <p><code>termopago/firmware/{_esc(sketch or '—')}/</code></p>
  <p class="mut">Es el mismo archivo para todas las máquinas de tipo <b>{_esc(tipo)}</b>.
     No hay que editar nada del código.</p>
  <p><a class="btn" href="/admin/{clave}/firmware/{_esc(disp_id)}.zip?secretos=1">
     Descargar el .zip listo para compilar</a></p>
  <p class="mut" style="font-size:13px">El .zip incluye <code>secretos_privado.h</code> ya completo
     con el broker y el backend. No lo compartas ni lo subas al repo.</p>
</div>

<div class="caja">
  <h2>2 · Flasheá y cargá el ID en el portal</h2>
  <ol>
    <li>Subí el sketch al ESP32.</li>
    <li>Al arrancar levanta el portal WiFi <code>TermoPago-setup</code> (clave <code>termopago</code>).</li>
    <li>Conectate con el celular y cargá la red WiFi del lugar y este ID de caja:</li>
  </ol>
  <span class="grande">{_esc(ids_portal)}</span>
  {otras}
  <p class="mut">El ID queda guardado en el ESP. Para cambiarlo, mantené apretado
     el botón BOOT durante los primeros 5 segundos del arranque.</p>
</div>

<div class="caja">
  <h2>3 · Datos de esta caja</h2>
  <dl>
    <dt>ID</dt><dd><code>{_esc(disp_id)}</code></dd>
    <dt>Tipo</dt><dd>{_esc(TIPOS.get(tipo, tipo))}</dd>
    <dt>ESP</dt><dd><code>{_esc(esp)}</code>{'' if disp.get('canal') is None else f' · canal {disp["canal"]}'}</dd>
    <dt>Cliente</dt><dd>{_esc(disp.get('cliente') or 'TermoPago (cuenta propia)')}</dd>
    <dt>Cobro</dt><dd>${float(disp['precio']):g} por {int(disp['segundos'])} {unidad}</dd>
    <dt>Topic cmd</dt><dd><code>termopago/{_esc(disp_id)}/cmd</code></dd>
    <dt>Topic status</dt><dd><code>termopago/{_esc(disp_id)}/status</code></dd>
    <dt>Caja MP</dt><dd><code>{_esc(disp.get('external_pos_id'))}</code></dd>
  </dl>
</div>

<div class="caja">
  <h2>4 · El QR para pegar en la máquina</h2>
  {bloque_qr}
</div>

<div class="caja">
  <h2>5 · Probar sin poner plata</h2>
  <p><a class="btn" href="/simular_pago/{clave}/{int(disp['segundos'])}/{_esc(disp_id)}">Simular un pago</a></p>
  <p class="mut" style="font-size:13px">Manda el comando MQTT igual que un pago real.
     Si la máquina no reacciona, mirá <a href="/diag_caja/{clave}/{_esc(disp_id)}">el diagnóstico</a>.</p>
</div>
</div></body></html>"""


@app.route("/admin/<clave>/firmware/<disp_id>.zip")
def admin_firmware(clave, disp_id):
    """Arma un .zip con la carpeta del sketch que le toca a esa máquina, lista
    para descomprimir y abrir en el Arduino IDE. Con ?secretos=1 le mete adentro
    un secretos_privado.h completo con las credenciales del broker (que en el
    repo publico estan gitignoreadas)."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    disp = get_dispositivo(disp_id)
    if not disp:
        return "No existe esa máquina", 404
    tipo = disp.get("tipo") or "legacy"
    sketch = sketch_de(tipo)
    carpeta = os.path.join(FIRMWARE_DIR, sketch) if sketch else None
    if not carpeta or not os.path.isdir(carpeta):
        return (f"No encuentro el sketch para el tipo '{tipo}' "
                f"(esperaba firmware/{sketch}/ en el repo).", 404)

    hermanas = sorted(cajas_hermanas(disp_id))
    ids_portal = ",".join(hermanas)

    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as z:
        for raiz, _dirs, archivos in os.walk(carpeta):
            for a in archivos:
                # secretos_privado.h real nunca viaja desde el repo: si lo piden,
                # se genera abajo desde las variables de entorno.
                if a == "secretos_privado.h":
                    continue
                completo = os.path.join(raiz, a)
                rel = os.path.relpath(completo, carpeta)
                z.write(completo, os.path.join(sketch, rel))

        if request.args.get("secretos") == "1":
            if not (MQTT_HOST and MQTT_USER and MQTT_PASS):
                return "Faltan MQTT_HOST / MQTT_USER / MQTT_PASS en Railway", 400
            z.writestr(os.path.join(sketch, "secretos_privado.h"), f"""/*  secretos_privado.h — generado por el panel de TermoPago
    NO subir al repo (ya esta en .gitignore). */
#ifndef SECRETOS_PRIVADO_H
#define SECRETOS_PRIVADO_H

#define MQTT_HOST     "{MQTT_HOST}"
#define MQTT_PORT     {MQTT_PORT}
#define MQTT_USER     "{MQTT_USER}"
#define MQTT_PASS     "{MQTT_PASS}"

#define BACKEND_HOST  "{BASE_URL.replace('https://', '').replace('http://', '')}"

#endif
""")

        z.writestr(f"{sketch}/LEEME_{disp_id}.txt", f"""FLASHEO — {disp['nombre']}  ({disp_id})
{'=' * 60}

1. Descomprimí esta carpeta y abrí {sketch}/{sketch}.ino en el Arduino IDE.
   NO hay que editar el codigo: el ID de caja se carga desde el portal WiFi.

2. Subilo al ESP32.

3. Al arrancar levanta el portal WiFi "TermoPago-setup" (clave: termopago).
   Conectate con el celular y cargá:
       - la red WiFi del lugar y su clave
       - ID de caja:   {ids_portal}

   {'Este ESP maneja varias cajas: cargá los IDs separados por coma, en el' if len(hermanas) > 1 else ''}
   {'mismo orden que los canales del esclavo.' if len(hermanas) > 1 else ''}

4. Listo. En el LCD tiene que aparecer "WiFi conectado / Escanee el QR".
   Para volver a cambiar el ID: mantené el botón BOOT durante los primeros
   5 segundos del arranque y vuelve a levantar el portal.

DATOS DE ESTA CAJA
   tipo .............. {tipo}
   ESP ............... {disp.get('esp_id') or disp_id}
   canal ............. {'—' if disp.get('canal') is None else disp['canal']}
   cliente ........... {disp.get('cliente') or 'TermoPago (cuenta propia)'}
   precio ............ ${float(disp['precio']):g}
   por pago .......... {int(disp['segundos'])} {'ficha(s)' if tipo == 'fichas' else 'segundos'}
   topic cmd ......... termopago/{disp_id}/cmd
   topic status ...... termopago/{disp_id}/status

Librerias necesarias (Gestor de librerias del Arduino IDE):
   WiFiManager (tzapu) · PubSubClient (Nick O'Leary) · ArduinoJson (Benoit
   Blanchon) · hd44780 (Bill Perry, solo si el equipo lleva LCD)

Generado por el panel de TermoPago el {ahora_ar().strftime('%d/%m/%Y %H:%M')}.
""")

    buf.seek(0)
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="firmware_{disp_id}.zip"'},
    )


@app.route("/crear_dispositivo/<clave>/<disp_id>/<nombre>")
@app.route("/crear_dispositivo/<clave>/<disp_id>/<nombre>/<token_env>")
def crear_dispositivo(clave, disp_id, nombre, token_env=None):
    """Ej propio:   /crear_dispositivo/CLAVE/aspiradora_001/Aspiradora
    Ej cliente:  /crear_dispositivo/CLAVE/aspiradora_001/Aspiradora/MP_TOKEN_CLIENTE1
    (token_env = nombre de la variable de Railway con el Access Token del cliente)
    Crea la caja (y la sucursal si hace falta) en la cuenta correspondiente.
    El nombre no puede tener espacios: usar guiones (Poste-de-inflado)."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    # Tipo/ESP opcionales por query string, para poder dar de alta una caja
    # completa desde la URL: ...?tipo=fichas&esp=villagas_esp1&canal=0
    resultado = alta_dispositivo(
        disp_id, nombre, token_env,
        tipo=request.args.get("tipo"),
        esp_id=request.args.get("esp"),
        canal=request.args.get("canal"),
    )
    if "error" in resultado:
        return jsonify(resultado), resultado.pop("_http", 400)
    return jsonify(resultado)


def alta_dispositivo(disp_id, nombre, token_env=None, tipo=None, esp_id=None,
                     canal=None, precio=None, valor=None):
    """Crea (o re-apunta) una caja: la da de alta en la cuenta de MercadoPago
    que corresponda, la guarda en 'dispositivos' con su tipo y su ESP, y le arma
    el QR. La usan la ruta /crear_dispositivo y el panel /admin.
    Devuelve un dict: con 'error' si algo falló, si no los datos de la caja."""
    # token_env puede ser:  MP_TOKEN_XXX (variable de Railway)  o  cliente_ALIAS (OAuth)
    cliente_alias = None
    if token_env and token_env.startswith("cliente_"):
        cliente_alias = token_env[len("cliente_"):]
        token = token_cliente(cliente_alias)
        if not token:
            return {"error": f"El cliente '{cliente_alias}' no está conectado (usar /conectar_cliente)", "_http": 400}
        token_env = None
    elif token_env and not os.environ.get(token_env):
        return {"error": f"La variable {token_env} no existe en Railway", "_http": 400}
    else:
        token = os.environ.get(token_env) if token_env else MP_TOKEN

    nombre = nombre.replace("-", " ")
    existente = get_dispositivo(disp_id)
    external_id = "".join(c for c in disp_id.upper() if c.isalnum())[:40]

    # Tipo: el que pidan, el que ya tenía, o 'sostenida' (el caso más común).
    if tipo not in TIPOS:
        tipo = (existente.get("tipo") if existente else None) or "sostenida"
    esp_id = (esp_id or "").strip() or (existente.get("esp_id") if existente else None) or disp_id
    try:
        canal = int(canal) if canal not in (None, "") else (existente.get("canal") if existente else None)
    except (ValueError, TypeError):
        canal = None

    # Buscar caja existente en esa cuenta con ese external_id
    r = requests.get("https://api.mercadopago.com/pos", params={"external_id": external_id}, headers=mp_headers(token))
    cajas = r.json().get("results", []) if r.status_code == 200 else []

    if cajas:
        pos = cajas[0]
    else:
        # obtener sucursal: de una caja previa, o crearla si la cuenta no tiene
        r = requests.get("https://api.mercadopago.com/pos", headers=mp_headers(token))
        todas = r.json().get("results", []) if r.status_code == 200 else []
        if todas:
            store_id = int(todas[0]["store_id"])
        else:
            r = requests.get("https://api.mercadopago.com/users/me", headers=mp_headers(token))
            duenio_id = r.json().get("id")
            if not duenio_id:
                return {"error": "Token inválido", "detalle": r.json(), "_http": 400}
            sucursal = {
                "name": f"Sucursal {nombre}",
                "business_hours": {
                    dia: [{"open": "00:00", "close": "23:59"}]
                    for dia in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                },
                "location": {
                    "street_name": "Concordia",
                    "street_number": "1",
                    "city_name": "Concordia",
                    "state_name": "Entre Ríos",
                    "zip_code": "3200",
                    "latitude": -31.3927,
                    "longitude": -58.0157
                },
                "external_id": f"SUC{external_id}"[:40]
            }
            r1 = requests.post(
                f"https://api.mercadopago.com/users/{duenio_id}/stores",
                json=sucursal, headers=mp_headers(token)
            )
            store = r1.json()
            if "id" not in store:
                return {"error": "No se pudo crear la sucursal", "detalle": store, "_http": 400}
            store_id = int(store["id"])
        caja = {
            "name": f"Caja {nombre}",
            "fixed_amount": True,
            "store_id": store_id,
            "external_id": external_id,
            "category": 621102
        }
        r2 = requests.post("https://api.mercadopago.com/pos", json=caja, headers=mp_headers(token))
        pos = r2.json()
        if "id" not in pos:
            return {"error": "No se pudo crear la caja", "detalle": pos, "_http": 400}

    # Valor por defecto del campo 'segundos' segun el tipo: una expendedora
    # cuenta FICHAS por pago, las demas cuentan SEGUNDOS de servicio.
    try:
        precio_ini = float(precio) if precio not in (None, "") else None
    except (ValueError, TypeError):
        precio_ini = None
    try:
        valor_ini = int(valor) if valor not in (None, "") else None
    except (ValueError, TypeError):
        valor_ini = None
    if precio_ini is None:
        precio_ini = float(existente["precio"]) if existente else 500.0
    if valor_ini is None:
        valor_ini = int(existente["segundos"]) if existente else (1 if tipo == "fichas" else 300)

    if not existente:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO dispositivos (id, nombre, external_pos_id, precio, segundos, token_env, "
            "cliente, tipo, esp_id, canal, creado) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (disp_id, nombre, external_id, precio_ini, valor_ini, token_env,
             cliente_alias, tipo, esp_id, canal, ahora_ar().isoformat())
        )
        conn.commit()
        cur.close()
        conn.close()
    else:
        # Ya existía: actualizar la cuenta/caja. Permite re-apuntar un dispositivo
        # a otra cuenta de MercadoPago re-corriendo el alta con el token correcto.
        # Limpiamos la orden vieja (era de la otra cuenta) para que rearmar_qr cree
        # una nueva en la cuenta nueva sin intentar reconciliar la anterior.
        actualizar_dispositivo(disp_id, {
            "nombre": nombre,
            "external_pos_id": external_id,
            "token_env": token_env,
            "cliente": cliente_alias,
            "tipo": tipo,
            "esp_id": esp_id,
            "canal": canal,
            "precio": precio_ini,
            "segundos": valor_ini,
            "orden_qr_id": None,
            "ultimo_rearme": None,
        })

    # El tipo cambia como se comporta la caja en todo el backend: que el cache
    # lo vea YA, antes de armar el QR.
    invalidar_cache_tipos()
    disp = get_dispositivo(disp_id)
    rearmar_qr(disp)

    return {
        "dispositivo": disp_id,
        "nombre": nombre,
        "tipo": tipo,
        "esp_id": esp_id,
        "canal": canal,
        "cliente": cliente_alias,
        "caja_id": pos["id"],
        "external_pos_id": external_id,
        "qr_imagen": pos.get("qr", {}).get("image"),
        "qr_pdf": pos.get("qr", {}).get("template_document"),
        "nota": ("Fichas por pago" if tipo == "fichas" else "Tiempo") +
                f" y precio se ajustan en /config (quedo en ${precio_ini:g} / {valor_ini})",
    }

@app.route("/diag_caja/<clave>/<disp_id>")
def diag_caja(clave, disp_id):
    """Diagnostico: para un dispositivo, muestra a que CAJA MP (numerica)
    corresponde su external_pos_id, y el estado de su orden actual. Sirve para
    detectar si el QR fisico (una caja) no coincide con la caja donde el backend
    carga la orden."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    disp = get_dispositivo(disp_id)
    if not disp:
        return jsonify({"error": "no existe"}), 404
    tok = token_de(disp)
    out = {"disp": disp_id, "external_pos_id": disp.get("external_pos_id"),
           "token_env": disp.get("token_env"), "orden_qr_id": disp.get("orden_qr_id")}
    # 1) POS que matchean ese external_id en esa cuenta
    try:
        r = requests.get("https://api.mercadopago.com/pos",
                         params={"external_id": disp.get("external_pos_id")},
                         headers=mp_headers(tok), timeout=10)
        pos = r.json().get("results", []) if r.status_code == 200 else r.text
        out["pos_por_external_id"] = [
            {"id": x.get("id"), "name": x.get("name"),
             "external_id": x.get("external_id"),
             "store_id": x.get("store_id"),
             "qr_image": (x.get("qr") or {}).get("image")} for x in pos
        ] if isinstance(pos, list) else pos
    except Exception as e:
        out["pos_por_external_id"] = f"error: {e}"
    # 2) estado de la orden actual
    if disp.get("orden_qr_id"):
        try:
            r = requests.get(f"https://api.mercadopago.com/v1/orders/{disp['orden_qr_id']}",
                             headers=mp_headers(tok), timeout=10)
            if r.status_code == 200:
                o = r.json()
                out["orden"] = {"status": o.get("status"),
                                "total_amount": o.get("total_amount"),
                                "external_reference": o.get("external_reference"),
                                "external_pos_id": ((o.get("config") or {}).get("qr") or {}).get("external_pos_id"),
                                "expiration_time": o.get("expiration_time")}
            else:
                out["orden"] = {"http": r.status_code, "resp": r.text[:300]}
        except Exception as e:
            out["orden"] = f"error: {e}"
    # 3) pagos recientes de esta caja (para detectar uno trabado "en proceso")
    try:
        r = requests.get("https://api.mercadopago.com/v1/payments/search",
                         params={"external_reference": disp_id, "sort": "date_created", "criteria": "desc", "limit": 10},
                         headers=mp_headers(tok), timeout=10)
        if r.status_code == 200:
            res = r.json().get("results", [])
            out["pagos_recientes"] = [
                {"id": x.get("id"), "status": x.get("status"),
                 "status_detail": x.get("status_detail"),
                 "amount": x.get("transaction_amount"),
                 "date": x.get("date_created")} for x in res
            ]
        else:
            out["pagos_recientes"] = {"http": r.status_code, "resp": r.text[:300]}
    except Exception as e:
        out["pagos_recientes"] = f"error: {e}"
    return jsonify(out)

@app.route("/rearmar/<clave>/<disp_id>")
def rearmar_manual(clave, disp_id):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    disp = get_dispositivo(disp_id)
    if not disp:
        return jsonify({"error": "Dispositivo no encontrado"}), 404
    rearmar_qr(disp)
    disp = get_dispositivo(disp_id)
    return jsonify({"dispositivo": disp_id, "orden_qr_id": disp.get("orden_qr_id")})

# ─── Panel de configuración ──────────────────────────────────────

@app.route("/config/<clave>", methods=["GET", "POST"])
def config_panel(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    mensaje = ""
    if request.method == "POST":
        try:
            cambios = []
            for disp in get_dispositivos():
                nuevo_precio = float(request.form[f"precio__{disp['id']}"])
                # Cajas de pulso (inflado): el tiempo del conteo se edita en
                # SEGUNDOS (es corto); las de servicio sostenido, en minutos.
                if disp["id"] in ESTACIONES_MQTT:
                    nuevos_segundos = int(request.form[f"segundos__{disp['id']}"])
                else:
                    nuevos_segundos = int(request.form[f"minutos__{disp['id']}"]) * 60
                if nuevo_precio <= 0 or nuevos_segundos <= 0:
                    raise ValueError
                precio_cambio = nuevo_precio != float(disp["precio"])
                tiempo_cambio = nuevos_segundos != int(disp["segundos"])
                actualizar_dispositivo(disp["id"], {"precio": nuevo_precio, "segundos": nuevos_segundos})
                # re-armar el QR si cambió el precio O el tiempo (los minutos
                # van en la descripción del QR, así queda todo consistente)
                if precio_cambio or tiempo_cambio:
                    disp_actualizado = get_dispositivo(disp["id"])
                    cancelar_orden_qr(disp)
                    rearmar_qr(disp_actualizado)
                    cambios.append(disp["nombre"])
            if cambios:
                mensaje = "✅ Guardado. QR re-armado: " + ", ".join(cambios)
            else:
                mensaje = "✅ Guardado."
        except (ValueError, KeyError):
            mensaje = "❌ Valores inválidos, no se guardó nada."

    filas = ""
    for disp in get_dispositivos():
        if disp["id"] in ESTACIONES_FICHAS:
            campo_tiempo = (f'<label>Fichas por pago</label>'
                            f'<input type="number" name="segundos__{disp["id"]}" min="1" value="{int(disp["segundos"])}">')
        elif disp["id"] in ESTACIONES_MQTT:
            campo_tiempo = (f'<label>Tiempo del conteo (segundos)</label>'
                            f'<input type="number" name="segundos__{disp["id"]}" min="1" value="{int(disp["segundos"])}">')
        else:
            campo_tiempo = (f'<label>Tiempo (minutos)</label>'
                            f'<input type="number" name="minutos__{disp["id"]}" min="1" value="{disp["segundos"] // 60}">')
        filas += f"""
  <fieldset>
    <legend>{disp['nombre']} <small>({disp['id']})</small></legend>
    <label>Precio (ARS)</label>
    <input type="number" name="precio__{disp['id']}" step="0.01" min="1" value="{float(disp['precio']):g}">
    {campo_tiempo}
  </fieldset>"""

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TermoPago - Configuración</title>
<style>
  body {{ font-family: sans-serif; max-width: 420px; margin: 40px auto; padding: 0 16px; }}
  fieldset {{ margin-top: 16px; border: 1px solid #ccc; border-radius: 8px; padding: 12px; }}
  legend {{ font-weight: bold; padding: 0 6px; }}
  label {{ display: block; margin-top: 10px; }}
  input {{ width: 100%; padding: 10px; font-size: 18px; margin-top: 4px; box-sizing: border-box; }}
  button {{ margin-top: 20px; width: 100%; padding: 14px; font-size: 18px;
           background: #009ee3; color: white; border: none; border-radius: 6px; }}
  .msg {{ margin-top: 16px; font-size: 16px; }}
  .volver-admin {{ margin:0 0 14px; }}
  .volver-admin a {{ color:#009ee3; font-size:14px; text-decoration:none; }}
</style></head><body>
<p class="volver-admin"><a href="/admin/{clave}">← Volver al panel</a></p>
<h2>⚙️ TermoPago</h2>
<form method="post">
{filas}
  <button type="submit">Guardar</button>
</form>
<p class="msg">{mensaje}</p>
</body></html>"""

# ─── Diagnóstico de credenciales y QR ────────────────────────────

@app.route("/boot/<dispositivo_id>/<motivo>")
def boot(dispositivo_id, motivo):
    """El ESP reporta acá cada vez que arranca, con la causa del reinicio.
    Abierto (como /orden): el ESP no maneja la clave."""
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("INSERT INTO reinicios (dispositivo_id, motivo, fecha) VALUES (%s,%s,%s)",
                    (dispositivo_id, (motivo or "?")[:40], ahora_ar().isoformat()))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        return "err", 500
    return "ok"


@app.route("/reinicios_reset/<clave>")
@app.route("/reinicios_reset/<clave>/<dispositivo_id>")
def reinicios_reset(clave, dispositivo_id=None):
    """Borra el historial de reinicios para arrancar de cero (util despues de
    flasheos/pruebas). Con <dispositivo_id> borra solo esa caja; sin el, todas."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    conn = get_db(); cur = conn.cursor()
    if dispositivo_id:
        cur.execute("SELECT COUNT(*) AS n FROM reinicios WHERE dispositivo_id=%s", (dispositivo_id,))
        n = cur.fetchone()["n"]
        cur.execute("DELETE FROM reinicios WHERE dispositivo_id=%s", (dispositivo_id,))
    else:
        cur.execute("SELECT COUNT(*) AS n FROM reinicios")
        n = cur.fetchone()["n"]
        cur.execute("DELETE FROM reinicios")
    conn.commit(); cur.close(); conn.close()
    destino = dispositivo_id if dispositivo_id else "TODAS las cajas"
    return f"Historial de reinicios borrado ({n} registros) de {destino}. La pagina arranca de cero."

@app.route("/reinicios/<clave>")
def reinicios(clave):
    """Historial de reinicios del ESP con la causa y cuánto estuvo activo antes
    de cada reinicio. Sirve para saber POR QUÉ falla (watchdog, pico eléctrico,
    corte de luz, WiFi) sin adivinar."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    conn = get_db(); cur = conn.cursor()
    # Detalle: los MAS RECIENTES. Antes traia los mas VIEJOS (ASC LIMIT 1000) y con
    # mas de 1000 registros los reinicios nuevos quedaban afuera y no se veian.
    # DESC + reversed = recientes en orden cronologico (viejo->nuevo) para el gap.
    cur.execute("SELECT dispositivo_id, motivo, fecha FROM reinicios ORDER BY fecha DESC LIMIT 400")
    rows = list(reversed(cur.fetchall()))
    # Resumen por causa: cuenta TODA la historia (aparte del limite del detalle).
    cur.execute("SELECT motivo, COUNT(*) AS n FROM reinicios GROUP BY motivo")
    conteo = cur.fetchall()
    cur.close(); conn.close()
    nombres = {d["id"]: d["nombre"] for d in get_dispositivos()}

    def dur(s):
        s = int(s)
        if s < 60: return f"{s} seg"
        if s < 3600: return f"{s//60} min"
        if s < 86400: return f"{s//3600} h {(s%3600)//60} min"
        return f"{s//86400} d {(s%86400)//3600} h"

    etiqueta = {
        "corte-luz":     "🔌 Corte de luz / encendido",
        "software":      "🔄 Reinicio del programa (WiFi/servidor)",
        "panic-crash":   "⚠️ Crash de software",
        "wdt-task":      "⚠️ Watchdog (se colgó)",
        "wdt-interrupt": "⚠️ Watchdog interrupt",
        "wdt-otro":      "⚠️ Watchdog",
        "brownout-elec": "⚡ Bajón de tensión / pico eléctrico",
        "reset-externo": "Reset externo",
        "desconocido":   "Desconocido",
    }
    malos = ("panic-crash", "wdt-task", "wdt-interrupt", "wdt-otro", "brownout-elec")

    # tiempo activo = gap con el boot anterior del mismo equipo
    prev = {}
    eventos = []
    resumen = { (r["motivo"] or "desconocido"): r["n"] for r in conteo }
    for r in rows:
        did = r["dispositivo_id"]; mot = r["motivo"] or "desconocido"; f = r["fecha"] or ""
        activo = None
        if did in prev:
            try:
                activo = int((datetime.fromisoformat(f) - datetime.fromisoformat(prev[did])).total_seconds())
            except Exception:
                activo = None
        prev[did] = f
        eventos.append((f, did, mot, activo))

    filas_res = ""
    for mot, cant in sorted(resumen.items(), key=lambda x: -x[1]):
        col = "#c62828" if mot in malos else "#555"
        filas_res += f"<tr><td style='color:{col}'>{etiqueta.get(mot, mot)}</td><td><b>{cant}</b></td></tr>"
    if not filas_res:
        filas_res = '<tr><td colspan="2">Sin reinicios registrados 🎉</td></tr>'

    filas_det = ""
    for f, did, mot, activo in reversed(eventos[-150:]):
        dia = f[:10]; hora = f[11:16]
        act = dur(activo) if activo is not None else "—"
        col = "#c62828" if mot in malos else "#555"
        filas_det += (f"<tr><td>{dia[8:10]}/{dia[5:7]}</td><td><b>{hora}</b></td>"
                      f"<td>{nombres.get(did, did)} <span style='color:#888;font-size:12px'>{did}</span></td>"
                      f"<td style='color:{col}'>{etiqueta.get(mot, mot)}</td>"
                      f"<td>{act}</td></tr>")
    if not filas_det:
        filas_det = '<tr><td colspan="5">—</td></tr>'

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TermoPago - Reinicios</title>
<style>
  body {{ font-family: sans-serif; max-width: 640px; margin: 24px auto; padding: 0 14px; color:#222; }}
  h2 {{ margin-bottom:2px; }} h3 {{ margin:24px 0 8px; color:#1b4f72; }}
  .sub {{ color:#888; font-size:13px; margin-bottom:14px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #e0e0e0; padding: 8px 10px; font-size:14px; text-align:left; }}
  th {{ background:#009ee3; color:white; }}
  .volver-admin {{ margin:0 0 14px; }}
  .volver-admin a {{ color:#009ee3; font-size:14px; text-decoration:none; }}
</style></head><body>
<p class="volver-admin"><a href="/admin/{clave}">← Volver al panel</a></p>
<h2>🔁 Reinicios del equipo</h2>
<div class="sub">Cada vez que el ESP arranca reporta por qué se reinició y cuánto estuvo
activo antes. Los que están en <b style="color:#c62828">rojo</b> son fallas (colgado, pico
eléctrico, crash); los grises son normales (corte de luz, reinicio por WiFi). Hora de Argentina.</div>
<h3>Resumen por causa</h3>
<table><tr><th>Causa</th><th>Veces</th></tr>{filas_res}</table>
<h3>Detalle (últimos 150)</h3>
<table><tr><th>Día</th><th>Hora</th><th>Equipo</th><th>Causa del reinicio</th><th>Estuvo activo</th></tr>{filas_det}</table>
<p class="sub" style="margin-top:18px">Nota: "Estuvo activo" es cuánto funcionó desde el reinicio
anterior. Si ves muchos reinicios con poco tiempo activo, algo está fallando seguido.</p>
</body></html>"""


@app.route("/cortes/<clave>")
def cortes(clave):
    """Cortes de conexión (huecos > 30s) por día y por máquina.
    Sirve para ver si la señal es estable o tiene bajones durante el día."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT dispositivo_id, fin, duracion_seg FROM cortes ORDER BY fin DESC LIMIT 500")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    nombres = {d["id"]: d["nombre"] for d in get_dispositivos()}

    def dur(s):
        if s < 60: return f"{s} seg"
        if s < 3600: return f"{s//60} min {s%60} seg"
        if s < 86400: return f"{s//3600} h {(s%3600)//60} min"
        return f"{s//86400} días"

    # resumen por día+máquina y lista detallada
    resumen = {}   # (dia, disp) -> {cantidad, total}
    detalle = []
    for r in rows:
        f = r["fin"] or ""
        dia = f[:10]; hora = f[11:16]
        did = r["dispositivo_id"]; s = int(r["duracion_seg"] or 0)
        k = (dia, did)
        resumen.setdefault(k, {"cant": 0, "total": 0})
        resumen[k]["cant"] += 1; resumen[k]["total"] += s
        detalle.append((dia, hora, did, s))

    filas_res = ""
    for (dia, did) in sorted(resumen.keys(), reverse=True):
        d = resumen[(dia, did)]
        color = "#2e7d32" if d["cant"] == 0 else ("#f9a825" if d["cant"] <= 3 else "#c62828")
        filas_res += (f"<tr><td>{dia[8:10]}/{dia[5:7]}</td>"
                      f"<td>{nombres.get(did,did)} <span style='color:#888;font-size:12px'>{did}</span></td>"
                      f"<td style='color:{color};font-weight:700'>{d['cant']}</td>"
                      f"<td>{dur(d['total'])}</td></tr>")
    if not filas_res:
        filas_res = '<tr><td colspan="4">Sin cortes registrados 🎉</td></tr>'

    filas_det = ""
    for dia, hora, did, s in detalle[:100]:
        col = "#c62828" if s > 300 else "#f9a825"
        filas_det += (f"<tr><td>{dia[8:10]}/{dia[5:7]}</td><td><b>{hora}</b></td>"
                      f"<td>{nombres.get(did,did)} <span style='color:#888;font-size:12px'>{did}</span></td>"
                      f"<td style='color:{col};font-weight:600'>{dur(s)}</td></tr>")
    if not filas_det:
        filas_det = '<tr><td colspan="4">—</td></tr>'

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TermoPago - Cortes de conexión</title>
<style>
  body {{ font-family: sans-serif; max-width: 620px; margin: 24px auto; padding: 0 14px; color:#222; }}
  h2 {{ margin-bottom:2px; }} h3 {{ margin:24px 0 8px; color:#1b4f72; }}
  .sub {{ color:#888; font-size:13px; margin-bottom:14px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #e0e0e0; padding: 8px 10px; font-size:14px; text-align:left; }}
  th {{ background:#009ee3; color:white; }}
  .volver-admin {{ margin:0 0 14px; }}
  .volver-admin a {{ color:#009ee3; font-size:14px; text-decoration:none; }}
</style></head><body>
<p class="volver-admin"><a href="/admin/{clave}">← Volver al panel</a></p>
<h2>📉 Cortes de conexión</h2>
<div class="sub">Cada vez que un equipo se queda sin llegar al servidor por más de 30 seg,
queda registrado acá al reconectar. Hora de Argentina.</div>
<h3>Resumen por día</h3>
<table><tr><th>Día</th><th>Máquina</th><th>Cortes</th><th>Tiempo caído total</th></tr>{filas_res}</table>
<h3>Detalle (últimos 100)</h3>
<table><tr><th>Día</th><th>Reconectó</th><th>Máquina</th><th>Duración del corte</th></tr>{filas_det}</table>
<p class="sub" style="margin-top:18px">Nota: un corte muy largo (horas) probablemente sea que el equipo
estuvo apagado o sin luz, no un problema de señal.</p>
</body></html>"""

@app.route("/estado/<clave>")
def estado(clave):
    """Página simple: qué equipos están conectados y cuándo fue su último
    contacto. Verde = conectado, rojo = caído."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    ahora = ahora_ar()
    filas = ""
    for d in get_dispositivos():
        up = d.get("ultimo_poll")
        try:
            seg = (ahora - datetime.fromisoformat(up)).total_seconds() if up else None
        except (ValueError, TypeError):
            seg = None

        if seg is None:
            color, txt, hace = "#9e9e9e", "Nunca conectó", "—"
        elif seg < 90:
            color, txt = "#2e7d32", "🟢 Conectado"
        elif seg < 600:
            color, txt = "#f9a825", "🟡 Intermitente"
        else:
            color, txt = "#c62828", "🔴 Caído"

        if seg is not None:
            if seg < 60:      hace = f"hace {int(seg)} seg"
            elif seg < 3600:  hace = f"hace {int(seg//60)} min"
            elif seg < 86400: hace = f"hace {int(seg//3600)} h"
            else:             hace = f"hace {int(seg//86400)} días"

        hora = (up or "")[11:16]
        filas += (f'<tr>'
                  f'<td><b>{d["nombre"]}</b><br><span style="color:#888;font-size:12px">{d["id"]}</span></td>'
                  f'<td style="color:{color};font-weight:600">{txt}</td>'
                  f'<td>{hace}<br><span style="color:#aaa;font-size:12px">{hora}</span></td></tr>')

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>TermoPago - Estado de equipos</title>
<style>
  body {{ font-family: sans-serif; max-width: 560px; margin: 24px auto; padding: 0 14px; color:#222; }}
  h2 {{ margin-bottom: 2px; }}
  .sub {{ color:#888; font-size:13px; margin-bottom:16px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #e0e0e0; padding: 10px; font-size: 15px; text-align:left; }}
  th {{ background: #009ee3; color:white; }}
  .volver-admin {{ margin:0 0 14px; }}
  .volver-admin a {{ color:#009ee3; font-size:14px; text-decoration:none; }}
</style></head><body>
<p class="volver-admin"><a href="/admin/{clave}">← Volver al panel</a></p>
<h2>📡 Estado de equipos</h2>
<div class="sub">Se actualiza solo cada 30 seg · hora de Argentina<br>
🟢 conectado (&lt;90s) · 🟡 intermitente · 🔴 caído (&gt;10min)</div>
<table>
<tr><th>Máquina</th><th>Estado</th><th>Último contacto</th></tr>
{filas}
</table>
</body></html>"""

@app.route("/diagnostico/<clave>")
def diagnostico(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    resultado = {}

    r = requests.get("https://api.mercadopago.com/users/me", headers=mp_headers())
    me = r.json()
    resultado["token"] = {
        "status": r.status_code,
        "user_id_del_token": me.get("id"),
        "user_id_configurado": USER_ID,
        "coinciden": str(me.get("id")) == USER_ID,
        "nickname": me.get("nickname"),
        "site": me.get("site_id"),
        "es_cuenta_test": bool(me.get("tags") and "test_user" in me.get("tags", []))
    }

    r = requests.get(f"https://api.mercadopago.com/users/{USER_ID}/stores/search", headers=mp_headers())
    resultado["sucursales"] = {"status": r.status_code, "respuesta": r.json() if r.text else None}

    r = requests.get("https://api.mercadopago.com/pos", headers=mp_headers())
    resultado["cajas"] = {"status": r.status_code, "respuesta": r.json() if r.text else None}

    resultado["dispositivos"] = [dict(d) for d in get_dispositivos()]

    return jsonify(resultado)

# ─── Ver sucursales y cajas ──────────────────────────────────────

@app.route("/ver_sucursales/<clave>")
def ver_sucursales(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    r = requests.get(f"https://api.mercadopago.com/users/{USER_ID}/stores/search", headers=mp_headers())
    return r.text, r.status_code, {"Content-Type": "application/json"}

@app.route("/ver_cajas/<clave>")
def ver_cajas(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    r = requests.get("https://api.mercadopago.com/pos", headers=mp_headers())
    return r.text, r.status_code, {"Content-Type": "application/json"}

# ─── Checkout Pro (link de pago del termo) ───────────────────────

@app.route("/crear_pago")
def crear_pago():
    disp = get_dispositivo("termo_001")
    precio = float(disp["precio"]) if disp else PRECIO
    sdk = mercadopago.SDK(MP_TOKEN)
    preference = {
        "items": [{"title": "Agua caliente 30 minutos", "quantity": 1, "unit_price": precio, "currency_id": "ARS"}],
        "metadata": {"dispositivo_id": "termo_001"},
        "notification_url": f"{BASE_URL}/webhook",
        "payment_methods": {
            "excluded_payment_types": [
                {"id": "credit_card"},
                {"id": "ticket"},
                {"id": "atm"},
                {"id": "prepaid_card"}
            ],
            "installments": 1
        }
    }
    result = sdk.preference().create(preference)
    link = result["response"]["init_point"]
    return redirect(link)

# ─── Estadísticas de ventas ──────────────────────────────────────

@app.route("/estadisticas/<clave>")
def estadisticas(clave):
    """Resumen de ventas: totales, por día, por mes y por máquina.
    Solo cuenta pagos reales (QR y link), no las simulaciones."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    conn = get_db()
    cur = conn.cursor()
    cur.execute(r"""
        SELECT dispositivo_id, fecha, COALESCE(monto,0) AS monto, estado
        FROM ordenes
        WHERE id LIKE 'ord\_%' OR id LIKE 'pay\_%' OR id LIKE 'mo\_%'
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    # nombres lindos de cada máquina
    nombres = {d["id"]: d["nombre"] for d in get_dispositivos()}

    hoy = ahora_ar().strftime("%Y-%m-%d")
    mes_actual = ahora_ar().strftime("%Y-%m")

    def nuevo(): return {"ventas": 0, "monto": 0.0, "reemb": 0}
    por_dia, por_mes, por_maq = {}, {}, {}
    tot_hoy, tot_mes, tot_all = nuevo(), nuevo(), nuevo()

    for r in rows:
        dia = (r["fecha"] or "")[:10]
        mes = (r["fecha"] or "")[:7]
        m = float(r["monto"] or 0)
        reemb = 1 if r["estado"] == "reembolsada" else 0
        for destino in (por_dia.setdefault(dia, nuevo()),
                        por_mes.setdefault(mes, nuevo()),
                        por_maq.setdefault(r["dispositivo_id"], nuevo()),
                        tot_all):
            destino["ventas"] += 1; destino["monto"] += m; destino["reemb"] += reemb
        if dia == hoy:
            tot_hoy["ventas"] += 1; tot_hoy["monto"] += m; tot_hoy["reemb"] += reemb
        if mes == mes_actual:
            tot_mes["ventas"] += 1; tot_mes["monto"] += m; tot_mes["reemb"] += reemb

    def tarjeta(titulo, d):
        return (f'<div class="card"><div class="ct">{titulo}</div>'
                f'<div class="cv">${d["monto"]:,.0f}</div>'
                f'<div class="cs">{d["ventas"]} ventas'
                + (f' · {d["reemb"]} reemb.' if d["reemb"] else '') + '</div></div>')

    def tabla(titulo, datos, es_maquina=False, limite=None):
        claves = sorted(datos.keys(), reverse=True)
        if limite: claves = claves[:limite]
        filas = ""
        for k in claves:
            d = datos[k]
            etiqueta = f"{nombres.get(k, k)} ({k})" if es_maquina else k
            filas += (f"<tr><td>{etiqueta}</td><td>{d['ventas']}</td>"
                      f"<td>${d['monto']:,.0f}</td><td>{d['reemb'] or ''}</td></tr>")
        if not filas:
            filas = '<tr><td colspan="4">Sin datos</td></tr>'
        col1 = "Máquina" if es_maquina else titulo.split()[-1]
        return (f'<h3>{titulo}</h3><table>'
                f'<tr><th>{col1}</th><th>Ventas</th><th>Facturado</th><th>Reemb.</th></tr>'
                f'{filas}</table>')

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TermoPago - Ventas</title>
<style>
  body {{ font-family: sans-serif; max-width: 640px; margin: 24px auto; padding: 0 14px; color:#222; }}
  h2 {{ margin-bottom: 4px; }}
  h3 {{ margin: 26px 0 8px; color:#1b4f72; }}
  .cards {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }}
  .card {{ flex:1; min-width:140px; background:#009ee3; color:white; border-radius:10px; padding:12px 14px; }}
  .ct {{ font-size:13px; opacity:.9; }}
  .cv {{ font-size:26px; font-weight:bold; margin:2px 0; }}
  .cs {{ font-size:12px; opacity:.9; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 7px 10px; text-align: left; font-size: 14px; }}
  th {{ background: #eaf4fb; color:#1b4f72; }}
  tr:nth-child(even) td {{ background: #f7f9fb; }}
  .nota {{ color:#888; font-size:12px; margin-top:20px; }}
  .volver-admin {{ margin:0 0 14px; }}
  .volver-admin a {{ color:#009ee3; font-size:14px; text-decoration:none; }}
</style></head><body>
<p class="volver-admin"><a href="/admin/{clave}">← Volver al panel</a></p>
<h2>📊 TermoPago — Ventas</h2>
<div class="cards">
  {tarjeta("Hoy", tot_hoy)}
  {tarjeta("Este mes", tot_mes)}
  {tarjeta("Histórico", tot_all)}
</div>
{tabla("Por día (últimos 30)", por_dia, limite=30)}
{tabla("Por mes", por_mes)}
{tabla("Por máquina", por_maq, es_maquina=True)}
<p class="nota">Solo pagos reales (QR y link), no simulaciones. Fechas y horas
en horario de Argentina. Los montos se registran desde julio 2026; ventas
anteriores cuentan en cantidad pero pueden figurar en $0.</p>
</body></html>"""

# ─── Historial ───────────────────────────────────────────────────

@app.route("/historial/<clave>")
@app.route("/historial/<clave>/<int:cuantos>")
def historial(clave, cuantos=40):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordenes ORDER BY fecha DESC LIMIT %s", (min(cuantos, 300),))
    ordenes = cur.fetchall()
    cur.close()
    conn.close()

    # Respuesta JSON si se pide (?json=1)
    if request.args.get("json"):
        return jsonify([dict(o) for o in ordenes])

    nombres = {d["id"]: d["nombre"] for d in get_dispositivos()}

    # estado -> (texto legible, color de fondo)
    ESTADOS = {
        "pendiente":   ("Pagado (en espera)", "#fff8e1"),
        "ejecutando":  ("En uso",             "#e3f2fd"),
        "completada":  ("Completado",         "#e8f5e9"),
        "reembolsada": ("REEMBOLSADO",        "#ffe0e0"),
        "vencida":     ("Sin atender",        "#f0f0f0"),
    }

    filas = ""
    for o in ordenes:
        f = o["fecha"] or ""
        dia = f[8:10] + "/" + f[5:7] if len(f) >= 10 else f      # DD/MM
        hora = f[11:16] if len(f) >= 16 else ""                   # HH:MM
        did = o["dispositivo_id"]
        maq = (f'{nombres.get(did, did)} '
               f'<span style="color:#888;font-size:12px">{did}</span>')
        # tipo: pago real (QR/link) o prueba simulada
        oid = o["id"] or ""
        real = oid.startswith(("ord_", "pay_", "mo_"))
        if o.get("monto"):
            monto = f"${float(o['monto']):,.0f}"
        elif real:
            monto = "—"
        else:
            monto = "<span style='color:#aaa'>prueba</span>"
        texto, color = ESTADOS.get(o["estado"], (o["estado"], "#fff"))
        minutos = f"{(o['segundos'] or 0)//60}m" if (o['segundos'] or 0) >= 60 else f"{o['segundos']}s"
        filas += (f'<tr style="background:{color}">'
                  f'<td>{dia}</td><td><b>{hora}</b></td><td>{maq}</td>'
                  f'<td style="text-align:right">{monto}</td>'
                  f'<td>{texto}</td><td style="text-align:center;color:#888">{minutos}</td></tr>')
    if not filas:
        filas = '<tr><td colspan="6">Sin movimientos todavía</td></tr>'

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TermoPago - Historial</title>
<style>
  body {{ font-family: sans-serif; max-width: 680px; margin: 24px auto; padding: 0 12px; color:#222; }}
  h2 {{ margin-bottom: 2px; }}
  .sub {{ color:#888; font-size:13px; margin-bottom:14px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #e0e0e0; padding: 7px 9px; font-size: 14px; }}
  th {{ background: #009ee3; color:white; text-align:left; position:sticky; top:0; }}
  .volver-admin {{ margin:0 0 14px; }}
  .volver-admin a {{ color:#009ee3; font-size:14px; text-decoration:none; }}
</style></head><body>
<p class="volver-admin"><a href="/admin/{clave}">← Volver al panel</a></p>
<h2>🧾 TermoPago — Historial</h2>
<div class="sub">Últimos {len(ordenes)} movimientos · hora de Argentina ·
"prueba" = simulación (no es dinero real)</div>
<table>
<tr><th>Día</th><th>Hora</th><th>Máquina</th><th>Monto</th><th>Estado</th><th>Tiempo</th></tr>
{filas}
</table>
</body></html>"""

# ─── Reembolso automático de pagos no atendidos ──────────────────

def equipo_offline(disp):
    """True si el ESP32 del dispositivo no consulta hace más de 2 minutos."""
    if not disp or not disp.get("ultimo_poll"):
        return True
    try:
        return (ahora_ar() - datetime.fromisoformat(disp["ultimo_poll"])).total_seconds() > 120
    except (ValueError, TypeError):
        return True

def reembolsar_orden_mp(orden):
    """Devuelve el pago de una orden 'ord_XXX' al cliente final."""
    mp_id = orden["id"][4:]
    disp = get_dispositivo(orden["dispositivo_id"])
    headers = mp_headers(token_de(disp))
    headers["X-Idempotency-Key"] = str(uuid.uuid4())
    try:
        r = requests.post(f"https://api.mercadopago.com/v1/orders/{mp_id}/refund", headers=headers, timeout=10)
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"Error reembolsando {orden['id']}: {e}")
        return False

def marcar_orden(orden_id, nuevo_estado, solo_si=None):
    conn = get_db()
    cur = conn.cursor()
    if solo_si:
        cur.execute("UPDATE ordenes SET estado=%s WHERE id=%s AND estado=%s", (nuevo_estado, orden_id, solo_si))
    else:
        cur.execute("UPDATE ordenes SET estado=%s WHERE id=%s", (nuevo_estado, orden_id))
    tomada = cur.rowcount == 1
    conn.commit()
    cur.close()
    conn.close()
    return tomada

def marcar_ejecutando(orden_id):
    """Marca una orden 'ejecutando' con inicio=ahora (arranque de un servicio
    sostenido por MQTT). Mismo efecto que hace /orden con las cajas de polling,
    para que la recuperacion tras corte de luz calcule el tiempo restante."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE ordenes SET estado='ejecutando', inicio=%s WHERE id=%s AND estado='pendiente'",
                (ahora_ar().isoformat(), orden_id))
    conn.commit()
    cur.close()
    conn.close()

def vigilar_ordenes():
    """Cada minuto: si una orden pagada lleva más de REEMBOLSO_MINUTOS
    esperando y el equipo está sin conexión, se devuelve el dinero.
    Si el equipo está online (solo ocupado, con fila), no se toca."""
    while True:
        try:
            limite = (ahora_ar() - timedelta(minutes=REEMBOLSO_MINUTOS)).isoformat()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM ordenes WHERE estado='pendiente' AND fecha < %s", (limite,))
            viejas = cur.fetchall()
            cur.close()
            conn.close()
            for o in viejas:
                disp = get_dispositivo(o["dispositivo_id"])
                if not equipo_offline(disp):
                    continue  # el equipo está vivo: es fila de espera, no un corte
                # transición atómica: solo un proceso la toma
                if not marcar_orden(o["id"], "vencida", solo_si="pendiente"):
                    continue
                if o["id"].startswith("ord_"):
                    if reembolsar_orden_mp(o):
                        marcar_orden(o["id"], "reembolsada")
                        print(f"Orden {o['id']} reembolsada (equipo sin conexión)")
                    else:
                        print(f"Orden {o['id']} vencida — REEMBOLSO MANUAL requerido")
                else:
                    print(f"Orden {o['id']} vencida (simulada/legacy, sin reembolso)")
        except Exception as e:
            print(f"Error en vigilancia de órdenes: {e}")
        time.sleep(60)

# ─── Liveness por MQTT: leer el heartbeat que el ESP ya publica ──────
# El ESP de las cajas "pulso" (inflado) publica cada 60s en
# termopago/<caja>/status un heartbeat con uptime/rssi/heap/estado, y el
# broker publica ahí el LWT "offline" si el equipo se cae de golpe.
# Este suscriptor actualiza ultimo_poll (para /estado) y registra cortes
# (para /cortes), SIN pedirle al ESP ningún HTTP extra.

_lock_conn = None
def _tomar_lock_suscriptor():
    """Un solo proceso corre el suscriptor. Advisory lock de Postgres: si
    otro worker ya lo tiene, este no arranca (evita suscriptores/cortes
    duplicados si algún día se escala a varios workers de gunicorn)."""
    global _lock_conn
    try:
        _lock_conn = get_db()
        _lock_conn.autocommit = True
        cur = _lock_conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(918273645) AS ok")
        ok = cur.fetchone()["ok"]
        cur.close()
        if not ok:
            _lock_conn.close(); _lock_conn = None
        return ok
    except Exception as e:
        print(f"MQTT liveness lock: {e}")
        return False

def _registrar_corte(caja, ahora, gap):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("INSERT INTO cortes (dispositivo_id, fin, duracion_seg) VALUES (%s,%s,%s)",
                    (caja, ahora.isoformat(), int(gap)))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"Error registrando corte de {caja}: {e}")

def mqtt_liveness_loop():
    if not (MQTT_HOST and MQTT_USER and MQTT_PASS):
        print("MQTT liveness: sin credenciales, no arranco el suscriptor")
        return
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("MQTT liveness: falta paho-mqtt")
        return

    # esperar el lock (si otro worker lo tiene, reintenta por si aquel muere)
    while not _tomar_lock_suscriptor():
        time.sleep(30)

    def on_connect(client, userdata, flags, rc, *a):
        client.subscribe("termopago/+/status", qos=1)
        print("MQTT liveness: suscripto a termopago/+/status")

    def on_message(client, userdata, msg):
        try:
            caja = msg.topic.split("/")[1]
            data = _json.loads((msg.payload.decode() or "{}"))
        except Exception:
            return
        # Ignorar TODO mensaje retenido: un latido en vivo llega con retain=0; los
        # retenidos son re-entregas viejas del broker al re-suscribirse (dan falsos
        # "online"/"offline" y timestamps de contacto erroneos). Solo los latidos
        # en vivo cuentan para liveness/re-arme.
        if getattr(msg, "retain", False):
            return
        disp = get_dispositivo(caja)
        if not disp:
            return   # caja desconocida (todavía no dada de alta)
        # el LWT "offline" no actualiza contacto: el corte se calcula cuando
        # vuelve el primer heartbeat "online" (gap contra el último contacto).
        if data.get("estado") == "offline":
            # Equipo caído: cancelar el QR de TODAS las cajas de ese ESP para que
            # NADIE pueda pagar una máquina que no va a responder (aunque el LWT
            # llegue por una sola caja, el ESP es el mismo -> caen todas). Cada
            # una se re-arma sola con su próximo heartbeat cuando el equipo vuelva.
            for hid in cajas_hermanas(caja):
                hdisp = disp if hid == caja else get_dispositivo(hid)
                if hdisp and hdisp.get("orden_qr_id"):
                    cancelar_orden_qr(hdisp)
                    actualizar_dispositivo(hid, {"orden_qr_id": None, "ultimo_rearme": None})
                    print(f"QR de {hid} cancelado: equipo offline (LWT de {caja})")
            return
        ahora = ahora_ar()
        up = disp.get("ultimo_poll")
        if up:
            try:
                gap = (ahora - datetime.fromisoformat(up)).total_seconds()
                if gap > 90:   # se perdió más de un heartbeat -> hubo un corte
                    _registrar_corte(caja, ahora, gap)
            except (ValueError, TypeError):
                pass
        actualizar_dispositivo(caja, {"ultimo_poll": ahora.isoformat()})
        # Re-armar el QR si venció: las cajas MQTT no pollean /orden, así que
        # nadie más les renueva la orden del QR (que expira a los 15 min).
        # El heartbeat (cada 60s) lo mantiene siempre vigente.
        try:
            rearme = disp.get("ultimo_rearme")
            vencido = (not rearme) or (ahora - datetime.fromisoformat(rearme)).total_seconds() > REARME_SEGUNDOS
        except (ValueError, TypeError):
            vencido = True
        # Re-armar si vencio por tiempo O si el QR quedo cancelado (orden_qr_id
        # vacio, tipico tras un "offline"): asi vuelve al toque con el primer
        # heartbeat online, sin esperar los 10 min del vencimiento.
        if vencido or not disp.get("orden_qr_id"):
            rearmar_qr(disp)

    client = mqtt.Client(client_id="termopago-backend-sub", clean_session=True)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            print(f"MQTT liveness: reconectando ({e})")
            time.sleep(10)

# ─── Alertas por Telegram: avisar cuando un equipo se cae / vuelve ───
_alerta_estado = {}   # caja -> True si ya avisamos que esta caida

def enviar_telegram(texto):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"},
            timeout=10)
        if r.status_code != 200:
            print(f"Telegram {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

def vigilar_equipos():
    """Cada minuto revisa el ultimo_poll de las cajas MQTT. Avisa por Telegram
    cuando un equipo lleva mas de ALERTA_OFFLINE_S sin latido (se cayo) y cuando
    vuelve. Estado en memoria para no repetir el aviso. Un solo proceso corre
    esto (advisory lock 918273646)."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("Alertas: sin TELEGRAM_TOKEN/CHAT_ID -> no arranco el vigilante de equipos")
        return
    try:
        lock = get_db(); lock.autocommit = True
        c = lock.cursor(); c.execute("SELECT pg_try_advisory_lock(918273646) AS ok")
        got = c.fetchone()["ok"]; c.close()
        if not got:
            lock.close(); return
    except Exception as e:
        print(f"Alertas lock: {e}"); return
    time.sleep(90)   # margen al arranque para no avisar durante un deploy/boot
    while True:
        try:
            ahora = ahora_ar()
            for caja in ESTACIONES_MQTT:
                disp = get_dispositivo(caja)
                if not disp:
                    continue
                up = disp.get("ultimo_poll")
                if not up:
                    continue   # nunca conecto: no alertamos
                try:
                    gap = (ahora - datetime.fromisoformat(up)).total_seconds()
                except (ValueError, TypeError):
                    continue
                caido = gap > ALERTA_OFFLINE_S
                ya = _alerta_estado.get(caja, False)
                _n = disp.get("nombre")
                # incluir el id de la caja para distinguir aspiradora01/02, soplado01/02, etc.
                nombre = f"{_n} ({caja})" if _n and _n != caja else caja
                if caido and not ya:
                    _alerta_estado[caja] = True
                    enviar_telegram(f"\U0001F534 <b>{nombre}</b> se cayo.\nSin conexion hace {int(gap//60)} min. Los QR de ese equipo no cobran hasta que vuelva.")
                elif (not caido) and ya and gap < 90:
                    _alerta_estado[caja] = False
                    enviar_telegram(f"\U0001F7E2 <b>{nombre}</b> volvio a estar online.")
        except Exception as e:
            print(f"Error en vigilancia de equipos: {e}")
        time.sleep(60)

@app.route("/probar_telegram/<clave>")
def probar_telegram(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return "Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en Railway", 400
    ok = enviar_telegram("\u2705 Prueba de TermoPago: las alertas por Telegram funcionan.")
    return ("Enviado, fijate el Telegram" if ok else "Fallo el envio, revisa token/chat_id"), (200 if ok else 500)

threading.Thread(target=vigilar_ordenes, daemon=True).start()
threading.Thread(target=mqtt_liveness_loop, daemon=True).start()
threading.Thread(target=vigilar_equipos, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
