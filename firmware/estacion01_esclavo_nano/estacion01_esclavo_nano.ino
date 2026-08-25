// =====================================================================
// TermoPago - ESTACION 01 - ESCLAVO (Arduino Nano / ATmega328)
// =====================================================================
// Maneja el RELE, el TIEMPO del servicio y el CONTADOR en pantalla.
// Recibe ordenes del MAESTRO (ESP32) por UART. NO hace WiFi.
// RELE ACTIVO EN LOW (modulos comunes): LOW = encendido, HIGH = apagado.
//
// CONEXIONES:
//   Del maestro:  ESP32 GPIO17 (TX2)  ->  Nano D3 (RX)
//                 GND del ESP32  <->  GND del Nano   (OBLIGATORIO)
//                 (el ESP32 es 3.3V y el Nano lo lee como HIGH: va directo)
//   Reles (modulos comunes, activos en LOW):
//                 D5 -> canal 0 (aspiradora)
//                 D6 -> canal 1 (soplador)
//   LCD I2C:      SDA -> A4,  SCL -> A5,  VCC -> 5V,  GND -> GND
//   Alimentacion: 5V al pin 5V del Nano (GND comun con todo)
// =====================================================================

#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>
#include <SoftwareSerial.h>

#define RX_PIN 3       // viene del TX del maestro
#define TX_PIN 4       // sin uso (SoftwareSerial pide un pin TX)

const byte NUM = 2;
const byte RELAY_PIN[NUM] = {5, 6};   // ACTIVOS EN LOW

SoftwareSerial link(RX_PIN, TX_PIN);
hd44780_I2Cexp lcd;

bool          activo[NUM]     = {false, false};
unsigned long finMs[NUM]      = {0, 0};
char          lastOrden[NUM][16] = {"", ""};

char idle1[17] = "TermoPago";
char idle2[17] = "Iniciando...";
char cache0[17] = "";
char cache1[17] = "";
char buf[48];
byte bufLen = 0;
unsigned long ultimoRxMs = 0;
unsigned long ultimoLcdMs = 0;
unsigned long ultimoReinitMs = 0;

void escribir(byte fila, const char* txt) {
  char linea[17];
  byte i = 0;
  for (; i < 16 && txt[i]; i++) linea[i] = txt[i];
  for (; i < 16; i++) linea[i] = ' ';
  linea[16] = 0;
  char* cache = (fila == 0) ? cache0 : cache1;
  if (strcmp(linea, cache) != 0) {
    strcpy(cache, linea);
    lcd.setCursor(0, fila);
    lcd.print(linea);
  }
}

void tiempoRestante(byte i, char* out) {
  unsigned long rest = 0;
  if (millis() < finMs[i]) rest = (finMs[i] - millis()) / 1000UL;
  int m = rest / 60, sg = rest % 60;
  if (m > 0) sprintf(out, "%dm %ds", m, sg);
  else sprintf(out, "%d seg", sg);
}

void refrescar() {
  char l1[17], l2[17], tr[10];
  if (activo[0] && !activo[1])      { strcpy(l1, "Aspiradora"); tiempoRestante(0, tr); sprintf(l2, "Quedan %s", tr); }
  else if (!activo[0] && activo[1]) { strcpy(l1, "Soplador");   tiempoRestante(1, tr); sprintf(l2, "Quedan %s", tr); }
  else if (activo[0] && activo[1])  { tiempoRestante(0, tr); sprintf(l1, "Aspir: %s", tr); tiempoRestante(1, tr); sprintf(l2, "Sopla: %s", tr); }
  else {
    if (millis() - ultimoRxMs > 20000) { strcpy(l1, "Sin datos"); strcpy(l2, "revisar enlace"); }
    else { strcpy(l1, idle1); strcpy(l2, idle2); }
  }
  escribir(0, l1);
  escribir(1, l2);
}

