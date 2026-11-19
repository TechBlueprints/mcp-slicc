"""mcp-slicc endpoint: SLICC's OpenAI-compatible model, answered by headless Claude turns (2026-11-09).

SLICC's `local-llm` provider points here (base URL http://127.0.0.1:8660/v1, apiKey = the token in ~/.claude-slicc/token).
A SLICC call that belongs to the job is answered one of two ways, by the job's mode:

  * auto: one model decision: `claude -p` (claude_turn.py), or Qwen on the Spark for the qwen routes (spark_turn.py,
    2026-11-10). A step is classified by the change gate first (auto: returned to
    SLICC as its next tool call; ask: held until the operator answers `slicc_decide`, which Claude Code asks the person
    about; stop: refused, and the model is told). The final report is SLICC's last answer and report.json.
  * manual: no model. The call is held until the operator hands it a step (slicc_bash and the other step tools), ends
    the job (slicc_report) or stops it (slicc_stop).

Anything else SLICC sends (memory extraction, titles, a second conversation) gets an empty answer and starts no turn.

A held call can lose its caller: SLICC gives up, or its page reloads. The handler watches the connection and cancels
the held call (2026-11-10). Then no step is handed to a caller that is gone, an open question is withdrawn (an ask
must not outlive its call), and a step whose answer could not be written is marked lost, so the operator hears about
it.

That is backend local, the default. With backend forward (2026-11-14) every call goes instead to one upstream agent
endpoint, which picks and gates the steps; this endpoint keeps the hang-up handling, the hard stops and the job
record (_forward_call, forward.py).
"""
from __future__ import annotations

import hmac
import json
import secrets
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))   # also under python3 -I
import common as C
import claude_turn as T
import forward as F
import spark_turn as S

VERSION = C.code_version()   # the code this process started with; start_slicc.sh compares it with the files on disk
MAX_ATTEMPTS = 4          # model decisions per SLICC call before the turn gives up on a step
_locks: dict = {}
_locks_guard = threading.Lock()
_screen_cache: dict = {}


