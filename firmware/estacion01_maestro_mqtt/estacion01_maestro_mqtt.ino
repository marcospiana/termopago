// TermoPago - ESTACION 01 - MAESTRO (ESP32)  [MQTT push, SIN polling]
// =====================================================================
// Version MQTT del maestro. Reemplaza el polling HTTPS cada 4s (que
// fragmentaba la RAM y colgaba el ESP) por una conexion MQTT persistente.
//
// Maneja DOS canales: aspiradora01 (canal 0) y soplado01 (canal 1).
// Cuando entra un pago, el backend publica al topic del canal y este ESP
// le ordena al ESCLAVO (Arduino Nano) activar el canal por X segundos.
// El ESCLAVO maneja el RELE, el TIEMPO y el CONTADOR -> el servicio sigue
// aunque el maestro pierda WiFi o se reinicie. El maestro NO tiene reles ni LCD.
//
// HTTP puntual (NO fragmenta: el problema era el handshake CADA 4s):
//   - al bootear: GET /orden/<id> por canal, para retomar un servicio que
//     quedo a mitad por un corte de luz (el /orden devuelve el restante).
//   - al terminar un servicio: GET /completar/<id>.
//   - al arrancar: GET /boot/<id>/<motivo>.
//
// CONEXIONES (igual que el maestro viejo):
//   Enlace al esclavo Nano:  GPIO17 (TX2) -> Nano D3 (RX)   + GND comun
//   BOOT (GPIO0): ventana de 5s al encender para resetear el WiFi
//
// SECRETOS: copiar secretos_privado.h (el mismo de inflado) a esta carpeta.
//   Define MQTT_HOST/PORT/USER/PASS y BACKEND_HOST.
// =====================================================================

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <esp_task_wdt.h>
#include <esp_system.h>
#include "secretos_privado.h"   // MQTT_HOST/PORT/USER/PASS, BACKEND_HOST

// ── Canales de esta estacion ──────────────────────────────────────
const int   NUM_CANALES = 2;
const char* IDS[NUM_CANALES]  = {"aspiradora01", "soplado01"};
const char* ETIQ[NUM_CANALES] = {"Aspirad", "Soplado"};

// topics MQTT por canal
String TOPIC_CMD(int i)    { return String("termopago/") + IDS[i] + "/cmd"; }
String TOPIC_STATUS(int i) { return String("termopago/") + IDS[i] + "/status"; }

// ── Enlace UART al esclavo (un solo sentido) ──────────────────────
#define LINK_RX 16   // sin uso
#define LINK_TX 17   // -> RX del Nano (D3)

// ── BSSID lock opcional (band-steering 2.4/5GHz) ──────────────────
#define LOCK_ON 0
uint8_t LOCK_BSSID[6] = {0x00,0x00,0x00,0x00,0x00,0x00};
int32_t LOCK_CHANNEL  = 1;

// ── Tiempos ───────────────────────────────────────────────────────
const int      WDT_TIMEOUT_S    = 30;
const uint32_t HEARTBEAT_MS     = 60000UL;
const uint32_t MQTT_KEEPALIVE_S = 60;
const uint32_t MQTT_RETRY_MS    = 5000UL;
const uint32_t SIN_COMM_TIMEOUT = 90000UL;    // sin MQTT > 90s (en reposo) -> reinicio
const uint32_t REINICIO_PREVENT = 21600000UL; // 6h en reposo -> reinicio limpio

// ── Estado ────────────────────────────────────────────────────────
WiFiClientSecure espClient;
PubSubClient     mqtt(espClient);

bool          activo[NUM_CANALES]  = {false, false};
unsigned long finMs[NUM_CANALES]   = {0, 0};
String        ordenId[NUM_CANALES] = {"", ""};
String        pendienteCompletar[NUM_CANALES] = {"", ""};

// cola por canal: si entra un pago mientras el canal esta ocupado, espera turno
#define MAX_COLA 3
String        colaOrden[NUM_CANALES][MAX_COLA];
int           colaSeg[NUM_CANALES][MAX_COLA];
int           colaLen[NUM_CANALES] = {0, 0};

uint32_t bootMs = 0, ultimoHeartbeat = 0, ultimoMqttOk = 0, ultimoIntentoMqtt = 0, ultimoLcdMs = 0;
bool     wdtAdded = false;

