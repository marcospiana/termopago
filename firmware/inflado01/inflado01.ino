/*  ============================================================================
    TermoPago — Estacion "INFLADO01"  (infladora de neumaticos)
    ----------------------------------------------------------------------------
    Un solo ESP32 (SIN esclavo). MQTT push sobre TLS.
    El rele da UN pulso momentaneo (300 ms) que cierra la senal de 12 VDC contra
    GND, emulando el boton de arranque. Despues del pulso, la maquina corre su
    ciclo interno sola: si se cae el WiFi, NO se corta nada.

    Hereda todas las mejoras de confiabilidad de las estaciones 01/02:
      - Watchdog esp_task_wdt con trigger_panic = true (reinicia de verdad).
      - Reinicio por "sin comunicacion" adaptado a MQTT.
      - Reinicio preventivo cada 6 h en reposo.
      - Reconexion WiFi sin portal/flapping + WiFiManager portal "TermoPago-Inflado".
      - Ventana 5 s post-boot con boton BOOT para resetear credenciales.
      - BSSID lock opcional (LOCK_ON) para redes doble banda.
      - reportarBoot() con reintentos al backend (registra motivo de reinicio).
      - Chequeo de heap libre.
      - LCD 16x2 I2C robusto (recuperacion de bus, reinit periodico, reinit
        diferido tras conmutar el rele).

    SEGURIDAD (repo PUBLICO): las credenciales del broker MQTT y la URL del
    backend NO van en este archivo. Van en "secretos_privado.h" (en .gitignore).
    Copiá "secretos_privado.h.ejemplo" -> "secretos_privado.h" y completalo.

    Programado en Arduino IDE. Core: ESP32 (Espressif) 2.x o 3.x.
    Librerias necesarias (Gestor de librerias):
      - WiFiManager (tzapu)
      - PubSubClient (Nick O'Leary)
      - ArduinoJson (Benoit Blanchon) v6 o v7
      - hd44780 (Bill Perry)   -> para el LCD I2C
    ============================================================================ */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>          // tzapu
#include <PubSubClient.h>         // Nick O'Leary
#include <ArduinoJson.h>          // Benoit Blanchon
#include <Preferences.h>          // NVS (dedup persistente)
#include <HTTPClient.h>
#include <esp_task_wdt.h>
#include <esp_system.h>

// ---- LCD 16x2 I2C (hd44780) ----
#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>

// ---- Secretos (NO se commitea) ----
#include "secretos_privado.h"
#include <time.h>              // configTime/time() para validar el cert TLS
#include "ca_hivemq.h"        // raices CA de Lets Encrypt (ISRG X1/X2)
/*  secretos_privado.h debe definir:
      #define MQTT_HOST     "xxxxx.s1.eu.hivemq.cloud"
      #define MQTT_PORT     8883
      #define MQTT_USER     "usuario_del_broker"
      #define MQTT_PASS     "password_del_broker"
      #define BACKEND_HOST  "tu-backend.up.railway.app"   // sin https://
*/

// ============================================================================
//  CONFIGURACION DE ESTA CAJA
// ============================================================================
#define CAJA_ID           "inflado01"

// Topics MQTT
#define TOPIC_CMD         "termopago/" CAJA_ID "/cmd"      // backend -> equipo
#define TOPIC_STATUS      "termopago/" CAJA_ID "/status"   // equipo  -> backend (+ LWT)

// ---- Parametros del servicio (COMPLETAR cuando los definas) ----
#define PRECIO_ARS        0        // TODO: precio del inflado (solo informativo en display/log)
#define CICLO_SEGUNDOS    90       // TODO: duracion del ciclo interno de la maquina (SOLO estetico en el display)

// ---- Pulso de rele ----
#define RELE_PIN          26       // GPIO al modulo rele (evitar pines strapping)
#define PULSO_MS          300      // duracion del pulso (confirmado)
#define RELE_ACTIVO_BAJO  1        // 1 = modulo rele activo en BAJO (los tipicos azules). 0 = activo en ALTO.

