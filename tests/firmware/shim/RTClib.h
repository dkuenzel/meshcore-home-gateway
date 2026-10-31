#pragma once
// Adafruit RTClib: the firmware includes it, and the repeater's command line formats times with
// DateTime.
#include <stdint.h>
#include <time.h>

class DateTime {
  struct tm _tm;

 public:
  DateTime(uint32_t t = 0) { time_t tt = t; gmtime_r(&tt, &_tm); }
  int year() const { return _tm.tm_year + 1900; }
  int month() const { return _tm.tm_mon + 1; }
  int day() const { return _tm.tm_mday; }
  int hour() const { return _tm.tm_hour; }
  int minute() const { return _tm.tm_min; }
  int second() const { return _tm.tm_sec; }
};
