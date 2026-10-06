/*  ============================================================================
    TermoPago — dispensador.h
    Entrega de fichas en la placa ELECTROLACER, de a UNA ficha por ciclo.
    ----------------------------------------------------------------------------
    QUE HACE
    Cada ficha es un ciclo completo e independiente:

        pulso COIN  ->  pausa  ->  espera que acredite  ->  pulso BOTON
                                                              |     (opcional)
                                              espera el pulso del SENSOR
                                                              |
                                                      ficha entregada

    El paso del BOTON se saca con DISP_USAR_BOTON 0 si la placa entrega sola al
    acreditar (ver esa constante: hay una prueba de 30 segundos para saberlo).

    POR QUE DE A UNA Y NO "CARGAR TODO Y APRETAR"
    La placa acumula credito pero NO entrega sola: hay que apretar el boton.
    Nunca se supo si una pulsacion entrega UNA ficha o DRENA todo el credito
    cargado. Cargando credito para una sola ficha por vez, esa pregunta deja de
    importar: con credito para una, las dos respuestas dan lo mismo. Y si algo
    falla en la ficha 3 de 5, queda varado el precio de UNA ficha, no de tres.

    CREDITO VARADO
    Si el pulso de COIN entra pero la ficha no sale (hopper vacio o atascado),
    queda credito cargado en la maquina y el proximo que pase aprieta el boton
    fisico y se lleva una ficha gratis. Por eso el SENSOR no es opcional: es lo
    unico que detecta ese caso. Se cuenta en creditoVarado() para que la capa
    MQTT lo reporte al backend.

    NO BLOQUEA
    Todo avanza por millis() dentro de actualizar(). El loop principal sigue
    corriendo: MQTT vivo, watchdog alimentado, LCD refrescandose.

    CABLEADO (ver el doc del proyecto)
      COIN   -> rele: COM al pin GND del conector de 3 pines, NO al pin COIN.
                El pulso es a GND (activo bajo), medido: 9 V en reposo.
      BOTON  -> rele en PARALELO con el pulsador fisico que ya esta.
                Contacto seco: hace lo mismo que la persona al apretar.
      SENSOR -> opto PC817 desde la linea SENSOR de la placa.
      Los reles dan aislacion galvanica: no hace falta unir masas.
      El ESP con fuente propia. NO colgarlo del pin de 12 V de ese conector:
      es el mismo riel del rele del hopper y el arranque del motor hunde la
      tension justo cuando hay que confirmar el pago.

    ANTES DE USARLO, EN LA PLACA
      ENTRADA COIN = $0  (DIP 1 y 2 ON -> RESET -> boton 2 hasta $0 -> DIP OFF
      -> RESET). Con eso 1 pulso = 1 ficha. Si queda en $100 harian falta 30
      pulsos por ficha. Despues del cambio, meter un billete y confirmar que el
      billetero sigue acreditando bien.

    COMO SE USA (ver INTEGRACION al final del archivo)
      Dispensador dispensador;
      setup():  dispensador.begin();
      loop():   dispensador.actualizar();
      al pagar: dispensador.pedir(1);
    ============================================================================ */

#ifndef DISPENSADOR_H
#define DISPENSADOR_H

#include <Arduino.h>

// ---------------------------------------------------------------------------
//  CONFIGURACION  (se puede pisar con #define ANTES de incluir este archivo)
// ---------------------------------------------------------------------------
#ifndef DISP_PIN_COIN
#define DISP_PIN_COIN            26      // rele que pulsa la entrada COIN
#endif
#ifndef DISP_PIN_BOTON
#define DISP_PIN_BOTON           25      // rele en paralelo con el pulsador
#endif
#ifndef DISP_PIN_SENSOR
#define DISP_PIN_SENSOR          27      // opto PC817 desde la linea SENSOR
#endif

#ifndef DISP_COIN_ACTIVO_BAJO
#define DISP_COIN_ACTIVO_BAJO     1      // modulos rele tipicos: activos en BAJO
#endif
#ifndef DISP_BOTON_ACTIVO_BAJO
#define DISP_BOTON_ACTIVO_BAJO    1
#endif
#ifndef DISP_SENSOR_ACTIVO_BAJO
#define DISP_SENSOR_ACTIVO_BAJO   1      // PC817 saturado tira el pin a masa
#endif

