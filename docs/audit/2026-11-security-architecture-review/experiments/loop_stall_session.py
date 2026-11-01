"""Faithful variant: the real CompanionSession over a socket to a thread that plays a radio
answering after a delay; the loop is stalled while the session waits for the answer."""
import asyncio, socket, struct, sys, threading, time, logging
sys.path.insert(0, "src"); sys.path.insert(0, "tests")
from radio_emulator import EmulatedRadio
from meshcore_home_gateway.radio.session import CompanionSession, RadioError
from meshcore_home_gateway.protocol.framing import FrameDecoder, encode_frame
logging.basicConfig(level=logging.ERROR)

class SocketTransport:
    """A TCP-like transport on a socketpair (the Transport protocol)."""
    needs_flush = False
    description = "socketpair"
    def __init__(self, sock): self._sock = sock
    async def open(self): self._reader, self._writer = await asyncio.open_connection(sock=self._sock)
    async def read(self):
        data = await self._reader.read(4096)
        if not data: raise ConnectionError("closed")
        return data
    async def write(self, data): self._writer.write(data); await self._writer.drain()
    async def close(self): self._writer.close()

def radio_thread(sock, radio: EmulatedRadio, slow_from: threading.Event, delay_s: float):
    """Feed frames to the emulator synchronously and write its answers; delay them once asked."""
    loop = asyncio.new_event_loop()
    loop.run_until_complete(radio.open())
    dec = FrameDecoder()
    sock.settimeout(0.05)
    while True:
        try: data = sock.recv(4096)
        except socket.timeout: data = b""
        except OSError: return
        if data:
            loop.run_until_complete(radio.write(data))
        try:
            out = loop.run_until_complete(asyncio.wait_for(radio.read(), 0.01))
        except (asyncio.TimeoutError, TimeoutError): continue
        if slow_from.is_set(): time.sleep(delay_s)
        try: sock.sendall(out)
        except OSError: return

async def main(stall_s: float, answer_after_s: float):
    a, b = socket.socketpair()
    radio = EmulatedRadio(clock=int(time.time()))
    slow = threading.Event()
    threading.Thread(target=radio_thread, args=(b, radio, slow, answer_after_s), daemon=True).start()
    s = CompanionSession(SocketTransport(a), asyncio.Queue(), flush_settle_s=0.0)
    await s.start()
    slow.set()
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, time.sleep, stall_s)  # stall while the session waits for the answer
    try:
        bat = await s.get_battery()
        print(f"stall {stall_s} s, answer after {answer_after_s} s: battery {bat.millivolts} mV; closed {s.closed}")
    except RadioError as exc:
        print(f"stall {stall_s} s, answer after {answer_after_s} s: SESSION FAILED: {exc}")
    await s.close(); a.close(); b.close()

for stall, answer in ((0.0, 1.0), (6.0, 1.0), (4.0, 1.0), (6.0, 4.0)):
    asyncio.run(main(stall, answer))
