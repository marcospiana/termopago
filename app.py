from flask import Flask, jsonify, request
import mercadopago
import os

app = Flask(__name__)

orden_pendiente = False
MP_TOKEN = os.environ.get("MP_ACCESS_TOKEN")

@app.route("/orden/<dispositivo_id>")
def consultar_orden(dispositivo_id):
    global orden_pendiente
    if orden_pendiente:
        orden_pendiente = False
        return jsonify({"encender": True, "segundos": 10})
    return jsonify({"encender": False})

@app.route("/simular_pago")
def simular_pago():
    global orden_pendiente
    orden_pendiente = True
    return "✅ Pago simulado"

@app.route("/webhook", methods=["POST"])
def webhook():
    global orden_pendiente
    data = request.json

    if not data or data.get("type") != "payment":
        return "ok", 200

    sdk = mercadopago.SDK(MP_TOKEN)
    pago_id = data["data"]["id"]
    pago = sdk.payment().get(pago_id)["response"]

    if pago["status"] == "approved":
        orden_pendiente = True
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
            "dispositivo_id": "termo_001"
        },
        "notification_url": "https://web-production-94bbab.up.railway.app/webhook"
    }
    result = sdk.preference().create(preference)
    link = result["response"]["init_point"]
    return jsonify({"link": link})

@app.route("/check_token")
def check_token():
    token = MP_TOKEN or "NO HAY TOKEN"
    return jsonify({"token_inicio": token[:15]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)