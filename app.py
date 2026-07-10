from flask import Flask, jsonify, request, redirect
import mercadopago
import os
import psycopg2
import psycopg2.extras
import uuid
from datetime import datetime

app = Flask(__name__)

MP_TOKEN      = os.environ.get("MP_ACCESS_TOKEN")
CLAVE_SECRETA = os.environ.get("CLAVE_SECRETA")
PRECIO        = float(os.environ.get("PRECIO", "500"))
DATABASE_URL  = os.environ.get("DATABASE_URL")

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

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data or data.get("type") != "payment":
        return "ok", 200
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
        print("Pago aprobado: " + str(pago_id))
    return "ok", 200

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
            "excluded_payment_methods": [
                {"id": "rapipago"},
                {"id": "pagofacil"}
            ],
            "installments": 1
        }
    }
    result = sdk.preference().create(preference)
    link = result["response"]["init_point"]
    return redirect(link)

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
