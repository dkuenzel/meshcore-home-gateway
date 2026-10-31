#pragma once
// An I2C bus on which no device answers: EnvironmentSensorManager finds no environment sensor.
#include <Arduino.h>

class TwoWire {
 public:
  void begin() {}
  void begin(int, int, uint32_t = 100000) {}
  void setPins(int, int) {}
  void setClock(uint32_t) {}
  void beginTransmission(uint8_t) {}
  uint8_t endTransmission(bool = true) { return 2; }  // no acknowledgement of the address
};

extern TwoWire Wire;
extern TwoWire Wire1;
