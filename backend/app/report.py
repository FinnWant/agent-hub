"""The handoff report: the agent's own summary (asked for under fixed headings) next to what Kiln
measured itself, so the one can be checked against the other. Stored on the run as JSON."""

from __future__ import annotations

import re
from typing import Any

HEADINGS = ("Done", "Not done", "Assumptions", "How to verify", "Risks")
# A heading starts a line, optionally after markdown (#, a list marker, bold); then either a colon
# (text may follow on the same line: "Done: added the page"), or nothing else on the line.
# "Done with the refactor." in running text is not a heading.
HEAD = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*|[-*][ \t]+)?(?:\*\*)?(" + "|".join(HEADINGS) +
                  r")(?:\*\*)?[ \t]*(?::(?:\*\*)?[ \t]*(.*)|[ \t]*$)", re.I | re.M)


def sections(summary: str) -> dict[str, str]:
    """The summary's sections under the agreed headings ({} if it doesn't use them)."""
    found = list(HEAD.finditer(summary or ""))
    out: dict[str, str] = {}
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(summary)
        name = next(h for h in HEADINGS if h.lower() == m.group(1).lower())
        body = ((m.group(2) or "") + "\n" + summary[m.end():end]).strip()
        if body:
            out[name] = (out.get(name, "") + "\n" + body).strip()[:4000]
    return out


def build(run, summary: str | None, stats: dict | None, checkpoints: int, requests: list[dict],
          events: list[dict]) -> dict[str, Any]:
    from .usage import RunUsage

    u = RunUsage.load(run.usage) if run.usage else None
    duration = (run.finished_at or 0) - (run.started_at or 0)
    return {
        "summary": (summary or "")[:8000],
        "sections": sections(summary or ""),
        "facts": {
            "state": run.state,
            "error": run.error,
            "minutes": round(max(0.0, duration - (run.paused_seconds or 0)) / 60, 1),
            "waited_minutes": round((run.paused_seconds or 0) / 60, 1),
            "checks": run.verify,
            "check_rounds": run.verify_attempts,
            "files": (stats or {}).get("files"),
            "insertions": (stats or {}).get("insertions"),
            "deletions": (stats or {}).get("deletions"),
            "file_names": (stats or {}).get("names", [])[:50],
            "commit": run.commit_sha,
            "pushed": run.pushed,
            "cost_est": round(u.cost, 4) if u else run.cost_usd,
            "api_calls": u.requests if u else None,
            "checkpoints": checkpoints,
            "questions": sum(1 for r in requests if r["kind"] == "question"),
            "approvals": sum(1 for r in requests if r["kind"] == "approval"),
            "compactions": sum(1 for e in events if e.get("kind") == "system" and "compacted" in (e.get("text") or "")),
            "loops": sum(1 for e in events if e.get("kind") == "loop"),
        },
    }
