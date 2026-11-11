# Setting the clock back: a comment on MeshCore's pull request #3195

Channel: comment on https://github.com/meshcore-dev/MeshCore/pull/3195 ("Allow companion BLE/USB
CMD_SET_DEVICE_TIME to set clock backwards"). The request was raised before this draft: #3195 and
#1349 would let a client set the clock back, and #3741 would stop the start from setting it back.
This comment adds a client's case and the order in which the changes matter.

---

**A client's case for #3195.** Our home gateway (https://github.com/dkuenzel/meshcore-home-gateway)
runs a companion radio over USB on a host whose clock follows NTP, and keeps the radio's clock on
the host's. Today it can only move the clock forward (`MyMesh.cpp:1287-1296` at
`companion-v1.18.0`). To set a clock back it sends `CMD_REBOOT`, after writing back the contact
times the clock stamped ahead with `CMD_ADD_UPDATE_CONTACT`, so that `bootstrapRTCfromContacts`
(`BaseChatMesh.cpp:60-70`) starts the clock behind the host's (#3050); the next connect sets it
forward. The radio is off the air for a few seconds each time. We see two reasons to set it back:

* clocks that run fast, as in #3173 and #1349, which we set back at most once a day;
* on nRF52 boards without a clock chip, the jump of 100,663 s every 48.5 days of uptime (#3565,
  fixed by #3595), which we set back at once: the sooner the clock is back, the fewer nodes keep
  the radio's adverts ahead and drop the next ones as replays (`BaseChatMesh.cpp:131-136`).

**A bound for the client.** We limit the step ourselves: up to an hour, or whole jumps of #3565,
because a node that took an advert stamped ahead ignores the radio until real time passes it. A
client that knows its clock is right can decide that; the firmware need not.

**The order matters.** #3741 makes the start keep a later clock. Without #3195 or #1349 that
removes the one way a client has today to set a clock back: the restart would leave it ahead. We
would welcome #3195 (or #1349) in the same release as #3741, or before it.

Happy to test a change on the simulated air we run the firmware on, or on hardware.
