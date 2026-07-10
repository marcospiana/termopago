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
    conn.commit()
    cur.close()
    conn.close()

init_db()

def mp_headers():
    return {
        "Authorization": f"Bearer {MP_TOKEN}",
        "Content-Type": "application/json"
    }

def insertar_orden(orden_id, dispositivo_id, segundos):
    """Inserta una orden. El PK evita duplicados si MP notifica dos veces."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ordenes VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (orden_id, dispositivo_id, segundos, "pendiente", datetime.now().isoformat())
    )
    conn.commit()
    cur.close()
    conn.close()

# ─── Rutas del ESP32 ────────────────────────────────────────────

@app.route("/orden/<dispositivo_id>")
def consultar_orden(dispositivo_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM ordenes WHERE dispositivo_id=%s AND estado='pendiente' ORDER BY fecha ASC LIMIT 1",
        (dispositivo_id,)
    )
    orden = cur.fetchone()
    if orden:
        cur.execute("UPDATE ordenes SET estado='ejecutando' WHERE id=%s", (orden["id"],))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"encender": True, "segundos": orden["segundos"]})
    cur.close()
    conn.close()
    return jsonify({"encender": False})

# ─── Simulación de pago para pruebas ────────────────────────────

@app.route("/simular_pago/<clave>")
def simular_pago(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    insertar_orden(str(uuid.uuid4()), "termo_001", 10)
    return "Pago simulado"

# ─── Webhook MercadoPago ─────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data:
        return "ok", 200

    # Los pagos QR llegan como merchant_order. MP los manda en dos formatos:
    #   IPN:     {"topic": "merchant_order", "resource": "https://.../merchant_orders/123"}
    #   Webhook: {"type": "merchant_order", "data": {"id": "123"}}
    topic = data.get("topic") or data.get("type")

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
            # id derivado del merchant_order: si MP notifica 2 veces, no duplica
            insertar_orden(f"mo_{order['id']}", dispositivo_id, 1800)
            print(f"Pago QR aprobado para {dispositivo_id}")
        return "ok", 200

    # Notificaciones de la Orders API (tema "orders" configurado en el panel de MP)
    if topic in ("order", "orders", "topic_order") or (data.get("data", {}).get("id", "") or "").startswith("ORD"):
        order_id = data["data"]["id"]
        r = requests.get(f"https://api.mercadopago.com/v1/orders/{order_id}", headers=mp_headers())
        order = r.json()
        if order.get("status") == "processed":
            dispositivo_id = order.get("external_reference", "termo_001")
            insertar_orden(f"ord_{order_id}", dispositivo_id, 1800)
            print(f"Pago QR (Orders API) aprobado para {dispositivo_id}")
        return "ok", 200

    if topic == "payment":
        sdk = mercadopago.SDK(MP_TOKEN)
        pago_id = data["data"]["id"]
        pago = sdk.payment().get(pago_id)["response"]
        if pago.get("status") == "approved":
            dispositivo_id = pago.get("metadata", {}).get("dispositivo_id", "termo_001")
            insertar_orden(f"pay_{pago_id}", dispositivo_id, 1800)
            print(f"Pago checkout aprobado: {pago_id}")
        return "ok", 200

    return "ok", 200

# ─── Diagnóstico de credenciales y QR ────────────────────────────

@app.route("/diagnostico/<clave>")
def diagnostico(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    resultado = {}

    # 1. ¿El token es válido y de qué cuenta es?
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

    # 2. Sucursales
    r = requests.get(f"https://api.mercadopago.com/users/{USER_ID}/stores/search", headers=mp_headers())
    resultado["sucursales"] = {"status": r.status_code, "respuesta": r.json() if r.text else None}

    # 3. Cajas
    r = requests.get("https://api.mercadopago.com/pos", headers=mp_headers())
    resultado["cajas"] = {"status": r.status_code, "respuesta": r.json() if r.text else None}

    return jsonify(resultado)

# ─── Ver sucursales existentes ───────────────────────────────────

@app.route("/ver_sucursales")
def ver_sucursales():
    # El endpoint correcto es /stores/search (GET /users/{id}/stores no existe)
    r = requests.get(f"https://api.mercadopago.com/users/{USER_ID}/stores/search", headers=mp_headers())
    return r.text, r.status_code, {"Content-Type": "application/json"}

@app.route("/ver_cajas")
def ver_cajas():
    r = requests.get("https://api.mercadopago.com/pos", headers=mp_headers())
    return r.text, r.status_code, {"Content-Type": "application/json"}

# ─── Crear sucursal y caja (idempotente, se puede llamar varias veces) ───

@app.route("/setup_qr/<clave>")
def setup_qr(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    EXTERNAL_STORE_ID = "TERMOSUC001"
    EXTERNAL_POS_ID   = "TERMOPOS001"

    # 1. Buscar sucursal existente
    r = requests.get(
        f"https://api.mercadopago.com/users/{USER_ID}/stores/search",
        params={"external_id": EXTERNAL_STORE_ID},
        headers=mp_headers()
    )
    existentes = r.json().get("results", []) if r.status_code == 200 else []

    if existentes:
        store = existentes[0]
    else:
        sucursal = {
            "name": "TermoPago Concordia",
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
            "external_id": EXTERNAL_STORE_ID
        }
        r1 = requests.post(
            f"https://api.mercadopago.com/users/{USER_ID}/stores",
            json=sucursal, headers=mp_headers()
        )
        store = r1.json()
        if "id" not in store:
            return jsonify({"error": "No se pudo crear la sucursal", "detalle": store}), 400

    store_id = store["id"]

    # 2. Buscar caja existente
    r = requests.get(
        "https://api.mercadopago.com/pos",
        params={"external_id": EXTERNAL_POS_ID},
        headers=mp_headers()
    )
    cajas = r.json().get("results", []) if r.status_code == 200 else []

    if cajas:
        pos = cajas[0]
    else:
        caja = {
            "name": "Caja TermoPago 001",
            "fixed_amount": True,
            "store_id": int(store_id),
            "external_store_id": EXTERNAL_STORE_ID,
            "external_id": EXTERNAL_POS_ID,
            "category": 621102
        }
        r2 = requests.post("https://api.mercadopago.com/pos", json=caja, headers=mp_headers())
        pos = r2.json()
        if "id" not in pos:
            return jsonify({"error": "No se pudo crear la caja", "detalle": pos}), 400

    return jsonify({
        "sucursal_id": store_id,
        "caja_id": pos["id"],
        "external_pos_id": EXTERNAL_POS_ID,
        "qr_imagen": pos.get("qr", {}).get("image"),
        "qr_data": pos.get("qr", {}).get("template_document"),
        "siguiente_paso": f"GET /orden_qr/<clave>/{EXTERNAL_POS_ID} para cargar la orden al QR"
    })

# ─── Asignar orden al QR — Orders API (usar el external_id de la caja) ───

@app.route("/orden_qr/<clave>/<external_pos_id>")
def crear_orden_qr(clave, external_pos_id):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    headers = mp_headers()
    headers["X-Idempotency-Key"] = str(uuid.uuid4())

    monto = f"{PRECIO:.2f}"
    orden = {
        "type": "qr",
        "external_reference": "termo_001",
        "description": "Agua caliente 30 minutos",
        "expiration_time": "PT3H",
        "total_amount": monto,
        "config": {
            "qr": {
                "external_pos_id": external_pos_id,
                "mode": "static"
            }
        },
        "transactions": {
            "payments": [{"amount": monto}]
        },
        "items": [{
            "title": "Agua caliente 30 minutos",
            "unit_price": monto,
            "quantity": 1,
            "unit_measure": "unit",
            "external_code": "AGUA001"
        }]
    }

    r = requests.post("https://api.mercadopago.com/v1/orders", json=orden, headers=headers)
    return r.text, r.status_code, {"Content-Type": "application/json"}

# ─── Checkout Pro ─────────────────────────────────────────────────

@app.route("/crear_pago")
def crear_pago():
    sdk = mercadopago.SDK(MP_TOKEN)
    preference = {
        "items": [{"title": "Agua caliente 30 minutos", "quantity": 1, "unit_price": PRECIO, "currency_id": "ARS"}],
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
