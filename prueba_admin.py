"""Banco de pruebas: levanta app.py contra un SQLite en memoria haciendose
pasar por psycopg2, para verificar la migracion de tipos y el panel /admin
sin tocar la base de produccion."""
import os, re, sys, sqlite3, types

os.environ.setdefault("CLAVE_SECRETA", "TESTKEY")
os.environ.setdefault("DATABASE_URL", "postgresql://fake")
os.environ.setdefault("MQTT_HOST", "broker.test")
os.environ.setdefault("MQTT_USER", "termopago")
os.environ.setdefault("MQTT_PASS", "secreta")

_conn_unica = sqlite3.connect(":memory:", check_same_thread=False)
_conn_unica.row_factory = sqlite3.Row


def _traducir(sql):
    sql = sql.replace("%s", "?")
    sql = sql.replace("%%", "%")        # psycopg2 escapa el % literal como %%
    sql = re.sub(r"SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT", sql, flags=re.I)
    return sql


def _expandir_tuplas(sql, args):
    """psycopg2 adapta una tupla a (a,b,c) para un 'IN %s'; sqlite no sabe.
    Expandimos la tupla a tantos ? como elementos tenga."""
    if not args or not any(isinstance(a, (tuple, list)) for a in args):
        return sql, args
    partes = sql.split("?")
    if len(partes) - 1 != len(args):
        return sql, args
    salida, planos = partes[0], []
    for a, resto in zip(args, partes[1:]):
        if isinstance(a, (tuple, list)):
            salida += "(" + ",".join("?" * len(a)) + ")" + resto
            planos.extend(a)
        else:
            salida += "?" + resto
            planos.append(a)
    return salida, planos


class _Cur:
    def __init__(self, cur): self._c = cur
    def execute(self, sql, args=()):
        s = _traducir(sql)
        if re.search(r"ALTER TABLE .* ADD COLUMN IF NOT EXISTS", sql, re.I):
            s = re.sub(r"IF NOT EXISTS ", "", s, flags=re.I)
            try:
                return self._c.execute(s, args)
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e):
                    return None
                raise
        s, args = _expandir_tuplas(s, args)
        return self._c.execute(s, args)
    def fetchone(self):
        r = self._c.fetchone()
        return dict(r) if r else None
    def fetchall(self):
        return [dict(r) for r in self._c.fetchall()]
    def close(self): pass
    @property
    def rowcount(self): return self._c.rowcount


class _Conn:
    cursor_factory = None
    def cursor(self): return _Cur(_conn_unica.cursor())
    def commit(self): _conn_unica.commit()
    def close(self): pass


fake = types.ModuleType("psycopg2")
fake.connect = lambda *a, **k: _Conn()
fake.extras = types.ModuleType("psycopg2.extras")
fake.extras.RealDictCursor = object
sys.modules["psycopg2"] = fake
sys.modules["psycopg2.extras"] = fake.extras

# MercadoPago: nada de red en la prueba.
import requests
class _R:
    status_code = 200
    def json(self): return {"results": [{"id": 999, "store_id": 7,
                                         "qr": {"image": "http://qr", "template_document": "http://pdf"}}]}
requests.get = lambda *a, **k: _R()
requests.post = lambda *a, **k: _R()

import app
app.rearmar_qr = lambda disp: None          # no tocar MP
app.cancelar_orden_qr = lambda disp: None
app.MP_CLIENT_ID = "123"
app.MP_CLIENT_SECRET = "abc"

c = app.app.test_client()
K = "TESTKEY"
fallas = []


def chequear(nombre, cond, extra=""):
    print(("  OK   " if cond else "  FALLA") + f" {nombre} {extra if not cond else ''}")
    if not cond:
        fallas.append(nombre)


