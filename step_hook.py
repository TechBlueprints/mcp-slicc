"""mcp-slicc: the PreToolUse hook of a headless step decision (2026-11-10, README "Approvals"). Stdlib only.

Passed to `claude -p --permission-mode auto` with --settings, for the slicc-step server's tools only. It runs the change
gate on the step (common.classify_step, the attached-Chrome tab rule included):

  auto: allow. The gate passes the step by itself
  stop: deny, with the gate's reason. The model sees it, and the step never reaches SLICC
  ask:  nothing printed, so Claude Code's own permission system decides: in auto mode its classifier, with the step
        in front of it. This path is only used when approvals are set to classifier

A hook that cannot load or read the call denies it: a headless session has no person to ask, and a broken hook must
never wave a step through. Usage: step_hook.py <context.json> (the same context as step_mcp.py).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PREFIX = "mcp__slicc-step__"


def emit(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}, ensure_ascii=False))


def decide(ctx_path: str, event: dict) -> None:
    name = str(event.get("tool_name") or "")
    if not name.startswith(PREFIX):
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import step_mcp as S   # here, so a failed import is caught in main() and denies
    C = S.C
    ctx = S.load_context(ctx_path)
    tool, args = name[len(PREFIX):], event.get("tool_input") or {}
    job = C.job_record()
    if not job or job.get("id") != ctx["job"] or job.get("status") != "running":
        emit("deny", "The job is not running any more.")
        return
    v = C.classify_step(job, tool, args if isinstance(args, dict) else {})
    C.append_jsonl(C.job_dir(job["id"]) / "calls.jsonl", {
        "ts": C.now_iso(), "event": "step_hook", "tool": tool, "decision": v.get("decision"),
        "detail": str(v.get("detail", ""))[:300]})
    if v.get("decision") == "auto":
        emit("allow", f"The change gate passes this step: {v.get('reason')}")
    elif v.get("decision") == "stop":
        emit("deny", f"REFUSED by the change gate, and final: {v.get('detail')}: {v.get('reason')}. Do not try another "
                     "way to do this; take a different step or write your report.")
    elif ctx.get("approval") != "classifier":
        emit("deny", "This step needs a decision, and approvals are not set to Claude Code's classifier.")
    # ask, in classifier mode: silent, and Claude Code's permission system decides


def main() -> None:
    try:
        decide(sys.argv[1], json.load(sys.stdin))
    except Exception as e:   # noqa: BLE001 - fail closed
        emit("deny", f"The mcp-slicc step hook could not check this step ({type(e).__name__}).")


if __name__ == "__main__":
    main()
