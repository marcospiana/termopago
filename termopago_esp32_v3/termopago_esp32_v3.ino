#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <WiFiManager.h>
#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>
#include <esp_task_wdt.h>

const char* BACKEND_BASE = "https://web-production-94bbab.up.railway.app";
const char* DISPOSITIVO  = "termo_001";
const int   RELAY_PIN    = 25;
const int   WDT_TIMEOUT_S = 120;   // si el firmware se cuelga 2 min, reinicia solo

hd44780_I2Cexp lcd;
bool wifiConectadoMostrado = false;

void mostrarMensaje(String linea1, String linea2 = "") {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(linea1);
  if (linea2 != "") {
    lcd.setCursor(0, 1);
    lcd.print(linea2);
  }
}

void iniciarWatchdog() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  // Core ESP32 v3.x
  esp_task_wdt_deinit();
  esp_task_wdt_config_t wdt_config = {
    .timeout_ms = WDT_TIMEOUT_S * 1000,
    .idle_core_mask = 0,
    .trigger_panic = true
  };
  esp_task_wdt_init(&wdt_config);
#else
  // Core ESP32 v2.x
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);
}

void setup() {
  Serial.begin(115200);

  // Relé apagado por defecto
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, HIGH);

  // LCD
  Wire.begin(21, 22);
  lcd.begin(16, 2);
  lcd.backlight();
  mostrarMensaje("Iniciando...", "Por favor espere");
  delay(2000);

  // WiFi
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // sin ahorro de energía: elimina los picos de corriente
                         // periódicos del radio que hacen parpadear el LCD
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);

  mostrarMensaje("Conectando WiFi", "Por favor espere");

  WiFiManager wifiManager;
  wifiManager.setConfigPortalTimeout(180);
  if (!wifiManager.autoConnect("TermoPago")) {
    mostrarMensaje("Error WiFi", "Reiniciando...");
    delay(3000);
    ESP.restart();
  }

  mostrarMensaje("WiFi conectado!", "Escanee el QR");
  wifiConectadoMostrado = true;
  Serial.println("WiFi conectado: " + WiFi.localIP().toString());

  // Watchdog: se inicia DESPUES de conectar WiFi (el portal de
  // configuracion puede tardar mas que el timeout)
  iniciarWatchdog();
}

// Avisa al backend que la orden se completo (con reintentos).
// Si falla, no pasa nada grave: el backend deja de re-entregarla
// sola cuando se agota el tiempo.
void completarOrden(String ordenId) {
  if (ordenId == "") return;
  for (int intento = 0; intento < 3; intento++) {
    esp_task_wdt_reset();
    HTTPClient http;
    http.begin(String(BACKEND_BASE) + "/completar/" + ordenId);
    http.setTimeout(5000);
    int code = http.GET();
    http.end();
    if (code == 200) {
      Serial.println("Orden completada: " + ordenId);
      return;
    }
    delay(2000);
  }
  Serial.println("No se pudo confirmar la orden (el backend la expira solo)");
}

void activarRele(int segundos, String ordenId) {
  digitalWrite(RELAY_PIN, LOW); // encender

  // La línea 1 se escribe una sola vez; la 2 se sobrescribe sin borrar
  // la pantalla (así no parpadea).
  mostrarMensaje("Agua disponible!");

  for (int i = segundos; i > 0; i--) {
    esp_task_wdt_reset();  // alimentar el watchdog durante el servicio
    int min = i / 60;
    int seg = i % 60;
    String linea;
    if (min > 0) linea = "Quedan " + String(min) + "m " + String(seg) + "s";
    else         linea = "Quedan " + String(seg) + " seg";
    while (linea.length() < 16) linea += " ";
    lcd.setCursor(0, 1);
    lcd.print(linea.substring(0, 16));
    delay(1000);
  }

  digitalWrite(RELAY_PIN, HIGH); // apagar

  completarOrden(ordenId);

  mostrarMensaje("   Listo!      ", "   Gracias!    ");
  delay(5000);
  mostrarMensaje("WiFi conectado!", "Escanee el QR");
}

void loop() {
  esp_task_wdt_reset();

  // Si pierde WiFi, esperar reconexión automática
  if (WiFi.status() != WL_CONNECTED) {
    if (wifiConectadoMostrado) {
      wifiConectadoMostrado = false;
      mostrarMensaje("WiFi perdido", "Reconectando...");
      Serial.println("WiFi perdido, esperando reconexion...");
    }
    delay(3000);
    return;
  }

  // WiFi volvió
  if (!wifiConectadoMostrado) {
    mostrarMensaje("WiFi conectado!", "Escanee el QR");
    wifiConectadoMostrado = true;
    Serial.println("WiFi reconectado: " + WiFi.localIP().toString());
  }

  // Consultar backend
  HTTPClient http;
  http.begin(String(BACKEND_BASE) + "/orden/" + DISPOSITIVO);
  http.setTimeout(5000);
  int code = http.GET();

  if (code == 200) {
    StaticJsonDocument<300> doc;
    DeserializationError error = deserializeJson(doc, http.getString());
    if (!error && doc["encender"] == true) {
      int segundos = doc["segundos"];
      String ordenId = doc["orden_id"] | "";
      Serial.println("Activando rele por " + String(segundos) + " seg (orden " + ordenId + ")");
      http.end();
      activarRele(segundos, ordenId);
      return;
    }
  } else {
    Serial.println("Error HTTP: " + String(code));
  }

  http.end();
  delay(3000);
}