// ---- Boton BOOT (reset de credenciales) ----
#define BOOT_PIN          0        // GPIO0 = boton BOOT del devkit

// ---- LCD ----
#define USAR_LCD          1        // 1 = con LCD 16x2. 0 = sin display.
#define LCD_COLS          16
#define LCD_ROWS          2

// ---- BSSID lock (redes doble banda / band-steering) ----
#define LOCK_ON           0        // 0 = portatil (cualquier AP del SSID). 1 = fija al BSSID de abajo.
uint8_t BSSID_FIJO[6] = { 0x00,0x00,0x00,0x00,0x00,0x00 }; // completar si LOCK_ON=1 (la MAC no es secreta)

// ---- Tiempos de confiabilidad ----
const uint32_t HEARTBEAT_MS       = 60000UL;     // publish de estado cada 60 s
const uint32_t MQTT_KEEPALIVE_S   = 60;          // keepalive MQTT (LWT dispara a ~1.5x)
const uint32_t MQTT_RETRY_MS      = 5000UL;      // reintento de conexion al broker
const uint32_t SIN_COMM_TIMEOUT   = 90000UL;     // sin conexion MQTT > 90 s -> reinicio
const uint32_t REINICIO_PREVENT   = 21600000UL;  // 6 h en reposo -> reinicio preventivo
const uint32_t LCD_REINIT_MS      = 300000UL;    // reinit LCD cada 5 min
const uint32_t WDT_TIMEOUT_S      = 15;          // watchdog

// ============================================================================
//  ESTADO GLOBAL
// ============================================================================
WiFiClientSecure  espClient;
PubSubClient      mqtt(espClient);
Preferences       prefs;
#if USAR_LCD
hd44780_I2Cexp    lcd;
bool  lcdOk = false;
#endif

uint32_t bootMs         = 0;
uint32_t ultimoHeartbeat= 0;
uint32_t ultimoMqttOk   = 0;      // ultima vez que el broker estuvo conectado
uint32_t ultimoIntentoMqtt = 0;
uint32_t ultimoLcdReinit= 0;
uint32_t pulsosTotales  = 0;

String   ultimoPagoProcesado = "";   // dedup (se persiste en NVS): ultimo pago que ARRANCO

// Cola de pagos: si entra un pago mientras hay un servicio en curso, espera su
// turno y se activa al terminar (asi no se pierden servicios).
#define  MAX_COLA 5
String   colaPagos[MAX_COLA];
int      colaSegundos[MAX_COLA];
int      colaLen = 0;

// display de conteo estetico
bool     mostrandoCiclo = false;
uint32_t cicloInicioMs  = 0;
int      ultimoRestanteMostrado = -1;   // para refrescar el LCD solo al cambiar el segundo
uint32_t graciasHastaMs = 0;            // hasta cuando mostrar "Listo! Gracias" antes de volver a espera
const uint32_t GRACIAS_MS = 5000;       // 5 s el mensaje de gracias tras el servicio
int      segundosPendiente = 0;         // segundos del conteo que mando el backend en el cmd
int      cicloSegundos = CICLO_SEGUNDOS;// conteo actual (del server si vino, o el default)

// ============================================================================
//  LCD helpers (robustos)
// ============================================================================
#if USAR_LCD
void lcdRecuperarBus() {
  // 9 pulsos de reloj para liberar un esclavo I2C colgado
  pinMode(SCL, OUTPUT);
  for (int i = 0; i < 9; i++) { digitalWrite(SCL, HIGH); delayMicroseconds(5);
                                digitalWrite(SCL, LOW);  delayMicroseconds(5); }
  pinMode(SCL, INPUT);
}

bool lcdInit() {
  Wire.begin();
  Wire.setTimeOut(50);
  lcdRecuperarBus();
  for (int intento = 0; intento < 12; intento++) {   // hasta 12 reintentos de init
    if (lcd.begin(LCD_COLS, LCD_ROWS) == 0) {
      lcd.clear();
      return true;
    }
    delay(120);
    lcdRecuperarBus();
  }
  return false;
}

