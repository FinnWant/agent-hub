"""Requests for the owner: things a run can't decide alone. Each is stored, shown in the run log
and under "Needs you", and pushed as a notification; the run's clock stops while one is open.

  approval  a tool call matched the sandbox's approval rules (a Kiln PreToolUse hook holds it).
            Allow lets it run; Deny, or no answer within APPROVAL_TIMEOUT, blocks it with the
            reason given to the agent.
  question  the agent ran `kiln-ask "question" ["option" ...]`. kiln-ask waits ASK_WAIT seconds;
            an answer after that reaches the agent as a message (if it is still running).
  budget    the run's estimated spend reached its budget: the gateway holds its API calls.
            Extend raises the budget and lets them through; Stop, or no answer within
            BUDGET_WAIT, stops the run.
  stuck     the loop breaker (loops.py) tripped a second time: the agent's calls are held.
            Continue (advice, if given, is sent to the agent) or Stop; no answer stops it.

Approval rules (one per line, # comments): `Bash: <text>` matches shell commands containing the
text; `Edit: <text>` / `Write: <text>` match file paths (Edit covers MultiEdit and NotebookEdit
too); a line without a tool name means `Bash:`. Matching is plain, case-sensitive substring
matching: a guard rail for the agent's own judgement, not a security boundary (the agent can
still do anything inside its sandbox by other means).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from . import config
from .hooks import HookCall, HookReply

if TYPE_CHECKING:
    from .db import Run
    from .runs import RunManager

log = logging.getLogger(__name__)

KINDS = ("approval", "question", "budget", "stuck")
FILE_TOOLS = {"Edit": ("Edit", "MultiEdit", "NotebookEdit"), "Write": ("Write",)}
MAX_RULES = 50


# ---------------------------------------------------------------------------
# Approval rules
# ---------------------------------------------------------------------------

def parse_rules(text: str | None) -> list[tuple[str, str]]:
    """[(tool, text)] with tool in Bash / Edit / Write."""
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tool, sep, rest = line.partition(":")
        if sep and tool.strip() in ("Bash", "Edit", "Write"):
            tool, needle = tool.strip(), rest.strip()
        else:
            tool, needle = "Bash", line
        if needle:
            rules.append((tool, needle[:300]))
    return rules[:MAX_RULES]


def rules_matcher(rules: list[tuple[str, str]]) -> str:
    tools = sorted({t for tool, _ in rules for t in ((tool,) if tool == "Bash" else FILE_TOOLS[tool])})
    return "|".join(tools)


def match_rule(rules: list[tuple[str, str]], payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """(tool, rule text, what matched) for the first rule the tool call matches."""
    tool = str(payload.get("tool_name") or "")
    inp = payload.get("tool_input") or {}
    for rtool, needle in rules:
        if rtool == "Bash" and tool == "Bash":
            what = str(inp.get("command") or "")
        elif rtool != "Bash" and tool in FILE_TOOLS[rtool]:
            what = str(inp.get("file_path") or inp.get("notebook_path") or "")
        else:
            continue
        if needle in what:
            return tool, needle, what
    return None


# ---------------------------------------------------------------------------
# The requests
# ---------------------------------------------------------------------------

def public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.pop("hook_id", None)
    for k, default in (("options", []), ("answer", None), ("data", {})):
        try:
            out[k] = json.loads(out[k]) if out.get(k) else default
        except ValueError:
            out[k] = default
    return out


class Owner:
    def __init__(self, manager: RunManager) -> None:
        self.m = manager
        self.db = manager.db
        self._waiters: dict[str, asyncio.Future] = {}
        self._expiry: dict[str, asyncio.TimerHandle] = {}
        self.notify = None  # callable(event dict), e.g. Notifier.publish

    # ---- creating and waiting ------------------------------------------------

    def create(self, run: Run, kind: str, title: str, body: str, options: list[str],
               wait: float | None, hook_id: str | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
        rid = secrets.token_hex(6)
        now = time.time()
        row = {"id": rid, "run_id": run.id, "sandbox_id": run.sandbox_id, "kind": kind, "state": "pending",
               "title": title[:300], "body": body[:4000], "options": json.dumps(options[:10]),
               "answer": None, "created_at": now, "answered_at": None,
               "deadline": now + wait if wait else None, "hook_id": hook_id, "data": json.dumps(data or {})}
        self.db.insert_request(row)
        self.m.pause_clock(run, +1)
        self.m.emit(run, {"type": "log", "kind": "request", "request": rid, "request_kind": kind,
                          "title": row["title"], "text": row["body"], "options": options[:10]})
        if wait:
            self._schedule(rid, wait)
        if self.notify:
            sb = self.m.sandboxes.get(run.sandbox_id)
            what = {"approval": "wants to", "question": "asks", "budget": "is over its budget",
                    "stuck": "seems stuck"}[kind]
            try:
                self.notify({"kind": "run.needs_you", "title": f"{sb.name}: the agent {what}",
                             "body": row["title"][:160], "sandbox": run.sandbox_id, "run": run.id,
                             "request": rid, "url": f"/sandboxes/{run.sandbox_id}/runs?run={run.id}"})
            except Exception:  # noqa: BLE001
                log.exception("needs-you notification failed")
        return row

    async def wait(self, rid: str, timeout: float | None = None) -> dict[str, Any] | None:
        """The answer, or None (expired, closed, or `timeout` passed: the request stays open and a
        later answer is delivered another way)."""
        row = self.db.get_request(rid)
        if row and row["state"] != "pending":
            return json.loads(row["answer"]) if row["answer"] else None
        fut = self._waiters.setdefault(rid, asyncio.get_running_loop().create_future())
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except TimeoutError:
            return None
        finally:
            if self._waiters.get(rid) is fut and (fut.done() or timeout is not None):
                self._waiters.pop(rid, None)

    def _schedule(self, rid: str, delay: float) -> None:
        loop = asyncio.get_running_loop()
        self._expiry[rid] = loop.call_later(max(0.0, delay), lambda: asyncio.ensure_future(self.expire(rid)))

    # ---- resolving ------------------------------------------------------------

    async def answer(self, rid: str, choice: str | None, text: str | None, amount: float | None,
                     ip: str) -> dict[str, Any]:
        row = self.db.get_request(rid)
        if row is None:
            raise LookupError("No such request")
        if row["state"] != "pending":
            raise ValueError(f"This request is already {row['state']}")
        options = json.loads(row["options"])
        kind = row["kind"]
        if kind in ("approval", "budget", "stuck") and choice not in [o.lower() for o in options]:
            raise ValueError(f"Choose one of: {', '.join(options)}")
        if kind == "question" and not (text or "").strip() and not choice:
            raise ValueError("Write an answer")
        if kind == "budget" and choice == "extend" and not (amount and 0 < amount <= 10_000):
            raise ValueError("Give an amount to extend the budget by")
        ans = {"choice": choice, "text": (text or "").strip()[:4000] or None, "amount": amount}
        self._resolve(row, "answered", ans)
        self.db.audit(ip, "request.answer", row["sandbox_id"], detail={"request": rid, "kind": kind, "choice": choice})
        await self._deliver(row, ans)
        return self.db.get_request(rid) or row

    async def expire(self, rid: str) -> None:
        row = self.db.get_request(rid)
        if row is None or row["state"] != "pending":
            return
        self._resolve(row, "expired", None)
        await self._deliver(row, None)

    def close_for_run(self, run_id: str) -> None:
        """The run ended: whatever is still open can't be acted on any more."""
        for row in self.db.list_requests(state="pending", run_id=run_id):
            self._resolve(row, "closed", None)

    def restore(self, run_ids: set[str]) -> None:
        """After a restart: keep the open requests of runs Kiln follows again, close the rest."""
        now = time.time()
        for row in self.db.list_requests(state="pending", limit=1000):
            if row["run_id"] not in run_ids:
                self._resolve(row, "closed", None, clock=False)
                continue
            run = self.db.get_run(row["run_id"])
            if run is not None:
                self.m.pause_clock(run, +1)
            if row["deadline"]:
                self._schedule(row["id"], row["deadline"] - now)

    def _resolve(self, row: dict[str, Any], state: str, ans: dict[str, Any] | None, clock: bool = True) -> None:
        self.db.update_request(row["id"], state=state, answer=json.dumps(ans) if ans else None,
                               answered_at=time.time())
        if (h := self._expiry.pop(row["id"], None)) is not None:
            h.cancel()
        run = self.db.get_run(row["run_id"])
        if run is None:
            return
        if clock:
            self.m.pause_clock(run, -1)
        if state != "closed":
            said = {"answered": "Answered", "expired": "No answer in time"}[state]
            detail = ""
            if ans:
                detail = ans.get("text") or ans.get("choice") or ""
                if ans.get("choice") == "extend":
                    detail = f"extend by ${ans['amount']:.2f}"
            self.m.emit(run, {"type": "log", "kind": "answer", "request": row["id"], "state": state,
                              "text": f"{said}: {detail}" if detail else said})

    async def _deliver(self, row: dict[str, Any], ans: dict[str, Any] | None) -> None:
        fut = self._waiters.pop(row["id"], None)
        if fut is not None and not fut.done():
            fut.set_result(ans)  # the hook handler that is waiting answers the agent
            return
        kind, run_id = row["kind"], row["run_id"]
        proc = self.m._procs.get(run_id)
        try:
            if kind == "approval" and proc is not None and row["hook_id"]:
                await proc.reply_hook(row["hook_id"], approval_reply(ans))
            elif kind == "question" and ans and proc is not None and getattr(proc, "can_send", False):
                await proc.send(f"[Owner's answer to your question \"{row['title'][:200]}\"]\n"
                                f"{ans.get('text') or ans.get('choice')}")
            elif kind == "budget":
                await self.m.budget_decided(run_id, ans)
            elif kind == "stuck":
                await self.m.stuck_decided(run_id, ans)
        except Exception:  # noqa: BLE001  (the run may be ending)
            log.warning("couldn't deliver the answer to request %s", row["id"], exc_info=True)