def _lock(jid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(jid, threading.Lock())


def _is_line(text: str, line: str) -> bool:
    """A user message that carries `line`, alone or as one of several queued lines, with or without SLICC's envelope."""
    return any(C.job_prompt(ln) == line for ln in (text or "").splitlines())


def job_messages(job: dict, messages: list, tools: Any) -> Optional[list]:
    """The part of this call's conversation that is the job, or None when the call is not the job's conversation.

    A launcher job: SLICC was started for it with the launch prompt, so the whole conversation is the job, and its
    first user message is that prompt. An attached job: the SLICC in the person's own Chrome joins by the job's start
    line (common.attach_line), and the job is the chat from that message on. An older chat in the same tab does not
    carry the line, so it is never taken for the job. Either way the call offers SLICC's bash tool; memory extraction,
    titles and the like do not."""
    if "bash" not in C.tool_names(tools):
        return None
    if job.get("origin") == "chat":                     # a chat job: from its anchor message on
        for i, m in enumerate(messages or []):
            if m.get("role") == "user" and _msg_key(m) == job.get("anchor"):
                return messages[i:]
        return None
    if C.is_attached(job):
        line = C.attach_line(job["id"])
        for i, m in enumerate(messages or []):
            if m.get("role") == "user" and _is_line(C.text_of(m.get("content")), line):
                return messages[i:]
        return None
    first = next((C.text_of(m.get("content")) for m in messages or [] if m.get("role") == "user"), "")
    return messages if C.job_prompt(first) == C.AUTO_PROMPT else None


NOT_A_JOB = ("This SLICC's model is the local mcp-slicc endpoint, which only answers jobs started from Claude Code "
             "(slicc_task with launch \"attach\"). No job is running for this chat. To join a running job, send its "
             "start line here as a message.")


BUSY = ("mcp-slicc is working on another job ({job}) right now, so this chat gets no job of its own. Send your "
        "message again when that job has ended.")
_chat_guard = threading.Lock()


def _msg_key(m: dict) -> str:
    """A user message, as one short hash of its text as SLICC sends it (with its envelope and time)."""
    import hashlib
    return hashlib.sha256(C.text_of(m.get("content")).encode("utf-8", "replace")).hexdigest()[:24]


def start_chat_job(messages: list, tools: Any) -> Optional[dict]:
    """A chat job (2026-11-10, README "Chat jobs"): a call that offers SLICC's bash tool while no job runs starts an
    auto job for its chat. The instruction is the newest user message. The job is the chat from the message that
    started this chat's first chat job (kept in state/chat_anchors.json), so a second question still has the first
    exchange in front of it; older messages are not sent. None when the call is no chat (no bash tool, the launcher's
    own prompt, no user message), or another job runs."""
    if not C.CHAT_JOBS or "bash" not in C.tool_names(tools):
        return None
    users = [i for i, m in enumerate(messages or []) if m.get("role") == "user"]
    if not users:
        return None
    first = C.text_of(messages[users[0]].get("content"))
    newest = C.text_of(messages[users[-1]].get("content"))
    instruction = C.job_prompt(newest)
    if C.job_prompt(first) == C.AUTO_PROMPT or not instruction:
        return None
    if not C.sent_by_person(newest):       # SLICC's own event (a discovery, a lick, cron): it never starts a job
        return None
    with _chat_guard:
        if C.current_job():
            return None
        path = C.HOME / "state" / "chat_anchors.json"
        known = [str(k) for k in (C.read_json(path) or []) if isinstance(k, str)]
        anchor = next((_msg_key(messages[i]) for i in users if _msg_key(messages[i]) in known),
                      _msg_key(messages[users[-1]]))
        if anchor not in known:
            C.write_json(path, (known + [anchor])[-100:])
        jid = "job-" + secrets.token_hex(6)
        job = {"id": jid, "instruction": instruction, "model": C.DEFAULT_MODEL, "model_explicit": False,
               "mode": "auto", "launch": "attach", "origin": "chat", "anchor": anchor, "budget": C.STEP_BUDGET,
               "status": "running", "started": C.now_iso(), "slicc": "7" if C.is_seven_call(tools) else "6"}
        for sub in ("asks", "decisions", "steps"):
            (C.job_dir(jid) / sub).mkdir(parents=True, exist_ok=True)
        C.write_json(C.HOME / "state" / "job.json", job)
        _log(jid, {"event": "chat_job", "instruction": instruction[:300], "anchor": anchor})
        return job


def _not_a_job(job: Optional[dict], messages: list, tools: Any) -> dict:
    """The answer to a call that is not the running job's conversation. Nothing is started by it."""
    first = next((C.text_of(m.get("content")) for m in messages or [] if m.get("role") == "user"), "")
    C.append_jsonl(C.HOME / "side_calls.jsonl", {"ts": C.now_iso(), "first_user": C.job_prompt(first)[:120]})
    if "bash" not in C.tool_names(tools):
        return _final("")                                  # memory extraction, titles: an empty answer
    newest = next((C.text_of(m.get("content")) for m in reversed(messages or []) if m.get("role") == "user"), "")
    if not C.sent_by_person(newest):
        return _final("")                                  # SLICC's own event: no job, and nothing to say
    if job is None and C.job_prompt(first) == C.AUTO_PROMPT:
        return _final("No SLICC job is running. Stop here.")   # a launcher SLICC whose job has ended
    if job is not None and C.CHAT_JOBS:
        return _final(BUSY.format(job=job["id"]))          # one job at a time
    if job is None or C.is_attached(job):
        return _final(NOT_A_JOB)                           # a chat in the person's own SLICC
    return _final("")                                      # a second conversation beside a launcher job


def _log(jid: str, rec: dict) -> None:
    C.append_jsonl(C.job_dir(jid) / "calls.jsonl", {"ts": C.now_iso(), **rec})


def _final(text: str) -> dict:
    return {"content": text, "tool_calls": []}


def _finish(job: dict, text: str, failed: bool = False) -> dict:
    jid = job["id"]
    if C.is_stopped(jid):   # a decision that was in flight when slicc_stop came: the job stays stopped (2026-11-18)
        _log(jid, {"event": "report_after_stop", "failed": failed, "chars": len(text)})
        return _final("The job was stopped. Stop here.")
    C.write_json(C.job_dir(jid) / "report.json", {"ts": C.now_iso(), "text": text, "failed": failed})
    rec = C.job_record() or {}
    if rec.get("id") == jid:
        rec.update(status="failed" if failed else "done", ended=C.now_iso())
        C.write_json(C.HOME / "state" / "job.json", rec)
    _log(jid, {"event": "report", "failed": failed, "chars": len(text)})
    return _final(text)


def _on_screen(jid: str):
    """Each screen that cost a model call is recorded, so slicc_status can count them."""
    return lambda verdict: _log(jid, {"event": "screen", "verdict": verdict})


def _take_steer(jid: str) -> list:
    """Notes the person added with slicc_tell since the last step (each one is delivered once)."""
    rows = C.read_jsonl(C.job_dir(jid) / "steer.jsonl")
    seen_path = C.job_dir(jid) / "steer_seen"
    try:
        seen = int(seen_path.read_text().strip())
    except (OSError, ValueError):
        seen = 0
    new = [str(r.get("text", "")) for r in rows[seen:] if r.get("text")]
    seen_path.write_text(str(len(rows)))
    return new


def _ask(jid: str, tool: str, args: dict, v: dict, cancel: threading.Event) -> str:
    """Hold the step until the operator answers: allow, deny, 'stopped' (which wins over any answer), 'dropped'
    (SLICC hung up) or 'timeout'. A question that ends without an answer is closed, so nobody is asked about a step
    that can no longer run."""
    aid = "ma-" + secrets.token_hex(6)
    C.write_json(C.job_dir(jid) / "asks" / f"{aid}.json", {
        "id": aid, "job": jid, "ts": C.now_iso(), "tool": tool, "args": args,
        "detail": v.get("detail", ""), "reason": v.get("reason", ""), "source": "claude-turn"})
    path = C.job_dir(jid) / "decisions" / f"{aid}.json"

    def close(why: str) -> None:
        if not path.exists():
            C.write_json(path, {"answer": why, "ts": C.now_iso()})

    end = time.time() + C.ASK_WAIT_S
    while time.time() < end:
        if C.is_stopped(jid):
            return "stopped"
        if cancel.is_set():
            close("withdrawn")
            return "dropped"
        d = C.read_json(path)
        if isinstance(d, dict) and d.get("answer") in ("allow", "deny"):
            return d["answer"]
        time.sleep(0.5)
    close("expired")
    return "timeout"


def _resolve_results(jid: str, msgs: list) -> None:
    """Match SLICC's conversation against the steps it was handed. A result it carries finishes its step: the result
    is screened here, where no tool call is waiting on a deadline, and stored with its verdict before the step turns
    done. A handed-over step that SLICC's conversation does not contain was never taken: it is lost."""
    results = {m.get("tool_call_id"): C.text_of(m.get("content")) for m in msgs if m.get("role") == "tool"}
    taken = {tc.get("id") for m in msgs if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])}
    for s in C.read_steps(jid):
        cid = s.get("tool_call_id")
        if s.get("status") in ("delivered", "lost") and cid in results:
            text = results[cid]
            if s.get("tool") == "bash":
                C.note_tabs(jid, str((s.get("args") or {}).get("command") or ""), text)
            verdict = T.screen_verdict(text, _screen_cache, _on_screen(jid))
            s.update(status="done", result=text, screen=verdict, reported=False, done_ts=C.now_iso())
            C.write_step(jid, s)
            _log(jid, {"event": "step_done", "step": s["id"], "chars": len(text), "screen": verdict})
        elif s.get("status") == "delivered" and cid not in taken:
            _lose(jid, s, "SLICC's next call does not contain it")


