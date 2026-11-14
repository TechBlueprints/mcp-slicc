#!/usr/bin/env python3
"""Stands in for `claude -p` in the tests (2026-11-09). Records each call's argv and env in $FAKE_CLAUDE_LOG, answers
haiku calls from the text (INJECT in the prompt = flagged), and answers the other calls from the queue in
$FAKE_CLAUDE_QUEUE: a JSON list of structured outputs, or {"__error__": "..."} for a failed turn.
$FAKE_CLAUDE_MODE=text puts the object in the result text instead of structured_output.

With --permission-mode (the headless step session, 2026-11-10) it plays the session: each queued step is a tool call
that runs the real hook from --settings and, when permitted, the real step server from --mcp-config. A gated step the
hook leaves silent goes to a stand-in classifier, which blocks it when $FAKE_CLASSIFIER_BLOCK occurs in its input. A
blocked call is a permission denial, and the next queued item is the model's next try. A prompt that asks for one exact
call (claude_turn.approve_step) makes that call and uses no queue."""
import json
import os
import subprocess
import sys

argv = sys.argv[1:]
model = argv[argv.index("--model") + 1] if "--model" in argv else ""
prompt = argv[1] if len(argv) > 1 else ""

if "--permission-mode" not in argv:
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
        f.write(json.dumps({"model": model, "prompt": prompt, "argv": argv,
                            "api_key_in_env": "ANTHROPIC_API_KEY" in os.environ}) + "\n")


def agent_session() -> None:
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    server_name, server = next(iter(mcp.items()))
    hook = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    denials, trail = [], []

    def tool_call(tool: str, args: dict) -> str:
        """'recorded', 'denied' or 'error'."""
        name = f"mcp__{server_name}__{tool}"
        h = subprocess.run(hook, shell=True, capture_output=True, text=True, input=json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": args}))
        out = h.stdout.strip()
        decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None
        if decision is None:
            block = os.environ.get("FAKE_CLASSIFIER_BLOCK")
            decision = "classifier-deny" if block and block in json.dumps(args) else "classifier-allow"
        trail.append({"tool": tool, "args": args, "decision": decision})
        if decision in ("deny", "classifier-deny"):
            denials.append({"tool_name": name, "tool_use_id": f"toolu_{len(trail)}", "tool_input": args})
            return "denied"
        srv = subprocess.run([server["command"], *server["args"]], capture_output=True, text=True, input="\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": tool, "arguments": args}})]) + "\n")
        reply = [json.loads(ln) for ln in srv.stdout.splitlines() if ln.strip()][-1]["result"]
        trail[-1]["server"] = reply["content"][0]["text"]
        return "error" if reply.get("isError") else "recorded"

    def finish(text: str) -> None:
        with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
            f.write(json.dumps({"model": model, "prompt": prompt, "argv": argv, "agent": True, "trail": trail}) + "\n")
        print(json.dumps([{"type": "system", "subtype": "init"}, {
            "type": "result", "is_error": False, "result": text, "total_cost_usd": 0.02, "duration_ms": 9,
            "permission_denials": denials}]))
        sys.exit(0)

    if "Make exactly this tool call once" in prompt:
        tool = prompt.split("\nTool: ", 1)[1].split("\n", 1)[0]
        args = json.loads(prompt.split("\nArguments: ", 1)[1].split("\n", 1)[0])
        finish("" if tool_call(tool, args) == "recorded" else "The call was blocked.")
    queue_path = os.environ["FAKE_CLAUDE_QUEUE"]
    while True:
        with open(queue_path) as f:
            queue = json.load(f)
        if not queue:
            finish("")
        item = queue.pop(0)
        with open(queue_path, "w") as f:
            json.dump(queue, f)
        if item.get("action") != "step":
            finish(str(item.get("report") or ""))
        if tool_call(item["tool"], item.get("arguments") or {}) == "recorded":
            finish("")


if "--permission-mode" in argv:
    agent_session()

if model == "haiku":
    obj = {"instructions_to_ai": "INJECT" in prompt}
    print(json.dumps([{"type": "system"}, {"type": "result", "is_error": False,
                                           "structured_output": obj, "total_cost_usd": 0.0, "duration_ms": 1}]))
    sys.exit(0)

queue_path = os.environ["FAKE_CLAUDE_QUEUE"]
with open(queue_path) as f:
    queue = json.load(f)
if not queue:
    print("fake claude: queue empty", file=sys.stderr)
    sys.exit(1)
item = queue.pop(0)
with open(queue_path, "w") as f:
    json.dump(queue, f)

if "__error__" in item:
    print(json.dumps([{"type": "system"}, {"type": "result", "is_error": True, "result": item["__error__"]}]))
    sys.exit(0)

result = {"type": "result", "is_error": False, "result": "", "total_cost_usd": 0.01, "duration_ms": 5}
if os.environ.get("FAKE_CLAUDE_MODE") == "text":
    result["result"] = json.dumps(item)
else:
    result["structured_output"] = item
print(json.dumps([{"type": "system", "subtype": "init"}, {"type": "assistant"}, result]))
