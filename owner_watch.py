#!/usr/bin/env python3
"""mcp-slicc's answer to SLICC seven's owner tier (2026-11-18, README "SLICC seven"). Stdlib only.

    python3 owner_watch.py <job> <cdp-url> [--follower]

The launcher sets seven's approval policy to owner.tools "user" (seven.mjs). From then on seven files every tool call
of its cones and scoops, codemode's nested calls included, as an `owner-tool` item that waits up to 5 minutes for an
owner device's answer, and runs the call only on "once" or "always". This process is that owner device for the job:
it follows `slicc approvals --watch --json` over the launcher's CDP endpoint and answers each item with `slicc decide`:

  * the change gate (common.classify_step, seven's rules) judges the call: auto is "once", stop is "deny" with the
    gate's reason
  * a call the endpoint itself handed seven (handed.jsonl: same tool, same arguments) was judged and approved there
    already, so it is "once" without asking again
  * a gated call (ask) goes where the endpoint's own gated steps go: with approvals set to classifier, one short
    headless session puts that exact call to Claude Code's classifier (claude_turn.approve_step); otherwise it is a
    question of the job that slicc_decide answers
  * arguments seven cut short (argsTruncated) or cannot be read are denied: the gate never saw all of them

With --follower (an attached job, 2026-11-19, README "Attached jobs on seven") the CDP URL is the Chrome of mcp-slicc's
follower seven, which follows the person's own seven with View: an owner device. The items come from the agent
connection on that page (`seven.mjs approvals`, one process for the job), and the answers go back through the same
process. The gate's verdicts are the same.

"always" is never given, so no rule outlives the job. Other kinds (sudo, capture, guests) are left alone: nothing here
answers seven's sudo, which stays seven's own second lock. When this process is not running, nobody answers, and seven
denies each call after 5 minutes: it fails closed.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # also under python3 -I
import common as C

SEEN: set = set()
LOCK = threading.Lock()


def slicc_bin() -> str:
    return os.environ.get("CLAUDE_SLICC_SEVEN_BIN") or str(C.HOME / "slicc7" / "node_modules" / ".bin" / "slicc")


def _env() -> dict:
    return dict(os.environ, SLICC_NO_TELEMETRY="1", SLICC_NO_UPDATE_CHECK="1")


def _log(jid: str, rec: dict) -> None:
    C.append_jsonl(C.job_dir(jid) / "calls.jsonl", {"ts": C.now_iso(), "event": "owner_tool", **rec})


FOLLOWER = {"proc": None}
FOLLOWER_LOCK = threading.Lock()


def seven_mjs() -> list:
    return [os.environ.get("CLAUDE_SLICC_NODE", "node"), str(Path(__file__).resolve().parent / "seven.mjs")]


def decide(cdp: str, item: str, answer: str, reason: str = "") -> bool:
    proc = FOLLOWER["proc"]
    if proc is not None:              # the follower: the answer goes through the job's one seven.mjs approvals process
        line = json.dumps({"id": item, "answer": answer, **({"reason": reason[:500]} if reason else {})})
        try:
            with FOLLOWER_LOCK:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False
    cmd = [slicc_bin(), "decide", "--cdp", cdp, item, answer, "--json"] + (["--reason", reason[:500]] if reason else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=_env(), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def take_handed(jid: str, tool: str, key: str) -> bool:
    """Whether the endpoint handed seven exactly this call in the last 10 minutes; each handed call answers one item."""
    path = C.job_dir(jid) / "handed.jsonl"
    used_path = C.job_dir(jid) / "handed_used.json"
    with LOCK:
        used = set(C.read_json(used_path) or [])
        for i, row in enumerate(C.read_jsonl(path)):
            if i in used or row.get("tool") != tool or row.get("args") != key:
                continue
            if time.time() - float(row.get("ts") or 0) > 600:
                continue
            used.add(i)
            C.write_json(used_path, sorted(used))
            return True
    return False


def ask_person(job: dict, tool: str, args: dict, v: dict, expires_ms: float) -> str:
    """A question of the job, answered with slicc_decide: allow, deny, or why there was no answer."""
    jid = job["id"]
    aid = "ma-" + secrets.token_hex(6)
    C.write_json(C.job_dir(jid) / "asks" / f"{aid}.json", {
        "id": aid, "job": jid, "ts": C.now_iso(), "tool": tool, "args": args, "detail": v.get("detail", ""),
        "reason": v.get("reason", ""), "source": "owner-tier"})
    path = C.job_dir(jid) / "decisions" / f"{aid}.json"
    end = min(time.time() + C.ASK_WAIT_S, expires_ms / 1000.0 - 10)
    while time.time() < end:
        if C.is_stopped(jid) or (C.job_record() or {}).get("status") != "running":
            break
        d = C.read_json(path)
        if isinstance(d, dict) and d.get("answer") in ("allow", "deny"):
            return d["answer"]
        time.sleep(0.5)
    if not path.exists():
        C.write_json(path, {"answer": "expired", "ts": C.now_iso()})
    return "expired"


def handle(cdp: str, item: dict) -> None:
    job = C.job_record() or {}
    jid = job.get("id")
    if not jid or job.get("status") != "running":
        decide(cdp, item["id"], "deny", "The mcp-slicc job has ended.")
        return
    detail = item.get("detail") or {}
    tool = str(detail.get("tool") or "")
    try:
        args = json.loads(detail.get("arguments") or "{}")
    except ValueError:
        args = None
    if detail.get("argsTruncated") or not isinstance(args, dict):
        why = "the call's arguments were cut short or cannot be read, so mcp-slicc's gate cannot check them"
        ok = decide(cdp, item["id"], "deny", f"Refused: {why}.")
        _log(jid, {"item": item["id"], "tool": tool, "decision": "stop", "answer": "deny", "reason": why, "sent": ok})
        return
    key = C.args_key(args)
    if take_handed(jid, tool, key):
        ok = decide(cdp, item["id"], "once")
        _log(jid, {"item": item["id"], "tool": tool, "decision": "handed", "answer": "once", "by": "endpoint",
                   "sent": ok})
        return
    v = C.classify_step(job, C.SIX_NAMES.get(tool, tool), args)
    decision, by, answer = v.get("decision"), "gate", "deny"
    if decision == "auto":
        answer = "once"
    elif decision == "ask":
        if C.approval() == "classifier" and C.AGENT_STEPS and tool in ("bash", "read", "write", "edit"):
            try:
                import claude_turn as T
                allowed, meta = T.approve_step(job, C.SIX_NAMES.get(tool, tool), args, v)
            except Exception as e:   # noqa: BLE001 - a failed check is a no
                allowed, meta = False, {"error": f"{type(e).__name__}: {e}"[:300]}
            by, answer = "classifier", "once" if allowed else "deny"
            _log(jid, {"event": "approval", "tool": tool, "by": "classifier", "answer": answer,
                       "cost_usd": meta.get("total_cost_usd"), **({"error": meta["error"]} if "error" in meta else {})})
        else:
            by = C.approval()
            got = ask_person(job, tool, args, v, float(item.get("expiresAt") or (time.time() + 290) * 1000))
            answer = "once" if got == "allow" else "deny"
    if answer == "once":
        ok = decide(cdp, item["id"], "once")
    else:
        reason = (f"Refused by mcp-slicc's change gate, and final: {v.get('detail')}: {v.get('reason')}"
                  if decision == "stop" else f"Not approved ({by}): {v.get('detail')}")
        ok = decide(cdp, item["id"], "deny", reason)
    _log(jid, {"item": item["id"], "tool": tool, "decision": decision, "answer": answer, "by": by,
               "detail": str(v.get("detail", ""))[:300], "sent": ok})


def watch(jid: str, cdp: str, follower: bool = False) -> None:
    while True:
        rec = C.job_record() or {}
        if rec.get("id") != jid or rec.get("status") != "running":
            return
        if follower:
            proc = subprocess.Popen(seven_mjs() + ["approvals", "--cdp", cdp], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, stdin=subprocess.PIPE, text=True, env=_env())
            FOLLOWER["proc"] = proc
        else:
            proc = subprocess.Popen([slicc_bin(), "approvals", "--cdp", cdp, "--watch", "--json"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    text=True, env=_env())
        stop = threading.Event()

        def ender():
            while not stop.wait(2):
                r = C.job_record() or {}
                if r.get("id") != jid or r.get("status") != "running":
                    proc.terminate()
                    return
        threading.Thread(target=ender, daemon=True).start()
        try:
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") == "error" and ev.get("id"):
                    _log(jid, {"item": ev.get("id"), "answer": None, "note": f"not answered: {ev.get('message')}"[:300]})
                if ev.get("event") != "approvals":
                    continue
                for item in ev.get("approvals") or []:
                    iid = str(item.get("id") or "")
                    if not iid or iid in SEEN:
                        continue
                    SEEN.add(iid)
                    if item.get("kind") != "owner-tool":
                        _log(jid, {"item": iid, "kind": item.get("kind"), "answer": None,
                                   "note": "not an owner-tool item: left to seven's own dialog"})
                        continue
                    threading.Thread(target=handle, args=(cdp, item), daemon=True).start()
        finally:
            stop.set()
            FOLLOWER["proc"] = None
            proc.terminate()
        time.sleep(2)


def main() -> None:
    argv = sys.argv[1:]
    follower = "--follower" in argv
    argv = [a for a in argv if a != "--follower"]
    if len(argv) != 2 or not C.JOB_ID.fullmatch(argv[0]):
        sys.exit("usage: owner_watch.py <job> <cdp-url> [--follower]")
    watch(argv[0], argv[1], follower)


if __name__ == "__main__":
    main()
