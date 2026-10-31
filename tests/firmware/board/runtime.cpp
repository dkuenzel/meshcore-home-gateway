// The runtime of a simulated node: the Arduino core's time and random numbers, the USB serial on
// a pseudo terminal, LittleFS in a host directory, the board objects of target.h, control lines
// on standard input (its end stops the node), and a main() that calls the firmware's own setup()
// and loop().
//
// Environment: SIM_NAME (log prefix), SIM_NODE_ID (1..255 on the air), SIM_AIR_PORT (the air's
// UDP port), SIM_FS_DIR (the flash), SIM_PTY_LINK (a symlink to the serial port), SIM_SEED (keys
// and random numbers that repeat), SIM_GPS ("lat,lon,alt" or "nofix"; unset: no GPS module),
// SIM_BATT_MV, SIM_RTC_WALL (set: the clock starts at the host's time, as on a board with an RTC).
#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>
#include <Wire.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/inotify.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include <filesystem>
#include <random>
#include <string>
#include <thread>

#include "target.h"

static const char* node_name() {
  const char* n = getenv("SIM_NAME");
  return n ? n : "node";
}

// ---------------------------------------------------------------- time, random numbers

static struct timespec t0;

// The clock starts at its first call: global constructors (Dispatcher::Dispatcher reads millis())
// run before main(), as on a board where millis() counts from power-on.
unsigned long millis() {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  if (t0.tv_sec == 0 && t0.tv_nsec == 0) t0 = t;
  double ms = (t.tv_sec - t0.tv_sec) * 1000.0 + (t.tv_nsec - t0.tv_nsec) / 1e6;
  return (unsigned long)ms + 1;
}
unsigned long micros() { return millis() * 1000; }
void delay(unsigned long ms) { usleep((useconds_t)(ms * 1000)); }

static std::mt19937 prng(1);
void randomSeed(unsigned long seed) { prng.seed((uint32_t)seed); }
long random(long max_excl) { return max_excl <= 0 ? 0 : (long)(prng() % (uint32_t)max_excl); }
long random(long lo, long hi) { return hi <= lo ? lo : lo + random(hi - lo); }

// ---------------------------------------------------------------- USB serial on a pseudo terminal

static int pty_master = -1;
// The node keeps the terminal's other end open, or the master would see a hang-up whenever the
// gateway closes the port.
static int pty_slave = -1;
static int pty_closes = -1;  // inotify: another program closed the port
static uint8_t rx_buf[4096];
static size_t rx_len = 0, rx_pos = 0;

static void pty_open() {
  const char* master_env = getenv("SIM_PTY_FD");  // after a reboot (re-exec) the same terminal
  const char* slave_env = getenv("SIM_PTY_SLAVE_FD");
  if (master_env && slave_env) {
    pty_master = atoi(master_env);
    pty_slave = atoi(slave_env);
  } else {
    pty_master = posix_openpt(O_RDWR | O_NOCTTY);
    if (pty_master < 0 || grantpt(pty_master) != 0 || unlockpt(pty_master) != 0) {
      perror("pseudo terminal");
      exit(2);
    }
    pty_slave = open(ptsname(pty_master), O_RDWR | O_NOCTTY);
    struct termios tio;
    tcgetattr(pty_slave, &tio);
    cfmakeraw(&tio);
    tcsetattr(pty_slave, TCSANOW, &tio);
    fcntl(pty_master, F_SETFL, O_NONBLOCK);
    char buf[16];
    snprintf(buf, sizeof(buf), "%d", pty_master);
    setenv("SIM_PTY_FD", buf, 1);
    snprintf(buf, sizeof(buf), "%d", pty_slave);
    setenv("SIM_PTY_SLAVE_FD", buf, 1);
  }
  const char* slave = ptsname(pty_master);
  const char* link = getenv("SIM_PTY_LINK");
  if (link) {
    unlink(link);
    if (symlink(slave, link) != 0) perror(link);
  }
  fprintf(stderr, "[%s] serial port %s%s%s\n", node_name(), slave, link ? " at " : "", link ? link : "");
}

// The gateway opens the port exclusively (TIOCEXCL). A USB serial port loses that lock when the
// gateway closes it, because the device closes with it; this terminal stays open in the node, so
// a thread of the node clears the lock when another program closes the port. The thread waits
// for the close itself, not for the firmware's loop, so the lock goes a fraction of a millisecond
// after the close. Without it, only root could open the port a second time.
static void pty_release() {
  char events[512];
  for (;;) {
    ssize_t n = read(pty_closes, events, sizeof(events));  // blocks until a close
    if (n > 0) ioctl(pty_slave, TIOCNXCL);
    if (n < 0 && errno != EINTR) return;
  }
}

static void pty_watch() {
  pty_closes = inotify_init1(IN_CLOEXEC);
  inotify_add_watch(pty_closes, ptsname(pty_master), IN_CLOSE_WRITE | IN_CLOSE_NOWRITE);
  std::thread(pty_release).detach();  // a reboot (exec) ends it, and the new program starts one
}

