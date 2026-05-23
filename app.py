from flask import Flask, jsonify

app = Flask(__name__)

orden_pendiente = False

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
    return "✅ Pago simulado — el NodeMCU debería encender en segundos"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)