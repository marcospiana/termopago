# PROMPT — Nueva estación "INFLADO" (TermoPago)

> Copiá TODO este texto en una conversación nueva de Claude para armar la estación **inflado**.
> Está pensado para que Claude arranque con TODO el contexto del proyecto, sin repetir errores ya resueltos.

---

## Quién soy y qué quiero

Soy Marcos. Tengo un sistema propio llamado **TermoPago**: máquinas de autoservicio que se activan cuando el cliente **paga con QR de MercadoPago**. Un ESP32 lee el pago contra mi backend y acciona la máquina.

Quiero armar una **nueva estación llamada "inflado"** (máquina para **inflar neumáticos**), heredando **todas las mejoras** que ya venimos aplicando en las estaciones 01 y 02, pero con **dos decisiones nuevas** para esta:

1. **Arranco con MQTT push desde el día uno** (no HTTPS-polling). Voy a probar el equipo **una semana en el taller** antes de instalarlo, así tengo tiempo de estrenar y validar MQTT sin apuro.
2. **"inflado" es un SOLO pulso de relé**, no un servicio sostenido. Ver más abajo.

**Importante:**
- Ya pago **Railway** (plan Hobby, con tarjeta cargada). **Seguimos en Railway.**
- Hablame en español, criollo y directo.
- Programo con **Arduino IDE**. Subo a **GitHub por CMD** y **Railway auto-deploya** desde `main`.

---

## Cómo funciona "inflado" (lo que la hace distinta a las otras)

La máquina de inflado **arranca sola con su propio ciclo interno**. Yo solo tengo que darle un **pulso**: el relé cierra momentáneamente una **señal de 12V continua a GND** (emula el botón de arranque). Después de ese pulso, **la máquina se maneja sola**.

Consecuencia clave: **NO necesito arquitectura maestro-esclavo acá.** El esquema maestro-esclavo existía para que un servicio pago (aspiradora/soplador) no se corte si el ESP pierde WiFi o se reinicia — porque el ESP sostenía el relé durante todo el servicio. Pero en "inflado", **una vez que el pulso salió, que se caiga el WiFi no corta nada**: la máquina ya está andando por su cuenta.

Por eso **"inflado" = UN SOLO ESP32** (sin esclavo). Más simple y suficiente.

- **1 solo canal / 1 relé**, pulso momentáneo (a confirmar duración, ej. **500 ms**).
- El relé cierra la **señal 12V DC → GND** para disparar el arranque de la máquina.
- Opcional: display 16x2 mostrando "Inflando..." + un conteo estético del ciclo (el ESP no controla el ciclo, solo lo muestra; si se cae el WiFi la máquina sigue igual).

**Sobre el pico de la bobina (el problema físico de las otras estaciones):** acá es **mucho menor**, porque no conmuto una bobina de contactor en 220 VAC, sino una **señal de bajo consumo en 12 VDC**. Aun así, si esa línea de 12V dispara internamente un relé/optoacoplador con algo de inductancia, conviene **un diodo flyback (ej. 1N4007) en paralelo a la carga** o un pequeño snubber RC como precaución. No es el drama que era en 220 VAC.

---

## Comunicación: MQTT push (así lo quiero armar)

En vez de que el ESP pregunte por HTTPS cada pocos segundos (lo que fragmenta la RAM y colgaba a los ESP32), quiero **MQTT**: una sola conexión persistente y liviana.

**Flujo:**
- El **backend (Railway)**, cuando MercadoPago confirma el pago (webhook), **publica** un mensaje de "activar" al topic del equipo.
- El **ESP32** está suscrito y recibe la orden **al instante** → da el pulso.
- **Keep-alive + LWT (Last Will & Testament):** el broker sabe al instante si el equipo se cae. El LWT deja "inflado offline" publicado si el ESP se desconecta de golpe.
- **Heartbeat cada 60 s** (un publish con uptime, RSSI, RAM, estado) para monitoreo en vivo — sin reabrir HTTPS.

**Diseño sugerido de topics (a confirmar):**
- `termopago/inflado/cmd`    → backend → equipo: orden de activar (con id de pago para deduplicar).
- `termopago/inflado/status` → equipo → backend: heartbeat / estado. LWT publica "offline" acá.

**Broker:** definir entre **HiveMQ Cloud (free tier, TLS 8883)** o **Mosquitto** propio / en Railway. Recomendame cuál conviene para empezar.

**Backend (Flask):** agregar cliente MQTT (ej. `paho-mqtt`). Solo necesito **publicar** desde el handler del webhook de pago (conectar/publicar es simple; ojo con múltiples workers de gunicorn si algún día quiero suscribir, pero para publicar no es problema).

