// =====================================================================
// TermoPago - ESTACION 02 - ESCLAVO / DISPLAY (ESP32)
// =====================================================================
// Recibe por UART (Serial2, RX=GPIO16) las 2 lineas del LCD que le manda
// el MAESTRO, y las dibuja. NO tiene WiFi ni logica: solo muestra.
// Incluye recuperacion fisica del bus I2C + watchdog. Si el ruido lo
// cuelga, se reinicia solo; el cobro sigue en el maestro.
//
// CONEXIONES:
//   Enlace:  MAESTRO GPIO17 (TX2)  ->  ESCLAVO GPIO16 (RX2)
//            GND comun entre las dos placas (OBLIGATORIO)
//   LCD I2C: SDA -> GPIO21   SCL -> GPIO22   VCC -> 5V   GND -> GND
// =====================================================================

#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>
#include <esp_task_wdt.h>

#if __has_include("rtc_wdt.h")
  #include "rtc_wdt.h"
  #define HAS_RTC_WDT 1
#else
  #define HAS_RTC_WDT 0
#endif

const int I2C_SDA_PIN = 21;
const int I2C_SCL_PIN = 22;
const int WDT_TIMEOUT_S = 30;

hd44780_I2Cexp lcd;
String cache[2] = {"", ""};
String buf = "";
unsigned long ultimoRxMs = 0;
unsigned long ultimoReinitMs = 0;
bool sinDatos = false;

// Recuperacion fisica del bus I2C (9 pulsos en SCL) por si el ruido
// deja la linea SDA trabada en LOW.
void recuperarBusI2C() {
  pinMode(I2C_SDA_PIN, INPUT_PULLUP);
  pinMode(I2C_SCL_PIN, INPUT_PULLUP);
  delayMicroseconds(10);
  if (digitalRead(I2C_SDA_PIN) == LOW) {
    pinMode(I2C_SCL_PIN, OUTPUT);
    for (int i = 0; i < 9; i++) {
      digitalWrite(I2C_SCL_PIN, LOW);  delayMicroseconds(10);
      digitalWrite(I2C_SCL_PIN, HIGH); delayMicroseconds(10);
      if (digitalRead(I2C_SDA_PIN) == HIGH) break;
    }
  }
  pinMode(I2C_SDA_PIN, OUTPUT);
  digitalWrite(I2C_SDA_PIN, LOW);  delayMicroseconds(10);
  digitalWrite(I2C_SCL_PIN, HIGH); delayMicroseconds(10);
  digitalWrite(I2C_SDA_PIN, HIGH); delayMicroseconds(10);
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setTimeOut(50);
  Wire.setClock(50000);
}

void escribir(int fila, String txt) {
  while (txt.length() < 16) txt += " ";
  txt = txt.substring(0, 16);
  if (txt != cache[fila]) {
    cache[fila] = txt;
    lcd.setCursor(0, fila);
    lcd.print(txt);
  }
}

void reiniciarLcd() {
  recuperarBusI2C();
  lcd.begin(16, 2);
  lcd.backlight();
  cache[0] = ""; cache[1] = "";
}

void iniciarWatchdog() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_deinit();
  esp_task_wdt_config_t cfg = { .timeout_ms = (uint32_t)WDT_TIMEOUT_S * 1000, .idle_core_mask = 0, .trigger_panic = true };
  esp_task_wdt_init(&cfg);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);
#if HAS_RTC_WDT
  rtc_wdt_protect_off();
  rtc_wdt_set_stage(RTC_WDT_STAGE0, RTC_WDT_STAGE_ACTION_RESET_SYSTEM);
  rtc_wdt_set_time(RTC_WDT_STAGE0, 20000);
  rtc_wdt_enable();
  rtc_wdt_protect_on();
#endif
}

void alimentarWdt() {
  esp_task_wdt_reset();
#if HAS_RTC_WDT
  rtc_wdt_feed();
#endif
}

void setup() {
  Serial.begin(115200);
  Serial2.begin(9600, SERIAL_8N1, 16, 17);   // RX=GPIO16 (del maestro), TX=17 sin uso
  recuperarBusI2C();
  lcd.begin(16, 2);
  lcd.backlight();
  lcd.setCursor(0, 0); lcd.print("TermoPago");
  lcd.setCursor(0, 1); lcd.print("Iniciando...");
  ultimoRxMs = millis();
  iniciarWatchdog();
}

void procesarLinea(String s) {
  if (s.length() < 1) return;
  char c = s.charAt(0);
  String txt = s.substring(1);
  if (c == '1') escribir(0, txt);
  else if (c == '2') escribir(1, txt);
}

void loop() {
  alimentarWdt();

  // 1. Leer lo que manda el maestro
  while (Serial2.available()) {
    char c = Serial2.read();
    ultimoRxMs = millis();
    if (sinDatos) { sinDatos = false; cache[0] = ""; cache[1] = ""; }
    if (c == '\n') { procesarLinea(buf); buf = ""; }
    else if (c != '\r') { buf += c; if (buf.length() > 40) buf = ""; }
  }

  // 2. Reinit periodico del LCD (recupera del ruido, cada 5 min)
  if (millis() - ultimoReinitMs > 300000) { ultimoReinitMs = millis(); reiniciarLcd(); }

  // 3. Si hace >6s que no llega nada -> avisar (cable/maestro)
  if (!sinDatos && millis() - ultimoRxMs > 6000) {
    sinDatos = true;
    escribir(0, "Sin datos");
    escribir(1, "revisar enlace");
  }

  delay(10);
}
