// Prueba de la maquina de estados de dispensador.h, corriendo en la PC contra
// un Arduino falso. El reloj lo movemos a mano, asi que se puede simular un
// timeout de 8 s sin esperarlo.
//
//   g++ -std=c++17 -I stub prueba_dispensador.cpp -o prueba && ./prueba

#include <cstdint>
#include <map>
#include <string>
#include <vector>
#include <cstdio>

uint32_t _reloj = 0;
std::map<int, int> _pines;
std::map<int, int> _modos;
#include "stub/Arduino.h"
_Serial Serial;

#include "../firmware/termopago_fichas/dispensador.h"

// ---- utilidades de la prueba ----
int fallas = 0;
void chequear(const std::string& nombre, bool ok, const std::string& extra = "") {
    printf("  %s %s%s\n", ok ? "OK  " : "FALLA", nombre.c_str(),
           ok ? "" : ("  <- " + extra).c_str());
    if (!ok) fallas++;
}

Dispensador disp;

int finPedidas = -1, finEntregadas = -1;
DispError finError = DISP_OK;
int vecesFin = 0;
std::vector<std::string> pantallas;

void alFin(int p, int e, DispError err) {
    finPedidas = p; finEntregadas = e; finError = err; vecesFin++;
}
void alMostrar(const char* a, const char* b) {
    pantallas.push_back(std::string(a) + " / " + b);
}

// Avanza el reloj de a 1 ms llamando actualizar(), como haria el loop real.
// Si 'sensorAl' es > 0, dispara el sensor a ese ms del recorrido.
void correr(uint32_t ms, int sensorEn = -1) {
    for (uint32_t i = 0; i < ms; i++) {
        _reloj++;
        if (sensorEn >= 0 && (int)i == sensorEn) _dispFichasSensor++;
        disp.actualizar();
    }
}

// Corre hasta que el dispensador quede libre, disparando el sensor cada vez
// que entra en estado DISP_SENSOR (simula una maquina que entrega bien).
void correrEntregando(uint32_t topeMs) {
    DispEstado previo = disp.estado();
    for (uint32_t i = 0; i < topeMs && disp.ocupado(); i++) {
        _reloj++;
        disp.actualizar();
        if (disp.estado() == DISP_SENSOR && previo != DISP_SENSOR) {
            _dispFichasSensor++;      // la ficha cae apenas se suelta el boton
        }
        previo = disp.estado();
    }
}

// Un pin que nunca se configuro como salida NO esta manejando ningun rele:
// en modo sin boton, GPIO25 queda libre y no hay que leerlo como "cerrado".
bool manejado(int pin) { return _modos.count(pin) && _modos[pin] == OUTPUT; }
bool coinCerrado() {
    return manejado(DISP_PIN_COIN) &&
           _pines[DISP_PIN_COIN] == (DISP_COIN_ACTIVO_BAJO ? LOW : HIGH);
}
bool botonCerrado() {
    return manejado(DISP_PIN_BOTON) &&
           _pines[DISP_PIN_BOTON] == (DISP_BOTON_ACTIVO_BAJO ? LOW : HIGH);
}

void reiniciar() {
    _reloj = 1000;
    _pines.clear();
    _modos.clear();
    _dispFichasSensor = 0;
    _dispUltimoFlanco = 0;
    vecesFin = 0; finPedidas = finEntregadas = -1; finError = DISP_OK;
    pantallas.clear();
    disp = Dispensador();
    disp.begin();
    disp.alTerminar(alFin);
    disp.alMostrar(alMostrar);
}

