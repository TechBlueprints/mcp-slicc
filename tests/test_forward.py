"""Tests for the backends (2026-11-14): the forward brain against a fake upstream agent, the operator CLI, approvals
set to external, the systemd host and the state settings. Run with the others:
python3 -m unittest discover -s tests -t tests

The upstream is tests/fake_upstream.py on a Unix socket (or a TCP port), so nothing here reaches a real agent, and
systemctl is a stand-in script. Backend local is what every other test file runs.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fake_upstream import FakeUpstream, final, step
os.environ.setdefault("CLAUDE_SLICC_SLICC", "6")   # six's tests; test_seven.py has seven's

from test_claude_slicc import HERE, TOOLS_BODY, Base, main_body, start_job

import common as C
import endpoint as E
import forward as F
import gate_hook as G
import slicc_mcp as M

OP = HERE.parent / "slicc_op.py"
SETTINGS = ("BACKEND", "FORWARD_URL", "FORWARD_TOKEN", "FORWARD_TOKEN_FILE", "FORWARD_TIMEOUT", "FORWARD_ASKS",
            "HOST_KIND", "SYSTEMD_UNITS", "SYSTEMCTL", "STATE_MODE", "STATE_OWNER")


def short_dir() -> Path:
    """A folder for a Unix socket: its path must stay under about 100 characters."""
    d = Path(tempfile.mkdtemp(prefix="up"))
    if len(str(d)) > 80:
        d = Path(tempfile.mkdtemp(prefix="up", dir="/tmp"))
    return d


class SettingsBase(Base):
    def setUp(self):
        super().setUp()
        saved = {k: getattr(C, k) for k in SETTINGS}
        self.addCleanup(lambda: [setattr(C, k, v) for k, v in saved.items()])
        for k in SETTINGS:
            setattr(C, k, "")
        saved_tries = F.CONNECT_TRIES
        self.addCleanup(setattr, F, "CONNECT_TRIES", saved_tries)

    def wait_for(self, cond, timeout: float = 10.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for the condition")

    def rows(self, jid: str, event: str) -> list:
        return [r for r in C.read_jsonl(C.job_dir(jid) / "calls.jsonl") if r.get("event") == event]


class ForwardBase(SettingsBase):
    def setUp(self):
        super().setUp()
        self.sock_dir = short_dir()
        self.up = FakeUpstream(str(self.sock_dir / "up.sock"))
        self.addCleanup(self.up.close)
        C.BACKEND = "forward"
        C.FORWARD_URL = self.up.url
        C.FORWARD_TOKEN = "up-token"
        C.APPROVAL = "external"

    def auto_job(self) -> str:
        return M.t_task({"instruction": "List the repos on example.org and report."})["job"]

    def call(self, body=None) -> tuple:
        """One SLICC call in a thread: (thread, box with 'r' once it returns, the call's cancel event)."""
        box: dict = {}
        cancel = threading.Event()
        th = threading.Thread(target=lambda: box.setdefault("r", E.on_call(body or main_body(), cancel)), daemon=True)
        th.start()
        return th, box, cancel

    def server(self) -> int:
        srv = E.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def post(self, port: int, raw: bytes, key: str = None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=raw, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {key or C.token()}"})
        return urllib.request.urlopen(req, timeout=30)


class Answers(ForwardBase):
    def test_a_step_goes_to_slicc_and_the_call_reaches_the_upstream_as_it_came(self):
        jid = self.auto_job()
        self.up.script = [step("ls /workspace")]
        port = self.server()
        raw = json.dumps(main_body(), separators=(", ", ":  ")).encode()   # an unusual layout, kept byte for byte
        msg = json.loads(self.post(port, raw).read())["choices"][0]["message"]
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"command": "ls /workspace"})
        req = self.up.chat_requests()[0]
        self.assertEqual(req["raw"], raw)
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"]["Authorization"], "Bearer up-token")
        self.assertEqual(req["headers"]["X-Slicc-Job"], jid)
        self.assertEqual(req["headers"]["X-Slicc-Conversation"], "job")
        self.assertRegex(req["headers"]["X-Slicc-Call"], r"^fwd-[0-9a-f]{12}$")
        step_row = self.rows(jid, "step")[0]
        self.assertEqual((step_row["decision"], step_row["approved_by"], step_row["gate"]), ("upstream", "upstream", "auto"))
        self.assertEqual(self.rows(jid, "call")[0]["call"], req["headers"]["X-Slicc-Call"])
        self.assertEqual(C.current_job()["id"], jid)                  # a step is not the end of the job

    def test_a_streamed_answer_and_a_streamed_reply_to_slicc(self):
        self.auto_job()
        self.up.script = [{**step("ls /workspace"), "sse": True}]
        port = self.server()
        resp = self.post(port, json.dumps({**main_body(), "stream": True}).encode())
        self.assertIn("text/event-stream", resp.headers.get("Content-Type"))
        events = [json.loads(ln[6:]) for ln in resp.read().decode().splitlines()
                  if ln.startswith("data: ") and ln != "data: [DONE]"]
        calls = [tc for ev in events for ch in ev["choices"] for tc in ch["delta"].get("tool_calls") or []]
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"command": "ls /workspace"})
        self.assertTrue(json.loads(self.up.chat_requests()[0]["raw"])["stream"])

    def test_an_answer_without_a_step_is_the_jobs_report(self):
        jid = self.auto_job()
        self.up.script = [{**final("Found 3 repos."), "sse": True}]
        r = E.on_call(main_body())
        self.assertEqual(r, {"content": "Found 3 repos.", "tool_calls": []})
        self.assertEqual(C.job_record()["status"], "done")
        w = M.t_wait({"job": jid, "seconds": 1})
        self.assertEqual((w["status"], w["failed"], w["report"]), ("done", False, "Found 3 repos."))
        self.assertEqual(w["model_calls"], 1)

    def test_the_upstream_can_mark_its_report_failed(self):
        jid = self.auto_job()
        self.up.script = [{**final("The turn failed."), "headers": {"X-Slicc-Report": "failed"}}]
        E.on_call(main_body())
        self.assertTrue(C.read_json(C.job_dir(jid) / "report.json")["failed"])
        self.assertEqual(C.job_record()["status"], "failed")

    def test_a_side_call_is_forwarded_and_an_empty_answer_is_not_a_report(self):
        jid = self.auto_job()
        r = E.on_call({"messages": [{"role": "user", "content": "Give this chat a title"}], "tools": []})
        self.assertEqual(r, {"content": "", "tool_calls": []})
        self.assertEqual(self.up.chat_requests()[0]["headers"]["X-Slicc-Conversation"], "other")
        self.assertEqual(C.current_job()["id"], jid)
        self.assertEqual(self.rows(jid, "side_call")[0]["action"], "final")

    def test_with_no_job_the_call_is_forwarded_without_a_job_id(self):
        self.up.script = [final("No job is running here.")]
        r = E.on_call(main_body())
        self.assertEqual(r["content"], "No job is running here.")
        headers = self.up.chat_requests()[0]["headers"]
        self.assertNotIn("X-Slicc-Job", headers)
        self.assertNotIn("X-Slicc-Conversation", headers)
        self.assertTrue(C.read_jsonl(C.HOME / "side_calls.jsonl"))
        self.assertIsNone(C.job_record())                             # nothing is started: no chat jobs here

    def test_a_tcp_upstream_works_the_same(self):
        up = FakeUpstream()
        self.addCleanup(up.close)
        C.FORWARD_URL = up.url
        self.auto_job()
        up.script = [step("ls /workspace")]
        r = E.on_call(main_body())
        self.assertEqual(r["tool_calls"][0]["function"]["name"], "bash")
        self.assertEqual(up.chat_requests()[0]["path"], "/v1/chat/completions")

    def test_the_models_are_the_upstreams_with_the_endpoints_version(self):
        port = self.server()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                     headers={"Authorization": f"Bearer {C.token()}"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())["data"]
        self.assertEqual([(m["id"], m["version"]) for m in data], [("upstream-agent", C.code_version())])
        self.up.close()
        C.FORWARD_URL = f"unix:{self.sock_dir / 'gone.sock'}"
        F.CONNECT_TRIES = 1
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())["data"]
        self.assertEqual([(m["id"], m["version"]) for m in data], [("claude-slicc", C.code_version())])