// ─── Enlace al esclavo por UART (protocolo intacto) ───────────────
void mostrar(String l1, String l2) {
  while (l1.length() < 16) l1 += " ";  l1 = l1.substring(0, 16);
  while (l2.length() < 16) l2 += " ";  l2 = l2.substring(0, 16);
  Serial2.print('1'); Serial2.print(l1); Serial2.print('\n');
  Serial2.print('2'); Serial2.print(l2); Serial2.print('\n');
}
void enviarActivar(int canal, int segundos, String orden) {
  Serial2.print("A,"); Serial2.print(canal); Serial2.print(",");
  Serial2.print(segundos); Serial2.print(","); Serial2.print(orden);
  Serial2.print('\n');
  Serial.printf("-> esclavo: A,%d,%d,%s\n", canal, segundos, orden.c_str());
}

// ─── Watchdog (esp_task_wdt con trigger_panic) ────────────────────
void watchdogInit() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_config_t cfg = { .timeout_ms = (uint32_t)WDT_TIMEOUT_S*1000, .idle_core_mask = 0, .trigger_panic = true };
  if (esp_task_wdt_reconfigure(&cfg) != ESP_OK) esp_task_wdt_init(&cfg);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  // el loop se suscribe recien tras conectar el WiFi (el portal puede tardar)
}
void watchdogAddTask() { esp_task_wdt_add(NULL); wdtAdded = true; }
void watchdogFeed()    { if (wdtAdded) esp_task_wdt_reset(); }

// ─── Causa del reinicio ───────────────────────────────────────────
String motivoReinicio() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:   return "corte-luz";
    case ESP_RST_SW:        return "software";
    case ESP_RST_PANIC:     return "panic-crash";
    case ESP_RST_INT_WDT:   return "wdt-interrupt";
    case ESP_RST_TASK_WDT:  return "wdt-task";
    case ESP_RST_WDT:       return "wdt-otro";
    case ESP_RST_BROWNOUT:  return "brownout-elec";
    default:                return "desconocido";
  }
}

// ─── HTTP puntual: boot / completar / resume ──────────────────────
bool httpGet(const String& url, String* body = nullptr) {
  if (WiFi.status() != WL_CONNECTED) return false;
  WiFiClientSecure c; c.setInsecure();
  HTTPClient http; http.setConnectTimeout(5000); http.setTimeout(6000);
  if (!http.begin(c, url)) return false;
  int code = http.GET();
  if (body && code == 200) *body = http.getString();
  http.end();
  return code == 200;
}

void reportarBoot() {
  String motivo = motivoReinicio();
  for (int i = 0; i < 5; i++) {
    watchdogFeed();
    if (httpGet(String("https://") + BACKEND_HOST + "/boot/" + IDS[0] + "/" + motivo)) {
      Serial.println("[BOOT] reportado: " + motivo); return;
    }
    delay(1500);
  }
}

void completarOrden(const String& id) {
  if (id == "") return;
  for (int i = 0; i < 3; i++) {
    watchdogFeed();
    if (httpGet(String("https://") + BACKEND_HOST + "/completar/" + id)) {
      Serial.println("[COMPLETAR] ok: " + id); return;
    }
    delay(1500);
  }
  Serial.println("[COMPLETAR] fallo (el backend la expira solo): " + id);
}

// Al bootear: retomar un servicio que quedo a mitad (corte de luz).
// Reusa /orden, que devuelve el restante de una orden 'ejecutando'. El esclavo
// deduplica por orden: si el canal sigue corriendo, ignora; si hubo corte de
// luz total, lo retoma con el restante.
void resumeCanal(int i) {
  String body;
  if (!httpGet(String("https://") + BACKEND_HOST + "/orden/" + IDS[i], &body)) return;
  StaticJsonDocument<300> doc;
  if (deserializeJson(doc, body)) return;
  if (doc["encender"] == true) {
    int seg = doc["segundos"] | 0;
    String orden = String((const char*)(doc["orden_id"] | ""));
    if (seg > 5) {
      activo[i] = true;
      finMs[i] = millis() + (unsigned long)seg * 1000UL;
      ordenId[i] = orden;
      enviarActivar(i, seg, orden);
      Serial.printf("[RESUME] %s retomado %ds\n", IDS[i], seg);
    }
  }
}

// ─── Activar un canal (o encolar si esta ocupado) ─────────────────
void activarCanal(int canal, int seg, const String& orden) {
  if (canal < 0 || canal >= NUM_CANALES) return;
  // dedup: mismo pago en curso o ya en cola
  if (orden == ordenId[canal] && activo[canal]) return;
  for (int k = 0; k < colaLen[canal]; k++) if (colaOrden[canal][k] == orden) return;

  if (activo[canal]) {                     // ocupado: a la cola
    if (colaLen[canal] < MAX_COLA) {
      colaOrden[canal][colaLen[canal]] = orden;
      colaSeg[canal][colaLen[canal]]   = seg;
      colaLen[canal]++;
      Serial.printf("[COLA] %s encolado en canal %d. En cola: %d\n", orden.c_str(), canal, colaLen[canal]);
    }
    return;
  }
  // libre: activar ya
  activo[canal]   = true;
  finMs[canal]    = millis() + (unsigned long)seg * 1000UL;
  ordenId[canal]  = orden;
  enviarActivar(canal, seg, orden);
}

