"""Keeps the real Claude token out of sandboxes.

A small `kiln-gateway` container (see gateway_proxy.py) holds the token. Each sandbox has its
own Docker network, and the gateway is attached to the network of a sandbox while it has a run,
reachable there as http://kiln-gateway:8080. Each run gets a random key that Kiln registers
with the gateway when the run starts and removes when it ends, so a key copied out of a
sandbox (or a process left running after its run) stops working at the end of the run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any

from . import config
from .docker_backend import GATEWAY_LABEL, GATEWAY_NAME, GATEWAY_NETWORK, MANAGED_LABEL, Backend, BackendError
from .templates import TEMPLATES

log = logging.getLogger(__name__)

GATEWAY_PORT = 8080
GATEWAY_URL = f"http://{GATEWAY_NAME}:{GATEWAY_PORT}"
KEYS_DIR = "/keys"
USAGE_DIR = "/tmp/usage"   # gateway_proxy.py: <digest>.jsonl, one line per Messages API call
MAX_USAGE_READ = 256 * 1024
SOURCE = (Path(__file__).parent / "gateway_proxy.py").read_text()


def _is_digest(d: str) -> bool:
    return len(d) == 64 and all(c in "0123456789abcdef" for c in d)


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def gateway_spec(token: str) -> dict[str, Any]:
    image = TEMPLATES["python"].image
    # Changes to the token, the proxy code or the image replace the container.
    version = hashlib.sha256(f"{token}\0{SOURCE}\0{image}".encode()).hexdigest()[:16]
    return {
        "image": image,
        "name": GATEWAY_NAME,
        "command": ["python3", "-I", "-c", SOURCE],
        "user": "agent",
        "environment": {"KILN_UPSTREAM_TOKEN": token, "KILN_KEYS_DIR": KEYS_DIR,
                        "KILN_LISTEN_PORT": str(GATEWAY_PORT)},
        "labels": {MANAGED_LABEL: "true", GATEWAY_LABEL: version},
        "privileged": False,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "read_only": True,
        "tmpfs": {KEYS_DIR: "size=1m,mode=1777", "/tmp": "size=8m,mode=1777"},
        "mem_limit": 128 * 2**20,
        "memswap_limit": 128 * 2**20,
        "nano_cpus": 500_000_000,
        "pids_limit": 64,
        "network": GATEWAY_NETWORK,
        "log_config": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
        "restart_policy": {"Name": "unless-stopped"},
        "detach": True,
    }


class Gateway:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self._lock = threading.Lock()
        self._keys: dict[str, str] = {}  # run id -> key digest

    def issue(self, sandbox_id: str, run_id: str) -> str:
        """Start the gateway if needed, connect it to the sandbox's network, and register a new
        key for this run. Returns the key to give the agent."""
        token = config.settings.claude_code_oauth_token
        if not token:
            raise BackendError("No Claude token is configured")
        key = "sk-ant-oat01-kiln-" + secrets.token_urlsafe(32)
        digest = key_digest(key)
        with self._lock:
            self.backend.ensure_gateway(gateway_spec(token))
            self.backend.attach_gateway(sandbox_id)
            r = self.backend.gateway_exec(["sh", "-c", 'touch "$0/$1"', KEYS_DIR, digest])
            if r.exit_code != 0:
                raise BackendError(f"Couldn't register the run with the Claude gateway: {r.output.strip()[-200:]}")
            self._keys[run_id] = digest
        return key

    def digest(self, run_id: str) -> str | None:
        with self._lock:
            return self._keys.get(run_id)

    def restore(self, sandbox_id: str, run_id: str, digest: str) -> None:
        """After a Kiln restart: the run's agent is still running with its key. Make sure the
        gateway is up and on the sandbox's network, and that the key is registered."""
        if not (len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
            raise BackendError("The run has no gateway key to restore")
        token = config.settings.claude_code_oauth_token
        if not token:
            raise BackendError("No Claude token is configured")
        with self._lock:
            self.backend.ensure_gateway(gateway_spec(token))
            self.backend.attach_gateway(sandbox_id)
            r = self.backend.gateway_exec(["sh", "-c", 'touch "$0/$1"', KEYS_DIR, digest])
            if r.exit_code != 0:
                raise BackendError(f"Couldn't register the run with the Claude gateway: {r.output.strip()[-200:]}")
            self._keys[run_id] = digest

    def read_usage(self, digest: str, offset: int) -> tuple[list[dict[str, Any]], int]:
        """New usage records of a run's key since byte `offset`, and the offset after them."""
        if not _is_digest(digest):
            return [], offset
        r = self.backend.gateway_exec(["sh", "-c", 'tail -c "+$1" "$0" 2>/dev/null | head -c "$2"; true',
                                       f"{USAGE_DIR}/{digest}.jsonl", str(offset + 1), str(MAX_USAGE_READ)])
        data = r.output.encode()
        end = data.rfind(b"\n") + 1  # whole lines only
        records = []
        for line in data[:end].splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
        return records, offset + end

    def hold(self, digest: str, on: bool) -> None:
        """While held, the key's new requests wait in the gateway (see gateway_proxy.py)."""
        if not _is_digest(digest):
            return
        if on:
            self.backend.gateway_exec(["sh", "-c", 'touch "$0/$1"', KEYS_DIR, f"{digest}.hold"])
        else:
            self.backend.gateway_exec(["rm", "-f", f"{KEYS_DIR}/{digest}.hold"])

    def revoke(self, run_id: str) -> None:
        with self._lock:
            digest = self._keys.pop(run_id, None)
            if digest is None:
                return
            try:
                self.backend.gateway_exec(["rm", "-f", f"{KEYS_DIR}/{digest}", f"{KEYS_DIR}/{digest}.hold",
                                           f"{USAGE_DIR}/{digest}.jsonl"])
            except Exception:  # noqa: BLE001  (a stopped gateway has no keys anyway)
                log.warning("couldn't revoke the gateway key of run %s", run_id, exc_info=True)

    def revoke_all(self, keep: set[str] | frozenset[str] = frozenset()) -> None:
        """At startup: remove the keys of every run except those Kiln re-attaches to (`keep`:
        key digests)."""
        keep = {d for d in keep if _is_digest(d)}
        with self._lock:
            self._keys = {r: d for r, d in self._keys.items() if d in keep}
            # Kept runs keep their key, any hold (re-checked by Kiln) and their usage so far.
            for root, names in ((KEYS_DIR, lambda d: (d, f"{d}.hold")), (USAGE_DIR, lambda d: (f"{d}.jsonl",))):
                argv = ["find", root, "-type", "f"]
                for d in sorted(keep):
                    for name in names(d):
                        argv += ["!", "-name", name]
                try:
                    self.backend.gateway_exec([*argv, "-delete"])
                except Exception:  # noqa: BLE001
                    pass
