# [Feature request] Companion protocol: let the ADVERT push (0x80) carry what the accepted advert held

Channel: public issue in meshcore-dev/MeshCore.

---

**Context.** Our home gateway (https://github.com/dkuenzel/meshcore-home-gateway) talks to a
companion radio over USB. It warns a household when one of its companions shares its position in
its adverts, which everyone in range can read. To do that it has to know what the newest advert
of a contact carried.

**What the gateway gets today.** `PUSH_CODE_ADVERT` (0x80) carries only the contact's public key
(`MyMesh.cpp:358-367` at `companion-v1.18.0`). The contact record keeps a position the adverts no
longer carry (`BaseChatMesh.cpp:193-196`), so a table read cannot tell whether the newest advert
still shares one. The gateway therefore infers it from the raw packet in the `LOG_RX_DATA` push
(0x88) directly before the `ADVERT` push. That relies on behaviour no interface promises:

1. The radio logs a received packet before it handles it (`src/Dispatcher.cpp:199`).
2. With a receive delay of 0, the default, a flood packet is handled in the pass that logged it
   (`src/Dispatcher.cpp:243-246`).
3. Handling an accepted advert writes the push at once, with no other frame between the log and
   the push (`MyMesh.cpp:358-367`).

A release that reorders this loop, or a non-zero receive delay, breaks the inference. A logged
advert is also unverified, because its signature is checked after the log
(`src/Mesh.cpp:271-290`). The client has to wait for the push to learn which logged copy the
radio accepted.

**Request.** Let `ADVERT` (0x80) also carry what the radio accepted: at least the advert's
timestamp and its app-data flags byte (`ADV_LATLON_MASK` and the others), ideally the app data
itself (at most 32 bytes, `MeshCore.h:12`). These could be appended after the public key, so that
clients that read 32 bytes keep working, or sent as a new push code if you prefer.

**What it would change.** A client would know exactly what the accepted advert carried,
independent of frame order and receive delay, and could skip parsing raw packets for this.
`NEW_ADVERT` (0x8A) already carries the record made from the advert alone
(`BaseChatMesh.cpp:108-120`); the request makes `ADVERT` match it for contacts already in the
table.

Happy to test a change on the simulated air we run the firmware on, or on hardware.
