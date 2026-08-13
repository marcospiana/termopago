// =====================================================================
// TEST - Escaner de redes WiFi 2.4GHz (para elegir a cual clavar el ESP)
// =====================================================================
// Flashealo en el ESP32 (maestro) EN LA ESTACION 02, abri el Monitor Serie
// a 115200, y anota de TU red: el BSSID, el Canal y el RSSI.
// (El ESP32 solo ve 2.4GHz, asi que todo lo que aparece es 2.4GHz.)
// =====================================================================

#include <WiFi.h>

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(300);
}

void loop() {
  Serial.println("\n================ Escaneando 2.4GHz ================");
  int n = WiFi.scanNetworks();
  if (n == 0) {
    Serial.println("No se ven redes.");
  } else {
    for (int i = 0; i < n; i++) {
      Serial.print(i + 1);
      Serial.print(") SSID: ");
      Serial.print(WiFi.SSID(i));
      Serial.print("  | BSSID: ");
      Serial.print(WiFi.BSSIDstr(i));       // <- esta es la MAC a clavar
      Serial.print("  | Canal: ");
      Serial.print(WiFi.channel(i));
      Serial.print("  | RSSI: ");
      Serial.print(WiFi.RSSI(i));
      Serial.println(" dBm");
    }
  }
  Serial.println("(cuanto mas cerca de 0 el RSSI, mejor senal. Ej: -55 es bueno, -85 es malo)");
  delay(8000);
}
