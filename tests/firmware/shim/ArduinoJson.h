#pragma once
// Just enough of ArduinoJson and Arduino's String for CayenneLPP 1.6.1 to compile with ARDUINO
// defined. The firmware only encodes; the library's JSON decoder is never called.
#include <string>

struct JsonVariant {
  template <typename T> T to() { return T(); }
  template <typename V> JsonVariant& operator=(const V&) { return *this; }
  JsonVariant operator[](const char*) { return JsonVariant(); }
};

struct JsonObject {
  JsonVariant operator[](const char*) { return JsonVariant(); }
};

struct JsonArray {
  template <typename T> T add() { return T(); }
  template <typename V> bool add(const V&) { return true; }
};

struct String : std::string {
  String(const char* s = "") : std::string(s) {}
  String(const std::string& s) : std::string(s) {}
  String operator+(const char* s) const { return String(std::string(*this) + s); }
  String operator+(int v) const { return String(std::string(*this) + std::to_string(v)); }
  operator const char*() const { return c_str(); }
};
