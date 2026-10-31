# Native build of MeshCore's companion and repeater firmware for the simulated air
# (docs/testing.md#tests-against-the-real-firmware). The top-level `make firmware` fetches the
# pinned sources and calls this file:
#   make -C tests/firmware SOURCES=<fetched sources> OUT=<build directory>
# Needs g++ (C++17) and OpenSSL's headers and library (libssl-dev).

SOURCES ?= ../../build/firmware/src
OUT ?= ../../build/firmware
CXX ?= g++
CC ?= gcc

MC := $(SOURCES)/meshcore
LPP := $(SOURCES)/cayennelpp/src

# Every node on the EU/UK Narrow preset with CR 4/8, the settings the hardware checklist assumes:
# both firmwares take LORA_CR as the coding rate of a node that has no saved preferences.
RADIO := -DLORA_FREQ=869.618 -DLORA_BW=62.5 -DLORA_SF=8 -DLORA_CR=8
# RP2040_PLATFORM selects the LittleFS code paths of the stores (shim/FS.h). Packet logging goes
# to the node's log, not to the serial port (shim/Arduino.h).
COMMON := -DRP2040_PLATFORM -DARDUINO=100 $(RADIO) -DENV_INCLUDE_GPS=1 -DPIN_GPS_TX=-1 \
  -DPIN_GPS_RX=-1 -DMESH_PACKET_LOGGING=1 -DFIRMWARE_BUILD_DATE='"sim"'
COMPANION_DEFS := $(COMMON) -DENABLE_USB_INTERFACE -DMAX_CONTACTS=350 -DMAX_GROUP_CHANNELS=40 \
  -DENABLE_PRIVATE_KEY_IMPORT=1 -DENABLE_PRIVATE_KEY_EXPORT=1
REPEATER_DEFS := $(COMMON) -DADVERT_NAME='"Repeater"' -DADMIN_PASSWORD='"sim-admin"' \
  -DMAX_NEIGHBOURS=50

# -w: the firmware and the libraries are third-party code, built as they are
FLAGS := -O1 -g -w -MMD -MP
# V=1 shows the compiler's command lines
Q := $(if $(filter 1,$(V)),,@)
INCLUDES := -Ishim -Iboard -I$(LPP) -I$(MC)/src -I$(MC)/lib/ed25519

MESH := $(addprefix $(MC)/src/,Dispatcher.cpp Identity.cpp Mesh.cpp Packet.cpp Utils.cpp \
  helpers/BaseChatMesh.cpp helpers/AdvertDataHelpers.cpp helpers/TxtDataHelpers.cpp \
  helpers/IdentityStore.cpp helpers/StaticPoolPacketManager.cpp \
  helpers/ArduinoSerialInterface.cpp helpers/TransportKeyStore.cpp helpers/RegionMap.cpp \
  helpers/ConfigSerializer.cpp helpers/DynamicConfigSerializer.cpp \
  helpers/CommonRadioPrefs.cpp helpers/sensors/EnvironmentSensorManager.cpp)
BOARD := board/runtime.cpp board/SimRadio.cpp board/crypto.cpp
COMPANION := $(MESH) $(LPP)/CayenneLPP.cpp $(BOARD) \
  $(addprefix $(MC)/examples/companion_radio/,main.cpp MyMesh.cpp DataStore.cpp)
REPEATER := $(MESH) $(LPP)/CayenneLPP.cpp $(BOARD) \
  $(MC)/src/helpers/CommonCLI.cpp $(MC)/src/helpers/ClientACL.cpp \
  $(addprefix $(MC)/examples/simple_repeater/,main.cpp MyMesh.cpp)
ED25519 := $(wildcard $(MC)/lib/ed25519/*.c)

# Objects by firmware and by source path: the two firmwares share no object built with the other
# one's defines, and both have a main.cpp and a MyMesh.cpp.
objects = $(patsubst $(MC)/%.cpp,$(OUT)/obj/$(1)/meshcore/%.o,$(patsubst \
  $(LPP)/%.cpp,$(OUT)/obj/$(1)/cayennelpp/%.o,$(patsubst board/%.cpp,$(OUT)/obj/$(1)/board/%.o,$(2))))
COMPANION_OBJS := $(call objects,companion,$(COMPANION))
REPEATER_OBJS := $(call objects,repeater,$(REPEATER))
ED25519_OBJS := $(patsubst $(MC)/lib/ed25519/%.c,$(OUT)/obj/ed25519/%.o,$(ED25519))

$(OUT)/obj/companion/%.o: DEFS := $(COMPANION_DEFS) -I$(MC)/examples/companion_radio
$(OUT)/obj/repeater/%.o: DEFS := $(REPEATER_DEFS) -I$(MC)/examples/simple_repeater

define compile
@mkdir -p $(dir $@)
@echo "  CXX  $(@:$(OUT)/obj/%=%)"
$(Q)$(CXX) -std=gnu++17 $(FLAGS) $(DEFS) $(INCLUDES) -c $< -o $@
endef

define rules
$(OUT)/obj/$(1)/meshcore/%.o: $(MC)/%.cpp
	$$(compile)
$(OUT)/obj/$(1)/cayennelpp/%.o: $(LPP)/%.cpp
	$$(compile)
$(OUT)/obj/$(1)/board/%.o: board/%.cpp
	$$(compile)
endef
$(foreach firmware,companion repeater,$(eval $(call rules,$(firmware))))

.PHONY: all clean
all: $(OUT)/bin/companion $(OUT)/bin/repeater

$(OUT)/bin/%:
	@mkdir -p $(dir $@)
	@echo "  LD   $(@:$(OUT)/%=%)"
	$(Q)$(CXX) -o $@ $^ -lcrypto

$(OUT)/bin/companion: $(COMPANION_OBJS) $(ED25519_OBJS)
$(OUT)/bin/repeater: $(REPEATER_OBJS) $(ED25519_OBJS)

$(OUT)/obj/ed25519/%.o: $(MC)/lib/ed25519/%.c
	@mkdir -p $(dir $@)
	@echo "  CC   $(@:$(OUT)/obj/%=%)"
	$(Q)$(CC) $(FLAGS) -c $< -o $@

clean:
	rm -rf $(OUT)/bin $(OUT)/obj

-include $(COMPANION_OBJS:.o=.d) $(REPEATER_OBJS:.o=.d) $(ED25519_OBJS:.o=.d)