void lcdLinea(uint8_t fila, const String& txt) {
  if (!lcdOk) return;
  lcd.setCursor(0, fila);
  String s = txt;
  while (s.length() < LCD_COLS) s += ' ';
  if (s.length() > LCD_COLS) s = s.substring(0, LCD_COLS);
  lcd.print(s);
}
#else
bool lcdInit() { return true; }
void lcdLinea(uint8_t, const String&) {}
#endif

void mostrar(const String& l1, const String& l2) {
  lcdLinea(0, l1);
  lcdLinea(1, l2);
  Serial.printf("[LCD] %s | %s\n", l1.c_str(), l2.c_str());
}

// Pantalla de reposo: lo que ve el cliente cuando el equipo esta listo para
// cobrar. El texto coincide con el cartel ("la pantalla debe decir WiFi conectado").
void pantallaEspera() {
  mostrar("WiFi conectado", "Escanea el QR");
}

// ============================================================================
//  WATCHDOG (esp_task_wdt con trigger_panic = true)
//  Guarda por version del core (API distinta en 2.x vs 3.x)
// ============================================================================
bool wdtTaskAdded = false;   // true cuando el loop ya esta suscripto al watchdog
void watchdogInit() {
#if __has_include("esp_task_wdt.h")
  #if ESP_ARDUINO_VERSION_MAJOR >= 3
    // Core 3.x: config por struct
    esp_task_wdt_config_t cfg = {
      .timeout_ms   = WDT_TIMEOUT_S * 1000,
      .idle_core_mask = 0,
      .trigger_panic = true
    };
    // si ya estaba inicializado por el core, reconfiguramos
    if (esp_task_wdt_reconfigure(&cfg) != ESP_OK) {
      esp_task_wdt_init(&cfg);
    }
    // OJO: NO suscribimos el loop task todavia. El portal de WiFiManager puede
    // tardar minutos y bloquea el loop -> el WDT lo reiniciaria. Se suscribe
    // recien despues de conectar el WiFi, con watchdogAddTask().
  #else
    // Core 2.x: firma (timeout_s, panic)
    esp_task_wdt_init(WDT_TIMEOUT_S, true);   // true = trigger_panic -> reinicia
  #endif
  Serial.println("[WDT] watchdog iniciado (se activa la vigilancia tras el WiFi)");
#endif
}
// Suscribe el loop task al watchdog. Llamar SOLO despues de conectar el WiFi,
// para que el portal de configuracion pueda tardar sin provocar un reinicio.
void watchdogAddTask() {
#if __has_include("esp_task_wdt.h")
  esp_task_wdt_add(NULL);
  wdtTaskAdded = true;
  Serial.println("[WDT] loop vigilado por el watchdog");
#endif
}
void watchdogFeed() {
#if __has_include("esp_task_wdt.h")
  if (wdtTaskAdded) esp_task_wdt_reset();   // solo alimenta si el loop ya esta suscripto
#endif
}

// ============================================================================
//  MOTIVO DE REINICIO
// ============================================================================
String motivoReinicio() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  return "poweron";
    case ESP_RST_SW:       return "sw_restart";
    case ESP_RST_PANIC:    return "panic";
    case ESP_RST_INT_WDT:  return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT:      return "wdt";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_DEEPSLEEP:return "deepsleep";
    default:               return "otro";
  }
}

// reporte al backend por HTTPS (5 reintentos) -> /boot/<id>/<motivo>
void reportarBoot() {
  String motivo = motivoReinicio();
  String url = String("https://") + BACKEND_HOST + "/boot/" + CAJA_ID + "/" + motivo;
  for (int i = 0; i < 5; i++) {
    watchdogFeed();
    if (WiFi.status() != WL_CONNECTED) { delay(1000); continue; }
    WiFiClientSecure c;
    c.setInsecure();                 // reporte de boot: sin pin de CA (no es critico)
    HTTPClient http;
    http.setTimeout(8000);
    if (http.begin(c, url)) {
      int code = http.GET();
      http.end();
      Serial.printf("[BOOT] reporte '%s' intento %d -> %d\n", motivo.c_str(), i+1, code);
      if (code > 0) return;
    }
    delay(1500);
  }
  Serial.println("[BOOT] no se pudo reportar el boot (sigo igual)");
}