def _lose(jid: str, s: dict, why: str) -> None:
    s.update(status="lost", result=C.LOST_TEXT, reported=False, lost_ts=C.now_iso())
    C.write_step(jid, s)
    _log(jid, {"event": "step_lost", "step": s["id"], "why": why})


def undelivered(result: Optional[dict]) -> None:
    """The answer could not reach SLICC (it had hung up). A manual step in it was not taken: mark it lost, so the
    operator hears about it instead of waiting for a result that cannot come. In an auto job nothing is kept: SLICC's
    next call gets a fresh decision."""
    job = C.job_record()
    if not job or result is None:
        return
    ids = {tc.get("id") for tc in result.get("tool_calls") or []}
    _log(job["id"], {"event": "call_dropped", "had_step": bool(ids)})
    for s in C.read_steps(job["id"]):
        if s.get("status") == "delivered" and s.get("tool_call_id") in ids:
            _lose(job["id"], s, "the answer could not be written")


def _settle_asks(jid: str) -> None:
    """A step the operator asked about: allow queues it, deny refuses it (it is never handed to SLICC)."""
    for s in C.read_steps(jid):
        if s.get("status") != "asked":
            continue
        d = C.read_json(C.job_dir(jid) / "decisions" / f"{s.get('ask')}.json")
        if not isinstance(d, dict) or d.get("answer") not in ("allow", "deny"):
            continue
        if d["answer"] == "allow":
            s["status"] = "queued"
        else:
            s.update(status="refused", result=f"This step was denied ({(s.get('verdict') or {}).get('detail')}). "
                                              "Do not try another way; carry on without it, or report.")
        s["decided_ts"] = C.now_iso()
        C.write_step(jid, s)
        _log(jid, {"event": "ask_answer", "step": s["id"], "answer": d["answer"]})


def _handed(jid: str, name: str, args: dict) -> None:
    """A step handed to SLICC seven, by seven's tool name: owner_watch.py answers the owner tier's item for exactly
    this call without judging it again."""
    C.append_jsonl(C.job_dir(jid) / "handed.jsonl", {"ts": time.time(), "tool": name, "args": C.args_key(args)})