// 1 = verifica cada ficha con el sensor (RECOMENDADO, es lo unico que detecta
// el credito varado). 0 = entrega a ciegas mientras no este cableado.
#ifndef DISP_USAR_SENSOR
#define DISP_USAR_SENSOR          1
#endif

// 1 = despues de acreditar hay que apretar el boton para que entregue.
// 0 = con el credito cargado la placa entrega sola (un solo rele, el de COIN).
//
// En el banco se confirmo que la placa acumula credito y NO entrega sola: hay
// que apretar. PERO ese ensayo fue con ENTRADA COIN = $100, juntando de a $100
// hasta los $3000. Con ENTRADA COIN = $0 un solo pulso ya vale la ficha entera
// y nunca se probo si en ese caso entrega sola.
//
// COMO SABER CUAL VA (30 segundos, sin soldar nada):
//   poner ENTRADA COIN = $0, puentear COIN a GND UNA vez y NO tocar el boton.
//   Si cae la ficha sola      -> DISP_USAR_BOTON 0  (y te ahorras un rele)
//   Si el credito queda en el display -> DISP_USAR_BOTON 1
#ifndef DISP_USAR_BOTON
#define DISP_USAR_BOTON           1
#endif

// --- Tiempos ---
// Pulsos de COIN que hacen falta para pagar UNA ficha:
//     DISP_PULSOS_POR_FICHA = PRECIO FICHA / ENTRADA COIN
// Lo ideal es 1 (ENTRADA COIN = precio ficha, o = $0 que es equivalente), pero
// no todas las placas lo permiten: en la de Villagas el menu ENTRADA COIN topea
// en $250 y no acepta $0 ("codificado"), asi que con precio ficha $3000 van
// 3000 / 250 = 12 pulsos exactos, sin resto ni credito colgado.
// Elegir SIEMPRE una division exacta: si sobra resto, queda credito acumulandose
// en la maquina y tarde o temprano alguien se lleva una ficha gratis.
#ifndef DISP_PULSOS_POR_FICHA
#define DISP_PULSOS_POR_FICHA     1
#endif

#ifndef DISP_PULSO_COIN_MS
#define DISP_PULSO_COIN_MS      100      // nominal del RM5; la placa tolera 10 ms - 2 s
#endif
#ifndef DISP_PAUSA_COIN_MS
#define DISP_PAUSA_COIN_MS      250      // rele abierto despues del pulso
#endif
#ifndef DISP_ESPERA_ACREDITA_MS
#define DISP_ESPERA_ACREDITA_MS 600      // que la placa registre el credito
#endif
#ifndef DISP_PULSO_BOTON_MS
#define DISP_PULSO_BOTON_MS     250      // imita una pulsacion humana
#endif
#ifndef DISP_TIMEOUT_SENSOR_MS
#define DISP_TIMEOUT_SENSOR_MS 8000      // sin ficha en 8 s -> hopper vacio/atascado
#endif
#ifndef DISP_ESPERA_CIEGA_MS
#define DISP_ESPERA_CIEGA_MS   3000      // solo con DISP_USAR_SENSOR = 0
#endif
#ifndef DISP_ENTRE_FICHAS_MS
#define DISP_ENTRE_FICHAS_MS    500      // respiro entre ficha y ficha
#endif
#ifndef DISP_REBOTE_SENSOR_MS
#define DISP_REBOTE_SENSOR_MS    30      // dos flancos mas juntos que esto = uno solo
#endif
#ifndef DISP_MAX_FICHAS
#define DISP_MAX_FICHAS          20      // tope por pago (anti-vaciado)
#endif

// ---------------------------------------------------------------------------
//  ESTADOS Y ERRORES
// ---------------------------------------------------------------------------
enum DispEstado {
  DISP_REPOSO = 0,   // sin nada que hacer
  DISP_COIN,         // rele COIN cerrado
  DISP_PAUSA,        // rele COIN abierto, esperando
  DISP_ACREDITA,     // dandole tiempo a la placa a registrar el credito
  DISP_BOTON,        // rele BOTON cerrado
  DISP_SENSOR,       // esperando que caiga la ficha
  DISP_ENTRE,        // respiro antes de la proxima ficha
  DISP_FIN           // termino (con o sin error); vuelve a REPOSO al leerlo
};

enum DispError {
  DISP_OK = 0,
  DISP_ERR_SIN_FICHA     // se pulso el boton y el sensor no vio caer nada
};