void activar(int canal, long seg, const char* orden) {
  if (canal < 0 || canal >= (int)NUM) return;
  if (orden[0] && strcmp(orden, lastOrden[canal]) == 0) return;   // mismo pago: ignorar
  strncpy(lastOrden[canal], orden, 15); lastOrden[canal][15] = 0;
  activo[canal] = true;
  finMs[canal] = millis() + (unsigned long)seg * 1000UL;
  digitalWrite(RELAY_PIN[canal], LOW);     // ENCENDER (activo en LOW)
  Serial.print(F("ON canal ")); Serial.print(canal); Serial.print(' '); Serial.print(seg); Serial.println('s');
}

void procesarLinea(char* s) {
  if (!s[0]) return;
  Serial.print(F("RX: ")); Serial.println(s);
  char c = s[0];
  if (c == '1')      { strncpy(idle1, s + 1, 16); idle1[16] = 0; }
  else if (c == '2') { strncpy(idle2, s + 1, 16); idle2[16] = 0; }
  else if (c == 'A') {                       // A,canal,seg,orden
    char* p = s + 1;
    if (*p == ',') p++;
    char* canalS = strtok(p, ",");
    char* segS   = strtok(NULL, ",");
    char* ordenS = strtok(NULL, ",");
    int  canal = canalS ? atoi(canalS) : -1;
    long seg   = segS   ? atol(segS)   : 0;
    activar(canal, seg, ordenS ? ordenS : "");
  }
}

// Recuperacion fisica del bus I2C (9 pulsos) por si el ruido lo traba
void recuperarBusI2C() {
  pinMode(A4, INPUT_PULLUP);  // SDA
  pinMode(A5, INPUT_PULLUP);  // SCL
  delayMicroseconds(10);
  if (digitalRead(A4) == LOW) {
    pinMode(A5, OUTPUT);
    for (byte i = 0; i < 9; i++) {
      digitalWrite(A5, LOW);  delayMicroseconds(10);
      digitalWrite(A5, HIGH); delayMicroseconds(10);
      if (digitalRead(A4) == HIGH) break;
    }
  }
  Wire.begin();
}

void reiniciarLcd() {
  recuperarBusI2C();
  lcd.begin(16, 2);
  lcd.backlight();
  cache0[0] = 0; cache1[0] = 0;   // fuerza redibujo
}

void setup() {
  for (byte i = 0; i < NUM; i++) { pinMode(RELAY_PIN[i], OUTPUT); digitalWrite(RELAY_PIN[i], HIGH); } // apagados (HIGH = off)
  Wire.begin();
#if defined(WIRE_HAS_TIMEOUT)
  Wire.setWireTimeout(25000, true);   // corta el I2C si se traba (no cuelga el Nano)
#endif
  // Reintenta iniciar el LCD hasta que responda
  for (byte i = 0; i < 10; i++) { if (lcd.begin(16, 2) == 0) break; delay(200); }
  lcd.backlight();
  escribir(0, "TermoPago");
  escribir(1, "Iniciando...");
  link.begin(9600);
  Serial.begin(115200);
  Serial.println(F("TermoPago Nano - esclavo listo"));
  ultimoRxMs = millis();
}

void loop() {
  // 1. Leer ordenes del maestro
  while (link.available()) {
    char c = link.read();
    ultimoRxMs = millis();
    if (c == '\n') { buf[bufLen] = 0; procesarLinea(buf); bufLen = 0; }
    else if (c != '\r') { if (bufLen < sizeof(buf) - 1) buf[bufLen++] = c; else bufLen = 0; }
  }

  // 2. Apagar canales cuyo tiempo termino
  for (byte i = 0; i < NUM; i++) {
    if (activo[i] && millis() >= finMs[i]) {
      digitalWrite(RELAY_PIN[i], HIGH);     // APAGAR (activo en LOW)
      activo[i] = false;
      Serial.print(F("OFF canal ")); Serial.println(i);
    }
  }

  // 3. Contador / display cada 500ms
  if (millis() - ultimoLcdMs >= 500) { ultimoLcdMs = millis(); refrescar(); }

  // 4. Reinit periodico del LCD (cada 5 min, recupera del ruido)
  if (millis() - ultimoReinitMs >= 300000UL) { ultimoReinitMs = millis(); reiniciarLcd(); }
}