def _tool_call(job: dict, tool: str, args: dict, seven: bool) -> dict:
    """One step for SLICC: mcp-slicc's tool name as the SLICC that takes it names it."""
    name = C.native_name(tool, seven)
    if seven:
        _handed(job["id"], name, args)
    return {"id": "call_" + secrets.token_hex(8), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def _manual_turn(job: dict, msgs: list, cancel: threading.Event, seven: bool = False) -> Optional[dict]:
    """A manual job: no model call picks the step. SLICC's call is held until the operator hands it a step (slicc_bash
    and the other step tools write a queued step), or until the operator ends the job (slicc_report) or stops it
    (slicc_stop). The step is SLICC's next tool call; its result answers the step. None when SLICC hung up while the
    call was held: nothing was handed over."""
    jid = job["id"]
    _resolve_results(jid, msgs)
    marker = C.job_dir(jid) / "waiting"
    marker.write_text(C.now_iso() + "\n")
    started = time.time()
    try:
        while True:
            if cancel.is_set():
                _log(jid, {"event": "call_dropped", "had_step": False})
                return None
            if C.is_stopped(jid):
                return _final("The job was stopped. Stop here.")
            rep = C.read_json(C.job_dir(jid) / "report_request.json")
            if isinstance(rep, dict):
                (C.job_dir(jid) / "report_request.json").unlink(missing_ok=True)
                return _finish(job, str(rep.get("text") or "(no report)"))
            _settle_asks(jid)
            queued = [s for s in C.read_steps(jid) if s.get("status") == "queued"]
            if queued:
                s = queued[0]
                tc = _tool_call(job, s["tool"], s.get("args") or {}, seven)
                s.update(status="delivered", tool_call_id=tc["id"], delivered_ts=C.now_iso())
                C.write_step(jid, s)
                _log(jid, {"event": "step_delivered", "step": s["id"], "tool": s["tool"]})
                return {"content": "", "tool_calls": [tc]}
            if time.time() - started > C.MANUAL_IDLE_S:
                return _finish(job, "(endpoint) The operator sent no step for an hour, so the job ends here.", failed=True)
            time.sleep(0.2)
    finally:
        marker.unlink(missing_ok=True)


class _ClaudeDecider:
    """The claude CLI's side of one SLICC call: every decision renders the whole transcript again, and what the
    endpoint says back (a refusal, a denial) goes in the next decision's notes."""

    def __init__(self, job: dict, msgs: list, offered: list, screen, model: str, effort: str, steps_used: int,
                 budget: int):
        self.job, self.msgs, self.offered, self.screen = job, msgs, offered, screen
        self.model, self.effort, self.steps_used, self.budget = model, effort, steps_used, budget
        self.notes: list = []

    def decide(self, steer: list) -> tuple:
        notes, self.notes = steer + self.notes, []
        return T.decide_step(self.msgs, self.job, self.offered, notes, self.steps_used, self.budget, self.screen,
                             model=self.model, effort=self.effort)

    def refuse(self, _step: dict, text: str) -> None:
        self.notes.append(text)


class _AgentDecider(_ClaudeDecider):
    """A Claude model in classifier mode: the step is a real tool call in a headless Claude Code session, where the
    change gate runs as its hook and Claude Code's classifier decides a gated step (claude_turn.decide_step_agent)."""

    def decide(self, steer: list) -> tuple:
        notes, self.notes = steer + self.notes, []
        return T.decide_step_agent(self.msgs, self.job, self.offered, notes, self.steps_used, self.budget,
                                   self.screen, model=self.model, effort=self.effort)


def _agent_path() -> bool:
    """Gated steps go to Claude Code's classifier in a headless session: approvals set to classifier. In person and
    operator mode a gated step still waits for slicc_decide, where the person or the operator session answers."""
    return C.AGENT_STEPS and C.approval() == "classifier"


def _route(job: dict, requested: Any) -> tuple:
    """(model, effort, where it came from, backend) for this call. A job that has run on the Spark keeps the model
    and effort of its first Spark call to the end: a different effort changes the prompt and loses the Spark's KV
    cache (spark_turn.py, rule 3). A different pick in SLICC is logged and ignored until the next job."""
    model, effort, model_from, backend = C.turn_model(job, requested)
    st = S.job_state(job["id"])
    if st is None:
        return model, effort, model_from, backend
    if (backend, model, effort) == ("spark", st["model"], st["effort"]):
        return model, effort, model_from, backend
    _log(job["id"], {"event": "route_change_ignored", "wanted": [backend, model, effort or None],
                     "kept": ["spark", st["model"], st["effort"]]})
    return st["model"], st["effort"], "first-call", "spark"


def _turn(job: dict, msgs: list, offered: list, cancel: threading.Event, requested: Any = None,
          seven: bool = False) -> Optional[dict]:
    """An auto job: its model picks the step, on the route of common.turn_model (`requested` is the model SLICC's
    call names, which is the one picked in SLICC): the claude CLI, or the Spark for the qwen-3.8 names.
    Everything after the decision is the same for both: the gate, the approval setting, questions, the budget, the
    report. None when SLICC hung up while the call was held."""
    jid = job["id"]
    name = str(requested or "").strip()
    if name and name != "claude-slicc" and not job.get("model_explicit") and C.parse_model(name) is None:
        # A name picked in SLICC that this endpoint does not know must not run on some other model without saying so.
        return _finish(job, f"(endpoint) SLICC picked the model \"{name}\", which this endpoint does not know. "
                            f"Known names: {', '.join(C.MODEL_MENU)} (or family-version-effort in that form). "
                            "The job ends here; nothing was run.", failed=True)
    model, effort, model_from, backend = _route(job, requested)
    budget = int(job.get("budget") or C.STEP_BUDGET)
    steps_used = sum(len(m.get("tool_calls") or []) for m in msgs if m.get("role") == "assistant")
    screen = T.make_screen(_screen_cache, _on_screen(jid))
    if C.is_attached(job):                                # the tabs this job opened, from its own open commands
        commands = {tc.get("id"): str((tc.get("function") or {}).get("arguments") or "")
                    for m in msgs if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])}
        for m in msgs:
            if m.get("role") == "tool" and m.get("tool_call_id") in commands:
                C.note_tabs(jid, commands[m["tool_call_id"]], C.text_of(m.get("content")))
    agent = _agent_path()
    if backend == "spark":
        verdict = lambda text: T.screen_verdict(text, _screen_cache, _on_screen(jid))   # noqa: E731 - kept per result
        decider = S.SparkJob(job, msgs, verdict, model, effort, steps_used, budget)
    elif agent:
        decider = _AgentDecider(job, msgs, offered, screen, model, effort, steps_used, budget)
    else:
        decider = _ClaudeDecider(job, msgs, offered, screen, model, effort, steps_used, budget)
    for _ in range(MAX_ATTEMPTS):
        if C.is_stopped(jid):
            return _final("The job was stopped. Stop here.")
        if cancel.is_set():
            _log(jid, {"event": "call_dropped", "had_step": False})
            return None
        try:
            step, meta = decider.decide(_take_steer(jid))
        except T.TurnError as e:
            _log(jid, {"event": "turn_error", "backend": backend, "error": str(e)[:300]})
            return _finish(job, f"(the model turn failed: {e}). The job ends here; report what you have.", failed=True)
        _log(jid, {"event": "call", "model": model, "effort": effort or None, "model_from": model_from,
                   "backend": backend, "action": step.get("action"),
                   "tool": step.get("tool"), "cost_usd": meta.get("total_cost_usd"),
                   "duration_ms": meta.get("duration_ms"),
                   **({"prompt_tokens": meta.get("prompt_tokens"), "cached_tokens": meta.get("cached_tokens"),
                       "completion_tokens": meta.get("completion_tokens"), "prefix_kept": meta.get("prefix_kept")}
                      if backend == "spark" else {}),
                   **({"path": "headless"} if isinstance(decider, _AgentDecider) else {})})
        for d in meta.get("permission_denials") or []:     # steps the headless session's permission system blocked
            _log(jid, {"event": "step_blocked", "tool": d.get("tool"), "input": str(d.get("input"))[:300]})
        if step["action"] == "final":
            return _finish(job, str(step.get("report") or "(no report)"))
        if step["action"] == "invalid":                    # the Spark only: a reply the endpoint answers with a note
            decider.refuse(step, str(step.get("why")))
            continue
        tool = str(step.get("tool"))
        args = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        if tool not in offered:
            decider.refuse(step, f"'{tool}' is not one of your tools here. Use one of: {', '.join(offered)}.")
            continue
        if steps_used >= budget:
            return _finish(job, f"(endpoint) The step budget of {budget} is used up, so the job ends here. "
                                "Report what you found.")
        v = C.classify_step(job, tool, args)
        approved = step.get("approved_by") if step.get("checked") else None
        _log(jid, {"event": "step", "tool": tool, "decision": v.get("decision"),
                   "detail": str(v.get("detail", ""))[:300], "reason": str(v.get("reason", ""))[:200],
                   **({"approved_by": approved} if approved and v.get("decision") != "stop" else {})})
        if v.get("decision") == "stop":
            decider.refuse(step, f"REFUSED, and final: {v.get('detail')}: {v.get('reason')}. Do not try another way "
                                 "to do this; carry on without it, or write your report.")
            continue
        if v.get("decision") == "ask" and approved == "classifier":
            pass                   # Claude Code's permission system permitted this call in the headless session
        elif v.get("decision") == "ask" and agent and backend == "spark":
            try:
                ok, ameta = T.approve_step(job, tool, args, v)
            except T.TurnError as e:
                ok, ameta = False, {"error": str(e)[:300]}
            _log(jid, {"event": "approval", "tool": tool, "by": "classifier", "answer": "allow" if ok else "blocked",
                       "cost_usd": ameta.get("total_cost_usd"), **({"error": ameta["error"]} if "error" in ameta else {})})
            if not ok:
                decider.refuse(step, f"Blocked by Claude Code's permission check: {v.get('detail')}. Do not try "
                                     "another way; carry on without it, or write your report.")
                continue
        elif v.get("decision") == "ask":
            answer = _ask(jid, tool, args, v, cancel)
            _log(jid, {"event": "ask_answer", "tool": tool, "answer": answer})
            if answer == "stopped":
                return _final("The job was stopped. Stop here.")
            if answer == "dropped":
                return None
            if answer != "allow":
                said = {"deny": "This step was denied", "timeout": "Nobody answered in time about this step"}.get(
                    answer, "No answer came for this step")
                decider.refuse(step, f"{said} ({v.get('detail')}). Do not try another way; carry on without it, or "
                                     "report.")
                continue
        return {"content": "", "tool_calls": [_tool_call(job, tool, args, seven)]}
    return _finish(job, "(endpoint) No allowed step came out of several tries. Report what you have.")


