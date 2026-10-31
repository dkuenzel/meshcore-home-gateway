#pragma once
// Ed25519::verify of rweather/Crypto on OpenSSL; mesh::Identity::verify calls it. Signing stays
// with the firmware's own lib/ed25519.
#include <stddef.h>
#include <stdint.h>
#include <openssl/evp.h>

class Ed25519 {
 public:
  static bool verify(const uint8_t sig[64], const uint8_t pub[32], const void* msg, size_t len) {
    EVP_PKEY* key = EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, NULL, pub, 32);
    if (!key) return false;
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    bool ok = EVP_DigestVerifyInit(ctx, NULL, NULL, NULL, key) == 1 &&
              EVP_DigestVerify(ctx, sig, 64, (const uint8_t*)msg, len) == 1;
    EVP_MD_CTX_free(ctx);
    EVP_PKEY_free(key);
    return ok;
  }
};
