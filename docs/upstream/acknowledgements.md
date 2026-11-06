# Direct-message acknowledgements are not authenticated: whoever guesses the text can confirm a message nobody received

Channel: private vulnerability report (Security tab, "Report a vulnerability"), as MeshCore's
SECURITY.md asks.

---

**Component:** companion firmware, the acknowledgement of direct messages. Verified at
`companion-v1.18.0` (commit `81dbae5`):

* `src/helpers/BaseChatMesh.cpp:244-248`: the recipient computes the acknowledgement as
  `SHA-256(timestamp ‖ attempt & 3 ‖ text ‖ sender's public key)[0:4]`. Since 1.18 an attempt
  byte and a random byte follow it.
* `src/Mesh.cpp:560-571`: `createAck` sends those bytes as the payload of a `PAYLOAD_TYPE_ACK`
  packet, neither encrypted nor signed.
* `src/Mesh.cpp:117-128`: the sender takes the first 4 bytes of any ACK packet it has not seen
  before.
* `examples/companion_radio/MyMesh.cpp:424-434`: `processAck` matches them against
  `expected_ack_table`. On a match it pushes `SEND_CONFIRMED` and clears the entry, so the
  retries stop.

**What an attacker can do:** confirm a direct message that the recipient never received. The
sender's radio then reports it as delivered, with a plausible round trip, and stops its retries
and the flood after them.

**Conditions:** the attacker is in radio range of the sender, or of a repeater on the path, and
can transmit raw packets. They need no key and do not have to be a contact. They have to know or
guess the text. The other three inputs to the hash are not secret:

* the sender's public key is in its adverts;
* the attempt number is 0 for the first transmission;
* the timestamp is the sender's clock when it queued the message, so a few seconds of guesses
  around the packet's time on air cover it.

Texts that matter for delivery are often fixed phrases ("Gate open", "Smoke in the kitchen"), so
a short list of candidates is enough. The two bytes added in 1.18 change nothing, because only
the first four are compared (`MyMesh.cpp:425`).

**Remotely exploitable:** over the air, within radio range. Nothing is decrypted or injected;
only the delivery state of the sender's own messages is falsified.

**Reproduction:** a scripted test runs this on the simulated air with the unmodified 1.18.0
firmware. A stand-in node that knows the text transmits the acknowledgement, and the sender's
radio pushes `SEND_CONFIRMED` for a message the recipient never received:
`test_a_stranger_who_knows_the_text_confirms_what_nobody_received` in
https://github.com/dkuenzel/meshcore-home-gateway/blob/main/tests/firmware/test_acknowledgements.py.

**Ideas for a fix** (yours to judge):

* Bind the acknowledgement to the secret the two nodes share. For example, derive it from the
  ECDH secret that already encrypts the message (an HMAC over timestamp, attempt and text), or
  encrypt the ACK payload with it. Only the recipient, who decrypted the message, could then
  produce it.
* Or put a random nonce inside the encrypted message and into the hash, so that the text alone
  no longer gives the code. The random byte that 1.18 appends would do this if it were part of
  the encrypted message and of the comparison.

**Disclosure status:** this analysis has been public since 2026-11-01, in an independent security
review published in our repository
(`docs/audit/2026-11-security-architecture-review/03-security-findings.md`, SEC-001). We report
it here so that you hear it from us and can track it privately if you want to.

Our gateway documents what a confirmation proves. It also offers two mitigations of its own: an
opt-in random suffix on direct messages, which makes the text unguessable, and a flag for
acknowledgements that arrive sooner than the known path allows. The cause is in the protocol,
though, and so is the fix.

Thank you for MeshCore.