class HardStops(ForwardBase):
    def test_a_step_the_gate_never_allows_is_not_handed_over_and_the_job_ends(self):
        jid = self.auto_job()
        self.up.script = [step("npm install left-pad")]
        r = E.on_call(main_body())
        self.assertEqual(r["tool_calls"], [])
        self.assertIn("hard stop", r["content"])
        report = C.read_json(C.job_dir(jid) / "report.json")
        self.assertTrue(report["failed"])
        self.assertEqual(C.job_record()["status"], "failed")
        self.assertEqual(self.rows(jid, "step")[0]["decision"], "stop")
        self.assertEqual(M.t_status({})["refused"], 1)

    def test_a_step_the_gate_would_ask_about_is_the_upstreams_to_decide(self):
        jid = self.auto_job()
        self.up.script = [step("curl https://example.com")]
        r = E.on_call(main_body())
        self.assertEqual(json.loads(r["tool_calls"][0]["function"]["arguments"]), {"command": "curl https://example.com"})
        self.assertEqual(list((C.job_dir(jid) / "asks").glob("*.json")), [])     # never asked here
        self.assertEqual(self.rows(jid, "step")[0]["gate"], "ask")

    def test_a_step_whose_arguments_cannot_be_read_is_refused(self):
        self.auto_job()
        self.up.script = [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{not json"}}]}}]
        r = E.on_call(main_body())
        self.assertEqual(r["tool_calls"], [])
        self.assertIn("not a JSON object", r["content"])

    def test_a_refused_step_outside_the_job_ends_nothing(self):
        jid = self.auto_job()
        self.up.script = [step("sudo ls")]
        r = E.on_call({**main_body(), "messages": [{"role": "user", "content": "[x] User: hello"}]})
        self.assertEqual(r["tool_calls"], [])
        self.assertEqual(C.current_job()["id"], jid)
        self.assertTrue(self.rows(jid, "hard_stop"))


class HeldCalls(ForwardBase):
    def test_a_call_is_held_until_the_upstream_answers(self):
        jid = self.auto_job()
        self.up.script = [{**step("ls /workspace"), "hold": 1.5}]
        th, box, _ = self.call()
        th.join(0.8)
        self.assertNotIn("r", box)                               # still held
        th.join(10)
        self.assertEqual(box["r"]["tool_calls"][0]["function"]["name"], "bash")
        self.assertGreaterEqual(self.rows(jid, "call")[0]["duration_ms"], 1400)

    def test_slicc_hanging_up_cancels_the_upstream_call(self):
        jid = self.auto_job()
        self.up.script = [{"hold_until_gone": True}]
        port = self.server()
        body = json.dumps(main_body()).encode()
        sock = socket.create_connection(("127.0.0.1", port))
        sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                     b"Authorization: Bearer " + C.token().encode() + b"\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\n\r\n" + body)
        self.assertTrue(self.up.received.wait(10))
        sock.close()                                             # SLICC gives up
        self.assertTrue(self.up.gone.wait(10))                   # and the upstream hears it
        self.wait_for(lambda: self.rows(jid, "call_dropped"))
        self.assertEqual(self.rows(jid, "call_dropped")[0]["upstream"], "cancelled")
        self.assertEqual(C.current_job()["id"], jid)             # the job goes on: SLICC's next call is forwarded
        self.assertIsNone(C.read_json(C.job_dir(jid) / "report.json"))

    def test_a_cancelled_call_hands_nothing_over(self):
        self.auto_job()
        self.up.script = [{"hold_until_gone": True}]
        th, box, cancel = self.call()
        self.assertTrue(self.up.received.wait(10))
        cancel.set()
        th.join(10)
        self.assertIsNone(box["r"])
        self.assertTrue(self.up.gone.wait(10))

    def test_stopping_the_job_cancels_the_held_call(self):
        jid = self.auto_job()
        self.up.script = [{"hold_until_gone": True}]
        th, box, _ = self.call()
        self.assertTrue(self.up.received.wait(10))
        M.t_stop({"job": jid})
        th.join(10)
        self.assertEqual(box["r"]["content"], "The job was stopped. Stop here.")
        self.assertTrue(self.up.gone.wait(10))
        self.assertEqual(C.job_record()["status"], "stopped")

    def test_an_upstream_that_never_answers_times_out_and_the_job_ends(self):
        jid = self.auto_job()
        C.FORWARD_TIMEOUT = "1"
        self.up.script = [{"hold_until_gone": True}]
        r = E.on_call(main_body())
        self.assertIn("did not answer within 1 s", r["content"])
        self.assertTrue(C.read_json(C.job_dir(jid) / "report.json")["failed"])
        self.assertTrue(self.up.gone.wait(10))


class UpstreamDown(ForwardBase):
    def test_an_upstream_that_cannot_be_reached_ends_the_job_with_a_failure_report(self):
        jid = self.auto_job()
        self.up.close()
        C.FORWARD_URL = f"unix:{self.sock_dir / 'nobody.sock'}"
        F.CONNECT_TRIES = 2
        r = E.on_call(main_body())
        self.assertIn("could not be reached", r["content"])
        report = C.read_json(C.job_dir(jid) / "report.json")
        self.assertTrue(report["failed"])
        self.assertIn("nobody.sock", report["text"])
        self.assertEqual(C.job_record()["status"], "failed")
        self.assertEqual(self.rows(jid, "call")[0]["action"], "error")

    def test_an_upstream_that_is_starting_gets_another_try(self):
        self.auto_job()
        path = self.sock_dir / "late.sock"
        C.FORWARD_URL = f"unix:{path}"
        late = {}

        def start_late():
            time.sleep(0.5)
            late["up"] = FakeUpstream(str(path))
            late["up"].script = [step("ls /workspace")]
        threading.Thread(target=start_late, daemon=True).start()
        r = E.on_call(main_body())
        self.addCleanup(lambda: late["up"].close())
        self.assertEqual(r["tool_calls"][0]["function"]["name"], "bash")

    def test_an_http_error_ends_the_job_and_names_the_status(self):
        jid = self.auto_job()
        self.up.script = [{"status": 500, "body": "the gateway is restarting"}]
        r = E.on_call(main_body())
        self.assertIn("HTTP 500", r["content"])
        self.assertIn("HTTP 500", C.read_json(C.job_dir(jid) / "report.json")["text"])

    def test_with_no_job_a_failed_call_says_so_and_ends_nothing(self):
        self.up.close()
        C.FORWARD_URL = f"unix:{self.sock_dir / 'nobody.sock'}"
        F.CONNECT_TRIES = 1
        r = E.on_call(main_body())
        self.assertIn("could not answer", r["content"])
        self.assertIsNone(C.job_record())


class Tokens(ForwardBase):
    def test_slicc_with_a_wrong_key_reaches_nothing(self):
        self.auto_job()
        port = self.server()
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post(port, json.dumps(main_body()).encode(), key="wrong")
        self.assertEqual(e.exception.code, 401)
        self.assertEqual(self.up.chat_requests(), [])

    def test_an_upstream_that_refuses_mcp_sliccs_token_ends_the_job(self):
        jid = self.auto_job()
        C.FORWARD_TOKEN = "not-the-upstreams"
        r = E.on_call(main_body())
        self.assertIn("refused mcp-slicc's token (HTTP 401)", r["content"])
        self.assertTrue(C.read_json(C.job_dir(jid) / "report.json")["failed"])

    def test_the_token_comes_from_a_file_and_an_unreadable_file_sends_nothing(self):
        jid = self.auto_job()
        C.FORWARD_TOKEN = ""
        tok = self.tmp / "forward-token"
        tok.write_text("up-token\n")
        C.write_json(C.HOME / "config.json", {"forward_token_file": str(tok)})
        self.up.script = [step("ls /workspace")]
        self.assertEqual(E.on_call(main_body())["tool_calls"][0]["function"]["name"], "bash")
        tok.unlink()
        r = E.on_call(main_body())
        self.assertIn("token file cannot be read", r["content"])
        self.assertEqual(len(self.up.chat_requests()), 1)
        self.assertEqual(C.job_record()["status"], "failed")
        self.assertEqual(C.job_record()["id"], jid)


class UpstreamQuestions(ForwardBase):
    def setUp(self):
        super().setUp()
        self.asks = self.tmp / "upstream-asks"
        C.FORWARD_ASKS = str(self.asks / "{job}")

    def put_ask(self, jid: str, aid: str = "ma-0123456789ab", job: str = None) -> None:
        C.write_json(self.asks / jid / f"{aid}.json", {"id": aid, "job": job or jid, "ts": C.now_iso(),
                                                       "tool": "bash", "detail": "curl https://example.com",
                                                       "reason": "needs a person: curl"})

    def test_a_question_of_the_upstream_is_shown_and_answered_into_the_jobs_decisions(self):
        jid = self.auto_job()
        self.put_ask(jid)
        self.put_ask(jid, "ma-ffffffffffff", job="job-000000000000")      # another job's: not this one's
        w = M.t_wait({"job": jid, "seconds": 1})
        self.assertEqual(w["status"], "question")
        self.assertEqual([(q["id"], q["from"], q["wants"]) for q in w["questions"]],
                         [("ma-0123456789ab", "upstream", "curl https://example.com")])
        M.t_decide({"ask": "ma-0123456789ab", "answer": "allow", "ref": "approval-7"})
        d = C.read_json(C.job_dir(jid) / "decisions" / "ma-0123456789ab.json")
        self.assertEqual((d["answer"], d["by"], d["ref"]), ("allow", "external", "approval-7"))
        self.assertEqual(M.t_wait({"job": jid, "seconds": 1})["status"], "working")
        with self.assertRaises(M.Err):
            M.t_decide({"ask": "ma-0123456789ab", "answer": "deny"})

    def test_stop_denies_the_upstreams_open_questions_and_an_ended_job_has_none(self):
        jid = self.auto_job()
        self.put_ask(jid, "sa-0123456789ab")
        M.t_stop({"job": jid})
        self.assertEqual(C.read_json(C.job_dir(jid) / "decisions" / "sa-0123456789ab.json")["answer"], "deny")
        jid2 = self.auto_job()
        self.put_ask(jid2)
        M.t_stop({"job": jid2})
        (C.job_dir(jid2) / "decisions" / "ma-0123456789ab.json").unlink()
        self.assertEqual(C.open_asks(jid2), [])                          # the job is not running

    def test_removing_the_file_withdraws_the_question(self):
        jid = self.auto_job()
        self.put_ask(jid)
        self.assertEqual(len(C.open_asks(jid)), 1)
        (self.asks / jid / "ma-0123456789ab.json").unlink()
        self.assertEqual(C.open_asks(jid), [])


class Operator(ForwardBase):
    def test_the_job_record_says_how_it_was_started(self):
        jid = M.t_task({"instruction": "Find the docs.", "ref": "approval-1"})["job"]
        rec = C.job_record()
        self.assertEqual((rec["id"], rec["backend"], rec["approval"], rec["ref"], rec["status"]),
                         (jid, "forward", "external", "approval-1", "running"))
        st = M.t_status({})
        self.assertEqual((st["backend"], st["host"], st["approval"]), ("forward", "macos", "external"))

    def test_a_manual_job_and_a_missing_upstream_are_refused(self):
        with self.assertRaises(M.Err) as e:
            M.t_task({"instruction": "x", "mode": "manual"})
        self.assertIn("needs backend local", str(e.exception))
        C.FORWARD_URL = ""
        with self.assertRaises(M.Err):
            M.t_task({"instruction": "x"})
        self.assertIsNone(C.job_record())

    def test_a_note_goes_to_the_jobs_notes_for_the_upstream(self):
        jid = self.auto_job()
        out = M.t_tell({"job": jid, "note": "Only public repos.", "ref": "approval-2"})
        self.assertIn("upstream", out["note"])
        self.assertEqual([(r["text"], r["ref"]) for r in C.read_jsonl(C.job_dir(jid) / "steer.jsonl")],
                         [("Only public repos.", "approval-2")])

    def test_an_unknown_backend_answers_nothing_and_starts_nothing(self):
        C.BACKEND = "forwrad"
        r = E.on_call(main_body())
        self.assertIn("not one of local, forward", r["content"])
        self.assertEqual(self.up.chat_requests(), [])
        with self.assertRaises(M.Err):
            M.t_task({"instruction": "x"})


class ExternalApprovals(SettingsBase):
    def hook(self, tool: str, args: dict):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            G.decide({"tool_name": "mcp__slicc__" + tool, "tool_input": args})
        out = buf.getvalue().strip()
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None

    def test_through_claude_code_the_hook_asks_a_person(self):
        C.APPROVAL = "external"
        self.assertEqual(C.approval(), "external")
        self.assertEqual(self.hook("slicc_task", {"instruction": "x"}), "ask")
        self.assertEqual(self.hook("slicc_status", {}), "allow")

    def test_a_gated_manual_step_runs_once_the_caller_checked_it(self):
        C.APPROVAL = "external"
        C.STEP_WAIT_S = 1
        jid = M.t_task({"instruction": "List the files.", "mode": "manual"})["job"]
        out = M.t_bash({"command": "curl https://example.com"})
        self.assertEqual(out["status"], "working")                 # queued for SLICC, not a question
        self.assertIn("approval: external", out["gate"])
        self.assertEqual([s["status"] for s in C.read_steps(jid)], ["queued"])

    def test_local_is_the_default_and_behaves_as_before(self):
        self.assertEqual((C.backend(), C.host_kind(), C.state_mode(), C.state_owner()), ("local", "macos", None, ""))
        start_job()
        r = E.on_call({"messages": [{"role": "user", "content": "Summarize this chat"}], "tools": []})
        self.assertEqual(r, {"content": "", "tool_calls": []})
        self.assertEqual(self.fake_calls(), [])


class SystemdHost(SettingsBase):
    def setUp(self):
        super().setUp()
        self.ctl_log = self.tmp / "systemctl.log"
        fake = self.tmp / "fake-systemctl"
        fake.write_text('#!/bin/bash\necho "$*" >> "$FAKE_SYSTEMCTL_LOG"\n'
                        '[ "$1" = is-active ] && echo active\n'
                        '[ -n "${FAKE_SYSTEMCTL_FAIL:-}" ] && [ "$1" = restart ] && { echo "unit failed" >&2; exit 1; }\n'
                        'exit 0\n')
        fake.chmod(0o755)
        os.environ["FAKE_SYSTEMCTL_LOG"] = str(self.ctl_log)
        self.addCleanup(os.environ.pop, "FAKE_SYSTEMCTL_LOG", None)
        self.addCleanup(os.environ.pop, "FAKE_SYSTEMCTL_FAIL", None)
        C.write_json(C.HOME / "config.json", {"host": "systemd", "systemd_units": ["slicc-worker.service"],
                                              "systemctl": [str(fake)]})
        C.APPROVAL = "external"

    def ctl(self) -> list:
        return self.ctl_log.read_text().splitlines() if self.ctl_log.exists() else []

    def test_a_job_restarts_the_units_and_stop_stops_them(self):
        out = M.t_task({"instruction": "Find the docs."})
        jid = out["job"]
        self.assertEqual(out["slicc"], "started")
        self.assertEqual(self.ctl(), ["restart slicc-worker.service"])
        self.assertEqual((C.HOME / "run" / "slicc.job").read_text().strip(), jid)
        self.assertIn(f"SLICC_JOB={jid}\n", (C.HOME / "run" / "slicc.env").read_text())
        self.assertEqual(M.t_status({})["slicc"], "active")
        self.assertEqual(M.t_stop({"job": jid})["slicc"], "stopped")
        self.assertEqual(self.ctl()[-1], "stop slicc-worker.service")

    def test_a_unit_that_does_not_start_fails_the_job(self):
        os.environ["FAKE_SYSTEMCTL_FAIL"] = "1"
        with self.assertRaises(M.Err) as e:
            M.t_task({"instruction": "Find the docs."})
        self.assertIn("unit failed", str(e.exception))
        rec = C.job_record()
        self.assertEqual(rec["status"], "failed")
        self.assertTrue(C.read_json(C.job_dir(rec["id"]) / "report.json")["failed"])
        self.assertIsNone(C.current_job())                         # the next job can start

    def test_an_attached_job_starts_no_unit_and_no_units_is_an_error(self):
        M.t_task({"instruction": "x", "launch": "attach"})
        self.assertEqual(self.ctl(), [])
        M.t_stop({"job": C.job_record()["id"]})
        C.write_json(C.HOME / "config.json", {"host": "systemd"})
        with self.assertRaises(M.Err) as e:
            M.t_task({"instruction": "x"})
        self.assertIn("systemd_units", str(e.exception))


class StateSettings(SettingsBase):
    def test_state_mode_makes_the_job_readable_by_another_user(self):
        C.STATE_MODE = "0644"
        jid = M.t_task({"instruction": "x", "mode": "manual"})["job"]
        M.t_tell({"job": jid, "note": "a note"})
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)   # noqa: E731
        self.assertEqual(mode(C.HOME / "state" / "job.json"), 0o644)
        self.assertEqual(mode(C.job_dir(jid) / "steer.jsonl"), 0o644)
        self.assertEqual(mode(C.job_dir(jid) / "decisions"), 0o755)
        C.token()
        self.assertEqual(mode(C.HOME / "token"), 0o600)             # the endpoint's key stays private

    def test_without_state_mode_records_stay_private(self):
        start = M.t_task({"instruction": "x", "mode": "manual"})["job"]
        self.assertEqual(stat.S_IMODE(os.stat(C.HOME / "state" / "job.json").st_mode), 0o600)
        self.assertTrue(C.job_dir(start).is_dir())

    def test_state_owner_owns_what_a_root_operator_writes(self):
        me = pwd.getpwuid(os.getuid()).pw_name
        C.STATE_OWNER = me
        chowned = []
        real_chown, real_euid = os.chown, os.geteuid
        self.addCleanup(setattr, os, "chown", real_chown)
        self.addCleanup(setattr, os, "geteuid", real_euid)
        os.chown = lambda p, u, g: (chowned.append((str(p), u)), real_chown(p, u, g))
        os.geteuid = lambda: 0                                       # as if run by a root broker
        jid = M.t_task({"instruction": "x", "mode": "manual"})["job"]
        M.t_stop({"job": jid})
        names = {Path(p).name for p, _ in chowned}
        self.assertTrue({jid, "asks", "decisions", "steps", "stop"} <= names, names)
        self.assertTrue(any(p.endswith("job.json.tmp") for p, _ in chowned))
        self.assertEqual({u for _, u in chowned}, {os.getuid()})