def approval_reply(ans: dict[str, Any] | None) -> HookReply:
    if ans and ans.get("choice") == "allow":
        return HookReply()  # no objection: the tool call goes ahead
    if ans is None:
        why = (f"The owner didn't answer within {config.settings.approval_timeout // 60} minutes, so this "
               "is not allowed. Don't try to do the same thing another way; carry on without it or explain "
               "in your summary what you needed.")
    else:
        why = "The owner denied this."
        if ans.get("text"):
            why += f" Their reason: {ans['text']}"
        why += " Don't try to do the same thing another way."
    return HookReply(code=2, stderr=why)


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

async def approve_risky(manager: RunManager, run: Run, call: HookCall) -> HookReply | None:
    """PreToolUse: a tool call that matches the run's approval rules waits for the owner."""
    hit = match_rule(parse_rules(run.approval_rules), call.payload)
    if hit is None:
        return None
    tool, needle, what = hit
    row = manager.owner.create(run, "approval", f"{tool}: {what[:200]}",
                               f"The agent wants to run this {tool} call, which matches your rule \"{needle}\":\n\n{what}",
                               ["Allow", "Deny"], config.settings.approval_timeout, hook_id=call.id,
                               data={"tool": tool, "rule": needle})
    return approval_reply(await manager.owner.wait(row["id"]))