// ============================================================================
//  WIFI
// ============================================================================
void ventanaResetCredenciales() {
  // 5 s post-boot: si mantenés BOOT apretado, borra credenciales WiFi
  pinMode(BOOT_PIN, INPUT_PULLUP);
  mostrar("TermoPago " CAJA_ID, "BOOT=reset 5s");
  uint32_t t0 = millis();
  while (millis() - t0 < 5000) {
    watchdogFeed();
    if (digitalRead(BOOT_PIN) == LOW) {
      mostrar("Borrando WiFi...", "reiniciando");
      WiFiManager wm; wm.resetSettings();
      delay(800);
      ESP.restart();
    }
    delay(20);
  }
}

void conectarWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);

#if LOCK_ON
  // fija al BSSID indicado (redes doble banda / band-steering)
  WiFiManager wm;
  wm.setConfigPortalTimeout(180);
  mostrar("Conectando WiFi", "(BSSID fijo)");
  // WiFiManager no toma BSSID directo; usamos credenciales guardadas + WiFi.begin con BSSID
  // Si no hay credenciales, abre portal:
  if (WiFi.SSID().length() == 0) {
    if (!wm.autoConnect("TermoPago-Inflado")) { ESP.restart(); }
  } else {
    WiFi.begin(WiFi.SSID().c_str(), WiFi.psk().c_str(), 0, BSSID_FIJO);
  }
#else
  WiFiManager wm;
  wm.setConfigPortalTimeout(180);
  mostrar("Conectando WiFi", "portal si falla");
  if (!wm.autoConnect("TermoPago-Inflado")) {
    mostrar("Sin WiFi", "reiniciando");
    delay(1500);
    ESP.restart();
  }
#endif

  Serial.printf("[WiFi] conectado a %s  IP %s  RSSI %d\n",
                WiFi.SSID().c_str(), WiFi.localIP().toString().c_str(), WiFi.RSSI());
  mostrar("WiFi OK " CAJA_ID, WiFi.localIP().toString());
}

// reconexion sin flapping: mensaje + delay + reintento (NO reabre portal)
void asegurarWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.println("[WiFi] perdido, reintentando...");
  mostrar("WiFi caido", "reconectando...");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    watchdogFeed();
    WiFi.reconnect();
    delay(3000);
    // si estuvo mucho sin WiFi, el timeout de "sin comunicacion" lo reiniciara
    if (millis() - t0 > SIN_COMM_TIMEOUT) {
      Serial.println("[WiFi] demasiado tiempo sin red -> reinicio");
      ESP.restart();
    }
  }
  mostrar("WiFi OK " CAJA_ID, WiFi.localIP().toString());
}

// ============================================================================
//  MQTT
// ============================================================================
String heartbeatJson(const char* estado) {
  StaticJsonDocument<256> doc;
  doc["caja"]        = CAJA_ID;
  doc["estado"]      = estado;              // "online" / "offline"
  doc["uptime_s"]    = (millis() - bootMs) / 1000;
  doc["rssi"]        = WiFi.RSSI();
  doc["heap"]        = ESP.getFreeHeap();
  doc["pulsos"]      = pulsosTotales;
  doc["ultimo_pago"] = ultimoPagoProcesado;
  String out; serializeJson(doc, out);
  return out;
}

void publicarEstado(const char* estado, bool retained) {
  if (!mqtt.connected()) return;
  String p = heartbeatJson(estado);
  mqtt.publish(TOPIC_STATUS, p.c_str(), retained);
  Serial.printf("[MQTT] status -> %s\n", p.c_str());
}

