// TermoPago - ESTACION 01 - MAESTRO (ESP32)  [solo WiFi + backend]
// =====================================================================
// Este ESP SOLO hace WiFi + backend. Cuando entra un pago, le ordena al
// ESCLAVO que active el canal por X segundos. El ESCLAVO maneja el RELE,
// el TIEMPO y el CONTADOR en pantalla -> el servicio sigue aunque el
// maestro pierda WiFi o se reinicie. El maestro NO tiene reles ni LCD.
//
// CONEXIONES:
//   Enlace al esclavo:  GPIO17 (TX2) -> ESCLAVO RX
//                       GND comun con el esclavo (OBLIGATORIO)
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
const int   WDT_TIMEOUT_S = 30;   // si el loop se cuelga 30s, el watchdog reinicia
const unsigned long INTERVALO_POLL_MS = 3000;

const int NUM_CANALES = 2;
const char* IDS[NUM_CANALES]     = {"aspiradora01", "soplado01"};
const char* NOMBRES[NUM_CANALES] = {"Aspiradora", "Soplador"};
const char* ETIQ[NUM_CANALES]    = {"Aspirad", "Soplado"};
const int   RELAY_PIN[NUM_CANALES] = {25, 26};

// UART al display (ESP8266). Solo enviamos (un sentido).
#define LINK_RX 16   // sin uso
#define LINK_TX 17   // -> RX del ESP8266

// ── Clavar la conexion a la banda 2.4GHz (evita el band-steering a 5GHz). ──
//    BSSID = MAC de la radio 2.4 de la red (sacado del escaner). Si cambias
//    de red, actualiza estos dos valores. Para NO clavar, poner LOCK_ON 0.
#define LOCK_ON 1
uint8_t LOCK_BSSID[6] = {0x58, 0x56, 0xC2, 0x4A, 0x1C, 0x98};  // MEGA FIBRA-2.4G-6yEt 2.4GHz
int32_t LOCK_CHANNEL  = 2;

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

// ─── Enlace al esclavo por UART ──────────────────────────────────
// Texto de REPOSO (2 lineas). El contador de servicio lo dibuja el esclavo.
void mostrar(String l1, String l2) {
  while (l1.length() < 16) l1 += " ";  l1 = l1.substring(0, 16);
  while (l2.length() < 16) l2 += " ";  l2 = l2.substring(0, 16);
  Serial2.print('1'); Serial2.print(l1); Serial2.print('\n');
  Serial2.print('2'); Serial2.print(l2); Serial2.print('\n');
}

// Ordena al esclavo ACTIVAR un canal por X segundos. El esclavo maneja el
// rele y el tiempo; asi el servicio sigue aunque el maestro pierda WiFi o
// se reinicie. El orden_id evita re-activar dos veces el mismo pago.
void enviarActivar(int canal, int segundos, String orden) {
  Serial2.print("A,"); Serial2.print(canal); Serial2.print(",");
  Serial2.print(segundos); Serial2.print(","); Serial2.print(orden);
  Serial2.print('\n');
}

void refrescar() {
  // El maestro solo manda el texto de REPOSO. Cuando hay servicio, el esclavo
  // muestra su propio contador e ignora esto.
  bool serverOk = huboPollOk && (ciclosSinServer < 2);
  if (serverOk) mostrar("WiFi conectado!", "Escanee el QR");
  else          mostrar("Sin conexion",   "al servidor...");
}

