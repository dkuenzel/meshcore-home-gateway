#pragma once
// encode_base64 and decode_base64 of densaugeo/base64 (base64.hpp), with explicit lengths.
#include <stdint.h>
#include <string.h>

static inline unsigned int encode_base64(const unsigned char in[], unsigned int len, unsigned char out[]) {
  static const char* table = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  unsigned int o = 0;
  for (unsigned int i = 0; i < len; i += 3) {
    uint32_t v = in[i] << 16 | (i + 1 < len ? in[i + 1] << 8 : 0) | (i + 2 < len ? in[i + 2] : 0);
    out[o++] = table[(v >> 18) & 63];
    out[o++] = table[(v >> 12) & 63];
    out[o++] = i + 1 < len ? table[(v >> 6) & 63] : '=';
    out[o++] = i + 2 < len ? table[v & 63] : '=';
  }
  out[o] = 0;
  return o;
}

static inline int base64_value(unsigned char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+' || c == '-') return 62;
  if (c == '/' || c == '_') return 63;
  return -1;
}

static inline unsigned int decode_base64(const unsigned char in[], unsigned int len, unsigned char out[]) {
  unsigned int o = 0;
  uint32_t acc = 0;
  int bits = 0;
  for (unsigned int i = 0; i < len; i++) {
    int v = base64_value(in[i]);
    if (v < 0) {
      if (in[i] == '=') break;
      continue;
    }
    acc = (acc << 6) | v;
    bits += 6;
    if (bits >= 8) { bits -= 8; out[o++] = (acc >> bits) & 0xFF; }
  }
  return o;
}
