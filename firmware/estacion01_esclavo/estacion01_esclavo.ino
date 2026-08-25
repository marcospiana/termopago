// =====================================================================
// TermoPago - ESTACION 01 - ESCLAVO (NodeMCU ESP8266)
// =====================================================================
// Maneja el RELE, el TIEMPO del servicio y el CONTADOR en pantalla.
// Recibe ordenes del MAESTRO (ESP32) por UART. NO hace WiFi -> un servicio
// pago sigue corriendo aunque el maestro pierda conexion o se reinicie.
//
// PROTOCOLO (recibe del maestro):
//   "1<texto>" / "2<texto>"       -> lineas del display en REPOSO
//   "A,<canal>,<seg>,<orden>"     -> ACTIVAR canal por <seg> segundos
//
// CONEXIONES:
//   Enlace:  MAESTRO (ESP32) GPIO17 (TX2)  ->  ESCLAVO RX = D7 (GPIO13)
//            GND comun entre las placas (OBLIGATORIO)
//   Reles:   D5 (GPIO14) aspiradora   D6 (GPIO12) soplador   (activos en LOW)
//   LCD I2C: SDA -> D2 (GPIO4)   SCL -> D1 (GPIO5)   VCC -> 5V   GND -> GND
// =====================================================================

#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>
#include <SoftwareSerial.h>

#define SDA_PIN 4     // D2
#define SCL_PIN 5     // D1
#define RX_PIN  13    // D7  (viene del TX del maestro)

const int NUM = 2;
const char* NOMBRES[NUM]   = {"Aspiradora", "Soplador"};
const int   RELAY_PIN[NUM] = {14, 12};    // D5, D6  (activos en LOW)

SoftwareSerial link(RX_PIN);   // solo RX
hd44780_I2Cexp lcd;

bool          activo[NUM]    = {false, false};
unsigned long finMs[NUM]     = {0, 0};
String        lastOrden[NUM] = {"", ""};

String idle1 = "TermoPago", idle2 = "Iniciando...";
String cache[2] = {"", ""};
String buf = "";
unsigned long ultimoRxMs = 0;
unsigned long ultimoLcdMs = 0;
unsigned long ultimoReinitMs = 0;
unsigned long reinitLcdEnMs = 0;

void recuperarBusI2C() {
  pinMode(SDA_PIN, INPUT_PULLUP);
  pinMode(SCL_PIN, INPUT_PULLUP);
  delayMicroseconds(10);
  if (digitalRead(SDA_PIN) == LOW) {
    pinMode(SCL_PIN, OUTPUT);
    for (int i = 0; i < 9; i++) {
      digitalWrite(SCL_PIN, LOW);  delayMicroseconds(10);
      digitalWrite(SCL_PIN, HIGH); delayMicroseconds(10);
      if (digitalRead(SDA_PIN) == HIGH) break;
    }
  }
  pinMode(SDA_PIN, OUTPUT);
  digitalWrite(SDA_PIN, LOW);  delayMicroseconds(10);
  digitalWrite(SCL_PIN, HIGH); delayMicroseconds(10);
  digitalWrite(SDA_PIN, HIGH); delayMicroseconds(10);
  Wire.begin(SDA_PIN, SCL_PIN);
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
  digitalWrite(RELAY_PIN[canal], LOW);       // ENCENDER
  invalidarLcd();
  reinitLcdEnMs = millis() + 250;
}

void procesarLinea(String s) {
  if (s.length() < 1) return;
  char c = s.charAt(0);
  if (c == '1') { idle1 = s.substring(1); }
  else if (c == '2') { idle2 = s.substring(1); }
  else if (c == 'A') {
    int c1 = s.indexOf(','), c2 = s.indexOf(',', c1 + 1), c3 = s.indexOf(',', c2 + 1);
    if (c1 > 0 && c2 > c1 && c3 > c2) {
      int canal = s.substring(c1 + 1, c2).toInt();
      long seg  = s.substring(c2 + 1, c3).toInt();
      String orden = s.substring(c3 + 1);
      activar(canal, seg, orden);
    }
  }
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < NUM; i++) { pinMode(RELAY_PIN[i], OUTPUT); digitalWrite(RELAY_PIN[i], HIGH); } // apagados
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
  link.begin(9600);
  ultimoRxMs = millis();
}

void loop() {
  // 1. Leer ordenes del maestro
  while (link.available()) {
    char c = link.read();
    ultimoRxMs = millis();
    if (c == '\n') { procesarLinea(buf); buf = ""; }
    else if (c != '\r') { buf += c; if (buf.length() > 60) buf = ""; }
  }

  // 2. Apagar canales cuyo tiempo termino
  for (int i = 0; i < NUM; i++) {
    if (activo[i] && millis() >= finMs[i]) {
      digitalWrite(RELAY_PIN[i], HIGH);        // APAGAR
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

  yield();   // alimenta el watchdog del ESP8266
}
