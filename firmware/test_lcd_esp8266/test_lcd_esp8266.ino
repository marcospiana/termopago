// =====================================================================
// TEST de diagnostico - ESP8266 + LCD I2C  (SOLO la placa y el LCD)
// No necesita el maestro ni nada mas. Sirve para saber si el ESP8266
// realmente le habla al LCD.
//
// CONEXIONES del LCD:  SDA -> D2 (GPIO4)   SCL -> D1 (GPIO5)
//                      VCC -> 5V           GND -> GND
//
// USO: flashealo, abri el Monitor Serie a 115200 y mira lo que imprime.
// =====================================================================

#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>

hd44780_I2Cexp lcd;

void setup() {
  Serial.begin(115200);
  delay(600);
  Serial.println();
  Serial.println("=== TEST LCD ESP8266 ===");

  Wire.begin(4, 5);   // SDA=D2(GPIO4), SCL=D1(GPIO5)

  // 1) Escaner I2C: busca cualquier dispositivo en el bus
  Serial.println("Escaneando bus I2C...");
  byte encontrados = 0;
  for (byte addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.print("  -> Dispositivo I2C en 0x");
      if (addr < 16) Serial.print("0");
      Serial.println(addr, HEX);
      encontrados++;
    }
  }
  if (encontrados == 0) {
    Serial.println("  !! NADA en el bus I2C.");
    Serial.println("     -> Revisar: SDA a D2, SCL a D1 (probar cruzarlos),");
    Serial.println("        VCC a 5V, GND, y que los cables hagan buen contacto.");
  } else {
    Serial.print("  OK: "); Serial.print(encontrados); Serial.println(" dispositivo(s) en el bus.");
  }

  // 2) Probar el LCD y escribir texto
  int st = lcd.begin(16, 2);
  if (st != 0) {
    Serial.print("lcd.begin() FALLO, codigo = "); Serial.println(st);
    Serial.println("  -> El LCD no respondio. Casi seguro cableado I2C.");
  } else {
    Serial.println("lcd.begin() OK -> escribiendo en el display");
    lcd.backlight();
    lcd.setCursor(0, 0); lcd.print("TEST OK!");
    lcd.setCursor(0, 1); lcd.print("Hola TermoPago");
  }
  Serial.println("=== fin del test ===");
}

void loop() {}