def on_call(body: dict, cancel: Optional[threading.Event] = None, raw: Optional[bytes] = None) -> Optional[dict]:
    """The answer to one SLICC call: {content, tool_calls}. None when `cancel` was set while the call was held (the
    caller is gone, and nothing was handed over).

    The brain seam (README "Backends"): the install's one backend answers every call. local is the claude CLI and
    the Spark, below; forward sends the call to one upstream agent (_forward_call). `raw` is the call's body as SLICC
    sent it, which forward passes on unchanged."""
    cancel = cancel or threading.Event()
    brain = C.backend()
    if brain == "forward":
        return _forward_call(body, cancel, raw)
    if brain != "local":
        return _final(f"(endpoint) The backend setting {brain!r} is not one of {', '.join(C.BACKENDS)}, so nothing "
                      "answers here. Fix it in mcp-slicc's config.")
    return _local_call(body, cancel)


def _local_call(body: dict, cancel: threading.Event) -> Optional[dict]:
    """Backend local: the job's own model (auto) or the operator (manual) picks each step, as since 2026-11-09."""
    msgs, tools = body.get("messages") or [], body.get("tools")
    job = C.current_job() or start_chat_job(msgs, tools)
    if job is None or job_messages(job, msgs, tools) is None:
        return _not_a_job(C.current_job(), msgs, tools)
    offered = C.step_tools(tools) or list(C.STEP_TOOLS)
    seven = C.is_seven_call(tools)
    with _lock(job["id"]):
        job = C.current_job()          # re-read: the job may have ended while this call waited for the lock
        mine = job_messages(job, msgs, tools) if job else None
        if mine is None:
            return _not_a_job(job, msgs, tools)
        if cancel.is_set():            # the caller left while this call waited for the lock
            return None
        if seven != C.is_seven(job):   # the SLICC that calls decides whose rules judge its steps (an attached job)
            C.mark_job(job["id"], slicc="7" if seven else "6")
            job = C.current_job() or job
        _log(job["id"], {"event": "call_received", "messages": len(mine)})
        if job.get("mode") == "manual":
            return _manual_turn(job, mine, cancel, seven)
        return _turn(job, mine, offered, cancel, body.get("model"), seven)


