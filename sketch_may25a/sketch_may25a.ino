#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <ArduinoJson.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>
#include <Wire.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>
#include <EEPROM.h>

const char* BACKEND_URL = "https://web-production-94bbab.up.railway.app/orden/termo_001";
const int   RELAY_PIN   = D1;

hd44780_I2Cexp lcd;
WiFiClientSecure client;

void checkResetWifi() {
  EEPROM.begin(512);
  int contador = EEPROM.read(0);
  contador++;
  EEPROM.write(0, contador);
  EEPROM.commit();

  Serial.printf("Reinicios rápidos: %d\n", contador);

  if (contador >= 3) {
    // Resetear contador
    EEPROM.write(0, 0);
    EEPROM.commit();

    // Borrar configuración WiFi
    WiFiManager wifiManager;
    wifiManager.resetSettings();

    lcd.begin(16, 2);
    lcd.backlight();
    lcd.setCursor(0, 0);
    lcd.print("WiFi reseteado!");
    lcd.setCursor(0, 1);
    lcd.print("Conecta TermoPago");
    Serial.println("WiFi reseteado — abriendo portal...");
    delay(2000);
  }

  // Si pasan 10 segundos sin otro reset, reiniciamos el contador
  delay(10000);
  EEPROM.write(0, 0);
  EEPROM.commit();
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, HIGH);

  Wire.begin(D2, D3);
  lcd.begin(16, 2);
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print("Conectando WiFi");
  lcd.setCursor(0, 1);
  lcd.print("Por favor espere");

  checkResetWifi();

  WiFiManager wifiManager;

  if (!wifiManager.autoConnect("TermoPago")) {
    Serial.println("Error al conectar, reiniciando...");
    delay(3000);
    ESP.restart();
  }

  Serial.println("\n✅ WiFi conectado");
  client.setInsecure();

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("WiFi conectado!");
  lcd.setCursor(0, 1);
  lcd.print("Escanee el QR  ");
}

void activarRele(int segundos) {
  Serial.printf("⚡ Relé ON por %d segundo(s)\n", segundos);
  digitalWrite(RELAY_PIN, LOW);

  for (int i = segundos; i > 0; i--) {
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Calentando agua");
    lcd.setCursor(0, 1);
    lcd.print("Tiempo: ");
    lcd.print(i);
    lcd.print(" seg   ");
    delay(1000);
  }

  digitalWrite(RELAY_PIN, HIGH);
  Serial.println("🔴 Relé OFF");

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("   Listo!      ");
  lcd.setCursor(0, 1);
  lcd.print("   Gracias!    ");
  delay(10000);

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("WiFi conectado!");
  lcd.setCursor(0, 1);
  lcd.print("Escanee el QR  ");
}

void loop() {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(client, BACKEND_URL);
    int code = http.GET();

    if (code == 200) {
      StaticJsonDocument<200> doc;
      deserializeJson(doc, http.getString());
      if (doc["encender"] == true) {
        activarRele(doc["segundos"]);
      }
    }
    http.end();
  }
  delay(3000);
}