// ---------------------------------------------------------------------------
//  SENSOR: conteo por interrupcion, con antirrebote
// ---------------------------------------------------------------------------
// La ISR toca lo minimo: incrementa un contador y guarda el instante. El
// filtro de rebote va aca adentro para que dos flancos del mismo golpe no
// cuenten como dos fichas.
volatile uint32_t _dispFichasSensor = 0;
volatile uint32_t _dispUltimoFlanco = 0;

void IRAM_ATTR _dispISRSensor() {
  uint32_t ahora = millis();
  if (ahora - _dispUltimoFlanco < DISP_REBOTE_SENSOR_MS) return;   // rebote
  _dispUltimoFlanco = ahora;
  _dispFichasSensor++;
}

// ---------------------------------------------------------------------------
//  DISPENSADOR
// ---------------------------------------------------------------------------
class Dispensador {
 public:
  // cb de fin: (pedidas, entregadas, error). error == DISP_OK si salieron todas.
  typedef void (*CbFin)(int pedidas, int entregadas, DispError error);
  // cb de pantalla: dos lineas para el LCD. Opcional.
  typedef void (*CbMostrar)(const char* linea1, const char* linea2);

  void begin() {
    pinMode(DISP_PIN_COIN, OUTPUT);
    _releCoin(false);                 // arranca en reposo: sin pulso espurio
#if DISP_USAR_BOTON
    pinMode(DISP_PIN_BOTON, OUTPUT);
    _releBoton(false);
#endif
#if DISP_USAR_SENSOR
    pinMode(DISP_PIN_SENSOR, DISP_SENSOR_ACTIVO_BAJO ? INPUT_PULLUP : INPUT);
    attachInterrupt(digitalPinToInterrupt(DISP_PIN_SENSOR), _dispISRSensor,
                    DISP_SENSOR_ACTIVO_BAJO ? FALLING : RISING);
#endif
    _estado = DISP_REPOSO;
  }

  void alTerminar(CbFin cb)     { _cbFin = cb; }
  void alMostrar(CbMostrar cb)  { _cbMostrar = cb; }

  // Arranca la entrega de 'cantidad' fichas. Devuelve false si ya esta
  // trabajando (el que llama deberia encolar el pago, no insistir).
  bool pedir(int cantidad) {
    if (_estado != DISP_REPOSO) return false;
    if (cantidad < 1) cantidad = 1;
    if (cantidad > DISP_MAX_FICHAS) cantidad = DISP_MAX_FICHAS;
    _pedidas    = cantidad;
    _entregadas = 0;
    _error      = DISP_OK;
    _mostrar("Entregando", cantidad == 1 ? "1 ficha" : "fichas");
    _arrancarCiclo();
    return true;
  }

  // Llamar en cada vuelta del loop(). No bloquea nunca.
  void actualizar() {
    if (_estado == DISP_REPOSO) return;
    uint32_t ahora = millis();

    switch (_estado) {
      case DISP_COIN:
        // Pulso de moneda. Hacen falta DISP_PULSOS_POR_FICHA para juntar el
        // precio de UNA ficha (1 si ENTRADA COIN ya vale la ficha entera).
        if (ahora - _t0 >= DISP_PULSO_COIN_MS) {
          _releCoin(false);
          _creditoCargado = true;     // desde el primer pulso hay plata en la maquina
          _pulsosCoin++;
          _pulsosRestantes--;
          _ir(DISP_PAUSA);
        }
        break;

      case DISP_PAUSA:
        if (ahora - _t0 >= DISP_PAUSA_COIN_MS) {
          if (_pulsosRestantes > 0) {
            _releCoin(true);          // todavia falta plata: otro pulso
            _ir(DISP_COIN);
          } else {
            _ir(DISP_ACREDITA);       // ya esta el precio completo
          }
        }
        break;

      case DISP_ACREDITA:
        if (ahora - _t0 >= DISP_ESPERA_ACREDITA_MS) {
#if DISP_USAR_BOTON
          _releBoton(true);
          _ir(DISP_BOTON);
#else
          // La placa entrega sola con el credito cargado: no hay boton que
          // apretar, se pasa derecho a esperar que caiga la ficha.
          _ir(DISP_SENSOR);
#endif
        }
        break;

      case DISP_BOTON:
        if (ahora - _t0 >= DISP_PULSO_BOTON_MS) {
          _releBoton(false);
          _pulsosBoton++;
          _ir(DISP_SENSOR);
        }
        break;

      case DISP_SENSOR:
#if DISP_USAR_SENSOR
        if (_leerSensor() > _fichasAlPulsar) {       // cayo la ficha
          _entregadas++;
          _creditoCargado = false;                   // el credito se consumio
          _siguiente();
        } else if (ahora - _t0 >= DISP_TIMEOUT_SENSOR_MS) {
          // Se pulso el boton y no cayo nada: hopper vacio o atascado. El
          // credito quedo cargado -> lo contamos y cortamos, no insistimos:
          // seguir pulsando contra una maquina vacia solo carga mas plata.
          _error = DISP_ERR_SIN_FICHA;
          if (_creditoCargado) _creditoVarado++;
          _terminar();
        }
#else
        // Sin sensor: no hay forma de saber si salio. Se asume que si.
        if (ahora - _t0 >= DISP_ESPERA_CIEGA_MS) {
          _entregadas++;
          _creditoCargado = false;
          _siguiente();
        }
#endif
        break;

      case DISP_ENTRE:
        if (ahora - _t0 >= DISP_ENTRE_FICHAS_MS) _arrancarCiclo();
        break;

      default:
        break;
    }
  }

