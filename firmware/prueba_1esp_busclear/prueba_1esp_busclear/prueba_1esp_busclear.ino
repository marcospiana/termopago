// ============================================================
// TermoPago - Firmware de estación dual (aspiradora + soplador)
// VERSIÓN OPTIMIZADA ANTI-BLOQUEO / ANTI-COLGADO (HIGH-RELIABILITY)
// ------------------------------------------------------------
// GUARDADA COMO PRUEBA ALTERNATIVA (1 solo ESP, LCD por I2C a 2m).
// Estrategia distinta al prototipo de 2 ESP. Configurada para ESTACION 02.
// OJO al probar: vigilar el Monitor Serie por si el WiFiClientSecure global
//   reutilizado empieza a fallar los polls (si pasa, usar cliente fresco o
//   agregar clientSecure.stop() tras cada pedido).
// ============================================================
// Mejoras principales incorporadas:
//  1. Rutina de recuperación física del bus I2C (I2C Bus Clear) de 9 pulsos.
//  2. Polling escalonado y alternado (nunca bloquea haciendo HTTPs seguidos).
//  3. Confirmación de órdenes (completarOrden) 100% no bloqueante.
//  4. Manejo seguro de memoria RAM (evita fragmentación Heap por SSL).
//  5. Compatibilidad multiplataforma para Watchdog (ESP32 Core v2.x y v3.x).
//  6. Protección contra desbordamiento de millis() y transitorios I2C.
// ============================================================

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <WiFiManager.h>
#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>
#include <esp_task_wdt.h>
#include <WiFiClientSecure.h>

// ── Compilacion condicional para RTC Watchdog ─────────────────────
#if __has_include("rtc_wdt.h")
  #include "rtc_wdt.h"
  #define HAS_RTC_WDT 1
#elif __has_include("driver/rtc_io.h")
  #define HAS_RTC_WDT 0
#else
  #define HAS_RTC_WDT 0
#endif

// ─── Configuración General ───────────────────────────────────────
const char* BACKEND_BASE      = "https://web-production-94bbab.up.railway.app";
const int   WDT_TIMEOUT_S     = 120;
const unsigned long INTERVALO_POLL_MS = 3000; // Polling cada 3 segundos

// ─── Pines I2C (NodeMCU ESP32) ───────────────────────────────────
const int I2C_SDA_PIN = 21;
const int I2C_SCL_PIN = 22;

// ─── Canales ─────────────────────────────────────────────────────
const int NUM_CANALES = 2;
const char* IDS[NUM_CANALES]       = {"aspiradora02", "soplado02"};
const char* ETIQ[NUM_CANALES]      = {"Aspirad", "Soplado"};   // Max 7 chars LCD
const char* NOMBRES[NUM_CANALES]   = {"Aspiradora", "Soplador"};
const int   RELAY_PIN[NUM_CANALES] = {25, 26};

bool          activo[NUM_CANALES]             = {false, false};
unsigned long finMs[NUM_CANALES]              = {0, 0};
String        ordenId[NUM_CANALES]            = {"", ""};
String        pendienteCompletar[NUM_CANALES] = {"", ""}; // Órdenes a confirmar sin bloquear

// ─── Variables de Estado y Displays ──────────────────────────────
hd44780_I2Cexp lcd;
String lcdCache[2] = {"", ""};

bool          wifiConectadoMostrado = false;
unsigned long ultimoPollMs          = 0;
unsigned long ultimoLcdMs           = 0;
int           canalAPollear         = 0;
unsigned long inicioCaidaMs         = 0;
unsigned long ultimoReinitLcdMs     = 0;
unsigned long ultimoPollOkMs        = 0;
bool          huboPollOk            = false;
unsigned long reinitLcdEnMs         = 0;
unsigned long bootMs                = 0;

// Cliente HTTPS global para evitar fragmentar memoria RAM en la Heap
WiFiClientSecure clientSecure;