// encola un pago para activarlo cuando el equipo este libre (con dedup)
void encolarPago(const String& pagoId, int seg) {
  if (pagoId == ultimoPagoProcesado) {                 // ya se sirvio / esta corriendo
    Serial.printf("[COLA] %s ya procesado, ignoro\n", pagoId.c_str());
    return;
  }
  for (int i = 0; i < colaLen; i++)                    // ya esta esperando en la cola
    if (colaPagos[i] == pagoId) {
      Serial.printf("[COLA] %s ya en cola, ignoro\n", pagoId.c_str());
      return;
    }
  if (colaLen >= MAX_COLA) { Serial.println("[COLA] llena, descarto"); return; }
  colaPagos[colaLen]    = pagoId;
  colaSegundos[colaLen] = seg;
  colaLen++;
  Serial.printf("[COLA] encolado %s (%ds). En cola: %d\n", pagoId.c_str(), seg, colaLen);
}

// callback: llega orden de activar
void mqttCallback(char* topic, byte* payload, unsigned int len) {
  String msg; msg.reserve(len);
  for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];
  Serial.printf("[MQTT] %s : %s\n", topic, msg.c_str());

  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, msg)) { Serial.println("[MQTT] JSON invalido"); return; }

  const char* accion = doc["accion"] | "";
  String pagoId = String((const char*)(doc["pago_id"] | ""));
  if (strcmp(accion, "activar") != 0 || pagoId.length() == 0) return;

  // segundos del conteo (editable desde /config del backend); si no viene, usa el default
  int seg = (int)(doc["segundos"] | (int)CICLO_SEGUNDOS);

  // a la cola: si esta inflando, espera su turno; el loop lo activa al quedar libre
  encolarPago(pagoId, seg);
}

boolean mqttConectar() {
  String clientId = String("termopago-") + CAJA_ID;   // estable -> sesion persistente
  String willMsg  = heartbeatJson("offline");
  // connect(id, user, pass, willTopic, willQos, willRetain, willMsg, cleanSession=false)
  bool ok = mqtt.connect(clientId.c_str(), MQTT_USER, MQTT_PASS,
                         TOPIC_STATUS, 1, true, willMsg.c_str(), false);
  if (ok) {
    Serial.println("[MQTT] conectado al broker");
    mqtt.subscribe(TOPIC_CMD, 1);          // QoS 1
    publicarEstado("online", true);        // retenido: el backend ve el ultimo estado
    ultimoMqttOk = millis();
    if (!mostrandoCiclo) pantallaEspera();   // pantalla de reposo para el cliente
  } else {
    Serial.printf("[MQTT] fallo conexion, rc=%d\n", mqtt.state());
  }
  return ok;
}

void asegurarMqtt() {
  if (mqtt.connected()) { ultimoMqttOk = millis(); return; }
  if (millis() - ultimoIntentoMqtt < MQTT_RETRY_MS) return;
  ultimoIntentoMqtt = millis();
  mostrar("Reconectando", "broker MQTT...");
  mqttConectar();
}

// ============================================================================
//  PULSO DE RELE
// ============================================================================
inline void releEscribir(bool activo) {
#if RELE_ACTIVO_BAJO
  digitalWrite(RELE_PIN, activo ? LOW : HIGH);
#else
  digitalWrite(RELE_PIN, activo ? HIGH : LOW);
#endif
}