# ----------------------------------------------------------------------------------------- backend forward

FORWARD_FAILED = "(endpoint) The upstream agent could not answer ({why}). The job ends here; nothing more was run."
HARD_STOP = ("(endpoint) mcp-slicc's hard stop refused the upstream's step {tool}: {detail}: {reason}. Nothing was run, "
             "and the job ends here.")


def _forward_log(job: Optional[dict], rec: dict) -> None:
    """A forwarded call's record: the running job's calls.jsonl, or side_calls.jsonl when no job runs."""
    if job:
        _log(job["id"], rec)
    else:
        C.append_jsonl(C.HOME / "side_calls.jsonl", {"ts": C.now_iso(), **rec})


def hard_stop(tc: dict, seven: bool = False, job: Optional[dict] = None) -> dict:
    """Backend forward: the change gate's verdict on one step the upstream handed over. Only `stop` counts here: the
    upstream gated the step already, and this endpoint never asks. A step whose arguments are not a JSON object is a
    stop too, since nothing could check it."""
    fn = tc.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        args = None
    if not isinstance(args, dict):
        return {"decision": "stop", "reason": "its arguments are not a JSON object", "detail": str(fn.get("name"))}
    name = str(fn.get("name") or "")
    if seven and job:   # seven's own rules, with the job's owner tier (codemode)
        return C.classify_step({**job, "slicc": "7"}, C.SIX_NAMES.get(name, name), args)
    return C.classify(name, args, seven=seven)


