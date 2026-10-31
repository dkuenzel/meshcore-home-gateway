#pragma once
// mesh::Radio on the simulated air (tests/firmware/air.py): a node sends its transmissions as UDP
// datagrams to the air, which delivers them after their time on air to the nodes linked to the
// sender, with the link's SNR and RSSI, and drops receptions that overlap (collisions).
// Replaces RadioLibWrapper and CustomSX1262Wrapper (src/helpers/radiolib/) of the boards.
//
// Datagrams, node to air: 'H' id u32 freq_khz u32 bw_hz u8 sf u8 cr i8 dbm  hello, every 2 s
//                                                                           and at a change
//                         'T' id u32 airtime_ms packet                      a transmission
// air to node:            'B' u32 ms                    a linked node transmits (channel busy)
//                         'R' i16 snr*4 i16 rssi packet  a reception, at the end of its airtime
//                         'X'                           a reception lost in a collision
// Numbers are little-endian.
#include <Mesh.h>

#include <deque>
#include <vector>

class SimRadio : public mesh::Radio {
  int _sock = -1;
  uint8_t _node_id = 0;
  float _freq = 869.618f, _bw = 62.5f;
  uint8_t _sf = 8, _cr = 5;
  int8_t _tx_power = 20;
  bool _boosted = false;
  bool _tx_active = false;
  unsigned long _tx_end = 0;
  unsigned long _busy_until = 0;
  unsigned long _last_hello = 0;
  float _last_rssi = 0, _last_snr = 0;
  uint32_t n_recv = 0, n_sent = 0, n_recv_errors = 0;
  struct Rx {
    std::vector<uint8_t> data;
    float snr, rssi;
  };
  std::deque<Rx> _rxq;
  void poll();
  void hello();

 public:
  void init(uint8_t node_id, int air_port);
  void begin() override {}
  int recvRaw(uint8_t* bytes, int sz) override;
  uint32_t getEstAirtimeFor(int len_bytes) override;
  float packetScore(float snr, int packet_len) override;
  bool startSendRaw(const uint8_t* bytes, int len) override;
  bool isSendComplete() override;
  void onSendFinished() override { _tx_active = false; }
  bool isInRecvMode() const override { return !_tx_active; }
  bool isReceiving() override;
  void loop() override { poll(); }
  int getNoiseFloor() const override { return -120; }
  float getLastRSSI() const override { return _last_rssi; }
  float getLastSNR() const override { return _last_snr; }

  // the methods of RadioLibWrapper and CustomSX1262Wrapper that the firmware calls
  void setParams(float freq, float bw, uint8_t sf, uint8_t cr);
  void setTxPower(int8_t dbm);
  bool setRxBoostedGainMode(bool en) { _boosted = en; return true; }
  bool getRxBoostedGainMode() const { return _boosted; }
  uint32_t getRngSeed();
  uint32_t getPacketsRecv() const { return n_recv; }
  uint32_t getPacketsRecvErrors() const { return n_recv_errors; }
  uint32_t getPacketsSent() const { return n_sent; }
  void resetStats() { n_recv = n_sent = n_recv_errors = 0; }
  void powerOff() {}
};
