"""Set the admin password. Stores only a scrypt hash in .env.

    cd /opt/agent-hub/backend
    sudo -u agenthub ../.venv/bin/python -m app.set_password [path/to/.env]
    sudo systemctl restart agent-hub

Also creates SESSION_SECRET if it's missing. Changing the password logs out all sessions.
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import sys
from pathlib import Path

from .auth import hash_password

DEFAULT_ENV = Path("/opt/agent-hub/.env")
MIN_LENGTH = 12


def set_key(text: str, key: str, value: str, only_if_empty: bool = False) -> str:
    pattern = re.compile(rf"^{key}=(.*)$", re.M)
    m = pattern.search(text)
    if m:
        if only_if_empty and m.group(1).strip():
            return text
        return pattern.sub(lambda _: f"{key}={value}", text, count=1)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ENV if DEFAULT_ENV.exists() else Path(".env")
    if not path.exists():
        print(f"{path} not found. Pass the path to Kiln's .env.", file=sys.stderr)
        return 1
    password = getpass.getpass("New admin password: ")
    if len(password) < MIN_LENGTH:
        print(f"Use at least {MIN_LENGTH} characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat it: ") != password:
        print("Passwords don't match.", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    text = set_key(text, "ADMIN_PASSWORD_HASH", hash_password(password))
    text = set_key(text, "SESSION_SECRET", secrets.token_hex(32), only_if_empty=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)
    print(f"Saved to {path}. Now run: sudo systemctl restart agent-hub")
    return 0


if __name__ == "__main__":
    sys.exit(main())