async def ask_owner(manager: RunManager, run: Run, call: HookCall) -> HookReply:
    """kiln-ask: the agent's question, with optional options."""
    text = str(call.payload.get("_text") or "")
    question, _, rest = text.partition("\n\x1e\n")
    question = question.strip() or "(empty question)"
    options = [o.strip() for o in rest.splitlines() if o.strip()][:6]
    row = manager.owner.create(run, "question", question.splitlines()[0][:200], question, options, None,
                               hook_id=call.id)
    ans = await manager.owner.wait(row["id"], config.settings.ask_wait - 5)
    if ans:
        return HookReply(text=f"The owner answered: {ans.get('text') or ans.get('choice')}")
    return HookReply(text="No answer yet. The owner has been notified, and their answer will reach you later as a "
                          "message. Carry on with a sensible assumption (say which in your summary), or with "
                          "other work in the meantime.")


def owner_hooks() -> list:
    from .runs import HookHandler

    s = config.settings
    return [
        HookHandler("PreToolUse", approve_risky, wait=s.approval_timeout + 30,
                    applies=lambda run: bool(parse_rules(run.approval_rules)),
                    matcher_for=lambda run: rules_matcher(parse_rules(run.approval_rules))),
        HookHandler("KilnAsk", ask_owner, wait=s.ask_wait, installed=False),
    ]