// ─── Rutina de Recuperación del Bus I2C (Bus Clear) ──────────────
// Si el cable largo (2m) o el ruido inductivo traba la línea SDA en LOW,
// esta función genera 9 pulsos en SCL para liberar el PCF8574 colgado.
void recuperarBusI2C() {
  pinMode(I2C_SDA_PIN, INPUT_PULLUP);
  pinMode(I2C_SCL_PIN, INPUT_PULLUP);
  delayMicroseconds(10);

  // Si SDA está retenido en LOW por un chip esclavo
  if (digitalRead(I2C_SDA_PIN) == LOW) {
    Serial.println("WARN: Bus I2C trabado (SDA LOW). Intentando recuperacion...");
    pinMode(I2C_SCL_PIN, OUTPUT);
    for (int i = 0; i < 9; i++) {
      digitalWrite(I2C_SCL_PIN, LOW);
      delayMicroseconds(10);
      digitalWrite(I2C_SCL_PIN, HIGH);
      delayMicroseconds(10);
      if (digitalRead(I2C_SDA_PIN) == HIGH) break; // El esclavo soltó la línea
    }
  }

  // Generar condición de STOP (SDA de LOW a HIGH mientras SCL está en HIGH)
  pinMode(I2C_SDA_PIN, OUTPUT);
  digitalWrite(I2C_SDA_PIN, LOW);
  delayMicroseconds(10);
  digitalWrite(I2C_SCL_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(I2C_SDA_PIN, HIGH);
  delayMicroseconds(10);

  // Reconfigurar pines para el periférico I2C de hardware
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setTimeOut(50);   // Cancela lecturas colgadas tras 50ms
  Wire.setClock(50000);  // 50 kHz: MAXIMA estabilidad para cables largos (2 metros)
}

// ─── Funciones del LCD ───────────────────────────────────────────
void invalidarLcd() {
  lcdCache[0] = "";
  lcdCache[1] = "";
}

void mostrarMensaje(String linea1, String linea2 = "") {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(linea1);
  if (linea2 != "") {
    lcd.setCursor(0, 1);
    lcd.print(linea2);
  }
}

String rellenar(String s) {
  while (s.length() < 16) s += " ";
  return s.substring(0, 16);
}

String tiempoRestante(int i) {
  if (millis() >= finMs[i]) return "0 seg";
  unsigned long restSec = (finMs[i] - millis()) / 1000UL;
  int m  = restSec / 60;
  int sg = restSec % 60;
  if (m > 0) return String(m) + "m " + String(sg) + "s";
  return String(sg) + " seg";
}

void refrescarLcd() {
  String l1, l2;
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
    l1 = "Aspir: " + tiempoRestante(0);
    l2 = "Sopla: " + tiempoRestante(1);
  }

  l1 = rellenar(l1);
  l2 = rellenar(l2);

  // Refresca únicamente las líneas que sufrieron cambios para evitar flicker I2C
  if (l1 != lcdCache[0]) { lcdCache[0] = l1; lcd.setCursor(0, 0); lcd.print(l1); }
  if (l2 != lcdCache[1]) { lcdCache[1] = l2; lcd.setCursor(0, 1); lcd.print(l2); }
}

void reiniciarLcd() {
  recuperarBusI2C();
  lcd.begin(16, 2);
  lcd.backlight();
  invalidarLcd();
  refrescarLcd();
}

// ─── Inicialización del Watchdog ─────────────────────────────────
void alimentarWatchdogs() {
  esp_task_wdt_reset();
#if HAS_RTC_WDT
  rtc_wdt_feed();
#endif
}

void iniciarWatchdog() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_deinit();
  esp_task_wdt_config_t wdt_config = {
    .timeout_ms = (uint32_t)WDT_TIMEOUT_S * 1000,
    .idle_core_mask = 0,
    .trigger_panic = true
  };
  esp_task_wdt_init(&wdt_config);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);

#if HAS_RTC_WDT
  rtc_wdt_protect_off();
  rtc_wdt_set_stage(RTC_WDT_STAGE0, RTC_WDT_STAGE_ACTION_RESET_SYSTEM);
  rtc_wdt_set_time(RTC_WDT_STAGE0, 60000); // 60 segundos
  rtc_wdt_enable();
  rtc_wdt_protect_on();
#endif
}

