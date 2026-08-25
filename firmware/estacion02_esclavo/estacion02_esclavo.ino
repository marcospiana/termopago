// =====================================================================
// TermoPago - ESTACION 02 - ESCLAVO (ESP32)
// =====================================================================
// Este ESP maneja el RELE, el TIEMPO del servicio y el CONTADOR en pantalla.
// Recibe ordenes del MAESTRO por UART. NO hace WiFi -> casi nunca se reinicia,
// asi que un servicio pago sigue corriendo aunque el maestro pierda conexion
// o se reinicie. El contador tambien es de este ESP (sobrevive todo eso).
//
// PROTOCOLO (recibe del maestro):
//   "1<texto>"            -> linea 1 del display en REPOSO
//   "2<texto>"            -> linea 2 del display en REPOSO
//   "A,<canal>,<seg>,<orden>" -> ACTIVAR canal por <seg> segundos
//
// CONEXIONES:
//   Enlace:  MAESTRO GPIO17 (TX2)  ->  ESCLAVO GPIO16 (RX2)
//            GND comun entre las placas (OBLIGATORIO)
//   Reles:   GPIO25 (aspiradora)   GPIO26 (soplador)   (activos en LOW)
//   LCD I2C: SDA -> GPIO21   SCL -> GPIO22   VCC -> 5V   GND -> GND
// =====================================================================

#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>
#include <esp_task_wdt.h>

const int I2C_SDA_PIN = 21;
const int I2C_SCL_PIN = 22;
const int WDT_TIMEOUT_S = 20;

const int NUM = 2;
const char* NOMBRES[NUM]  = {"Aspiradora", "Soplador"};
const int   RELAY_PIN[NUM] = {25, 26};   // activos en LOW

hd44780_I2Cexp lcd;

bool          activo[NUM]   = {false, false};
unsigned long finMs[NUM]    = {0, 0};
String        lastOrden[NUM] = {"", ""};   // evita re-activar el mismo pago

String idle1 = "TermoPago", idle2 = "Iniciando...";
String cache[2] = {"", ""};
String buf = "";
unsigned long ultimoRxMs = 0;
unsigned long ultimoLcdMs = 0;
unsigned long ultimoReinitMs = 0;
unsigned long reinitLcdEnMs = 0;

// ── Recuperacion fisica del bus I2C (9 pulsos) ───────────────────
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

void invalidarLcd() { cache[0] = ""; cache[1] = ""; }

void reiniciarLcd() {
  recuperarBusI2C();
  lcd.begin(16, 2);
  lcd.backlight();
  invalidarLcd();
}

String tiempoRestante(int i) {
  if (millis() >= finMs[i]) return "0 seg";
  unsigned long r = (finMs[i] - millis()) / 1000UL;
  int m = r / 60, sg = r % 60;
  if (m > 0) return String(m) + "m " + String(sg) + "s";
  return String(sg) + " seg";
}

void refrescar() {
  String l1, l2;
  if (activo[0] && !activo[1])      { l1 = NOMBRES[0]; l2 = "Quedan " + tiempoRestante(0); }
  else if (!activo[0] && activo[1]) { l1 = NOMBRES[1]; l2 = "Quedan " + tiempoRestante(1); }
  else if (activo[0] && activo[1])  { l1 = "Aspir: " + tiempoRestante(0); l2 = "Sopla: " + tiempoRestante(1); }
  else {
    // reposo: texto del maestro; si hace rato que no llega, avisar
    if (millis() - ultimoRxMs > 20000) { l1 = "Sin datos"; l2 = "revisar enlace"; }
    else { l1 = idle1; l2 = idle2; }
  }
  escribir(0, l1);
  escribir(1, l2);
}

void activar(int canal, long seg, String orden) {
  if (canal < 0 || canal >= NUM) return;
  if (orden.length() > 0 && orden == lastOrden[canal]) return;  // mismo pago: ignorar
  lastOrden[canal] = orden;
  activo[canal] = true;
  finMs[canal] = millis() + (unsigned long)seg * 1000UL;
  digitalWrite(RELAY_PIN[canal], LOW);      // ENCENDER
  invalidarLcd();
  reinitLcdEnMs = millis() + 250;           // reinit del LCD tras el pico del rele
}

void procesarLinea(String s) {
  if (s.length() < 1) return;
  char c = s.charAt(0);
  if (c == '1') { idle1 = s.substring(1); }
  else if (c == '2') { idle2 = s.substring(1); }
  else if (c == 'A') {                       // A,canal,seg,orden
    int c1 = s.indexOf(','), c2 = s.indexOf(',', c1 + 1), c3 = s.indexOf(',', c2 + 1);
    if (c1 > 0 && c2 > c1 && c3 > c2) {
      int canal = s.substring(c1 + 1, c2).toInt();
      long seg  = s.substring(c2 + 1, c3).toInt();
      String orden = s.substring(c3 + 1);
      activar(canal, seg, orden);
    }
  }
}

void iniciarWatchdog() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_config_t cfg = { .timeout_ms = (uint32_t)WDT_TIMEOUT_S * 1000, .idle_core_mask = 0, .trigger_panic = true };
  esp_task_wdt_reconfigure(&cfg);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < NUM; i++) { pinMode(RELAY_PIN[i], OUTPUT); digitalWrite(RELAY_PIN[i], HIGH); } // apagados
  Serial2.begin(9600, SERIAL_8N1, 16, 17);   // RX=GPIO16 (del maestro), TX=17 sin uso
  delay(500);   // deja estabilizar la alimentacion y el LCD tras encender
  // Reintenta iniciar el LCD hasta que responda (el init es intermitente al arrancar)
  bool lcdOk = false;
  for (int i = 0; i < 12 && !lcdOk; i++) {
    recuperarBusI2C();
    if (lcd.begin(16, 2) == 0) lcdOk = true;
    else delay(300);
  }
  lcd.backlight();
  escribir(0, "TermoPago");
  escribir(1, "Iniciando...");
  ultimoRxMs = millis();
  iniciarWatchdog();
}

void loop() {
  esp_task_wdt_reset();

  // 1. Leer ordenes del maestro
  while (Serial2.available()) {
    char c = Serial2.read();
    ultimoRxMs = millis();
    if (c == '\n') { procesarLinea(buf); buf = ""; }
    else if (c != '\r') { buf += c; if (buf.length() > 60) buf = ""; }
  }

  // 2. Apagar canales cuyo tiempo termino
  for (int i = 0; i < NUM; i++) {
    if (activo[i] && millis() >= finMs[i]) {
      digitalWrite(RELAY_PIN[i], HIGH);      // APAGAR
      activo[i] = false;
      invalidarLcd();
      reinitLcdEnMs = millis() + 250;
    }
  }

  // 3. Contador / display cada 500ms
  if (millis() - ultimoLcdMs >= 500) { ultimoLcdMs = millis(); refrescar(); }

  // 4. Reinit del LCD diferido (tras conmutar el rele)
  if (reinitLcdEnMs && millis() >= reinitLcdEnMs) { reinitLcdEnMs = 0; reiniciarLcd(); refrescar(); }

  // 5. Reinit periodico de seguridad (cada 5 min)
  if (millis() - ultimoReinitMs > 300000) { ultimoReinitMs = millis(); reiniciarLcd(); }

  delay(10);
}
