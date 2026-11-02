"""Experiment of the review (RES-009): the pure asyncio question. The loop is stalled while (a)
data arrives on a socket and (b) an asyncio.wait timeout expires. When the loop resumes, does the
waiter see the data or the timeout? Here the waiter sees the data in every case: the reading
task completes in the iteration before the waiter resumes. The session's extra hop through a
queue's getter task is what loses it (loop_stall_session.py). Run from the repository root:

    python3 docs/audit/2026-11-security-architecture-review/experiments/loop_stall_asyncio.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time


def experiment(stall_s: float, answer_after_s: float) -> str:
    a, b = socket.socketpair()

    def radio() -> None:  # another thread plays the radio: answers after a delay
        time.sleep(answer_after_s)
        b.sendall(b"answer")

    async def main() -> str:
        loop = asyncio.get_running_loop()
        reader, _ = await asyncio.open_connection(sock=a)
        threading.Thread(target=radio, daemon=True).start()
        frame = asyncio.create_task(reader.read(100))
        loop.call_later(0.2, time.sleep, stall_s)  # the stall begins after the wait started
        done, _ = await asyncio.wait({frame}, timeout=5.0)
        return "data seen" if frame in done else "TIMEOUT, data missed"

    try:
        return asyncio.run(main())
    finally:
        a.close()
        b.close()


if __name__ == "__main__":
    for stall, answer in ((0.0, 1.0), (6.0, 1.0), (6.0, 0.1), (4.0, 1.0)):
        sys.stdout.write(f"stall {stall} s, answer after {answer} s: {experiment(stall, answer)}\n")
