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
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ordenes VALUES (%s, %s, %s, %s, %s)",
        (str(uuid.uuid4()), "termo_001", 10, "pendiente", datetime.now().isoformat())
    )
    conn.commit()
    cur.close()
    conn.close()
    return "Pago simulado"

# ─── Webhook MercadoPago ─────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data:
        return "ok", 200

    # Webhook de QR punto de venta
    if data.get("type") == "merchant_order":
        order_id = data["data"]["id"]
        headers = {"Authorization": f"Bearer {MP_TOKEN}"}
        r = requests.get(f"https://api.mercadopago.com/merchant_orders/{order_id}", headers=headers)
        order = r.json()
        pagos_aprobados = [p for p in order.get("payments", []) if p["status"] == "approved"]
        if pagos_aprobados and order.get("order_status") == "paid":
            dispositivo_id = order.get("external_reference", "termo_001")
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO ordenes VALUES (%s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), dispositivo_id, 1800, "pendiente", datetime.now().isoformat())
            )
            conn.commit()
            cur.close()
            conn.close()
            print(f"Pago QR aprobado para {dispositivo_id}")
        return "ok", 200

    # Webhook de Checkout Pro (mantener compatibilidad)
    if data.get("type") == "payment":
        sdk = mercadopago.SDK(MP_TOKEN)
        pago_id = data["data"]["id"]
        pago = sdk.payment().get(pago_id)["response"]
        if pago["status"] == "approved":
            dispositivo_id = pago.get("metadata", {}).get("dispositivo_id", "termo_001")
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO ordenes VALUES (%s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), dispositivo_id, 1800, "pendiente", datetime.now().isoformat())
            )
            conn.commit()
            cur.close()
            conn.close()
            print(f"Pago checkout aprobado: {pago_id}")
        return "ok", 200

    return "ok", 200

# ─── Crear sucursal y caja (ejecutar una sola vez) ───────────────

@app.route("/setup_qr/<clave>")
def setup_qr(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    headers = {
        "Authorization": f"Bearer {MP_TOKEN}",
        "Content-Type": "application/json"
    }

    # 1. Crear sucursal
    sucursal = {
        "name": "TermoPago Concordia",
        "business_hours": {
            "monday": [{"open": "00:00", "close": "23:59"}],
            "tuesday": [{"open": "00:00", "close": "23:59"}],
            "wednesday": [{"open": "00:00", "close": "23:59"}],
            "thursday": [{"open": "00:00", "close": "23:59"}],
            "friday": [{"open": "00:00", "close": "23:59"}],
            "saturday": [{"open": "00:00", "close": "23:59"}],
            "sunday": [{"open": "00:00", "close": "23:59"}]
        },
        "location": {
            "street_name": "Concordia",
            "city_name": "Concordia",
            "state_name": "Entre Ríos",
            "country_name": "Argentina",
            "latitude": -31.3927,
            "longitude": -58.0157
        },
        "external_id": "termo_sucursal_001"
    }

    r1 = requests.post(
        f"https://api.mercadopago.com/users/{USER_ID}/stores",
        json=sucursal,
        headers=headers
    )
    store = r1.json()

    if "id" not in store:
        return jsonify({"error": "No se pudo crear la sucursal", "detalle": store}), 400

    store_id = store["id"]

    # 2. Crear caja/POS
    caja = {
        "name": "Caja TermoPago 001",
        "fixed_amount": True,
        "store_id": store_id,
        "external_store_id": "termo_sucursal_001",
        "external_id": "termo_001",
        "notification_url": "https://web-production-94bbab.up.railway.app/webhook"
    }

    r2 = requests.post(
        f"https://api.mercadopago.com/pos",
        json=caja,
        headers=headers
    )
    pos = r2.json()

    if "id" not in pos:
        return jsonify({"error": "No se pudo crear la caja", "detalle": pos}), 400

    return jsonify({
        "sucursal_id": store_id,
        "caja_id": pos["id"],
        "qr_link": pos.get("qr", {}).get("image"),
        "qr_data": pos.get("qr", {}).get("template_document"),
        "pos_completo": pos
    })

# ─── Asignar orden al QR (cliente escanea → ve el precio) ────────

@app.route("/orden_qr/<clave>/<pos_id>")
def crear_orden_qr(clave, pos_id):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403

    headers = {
        "Authorization": f"Bearer {MP_TOKEN}",
        "Content-Type": "application/json",
        "x-idempotency-key": str(uuid.uuid4())
    }

    orden = {
        "external_reference": "termo_001",
        "notification_url": "https://web-production-94bbab.up.railway.app/webhook",
        "items": [{
            "sku_number": "AGUA001",
            "category": "services",
            "title": "Agua caliente 30 minutos",
            "description": "Servicio de agua caliente por 30 minutos",
            "unit_price": PRECIO,
            "quantity": 1,
            "unit_measure": "unit",
            "total_amount": PRECIO
        }],
        "total_amount": PRECIO
    }

    r = requests.put(
        f"https://api.mercadopago.com/instore/orders/qr/seller/collectors/{USER_ID}/pos/{pos_id}/qrs",
        json=orden,
        headers=headers
    )

    return jsonify(r.json())

# ─── Checkout Pro (mantener para compatibilidad) ─────────────────

@app.route("/crear_pago")
def crear_pago():
    sdk = mercadopago.SDK(MP_TOKEN)
    preference = {
        "items": [{"title": "Agua caliente 30 minutos", "quantity": 1, "unit_price": PRECIO, "currency_id": "ARS"}],
        "metadata": {"dispositivo_id": "termo_001"},
        "notification_url": "https://web-production-94bbab.up.railway.app/webhook",
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