static void pty_fill() {
  if (rx_pos < rx_len) return;
  ssize_t n = read(pty_master, rx_buf, sizeof(rx_buf));
  rx_pos = 0;
  rx_len = n > 0 ? n : 0;
}

int SimSerialPort::available() { pty_fill(); return rx_len - rx_pos; }
int SimSerialPort::read() { pty_fill(); return rx_pos < rx_len ? rx_buf[rx_pos++] : -1; }
int SimSerialPort::peek() { pty_fill(); return rx_pos < rx_len ? rx_buf[rx_pos] : -1; }

size_t SimSerialPort::write(const uint8_t* buf, size_t len) {
  size_t done = 0;
  for (int tries = 0; done < len && tries < 200; tries++) {  // as USB CDC: wait a little, then drop
    ssize_t n = ::write(pty_master, buf + done, len - done);
    if (n > 0) {
      done += n;
      continue;
    }
    if (n < 0 && errno != EAGAIN) break;
    usleep(1000);
  }
  return done;
}

// Text goes to the node's log, each line with the node's name and its clock in milliseconds.
size_t SimSerialPort::printText(const char* text, size_t len) {
  static bool line_start = true;
  for (size_t i = 0; i < len; i++) {
    char c = text[i];
    if (c == '\r') continue;
    if (line_start) fprintf(stderr, "[%s %lu] ", node_name(), millis());
    fputc(c, stderr);
    line_start = c == '\n';
  }
  return len;
}

SimSerialPort Serial;
SimGpsUart Serial1;
int SimGpsUart::available() { return sim_gps.attached ? 1 : 0; }

// ---------------------------------------------------------------- LittleFS in a host directory

namespace fs {

std::string FS::hostPath(const char* path) const {
  const char* root = getenv("SIM_FS_DIR");
  std::string r = root ? root : "./fs";
  if (path[0] != '/') r += "/";
  return r + path;
}

bool FS::begin() {
  std::error_code ignored;
  std::filesystem::create_directories(hostPath("/"), ignored);
  return true;
}

File FS::open(const char* path, const char* mode) {
  const char* m = mode[0] == 'w' ? "wb" : mode[0] == 'a' ? "ab" : (strchr(mode, '+') ? "r+b" : "rb");
  FILE* f = fopen(hostPath(path).c_str(), m);
  return f ? File(f, path) : File();
}

bool FS::exists(const char* path) { struct stat st; return stat(hostPath(path).c_str(), &st) == 0; }
bool FS::remove(const char* path) { return unlink(hostPath(path).c_str()) == 0; }
bool FS::mkdir(const char* path) { return ::mkdir(hostPath(path).c_str(), 0755) == 0 || errno == EEXIST; }
bool FS::rmdir(const char* path) { return ::rmdir(hostPath(path).c_str()) == 0; }
bool FS::rename(const char* a, const char* b) { return ::rename(hostPath(a).c_str(), hostPath(b).c_str()) == 0; }
bool FS::info(FSInfo& info) { info.totalBytes = 1024 * 1024; info.usedBytes = 64 * 1024; return true; }

bool FS::format() {
  std::error_code error;
  for (const auto& entry : std::filesystem::directory_iterator(hostPath("/"), error)) {
    std::filesystem::remove_all(entry.path(), error);
  }
  return !error;
}

int File::available() { return _f ? (int)(size() - position()) : 0; }
int File::read() { return _f ? fgetc(_f.get()) : -1; }
int File::peek() {
  if (!_f) return -1;
  int c = fgetc(_f.get());
  if (c != EOF) ungetc(c, _f.get());
  return c;
}
int File::read(uint8_t* buf, size_t len) { return _f ? (int)fread(buf, 1, len, _f.get()) : -1; }
size_t File::write(const uint8_t* buf, size_t len) { return _f ? fwrite(buf, 1, len, _f.get()) : 0; }
bool File::seek(uint32_t pos, SeekMode mode) { return _f && fseek(_f.get(), pos, mode) == 0; }
size_t File::position() const { return _f ? ftell(_f.get()) : 0; }
size_t File::size() const {
  struct stat st;
  return _f && fstat(fileno(_f.get()), &st) == 0 ? st.st_size : 0;
}
void File::flush() { if (_f) fflush(_f.get()); }
const char* File::name() const {
  size_t p = _name.rfind('/');
  return _name.c_str() + (p == std::string::npos ? 0 : p + 1);
}

}  // namespace fs

fs::FS LittleFS;

TwoWire Wire, Wire1;

// declared in helpers/sensors/LocationProvider.h, defined by no file the firmware builds with
void LocationProvider::sendSentence(const char*) {}

// ---------------------------------------------------------------- board objects (target.cpp)

SimBoard board;
SimRadio radio_driver;
VolatileRTCClock rtc_clock;
SimLocationProvider sim_gps;
EnvironmentSensorManager sensors(sim_gps);

static char** saved_argv;

