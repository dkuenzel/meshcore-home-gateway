#pragma once
// The part of the Arduino core that the firmware uses, for the firmware built natively.
// Time, random numbers and the serial ports are in board/runtime.cpp.
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <algorithm>
#include <cmath>

#include "Stream.h"

using std::isnan;
// Arduino's min and max are macros that take mixed types (simple_repeater/MyMesh.cpp)
template <class A, class B> inline auto min(A a, B b) -> decltype(a < b ? a : b) { return a < b ? a : b; }
template <class A, class B> inline auto max(A a, B b) -> decltype(a > b ? a : b) { return a > b ? a : b; }
template <typename T, typename L, typename H> inline T constrain(T x, L lo, H hi) { return x < lo ? lo : (x > hi ? hi : x); }

#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define INPUT_PULLDOWN 3
#define PI 3.1415926535897932384626433832795

unsigned long millis();
unsigned long micros();
void delay(unsigned long ms);
inline void yield() {}
long random(long max_excl);
long random(long min_incl, long max_excl);
void randomSeed(unsigned long seed);
inline void pinMode(int, int) {}
inline void digitalWrite(int, int) {}
inline int digitalRead(int) { return 0; }
inline int analogRead(int) { return 0; }

// TxtDataHelpers.cpp formats numbers with ltoa
inline char* ltoa(long v, char* buf, int base) {
  if (base == 10) { sprintf(buf, "%ld", v); return buf; }
  char tmp[66];
  int i = 0;
  unsigned long u = v;
  do { int d = u % base; tmp[i++] = d < 10 ? '0' + d : 'a' + d - 10; u /= base; } while (u);
  int j = 0;
  while (i) buf[j++] = tmp[--i];
  buf[j] = 0;
  return buf;
}

// Serial: the USB serial. Bytes (the companion's frames) go to a pseudo terminal, text (packet
// logging, debug output, the repeater's command line replies) to the node's log on stderr.
class SimSerialPort : public Stream {
 public:
  void begin(unsigned long) {}
  operator bool() const { return true; }
  int available() override;
  int read() override;
  int peek() override;
  size_t write(uint8_t b) override { return write(&b, 1); }
  size_t write(const uint8_t* buf, size_t len) override;
  using Print::write;

 protected:
  size_t printText(const char* text, size_t len) override;
};

// Serial1: the GPS UART. available() > 0 while the simulated GPS is attached, as on a board whose
// GPS sends NMEA, so that EnvironmentSensorManager::initBasicGPS() finds it.
class SimGpsUart : public Stream {
 public:
  void begin(unsigned long) {}
  void end() {}
  void setPins(int, int) {}
  int available() override;
  int read() override { return -1; }
  int peek() override { return -1; }
  size_t write(uint8_t) override { return 1; }
  using Print::write;
};

extern SimSerialPort Serial;
extern SimGpsUart Serial1;
