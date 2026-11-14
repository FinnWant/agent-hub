"""Terminal WebSocket checks, called by smoke-test.sh with the logged-in session cookie.

    python terminal-check.py BASE_URL SANDBOX_ID   (cookie value in $AH_SESSION)

Prints PASS/FAIL lines like smoke-test.sh and exits non-zero on any failure.
Uses the `websockets` package from the Agent Hub venv.
"""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlsplit

from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

base, sid = sys.argv[1], sys.argv[2]
cookie = os.environ["AH_SESSION"]
u = urlsplit(base)
ws_url = f"ws://{u.netloc}/api/sandboxes/{sid}/terminal?cols=100&rows=30"
origin = f"{u.scheme}://{u.netloc}"
failed = 0


def report(ok: bool, msg: str) -> None:
    global failed
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}")
    failed += not ok


def refused(**kw) -> bool:
    try:
        with connect(ws_url, open_timeout=10, **kw):
            return False
    except InvalidStatus:
        return True


report(refused(origin=origin), "terminal refused without a session")
report(refused(origin="http://evil.example", additional_headers={"Cookie": f"ah_session={cookie}"}),
       "terminal refused from another origin")

out = b""
try:
    with connect(ws_url, origin=origin, additional_headers={"Cookie": f"ah_session={cookie}"},
                 open_timeout=10) as ws:
        ws.send(b"python3 --version; echo \"user=$(id -un) size=$(stty size)\"\r")
        deadline = time.monotonic() + 15
        while b"user=agent" not in out and time.monotonic() < deadline:
            msg = ws.recv(timeout=deadline - time.monotonic())
            out += msg if isinstance(msg, bytes) else msg.encode()
except Exception as e:  # noqa: BLE001
    out += f"\n[{type(e).__name__}: {e}]".encode()
text = out.decode(errors="replace")
version = next((w for w in text.split() if w.startswith("3.") and w[2:3].isdigit()), "?")
report("Python 3." in text, f"terminal ran python3 --version (Python {version})")
report("user=agent size=30 100" in text, "shell runs as agent with the requested size")
sys.exit(1 if failed else 0)