// A reboot starts the program again in the same process, with the same terminal and flash.
void SimBoard::reboot() {
  fprintf(stderr, "[%s] reboot\n", node_name());
  execv("/proc/self/exe", saved_argv);
  _exit(1);
}

// RadioNoiseListener on the boards: the identity's random bytes
class SimIdentityRNG : public mesh::RNG {
 public:
  void random(uint8_t* dest, size_t sz) override {
    const char* s = getenv("SIM_SEED");
    if (s) {  // the same keys for the same seed
      std::mt19937 g((uint32_t)strtoul(s, NULL, 0) ^ 0x5eed);
      for (size_t i = 0; i < sz; i++) dest[i] = g() & 0xFF;
      return;
    }
    FILE* f = fopen("/dev/urandom", "rb");
    if (!f || fread(dest, 1, sz, f) != sz) abort();
    fclose(f);
  }
};

mesh::LocalIdentity radio_new_identity() {
  SimIdentityRNG rng;
  return mesh::LocalIdentity(&rng);
}

bool radio_init() {
  const char* id = getenv("SIM_NODE_ID");
  const char* port = getenv("SIM_AIR_PORT");
  if (!id || !port) {
    fprintf(stderr, "[%s] SIM_NODE_ID and SIM_AIR_PORT must be set\n", node_name());
    exit(2);
  }
  radio_driver.init(atoi(id), atoi(port));
  if (getenv("SIM_RTC_WALL")) rtc_clock.setCurrentTime(time(NULL));
  return true;
}

// ---------------------------------------------------------------- control lines on stdin

// The board's user button, where the firmware has one (board/companion.cpp)
void sim_button_advert() __attribute__((weak));

// gps attach | gps detach | gps nofix | gps <lat> <lon> [<alt>] | batt <mV> | temp <C>|nan | reboot
// | advert (the Advert button)
static void control(const char* line) {
  char cmd[16] = {0}, a[32] = {0}, b[32] = {0}, c[32] = {0};
  int n = sscanf(line, "%15s %31s %31s %31s", cmd, a, b, c);
  if (n < 1) return;
  fprintf(stderr, "[%s] control: %s\n", node_name(), line);
  if (strcmp(cmd, "gps") == 0) {
    if (strcmp(a, "attach") == 0) {
      sim_gps.attached = true;
    } else if (strcmp(a, "detach") == 0) {
      sim_gps.attached = false;
      sim_gps.fix = false;
    } else if (strcmp(a, "nofix") == 0) {
      sim_gps.fix = false;
    } else if (n >= 3) {
      sim_gps.attached = sim_gps.fix = true;
      sim_gps.sats = 9;
      sim_gps.lat = atof(a);
      sim_gps.lon = atof(b);
      sim_gps.alt = n >= 4 ? atof(c) : 0;
    }
  } else if (strcmp(cmd, "batt") == 0) {
    board.batt_mv = atoi(a);
  } else if (strcmp(cmd, "temp") == 0) {
    board.mcu_temp = strcmp(a, "nan") == 0 ? NAN : atof(a);
  } else if (strcmp(cmd, "reboot") == 0) {
    board.reboot();
  } else if (strcmp(cmd, "advert") == 0) {
    if (sim_button_advert) {
      sim_button_advert();
    } else {
      fprintf(stderr, "[%s] this firmware has no Advert button\n", node_name());
    }
  }
}

// The end of the input stops the node: the test that started it is gone, and no node outlives it.
static void poll_stdin() {
  static std::string pending;
  struct pollfd p = {0, POLLIN, 0};
  if (poll(&p, 1, 0) <= 0) return;
  char buf[256];
  ssize_t n = ::read(0, buf, sizeof(buf));
  if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) {
    fprintf(stderr, "[%s] end of input, stopping\n", node_name());
    exit(0);
  }
  if (n < 0) return;
  pending.append(buf, n);
  size_t nl;
  while ((nl = pending.find('\n')) != std::string::npos) {
    control(pending.substr(0, nl).c_str());
    pending.erase(0, nl + 1);
  }
}

// ---------------------------------------------------------------- main

void setup();
void loop();

int main(int argc, char** argv) {
  (void)argc;
  saved_argv = argv;
  millis();  // start the clock if no constructor did
  setvbuf(stderr, NULL, _IOLBF, 0);
  const char* gps = getenv("SIM_GPS");
  if (gps) {
    sim_gps.attached = true;
    if (strcmp(gps, "nofix") != 0) {
      sim_gps.fix = sscanf(gps, "%lf,%lf,%lf", &sim_gps.lat, &sim_gps.lon, &sim_gps.alt) >= 2;
      sim_gps.sats = 9;
    }
  }
  if (const char* v = getenv("SIM_BATT_MV")) board.batt_mv = atoi(v);
  LittleFS.begin();
  pty_open();
  pty_watch();
  setup();
  fprintf(stderr, "[%s] setup done\n", node_name());
  for (;;) {
    poll_stdin();
    loop();
    usleep(500);
  }
}