// ─── MQTT ─────────────────────────────────────────────────────────
String heartbeatJson(const char* caja, const char* estado) {
  StaticJsonDocument<200> doc;
  doc["caja"] = caja; doc["estado"] = estado;
  doc["uptime_s"] = (millis() - bootMs) / 1000;
  doc["rssi"] = WiFi.RSSI(); doc["heap"] = ESP.getFreeHeap();
  doc["a0"] = activo[0]; doc["a1"] = activo[1];
  String out; serializeJson(doc, out); return out;
}

void publicarEstado(const char* estado) {
  if (!mqtt.connected()) return;
  for (int i = 0; i < NUM_CANALES; i++)
    mqtt.publish(TOPIC_STATUS(i).c_str(), heartbeatJson(IDS[i], estado).c_str(), true);
}

void mqttCallback(char* topic, byte* payload, unsigned int len) {
  String msg; msg.reserve(len);
  for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];
  Serial.printf("[MQTT] %s : %s\n", topic, msg.c_str());

  // que canal es este topic
  int canal = -1;
  for (int i = 0; i < NUM_CANALES; i++) if (TOPIC_CMD(i) == String(topic)) { canal = i; break; }
  if (canal < 0) return;

  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, msg)) return;
  if (strcmp(doc["accion"] | "", "activar") != 0) return;
  String orden = String((const char*)(doc["pago_id"] | ""));
  int seg = doc["segundos"] | 0;
  if (orden.length() == 0 || seg <= 0) return;

  activarCanal(canal, seg, orden);
}

bool mqttConectar() {
  String clientId = "termopago-estacion01";
  // LWT en el canal 0 (una conexion = un LWT). El canal 1 usa el respaldo de
  // vencimiento del QR (15 min) si el equipo se cae de golpe.
  String willMsg = heartbeatJson(IDS[0], "offline");
  bool ok = mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS,
                         TOPIC_STATUS(0).c_str(), 1, true, willMsg.c_str(), false);
  if (ok) {
    Serial.println("[MQTT] conectado");
    for (int i = 0; i < NUM_CANALES; i++) mqtt.subscribe(TOPIC_CMD(i).c_str(), 1);
    publicarEstado("online");
    ultimoMqttOk = millis();
  } else {
    Serial.printf("[MQTT] fallo rc=%d\n", mqtt.state());
  }
  return ok;
}

void asegurarMqtt() {
  if (mqtt.connected()) { ultimoMqttOk = millis(); return; }
  if (millis() - ultimoIntentoMqtt < MQTT_RETRY_MS) return;
  ultimoIntentoMqtt = millis();
  mqttConectar();
}

// ─── WiFi ─────────────────────────────────────────────────────────
void conectarWiFi() {
  WiFi.mode(WIFI_STA); WiFi.setSleep(false);
  WiFi.setAutoReconnect(true); WiFi.persistent(true);
  WiFiManager wm;
  String red = wm.getWiFiSSID();
  mostrar("Conectando WiFi", "Por favor espere");
  if (red.length() > 0) {
    unsigned long t0 = millis();
#if LOCK_ON
    WiFi.begin(red.c_str(), wm.getWiFiPass().c_str(), LOCK_CHANNEL, LOCK_BSSID);
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 18000) delay(250);
    if (WiFi.status() != WL_CONNECTED) { WiFi.begin(); t0 = millis();
      while (WiFi.status() != WL_CONNECTED && millis() - t0 < 12000) delay(250); }
#else
    WiFi.begin();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 25000) delay(250);
#endif
    if (WiFi.status() != WL_CONNECTED) { mostrar("Sin WiFi", "Reintentando..."); delay(1000); ESP.restart(); }
  } else {
    wm.setConfigPortalTimeout(180);
    if (!wm.autoConnect("TermoPago-Est01")) { delay(1000); ESP.restart(); }
  }
  Serial.println("[WiFi] " + WiFi.localIP().toString());
}

