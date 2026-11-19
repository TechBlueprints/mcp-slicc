"""SLICC seven (2026-11-18, README "SLICC seven"): the endpoint as seven's `local` provider, seven's rules in the gate,
the owner-tier watcher and the seven launcher, with a recorded seven call (fixtures/seven_call.json) and stand-ins for
`slicc` and node. No SLICC, no browser, no login.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from test_approval_and_drops import ManualBase
from test_claude_slicc import HERE, Base, start_job

import common as C
import endpoint as E
import owner_watch as W
import slicc_gate as G
import slicc_mcp as M

FIXTURE = json.loads((HERE / "fixtures" / "seven_call.json").read_text())
SEVEN_TOOLS = FIXTURE["body"]["tools"]
REPO = HERE.parent


def seven_body(*extra, first: str = C.AUTO_PROMPT) -> dict:
    """The recorded call's shape (system prompt, seven's ten tools, stream flags), as the launcher's job sends it."""
    body = json.loads(json.dumps(FIXTURE["body"]))
    body["messages"] = [body["messages"][0], {"role": "user", "content": first}] + list(extra)
    return body


def seven_job(jid: str = "job-5e7e0000aaaa", **more) -> dict:
    job = start_job(jid)
    job.update(slicc="7", **more)
    C.write_json(C.HOME / "state" / "job.json", job)
    return job


class SevenBase(Base):
    def setUp(self):
        super().setUp()
        self._slicc = C.SLICC
        C.SLICC = "7"
        self.addCleanup(setattr, C, "SLICC", self._slicc)


