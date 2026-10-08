// Arduino falso: lo minimo para compilar dispensador.h en la PC y poder
// manejar el reloj y los pines a mano desde la prueba.
#pragma once
#include <cstdint>
#include <cstdio>
#include <map>
#define IRAM_ATTR
#define OUTPUT 1
#define INPUT 0
#define INPUT_PULLUP 2
#define LOW 0
#define HIGH 1
#define FALLING 2
#define RISING 3
extern uint32_t _reloj;
extern std::map<int,int> _pines;
extern std::map<int,int> _modos;
inline uint32_t millis() { return _reloj; }
inline void pinMode(int p, int m) { _modos[p] = m; }
inline void digitalWrite(int p, int v) { _pines[p] = v; }
inline int  digitalRead(int p) { return _pines.count(p) ? _pines[p] : 1; }
inline int  digitalPinToInterrupt(int p) { return p; }
inline void attachInterrupt(int, void(*)(), int) {}
inline void noInterrupts() {}
inline void interrupts() {}
struct _Serial {
  void printf(const char* f, ...) {}
  void println(const char*) {}
};
extern _Serial Serial;
