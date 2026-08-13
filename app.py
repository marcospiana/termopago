from flask import Flask, jsonify, request, redirect
import mercadopago
import os
import psycopg2
import psycopg2.extras
import uuid
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
    try:
        r = requests.post("https://api.mercadopago.com/v1/orders", json=orden, headers=headers, timeout=10)
        if r.status_code == 201:
            actualizar_dispositivo(disp["id"], {"orden_qr_id": r.json().get("id", ""), "ultimo_rearme": ahora})
            print(f"QR re-armado: {disp['id']}")
        else:
            # si ya hay orden activa, registrar el intento para no insistir
            actualizar_dispositivo(disp["id"], {"ultimo_rearme": ahora})
            print(f"Re-arme {disp['id']}: {r.status_code} {r.text[:200]}")
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
    insertar_orden(str(uuid.uuid4()), dispositivo_id, segundos)
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
            insertar_orden(f"ord_{order_id}", dispositivo_id, segundos_de(dispositivo_id), order.get("total_amount"))
            print(f"Pago QR aprobado para {dispositivo_id}")
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

# ─── Alta de dispositivos: crea la caja y el QR en MercadoPago ────

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

    # token_env puede ser:  MP_TOKEN_XXX (variable de Railway)  o  cliente_ALIAS (OAuth)
    cliente_alias = None
    if token_env and token_env.startswith("cliente_"):
        cliente_alias = token_env[len("cliente_"):]
        token = token_cliente(cliente_alias)
        if not token:
            return jsonify({"error": f"El cliente '{cliente_alias}' no está conectado (usar /conectar_cliente)"}), 400
        token_env = None
    elif token_env and not os.environ.get(token_env):
        return jsonify({"error": f"La variable {token_env} no existe en Railway"}), 400
    else:
        token = os.environ.get(token_env) if token_env else MP_TOKEN

    nombre = nombre.replace("-", " ")
    existente = get_dispositivo(disp_id)
    external_id = "".join(c for c in disp_id.upper() if c.isalnum())[:40]

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
                return jsonify({"error": "Token inválido", "detalle": r.json()}), 400
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
                return jsonify({"error": "No se pudo crear la sucursal", "detalle": store}), 400
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
            return jsonify({"error": "No se pudo crear la caja", "detalle": pos}), 400

    if not existente:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO dispositivos (id, nombre, external_pos_id, precio, segundos, token_env, cliente) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (disp_id, nombre, external_id, 500, 300, token_env, cliente_alias)
        )
        conn.commit()
        cur.close()
        conn.close()

    disp = get_dispositivo(disp_id)
    rearmar_qr(disp)

    return jsonify({
        "dispositivo": disp_id,
        "nombre": nombre,
        "caja_id": pos["id"],
        "external_pos_id": external_id,
        "qr_imagen": pos.get("qr", {}).get("image"),
        "qr_pdf": pos.get("qr", {}).get("template_document"),
        "nota": f"Precio y tiempo se ajustan en /config (por defecto $500 / 5 min)"
    })

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
                nuevos_minutos = int(request.form[f"minutos__{disp['id']}"])
                if nuevo_precio <= 0 or nuevos_minutos <= 0:
                    raise ValueError
                precio_cambio = nuevo_precio != float(disp["precio"])
                tiempo_cambio = (nuevos_minutos * 60) != int(disp["segundos"])
                actualizar_dispositivo(disp["id"], {"precio": nuevo_precio, "segundos": nuevos_minutos * 60})
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
        filas += f"""
  <fieldset>
    <legend>{disp['nombre']} <small>({disp['id']})</small></legend>
    <label>Precio (ARS)</label>
    <input type="number" name="precio__{disp['id']}" step="0.01" min="1" value="{float(disp['precio']):g}">
    <label>Tiempo (minutos)</label>
    <input type="number" name="minutos__{disp['id']}" min="1" value="{disp['segundos'] // 60}">
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
</style></head><body>
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


@app.route("/reinicios/<clave>")
def reinicios(clave):
    """Historial de reinicios del ESP con la causa y cuánto estuvo activo antes
    de cada reinicio. Sirve para saber POR QUÉ falla (watchdog, pico eléctrico,
    corte de luz, WiFi) sin adivinar."""
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT dispositivo_id, motivo, fecha FROM reinicios ORDER BY fecha ASC LIMIT 1000")
    rows = cur.fetchall()
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
    resumen = {}
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
        resumen[mot] = resumen.get(mot, 0) + 1

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
</style></head><body>
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
</style></head><body>
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
</style></head><body>
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
</style></head><body>
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
</style></head><body>
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

threading.Thread(target=vigilar_ordenes, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
