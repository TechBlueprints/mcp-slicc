"""mcp-slicc PreToolUse hook (2026-11-09): what Claude Code does before each slicc_* operator call.

Claude Code runs this before each slicc tool call, with the call as JSON on stdin. The hook answers allow, deny or
ask (hookSpecificOutput.permissionDecision), or prints nothing, which leaves the call to Claude Code's own permission
system: its classifier in a session in auto mode, a prompt to the person in default mode.

A call is "gated" when it starts a job, adds a note, allows a question, or is a manual step the change gate does not
pass by itself. What happens to a gated call is common.approval():

  classifier (the default since 2026-11-10): nothing is
      printed, so Claude Code's permission system decides, with the step in front of it.
  operator: allow. The Claude session driving SLICC decides by itself.
  person: ask. Claude Code shows the person the instruction, the exact step or the note.

In all three: reads, stops and reports are allowed; a manual step the gate passes by itself is allowed; a step the
gate never allows is allowed through to the server, which refuses and records it; a slicc_decide for a question that
is not open is denied; and a hook that cannot load its code or read the call asks a person, so a broken hook never
waves a call through. Only calls to the `slicc` MCP server are touched; for any other tool the hook prints nothing.
"""
from __future__ import annotations

import json
import sys

PREFIX = "mcp__slicc__"
OPERATOR = "Approvals are set to the operator (mcp-slicc config): no check."
STEP_TOOLS = {"slicc_bash": "bash", "slicc_read_file": "read_file", "slicc_write_file": "write_file",
              "slicc_edit": "edit"}


def emit(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


def _short(text: str, n: int = 600) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[:n] + " ..."


def gated(mode: str, ask_text: str) -> None:
    """A gated call: the person is asked (person), nothing checks (operator), or the hook stays silent and Claude
    Code's own permission system decides (classifier)."""
    if mode == "person":
        emit("ask", ask_text)
    elif mode == "operator":
        emit("allow", OPERATOR)


def decide(event: dict) -> None:
    name = str(event.get("tool_name") or "")
    args = event.get("tool_input") or {}
    if not name.startswith(PREFIX):
        return
    import common as C   # here, not at the top: a failed import is caught in main() and asks a person
    tool = name[len(PREFIX):]
    mode = C.approval()
    if tool == "slicc_task":
        gated(mode, f"Start a SLICC job (a browser worker, model {args.get('model') or C.DEFAULT_MODEL}, "
                    f"{args.get('mode') or 'auto'}). Instruction: {_short(args.get('instruction'))}")
    elif tool == "slicc_tell":
        gated(mode, f"Add this note to the running SLICC job, for its next step: {_short(args.get('note'))}")
    elif tool == "slicc_decide":
        if args.get("answer") == "deny":
            emit("allow", "A no to a SLICC question needs no approval.")
            return
        ask_id = str(args.get("ask") or "")
        cur = C.current_job()
        path = C.job_dir(cur["id"]) / "asks" / f"{ask_id}.json" if cur and C.ASK_ID.fullmatch(ask_id) else None
        ask = C.read_json(path) if path else None
        if not isinstance(ask, dict):
            emit("deny", f"{ask_id or 'this id'} is not a question of the running job.")
            return
        if (C.job_dir(cur["id"]) / "decisions" / f"{ask_id}.json").exists():
            emit("deny", f"{ask_id} is closed: it was answered already, or SLICC's call ended before an answer came.")
            return
        if mode == "classifier":
            if ask.get("source") == "operator-step":
                # A manual step the operator sent: the step itself was the call Claude Code's permission system judged.
                emit("allow", "This step was checked when it was sent.")
                return
            if str(args.get("wants") or "") != str(ask.get("detail") or ""):
                emit("deny", "Repeat the question's `wants` text in the call, so Claude Code's classifier sees the "
                             f"step it is asked to allow: {_short(ask.get('detail'))}")
                return
        gated(mode, f"Let SLICC take this step? {ask.get('tool')}: {_short(ask.get('detail'))}. "
                    f"Why it needs you: {_short(ask.get('reason'), 300)}. The step runs only if you allow it.")
    elif tool in STEP_TOOLS:
        job = C.current_job()
        step_args = {k: v for k, v in args.items() if k != "purpose"}
        verdict = (C.classify_step(job, STEP_TOOLS[tool], step_args) if job and job.get("mode") == "manual"
                   else {"decision": "none"})
        if verdict.get("decision") == "ask" and mode == "classifier":
            return   # silent: Claude Code's permission system decides this step, with the command in the call
        emit("allow", "The server checks this step with the change gate: it runs what the gate passes, refuses what "
                      "it never allows, and holds or runs the rest by the approval setting.")
    else:
        emit("allow", "A read of SLICC's state, a stop or a report.")


def main() -> None:
    try:
        event = json.load(sys.stdin)
        decide(event)
    except Exception as e:   # noqa: BLE001 - a hook that cannot load or read the call must not wave it through
        emit("ask", f"The SLICC hook could not check this call ({type(e).__name__}); a person decides.")


if __name__ == "__main__":
    main()