void ventanaResetWiFi() {
  pinMode(0, INPUT_PULLUP);
  mostrar("Cambiar WiFi?", "Apriete BOOT 5s");
  unsigned long t0 = millis();
  while (millis() - t0 < 5000) {
    if (digitalRead(0) == LOW) {
      unsigned long t1 = millis();
      while (digitalRead(0) == LOW && millis() - t1 < 1000) delay(50);
      if (millis() - t1 >= 1000) { WiFiManager wm; wm.resetSettings();
        mostrar("WiFi borrado", "Configure de nuevo"); delay(1500); ESP.restart(); }
    }
    delay(50);
  }
}

// ─── Setup ────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Serial2.begin(9600, SERIAL_8N1, LINK_RX, LINK_TX);   // enlace al esclavo Nano
  mostrar("Iniciando...", "Por favor espere");

  watchdogInit();
  ventanaResetWiFi();
  conectarWiFi();
  watchdogAddTask();        // recien ahora vigilamos el loop
  reportarBoot();

  // MQTT sobre TLS
  espClient.setInsecure();  // TALLER: sin pin de CA. Produccion: setCACert(ISRG Root X1).
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(MQTT_KEEPALIVE_S);
  mqtt.setBufferSize(512);
  mqtt.setCallback(mqttCallback);
  mqttConectar();

  // retomar servicios que quedaron a mitad por corte de luz
  for (int i = 0; i < NUM_CANALES; i++) resumeCanal(i);

  bootMs = millis();
  ultimoHeartbeat = millis();
}

// ─── Loop ─────────────────────────────────────────────────────────
void loop() {
  watchdogFeed();

  bool enServicio = activo[0] || activo[1];

  // Red de seguridad: reinicios SOLO en reposo (un reinicio del maestro no
  // corta el servicio -> el esclavo sostiene el rele; pero igual esperamos a
  // que termine para no perder el display ni la cola).
  if (!enServicio) {
    if (millis() - bootMs > SIN_COMM_TIMEOUT && millis() - ultimoMqttOk > SIN_COMM_TIMEOUT) {
      Serial.println("[SIN COMM] > 90s sin broker -> reinicio");
      mostrar("Reconectando", "Reiniciando..."); delay(300); ESP.restart();
    }
    if (ESP.getFreeHeap() < 40000) { Serial.println("[HEAP] bajo -> reinicio"); delay(200); ESP.restart(); }
    if (millis() - bootMs > REINICIO_PREVENT) {
      Serial.println("[PREVENTIVO] 6h -> reinicio"); mostrar("Mantenimiento", "Reiniciando..."); delay(300); ESP.restart();
    }
  }

  // WiFi caido: avisar y esperar auto-reconnect (el reinicio lo cubre el de arriba)
  if (WiFi.status() != WL_CONNECTED) { mostrar("WiFi perdido", "Reconectando..."); delay(200); return; }

  asegurarMqtt();
  mqtt.loop();

  // Timer paralelo: marca fin de servicio y agenda /completar (el rele lo apaga
  // el esclavo por su cuenta). Luego, si hay cola, activa el siguiente.
  for (int i = 0; i < NUM_CANALES; i++) {
    if (activo[i] && (long)(finMs[i] - millis()) <= 0) {
      activo[i] = false;
      pendienteCompletar[i] = ordenId[i];
      ordenId[i] = "";
      Serial.printf("[FIN] canal %d\n", i);
    }
    // activar el proximo de la cola cuando el canal quedo libre
    if (!activo[i] && colaLen[i] > 0) {
      String o = colaOrden[i][0]; int s = colaSeg[i][0];
      for (int k = 1; k < colaLen[i]; k++) { colaOrden[i][k-1] = colaOrden[i][k]; colaSeg[i][k-1] = colaSeg[i][k]; }
      colaLen[i]--;
      activarCanal(i, s, o);
    }
  }

  // Confirmar ordenes terminadas (HTTP puntual)
  for (int i = 0; i < NUM_CANALES; i++)
    if (pendienteCompletar[i] != "") { completarOrden(pendienteCompletar[i]); pendienteCompletar[i] = ""; }

  // Heartbeat cada 60s (los dos canales)
  if (millis() - ultimoHeartbeat >= HEARTBEAT_MS) { ultimoHeartbeat = millis(); publicarEstado("online"); }

  // Display de reposo cada 1s (el esclavo dibuja su propio contador en servicio)
  if (millis() - ultimoLcdMs >= 1000) {
    ultimoLcdMs = millis();
    if (!activo[0] && !activo[1])
      mostrar(mqtt.connected() ? "WiFi conectado!" : "Conectando...", mqtt.connected() ? "Escanee el QR" : "al servidor...");
  }

  delay(20);
}