  bool ocupado() const        { return _estado != DISP_REPOSO; }
  DispEstado estado() const   { return _estado; }
  int  pedidas() const        { return _pedidas; }
  int  entregadas() const     { return _entregadas; }
  DispError error() const     { return _error; }

  // Fichas que se pagaron desde el ESP y no salieron: quedo credito cargado
  // en la maquina. Si esto es > 0, alguien puede llevarse una ficha gratis
  // apretando el boton fisico. Reportarlo por MQTT.
  uint32_t creditoVarado() const { return _creditoVarado; }
  void limpiarCreditoVarado()    { _creditoVarado = 0; }

  uint32_t pulsosCoin() const  { return _pulsosCoin; }
  uint32_t pulsosBoton() const { return _pulsosBoton; }
  uint32_t fichasVistas() const { return _leerSensor(); }

  const char* estadoTxt() const {
    switch (_estado) {
      case DISP_REPOSO:   return "reposo";
      case DISP_COIN:     return "coin";
      case DISP_PAUSA:    return "pausa";
      case DISP_ACREDITA: return "acreditando";
      case DISP_BOTON:    return "boton";
      case DISP_SENSOR:   return "esperando ficha";
      case DISP_ENTRE:    return "entre fichas";
      default:            return "fin";
    }
  }

  static const char* errorTxt(DispError e) {
    return e == DISP_ERR_SIN_FICHA ? "sin_ficha" : "ok";
  }

 private:
  DispEstado _estado    = DISP_REPOSO;
  DispError  _error     = DISP_OK;
  uint32_t   _t0        = 0;
  int        _pedidas   = 0;
  int        _entregadas = 0;
  uint32_t   _fichasAlPulsar = 0;
  int        _pulsosRestantes = 0;   // pulsos de COIN que faltan para esta ficha
  bool       _creditoCargado = false;
  uint32_t   _creditoVarado  = 0;
  uint32_t   _pulsosCoin     = 0;
  uint32_t   _pulsosBoton    = 0;
  CbFin      _cbFin     = nullptr;
  CbMostrar  _cbMostrar = nullptr;

  static uint32_t _leerSensor() {
    noInterrupts();
    uint32_t v = _dispFichasSensor;
    interrupts();
    return v;
  }

  void _ir(DispEstado e) { _estado = e; _t0 = millis(); }

  void _arrancarCiclo() {
    _creditoCargado = false;
    // Marca de referencia ANTES del pulso: cualquier ficha que caiga a partir
    // de aca cuenta como la de este ciclo. Tomarla mas tarde se perderia la
    // ficha en el modo sin boton, donde la placa puede entregar apenas acredita.
    _fichasAlPulsar = _leerSensor();
    _pulsosRestantes = DISP_PULSOS_POR_FICHA;
    _releCoin(true);
    _ir(DISP_COIN);
  }

  // Termino una ficha: o arranca la proxima, o cierra.
  void _siguiente() {
    if (_entregadas >= _pedidas) _terminar();
    else                        _ir(DISP_ENTRE);
  }