int main() {
    printf("\n== MODO: %s ==\n", DISP_USAR_BOTON ? "con rele de BOTON" : "sin boton (la placa entrega sola)");

    printf("\n== 1. Arranque seguro ==\n");
    reiniciar();
    chequear("los reles arrancan abiertos", !coinCerrado() && !botonCerrado());
    chequear("arranca en reposo", !disp.ocupado());
    chequear("COIN y BOTON son pines distintos", DISP_PIN_COIN != DISP_PIN_BOTON);
    chequear(DISP_USAR_BOTON ? "configura el pin de BOTON como salida"
                             : "sin boton: deja GPIO25 libre, sin configurar",
             manejado(DISP_PIN_BOTON) == (bool)DISP_USAR_BOTON);
    chequear("siempre configura el pin de COIN", manejado(DISP_PIN_COIN));

    printf("\n== 2. Una ficha, todo bien ==\n");
    reiniciar();
    chequear("pedir(1) arranca", disp.pedir(1));
    chequear("cierra el rele COIN de una", coinCerrado());
    correr(DISP_PULSO_COIN_MS - 5);
    chequear("el pulso COIN sigue cerrado antes de tiempo", coinCerrado());
    correr(10);
    chequear("el pulso COIN se abre a los 100 ms", !coinCerrado());
    chequear("no aprieta el boton todavia", !botonCerrado());
    correr(DISP_PAUSA_COIN_MS + DISP_ESPERA_ACREDITA_MS + 5);
#if DISP_USAR_BOTON
    chequear("despues de acreditar, aprieta el boton", botonCerrado(), disp.estadoTxt());
    correr(DISP_PULSO_BOTON_MS + 5);
    chequear("suelta el boton", !botonCerrado());
#else
    chequear("sin boton: nunca toca el rele de BOTON", !botonCerrado());
    chequear("pasa derecho a esperar la ficha", disp.estado() == DISP_SENSOR, disp.estadoTxt());
#endif
    chequear("queda esperando la ficha", disp.estado() == DISP_SENSOR, disp.estadoTxt());
#if DISP_USAR_SENSOR
    _dispFichasSensor++;                 // cae la ficha
    correr(5);
    chequear("termina al ver la ficha", !disp.ocupado(), disp.estadoTxt());
#else
    correr(DISP_ESPERA_CIEGA_MS + 5);
    chequear("a ciegas: termina por tiempo, sin confirmar nada", !disp.ocupado(),
             disp.estadoTxt());
#endif
    chequear("aviso una sola vez", vecesFin == 1, std::to_string(vecesFin));
    chequear("entrego 1 de 1", finPedidas == 1 && finEntregadas == 1);
    chequear("sin error", finError == DISP_OK);
    chequear("sin credito varado", disp.creditoVarado() == 0);
    chequear("un solo pulso de COIN", disp.pulsosCoin() == 1, std::to_string(disp.pulsosCoin()));
    chequear("un solo pulso de BOTON" , disp.pulsosBoton() == (DISP_USAR_BOTON ? 1u : 0u),
             std::to_string(disp.pulsosBoton()));

    printf("\n== 3. NUNCA carga credito para dos fichas a la vez ==\n");
    reiniciar();
    disp.pedir(3);
    uint32_t maxCoinsSinEntregar = 0, coinsAntes = 0;
    DispEstado previo = disp.estado();
    for (int i = 0; i < 60000 && disp.ocupado(); i++) {
        _reloj++;
        disp.actualizar();
        if (disp.estado() == DISP_SENSOR && previo != DISP_SENSOR) _dispFichasSensor++;
        previo = disp.estado();
        // credito "en la maquina" = pulsos de COIN mandados - fichas que salieron
        uint32_t pendiente = disp.pulsosCoin() - (uint32_t)disp.entregadas();
        if (pendiente > maxCoinsSinEntregar) maxCoinsSinEntregar = pendiente;
    }
    (void)coinsAntes;
    chequear("nunca hay mas de 1 ficha de credito cargada",
             maxCoinsSinEntregar <= 1, std::to_string(maxCoinsSinEntregar));
    chequear("entrego las 3", finEntregadas == 3, std::to_string(finEntregadas));
    chequear("3 pulsos de COIN, uno por ficha", disp.pulsosCoin() == 3,
             std::to_string(disp.pulsosCoin()));
    chequear("un ciclo completo por ficha, no un tren de pulsos",
             disp.pulsosBoton() == (DISP_USAR_BOTON ? disp.pulsosCoin() : 0u));

#if DISP_USAR_SENSOR
    printf("\n== 4. Hopper vacio: timeout, credito varado, y NO insiste ==\n");
    reiniciar();
    disp.pedir(2);
    correr(DISP_PULSO_COIN_MS + DISP_PAUSA_COIN_MS + DISP_ESPERA_ACREDITA_MS +
           DISP_PULSO_BOTON_MS + DISP_TIMEOUT_SENSOR_MS + 50);   // sin sensor nunca
    chequear("corta por timeout", !disp.ocupado(), disp.estadoTxt());
    chequear("reporta el error", finError == DISP_ERR_SIN_FICHA);
    chequear("entrego 0 de 2", finPedidas == 2 && finEntregadas == 0);
    chequear("cuenta el credito varado", disp.creditoVarado() == 1,
             std::to_string(disp.creditoVarado()));
    chequear("NO sigue pulsando COIN contra una maquina vacia",
             disp.pulsosCoin() == 1, std::to_string(disp.pulsosCoin()));
    chequear("deja los dos reles abiertos", !coinCerrado() && !botonCerrado());
    chequear("la pantalla avisa", !pantallas.empty() &&
             pantallas.back().find("Sin fichas") != std::string::npos,
             pantallas.empty() ? "(nada)" : pantallas.back());

    printf("\n== 5. Falla en la ficha 2 de 3: solo se varia una ==\n");
    reiniciar();
    disp.pedir(3);
    int entregadasSimuladas = 0;
    previo = disp.estado();
    for (int i = 0; i < 60000 && disp.ocupado(); i++) {
        _reloj++;
        disp.actualizar();
        if (disp.estado() == DISP_SENSOR && previo != DISP_SENSOR) {
            if (entregadasSimuladas < 1) { _dispFichasSensor++; entregadasSimuladas++; }
            // a partir de la segunda, la maquina no entrega mas
        }
        previo = disp.estado();
    }
    chequear("entrego 1 de 3", finEntregadas == 1, std::to_string(finEntregadas));
    chequear("error de sin ficha", finError == DISP_ERR_SIN_FICHA);
    chequear("solo 1 ficha de credito varado, no 2",
             disp.creditoVarado() == 1, std::to_string(disp.creditoVarado()));
    chequear("mando 2 pulsos de COIN (la que salio y la que fallo)",
             disp.pulsosCoin() == 2, std::to_string(disp.pulsosCoin()));

#else
    printf("\n== 4b. Sin sensor: entrega a ciegas (y sus limites) ==\n");
    reiniciar();
    disp.pedir(2);
    correr(60000);                        // el sensor nunca dispara
    chequear("a ciegas termina igual", !disp.ocupado(), disp.estadoTxt());
    chequear("dice haber entregado las 2 aunque no cayo ninguna",
             finEntregadas == 2, std::to_string(finEntregadas));
    chequear("no reporta error: no tiene con que saberlo", finError == DISP_OK);
    chequear("y por eso NO detecta el credito varado",
             disp.creditoVarado() == 0, std::to_string(disp.creditoVarado()));
    chequear("igual respeta un ciclo por ficha", disp.pulsosCoin() == 2,
             std::to_string(disp.pulsosCoin()));
#endif

    printf("\n== 6. Antirrebote del sensor ==\n");
    reiniciar();
    _reloj = 5000;
    _dispUltimoFlanco = 0;
    uint32_t antes = disp.fichasVistas();
    _dispISRSensor();                       // primer flanco
    _reloj += DISP_REBOTE_SENSOR_MS / 3;    // rebote del mismo golpe
    _dispISRSensor();
    _reloj += DISP_REBOTE_SENSOR_MS / 3;
    _dispISRSensor();
    chequear("tres flancos juntos cuentan como una ficha",
             disp.fichasVistas() - antes == 1, std::to_string(disp.fichasVistas() - antes));
    _reloj += DISP_REBOTE_SENSOR_MS + 5;    // ficha de verdad, mas tarde
    _dispISRSensor();
    chequear("un flanco separado si cuenta",
             disp.fichasVistas() - antes == 2, std::to_string(disp.fichasVistas() - antes));

    printf("\n== 7. No se pisan dos pagos ==\n");
    reiniciar();
    chequear("el primer pedido entra", disp.pedir(1));
    chequear("el segundo se rechaza mientras trabaja", !disp.pedir(1));
    chequear("sigue ocupado", disp.ocupado());
    correrEntregando(60000);
    chequear("al quedar libre acepta de nuevo", disp.pedir(1));

    printf("\n== 8. Topes de cantidad ==\n");
    reiniciar();
    disp.pedir(0);
    chequear("pedir(0) se corrige a 1", disp.pedidas() == 1, std::to_string(disp.pedidas()));
    reiniciar();
    disp.pedir(9999);
    chequear("pedir(9999) se corta en MAX_FICHAS",
             disp.pedidas() == DISP_MAX_FICHAS, std::to_string(disp.pedidas()));

    printf("\n== 9. El reloj de millis() dando la vuelta ==\n");
    reiniciar();
    _reloj = 0xFFFFFF00;                 // faltan 255 ms para el desborde (49 dias)
    disp.pedir(1);
    correrEntregando(60000);
    chequear("entrega igual cruzando el desborde de millis",
             finEntregadas == 1 && finError == DISP_OK,
             std::to_string(finEntregadas));

    printf("\n%s\n", fallas ? ("FALLARON " + std::to_string(fallas)).c_str() : "TODO OK");
    return fallas ? 1 : 0;
}