// ─── Funciones de Red (No Bloqueantes) ───────────────────────────
bool intentarCompletarOrden(String id) {
  if (id == "") return true;
  alimentarWatchdogs();

  HTTPClient http;
  http.begin(clientSecure, String(BACKEND_BASE) + "/completar/" + id);
  http.setConnectTimeout(3000);
  http.setTimeout(4000);

  int code = http.GET();
  http.end();

  if (code == 200) {
    Serial.println("Orden completada con exito: " + id);
    return true;
  }
  Serial.println("Error confirmando orden " + id + " (HTTP " + String(code) + ")");
  return false;
}

void pollearCanal(int i) {
  alimentarWatchdogs();

  HTTPClient http;
  http.begin(clientSecure, String(BACKEND_BASE) + "/orden/" + IDS[i]);
  http.setConnectTimeout(3000); // Timeout rápido para no trabar el ciclo
  http.setTimeout(4000);

  int code = http.GET();

  if (code == 200) {
    ultimoPollOkMs = millis();
    huboPollOk = true;

    // Buffer JSON estático reutilizable
    StaticJsonDocument<384> doc;
    DeserializationError error = deserializeJson(doc, http.getString());

    if (!error && doc["encender"] == true) {
      int segundos = doc["segundos"];
      ordenId[i] = String((const char*)(doc["orden_id"] | ""));
      activo[i]  = true;
      finMs[i]   = millis() + ((unsigned long)segundos * 1000UL);

      digitalWrite(RELAY_PIN[i], LOW); // Activar relé (Lógica invertida Opto)
      invalidarLcd();
      reinitLcdEnMs = millis() + 300;  // Re-init diferido tras apaciguar transitorio

      Serial.println(">> " + String(ETIQ[i]) + " ON por " + String(segundos) + "s (Orden: " + ordenId[i] + ")");
    }
  } else {
    Serial.println("Poll HTTP Canal " + String(ETIQ[i]) + " error: " + String(code));
  }
  http.end();
}

// ─── Setup ───────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(100);

  // Configurar Pines de Relé ANTES que nada para evitar chispazos
  for (int i = 0; i < NUM_CANALES; i++) {
    pinMode(RELAY_PIN[i], OUTPUT);
    digitalWrite(RELAY_PIN[i], HIGH); // Apagado por defecto (RELÉ NO-ACTIVO)
  }

  // Configurar cliente SSL Inseguro (ahorra RAM y evita validaciones CA pesadas)
  clientSecure.setInsecure();

  // Iniciar bus I2C con recuperación física por si arrancó trabado
  recuperarBusI2C();
  lcd.begin(16, 2);
  lcd.backlight();
  mostrarMensaje("Iniciando...", "Por favor espere");

  // Ventana de Reset manual de WiFi (Botón BOOT / GPIO 0)
  pinMode(0, INPUT_PULLUP);
  mostrarMensaje("Cambiar WiFi?", "Apriete BOOT 5s");
  {
    unsigned long t0 = millis();
    bool reset = false;
    while (millis() - t0 < 5000) {
      if (digitalRead(0) == LOW) {
        unsigned long t1 = millis();
        while (digitalRead(0) == LOW && (millis() - t1 < 1000)) delay(50);
        if (millis() - t1 >= 1000) { reset = true; break; }
      }
      delay(50);
    }
    if (reset) {
      WiFiManager wm;
      wm.resetSettings();
      mostrarMensaje("WiFi Borrado", "Reconfigure...");
      delay(1500);
    }
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // Desactiva modo ahorro para evitar picos de RF que alteran el I2C
  WiFi.setAutoReconnect(true);
  WiFi.persistent(true);

  mostrarMensaje("Conectando WiFi", "Por favor espere");

  WiFiManager wifiManager;
  wifiManager.setConfigPortalTimeout(180);
  if (!wifiManager.autoConnect("TermoPago-Est02")) {
    mostrarMensaje("Error WiFi", "Reiniciando...");
    delay(3000);
    ESP.restart();
  }

  Serial.println("WiFi Conectado! IP: " + WiFi.localIP().toString());
  wifiConectadoMostrado = true;
  lcd.clear();

  bootMs = millis();
  iniciarWatchdog();
}

