from flask import Flask, jsonify, request
import mercadopago
import os
import sqlite3
import uuid
from datetime import datetime

app = Flask(__name__)

MP_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
CLAVE_SECRETA = os.environ.get("CLAVE_SECRETA")

def init_db():
    db = sqlite3.connect("ordenes.db")
    db.execute("""
        CREATE TABLE IF NOT EXISTS ordenes (
            id TEXT PRIMARY KEY,
            dispositivo_id TEXT,
            segundos INTEGER,
            estado TEXT,
            fecha TEXT
        )
    """)
    db.commit()
    db.close()

def get_db():
    db = sqlite3.connect("ordenes.db")
    db.row_factory = sqlite3.Row
    return db

init_db()  # ← acá, después de definirla

@app.route("/orden/<dispositivo_id>")
def consultar_orden(dispositivo_id):
    db = get_db()
    orden = db.execute(
        "SELECT * FROM ordenes WHERE dispositivo_id=? AND estado='pendiente' ORDER BY fecha ASC LIMIT 1",
        (dispositivo_id,)
    ).fetchone()
    if orden:
        db.execute("UPDATE ordenes SET estado='ejecutando' WHERE id=?", (orden["id"],))
        db.commit()
        db.close()
        return jsonify({"encender": True, "segundos": orden["segundos"]})
    db.close()
    return jsonify({"encender": False})

@app.route("/simular_pago/<clave>")
def simular_pago(clave):
    if clave != CLAVE_SECRETA:
        return "No autorizado", 403
    db = get_db()
    db.execute(
        "INSERT INTO ordenes VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), "termo_001", 10, "pendiente", datetime.now().isoformat())
    )
    db.commit()
    db.close()
    return "✅ Pago simulado"

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
        db = get_db()
        db.execute(
            "INSERT INTO ordenes VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), dispositivo_id, 10, "pendiente", datetime.now().isoformat())
        )
        db.commit()
        db.close()
        print(f"✅ Pago aprobado: {pago_id}")
    return "ok", 200

@app.route("/crear_pago")
def crear_pago():
    sdk = mercadopago.SDK(MP_TOKEN)
    preference = {
        "items": [{
            "title": "Agua caliente 10 minutos",
            "quantity": 1,
            "unit_price": 500.0,
            "currency_id": "ARS"
        }],
        "metadata": {