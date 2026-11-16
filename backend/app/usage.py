"""What a run's agent spends, measured at the Claude gateway (gateway_proxy.py), which sees every
Messages API call made with the run's key, including calls the agent makes by other means than
Claude Code. A model's output can't fake these numbers, unlike Claude Code's own report.

Cost is an *estimate at API list prices*. With a Claude subscription (CLAUDE_CODE_OAUTH_TOKEN)
nothing is billed per token; the estimate still shows what a run is worth and drives budgets.
Prices: Claude API list prices as cached in the claude-api skill on 2026-11-03 (USD per million
tokens). Cache writes cost 1.25x input (5-minute) or 2x (1-hour); cache reads differ by model.
Unknown models count tokens but no cost.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

# model id prefix: (input, output, cache read) in USD per million tokens
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-mythos-5-2": (8.00, 40.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-mythos-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
# Claude Haiku 5.5: prompts over 100K tokens cost $0.50 / $2.50 per million
LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}


def price(model: str) -> tuple[str, tuple[float, float, float]] | None:
    m = model.removeprefix("anthropic.").split("[")[0].split("@")[0]
    best = max((p for p in PRICES if m == p or m.startswith(p + "-")), key=len, default=None)
    return (best, PRICES[best]) if best else None


def call_cost(rec: dict[str, Any]) -> float | None:
    """Estimated USD of one metered call (see gateway_proxy.record_usage)."""
    found = price(str(rec.get("m") or ""))
    if found is None:
        return None
    name, (inp, out, read) = found
    uncached, cr, cw, cw1h, outt = (int(rec.get(k) or 0) for k in ("in", "cr", "cw", "cw1h", "out"))
    if name in LONG_PROMPT and uncached + cr + cw > LONG_PROMPT[name][0]:
        inp, out, read = LONG_PROMPT[name][1]
    cw1h = min(cw1h, cw)
    return (uncached * inp + cr * read + (cw - cw1h) * inp * 1.25 + cw1h * inp * 2 + outt * out) / 1e6


@dataclass
class RunUsage:
    """A run's running totals (stored as JSON on the run)."""

    requests: int = 0
    input: int = 0          # uncached input tokens
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    cost: float = 0.0       # estimate, known models only
    unpriced: int = 0       # calls to models without a price
    context: int = 0        # prompt size of the last call to the main model
    model: str = ""         # the main model: most output tokens
    by_model: dict[str, int] = field(default_factory=dict)  # output tokens per model
    offset: int = 0         # bytes of the gateway's usage file read so far

    @classmethod
    def load(cls, text: str | None) -> RunUsage:
        try:
            data = json.loads(text or "{}")
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (ValueError, TypeError):
            return cls()

    def dump(self) -> str:
        return json.dumps(asdict(self))

    def add(self, rec: dict[str, Any]) -> None:
        model = str(rec.get("m") or "?")[:80]
        uncached, cr, cw, out = (int(rec.get(k) or 0) for k in ("in", "cr", "cw", "out"))
        self.requests += 1
        self.input += uncached
        self.cache_read += cr
        self.cache_write += cw
        self.output += out
        cost = call_cost(rec)
        if cost is None:
            self.unpriced += 1
        else:
            self.cost += cost
        self.by_model[model] = self.by_model.get(model, 0) + out
        self.model = max(self.by_model, key=self.by_model.get)
        if model == self.model:
            self.context = uncached + cr + cw

    def public(self) -> dict[str, Any]:
        """For the API (no offset)."""
        d = asdict(self)
        d.pop("offset")
        d["cost"] = round(self.cost, 4)
        return d