void darPulso(const String& pagoId) {
  // DEDUP: si ya procesamos este pago, no repetimos
  if (pagoId == ultimoPagoProcesado) {
    Serial.printf("[PULSO] pago %s ya procesado, ignoro\n", pagoId.c_str());
    return;
  }

  Serial.printf("[PULSO] activando por pago %s\n", pagoId.c_str());
  mostrar("Arrancando...", "pago ok");

  releEscribir(true);
  uint32_t t0 = millis();
  while (millis() - t0 < PULSO_MS) { watchdogFeed(); delay(5); }
  releEscribir(false);

  pulsosTotales++;
  ultimoPagoProcesado = pagoId;
  prefs.putString("ultpago", ultimoPagoProcesado);   // persistir dedup en NVS

#if USAR_LCD
  // reinit diferido del LCD 250 ms despues de conmutar el rele (ruido en el bus)
  delay(250);
  if (!lcd.status()) { lcdOk = lcdInit(); }
#endif

  // ack por status + arranca el conteo estetico
  publicarEstado("online", true);
  cicloSegundos  = (segundosPendiente > 0) ? segundosPendiente : CICLO_SEGUNDOS;  // del server o default
  mostrandoCiclo = true;
  cicloInicioMs  = millis();
  ultimoRestanteMostrado = -1;   // fuerza el primer refresco del conteo

  Serial.println("[PULSO] listo. La maquina corre su ciclo interno.");
}

// conteo estetico del ciclo (el ESP NO controla la maquina, solo lo muestra)
void actualizarDisplayCiclo() {
  if (!mostrandoCiclo) return;
  uint32_t transcurrido = (millis() - cicloInicioMs) / 1000;
  if (transcurrido >= (uint32_t)cicloSegundos) {
    mostrandoCiclo = false;
    ultimoRestanteMostrado = -1;
    mostrar("Listo!", "Gracias!");
    graciasHastaMs = millis() + GRACIAS_MS;   // luego vuelve a "Escanea el QR"
    return;
  }
  int restante = cicloSegundos - (int)transcurrido;
  if (restante == ultimoRestanteMostrado) return;   // solo refresca cuando cambia el segundo
  ultimoRestanteMostrado = restante;
  char l2[17];
  snprintf(l2, sizeof(l2), "quedan %3d s", restante);
  mostrar("Inflando...", String(l2));
}

// ============================================================================
//  SETUP
// ============================================================================
// -- Hora por NTP: necesaria para validar el certificado TLS del broker.
// Si no la conseguimos, el que llama cae a setInsecure() para NO dejar el
// equipo sin conectar por un problema de reloj. Alimenta el watchdog mientras
// espera. Devuelve true si consiguio hora real (epoch > nov-2023).
bool sincronizarHora() {
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  unsigned long t0 = millis();
  while (time(nullptr) < 1700000000UL && millis() - t0 < 10000) {
    delay(200);
    watchdogFeed();
  }
  return time(nullptr) > 1700000000UL;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  bootMs = millis();

  pinMode(RELE_PIN, OUTPUT);
  releEscribir(false);            // arranca en reposo (rele abierto)

  watchdogInit();

#if USAR_LCD
  lcdOk = lcdInit();
#endif
  mostrar("TermoPago", CAJA_ID);

  // recuperar dedup de NVS
  prefs.begin("inflado", false);
  ultimoPagoProcesado = prefs.getString("ultpago", "");
  pulsosTotales       = prefs.getUInt("pulsos", 0);

  ventanaResetCredenciales();     // 5 s con BOOT para borrar WiFi
  conectarWiFi();                 // aca puede abrirse el portal y tardar lo que haga falta

  watchdogAddTask();              // recien ahora el watchdog vigila el loop (WiFi ya conectado)

  reportarBoot();                 // registra el motivo del ultimo reinicio

  // MQTT sobre TLS
  // TLS del broker: si tenemos hora, validamos el certificado (setCACert con
  // las raices ISRG X1/X2). Si NO hay hora (NTP caido), caemos a setInsecure()
  // para que el equipo se conecte igual -> el endurecimiento nunca lo aisla.
  if (sincronizarHora()) {
    espClient.setCACert(HIVEMQ_CA_BUNDLE);
    Serial.println("[TLS] hora OK -> valido el certificado del broker");
  } else {
    espClient.setInsecure();
    Serial.println("[TLS] sin hora NTP -> setInsecure (degradado, pero conecta)");
  }
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(MQTT_KEEPALIVE_S);
  mqtt.setBufferSize(512);        // JSON + TLS: subir el buffer (default 256 es justo)
  mqtt.setCallback(mqttCallback);
  mqttConectar();

  ultimoMqttOk    = millis();
  ultimoHeartbeat = millis();
  ultimoLcdReinit = millis();
}

