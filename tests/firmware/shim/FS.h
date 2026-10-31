#pragma once
// fs::FS and fs::File with the API of the RP2040 Arduino core's LittleFS, kept in a directory of
// the host (SIM_FS_DIR). The firmware is built with RP2040_PLATFORM, so DataStore and IdentityStore
// take their LittleFS code paths. The code is in board/runtime.cpp.
#include <Arduino.h>

#include <memory>
#include <string>

namespace fs {

enum SeekMode { SeekSet = 0, SeekCur = 1, SeekEnd = 2 };

struct FSInfo {
  size_t totalBytes = 0, usedBytes = 0, blockSize = 4096, pageSize = 256, maxOpenFiles = 8, maxPathLength = 64;
};

class File : public Stream {
  std::shared_ptr<FILE> _f;
  std::string _name;

 public:
  File() {}
  File(FILE* f, const std::string& name) : _f(f, [](FILE* p) { if (p) fclose(p); }), _name(name) {}
  operator bool() const { return (bool)_f; }
  int available() override;
  int read() override;
  int peek() override;
  int read(uint8_t* buf, size_t len);
  size_t write(uint8_t b) override { return write(&b, 1); }
  size_t write(const uint8_t* buf, size_t len) override;
  using Print::write;
  bool seek(uint32_t pos, SeekMode mode = SeekSet);
  size_t position() const;
  size_t size() const;
  void flush() override;
  void close() { _f.reset(); }
  const char* name() const;
  bool isDirectory() const { return false; }
  File openNextFile() { return File(); }
};

class FS {
 public:
  virtual ~FS() = default;
  bool begin();
  File open(const char* path, const char* mode = "r");
  bool exists(const char* path);
  bool remove(const char* path);
  bool mkdir(const char* path);
  bool rmdir(const char* path);
  bool rename(const char* from, const char* to);
  bool info(FSInfo& info);
  bool format();
  std::string hostPath(const char* path) const;
};

}  // namespace fs

using fs::File;
using fs::FS;
using fs::FSInfo;