def _forward_call(body: dict, cancel: threading.Event, raw: Optional[bytes]) -> Optional[dict]:
    """Backend forward (2026-11-14): this call goes, as it came, to the upstream agent (forward.py), which picks and
    gates every step, and is held until the upstream answers. What this endpoint still does:

      * the headers: the upstream's token, a call id, the running job's id, and whether this call is the job's own
        conversation (job_messages)
      * hang-ups: SLICC leaving, or the job being stopped, shuts the upstream call down; nothing is handed over
      * the hard stops: a step the change gate never allows is not handed to SLICC, and the job ends failed. A step
        the gate would ask about is the upstream's to decide
      * the job record: in the job's own conversation the upstream's answer without a step is the job's report
        (failed when the upstream sends `X-Slicc-Report: failed`), and an upstream that cannot answer ends the job
        with a failure report
    """
    job = C.current_job()
    msgs, tools = body.get("messages") or [], body.get("tools")
    mine = job_messages(job, msgs, tools) if job else None
    cid = "fwd-" + secrets.token_hex(6)
    token = C.forward_token()
    headers = {"Content-Type": "application/json", "X-Slicc-Call": cid}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if job:
        headers["X-Slicc-Job"] = job["id"]
        headers["X-Slicc-Conversation"] = "job" if mine is not None else "other"
    rec = {"event": "call" if mine is not None else "side_call", "backend": "forward", "call": cid}
    if mine is not None:
        _log(job["id"], {"event": "call_received", "messages": len(mine), "call": cid})
    started = time.time()
    r, why, status, rh = None, "", 0, {}
    try:
        if token is None:
            raise F.UpstreamError("the forward token file cannot be read")
        status, rh, out = F.request("POST", "/chat/completions",
                                    raw if raw is not None else json.dumps(body, ensure_ascii=False).encode(),
                                    headers, cancel, stopped=(lambda: C.is_stopped(job["id"])) if job else bool)
        r = F.message_of(status, out)
    except F.Cancelled as e:
        ms = int((time.time() - started) * 1000)
        if e.why == "dropped":
            _forward_log(job, {"event": "call_dropped", "had_step": False, "backend": "forward", "call": cid,
                               "upstream": "cancelled", "duration_ms": ms})
            return None
        if e.why == "stopped":
            _forward_log(job, {**rec, "action": "stopped", "upstream": "cancelled", "duration_ms": ms})
            return _final("The job was stopped. Stop here.")
        why = f"it did not answer within {C.forward_timeout():g} s"
    except F.UpstreamError as e:
        why = str(e)
    ms = int((time.time() - started) * 1000)
    if r is None:
        _forward_log(job, {**rec, "action": "error", "error": why[:300], "duration_ms": ms})
        if mine is not None:
            return _finish(job, FORWARD_FAILED.format(why=why), failed=True)
        return _final(f"(endpoint) The upstream agent could not answer ({why})." if "bash" in C.tool_names(tools)
                      else "")
    calls = r["tool_calls"]
    _forward_log(job, {**rec, "action": "step" if calls else "final", "status": status, "duration_ms": ms,
                       "tools": [(tc.get("function") or {}).get("name") for tc in calls]})
    if job and C.is_stopped(job["id"]):
        return _final("The job was stopped. Stop here.")
    refused = None
    for tc in calls:
        v = hard_stop(tc, C.is_seven_call(tools), job)
        name = (tc.get("function") or {}).get("name")
        if mine is not None:
            _log(job["id"], {"event": "step", "tool": name, "call": cid,
                             "decision": "stop" if v.get("decision") == "stop" else "upstream",
                             "gate": v.get("decision"), "detail": str(v.get("detail", ""))[:300],
                             "reason": str(v.get("reason", ""))[:200],
                             **({} if v.get("decision") == "stop" else {"approved_by": "upstream"})})
        if v.get("decision") == "stop" and refused is None:
            refused = HARD_STOP.format(tool=name, detail=v.get("detail"), reason=v.get("reason"))
    if refused:
        if mine is not None:
            return _finish(job, refused, failed=True)
        _forward_log(job, {"event": "hard_stop", "call": cid, "text": refused[:300]})
        return _final(refused)
    if mine is not None and not calls:
        return _finish(job, r["content"], failed=str(rh.get("x-slicc-report", "")).lower() == "failed")
    return r