class OperatorCLI(SettingsBase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "op-home"
        fake = self.tmp / "fake-systemctl"
        fake.write_text('#!/bin/bash\necho "$*" >> "$FAKE_SYSTEMCTL_LOG"\n[ "$1" = is-active ] && echo active\nexit 0\n')
        fake.chmod(0o755)
        self.ctl_log = self.tmp / "systemctl.log"
        self.asks = self.tmp / "up-asks"
        C.write_json(self.home / "config.json", {
            "backend": "forward", "forward_url": "unix:/nonexistent/up.sock", "approval": "external",
            "host": "systemd", "systemd_units": ["slicc-worker.service"], "systemctl": [str(fake)],
            "forward_asks": str(self.asks / "{job}"), "state_mode": "0644"})
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_SLICC_")}
        self.env["CLAUDE_SLICC_SLICC"] = "6"
        self.env["FAKE_SYSTEMCTL_LOG"] = str(self.ctl_log)

    def op(self, *argv, stdin: str = None) -> tuple:
        p = subprocess.run([sys.executable, "-I", str(OP), "--home", str(self.home), *argv], capture_output=True,
                           text=True, timeout=60, env=self.env, input=stdin, cwd=str(self.tmp))
        return p.returncode, (json.loads(p.stdout) if p.stdout.strip() else None), p.stderr

    def test_the_commands_print_json_and_exit_by_the_result(self):
        self.assertEqual(self.op("status")[:2], (0, {"status": "no job yet"}))
        code, out, _ = self.op("task", "List the repos on example.org.", "--ref", "approval-1")
        self.assertEqual((code, out["status"], out["slicc"]), (0, "started", "started"))
        jid = out["job"]
        rec = json.loads((self.home / "state" / "job.json").read_text())
        self.assertEqual((rec["approval"], rec["backend"], rec["ref"]), ("external", "forward", "approval-1"))
        self.assertEqual(stat.S_IMODE(os.stat(self.home / "state" / "job.json").st_mode), 0o644)
        code, out, _ = self.op("task", "A second job.")
        self.assertEqual(code, 1)
        self.assertIn("already running", out["error"])
        self.assertEqual(self.op("tell", jid, "Only public repos.", "--ref", "approval-2")[:2][0], 0)
        code, out, _ = self.op("wait", jid, "--seconds", "1")
        self.assertEqual((code, out["status"]), (0, "working"))
        C.write_json(self.asks / jid / "ma-0123456789ab.json", {"id": "ma-0123456789ab", "job": jid, "ts": "t",
                                                                "tool": "bash", "detail": "curl https://example.com",
                                                                "reason": "gated"})
        code, out, _ = self.op("wait", jid, "--seconds", "1")
        self.assertEqual((out["status"], out["questions"][0]["id"]), ("question", "ma-0123456789ab"))
        code, out, _ = self.op("decide", "ma-0123456789ab", "allow", "--ref", "approval-3")
        self.assertEqual((code, out["answer"]), (0, "allow"))
        d = json.loads((self.home / "jobs" / jid / "decisions" / "ma-0123456789ab.json").read_text())
        self.assertEqual((d["by"], d["ref"]), ("external", "approval-3"))
        code, out, _ = self.op("status")
        self.assertEqual((out["job"], out["slicc"], out["backend"], out["host"]), (jid, "active", "forward", "systemd"))
        code, out, _ = self.op("stop", jid)
        self.assertEqual((code, out["status"], out["slicc"]), (0, "stopped", "stopped"))
        self.assertEqual(self.ctl_log.read_text().splitlines(),
                         ["restart slicc-worker.service", "is-active slicc-worker.service",
                          "stop slicc-worker.service"])

    def test_manual_steps_and_errors(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg.update(backend="local", host="macos")
        C.write_json(self.home / "config.json", cfg)
        self.env["CLAUDE_SLICC_START_CMD"] = ""
        self.env["CLAUDE_SLICC_STEP_WAIT"] = "1"
        code, out, _ = self.op("task", "List the files.", "--mode", "manual")
        self.assertEqual(code, 0)
        code, out, _ = self.op("bash", "sudo ls", "--purpose", "a test")
        self.assertEqual((code, out["status"]), (0, "refused"))
        code, out, _ = self.op("write-file", "/tmp/a.txt", stdin="hello")
        self.assertEqual((code, out["status"]), (0, "working"))
        code, out, _ = self.op("edit", "/tmp/a.txt", "--edits", "not json")
        self.assertEqual((code, out), (1, {"error": "--edits is not JSON"}))
        self.assertEqual(self.op("decide", "ma-000000000000", "allow")[0], 1)
        self.assertEqual(self.op("frobnicate")[0], 2)                # a usage error