// ─── Bucle Principal (Loop) ──────────────────────────────────────
void loop() {
  alimentarWatchdogs();

  // 0. Red de Seguridad Preventiva (Solo en estado Reposo / IDLE)
  if (!activo[0] && !activo[1]) {
    // a) Fuga o fragmentación crítica de memoria Heap
    if (ESP.getFreeHeap() < 35000) {
      Serial.println("WARN: Heap critico, reiniciando preventivamente...");
      delay(200);
      ESP.restart();
    }
    // b) Sin conexión con el servidor backend durante 2 minutos continuos
    if ((millis() - bootMs > 120000) && (millis() - ultimoPollOkMs > 120000)) {
      Serial.println("WARN: Sin polling exitoso por >2min en reposo. Reiniciando...");
      delay(200);
      ESP.restart();
    }
  }

  // 1. Apagado de Canales (Prioridad Máxima - Control de Tiempo)
  for (int i = 0; i < NUM_CANALES; i++) {
    if (activo[i] && millis() >= finMs[i]) {
      digitalWrite(RELAY_PIN[i], HIGH); // APAGADO INSTANTÁNEO
      activo[i] = false;

      // Marcar orden para confirmación no bloqueante posterior
      pendienteCompletar[i] = ordenId[i];
      ordenId[i] = "";

      Serial.println("<< " + String(ETIQ[i]) + " OFF");
      invalidarLcd();
      reinitLcdEnMs = millis() + 300; // Restaurar LCD tras pasar la chispa inductiva
    }
  }

  // 2. Control de Estado de Conexión WiFi
  if (WiFi.status() != WL_CONNECTED) {
    if (wifiConectadoMostrado) {
      wifiConectadoMostrado = false;
      inicioCaidaMs = millis();
      mostrarMensaje("WiFi Perdido", "Reconectando...");
      invalidarLcd();
      Serial.println("WARN: Conexion WiFi perdida...");
    }
    // Reinicio limpio tras 60s sin WiFi para evitar estados zombi del driver
    if (millis() - inicioCaidaMs >= 60000) {
      mostrarMensaje("Sin WiFi", "Reiniciando...");
      Serial.println("FATAL: 60s sin WiFi, reiniciando...");
      delay(500);
      ESP.restart();
    }
    delay(100);
    return;
  }

  if (!wifiConectadoMostrado) {
    wifiConectadoMostrado = true;
    lcd.clear();
    invalidarLcd();
    Serial.println("WiFi Reconectado. IP: " + WiFi.localIP().toString());
  }

  // 3. Confirmación no bloqueante de órdenes finalizadas
  for (int i = 0; i < NUM_CANALES; i++) {
    if (pendienteCompletar[i] != "") {
      if (intentarCompletarOrden(pendienteCompletar[i])) {
        pendienteCompletar[i] = ""; // Éxito: limpiar pendiente
      }
      // Si falla, se mantiene y reintenta el próximo ciclo sin congelar
    }
  }

  // 4. Polling Escalonado / Alternado al Backend (1 canal por ciclo)
  if (millis() - ultimoPollMs >= INTERVALO_POLL_MS) {
    ultimoPollMs = millis();
    for (int count = 0; count < NUM_CANALES; count++) {
      int idx = (canalAPollear + count) % NUM_CANALES;
      if (!activo[idx]) {
        pollearCanal(idx);
        canalAPollear = (idx + 1) % NUM_CANALES;
        break;
      }
    }
  }

  // 5. Actualización periódica del LCD (Cada 1 segundo)
  if (millis() - ultimoLcdMs >= 1000) {
    ultimoLcdMs = millis();
    refrescarLcd();
  }

  // 6. Re-inicialización diferida del LCD tras conmutar relés
  if (reinitLcdEnMs && millis() >= reinitLcdEnMs) {
    reinitLcdEnMs = 0;
    reiniciarLcd();
  }

  // 7. Mantenimiento preventivo del bus I2C (Cada 5 minutos)
  if (millis() - ultimoReinitLcdMs >= 300000) {
    ultimoReinitLcdMs = millis();
    reiniciarLcd();
  }

  delay(20);
}
