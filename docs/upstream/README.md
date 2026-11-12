# Proposals for MeshCore

Drafts for MeshCore's maintainers, from the security and architecture review of 2026-11 and from
running the gateway. None has been sent: the maintainer of this project sends each, by the
channel it names, once they agree with it. Each draft says what it relies on at the firmware the
gateway is tested with (`companion-v1.18.0`, `tests/firmware/sources.toml`); check that again
before sending.

Checked on 2026-11-11 against MeshCore's `main` (`d2096bd`) and `dev` (`6e6b7b1`): no release
after 1.18.0, and neither branch changes the code the drafts cite. Its issues and pull requests
raise nothing on acknowledgements or the `ADVERT` push. Setting the clock back is requested
already (#1349, #3195), so that draft became a comment on #3195.

| Draft | Channel | Why |
| --- | --- | --- |
| [Unauthenticated acknowledgements](acknowledgements.md) | private vulnerability report | SEC-001: a confirmation proves knowledge of the text, not receipt |
| [What the ADVERT push carries](advert-push.md) | public issue | ARC-010: the advert check reads the packet log before the push |
| [Setting the clock back](clock-step-back.md) | comment on pull request #3195 | the gateway restarts the radio to set its clock back; #3741 would end that |
