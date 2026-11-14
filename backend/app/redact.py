"""Remove secret values from text before it is stored or sent to the browser.

Known secrets (the Claude and GitHub tokens) are replaced wherever they appear, including the
common encodings a tool might print them in (URL-encoded, base64 as in an HTTP Basic header).
A pattern pass also catches token shapes we can recognise even if we don't hold the value
(another GitHub or Anthropic token the agent found somewhere)."""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote

from . import config

MASK = "[redacted]"
MIN_SECRET_LEN = 8

PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),            # Anthropic API keys and OAuth tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),         # GitHub classic tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),       # GitHub fine-grained tokens
    re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)\s+)[A-Za-z0-9+/=._\-]{8,}"),
    re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"),          # credentials embedded in URLs
]


def _variants(secret: str) -> set[str]:
    out = {secret, quote(secret, safe="")}
    for user in ("x-access-token", "git", "oauth2", ""):
        out.add(base64.b64encode(f"{user}:{secret}".encode()).decode())
    return {v for v in out if len(v) >= MIN_SECRET_LEN}


class Redactor:
    def __init__(self, secrets: list[str]) -> None:
        values: set[str] = set()
        for s in secrets:
            if s and len(s) >= MIN_SECRET_LEN:
                values |= _variants(s)
        # Longest first so a value containing another is masked whole.
        self.values = sorted(values, key=len, reverse=True)

    @classmethod
    def from_settings(cls) -> Redactor:
        s = config.settings
        return cls([s.claude_code_oauth_token, s.github_token, s.session_secret])

    def text(self, value: str) -> str:
        for secret in self.values:
            if secret in value:
                value = value.replace(secret, MASK)
        for pattern in PATTERNS:
            value = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + MASK, value)
        return value

    def obj(self, value: Any) -> Any:
        """Redact every string inside a JSON-like structure."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        return value