print("\n== 1. Backfill de las cajas viejas ==")
cur = _conn_unica.cursor()
for cid, nom in [("inflado01", "Inflador"), ("aspiradora01", "Aspiradora 1"),
                 ("soplado01", "Soplado 1"), ("aspiradora02", "Aspiradora 2"),
                 ("soplado02", "Soplado 2"), ("villagas01", "Fichas Villagas"),
                 ("termo_002", "Termo viejo")]:
    cur.execute("INSERT OR IGNORE INTO dispositivos (id,nombre,precio,segundos) VALUES (?,?,?,?)",
                (cid, nom, 500, 300))
_conn_unica.commit()
app.init_db()
app.invalidar_cache_tipos()

tipos = {r["id"]: (r["tipo"], r["esp_id"], r["canal"])
         for r in _Cur(_conn_unica.cursor()).execute(
             "SELECT id,tipo,esp_id,canal FROM dispositivos").fetchall()}
chequear("villagas01 quedo 'fichas'", tipos["villagas01"][0] == "fichas", tipos["villagas01"])
chequear("inflado01 quedo 'pulso'", tipos["inflado01"][0] == "pulso", tipos["inflado01"])
chequear("aspiradora01 quedo 'sostenida'", tipos["aspiradora01"][0] == "sostenida", tipos["aspiradora01"])
chequear("termo_002 quedo 'legacy'", tipos["termo_002"][0] == "legacy", tipos["termo_002"])
chequear("aspiradora01 y soplado01 comparten esp", tipos["aspiradora01"][1] == tipos["soplado01"][1] == "estacion01")
chequear("canales 0 y 1 asignados", (tipos["aspiradora01"][2], tipos["soplado01"][2]) == (0, 1))
chequear("caja sola = su propio esp", tipos["inflado01"][1] == "inflado01")

print("\n== 2. Los conjuntos dinamicos dan lo mismo que los sets viejos ==")
chequear("ESTACIONES_MQTT == semilla", set(app.ESTACIONES_MQTT) == app.SEMILLA_MQTT, sorted(app.ESTACIONES_MQTT))
chequear("ESTACIONES_PULSO == semilla", set(app.ESTACIONES_PULSO) == app.SEMILLA_PULSO, sorted(app.ESTACIONES_PULSO))
chequear("ESTACIONES_FICHAS == semilla", set(app.ESTACIONES_FICHAS) == app.SEMILLA_FICHAS, sorted(app.ESTACIONES_FICHAS))
chequear("'villagas01' in ESTACIONES_FICHAS", "villagas01" in app.ESTACIONES_FICHAS)
chequear("'termo_002' NO es MQTT", "termo_002" not in app.ESTACIONES_MQTT)
chequear("caja inexistente no es MQTT", "no_existe" not in app.ESTACIONES_MQTT)
chequear("hermanas de aspiradora01", app.cajas_hermanas("aspiradora01") == {"aspiradora01", "soplado01"},
         app.cajas_hermanas("aspiradora01"))
chequear("hermanas de inflado01", app.cajas_hermanas("inflado01") == {"inflado01"})
chequear("hermanas de una caja desconocida", app.cajas_hermanas("zzz") == {"zzz"})

print("\n== 2b. estacion02 (aspiradora02 + soplado02) intacta ==")
chequear("aspiradora02 sigue sostenida", tipos["aspiradora02"][0] == "sostenida", tipos["aspiradora02"])
chequear("soplado02 sigue sostenida", tipos["soplado02"][0] == "sostenida", tipos["soplado02"])
chequear("las dos son MQTT", {"aspiradora02", "soplado02"} <= set(app.ESTACIONES_MQTT))
chequear("ninguna es de pulso", not ({"aspiradora02", "soplado02"} & set(app.ESTACIONES_PULSO)))
chequear("comparten el ESP estacion02",
         app.cajas_hermanas("aspiradora02") == {"aspiradora02", "soplado02"},
         app.cajas_hermanas("aspiradora02"))
chequear("canales 0 y 1", (tipos["aspiradora02"][2], tipos["soplado02"][2]) == (0, 1))