  void _terminar() {
    _releCoin(false);          // por las dudas: nunca dejar un rele cerrado
#if DISP_USAR_BOTON
    _releBoton(false);
#endif
    _estado = DISP_REPOSO;
    if (_error == DISP_OK) {
      _mostrar("Listo!", "Gracias");
    } else {
      _mostrar("Sin fichas", "Avise al local");
    }
    if (_cbFin) _cbFin(_pedidas, _entregadas, _error);
  }

  void _mostrar(const char* l1, const char* l2) {
    if (_cbMostrar) _cbMostrar(l1, l2);
  }

  static void _releCoin(bool activo) {
#if DISP_COIN_ACTIVO_BAJO
    digitalWrite(DISP_PIN_COIN, activo ? LOW : HIGH);
#else
    digitalWrite(DISP_PIN_COIN, activo ? HIGH : LOW);
#endif
  }

  static void _releBoton(bool activo) {
#if DISP_BOTON_ACTIVO_BAJO
    digitalWrite(DISP_PIN_BOTON, activo ? LOW : HIGH);
#else
    digitalWrite(DISP_PIN_BOTON, activo ? HIGH : LOW);
#endif
  }
};

/*  ============================================================================
    INTEGRACION con termopago_fichas.ino
    ----------------------------------------------------------------------------
    El sketch de hoy entrega con un tren de pulsos al COIN y NADA MAS: no aprieta
    el boton, asi que contra la placa real carga credito y no entrega. Estos son
    los cambios, todos chicos:

    1) Arriba, despues de los otros #include:
           #include "dispensador.h"
           Dispensador dispensador;

    2) Sacar (o dejar sin usar) el rele viejo de un solo pin: RELE_PIN, PULSO_MS,
       PAUSA_MS y releEscribir() los reemplaza el modulo. OJO: en setup() hay un
       pinMode(RELE_PIN, OUTPUT) + releEscribir(false) que hay que borrar, porque
       RELE_PIN y DISP_PIN_COIN son el mismo GPIO 26 y se pisan.

    3) En setup(), donde estaba eso:
           dispensador.begin();
           dispensador.alMostrar([](const char* a, const char* b){ mostrar(a, b); });
           dispensador.alTerminar(entregaTerminada);

    4) Reemplazar entregarFichas() entera por el arranque no bloqueante:

           void entregarFichas(const String& pagoId, int cantidad) {
             if (pagoId == ultimoPagoProcesado) return;      // dedup
             if (!dispensador.pedir(cantidad)) return;       // ocupado: sigue en cola
             pagoEnCurso = pagoId;
           }

       y agregar el callback de fin, que es donde ahora se cierra todo:

           String pagoEnCurso = "";

           void entregaTerminada(int pedidas, int entregadas, DispError err) {
             ultimoPagoProcesado = pagoEnCurso;              // recien aca: ya se entrego
             prefs.putString("ultpago", ultimoPagoProcesado);
             pulsosTotales += entregadas;
             prefs.putUInt("pulsos", pulsosTotales);
             graciasHastaMs = millis() + GRACIAS_MS;
             publicarEstado("online", false);
             if (err != DISP_OK)
               Serial.printf("[FICHAS] FALLA: pedidas %d, entregadas %d (%s)\n",
                             pedidas, entregadas, Dispensador::errorTxt(err));
           }

    5) En loop(), agregar la linea del modulo y cambiar la condicion de la cola
       para que no arranque un pago nuevo mientras hay uno entregando:

           dispensador.actualizar();
           if (!dispensador.ocupado() && graciasHastaMs == 0 && colaLen > 0) { ... }

       Y en los reinicios de seguridad (sin-comm, preventivo, heap), sumar
       !dispensador.ocupado() a la condicion: un reinicio en el medio de una
       entrega deja credito cargado y la ficha sin salir.

    6) En heartbeatJson(), para que el backend vea lo que pasa:
           doc["dispensador"]      = dispensador.estadoTxt();
           doc["credito_varado"]   = dispensador.creditoVarado();
           doc["fichas_sensor"]    = dispensador.fichasVistas();

    ANTES DE PONERLO EN LA CALLE
    Prueba de rebote obligatoria: pedir 50 fichas y contar que salgan 50 exactas.
    Si salen de mas, el rebote del contacto del rele (~1 ms) esta generando dobles
    pulsos: reemplazar el rele de COIN por un PC817 (colector a COIN, emisor a GND
    de la placa; ahi si se unen masas).
    ============================================================================ */

#endif  // DISPENSADOR_H