class TheRecordedCall(SevenBase):
    def test_the_fixture_is_what_seven_sends(self):
        names = [t["function"]["name"] for t in SEVEN_TOOLS]
        self.assertEqual(names, ["read", "write", "edit", "bash", "memory_write", "codemode", "tool_search",
                                 "lick_confirm", "lick_dismiss", "workflow"])
        self.assertTrue(C.is_seven_call(SEVEN_TOOLS))
        self.assertFalse(C.is_seven_call([{"type": "function", "function": {"name": n}}
                                          for n in ("bash", "read_file", "write_file", "edit")]))
        self.assertEqual(C.step_tools(SEVEN_TOOLS), ["bash", "read_file", "write_file", "edit"])
        headers = {k.lower(): v for k, v in FIXTURE["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer local")   # a keyless account
        self.assertEqual((FIXTURE["body"]["stream"], "max_tokens" in FIXTURE["body"]), (True, True))

    def test_a_lick_is_seven_s_own_event_and_starts_no_chat_job(self):
        lick = next(m["content"] for m in FIXTURE["body"]["messages"] if "<lick" in str(m.get("content")))
        self.assertFalse(C.sent_by_person(lick))
        self.assertTrue(C.sent_by_person("Check the browser."))
        body = json.loads(json.dumps(FIXTURE["body"]))
        body["messages"] = body["messages"][:1] + [{"role": "user", "content": lick}]
        self.assertEqual(E.on_call(body), {"content": "", "tool_calls": []})
        self.assertIsNone(C.current_job())

    def test_the_launcher_job_is_recognised_and_its_steps_go_out_under_seven_s_names(self):
        job = seven_job()
        self.assertEqual(E.job_messages(job, seven_body()["messages"], SEVEN_TOOLS), seven_body()["messages"])
        self.queue_set([{"action": "step", "tool": "read_file", "arguments": {"path": "/tmp/notes.txt"}}])
        r = E.on_call(seven_body())
        tc = r["tool_calls"][0]["function"]
        self.assertEqual((tc["name"], json.loads(tc["arguments"])), ("read", {"path": "/tmp/notes.txt"}))
        handed = C.read_jsonl(C.job_dir(job["id"]) / "handed.jsonl")
        self.assertEqual([(h["tool"], h["args"]) for h in handed], [("read", '{"path":"/tmp/notes.txt"}')])
        self.assertIn("SLICC seven's own shell", json.dumps(self.fake_calls()[0]["argv"]))

    def test_a_seven_step_is_judged_by_seven_s_rules(self):
        job = seven_job()
        self.queue_set([{"action": "step", "tool": "bash", "arguments": {"command": "agent spawn --model x helper"}},
                        {"action": "final", "report": "Could not."}])
        r = E.on_call(seven_body())
        self.assertEqual(r["tool_calls"], [])
        stops = [x for x in self.job_rows(job["id"]) if x.get("event") == "step"]
        self.assertEqual(stops[0]["decision"], "stop")
        self.assertIn("`agent` is never run", stops[0]["reason"])


class StopWins(SevenBase):
    def test_a_report_that_comes_after_slicc_stop_leaves_the_job_stopped(self):
        job = seven_job("job-5e7e0000bbbb")
        self.queue_set([{"action": "final", "report": "All four titles."}])
        orig = E.T.decide_step

        def deciding(*a, **k):   # slicc_stop lands while the model decides
            out = orig(*a, **k)
            M.t_stop({"job": job["id"]})
            return out
        E.T.decide_step = deciding
        self.addCleanup(setattr, E.T, "decide_step", orig)
        r = E.on_call(seven_body())
        self.assertEqual(r["content"], "The job was stopped. Stop here.")
        self.assertEqual(C.job_record()["status"], "stopped")
        self.assertFalse((C.job_dir(job["id"]) / "report.json").exists())


class TheEndpoint(SevenBase):
    def setUp(self):
        super().setUp()
        self.srv = E.make_server(port=0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def get(self, path: str, auth: str = "") -> tuple:
        req = urllib.request.Request(self.base + path, headers={"Authorization": auth} if auth else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_the_token_in_the_path_or_the_header(self):
        tok = C.token()
        code, body = self.get(f"/t/{tok}/v1/models", "Bearer local")
        self.assertEqual(code, 200)
        self.assertEqual(self.get("/v1/models", f"Bearer {tok}")[0], 200)
        self.assertEqual(self.get("/v1/models", "Bearer local")[0], 401)
        self.assertEqual(self.get("/t/not-the-token/v1/models", "Bearer local")[0], 401)
        self.assertEqual(self.get(f"/t/{tok}/v2/models")[0], 404)
        self.assertTrue(C.seven_endpoint().endswith(f"/t/{tok}/v1"))

    def test_models_carry_their_context_window(self):
        _, body = self.get(f"/t/{C.token()}/v1/models")
        ctx = {m["id"]: m["context_length"] for m in body["data"]}
        self.assertEqual(ctx["claude-slicc"], 200000)
        self.assertEqual(ctx["qwen-3.8-medium"], C.SPARK_CONTEXT)

    def test_seven_s_origin_may_call(self):
        self.assertIn("https://seven.sliccy.ai", C.ORIGINS)


class TheGate(SevenBase):
    def v(self, tool: str, args: dict) -> str:
        G.POLICY = G.load_policy()
        return G.slicc7_classify(tool, args)["decision"]

    def test_seven_s_rules(self):
        cases = [
            ("bash", {"command": "ls /home && cat /etc/hostname | wc -l"}, "auto"),
            ("bash", {"command": "playwright-cli snapshot --tab=AB12"}, "auto"),
            ("bash", {"command": "playwright-cli open https://example.com"}, "ask"),
            ("bash", {"command": "playwright-cli open file:///etc/passwd"}, "stop"),
            ("bash", {"command": "curl -s https://example.com"}, "ask"),
            ("bash", {"command": "sudo apk add x"}, "ask"),
            ("bash", {"command": "sudo tee /etc/sudoers.d/x"}, "stop"),
            ("bash", {"command": "echo '{}' > ~/.pi/agent/settings.json"}, "stop"),
            ("bash", {"command": "slicc tray"}, "stop"),
            ("bash", {"command": "agent spawn helper"}, "stop"),
            ("bash", {"command": "fnox env"}, "stop"),
            ("bash", {"command": "npm i left-pad"}, "stop"),
            ("bash", {"command": "echo sk-ant-api03-abcdefghijklmnop"}, "stop"),
            ("read", {"path": "/etc/hostname"}, "auto"),
            ("read", {"path": "/mnt/host/notes.txt"}, "ask"),
            ("write", {"path": "/tmp/out.txt", "content": "x"}, "auto"),
            ("write", {"path": "/tmp/../etc/slicc/APPROVALS.md", "content": "x"}, "stop"),
            ("write", {"path": ".pi/agent/settings.json", "content": "{}"}, "stop"),
            ("edit", {"path": "/home/cone/MEMORY.md", "edits": []}, "stop"),
            ("edit", {"path": "notes.md", "edits": []}, "ask"),
            ("codemode", {"code": "return 1"}, "stop"),
            ("memory_write", {"section": "a", "title": "b"}, "stop"),
            ("workflow", {"action": "run"}, "stop"),
            ("lick_confirm", {"lick_id": "lk-1"}, "stop"),
            ("lick_dismiss", {"lick_id": "lk-1"}, "auto"),
            ("tool_search", {"query": "mcp"}, "auto"),
            ("mcp__server__do_something", {}, "ask"),
        ]
        for tool, args, want in cases:
            with self.subTest(tool=tool, args=args):
                self.assertEqual(self.v(tool, args), want)

    def test_six_s_verdicts_do_not_change(self):
        G.POLICY = G.load_policy()
        for cmd, want in (("sudo ls", "stop"), ("serve /workspace", "stop"), ("ls /workspace", "auto"),
                          ("echo x > /etc/slicc/x", "ask"), ("playwright-cli open https://example.com", "ask")):
            self.assertEqual(G.slicc_classify("bash", {"command": cmd})["decision"], want, cmd)
        self.assertEqual(G.slicc_classify("write_file", {"path": "/tmp/../etc/x"})["decision"], "auto")

    def test_codemode_runs_only_under_the_owner_tier(self):
        self.assertEqual(C.classify_step(seven_job(), "codemode", {"code": "return 1"})["decision"], "stop")
        v = C.classify_step(seven_job(owner_tier=True), "codemode", {"code": "return 1"})
        self.assertEqual(v["decision"], "auto")
        self.assertIn("owner tier", v["reason"])
        self.assertEqual(C.classify_step(seven_job(owner_tier=True), "memory_write", {})["decision"], "stop")

    def test_a_gate_without_seven_s_rules_stops_seven_s_steps(self):
        old = self.tmp / "old_gate.py"
        old.write_text("import json\nPOLICY={}\ndef load_policy():\n    return {'slicc': {}}\n"
                       "def _segments(c):\n    return None\n"
                       "def slicc_classify(t, a):\n    return {'decision': 'auto', 'reason': 'x', 'detail': t}\n")
        C.GATE_PATH, C._gate = str(old), None
        v = C.classify_step(seven_job(), "bash", {"command": "ls"})
        self.assertEqual((v["decision"], v["reason"]), ("stop", "the change gate has no rules for SLICC seven"))


class ManualOnSeven(ManualBase):
    def setUp(self):
        super().setUp()
        self._slicc = C.SLICC
        C.SLICC = "7"
        self.addCleanup(setattr, C, "SLICC", self._slicc)

    def test_a_manual_step_goes_to_seven_under_seven_s_name(self):
        jid = self.start_manual()
        self.assertEqual(C.job_record()["slicc"], "7")
        th, box, _ = self.call_slicc(seven_body())
        self.held(jid)
        out = M.t_write_file({"path": "/tmp/a.txt", "content": "hello"})
        self.assertEqual(out["status"], "working")
        th.join(5)
        tc = box["r"]["tool_calls"][0]["function"]
        self.assertEqual((tc["name"], json.loads(tc["arguments"])), ("write", {"path": "/tmp/a.txt", "content": "hello"}))
        refused = M.t_bash({"command": "slicc tray"})
        self.assertEqual(refused["status"], "refused")

    def test_an_attached_seven_job_says_what_is_gated(self):
        out = M.t_task({"instruction": "Read a page.", "launch": "attach"})
        self.assertIn("codemode", out["gated"])
        self.assertIn("another provider", out["gated"])


FAKE_SLICC = r'''#!/usr/bin/env python3
"""A stand-in for slicc: approvals --watch prints $FAKE_ITEMS as one list; decide appends its argv to $FAKE_DECIDED."""
import json, os, sys, time
a = sys.argv[1:]
if a[0] == "approvals":
    print(json.dumps({"schema": 1, "event": "approvals", "approvals": json.load(open(os.environ["FAKE_ITEMS"]))}),
          flush=True)
    time.sleep(30)
elif a[0] == "decide":
    with open(os.environ["FAKE_DECIDED"], "a") as f:
        f.write(json.dumps(a) + "\n")
    print(json.dumps({"schema": 1, "event": "decided", "id": a[3], "answer": a[4]}))
elif a[0] == "capabilities":
    print(os.environ.get("FAKE_CAPS", '{"schema":1,"version":"1.69.1","contract":1}'))
'''


def item(iid: str, tool: str, args, kind: str = "owner-tool", **more) -> dict:
    detail = {"tool": tool, "summary": "x", "arguments": args if isinstance(args, str) else json.dumps(args), **more}
    return {"id": iid, "kind": kind, "agentId": "cone", "detail": detail, "tier": "user", "always": True,
            "expiresAt": (time.time() + 300) * 1000}


class OwnerTier(SevenBase):
    def setUp(self):
        super().setUp()
        fake = self.tmp / "slicc"
        fake.write_text(FAKE_SLICC)
        fake.chmod(0o755)
        os.environ["CLAUDE_SLICC_SEVEN_BIN"] = str(fake)
        self.addCleanup(os.environ.pop, "CLAUDE_SLICC_SEVEN_BIN", None)
        self.items, self.decided = self.tmp / "items.json", self.tmp / "decided.jsonl"
        os.environ.update(FAKE_ITEMS=str(self.items), FAKE_DECIDED=str(self.decided))
        W.SEEN.clear()

    def answers(self, n: int, timeout: float = 20) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            rows = [json.loads(ln) for ln in self.decided.read_text().splitlines()] if self.decided.exists() else []
            if len(rows) >= n:
                return {r[3]: (r[4], r[r.index("--reason") + 1] if "--reason" in r else "") for r in rows}
            time.sleep(0.1)
        self.fail(f"only {self.decided.read_text() if self.decided.exists() else 'no'} answers")

    def run_watch(self, job: dict) -> threading.Thread:
        th = threading.Thread(target=W.watch, args=(job["id"], "http://127.0.0.1:1"), daemon=True)
        th.start()
        return th

    def test_each_item_gets_the_gate_s_answer_and_never_always(self):
        job = seven_job(owner_tier=True)
        C.APPROVAL = "operator"
        E._handed(job["id"], "bash", {"intent": "Opening the page", "command": "playwright-cli open https://example.com"})
        self.items.write_text(json.dumps([
            item("apr-handed", "bash", {"command": "playwright-cli open https://example.com"}),
            item("apr-read", "bash", {"command": "ls /tmp"}),
            item("apr-code", "codemode", {"code": "await tools.bash({command: 'ls'})"}),
            item("apr-stop", "bash", {"command": "sudo tee /etc/sudoers"}),
            item("apr-mem", "memory_write", {"section": "a", "title": "b"}),
            item("apr-cut", "bash", '{"command": "ls', argsTruncated=True),
            item("apr-sudo", "sudo", {}, kind="sudo"),
        ]))
        self.run_watch(job)
        got = self.answers(6)
        self.assertEqual(got["apr-handed"][0], "once")
        self.assertEqual(got["apr-read"][0], "once")
        self.assertEqual(got["apr-code"][0], "once")
        self.assertEqual(got["apr-stop"][0], "deny")
        self.assertIn("Refused by mcp-slicc's change gate", got["apr-stop"][1])
        self.assertEqual(got["apr-mem"][0], "deny")
        self.assertEqual(got["apr-cut"][0], "deny")
        time.sleep(0.5)
        self.assertNotIn("apr-sudo", self.answers(6))                  # seven's sudo is not answered here
        self.assertNotIn("always", self.decided.read_text())
        rows = [r for r in self.job_rows(job["id"]) if r.get("event") == "owner_tool" and r.get("item")]
        self.assertEqual({r["item"]: r.get("decision") for r in rows}["apr-handed"], "handed")
        C.mark_job(job["id"], status="done")

    def test_a_gated_nested_call_is_a_question_for_slicc_decide(self):
        job = seven_job(owner_tier=True)
        C.APPROVAL = "person"
        self.items.write_text(json.dumps([item("apr-curl", "bash", {"command": "curl https://example.com"})]))
        self.run_watch(job)
        end = time.time() + 10
        while time.time() < end and not C.open_asks(job["id"]):
            time.sleep(0.1)
        ask = C.open_asks(job["id"])[0]
        self.assertEqual((ask["source"], ask["tool"]), ("owner-tier", "bash"))
        M.t_decide({"ask": ask["id"], "answer": "allow"})
        self.assertEqual(self.answers(1)["apr-curl"][0], "once")
        C.mark_job(job["id"], status="done")

    def test_without_the_owner_tier_codemode_is_refused(self):
        job = seven_job()
        self.items.write_text(json.dumps([item("apr-code", "codemode", {"code": "return 1"})]))
        self.run_watch(job)
        self.assertEqual(self.answers(1)["apr-code"][0], "deny")
        C.mark_job(job["id"], status="done")


class TheLauncher(unittest.TestCase):
    """start_seven.sh and stop_slicc.sh with stand-ins for slicc and node under a temporary home."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.cs = self.tmp / "home", self.tmp / "cs"
        conf = self.home / ("Library/Application Support" if sys.platform == "darwin" else ".config")   # slicc's
        self.pdir = conf / "slicc" / "seven-profiles" / "mcp-slicc"
        (self.pdir / "Default").mkdir(parents=True)
        (self.pdir / "Default" / "old.txt").write_text("from the last job")
        self.other = self.pdir.parent / "mcp-slicc-mine"
        self.other.mkdir()
        (self.other / "keep.txt").write_text("a profile of the person's own")
        self.cs.mkdir()
        fake = self.tmp / "slicc"
        fake.write_text("#!/bin/bash\n"
                        'echo "$@" >> "$FAKE_SLICC_LOG"\n'
                        'case "$1" in\n'
                        '  capabilities) if [ -n "$FAKE_CAPS" ]; then echo "$FAKE_CAPS"; else\n'
                        '      echo \'{"schema":1,"version":"1.69.1","contract":1}\'; fi ;;\n'
                        '  prompt|abort|approvals) ;;\n'
                        '  *) echo \'{"schema":1,"event":"launched","cdp":"http://127.0.0.1:9","profileDir":"\'"$FAKE_PDIR"\'"}\'\n'
                        '     echo \'{"schema":1,"event":"linked","tab":"https://seven.sliccy.ai"}\'\n'
                        '     trap "exit 0" TERM; sleep 60 & wait ;;\n'
                        'esac\n')
        fake.chmod(0o755)
        node = self.tmp / "node"
        node.write_text('#!/bin/bash\necho "$@" >> "$FAKE_SLICC_LOG"\n'
                        'if [ -n "$FAKE_SEED" ]; then echo "$FAKE_SEED"; else echo \'{"ok":true}\'; fi\n')
        node.chmod(0o755)
        self.log = self.tmp / "slicc.log"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_SLICC_") and k != "XDG_CONFIG_HOME"}
        self.env.update(HOME=str(self.home), CLAUDE_SLICC_HOME=str(self.cs), CLAUDE_SLICC_SEVEN_BIN=str(fake),
                        CLAUDE_SLICC_NODE=str(node), CLAUDE_SLICC_NO_SERVERS="1", CLAUDE_SLICC_SLICC="7",
                        CLAUDE_SLICC_OWNER_TIER="off", CLAUDE_SLICC_SEVEN_LINK_WAIT="10",
                        FAKE_SLICC_LOG=str(self.log), FAKE_PDIR=str(self.pdir))
        self.jid = "job-0000aaaa"
        (self.cs / "state").mkdir()
        (self.cs / "state" / "job.json").write_text(json.dumps({"id": self.jid, "status": "running", "slicc": "7"}))
        self.addCleanup(lambda: subprocess.run([str(REPO / "stop_slicc.sh"), "quiet"], env=self.env))

    def start(self, **env) -> subprocess.CompletedProcess:
        return subprocess.run([str(REPO / "start_slicc.sh"), self.jid, C.AUTO_PROMPT], env={**self.env, **env},
                              capture_output=True, text=True, timeout=60)

    def test_a_job_gets_a_fresh_profile_and_stop_ends_only_what_it_started(self):
        p = self.start()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse((self.pdir / "Default" / "old.txt").exists())
        self.assertTrue((self.other / "keep.txt").exists())
        self.assertEqual((self.cs / "run" / "seven.cdp").read_text().strip(), "http://127.0.0.1:9")
        for _ in range(50):
            if "prompt --cdp" in self.log.read_text():
                break
            time.sleep(0.1)
        calls = self.log.read_text()
        self.assertIn("--profile mcp-slicc --browser chrome --json --yes --no-fallback --no-kernel --no-jerk", calls)
        self.assertIn("--allow 127.0.0.1/32:8660", calls)
        self.assertIn("seed --cdp http://127.0.0.1:9 --endpoint http://127.0.0.1:8660/t/", calls)
        self.assertIn(f"prompt --cdp http://127.0.0.1:9 --json {C.AUTO_PROMPT}", calls)
        pid = int((self.cs / "run" / "seven.pid").read_text())
        subprocess.run([str(REPO / "stop_slicc.sh"), "quiet"], env=self.env, check=True)
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIn("abort --cdp http://127.0.0.1:9", self.log.read_text())

    def test_a_cli_of_another_contract_ends_the_job_with_one_line(self):
        p = self.start(FAKE_CAPS='{"schema":1,"version":"2.0.0","contract":2}')
        self.assertEqual(p.returncode, 1)
        rep = json.loads((self.cs / "jobs" / self.jid / "report.json").read_text())
        self.assertTrue(rep["failed"])
        self.assertIn("slicc 2.0.0 offers contract 2; mcp-slicc is built for slicc 1.x, contract 1", rep["text"])
        self.assertEqual(json.loads((self.cs / "state" / "job.json").read_text())["status"], "failed")

    def test_a_failed_seed_ends_the_job_and_stops_seven(self):
        p = self.start(FAKE_SEED='{"ok":false,"error":"SLICC seven runs slicc-agent 9.0.0; mcp-slicc is built for 8.x"}')
        self.assertEqual(p.returncode, 1)
        rep = json.loads((self.cs / "jobs" / self.jid / "report.json").read_text())
        self.assertIn("slicc-agent 9.0.0", rep["text"])
        self.assertFalse((self.cs / "run" / "seven.pid").exists())


if __name__ == "__main__":
    unittest.main()
