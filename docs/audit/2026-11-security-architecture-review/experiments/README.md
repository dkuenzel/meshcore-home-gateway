# Experiments of the review

Scripts the review ran to back findings; each uses only the project's own modules and test
helpers and runs from the repository root with the project's Python environment (they are not
tests and `pytest` does not collect them).

| Script | Backs | Run |
| --- | --- | --- |
| `advert_flood.py` | SEC-010, SEC-011, RES-005: `node` rows and bytes published per advert with invented keys | `python3 docs/audit/2026-11-security-architecture-review/experiments/advert_flood.py 5000` |
| `loop_stall_session.py` | CODE-001, RES-009: a loop stall longer than the command timeout ends the session although the answer arrived | `python3 docs/audit/2026-11-security-architecture-review/experiments/loop_stall_session.py` |
| `loop_stall_asyncio.py` | RES-009: the pure-asyncio variant, which does *not* lose the data (one hop fewer) | `python3 docs/audit/2026-11-security-architecture-review/experiments/loop_stall_asyncio.py` |

Observed on 2026-11-01 with Python 3.11.17 and `paho-mqtt` 2.1.0:

```text
advert_flood.py 5000:  adverts fed: 5000; node rows in history: 5000; db bytes: 5119480; heard nodes in memory: 50; publishes: 8012 (74965228 B); 4.02 s
advert_flood.py 50000: adverts fed: 50000; node rows in history: 50000; db bytes: 14376584; heard nodes in memory: 50; publishes: 80019 (755427752 B); 42.76 s
loop_stall_session.py: stall 0.0 s, answer after 1.0 s: battery 4012 mV; closed False
                       stall 6.0 s, answer after 1.0 s: SESSION FAILED: no answer from the radio
                       stall 4.0 s, answer after 1.0 s: battery 4012 mV; closed False
                       stall 6.0 s, answer after 4.0 s: SESSION FAILED: no answer from the radio
loop_stall_asyncio.py: data seen in all four cases
```
