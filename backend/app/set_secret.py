"""Store an agent credential in .env (mode 600). Input is hidden and never echoed or logged.

    cd /opt/agent-hub/backend
    sudo -u agenthub ../.venv/bin/python -m app.set_secret claude    # from `claude setup-token`
    sudo -u agenthub ../.venv/bin/python -m app.set_secret github    # fine-grained GitHub token
    sudo -u agenthub ../.venv/bin/python -m app.set_secret github --clear
    sudo systemctl restart agent-hub
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from pathlib import Path

from .set_password import DEFAULT_ENV, set_key

SECRETS = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "Claude token (output of `claude setup-token`)"),
    "github": ("GITHUB_TOKEN", "GitHub fine-grained token (Contents: read and write, selected repos)"),
}


def write_env(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    clear = "--clear" in argv
    if not args or args[0] not in SECRETS:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    key, label = SECRETS[args[0]]
    path = Path(args[1]) if len(args) > 1 else DEFAULT_ENV if DEFAULT_ENV.exists() else Path(".env")
    if not path.exists():
        print(f"{path} not found. Pass the path to Agent Hub's .env.", file=sys.stderr)
        return 1

    value = "" if clear else getpass.getpass(f"{label}: ").strip()
    if not clear:
        if not value:
            print("Nothing entered; unchanged.", file=sys.stderr)
            return 1
        if not re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,512}", value):
            print("That doesn't look like a token (unexpected characters or length).", file=sys.stderr)
            return 1
    write_env(path, set_key(path.read_text(encoding="utf-8"), key, value))
    print(f"{'Cleared' if clear else 'Saved'} {key} in {path}. Now run: sudo systemctl restart agent-hub")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