print("\n== 2c. DB caida con cache frio -> semillas, no 'todo legacy' ==")
_get_db_real = app.get_db
app.get_db = lambda: (_ for _ in ()).throw(RuntimeError("DB caida"))
app._cache_tipos["filas"] = {}          # simula recien deployado
app._cache_tipos["t"] = 0.0
chequear("aspiradora02 sigue siendo MQTT con la DB caida", "aspiradora02" in app.ESTACIONES_MQTT)
chequear("inflado01 sigue siendo pulso", "inflado01" in app.ESTACIONES_PULSO)
chequear("villagas01 sigue siendo fichas", "villagas01" in app.ESTACIONES_FICHAS)
chequear("las hermanas de estacion02 se mantienen",
         app.cajas_hermanas("soplado02") == {"aspiradora02", "soplado02"},
         app.cajas_hermanas("soplado02"))
chequear("el fallback NO se cachea (reintenta contra la DB)", app._cache_tipos["filas"] == {})
app.get_db = _get_db_real
app.invalidar_cache_tipos()
set(app.ESTACIONES_MQTT)   # fuerza una lectura ya con la DB sana
chequear("al volver la DB vuelve a mandar la DB (ve cajas que no estan en la semilla)",
         "termo_002" in app._cache_tipos["filas"], sorted(app._cache_tipos["filas"]))

print("\n== 3. El backfill no pisa un tipo cambiado a mano ==")
app.actualizar_dispositivo("termo_002", {"tipo": "pulso"})
app.invalidar_cache_tipos()
app.init_db()
app.invalidar_cache_tipos()
chequear("termo_002 sigue 'pulso' tras re-deploy", "termo_002" in app.ESTACIONES_PULSO)
app.actualizar_dispositivo("termo_002", {"tipo": "legacy"})
app.invalidar_cache_tipos()

print("\n== 4. Panel /admin ==")
r = c.get(f"/admin/{K}")
chequear("carga 200", r.status_code == 200, r.status_code)
chequear("clave mala -> 403", c.get("/admin/mala").status_code == 403)

r = c.post(f"/admin/{K}", data={"accion": "nuevo_cliente", "alias": "Lava Dero!", "nombre": "Lavadero Sur"})
chequear("alta de cliente", b"creado" in r.data, r.data[:200])
chequear("alias slugificado (espacio -> _, sin simbolos)",
         app.get_cliente("lava_dero") is not None, [x["alias"] for x in app.get_clientes()])
c.post(f"/admin/{K}", data={"accion": "nuevo_cliente", "alias": "lavadero", "nombre": "Lavadero Sur"})
cli = app.get_cliente("lavadero")
chequear("le genero panel_token y pin", bool(cli["panel_token"]) and len(cli["pin"]) == 4)

r = c.post(f"/admin/{K}", data={"accion": "nuevo_cliente", "alias": "lavadero", "nombre": "x"})
chequear("no deja duplicar alias", "Ya existe".encode() in r.data)

print("\n== 5. Alta de maquinas desde el panel ==")
r = c.post(f"/admin/{K}", data={
    "accion": "nueva_maquina", "cliente": "lavadero", "disp_id": "lavadero01",
    "nombre": "Aspiradora sur", "tipo": "sostenida", "esp_id": "lavadero_esp1",
    "canal": "0", "precio": "800", "valor": "240", "cuenta": ""})
chequear("crea la maquina", b"creada" in r.data, r.data[:300])
d = app.get_dispositivo("lavadero01")
chequear("guardo tipo/esp/canal", (d["tipo"], d["esp_id"], d["canal"]) == ("sostenida", "lavadero_esp1", 0), d and dict(d))
chequear("guardo precio y valor", (float(d["precio"]), int(d["segundos"])) == (800.0, 240))
chequear("quedo asociada al cliente", d["cliente"] == "lavadero")
app.invalidar_cache_tipos()
chequear("ya es MQTT sin redeploy", "lavadero01" in app.ESTACIONES_MQTT)
chequear("y NO es de pulso", "lavadero01" not in app.ESTACIONES_PULSO)

