#include "SimRadio.h"

#include <Arduino.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static struct sockaddr_in air_addr;

void SimRadio::init(uint8_t node_id, int air_port) {
  _node_id = node_id;
  _sock = socket(AF_INET, SOCK_DGRAM, 0);
  fcntl(_sock, F_SETFL, O_NONBLOCK);
  struct sockaddr_in me = {};
  me.sin_family = AF_INET;
  me.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  bind(_sock, (struct sockaddr*)&me, sizeof(me));
  air_addr.sin_family = AF_INET;
  air_addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  air_addr.sin_port = htons(air_port);
  // the first hello comes with the firmware's settings (setParams) or from poll()
}

void SimRadio::hello() {
  if (_sock < 0) return;
  uint8_t m[13];
  // the settings are floats: 869.618 MHz is 869.617981, so the frequency goes in whole kHz
  uint32_t freq_khz = (uint32_t)lround(_freq * 1e3), bw_hz = (uint32_t)lround(_bw * 1e3);
  m[0] = 'H';
  m[1] = _node_id;
  memcpy(&m[2], &freq_khz, 4);
  memcpy(&m[6], &bw_hz, 4);
  m[10] = _sf;
  m[11] = _cr;
  m[12] = (uint8_t)_tx_power;
  sendto(_sock, m, sizeof(m), 0, (struct sockaddr*)&air_addr, sizeof(air_addr));
  _last_hello = millis();
}

void SimRadio::setParams(float freq, float bw, uint8_t sf, uint8_t cr) {
  _freq = freq;
  _bw = bw;
  _sf = sf;
  _cr = cr;
  hello();
}

void SimRadio::setTxPower(int8_t dbm) {
  _tx_power = dbm;
  hello();
}

void SimRadio::poll() {
  if (_sock < 0) return;
  if (millis() - _last_hello > 2000) hello();
  uint8_t buf[512];
  for (;;) {
    ssize_t n = recv(_sock, buf, sizeof(buf), 0);
    if (n <= 0) break;
    if (buf[0] == 'B' && n >= 5) {
      uint32_t ms;
      memcpy(&ms, &buf[1], 4);
      unsigned long until = millis() + ms;
      if ((long)(until - _busy_until) > 0) _busy_until = until;
    } else if (buf[0] == 'R' && n >= 5) {
      int16_t snr4, rssi;
      memcpy(&snr4, &buf[1], 2);
      memcpy(&rssi, &buf[3], 2);
      // half duplex is the air's to decide: a reception during this node's transmission is an 'X'
      _rxq.push_back({std::vector<uint8_t>(buf + 5, buf + n), snr4 / 4.0f, (float)rssi});
    } else if (buf[0] == 'X') {
      n_recv_errors++;
    }
  }
}

int SimRadio::recvRaw(uint8_t* bytes, int sz) {
  poll();
  if (_rxq.empty()) return 0;
  Rx rx = _rxq.front();
  _rxq.pop_front();
  int len = rx.data.size() < (size_t)sz ? rx.data.size() : sz;
  memcpy(bytes, rx.data.data(), len);
  _last_snr = rx.snr;
  _last_rssi = rx.rssi;
  n_recv++;
  return len;
}

// LoRa time on air (Semtech AN1200.13: explicit header, CRC on, low data rate optimisation from
// 16 ms symbols), as RadioLib's SX126x::getTimeOnAir computes it for RadioLibWrapper
// (src/helpers/radiolib/RadioLibWrappers.cpp), with MeshCore's preamble of 32 symbols at SF 8 or
// less and 16 above.
uint32_t SimRadio::getEstAirtimeFor(int len) {
  double tsym = (double)(1 << _sf) / _bw;  // ms, bw in kHz
  int de = tsym >= 16.0 ? 1 : 0;
  int preamble = _sf <= 8 ? 32 : 16;
  double num = 8.0 * len - 4.0 * _sf + 28 + 16;
  double payload_sym = 8 + fmax(ceil(num / (4.0 * (_sf - 2 * de))) * _cr, 0);
  return (uint32_t)((preamble + 4.25 + payload_sym) * tsym);
}

// RadioLibWrapper::packetScoreInt with the configured spreading factor, as
// CustomSX1262Wrapper::packetScore calls it. After src/helpers/radiolib/RadioLibWrappers.cpp of
// MeshCore (MIT License, Copyright (c) 2025 Scott Powell / rippleradios.com).
float SimRadio::packetScore(float snr, int packet_len) {
  static const float threshold[] = {-7.5, -10, -12.5, -15, -17.5, -20};  // SF 7 to 12
  if (_sf < 7 || _sf > 12) return 0.0f;
  if (snr < threshold[_sf - 7]) return 0.0f;
  float s = (snr - threshold[_sf - 7]) / 10.0f;
  float c = 1 - (packet_len / 256.0f);
  return fmaxf(0.0f, fminf(1.0f, s * c));
}

bool SimRadio::startSendRaw(const uint8_t* bytes, int len) {
  if (len < 0 || len > 256) return false;
  uint32_t ms = getEstAirtimeFor(len);
  uint8_t m[6 + 256];
  m[0] = 'T';
  m[1] = _node_id;
  memcpy(&m[2], &ms, 4);
  memcpy(&m[6], bytes, len);
  sendto(_sock, m, 6 + len, 0, (struct sockaddr*)&air_addr, sizeof(air_addr));
  _tx_active = true;
  _tx_end = millis() + ms;
  n_sent++;
  return true;
}

bool SimRadio::isSendComplete() { return _tx_active && (long)(millis() - _tx_end) >= 0; }

bool SimRadio::isReceiving() {
  poll();
  return (long)(_busy_until - millis()) > 0;
}

// The seed of the firmware's fast random numbers: from SIM_SEED for a run that repeats
uint32_t SimRadio::getRngSeed() {
  const char* s = getenv("SIM_SEED");
  if (s) return (uint32_t)strtoul(s, NULL, 0) * 2654435761u;
  uint32_t seed = 0;
  FILE* f = fopen("/dev/urandom", "rb");
  if (f) {
    if (fread(&seed, 4, 1, f) != 1) seed = 0;
    fclose(f);
  }
  return seed;
}