class Handler(BaseHTTPRequestHandler):
    server_version = "mcp-slicc"

    def log_message(self, fmt, *args):   # keep the console quiet; calls.jsonl has the record
        return

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        self.send_header("Access-Control-Allow-Origin", origin if origin in C.ORIGINS else C.ORIGINS[0])
        self.send_header("Vary", "Origin")
        if self.headers.get("Access-Control-Request-Private-Network"):   # an https page calling this local port
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         self.headers.get("Access-Control-Request-Headers", "authorization, content-type"))
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, code: int, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _route(self) -> tuple:
        """(path without the token prefix, whether the path carried the endpoint's token). A SLICC seven account is
        given http://127.0.0.1:<port>/t/<token>/v1 (common.seven_endpoint), since seven sends a keyless account's
        calls with `Authorization: Bearer local`."""
        path = self.path.split("?", 1)[0]
        parts = path.split("/", 3)
        if len(parts) >= 3 and parts[1] == "t":
            ok = hmac.compare_digest(parts[2].encode(), C.token().encode())
            return "/" + (parts[3] if len(parts) > 3 else ""), ok
        return path, False

    def _authorized(self) -> bool:
        if self._route()[1]:
            return True
        want = f"Bearer {C.token()}"
        return hmac.compare_digest(self.headers.get("Authorization", ""), want)

    def _client_gone(self) -> bool:
        """True when the caller has closed its side: the socket reads as ended. Data waiting (not an end) is a caller
        that is still there."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self._route()[0].rstrip("/") != "/v1/models":
            return self._json(404, {"error": {"message": "not found"}})
        if not self._authorized():
            return self._json(401, {"error": {"message": "unauthorized"}})
        if C.backend() == "forward":                # the upstream's models, each with the code this endpoint runs
            items = F.models() or [{"id": "claude-slicc", "object": "model", "owned_by": "forward"}]
            return self._json(200, {"object": "list", "data": [{"context_length": C.context_length("claude-slicc"), **m, "version": VERSION}
                                                                for m in items]})
        ids = ("claude-slicc",) + C.MODEL_MENU     # claude-slicc: the job's own model; the rest name a model
        self._json(200, {"object": "list", "data": [
            {"id": i, "object": "model", "owned_by": getattr(C.parse_model(i), "backend", "claude"),
             "context_length": C.context_length(i), "version": VERSION} for i in ids]})

    def do_POST(self):
        if self._route()[0].rstrip("/") != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        if not self._authorized():
            return self._json(401, {"error": {"message": "unauthorized"}})
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except ValueError:
            return self._json(400, {"error": {"message": "bad json"}})
        stream = bool(body.get("stream"))
        box: dict = {}
        done = threading.Event()
        cancel = threading.Event()

        def work():
            try:
                r = on_call(body, cancel, raw)
                if cancel.is_set() and r is not None:   # answered in the instant the caller left
                    undelivered(r)
                    r = None
                box["r"] = r
            except Exception as e:   # noqa: BLE001 - the answer must still reach SLICC
                _log_safe(f"endpoint error: {type(e).__name__}: {e}")
                box["r"] = _final(f"(endpoint error: {type(e).__name__})")
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()
        cid = "chatcmpl-" + secrets.token_hex(8)
        try:
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self._cors()
                self.end_headers()
            last_keepalive = time.time()
            while not done.wait(0.5):
                if self._client_gone():
                    raise ConnectionAbortedError("the caller closed the connection")
                if stream and time.time() - last_keepalive >= 10:   # while a decision or a person's answer is pending
                    self.wfile.write(b": mcp-slicc is working\n\n")
                    self.wfile.flush()
                    last_keepalive = time.time()
        except OSError:
            cancel.set()      # the held call unwinds by itself and hands nothing over
            return
        r = box.get("r")
        if r is None:
            return
        try:
            if stream:
                self._stream(cid, r)
            else:
                self._completion(cid, r)
        except OSError:
            undelivered(r)

    def _completion(self, cid: str, r: dict) -> None:
        msg: dict = {"role": "assistant", "content": r["content"]}
        if r["tool_calls"]:
            msg["tool_calls"] = r["tool_calls"]
        self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": "claude-slicc",
                         "choices": [{"index": 0, "finish_reason": "tool_calls" if r["tool_calls"] else "stop",
                                      "message": msg}], "usage": {}})

    def _stream(self, cid: str, r: dict) -> None:
        def send(obj):
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
        base = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": "claude-slicc"}
        send({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": r["content"] or ""},
                                   "finish_reason": None}]})
        for i, tc in enumerate(r["tool_calls"]):
            send({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, "id": tc["id"],
                                       "type": "function", "function": tc["function"]}]}, "finish_reason": None}]})
        send({**base, "choices": [{"index": 0, "delta": {},
                                   "finish_reason": "tool_calls" if r["tool_calls"] else "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def _log_safe(text: str) -> None:
    C.append_jsonl(C.HOME / "endpoint.log", {"ts": C.now_iso(), "error": text[:500]})


def make_server(host: str = None, port: int = None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host or C.HOST, C.PORT if port is None else port), Handler)
    srv.daemon_threads = True
    return srv


def main() -> None:
    C.HOME.mkdir(parents=True, exist_ok=True)
    C.token()
    brain = C.backend()
    if brain not in C.BACKENDS:
        sys.exit(f"mcp-slicc endpoint: backend {brain!r} is not one of {', '.join(C.BACKENDS)} (mcp-slicc config)")
    srv = make_server()
    how = (f"forward to {C.forward_url() or '(no forward_url)'}" if brain == "forward" else
           f"local, billing: {C.BILLING}, budget: {C.STEP_BUDGET} steps")
    print(f"mcp-slicc endpoint {VERSION} on http://{C.HOST}:{srv.server_address[1]}/v1 (token: {C.HOME / 'token'}, "
          f"{how})", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