c.post(f"/admin/{K}", data={
    "accion": "nueva_maquina", "cliente": "lavadero", "disp_id": "lavadero02",
    "nombre": "Soplado sur", "tipo": "sostenida", "esp_id": "lavadero_esp1",
    "canal": "1", "precio": "800", "valor": "240", "cuenta": ""})
app.invalidar_cache_tipos()
chequear("2 cajas en un mismo ESP son hermanas",
         app.cajas_hermanas("lavadero01") == {"lavadero01", "lavadero02"}, app.cajas_hermanas("lavadero01"))

r = c.post(f"/admin/{K}", data={
    "accion": "nueva_maquina", "cliente": "lavadero", "disp_id": "lavadero03",
    "nombre": "Fichas", "tipo": "fichas", "esp_id": "", "canal": "",
    "precio": "3000", "valor": "", "cuenta": ""})
d = app.get_dispositivo("lavadero03")
app.invalidar_cache_tipos()
chequear("fichas: valor por defecto = 1 ficha", int(d["segundos"]) == 1, d and int(d["segundos"]))
chequear("fichas cuenta como pulso", "lavadero03" in app.ESTACIONES_PULSO)
chequear("fichas: esp_id vacio cae en su propio id", d["esp_id"] == "lavadero03")

r = c.post(f"/admin/{K}", data={
    "accion": "nueva_maquina", "cliente": "lavadero", "disp_id": "lavadero01",
    "nombre": "Repetida", "tipo": "pulso", "cuenta": ""})
chequear("no deja repetir ID de maquina", "Ya existe".encode() in r.data)

r = c.post(f"/admin/{K}", data={
    "accion": "nueva_maquina", "cliente": "lavadero", "disp_id": "malmal",
    "nombre": "X", "tipo": "inventado", "cuenta": ""})
chequear("rechaza tipo invalido", "inválido".encode() in r.data)

print("\n== 6. El payload MQTT cambia solo con el tipo ==")
capturado = {}
def _pub(topic=None, payload=None, **k): capturado["t"], capturado["p"] = topic, payload
app._mqtt_publish = types.SimpleNamespace(single=_pub)
app.publicar_activacion("lavadero03", "pago1")
chequear("expendedora manda 'cantidad'", '"cantidad": 1' in capturado["p"], capturado["p"])
app.publicar_activacion("lavadero01", "pago2")
chequear("sostenida manda 'segundos'", '"segundos": 240' in capturado["p"], capturado["p"])

print("\n== 7. Ficha de flasheo y .zip ==")
r = c.get(f"/admin/{K}/maquina/lavadero03")
chequear("ficha carga", r.status_code == 200, r.status_code)
chequear("muestra el ID a cargar en el portal", b"lavadero03" in r.data)
r = c.get(f"/admin/{K}/maquina/lavadero01")
chequear("ficha de ESP compartido lista las 2 cajas", b"lavadero02" in r.data)
chequear("ficha con clave mala -> 403", c.get("/admin/mala/maquina/lavadero01").status_code == 403)
chequear("ficha de maquina inexistente -> 404", c.get(f"/admin/{K}/maquina/nada").status_code == 404)

chequear("la ficha muestra el QR de la caja", b'class="qr"' in r.data)
chequear("y ofrece el PDF para imprimir", b"PDF para imprimir" in r.data)

# MercadoPago caido: la ficha tiene que seguir sirviendo para flashear.
_get_real = requests.get
requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("MP caido"))
r = c.get(f"/admin/{K}/maquina/lavadero01")
chequear("con MP caido la ficha igual carga", r.status_code == 200, r.status_code)
chequear("y sigue mostrando el ID a cargar en el portal", b"lavadero01" in r.data)
chequear("avisa que no pudo traer el QR", "No pude consultar MercadoPago".encode() in r.data)
requests.get = _get_real

