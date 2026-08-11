// TermoPago - ESTACION 02 - MAESTRO (ESP32)  [display remoto por UART, 2 ESP32]
// =====================================================================
// Este ESP maneja WiFi + backend + RELE. El DISPLAY lo maneja un ESP8266
// aparte: el cerebro le manda las 2 lineas del LCD por UART (Serial2).
// Al NO tener LCD/I2C, este ESP no puede colgarse por el bus del display.
//
// CONEXIONES:
//   Enlace al display:  GPIO17 (TX2) -> ESP8266 RX (D6/GPIO12)
//                       GND comun con el ESP8266 (obligatorio)
//   Reles:  GPIO25 (aspiradora)  GPIO26 (soplador)   (activos en LOW)
//   BOOT (GPIO0): ventana de 5s al encender para resetear el WiFi
// =====================================================================

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <WiFiManager.h>
#include <esp_task_wdt.h>
#include <WiFiClientSecure.h>
#include <esp_system.h>   // esp_reset_reason(): causa del ultimo reinicio
#include "rtc_wdt.h"   // si NO compila, borrar esta linea y las marcadas // RTC-WDT

const char* BACKEND_BASE  = "https://web-production-94bbab.up.railway.app";
const int   WDT_TIMEOUT_S = 120;
const unsigned long INTERVALO_POLL_MS = 3000;

const int NUM_CANALES = 2;
const char* IDS[NUM_CANALES]     = {"aspiradora02", "soplado02"};
const char* NOMBRES[NUM_CANALES] = {"Aspiradora", "Soplador"};
const char* ETIQ[NUM_CANALES]    = {"Aspirad", "Soplado"};
const int   RELAY_PIN[NUM_CANALES] = {25, 26};

// UART al display (ESP8266). Solo enviamos (un sentido).
#define LINK_RX 16   // sin uso
#define LINK_TX 17   // -> RX del ESP8266

bool          activo[NUM_CANALES]  = {false, false};
unsigned long finMs[NUM_CANALES]   = {0, 0};
String        ordenId[NUM_CANALES] = {"", ""};
String        pendienteCompletar[NUM_CANALES] = {"", ""};

bool wifiConectadoMostrado = false;
unsigned long ultimoPollMs = 0;
unsigned long ultimoLcdMs  = 0;
unsigned long inicioCaidaMs = 0;
unsigned long ultimoPollOkMs = 0;
bool huboPollOk = false;
unsigned long bootMs = 0;
int ciclosSinServer = 0;   // ciclos de polling seguidos sin llegar al server

// ─── Display remoto: manda las 2 lineas por UART ─────────────────
void mostrar(String l1, String l2) {
  while (l1.length() < 16) l1 += " ";  l1 = l1.substring(0, 16);
  while (l2.length() < 16) l2 += " ";  l2 = l2.substring(0, 16);
  Serial2.print('1'); Serial2.print(l1); Serial2.print('\n');
  Serial2.print('2'); Serial2.print(l2); Serial2.print('\n');
}

String tiempoRestante(int i) {
  long rest = (long)(finMs[i] - millis()) / 1000;
  if (rest < 0) rest = 0;
  int m = rest / 60, sg = rest % 60;
  if (m > 0) return String(m) + "m " + String(sg) + "s";
  return String(sg) + " seg";
}

void refrescar() {
  String l1, l2;
  // Solo declara "sin servidor" tras 2 ciclos de polling seguidos fallados
  // (evita mostrar la leyenda por un bache de una sola consulta).
  bool serverOk = huboPollOk && (ciclosSinServer < 2);
  if (!activo[0] && !activo[1]) {
    if (serverOk) { l1 = "WiFi conectado!"; l2 = "Escanee el QR"; }
    else          { l1 = "Sin conexion";   l2 = "al servidor..."; }
  } else if (activo[0] && !activo[1]) { l1 = NOMBRES[0]; l2 = "Quedan " + tiempoRestante(0); }
  else if (!activo[0] && activo[1])   { l1 = NOMBRES[1]; l2 = "Quedan " + tiempoRestante(1); }
  else { l1 = "Aspir: " + tiempoRestante(0); l2 = "Sopla: " + tiempoRestante(1); }
  mostrar(l1, l2);
}

