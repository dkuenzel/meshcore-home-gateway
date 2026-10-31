#pragma once
// SHA256 of rweather/Crypto (update, finalize, resetHMAC, finalizeHMAC) on OpenSSL's SHA-256, in
// board/crypto.cpp: OpenSSL's own SHA256() function would clash with the class name here.
#include <stddef.h>
#include <stdint.h>

class SHA256 {
  void* _ctx;
  uint8_t _keybuf[64];
  void formatKey(const void* key, size_t len, uint8_t pad);

 public:
  SHA256();
  ~SHA256();
  SHA256(const SHA256&) = delete;
  SHA256& operator=(const SHA256&) = delete;
  void reset();
  void update(const void* data, size_t len);
  void finalize(void* hash, size_t len);
  void resetHMAC(const void* key, size_t keyLen);
  void finalizeHMAC(const void* key, size_t keyLen, void* hash, size_t hashLen);
};