r = c.get(f"/admin/{K}/firmware/lavadero03.zip?secretos=1")
if r.status_code == 200:
    import io as _io, zipfile
    z = zipfile.ZipFile(_io.BytesIO(r.data))
    nombres = z.namelist()
    chequear("el zip trae el .ino", any(n.endswith(".ino") for n in nombres), nombres)
    chequear("el zip trae el LEEME", any("LEEME_lavadero03" in n for n in nombres), nombres)
    sec = [n for n in nombres if n.endswith("secretos_privado.h")]
    chequear("el zip trae secretos generados", bool(sec), nombres)
    if sec:
        chequear("secretos con el broker real", b"broker.test" in z.read(sec[0]))
    leeme = [n for n in nombres if "LEEME" in n][0]
    chequear("el LEEME dice el ID de caja", b"lavadero03" in z.read(leeme))
else:
    print(f"  (zip devolvio {r.status_code}: {r.data[:200]!r} — falta la carpeta del sketch en este entorno)")

print("\n== 8. Baja de maquina ==")
r = c.post(f"/admin/{K}", data={"accion": "borrar_maquina", "disp_id": "lavadero02"})
chequear("da de baja", b"baja" in r.data, r.data[:200])
chequear("desaparecio de la tabla", app.get_dispositivo("lavadero02") is None)
app.invalidar_cache_tipos()
chequear("y de los conjuntos", "lavadero02" not in app.ESTACIONES_MQTT)

print("\n== 8a. Renovar PIN y link del cliente ==")
antes = app.get_cliente("lavadero")
r = c.post(f"/admin/{K}", data={"accion": "nuevo_pin", "alias": "lavadero"})
ahora_cli = app.get_cliente("lavadero")
chequear("cambia el PIN", ahora_cli["pin"] != antes["pin"], (antes["pin"], ahora_cli["pin"]))
chequear("el PIN nuevo tiene 4 digitos", len(ahora_cli["pin"]) == 4 and ahora_cli["pin"].isdigit())
chequear("el mensaje muestra el PIN nuevo", ahora_cli["pin"].encode() in r.data)
chequear("no toca el link del panel", ahora_cli["panel_token"] == antes["panel_token"])

r = c.post(f"/admin/{K}", data={"accion": "nuevo_link", "alias": "lavadero"})
despues = app.get_cliente("lavadero")
chequear("cambia el token del panel", despues["panel_token"] != antes["panel_token"])
chequear("no toca el PIN", despues["pin"] == ahora_cli["pin"])
chequear("el link viejo deja de abrir",
         c.get(f"/panel/{antes['panel_token']}").status_code == 404)
chequear("el link nuevo abre", c.get(f"/panel/{despues['panel_token']}").status_code == 200)
r = c.post(f"/admin/{K}", data={"accion": "nuevo_pin", "alias": "no_existe"})
chequear("cliente inexistente no rompe", b"No existe ese cliente" in r.data)

print("\n== 8b. Baja de cliente ==")
r = c.post(f"/admin/{K}", data={"accion": "borrar_cliente", "alias": "lavadero"})
chequear("no deja borrar un cliente con maquinas", "todavía tiene".encode() in r.data, r.data[:300])
chequear("y el cliente sigue existiendo", app.get_cliente("lavadero") is not None)
# En este punto hay 2 clientes: 'lava_dero' sin maquinas y 'lavadero' con 2.
# El boton de baja tiene que aparecer una sola vez: para el que no tiene ninguna.
chequear("el boton de baja aparece solo para el cliente sin maquinas",
         c.get(f"/admin/{K}").data.count(b'value="borrar_cliente"') == 1,
         c.get(f"/admin/{K}").data.count(b'value="borrar_cliente"'))
r = c.post(f"/admin/{K}", data={"accion": "borrar_cliente", "alias": "lava_dero"})
chequear("borra un cliente sin maquinas", b"eliminado" in r.data, r.data[:300])
chequear("desaparecio de la tabla", app.get_cliente("lava_dero") is None)
chequear("ya no queda ningun boton de baja de cliente",
         c.get(f"/admin/{K}").data.count(b'value="borrar_cliente"') == 0)