// ─── Watchdog ────────────────────────────────────────────────────
void iniciarWatchdog() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_deinit();
  esp_task_wdt_config_t wdt_config = { .timeout_ms = WDT_TIMEOUT_S * 1000, .idle_core_mask = 0, .trigger_panic = true };
  esp_task_wdt_init(&wdt_config);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);
  rtc_wdt_protect_off();                                                // RTC-WDT
  rtc_wdt_set_stage(RTC_WDT_STAGE0, RTC_WDT_STAGE_ACTION_RESET_SYSTEM);  // RTC-WDT
  rtc_wdt_set_time(RTC_WDT_STAGE0, 60000);                              // RTC-WDT
  rtc_wdt_enable();                                                     // RTC-WDT
  rtc_wdt_protect_on();                                                 // RTC-WDT
}

// ─── Backend ─────────────────────────────────────────────────────
void completarOrden(String id) {
  if (id == "") return;
  for (int intento = 0; intento < 3; intento++) {
    esp_task_wdt_reset();
    WiFiClientSecure client; client.setInsecure();
    HTTPClient http;
    http.begin(client, String(BACKEND_BASE) + "/completar/" + id);
    http.setConnectTimeout(4000);
    http.setTimeout(5000);
    int code = http.GET();
    http.end();
    if (code == 200) { Serial.println("Orden completada: " + id); return; }
    delay(2000);
  }
  Serial.println("No se pudo confirmar (el backend la expira solo)");
}

bool pollearCanal(int i) {   // devuelve true si el server respondio (HTTP 200)
  esp_task_wdt_reset();
  WiFiClientSecure client; client.setInsecure();
  HTTPClient http;
  http.begin(client, String(BACKEND_BASE) + "/orden/" + IDS[i]);
  http.setConnectTimeout(4000);
  http.setTimeout(5000);
  int code = http.GET();
  bool ok = (code == 200);
  if (ok) {
    ultimoPollOkMs = millis(); huboPollOk = true;
    StaticJsonDocument<300> doc;
    DeserializationError error = deserializeJson(doc, http.getString());
    if (!error && doc["encender"] == true) {
      int segundos = doc["segundos"];
      ordenId[i] = String((const char*)(doc["orden_id"] | ""));
      activo[i] = true;
      finMs[i] = millis() + (unsigned long)segundos * 1000UL;
      digitalWrite(RELAY_PIN[i], LOW);   // encender
      Serial.println(String(ETIQ[i]) + " ON " + String(segundos) + "s");
    }
  } else {
    Serial.println("Error HTTP " + String(IDS[i]) + ": " + String(code));
  }
  http.end();
  return ok;
}

// ─── Causa del reinicio: se reporta al backend en cada arranque ──
String motivoReinicio() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:   return "corte-luz";      // encendido / volvio la luz
    case ESP_RST_SW:        return "software";       // ESP.restart() nuestro (WiFi/idle)
    case ESP_RST_PANIC:     return "panic-crash";    // crash de software
    case ESP_RST_INT_WDT:   return "wdt-interrupt";
    case ESP_RST_TASK_WDT:  return "wdt-task";        // watchdog: se colgo el loop
    case ESP_RST_WDT:       return "wdt-otro";
    case ESP_RST_BROWNOUT:  return "brownout-elec";   // bajon de tension (pico bobina!)
    case ESP_RST_EXT:       return "reset-externo";
    default:                return "desconocido";
  }
}

void reportarBoot() {
  WiFiClientSecure client; client.setInsecure();
  HTTPClient http;
  http.begin(client, String(BACKEND_BASE) + "/boot/" + IDS[0] + "/" + motivoReinicio());
  http.setConnectTimeout(4000);
  http.setTimeout(5000);
  http.GET();
  http.end();
  Serial.println("Boot reportado: " + motivoReinicio());
}

