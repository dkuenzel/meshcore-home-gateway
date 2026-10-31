#pragma once
// The simulated board: replaces variants/<board>/target.h and target.cpp of the firmware.
#include <Arduino.h>
#include <Mesh.h>
#include <helpers/ArduinoHelpers.h>
#include <helpers/SensorManager.h>
#include <helpers/sensors/EnvironmentSensorManager.h>
#include <helpers/sensors/LocationProvider.h>

#include "SimRadio.h"

class KeyValueStore;

class SimBoard : public mesh::MainBoard {
 public:
  uint16_t batt_mv = 3950;
  float mcu_temp = 23.4f;
  void begin() {}
  uint16_t getBattMilliVolts() override { return batt_mv; }
  float getMCUTemperature() override { return mcu_temp; }
  const char* getManufacturerName() const override { return "Native Sim"; }
  void reboot() override;
  uint8_t getStartupReason() const override { return BD_STARTUP_NORMAL; }
  void attachDynamicPrefs(KeyValueStore*) {}
};

// A GPS receiver that the test steers: a fix (latitude and longitude in degrees, altitude in m)
// or none.
class SimLocationProvider : public LocationProvider {
 public:
  bool attached = false;  // a GPS module on the board at all
  bool enabled = false;   // powered (begin, stop)
  bool fix = false;
  double lat = 0, lon = 0, alt = 0;
  long sats = 0;
  long getLatitude() override { return (long)(lat * 1000000.0); }
  long getLongitude() override { return (long)(lon * 1000000.0); }
  long getAltitude() override { return (long)(alt * 1000.0); }
  long satellitesCount() override { return fix ? sats : 0; }
  bool isValid() override { return enabled && fix; }
  long getTimestamp() override { return 0; }
  void reset() override {}
  void begin() override { enabled = true; }
  void stop() override { enabled = false; }
  void loop() override {}
  bool isEnabled() override { return enabled; }
};

extern SimBoard board;
extern SimRadio radio_driver;
extern VolatileRTCClock rtc_clock;
extern SimLocationProvider sim_gps;
extern EnvironmentSensorManager sensors;

bool radio_init();
mesh::LocalIdentity radio_new_identity();
