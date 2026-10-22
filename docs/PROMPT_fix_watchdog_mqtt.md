# PROMPT — Aplicar fix anti-task_wdt (MQTT) a todos los firmwares

> Pegá este texto en la conversación/página generadora para que actualice los demás equipos.

## Contexto del problema

En los firmwares MQTT de TermoPago hay un **empate de timeouts** que dispara reinicios por watchdog (`task_wdt`) en reconexiones lentas al broker:

- El watchdog está en `WDT_TIMEOUT_S = 15`.
- `PubSubClient` tiene por defecto `MQTT_SOCKET_TIMEOUT = 15` segundos.
- `mqtt.connect()` es **bloqueante y NO alimenta el watchdog**. Cuando el broker (HiveMQ) o el handshake TLS vienen lentos —típico en una reconexión con señal floja o un hipo del broker— `connect()` se cuelga hasta ~15s sin tocar `esp_task_wdt_reset()`. Como el watchdog también vence a los 15s, **están empatados** → salta `task_wdt` y reinicia.

Síntoma real observado en villagas01 (expendedora de fichas): tras su reinicio preventivo de 6h, tormenta de reinicios `task_wdt` con uptimes de 19-50 s durante ~3 minutos, hasta que el broker se normalizó.

## El fix (ya aplicado en termopago_fichas.ino)

Dos cambios que garantizan que `connect()` vuelva **antes** de que venza el watchdog:

1. **Acortar el timeout del socket MQTT** para que `connect()` no bloquee más de 8s. En `setup()`, junto a `setKeepAlive`:
   ```cpp
   mqtt.setSocketTimeout(8);   // connect()/socket no pueden colgarse mas de 8s
   ```

2. **Subir el watchdog** para que tenga margen sobre ese connect de 8s:
   ```cpp
   const uint32_t WDT_TIMEOUT_S = 30;   // era 15
   ```

Resultado: el peor caso de un `connect()` lento (8s) queda muy por debajo de los 30s del watchdog. El perro sigue cazando cuelgues reales, pero una reconexión lenta ya no lo dispara.

## Qué quiero que hagas

Aplicá **exactamente estos dos cambios** a los siguientes firmwares (son los que usan MQTT + PubSubClient):

- `firmware/inflado01/inflado01.ino`
- `firmware/estacion01_maestro_mqtt/estacion01_maestro_mqtt.ino`
- (y cualquier otro sketch MQTT nuevo que se genere desde ahora: dejalo ya con `setSocketTimeout(8)` y `WDT_TIMEOUT_S = 30`)

Reglas:
- No toques nada más de la lógica de esos sketches.
- Si en algún sketch el nombre de la constante del watchdog o la variable `mqtt` difiere, adaptá el cambio pero manteniendo la intención: **socket timeout 8s** y **watchdog ≥ 2.5x** ese valor (30s).
- El `WDT_TIMEOUT_S` del **esclavo Arduino Nano** NO se toca (el Nano no hace MQTT; su timeout de "Sin datos" de 20s es otra cosa).
- Es cambio de **firmware**: no va a git ni a Railway, solo se reflashea cada equipo.

## Verificación

Después de reflashear, mirar en `/reinicios` que no vuelvan a aparecer ráfagas de `task_wdt` con uptimes de pocos segundos. Un `sw_restart` aislado con "6h 0min" es el reinicio preventivo normal, no es problema.
