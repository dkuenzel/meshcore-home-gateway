#pragma once
// Print and Stream of the Arduino core for the firmware built natively.
// Derived from test/mocks/Stream.h of MeshCore (MIT License, Copyright (c) 2025 Scott Powell /
// rippleradios.com), with working print, println and printf.
// write() carries bytes. print, println and printf carry text through printText(), which a port
// may send elsewhere: the simulated USB serial writes text to the node's log (board/runtime.cpp).
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define DEC 10
#define HEX 16
#define OCT 8
#define BIN 2

class Print {
 public:
  virtual ~Print() = default;
  virtual size_t write(uint8_t b) = 0;
  virtual size_t write(const uint8_t* buffer, size_t size) {
    size_t n = 0;
    for (size_t i = 0; i < size; i++) n += write(buffer[i]);
    return n;
  }
  size_t write(const char* str) { return str ? write((const uint8_t*)str, strlen(str)) : 0; }
  size_t write(const char* buffer, size_t size) { return write((const uint8_t*)buffer, size); }
  virtual void flush() {}

  size_t print(const char* s) { return s ? printText(s, strlen(s)) : 0; }
  size_t print(char c) { return printText(&c, 1); }
  size_t print(unsigned char v, int base = DEC) { return number(v, base, false); }
  size_t print(int v, int base = DEC) { return v < 0 && base == DEC ? number(-(long long)v, base, true) : number((unsigned)v, base, false); }
  size_t print(unsigned int v, int base = DEC) { return number(v, base, false); }
  size_t print(long v, int base = DEC) { return v < 0 && base == DEC ? number(-(long long)v, base, true) : number((unsigned long)v, base, false); }
  size_t print(unsigned long v, int base = DEC) { return number(v, base, false); }
  size_t print(long long v, int base = DEC) { return v < 0 && base == DEC ? number(-(unsigned long long)v, base, true) : number((unsigned long long)v, base, false); }
  size_t print(unsigned long long v, int base = DEC) { return number(v, base, false); }
  size_t print(double v, int digits = 2) {
    char buf[64];
    int n = snprintf(buf, sizeof(buf), "%.*f", digits, v);
    return n > 0 ? printText(buf, (size_t)n < sizeof(buf) ? n : sizeof(buf) - 1) : 0;
  }
  size_t println() { return printText("\r\n", 2); }
  template <typename T> size_t println(T v) { size_t n = print(v); return n + println(); }
  template <typename T> size_t println(T v, int format) { size_t n = print(v, format); return n + println(); }
  size_t printf(const char* format, ...) __attribute__((format(printf, 2, 3))) {
    char buf[512];
    va_list ap;
    va_start(ap, format);
    int n = vsnprintf(buf, sizeof(buf), format, ap);
    va_end(ap);
    return n > 0 ? printText(buf, (size_t)n < sizeof(buf) ? n : sizeof(buf) - 1) : 0;
  }

 protected:
  virtual size_t printText(const char* text, size_t len) { return write((const uint8_t*)text, len); }

 private:
  size_t number(unsigned long long v, int base, bool negative) {
    char buf[70];
    char* p = &buf[sizeof(buf) - 1];
    *p = 0;
    if (base < 2) base = DEC;
    do { int d = v % base; *--p = d < 10 ? '0' + d : 'A' + d - 10; v /= base; } while (v);
    if (negative) *--p = '-';
    return printText(p, strlen(p));
  }
};

class Stream : public Print {
 public:
  virtual int available() = 0;
  virtual int read() = 0;
  virtual int peek() = 0;
  virtual int availableForWrite() { return 0; }
  void setTimeout(unsigned long ms) { _timeout = ms; }
  virtual size_t readBytes(char* buffer, size_t length) {
    size_t i = 0;
    while (i < length && available()) buffer[i++] = read();
    return i;
  }
  virtual size_t readBytes(uint8_t* buffer, size_t length) { return readBytes((char*)buffer, length); }

 protected:
  unsigned long _timeout = 1000;
};