r = c.post(f"/admin/{K}", data={"accion": "borrar_cliente", "alias": "no_existe"})
chequear("cliente inexistente da error, no rompe", b"No existe ese cliente" in r.data)

print("\n== 8c. Freno de fuerza bruta del PIN ==")
c.post(f"/admin/{K}", data={"accion": "nuevo_pin", "alias": "lavadero"})
cli = app.get_cliente("lavadero")
pin_ok, tok = cli["pin"], cli["panel_token"]
pin_malo = "0000" if pin_ok != "0000" else "1111"

for i in range(1, app.PIN_MAX_INTENTOS):
    r = c.post(f"/panel/{tok}", data={"accion": "regalar", "pin": pin_malo})
    chequear(f"intento fallido {i} avisa cuantos quedan",
             f"Te queda(n) {app.PIN_MAX_INTENTOS - i}".encode() in r.data, r.data[-400:])

r = c.post(f"/panel/{tok}", data={"accion": "regalar", "pin": pin_malo})
chequear("al llegar al tope se bloquea", "se bloqueo el regalo".encode() in r.data, r.data[-400:])
chequear("quedo grabado el bloqueo en la DB",
         bool(app.get_cliente("lavadero")["pin_bloqueado_hasta"]))

r = c.post(f"/panel/{tok}", data={"accion": "regalar", "pin": pin_ok})
chequear("bloqueado, ni el PIN correcto pasa", "Proba de nuevo en".encode() in r.data, r.data[-400:])

# El bloqueo se vence solo: lo corremos al pasado en vez de esperar 15 min.
app.guardar_cliente("lavadero", {
    "pin_bloqueado_hasta": (app.ahora_ar() - app.timedelta(minutes=1)).isoformat()})
r = c.post(f"/panel/{tok}", data={"accion": "regalar", "pin": pin_ok})
chequear("vencido el bloqueo, el PIN correcto vuelve a andar",
         "Proba de nuevo en".encode() not in r.data and b"PIN incorrecto" not in r.data,
         r.data[-400:])
chequear("el PIN correcto limpia el contador",
         not app.get_cliente("lavadero")["pin_fallidos"])

# Un PIN nuevo desde el admin tiene que levantar el bloqueo.
app.guardar_cliente("lavadero", {
    "pin_fallidos": 9,
    "pin_bloqueado_hasta": (app.ahora_ar() + app.timedelta(minutes=30)).isoformat()})
c.post(f"/admin/{K}", data={"accion": "nuevo_pin", "alias": "lavadero"})
cli = app.get_cliente("lavadero")
chequear("PIN nuevo levanta el bloqueo", not cli["pin_bloqueado_hasta"] and not cli["pin_fallidos"],
         (cli["pin_bloqueado_hasta"], cli["pin_fallidos"]))

print("\n== 9. /config sigue funcionando con los tipos nuevos ==")
r = c.get(f"/config/{K}")
chequear("config carga", r.status_code == 200, r.status_code)
chequear("expendedora pide 'Fichas por pago'", "Fichas por pago".encode() in r.data)

print("\n== 10. Todas las paginas de admin vuelven al panel ==")
for ruta in ["config", "reinicios", "cortes", "estado", "estadisticas", "historial"]:
    r = c.get(f"/{ruta}/{K}")
    ok = r.status_code == 200 and f'href="/admin/{K}"'.encode() in r.data
    chequear(f"/{ruta} tiene el link de volver", ok, f"status {r.status_code}")

# El panel del cliente NO puede filtrar la clave secreta.
cli = app.get_cliente("lavadero")
r = c.get(f"/panel/{cli['panel_token']}")
chequear("el panel del cliente carga", r.status_code == 200, r.status_code)
chequear("y NO expone la clave ni un link al admin",
         K.encode() not in r.data and b"/admin/" not in r.data)

print("\n" + ("TODO OK" if not fallas else f"FALLARON {len(fallas)}: {fallas}"))
sys.exit(1 if fallas else 0)
