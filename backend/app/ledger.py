"""The ledger: /workspace/.kiln/LEDGER.md, notes the agent keeps across runs and context
compaction (plan, decisions and assumptions, done, not done). It is never committed (Kiln adds
`/.kiln/` to .git/info/exclude) and survives in the sandbox's volume across runs and branches.

Kiln owns one block of it, between the kiln:begin / kiln:end markers: the latest request and one
line per run (how it ended, checks, cost, commit), rewritten when a run starts and when it ends,
so it also tells the agent what happened after an earlier run stopped (a time limit, failing
checks). The owner can read and edit the file in the UI; after a context compaction, a Kiln
SessionStart hook gives it back to the agent.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

from .docker_backend import BackendError
from .hooks import HookCall, HookReply

if TYPE_CHECKING:
    from .db import Run, Sandbox
    from .runs import RunManager

DIR = "/workspace/.kiln"
PATH = f"{DIR}/LEDGER.md"
MAX = 64 * 1024
BEGIN, END = "<!-- kiln:begin (written by Kiln; changes here are replaced) -->", "<!-- kiln:end -->"
BLOCK = re.compile(re.escape(BEGIN.split(" (")[0]) + r".*?" + re.escape(END), re.S)
TEMPLATE = """# Ledger

Notes kept across runs and context compaction. Keep them short and current.

## Plan

## Decisions and assumptions

## Done

## Not done yet

"""


def read(manager: RunManager, sb: Sandbox) -> str | None:
    try:
        return manager.sandboxes.backend.get_file(sb.container_id or "", PATH, MAX).decode("utf-8", "replace")
    except BackendError:
        return None


def write(manager: RunManager, sb: Sandbox, text: str) -> None:
    backend = manager.sandboxes.backend
    backend.exec(sb.container_id or "", ["mkdir", "-p", DIR], 30)
    backend.put_file(sb.container_id or "", DIR, "LEDGER.md", text.encode()[:MAX])


def kiln_block(manager: RunManager, sb: Sandbox, current: Run) -> str:
    from .runs import outcome

    lines = [BEGIN, "## Latest request", "", current.prompt.strip()[:2000], "", "## Runs (newest first)", ""]
    for r in manager.db.list_runs(sandbox_id=sb.id, limit=15):
        if r.id == current.id:
            r = current  # its final state may not be stored yet (runs.py stores it last)
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r.created_at))
        what = "running now" if r.active else outcome(r)
        extra = []
        if r.cost_est:
            extra.append(f"≈${r.cost_est:.2f}")
        if r.commit_sha:
            extra.append(f"commit {r.commit_sha[:7]}")
        if r.parent_run_id:
            extra.append(f"continues {r.parent_run_id}")
        first = (r.prompt.strip().splitlines() or [""])[0][:100]
        lines.append(f"- {when} UTC, run {r.id} ({first!r}): {what}" + (f"; {', '.join(extra)}" if extra else ""))
    return "\n".join(lines + ["", END])


def refresh(manager: RunManager, sb: Sandbox, run: Run) -> None:
    """Rewrite Kiln's block (creating the ledger from the template if there is none)."""
    text = read(manager, sb) or TEMPLATE
    block = kiln_block(manager, sb, run)
    text = BLOCK.sub(lambda _: block, text) if BLOCK.search(text) else text.rstrip() + "\n\n" + block + "\n"
    write(manager, sb, text)


async def refresh_quietly(manager: RunManager, sb: Sandbox, run: Run) -> None:
    try:
        await asyncio.to_thread(refresh, manager, sb, run)
    except Exception:  # noqa: BLE001  (the ledger is a help, never a reason to fail a run)
        pass


async def after_compaction(manager: RunManager, run: Run, call: HookCall) -> HookReply | None:
    """SessionStart (source "compact"): give the ledger back to the agent."""
    if call.payload.get("source") != "compact":
        return None
    sb = manager.sandboxes.get(run.sandbox_id)
    text = await asyncio.to_thread(read, manager, sb)
    if not text:
        return None
    context = ("Your context was just compacted. Your ledger (/workspace/.kiln/LEDGER.md), with the plan and "
               "decisions so far, is below; keep it up to date.\n\n" + text)[:9500]
    return HookReply(stdout={"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}})


def ledger_hook():
    from .runs import HookHandler

    return HookHandler("SessionStart", after_compaction, wait=20, matcher="compact")
