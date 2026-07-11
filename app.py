from flask import Flask, jsonify, request, redirect
import mercadopago
import os
import psycopg2
import psycopg2.extras
import uuid
import requests
from datetime import datetime

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
    if vence and (vence - datetime.now()).total_seconds() > 7 * 86400:
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
            vence_nuevo = datetime.fromtimestamp(datetime.now().timestamp() + t.get("expires_in", 15552000)).isoformat()
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

def insertar_orden(orden_id, dispositivo_id, segundos):
    """Inserta una orden. El PK evita duplicados si MP notifica dos veces."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ordenes (id, dispositivo_id, segundos, estado, fecha) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (orden_id, dispositivo_id, segundos, "pendiente", datetime.now().isoformat())
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
            if r.status_code == 200 and r.json().get("status") == "processed":
                o = r.json()
                insertar_orden(f"ord_{anterior}", o.get("external_reference", disp["id"]), disp["segundos"])
                print(f"Pago recuperado por verificación directa: {anterior}")
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
        "expiration_time": "PT3H",
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
    ahora = datetime.now().isoformat()
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

@app.route("/orden/<dispositivo_id>")
def consultar_orden(dispositivo_id):
    # re-armar el QR del dispositivo si nunca se armó o pasaron más de 2,5 hs
    disp = get_dispositivo(dispositivo_id)
    if disp:
        rearme = disp.get("ultimo_rearme")
        try:
            vencido = (not rearme) or (datetime.now() - datetime.fromisoformat(rearme)).total_seconds() > 9000
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
            (datetime.now().isoformat(), orden["id"])
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
            transcurrido = (datetime.now() - datetime.fromisoformat(ejecutando["inicio"])).total_seconds()
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
            insertar_orden(f"ord_{order_id}", dispositivo_id, segundos_de(dispositivo_id))
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
            insertar_orden(f"mo_{order['id']}", dispositivo_id, segundos_de(dispositivo_id))
            print(f"Pago QR (legacy) aprobado para {dispositivo_id}")
        return "ok", 200

    # Pagos de Checkout Pro
    if topic == "payment":
        sdk = mercadopago.SDK(MP_TOKEN)
        pago_id = data["data"]["id"]
        pago = sdk.payment().get(pago_id)["response"]
        if pago.get("status") == "approved":
            dispositivo_id = pago.get("metadata", {}).get("dispositivo_id", "termo_001")
            insertar_orden(f"pay_{pago_id}", dispositivo_id, segundos_de(dispositivo_id))
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
    vence = datetime.fromtimestamp(datetime.now().timestamp() + t.get("expires_in", 15552000)).isoformat()
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
                actualizar_dispositivo(disp["id"], {"precio": nuevo_precio, "segundos": nuevos_minutos * 60})
                if precio_cambio:
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

@app.route("/ver_sucursales")
def ver_sucursales():
    r = requests.get(f"https://api.mercadopago.com/users/{USER_ID}/stores/search", headers=mp_headers())
    return r.text, r.status_code, {"Content-Type": "application/json"}

@app.route("/ver_cajas")
def ver_cajas():
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

# ─── Historial ───────────────────────────────────────────────────

@app.route("/historial")
def historial():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ordenes ORDER BY fecha DESC LIMIT 20")
    ordenes = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(o) for o in ordenes])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
