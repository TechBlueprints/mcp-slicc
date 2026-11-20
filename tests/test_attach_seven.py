"""Attached jobs on SLICC seven (2026-11-19, README "Attached jobs on seven"): a stopped job's chat starts nothing, the
owner-tier watcher through mcp-slicc's follower seven, slicc_stop of an attached job, and attach_seven.sh, with
stand-ins for `slicc` and node. No SLICC, no browser, no login.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_claude_slicc import HERE
from test_seven import FIXTURE, SevenBase, item, seven_job

import common as C
import endpoint as E
import owner_watch as W

REPO = HERE.parent
JID = "job-a77ac4ed0001"

# Stands in for node running seven.mjs: argv is seven.mjs's path, then the command.
FAKE_NODE = r'''#!/bin/bash
shift
echo "$@" >> "$FAKE_NODE_LOG"
case "$1" in
  follow) if [ -n "$FAKE_FOLLOW" ]; then echo "$FAKE_FOLLOW"; else
            echo '{"ok":true,"name":"SLICC on test","access":{"view":true},"owner":{"tools":"off"}}'; fi ;;
  policy) echo '{"ok":true}' ;;
  approvals)
    if [ -s "${FAKE_ITEMS:-/nonexistent}" ]; then
      printf '{"schema":1,"event":"approvals","approvals":%s}\n' "$(cat "$FAKE_ITEMS")"
    else
      echo '{"schema":1,"event":"approvals","approvals":[]}'
    fi
    while read -r line; do echo "$line" >> "$FAKE_DECIDED"; done ;;
esac
'''


def fake_node(tmp: Path) -> Path:
    node = tmp / "node"
    node.write_text(FAKE_NODE)
    node.chmod(0o755)
    return node


def chat_body(*extra) -> dict:
    """A seven call in the person's own seven: no launch prompt, seven's ten tools."""
    body = json.loads(json.dumps(FIXTURE["body"]))
    body["messages"] = [body["messages"][0]] + list(extra)
    return body


def tool_round(call_id: str, command: str, result: str) -> list:
    return [{"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {
        "name": "bash", "arguments": json.dumps({"command": command})}}]},
            {"role": "tool", "tool_call_id": call_id, "content": result}]


class AStoppedJobsChat(SevenBase):
    """2026-11-19, live: an attached seven job was stopped after its first step; seven's next call came while no job
    ran, and the endpoint made the job's start line the instruction of a new chat job, which ran on in the person's
    Chrome."""

    def answer(self, body: dict) -> str:
        return E._local_call(body, threading.Event()).get("content") or ""

    def test_a_turn_going_on_after_its_job_ended_starts_nothing(self):
        line = C.attach_line(JID)
        body = chat_body({"role": "user", "content": line},
                         *tool_round("c1", "playwright-cli open https://example.com", "Opened https://example.com"))
        self.assertIsNone(E.start_chat_job(body["messages"], body["tools"]))
        self.assertIn(f"mcp-slicc's job {JID} has ended", self.answer(body))
        self.assertIsNone(C.current_job())

    def test_a_start_line_sent_again_after_its_job_ended_starts_nothing(self):
        body = chat_body({"role": "user", "content": C.attach_line(JID)})
        self.assertIsNone(E.start_chat_job(body["messages"], body["tools"]))
        self.assertIn("has ended", self.answer(body))
        self.assertIsNone(C.current_job())

    def test_a_new_message_from_the_person_still_starts_a_chat_job(self):
        body = chat_body({"role": "user", "content": C.attach_line(JID)},
                         *tool_round("c1", "ls", "a b"),
                         {"role": "assistant", "content": "Done."},
                         {"role": "user", "content": "What is the title of example.com?"})
        job = E.start_chat_job(body["messages"], body["tools"])
        self.assertIsNotNone(job)
        self.assertEqual(job["instruction"], "What is the title of example.com?")
        C.mark_job(job["id"], status="done")

    def test_the_running_job_s_own_chat_is_still_its_job(self):
        job = seven_job(JID, launch="attach")
        body = chat_body({"role": "user", "content": C.attach_line(JID)}, *tool_round("c1", "ls", "a b"))
        self.assertIsNotNone(E.job_messages(job, body["messages"], body["tools"]))
        C.mark_job(JID, status="done")


class TheFollowerWatch(SevenBase):
    """owner_watch.py --follower: items from seven.mjs approvals, answers on its stdin, the gate's verdicts."""

    def setUp(self):
        super().setUp()
        os.environ["CLAUDE_SLICC_NODE"] = str(fake_node(self.tmp))
        self.addCleanup(os.environ.pop, "CLAUDE_SLICC_NODE", None)
        self.items, self.decided = self.tmp / "items.json", self.tmp / "decided.jsonl"
        os.environ.update(FAKE_ITEMS=str(self.items), FAKE_DECIDED=str(self.decided),
                          FAKE_NODE_LOG=str(self.tmp / "node.log"))
        W.SEEN.clear()

    def answers(self, n: int, timeout: float = 20) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            rows = [json.loads(ln) for ln in self.decided.read_text().splitlines()] if self.decided.exists() else []
            if len(rows) >= n:
                return {r["id"]: (r["answer"], r.get("reason", "")) for r in rows}
            time.sleep(0.1)
        self.fail(f"only {self.decided.read_text() if self.decided.exists() else 'no'} answers")

    def test_each_item_is_answered_through_the_follower_with_the_gate_s_verdict(self):
        job = seven_job(JID, launch="attach", owner_tier=True)
        E._handed(JID, "bash", {"command": "playwright-cli open https://example.com"})
        self.items.write_text(json.dumps([
            item("apr-handed", "bash", {"command": "playwright-cli open https://example.com"}),
            item("apr-code", "codemode", {"code": "await tools.bash({command: 'ls'})"}),
            item("apr-stop", "bash", {"command": "sudo tee /etc/sudoers"}),
            item("apr-cut", "bash", '{"command": "ls'),
        ]))
        th = threading.Thread(target=W.watch, args=(JID, "http://127.0.0.1:1", True), daemon=True)
        th.start()
        got = self.answers(4)
        self.assertEqual(got["apr-handed"][0], "once")
        self.assertEqual(got["apr-code"][0], "once")
        self.assertEqual(got["apr-stop"][0], "deny")
        self.assertIn("Refused by mcp-slicc's change gate", got["apr-stop"][1])
        self.assertEqual(got["apr-cut"][0], "deny")
        self.assertNotIn("always", self.decided.read_text())
        self.assertIn("approvals --cdp http://127.0.0.1:1", (self.tmp / "node.log").read_text())
        C.mark_job(job["id"], status="done")
        th.join(10)
        self.assertFalse(th.is_alive())


