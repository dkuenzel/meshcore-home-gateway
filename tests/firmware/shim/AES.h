#pragma once
// AES128 of rweather/Crypto (setKey, encryptBlock, decryptBlock) on OpenSSL's AES.
#include <stddef.h>
#include <stdint.h>
#include <openssl/aes.h>

class AES128 {
  AES_KEY enc, dec;

 public:
  bool setKey(const uint8_t* key, size_t len) {
    AES_set_encrypt_key(key, 128, &enc);
    AES_set_decrypt_key(key, 128, &dec);
    return len == 16;
  }
  void encryptBlock(uint8_t* out, const uint8_t* in) { AES_encrypt(in, out, &enc); }
  void decryptBlock(uint8_t* out, const uint8_t* in) { AES_decrypt(in, out, &dec); }
};