// ─── Watchdog ────────────────────────────────────────────────────
void iniciarWatchdog() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  // El core 3.x ya arranca el Task-WDT, pero sin reset (solo avisa). Por eso
  // NO reiniciaba. Lo RECONFIGURAMOS con trigger_panic=true (resetea de verdad)
  // y le suscribimos el loop. Este era el motivo de que quedara muerto.
  esp_task_wdt_config_t wdt_config = { .timeout_ms = (uint32_t)WDT_TIMEOUT_S * 1000, .idle_core_mask = 0, .trigger_panic = true };
  esp_task_wdt_reconfigure(&wdt_config);
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
      enviarActivar(i, segundos, ordenId[i]);   // el esclavo enciende el rele
      Serial.println(String(ETIQ[i]) + " ON " + String(segundos) + "s (esclavo)");
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
  Serial2.begin(9600, SERIAL_8N1, LINK_RX, LINK_TX);   // enlace al esclavo
  // (sin reles: ahora los maneja el esclavo)

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
    String red  = wm.getWiFiSSID();   // SSID guardado (si ya se configuro una vez)
    String pass = wm.getWiFiPass();   // clave guardada (queda en NVS, no en el codigo)
    if (red.length() > 0) {
      // HAY red guardada: conectar SIN abrir el portal.
      unsigned long t0 = millis();
#if LOCK_ON
      // Primero, CLAVADO al BSSID de 2.4GHz (evita band-steering a 5GHz).
      WiFi.begin(red.c_str(), pass.c_str(), LOCK_CHANNEL, LOCK_BSSID);
      while (WiFi.status() != WL_CONNECTED && millis() - t0 < 18000) delay(250);
      // Si no conecto clavado (el router cambio de canal/MAC), probar normal.
      if (WiFi.status() != WL_CONNECTED) {
        WiFi.begin(red.c_str(), pass.c_str());
        t0 = millis();
        while (WiFi.status() != WL_CONNECTED && millis() - t0 < 12000) delay(250);
      }
#else
      WiFi.begin();                  // usa las credenciales guardadas (sin clavar)
      while (WiFi.status() != WL_CONNECTED && millis() - t0 < 25000) delay(250);
#endif
      if (WiFi.status() != WL_CONNECTED) {
        mostrar("Sin WiFi", "Reintentando...");
        delay(1000);
        ESP.restart();               // reintenta, sin portal
      }
    } else {
      // PRIMERA VEZ / sin credenciales (o tras borrar con BOOT): abrir portal
      wm.setConfigPortalTimeout(180);
      if (!wm.autoConnect("TermoPago-Est01")) { delay(1000); ESP.restart(); }
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

  // 0. Red de seguridad UNIVERSAL: si hace 60s que no llega al servidor —sea el
  //    motivo que sea (WiFi caido, enlace "zombie" que dice conectado pero no
  //    responde, cambio de canal del router, auto-reconnect fallado)— REINICIA.
  //    Un reinicio reconecta siempre. Y como el servicio lo tiene el esclavo,
  //    reiniciar el maestro NO corta nada.
  if (millis() - bootMs > 60000 && millis() - ultimoPollOkMs > 60000) {
    Serial.println("Sin llegar al server 60s, reiniciando para reconectar...");
    mostrar("Reconectando", "Reiniciando...");
    delay(300); ESP.restart();
  }
  if (!activo[0] && !activo[1] && ESP.getFreeHeap() < 40000) {
    Serial.println("Heap bajo, reiniciando..."); delay(200); ESP.restart();
  }

  // 1. Timer PARALELO (solo para confirmar /completar y no re-pollear el canal
  //    mientras dura). El rele lo apaga el esclavo por su cuenta.
  for (int i = 0; i < NUM_CANALES; i++) {
    if (activo[i] && (long)(finMs[i] - millis()) <= 0) {
      activo[i] = false;
      pendienteCompletar[i] = ordenId[i];
      ordenId[i] = "";
      Serial.println(String(ETIQ[i]) + " fin (paralelo)");
    }
  }

  // 2. WiFi caido: mostrar el estado y esperar al auto-reconnect. El reinicio de
  //    recuperacion lo dispara la red de seguridad universal de arriba (60s sin
  //    server), que cubre TAMBIEN el caso "zombie" (status conectado pero sin
  //    llegar al server) sin depender de un timer que el flapping resetee.
  if (WiFi.status() != WL_CONNECTED) {
    if (wifiConectadoMostrado) {
      wifiConectadoMostrado = false;
      mostrar("WiFi perdido", "Reconectando...");
      Serial.println("WiFi perdido...");
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
