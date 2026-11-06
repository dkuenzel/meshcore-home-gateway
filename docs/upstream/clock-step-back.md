# [Feature request] Companion protocol: let the client set the clock back by a bounded step

Channel: public issue in meshcore-dev/MeshCore.

---

**Context.** Our home gateway (https://github.com/dkuenzel/meshcore-home-gateway) runs a
companion radio over USB on a host whose clock follows NTP, and keeps the radio's clock on the
host's.

**What a client can do today** (`companion-v1.18.0`):

* `CMD_SET_DEVICE_TIME` takes only a time no earlier than the radio's (`MyMesh.cpp:1287-1296`);
  the repeater's command line refuses the same ("clock cannot go backwards",
  `CommonCLI.cpp:218,237`). A clock that runs fast, or that someone set ahead, stays ahead.
* At every start the companion sets its clock to the newest last-modified time of its contacts
  plus a second (`bootstrapRTCfromContacts`, `BaseChatMesh.cpp:60-70`), also back and also on
  boards with a clock chip. So the one way back is `CMD_REBOOT`, after which the client sets the
  clock forward. Since 2.0.0 our gateway does exactly that, for a clock between a minute and an
  hour ahead, at most once a day: the radio is off the air for a few seconds, and over USB it
  goes away and comes back.

**Why forward only makes sense.** Nodes drop an advert that is not newer than the last one they
stored (`BaseChatMesh.cpp:131-136`), and repeaters drop a login or request that is not newer
than the client's last (`simple_repeater/MyMesh.cpp:114,676`). After a step back, those nodes
ignore the radio until real time passes the times it sent before. For a step of days that is
harmful; for a step of seconds or minutes it costs nothing in practice, as adverts and requests
are minutes or hours apart.

**Request.** Let `CMD_SET_DEVICE_TIME` set the clock back by a bounded step, for example at most
a few minutes (a constant, or a byte in the command that names the client's limit), and keep
refusing larger steps. A client that keeps the radio on an NTP clock would then never have to
restart it.

Happy to test a change on the simulated air we run the firmware on, or on hardware.