**ESP32:** librería MQTT (PubSubClient o arduino-mqtt) sobre WiFiClientSecure. Conexión persistente, keepalive 60s, LWT, suscripción a `cmd`, publish de heartbeat cada 60s, **deduplicación por id de pago** (no repetir el mismo pago).

---

## Mejoras ya aplicadas en 01/02 (heredarlas TODAS en "inflado")

**Confiabilidad del ESP32:**
1. **Watchdog que SÍ reinicia:** `esp_task_wdt_reconfigure` con `trigger_panic = true` (con guarda `__has_include` / versión de core). ⚠️ **NO** usar el watchdog por hardware-timer viejo (causaba reinicios cada 90s: 833 reinicios). Ese se **eliminó**.
2. **Reinicio universal por "sin comunicación":** con MQTT, si se pierde la conexión al broker y no reconecta en ~60s, reintentar / reiniciar. (En HTTPS era: 60s sin poll OK → `ESP.restart()`.)
3. **Reinicio preventivo cada 6 h en reposo** (`millis()-bootMs > 21600000UL`) para limpiar RAM. Con MQTT es menos necesario, pero lo dejo como red de seguridad.
4. **Reconexión WiFi sin portal** (sin "flapping"): si pierde WiFi, muestra mensaje, `delay`, y sigue reintentando. WiFiManager con portal `TermoPago-Inflado`, ventana de 5s post-boot con botón BOOT para resetear credenciales, auto-reconnect.
5. **BSSID lock opcional** para redes doble banda / band-steering: `#define LOCK_ON 0` por defecto (0 = portátil, se conecta a cualquier AP del SSID). Útil porque lo pruebo en taller y después lo mudo.
6. **`reportarBoot()` con 5 reintentos** al endpoint `/boot/<id>/<motivo>` (registra motivo de reinicio con `esp_reset_reason`).
7. Chequeo de heap libre.

**Display (si lo pongo):**
- LCD I2C 16x2 (hd44780), con **recuperación física del bus I2C** (9 pulsos), `Wire.setTimeOut(50)`, reintento de init (12x), reinit periódico cada 5 min, y reinit diferido 250ms tras conmutar el relé.

**Backend:**
- Flask + gunicorn en **Railway**, PostgreSQL. **MercadoPago Orders API v1** con QR estático por caja (expiración PT15M re-armada), webhooks, `ahora_ar()` en UTC-3. Endpoints `/reinicios` y `/boot/<id>/<motivo>`.

---

## Seguridad (CRÍTICO — el repo es PÚBLICO)

- **Nunca** poner en el código: **Access Token de MercadoPago**, `CLAVE_SECRETA`, ni credenciales del **broker MQTT**. Todo va **solo en variables de entorno de Railway**.
- Archivos `*_privado.txt` en `.gitignore`, nunca se commitean.
- El **BSSID (MAC)** no es secreto.
- Para compartir código uso versión saneada con placeholders (`TU-BACKEND`, `TU_ID`, etc.).

---

## Detalles a confirmar de "inflado"

- **ID del canal / caja:** ej. `inflado01` (confirmame el nombre).
- **Duración del pulso:** ej. 500 ms (confirmame).
- **Segundos/precio del servicio** de inflado (para el ciclo estético del display y el alta en el backend).
- **Esclavo:** NO (single ESP32) — salvo que me convenzas de lo contrario.
- **Display:** sí/no (decime si quiero pantalla o solo el pulso).

---

## Qué quiero que hagas al arrancar la conversación nueva

1. Confirmá que entendés: **single ESP32 + MQTT push + pulso de relé**, heredando las mejoras de 01/02.
2. Hacé**me las preguntas mínimas** que falten (nombre del ID, duración del pulso, precio/segundos, si va display).
3. Recomendame el **broker MQTT** para empezar (HiveMQ Cloud free vs Mosquitto).
4. Generá:
   - El **firmware ESP32** de "inflado" (WiFi + MQTT persistente + LWT + heartbeat + pulso + dedup + todas las mejoras de confiabilidad).
   - Los **cambios en el backend** (cliente MQTT que publica al confirmar el pago + alta de la nueva caja/ID).
5. Dame las instrucciones de armado: pinout del relé, el **diodo flyback** en la línea de 12V como precaución, y la fuente 5V del ESP.
6. Recordá que **lo pruebo una semana en taller** antes de instalar: dejame un modo/checklist de prueba (simular pago, ver el heartbeat, cortar WiFi y confirmar que reconecta).

---

### Recordatorio de restricciones
- No buscar formas de saltear límites de fetch web.
- Tokens, `CLAVE_SECRETA` y credenciales del broker **solo** en variables de entorno de Railway, jamás en el código (repo público).
