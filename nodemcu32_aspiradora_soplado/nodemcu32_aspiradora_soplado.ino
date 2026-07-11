// TermoPago - Estación dual: Aspiradora + Soplador
// NodeMCU-32 / ESP32. Los dos servicios funcionan en paralelo
// (firmware no bloqueante con timers por canal).

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <WiFiManager.h>
#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>
#include <esp_task_wdt.h>

const char* BACKEND_BASE  = "https://web-production-94bbab.up.railway.app";
const int   WDT_TIMEOUT_S = 120;
const unsigned long INTERVALO_POLL_MS = 3000;

// ─── Canales ─────────────────────────────────────────────────────
const int NUM_CANALES = 2;
const char* IDS[NUM_CANALES]     = {"aspiradora_001", "soplado_001"};
const char* ETIQ[NUM_CANALES]    = {"Aspirad", "Soplado"};  // max 7 chars (LCD)
const int   RELAY_PIN[NUM_CANALES] = {25, 26};

bool          activo[NUM_CANALES]  = {false, false};
unsigned long finMs[NUM_CANALES]   = {0, 0};
String        ordenId[NUM_CANALES] = {"", ""};

hd44780_I2Cexp lcd;
bool wifiConectadoMostrado = false;
unsigned long ultimoPollMs = 0;
unsigned long ultimoLcdMs  = 0;
int canalAPollear = 0;

// ─── LCD ─────────────────────────────────────────────────────────
void mostrarMensaje(String linea1, String linea2 = "") {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(linea1);
  if (linea2 != "") {
    lcd.setCursor(0, 1);
    lcd.print(linea2);
  }
}

String estadoCanal(int i) {
  String s = String(ETIQ[i]) + ": ";
  if (activo[i]) {
    long rest = (long)(finMs[i] - millis()) / 1000;
    if (rest < 0) rest = 0;
    int m = rest / 60;
    int sg = rest % 60;
    if (m > 0) s += String(m) + "m" + String(sg) + "s";
    else       s += String(sg) + "s";
  } else {
    s += "escanee QR";
  }
  while (s.length() < 16) s += " ";
  return s.substring(0, 16);
}

String lcdCache[NUM_CANALES] = {"", ""};

void refrescarLcd() {
  // Solo reescribe una línea si su contenido cambió (evita parpadeo)
  for (int i = 0; i < NUM_CANALES; i++) {
    String s = estadoCanal(i);
    if (s != lcdCache[i]) {
      lcdCache[i] = s;
      lcd.setCursor(0, i);
      lcd.print(s);
    }
  }
}

void invalidarLcd() {
  lcdCache[0] = "";
  lcdCache[1] = "";
}

// ─── Watchdog ────────────────────────────────────────────────────
void iniciarWatchdog() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_deinit();
  esp_task_wdt_config_t wdt_config = {
    .timeout_ms = WDT_TIMEOUT_S * 1000,
    .idle_core_mask = 0,
    .trigger_panic = true
  };
  esp_task_wdt_init(&wdt_config);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);
}

// ─── Backend ─────────────────────────────────────────────────────
void completarOrden(String id) {
  if (id == "") return;
  for (int intento = 0; intento < 3; intento++) {
    esp_task_wdt_reset();
    HTTPClient http;
    http.begin(String(BACKEND_BASE) + "/completar/" + id);
    http.setTimeout(5000);
    int code = http.GET();
    http.end();
    if (code == 200) {
      Serial.println("Orden completada: " + id);
      return;
    }
    delay(2000);
  }
  Serial.println("No se pudo confirmar (el backend la expira solo)");
}

void pollearCanal(int i) {
  HTTPClient http;
  http.begin(String(BACKEND_BASE) + "/orden/" + IDS[i]);
  http.setTimeout(5000);
  int code = http.GET();

  if (code == 200) {
    StaticJsonDocument<300> doc;
    DeserializationError error = deserializeJson(doc, http.getString());
    if (!error && doc["encender"] == true) {
      int segundos = doc["segundos"];
      ordenId[i] = String((const char*)(doc["orden_id"] | ""));
      activo[i] = true;
      finMs[i] = millis() + (unsigned long)segundos * 1000UL;
      digitalWrite(RELAY_PIN[i], LOW);  // encender
      Serial.println(String(ETIQ[i]) + " ON por " + String(segundos) + "s (orden " + ordenId[i] + ")");
    }
  } else {
    Serial.println("Error HTTP " + String(IDS[i]) + ": " + String(code));
  }
  http.end();
}

// ─── Setup ───────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);

  for (int i = 0; i < NUM_CANALES; i++) {
    pinMode(RELAY_PIN[i], OUTPUT);
    digitalWrite(RELAY_PIN[i], HIGH);  // apagados por defecto
  }

  Wire.begin(21, 22);
  lcd.begin(16, 2);
  lcd.backlight();
  mostrarMensaje("Iniciando...", "Por favor espere");
  delay(2000);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // sin ahorro de energía: elimina los picos de corriente
                         // periódicos del radio que hacen parpadear el LCD
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);

  mostrarMensaje("Conectando WiFi", "Por favor espere");

  WiFiManager wifiManager;
  wifiManager.setConfigPortalTimeout(180);
  if (!wifiManager.autoConnect("TermoPago-Duo")) {
    mostrarMensaje("Error WiFi", "Reiniciando...");
    delay(3000);
    ESP.restart();
  }

  Serial.println("WiFi conectado: " + WiFi.localIP().toString());
  wifiConectadoMostrado = true;
  lcd.clear();

  iniciarWatchdog();
}

// ─── Loop principal (no bloqueante) ──────────────────────────────
void loop() {
  esp_task_wdt_reset();

  // 1. Apagar canales cuyo tiempo terminó
  for (int i = 0; i < NUM_CANALES; i++) {
    if (activo[i] && (long)(finMs[i] - millis()) <= 0) {
      digitalWrite(RELAY_PIN[i], HIGH);  // apagar
      activo[i] = false;
      Serial.println(String(ETIQ[i]) + " OFF");
      completarOrden(ordenId[i]);
      ordenId[i] = "";
    }
  }

  // 2. WiFi
  if (WiFi.status() != WL_CONNECTED) {
    if (wifiConectadoMostrado) {
      wifiConectadoMostrado = false;
      mostrarMensaje("WiFi perdido", "Reconectando...");
      invalidarLcd();
      Serial.println("WiFi perdido...");
    }
    // los servicios activos siguen corriendo aunque no haya WiFi
    delay(200);
    return;
  }
  if (!wifiConectadoMostrado) {
    wifiConectadoMostrado = true;
    lcd.clear();
    invalidarLcd();
    Serial.println("WiFi reconectado: " + WiFi.localIP().toString());
  }

  // 3. Polling alternado: un canal inactivo por ciclo
  if (millis() - ultimoPollMs >= INTERVALO_POLL_MS) {
    ultimoPollMs = millis();
    for (int intento = 0; intento < NUM_CANALES; intento++) {
      int i = canalAPollear;
      canalAPollear = (canalAPollear + 1) % NUM_CANALES;
      if (!activo[i]) {
        pollearCanal(i);
        break;
      }
    }
  }

  // 4. LCD cada 1 segundo
  if (millis() - ultimoLcdMs >= 1000) {
    ultimoLcdMs = millis();
    refrescarLcd();
  }

  delay(50);
}