// ============================================================================
//  LOOP
// ============================================================================
void loop() {
  watchdogFeed();

  asegurarWiFi();
  asegurarMqtt();
  mqtt.loop();

  // ---- activar el proximo pago de la cola cuando el equipo esta libre ----
  if (!mostrandoCiclo && graciasHastaMs == 0 && colaLen > 0) {
    String p          = colaPagos[0];
    segundosPendiente = colaSegundos[0];
    for (int i = 1; i < colaLen; i++) {   // desplazar la cola (FIFO)
      colaPagos[i-1]    = colaPagos[i];
      colaSegundos[i-1] = colaSegundos[i];
    }
    colaLen--;
    darPulso(p);
    prefs.putUInt("pulsos", pulsosTotales);
  }

  // ---- heartbeat cada 60 s ----
  if (millis() - ultimoHeartbeat >= HEARTBEAT_MS) {
    ultimoHeartbeat = millis();
    publicarEstado("online", true);
    // reasegurar la pantalla de reposo si estamos ociosos (recupera la pantalla
    // tras mensajes transitorios como "reconectando")
    if (!mostrandoCiclo && graciasHastaMs == 0 && mqtt.connected()) pantallaEspera();
  }

  // ---- conteo estetico del ciclo ----
  if (mostrandoCiclo) actualizarDisplayCiclo();

  // ---- volver a la pantalla de espera tras el mensaje de "gracias" ----
  if (!mostrandoCiclo && graciasHastaMs && millis() >= graciasHastaMs) {
    graciasHastaMs = 0;
    pantallaEspera();
  }

  // ---- reinicio por "sin comunicacion" (MQTT caido > 90 s) ----
  // NO reinicia durante un servicio: espera a que termine el conteo (el pulso ya
  // salio y la maquina corre sola; reiniciar solo perderia el display y la cola).
  if (!mostrandoCiclo && (millis() - ultimoMqttOk > SIN_COMM_TIMEOUT)) {
    Serial.println("[SIN COMM] broker inalcanzable > 90 s -> reinicio");
    mostrar("Sin broker", "reiniciando");
    delay(500);
    ESP.restart();
  }

  // ---- reinicio preventivo cada 6 h en reposo ----
  if (!mostrandoCiclo && (millis() - bootMs > REINICIO_PREVENT)) {
    Serial.println("[PREVENTIVO] 6 h en reposo -> reinicio limpio");
    delay(200);
    ESP.restart();
  }

#if USAR_LCD
  // ---- reinit periodico del LCD ----
  if (millis() - ultimoLcdReinit > LCD_REINIT_MS) {
    ultimoLcdReinit = millis();
    if (!lcd.status()) lcdOk = lcdInit();
  }
#endif

  // ---- chequeo de heap ----
  // El umbral blando (20k) espera a que termine el servicio; solo un OOM critico
  // (<10k) reinicia igual en pleno conteo para no colgarse.
  uint32_t heapLibre = ESP.getFreeHeap();
  if ((heapLibre < 20000 && !mostrandoCiclo) || heapLibre < 10000) {
    Serial.printf("[HEAP] bajo: %u -> reinicio preventivo\n", heapLibre);
    delay(200);
    ESP.restart();
  }

  delay(10);
}

/*  ============================================================================
    NOTA TLS: YA IMPLEMENTADO (ver setup()).
    El TLS del broker esta endurecido: se sincroniza la hora por NTP y se valida
    el certificado con las raices de Let's Encrypt (ca_hivemq.h: ISRG X1 + X2)
    via espClient.setCACert(HIVEMQ_CA_BUNDLE). Si el NTP falla, cae a
    setInsecure() para no dejar el equipo sin conectar (endurecimiento best-effort).
    El reporte de boot al backend (Railway) sigue en setInsecure() a proposito:
    apunta a otra CA distinta y no es un canal critico.
    ============================================================================ */
