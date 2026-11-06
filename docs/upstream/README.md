# Proposals for MeshCore

Drafts for MeshCore's maintainers, from the security and architecture review of 2026-11 and from
running the gateway. None has been sent: the maintainer of this project sends each, by the
channel it names, once he agrees with it. Each draft says what it relies on at the firmware the
gateway is tested with (`companion-v1.18.0`, `tests/firmware/sources.toml`); check that again
before sending.

| Draft | Channel | Why |
| --- | --- | --- |
| [Unauthenticated acknowledgements](acknowledgements.md) | private vulnerability report | SEC-001: a confirmation proves knowledge of the text, not receipt |
| [What the ADVERT push carries](advert-push.md) | public issue | ARC-010: the advert check reads the packet log before the push |
| [A bounded step back for the clock](clock-step-back.md) | public issue | 2.0.0: the gateway restarts the radio to set its clock back |
