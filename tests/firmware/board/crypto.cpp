// SHA256 and HMAC-SHA256 of rweather/Crypto (shim/SHA256.h) on OpenSSL's EVP digests.
#include <SHA256.h>
#include <openssl/evp.h>
#include <string.h>

#define CTX ((EVP_MD_CTX*)_ctx)

SHA256::SHA256() {
  _ctx = EVP_MD_CTX_new();
  reset();
}
SHA256::~SHA256() { EVP_MD_CTX_free(CTX); }
void SHA256::reset() { EVP_DigestInit_ex(CTX, EVP_sha256(), NULL); }
void SHA256::update(const void* data, size_t len) { EVP_DigestUpdate(CTX, data, len); }

void SHA256::finalize(void* hash, size_t len) {
  uint8_t h[32];
  unsigned int hl = 32;
  EVP_DigestFinal_ex(CTX, h, &hl);
  memcpy(hash, h, len > 32 ? 32 : len);
  reset();
}

void SHA256::formatKey(const void* key, size_t len, uint8_t pad) {
  uint8_t k[64] = {0};
  if (len > 64) {
    unsigned int hl = 32;
    EVP_Digest(key, len, k, &hl, EVP_sha256(), NULL);
  } else {
    memcpy(k, key, len);
  }
  for (int i = 0; i < 64; i++) _keybuf[i] = k[i] ^ pad;
}

void SHA256::resetHMAC(const void* key, size_t keyLen) {
  reset();
  formatKey(key, keyLen, 0x36);
  update(_keybuf, 64);
}

void SHA256::finalizeHMAC(const void* key, size_t keyLen, void* hash, size_t hashLen) {
  uint8_t inner[32];
  unsigned int hl = 32;
  EVP_DigestFinal_ex(CTX, inner, &hl);
  reset();
  formatKey(key, keyLen, 0x5c);
  update(_keybuf, 64);
  update(inner, 32);
  finalize(hash, hashLen);
}