// ─── Setup ───────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Serial2.begin(9600, SERIAL_8N1, LINK_RX, LINK_TX);   // enlace al display

  for (int i = 0; i < NUM_CANALES; i++) {
    pinMode(RELAY_PIN[i], OUTPUT);
    digitalWrite(RELAY_PIN[i], HIGH);   // apagados
  }

  mostrar("Iniciando...", "Por favor espere");

  // Reset de WiFi: ventana de 5s con BOOT al encender (no al arrancar)
  pinMode(0, INPUT_PULLUP);
  mostrar("Cambiar WiFi?", "Apriete BOOT 5s");
  { unsigned long t0 = millis(); bool reset = false;
    while (millis() - t0 < 5000) {
      if (digitalRead(0) == LOW) {
        unsigned long t1 = millis();
        while (digitalRead(0) == LOW && millis() - t1 < 1000) delay(50);
        if (millis() - t1 >= 1000) { reset = true; break; }
      }
      delay(50);
    }
    if (reset) { WiFiManager wm; wm.resetSettings(); mostrar("WiFi borrado", "Configure de nuevo"); delay(1500); }
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);

  mostrar("Conectando WiFi", "Por favor espere");
  {
    WiFiManager wm;
    String red = wm.getWiFiSSID();   // SSID guardado (si ya se configuro una vez)
    if (red.length() > 0) {
      // HAY red guardada: conectar SIN abrir el portal. Si no logra conectar,
      // reinicia y vuelve a intentar la MISMA red. Nunca queda esperando que
      // alguien lo configure (era lo que lo dejaba muerto hasta reiniciar).
      WiFi.begin();                  // usa las credenciales guardadas
      unsigned long t0 = millis();
      while (WiFi.status() != WL_CONNECTED && millis() - t0 < 25000) delay(250);
      if (WiFi.status() != WL_CONNECTED) {
        mostrar("Sin WiFi", "Reintentando...");
        delay(1000);
        ESP.restart();               // reintenta la red guardada, sin portal
      }
    } else {
      // PRIMERA VEZ / sin credenciales (o tras borrar con BOOT): abrir portal
      wm.setConfigPortalTimeout(180);
      if (!wm.autoConnect("TermoPago-Est02")) { delay(1000); ESP.restart(); }
    }
  }

  Serial.println("WiFi conectado: " + WiFi.localIP().toString());
  wifiConectadoMostrado = true;
  reportarBoot();          // avisa al backend por que se reinicio esta vez
  bootMs = millis();
  iniciarWatchdog();
}

// ─── Loop ────────────────────────────────────────────────────────
void loop() {
  esp_task_wdt_reset();
  rtc_wdt_feed();   // RTC-WDT

  // 0. Redes de seguridad SOLO en reposo (no cortar servicio pago)
  if (!activo[0] && !activo[1]) {
    if (ESP.getFreeHeap() < 40000) { Serial.println("Heap bajo, reiniciando..."); delay(200); ESP.restart(); }
    if (millis() - bootMs > 120000 && millis() - ultimoPollOkMs > 120000) {
      Serial.println("Sin server 2min (idle), reiniciando..."); delay(200); ESP.restart();
    }
  }

  // 1. Apagar canales cuyo tiempo termino (instantaneo; /completar despues)
  for (int i = 0; i < NUM_CANALES; i++) {
    if (activo[i] && (long)(finMs[i] - millis()) <= 0) {
      digitalWrite(RELAY_PIN[i], HIGH);   // apagar
      activo[i] = false;
      pendienteCompletar[i] = ordenId[i];
      ordenId[i] = "";
      Serial.println(String(ETIQ[i]) + " OFF");
    }
  }

  // 2. WiFi caido: simple. Auto-reconnect; si no vuelve en 60s, reboot limpio
  if (WiFi.status() != WL_CONNECTED) {
    if (wifiConectadoMostrado) {
      wifiConectadoMostrado = false;
      inicioCaidaMs = millis();
      mostrar("WiFi perdido", "Reconectando...");
      Serial.println("WiFi perdido...");
    }
    if (millis() - inicioCaidaMs >= 60000) {
      mostrar("Sin WiFi", "Reiniciando...");
      Serial.println("WiFi caido 60s, reiniciando..."); delay(500); ESP.restart();
    }
    delay(200);
    return;
  }
  if (!wifiConectadoMostrado) {
    wifiConectadoMostrado = true;
    Serial.println("WiFi reconectado: " + WiFi.localIP().toString());
  }

  // 2b. Confirmar ordenes terminadas (fuera del momento de apagado)
  for (int i = 0; i < NUM_CANALES; i++) {
    if (pendienteCompletar[i] != "") { completarOrden(pendienteCompletar[i]); pendienteCompletar[i] = ""; }
  }

  // 3. Polling de ambos canales inactivos. Cuenta ciclos seguidos sin server:
  //    recien tras 2 ciclos fallados el display muestra "Sin conexion".
  if (millis() - ultimoPollMs >= INTERVALO_POLL_MS) {
    ultimoPollMs = millis();
    bool polleado = false, algunOk = false;
    for (int i = 0; i < NUM_CANALES; i++) {
      if (!activo[i]) { polleado = true; if (pollearCanal(i)) algunOk = true; }
    }
    if (polleado) {                    // solo evaluamos si de verdad consultamos
      if (algunOk) ciclosSinServer = 0;
      else         ciclosSinServer++;  // este ciclo no llego al server
    }
  }

  // 4. Refrescar display (manda las 2 lineas por UART) cada 1s
  if (millis() - ultimoLcdMs >= 1000) { ultimoLcdMs = millis(); refrescar(); }

  delay(50);
}
