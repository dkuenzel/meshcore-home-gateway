"""Experiment: what an advert flood with invented keys does to the observer's memory and the
history database. Uses the project's own pure modules (no radio): feeds NEW_ADVERT records to
the observer the way the bridge does, with a real History on a temporary SQLite file."""
import os, sqlite3, sys, tempfile, time
sys.path.insert(0, "src")
from pathlib import Path
from meshcore_home_gateway.history import History, Retention
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.radio.session import AdvertHeard

class Bus:
    def __init__(self): self.n = 0; self.bytes = 0
    def publish(self, topic, payload, retain=False, qos=1): self.n += 1; self.bytes += len(payload)

tmp = Path(tempfile.mkdtemp())
history = History.open(tmp / "history.sqlite3", Retention())
bus = Bus()
ob = Observer(bus, Topics("meshcore", "radio1"), history=history, wall_clock=time.time)
ob.radio_connected(350, None)
ob.radio_event  # exists
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
t0 = time.time()
for i in range(N):
    key = i.to_bytes(4, "big") + os.urandom(28)
    name = f"Node{i}".encode().ljust(32, b"\0")
    raw = key + bytes([1, 0, 0xFF]) + bytes(64) + name + (1_800_000_000 + i).to_bytes(4, "little") + bytes(8) + bytes(4)
    rec = codec._contact(bytes([0x8A]) + raw)
    ob.radio_event(AdvertHeard(codec.Advert(key, True, rec), time.time() + i * 3, i + 1, 1))
dt = time.time() - t0
db = sqlite3.connect(tmp / "history.sqlite3")
rows = db.execute("select count(*) from node").fetchone()[0]
size = os.path.getsize(tmp / "history.sqlite3") + sum(os.path.getsize(p) for p in tmp.glob("history.sqlite3-*"))
print(f"adverts fed: {N}; node rows in history: {rows}; db bytes: {size}; "
      f"heard nodes in memory: {len(ob.mesh.nodes)}; publishes: {bus.n} ({bus.bytes} B); {dt:.2f} s")