class SliccStopOfAnAttachedJob(SevenBase):
    def test_it_ends_only_mcp_slicc_s_follower(self):
        job = seven_job(JID, launch="attach")
        ran = []
        orig = C._run_cmd
        C._run_cmd = lambda template, j, done: ran.append(template) or done
        self.addCleanup(setattr, C, "_run_cmd", orig)
        C.STOP_CMD = "/x/stop_slicc.sh"
        self.assertEqual(C.stop_slicc(job), C.LEFT_RUNNING)          # no follower running: nothing at all
        self.assertEqual(ran, [])
        C.write_text(C.HOME / "run" / "attach.pid", "1\n")
        self.assertIn("puts your seven's approval policy back", C.stop_slicc(job))
        self.assertEqual(ran, ["/x/stop_slicc.sh attach quiet"])
        six = dict(job, slicc="6")
        self.assertEqual(C.stop_slicc(six), C.LEFT_RUNNING)
        C.mark_job(JID, status="done")


class TheAttachSupervisor(unittest.TestCase):
    """attach_seven.sh under a temporary home, with stand-ins for slicc (launch) and node (seven.mjs)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cs = self.tmp / "cs"
        (self.cs / "state").mkdir(parents=True)
        slicc = self.tmp / "slicc"
        slicc.write_text("#!/bin/bash\n"
                         'echo "$@" >> "$FAKE_SLICC_LOG"\n'
                         'echo \'{"schema":1,"event":"launched","cdp":"http://127.0.0.1:9"}\'\n'
                         'trap "exit 0" TERM; sleep 60 & wait\n')
        slicc.chmod(0o755)
        self.log, self.nlog = self.tmp / "slicc.log", self.tmp / "node.log"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_SLICC_", "FAKE_"))}
        self.env.update(CLAUDE_SLICC_HOME=str(self.cs), CLAUDE_SLICC_SEVEN_BIN=str(slicc),
                        CLAUDE_SLICC_NODE=str(fake_node(self.tmp)), FAKE_SLICC_LOG=str(self.log),
                        FAKE_NODE_LOG=str(self.nlog), FAKE_DECIDED=str(self.tmp / "decided.jsonl"),
                        CLAUDE_SLICC_GATE=str(REPO / "slicc_gate.py"))
        self.state = self.cs / "state" / "job.json"
        self.state.write_text(json.dumps({"id": JID, "status": "running", "slicc": "7", "launch": "attach"}))
        self.follow = self.cs / "seven-follow"

    def record(self) -> dict:
        return json.loads(self.state.read_text())

    def run_script(self, wait: bool = True, **env):
        p = subprocess.Popen([str(REPO / "attach_seven.sh"), JID], env={**self.env, **env},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait:
            p.wait(60)
        return p

    def until(self, cond, timeout: float = 20) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        self.fail("timed out")

    def end_job(self, status: str = "done") -> None:
        rec = self.record()
        rec["status"] = status
        self.state.write_text(json.dumps(rec))

    def test_without_a_follow_link_nothing_is_touched(self):
        self.run_script()
        self.assertTrue(self.record()["attach_tier"].startswith("off: no follow link"))
        self.assertFalse(self.log.exists())
        self.assertFalse(self.nlog.exists())

    def test_the_policy_is_set_for_the_job_and_put_back_when_it_ends(self):
        self.follow.write_text("https://seven.sliccy.ai/#follow=x\n")
        p = self.run_script(wait=False)
        self.until(lambda: str(self.record().get("attach_tier", "")).startswith("on"))
        rec = self.record()
        self.assertTrue(rec["attach_tier"].startswith("on: owner.tools user"))
        self.assertIn('"tools": "off"', rec["attach_tier"])
        self.assertEqual(json.loads((self.cs / "run" / "attach.policy.json").read_text()), {"owner": {"tools": "off"}})
        self.assertIn("--profile mcp-slicc-follow --browser chrome --json --url https://seven.sliccy.ai/os/ --headless",
                      self.log.read_text())
        self.until(lambda: "approvals --cdp http://127.0.0.1:9" in self.nlog.read_text())
        follower = int((self.cs / "run" / "follower.pid").read_text())
        self.end_job()
        p.wait(60)
        calls = self.nlog.read_text().splitlines()
        self.assertIn("follow --cdp http://127.0.0.1:9 --follow-file " + str(self.follow) + " --timeout 180", calls)
        self.assertIn("policy --cdp http://127.0.0.1:9 --owner-tier user --timeout 60", calls)
        self.assertEqual(calls[-1], "policy --cdp http://127.0.0.1:9 --owner-tier off --timeout 60")
        self.assertFalse((self.cs / "run" / "attach.policy.json").exists())
        self.assertFalse((self.cs / "run" / "attach.pid").exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(follower, 0)

    def test_stop_puts_the_policy_back_too(self):
        self.follow.write_text("https://seven.sliccy.ai/#follow=x\n")
        p = self.run_script(wait=False)
        self.until(lambda: str(self.record().get("attach_tier", "")).startswith("on"))
        self.until(lambda: "approvals" in self.nlog.read_text())
        subprocess.run([str(REPO / "stop_slicc.sh"), "attach", "quiet"], env=self.env, timeout=60, check=True)
        self.assertIsNotNone(p.poll())
        self.assertEqual(self.nlog.read_text().splitlines()[-1],
                         "policy --cdp http://127.0.0.1:9 --owner-tier off --timeout 60")

    def test_a_kept_value_from_a_failed_restore_is_the_one_put_back(self):
        self.follow.write_text("https://seven.sliccy.ai/#follow=x\n")
        (self.cs / "run").mkdir()
        (self.cs / "run" / "attach.policy.json").write_text('{"owner": {"tools": "agent"}}')
        p = self.run_script(wait=False)
        self.until(lambda: str(self.record().get("attach_tier", "")).startswith("on"))
        self.end_job("stopped")
        p.wait(60)
        self.assertEqual(self.nlog.read_text().splitlines()[-1],
                         "policy --cdp http://127.0.0.1:9 --owner-tier agent --timeout 60")

    def test_a_follower_without_view_changes_nothing(self):
        self.follow.write_text("https://seven.sliccy.ai/#follow=x\n")
        self.run_script(FAKE_FOLLOW='{"ok":false,"code":"no-view","error":"mcp-slicc\'s follower waits for View"}')
        rec = self.record()
        self.assertEqual(rec["attach_tier"], "off: mcp-slicc's follower waits for View")
        self.assertNotIn("owner_tier", rec)
        self.assertNotIn("policy", self.nlog.read_text())
        self.assertFalse((self.cs / "run" / "follower.pid").exists())


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SevenMjsUsage(unittest.TestCase):
    def run_mjs(self, *args) -> dict:
        p = subprocess.run(["node", str(REPO / "seven.mjs"), *args], capture_output=True, text=True, timeout=30)
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_unknown_commands_and_bad_answers_are_refused_before_anything_is_reached(self):
        out = self.run_mjs("nope")
        if "Node 22" in out.get("error", ""):
            self.skipTest(out["error"])
        self.assertIn("usage:", out["error"])
        self.assertIn("usage: seven.mjs decide", self.run_mjs("decide", "--cdp", "http://127.0.0.1:1", "apr-1",
                                                              "always")["error"])
        self.assertIn("--owner-tier must be", self.run_mjs("policy", "--cdp", "http://127.0.0.1:1")["error"])
        self.assertIn("--follow-file is required", self.run_mjs("follow", "--cdp", "http://127.0.0.1:1")["error"])


if __name__ == "__main__":
    unittest.main()
