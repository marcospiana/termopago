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
const char* IDS[NUM_CANALES]     = {"aspiradora01", "soplado01"};
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
unsigned long inicioCaidaMs = 0;      // cuándo empezó la caída de WiFi
unsigned long ultimoReintentoMs = 0;  // último WiFi.begin() forzado
unsigned long ultimoReinitLcdMs = 0;  // última re-inicialización del LCD
unsigned long ultimoPollOkMs = 0;     // último polling EXITOSO al backend
bool huboPollOk = false;              // ¿alguna vez conectó al server?

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

const char* NOMBRES[NUM_CANALES] = {"Aspiradora", "Soplador"};

String rellenar(String s) {
  while (s.length() < 16) s += " ";
  return s.substring(0, 16);
}

String tiempoRestante(int i) {
  long rest = (long)(finMs[i] - millis()) / 1000;
  if (rest < 0) rest = 0;
  int m  = rest / 60;
  int sg = rest % 60;
  if (m > 0) return String(m) + "m " + String(sg) + "s";
  return String(sg) + " seg";
}

String lcdCache[2] = {"", ""};

void refrescarLcd() {
  String l1, l2;
  // ¿el backend responde? si el WiFi conecta pero hace >25s que no hay
  // un polling exitoso, es señal débil / sin llegada al servidor.
  bool serverOk = huboPollOk && (millis() - ultimoPollOkMs < 25000);
  if (!activo[0] && !activo[1]) {
    if (serverOk) {
      l1 = "WiFi conectado!";
      l2 = "Escanee el QR";
    } else {
      l1 = "Sin conexion";
      l2 = "al servidor...";
    }
  } else if (activo[0] && !activo[1]) {
    l1 = NOMBRES[0];
    l2 = "Quedan " + tiempoRestante(0);
  } else if (!activo[0] && activo[1]) {
    l1 = NOMBRES[1];
    l2 = "Quedan " + tiempoRestante(1);
  } else {
    // los dos en uso a la vez: una línea para cada uno
    l1 = "Aspir: " + tiempoRestante(0);
    l2 = "Sopla: " + tiempoRestante(1);
  }
  l1 = rellenar(l1);
  l2 = rellenar(l2);
  // Solo reescribe una línea si cambió (evita parpadeo)
  if (l1 != lcdCache[0]) { lcdCache[0] = l1; lcd.setCursor(0, 0); lcd.print(l1); }
  if (l2 != lcdCache[1]) { lcdCache[1] = l2; lcd.setCursor(0, 1); lcd.print(l2); }
}

void invalidarLcd() {
  lcdCache[0] = "";
  lcdCache[1] = "";
}

// Re-inicializa el LCD. El ruido del rele puede corromper el controlador
// (queda ilegible); esto lo restaura sin reiniciar la placa.
// Redibuja al instante para que no quede la pantalla en blanco.
void reiniciarLcd() {
  lcd.begin(16, 2);
  lcd.backlight();
  invalidarLcd();
  refrescarLcd();   // vuelve a escribir el contenido de inmediato
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
    ultimoPollOkMs = millis();   // el backend respondio: conexion OK
    huboPollOk = true;
    StaticJsonDocument<300> doc;
    DeserializationError error = deserializeJson(doc, http.getString());
    if (!error && doc["encender"] == true) {
      int segundos = doc["segundos"];
      ordenId[i] = String((const char*)(doc["orden_id"] | ""));
      activo[i] = true;
      finMs[i] = millis() + (unsigned long)segundos * 1000UL;
      digitalWrite(RELAY_PIN[i], LOW);  // encender
      delay(30);
      reiniciarLcd();  // recupera el LCD del pico de conmutacion del rele
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

  // ── Reset de WiFi: al ENCENDER NORMAL (sin apretar nada), hay una
  //    ventana de 5 seg para apretar BOOT y borrar la red guardada.
  //    (No apretar BOOT al prender: eso mete al ESP en modo grabacion.)
  pinMode(0, INPUT_PULLUP);
  mostrarMensaje("Cambiar WiFi?", "Apriete BOOT 5s");
  { unsigned long t0 = millis(); bool reset = false;
    while (millis() - t0 < 5000) {
      if (digitalRead(0) == LOW) {          // BOOT apretado durante la ventana
        unsigned long t1 = millis();
        while (digitalRead(0) == LOW && millis() - t1 < 1000) delay(50);
        if (millis() - t1 >= 1000) { reset = true; break; }
      }
      delay(50);
    }
    if (reset) {
      WiFiManager wm;
      wm.resetSettings();                   // olvida la red guardada
      mostrarMensaje("WiFi borrado", "Configure de nuevo");
      delay(1500);
    }
  }

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
      delay(30);
      reiniciarLcd();  // recupera el LCD del pico de conmutacion del rele
      activo[i] = false;
      Serial.println(String(ETIQ[i]) + " OFF");
      completarOrden(ordenId[i]);
      ordenId[i] = "";
    }
  }

  // 2. WiFi con reconexión ACTIVA (no depende solo de setAutoReconnect).
  //    Ante caída dura del enlace WDS, el auto-reconnect a veces se rinde;
  //    acá forzamos begin() periódicamente y, si no vuelve, reiniciamos.
  if (WiFi.status() != WL_CONNECTED) {
    unsigned long ahora = millis();
    if (wifiConectadoMostrado) {
      wifiConectadoMostrado = false;
      inicioCaidaMs = ahora;          // marca cuándo empezó la caída
      ultimoReintentoMs = ahora;
      mostrarMensaje("WiFi perdido", "Reconectando...");
      invalidarLcd();
      Serial.println("WiFi perdido...");
    }
    // reintento forzado cada 15 s (WiFi.begin despierta al driver colgado)
    if (ahora - ultimoReintentoMs >= 15000) {
      ultimoReintentoMs = ahora;
      Serial.println("Forzando reconexion WiFi...");
      WiFi.disconnect();
      WiFi.begin();                   // usa las credenciales guardadas
    }
    // si sigue caído tras 3 min, reiniciar (ultimo recurso, se auto-recupera)
    if (ahora - inicioCaidaMs >= 180000) {
      mostrarMensaje("Sin WiFi 3min", "Reiniciando...");
      Serial.println("WiFi caido 3min, reiniciando...");
      delay(1500);
      ESP.restart();
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

  // 5. Re-inicializacion periodica de seguridad (cada 5 min): red de
  //    seguridad minima. El ruido real se cubre al encender/apagar el rele.
  if (millis() - ultimoReinitLcdMs >= 300000) {
    ultimoReinitLcdMs = millis();
    reiniciarLcd();
  }

  delay(50);
}
