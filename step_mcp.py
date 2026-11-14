"""mcp-slicc: the step server of a headless step decision (2026-11-10, README "Approvals"). Stdlib only.

A chat typed in SLICC has no Claude Code session to decide its gated steps, so each step is decided in a headless
Claude Code session, and Claude Code's own classifier judges each gated step. The step decision is a
`claude -p --permission-mode auto` session whose only tools are this server's four: bash, read_file, write_file, edit.
A call that reaches this server was permitted: by the change gate (step_hook.py allowed it) or by Claude Code's
permission system (the hook stayed silent on a gated step, so its classifier decided). The server does not run the
step. It records it in the file the endpoint named, and tells the model to end its turn; the endpoint hands the step
to SLICC, whose result comes in the next decision.

One step per decision: a second call is refused. A step the gate never allows is refused here too, so a session in
which the hook did not run still cannot pass one. Its name is not `slicc`: the user-level hook for the operator tools
matches `mcp__slicc__.*` and would answer for these calls.

Usage (from claude_turn.agent_argv): step_mcp.py <context.json>, where the context names the mcp-slicc home, the
gate, the job and the file the step goes to.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import common as C  # noqa: E402

NAME = "slicc-step"
PROTOCOL = "2025-06-18"
TOOLS = [
    {"name": "bash", "description": "Run one shell command in SLICC's own shell (its virtual system, not this computer). "
     "The command is SLICC's next step: it is recorded, SLICC runs it, and its output comes in your next turn.",
     "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read a file in SLICC's workspace (SLICC's next step).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write a file in SLICC's workspace (SLICC's next step).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                     "required": ["path", "content"]}},
    {"name": "edit", "description": "Exact text replacements in a file in SLICC's workspace (SLICC's next step).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "edits": {"type": "array", "items": {
         "type": "object", "properties": {"oldText": {"type": "string"}, "newText": {"type": "string"}},
         "required": ["oldText", "newText"]}}}, "required": ["path", "edits"]}},
]
RECORDED = ("Recorded as SLICC's next step. SLICC runs it after this turn ends, and its result comes in your next "
            "turn. No other tool call runs in this turn, so do not make one: end your turn now and write nothing more.")


def load_context(path: str) -> dict:
    """The context file the endpoint wrote for this decision, with common's settings pointed at it."""
    ctx = json.loads(Path(path).read_text())
    C.HOME = Path(ctx["home"])
    C.GATE_PATH = ctx["gate"]
    return ctx


def call(ctx: dict, name: str, args: dict) -> tuple:
    """(text, is_error) for one tool call."""
    if name not in C.STEP_TOOLS:
        return f"unknown tool {name}", True
    out = Path(ctx["out"])
    if out.exists():
        return ("Not run: one step per turn, and this turn's step is already recorded. Its result comes in your next "
                "turn. End your turn now and write nothing more."), True
    job = C.job_record()
    if not job or job.get("id") != ctx["job"] or job.get("status") != "running":
        return "The job is not running any more. End your turn now.", True
    v = C.classify_step(job, name, args)
    if v.get("decision") == "stop":
        return (f"REFUSED by the change gate, and final: {v.get('detail')}: {v.get('reason')}. Do not try another "
                "way to do this; take a different step or write your report."), True
    C.write_json(out, {"tool": name, "arguments": args, "decision": v.get("decision"),
                       "approved_by": "gate" if v.get("decision") == "auto" else "classifier", "ts": C.now_iso()})
    return RECORDED, False


def handle(ctx: dict, msg: dict):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        return {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", PROTOCOL),
                "capabilities": {"tools": {}}, "serverInfo": {"name": NAME, "version": "0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            text, err = call(ctx, str(p.get("name") or ""), p.get("arguments") or {})
        except Exception as e:   # noqa: BLE001 - a failure is an error result, and records nothing
            text, err = f"the step could not be recorded ({type(e).__name__})", True
        return {"content": [{"type": "text", "text": text}], "isError": err}
    return {"__error__": (-32601, f"method not found: {method}")}


def main() -> None:
    ctx = load_context(sys.argv[1])
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        result = handle(ctx, msg)
        if result is None:
            continue
        if "__error__" in result:
            code, text = result["__error__"]
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": code, "message": text}}
        else:
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
