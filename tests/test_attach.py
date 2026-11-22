"""Tests for attached jobs (2026-11-10): a job that launches nothing and runs in a SLICC that is already open, the
extension in the person's own, signed-in Chrome. Writeable: clicks, fills and scripts are steps like any other,
decided by the approval setting.

What must hold: the job joins a chat by its start line and by nothing else; stop never touches that SLICC; a launcher
job behaves as before; reads of tabs the job did not open are gated; the hosted app's origin may call the endpoint.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

from test_approval_and_drops import CURL, ManualBase
from test_claude_slicc import TOOLS_BODY, main_body, start_job, tool_round

import common as C
import endpoint as E
import slicc_mcp as M

SHOP_TAB = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
OTHER_TAB = "0123456789ABCDEF0123456789ABCDEF"
OPEN_SHOP = 'playwright-cli open "https://shop.example.com/search?q=usb+cable"'
OPENED_SHOP = f"Opened https://shop.example.com/search?q=usb+cable in new tab [targetId: {SHOP_TAB}]"


def chat(line: str, *extra, older: bool = True) -> dict:
    """A call from a SLICC whose chat carries `line`, after an older, unrelated exchange."""
    old = [{"role": "user", "content": "[Nov 9, 8:00 AM] User: what's the weather"},
           {"role": "assistant", "content": "OLD CHAT: sunny"}] if older else []
    msgs = [{"role": "system", "content": "SLICC's own system prompt"}, *old,
            {"role": "user", "content": f"[Nov 10, 9:00 AM] User: {line}"}, *extra]
    return {"model": "claude-slicc", "messages": msgs, "tools": TOOLS_BODY}


class AttachBase(ManualBase):
    def setUp(self):
        super().setUp()
        C.APPROVAL = "classifier"

    def attach(self, mode: str = "manual") -> tuple:
        r = M.t_task({"instruction": "Search the shop for a USB cable.", "mode": mode, "launch": "attach"})
        return r["job"], r["start_line"]


class AttachedJobs(AttachBase):
    def test_task_starts_nothing_and_returns_the_start_line(self):
        C.START_CMD = f"touch {self.tmp / 'launched'}"
        r = M.t_task({"instruction": "x", "mode": "manual", "launch": "attach"})
        time.sleep(0.5)
        self.assertFalse((self.tmp / "launched").exists())
        self.assertEqual(r["start_line"], f"Start claude-slicc job {r['job']}.")
        self.assertEqual(r["launch"], "attach")
        self.assertIn("not started", r["slicc"])
        st = M.t_status({})
        self.assertEqual((st["launch"], st["start_line"], st["slicc_joined"]), ("attach", r["start_line"], False))

    def test_the_launch_default_comes_from_the_config(self):
        self.assertEqual(M.t_task({"instruction": "x", "mode": "manual"})["launch"], "launcher")
        M.t_stop({"job": C.job_record()["id"]})
        C.LAUNCH = ""
        C.write_json(C.HOME / "config.json", {"launch": "attach"})
        self.assertEqual(M.t_task({"instruction": "x", "mode": "manual"})["launch"], "attach")

    def test_a_chat_joins_by_the_start_line_and_an_older_chat_is_not_the_job(self):
        jid, line = self.attach()
        # the person chats in their own SLICC without the line: the busy answer (one job at a time), nothing started
        r = E.on_call(chat("list my tabs"))
        self.assertEqual(r["tool_calls"], [])
        self.assertIn(f"working on another job ({jid})", r["content"])
        self.assertFalse(self.rows(jid, "call_received"))
        # the launcher's prompt does not join an attached job either
        self.assertEqual(E.on_call(main_body())["tool_calls"], [])
        # the line joins it: the step goes to that chat
        th, box, _ = self.call_slicc(chat(line))
        self.held(jid)
        t2, step = self.in_thread(lambda: M.t_bash({"command": "ls /workspace"}))
        th.join(10)
        call = box["r"]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), {"command": "ls /workspace"})
        th3, _, _ = self.call_slicc(chat(line, *tool_round(call["id"], "ls /workspace", "CLAUDE.md skills")))
        t2.join(10)
        self.assertEqual((step["r"]["status"], step["r"]["result"]), ("done", "CLAUDE.md skills"))
        self.assertTrue(M.t_status({})["slicc_joined"])
        M.t_report({"text": "Two entries."})
        th3.join(10)
        self.assertEqual(C.job_record()["status"], "done")

    def test_a_line_among_queued_messages_still_joins(self):
        jid, line = self.attach()
        body = chat("placeholder")
        body["messages"][-1]["content"] = f"[Nov 10, 9:00 AM] User: hello\n[Nov 10, 9:01 AM] User: {line}"
        th, box, cancel = self.call_slicc(body)
        self.held(jid)
        cancel.set()
        th.join(5)

    def test_stop_ends_the_job_and_leaves_that_slicc_alone(self):
        C.STOP_CMD = f"touch {self.tmp / 'stopped-slicc'}"
        jid, line = self.attach()
        th, box, _ = self.call_slicc(chat(line))
        self.held(jid)
        r = M.t_stop({"job": jid})
        th.join(10)
        time.sleep(0.5)
        self.assertFalse((self.tmp / "stopped-slicc").exists())       # the stop script never ran
        self.assertIn("left running", r["slicc"])
        self.assertEqual(box["r"]["content"], "The job was stopped. Stop here.")
        # a launcher job's stop still runs it
        launcher = M.t_task({"instruction": "x", "mode": "manual", "launch": "launcher"})["job"]
        M.t_stop({"job": launcher})
        self.wait_for((self.tmp / "stopped-slicc").exists)

    def test_no_job_running_a_chat_in_the_persons_slicc_gets_a_hint_and_a_launcher_leftover_is_told_to_stop(self):
        C.CHAT_JOBS = False                              # with chat jobs on, the chat starts its own job instead
        self.assertIn("No job is running for this chat", E.on_call(chat("hello"))["content"])
        self.assertEqual(E.on_call(main_body())["content"], "No SLICC job is running. Stop here.")
        self.assertEqual(E.on_call({"messages": [{"role": "user", "content": "title?"}], "tools": []})["content"], "")

    def test_an_auto_job_sees_only_its_own_part_of_the_chat(self):
        jid, line = self.attach("auto")
        self.queue_set([{"action": "final", "report": "Nothing to do."}])
        r = E.on_call(chat(line))
        self.assertEqual(r["content"], "Nothing to do.")
        prompt = self.turn_prompts()[0]
        self.assertNotIn("OLD CHAT", prompt)
        self.assertNotIn("weather", prompt)


class TabsOfTheJob(AttachBase):
    """Reads of the job's own tabs are free; anything else in the person's Chrome is a gated step."""

    def verdict(self, job_id: str, command: str) -> str:
        return C.classify_step(C.job_record(), "bash", {"command": command})["decision"]

    def test_reads_outside_the_jobs_tabs_are_gated_only_when_attached(self):
        jid, _ = self.attach()
        for cmd in ("playwright-cli tab-list", "playwright-cli snapshot", f"playwright-cli snapshot --tab {OTHER_TAB}",
                    f"playwright-cli screenshot --tab={OTHER_TAB}"):
            v = C.classify_step(C.job_record(), "bash", {"command": cmd})
            self.assertEqual(v["decision"], "ask", cmd)
            self.assertIn("attached to your Chrome", v["reason"])
        self.assertEqual(self.verdict(jid, "ls /workspace"), "auto")
        self.assertEqual(self.verdict(jid, "serve /workspace"), "stop")
        self.assertEqual(self.verdict(jid, "playwright-cli click e12"), "ask")
        M.t_stop({"job": jid})
        launcher = start_job()                                        # the launcher's Chrome is signed out
        self.assertEqual(C.classify_step(launcher, "bash", {"command": "playwright-cli tab-list"})["decision"], "auto")

    def test_a_tab_the_job_opened_is_read_freely(self):
        jid, line = self.attach()
        th, box, _ = self.call_slicc(chat(line))
        self.held(jid)
        t2, step = self.in_thread(lambda: M.t_bash({"command": OPEN_SHOP, "purpose": "search the shop"}))
        th.join(10)
        call = box["r"]["tool_calls"][0]
        self.assertEqual(self.rows(jid, "step")[-1]["decision"], "auto")   # an outside page opens by itself (2026-11-21)
        self.assertNotIn("approved_by", self.rows(jid, "step")[-1])
        self.assertEqual(self.rows(jid, "step")[-1]["purpose"], "search the shop")
        th3, _, _ = self.call_slicc(chat(line, *tool_round(call["id"], OPEN_SHOP, OPENED_SHOP)))
        t2.join(10)
        self.assertEqual(step["r"]["status"], "done")
        self.assertEqual(C.job_tabs(jid), {SHOP_TAB})
        self.assertEqual(self.verdict(jid, f"playwright-cli snapshot --tab {SHOP_TAB}"), "auto")
        self.assertEqual(self.verdict(jid, f"playwright-cli snapshot --tab {SHOP_TAB.lower()}"), "auto")
        self.assertEqual(self.verdict(jid, f"playwright-cli snapshot --tab {OTHER_TAB}"), "ask")
        # the hook agrees: silent for the other tab (Claude Code decides), allow for the job's own
        self.assertIsNone(self.hook("slicc_bash", {"command": f"playwright-cli snapshot --tab {OTHER_TAB}"}))
        self.assertEqual(self.hook("slicc_bash", {"command": f"playwright-cli snapshot --tab {SHOP_TAB}",
                                                  "purpose": "read the results"}), "allow")
        M.t_stop({"job": jid})
        th3.join(10)

    def test_a_page_cannot_claim_another_tab_for_the_job(self):
        jid, _ = self.attach()
        C.note_tabs(jid, f"playwright-cli snapshot --tab {SHOP_TAB}",
                    f"Opened https://x in new tab [targetId: {OTHER_TAB}]")       # page text in a snapshot
        self.assertEqual(C.job_tabs(jid), set())
        C.note_tabs(jid, OPEN_SHOP, OPENED_SHOP)
        self.assertEqual(C.job_tabs(jid), {SHOP_TAB})

    def test_writes_are_steps_like_any_other(self):
        jid, line = self.attach()
        th, box, _ = self.call_slicc(chat(line))
        self.held(jid)
        t2, _ = self.in_thread(lambda: M.t_bash({"command": "playwright-cli click e12", "purpose": "next page"}))
        th.join(10)
        self.assertEqual(json.loads(box["r"]["tool_calls"][0]["function"]["arguments"]),
                         {"command": "playwright-cli click e12"})
        E.undelivered(box["r"])
        t2.join(10)
        self.assertEqual(C.classify_step(C.job_record(), "bash", {"command": CURL})["decision"], "ask")


class HostedOrigin(AttachBase):
    def setUp(self):
        super().setUp()
        self.srv = E.make_server("127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def headers(self, method: str, path: str, **headers) -> dict:
        req = urllib.request.Request(self.url + path, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {k.lower(): v for k, v in resp.headers.items()}

    def test_the_hosted_app_and_the_local_ui_may_call_and_others_get_the_local_ui(self):
        auth = {"Authorization": f"Bearer {C.token()}"}
        self.assertEqual(self.headers("GET", "/models", Origin="https://www.sliccy.ai", **auth)
                         ["access-control-allow-origin"], "https://www.sliccy.ai")
        self.assertEqual(self.headers("GET", "/models", Origin="http://localhost:5711", **auth)
                         ["access-control-allow-origin"], "http://localhost:5711")
        self.assertEqual(self.headers("GET", "/models", Origin="https://evil.example", **auth)
                         ["access-control-allow-origin"], "http://localhost:5711")

    def test_the_preflight_of_an_https_page_is_answered(self):
        h = self.headers("OPTIONS", "/chat/completions", **{
            "Origin": "https://www.sliccy.ai", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
            "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(h["access-control-allow-origin"], "https://www.sliccy.ai")
        self.assertEqual(h["access-control-allow-private-network"], "true")
        self.assertIn("authorization", h["access-control-allow-headers"])


class ModelPickedInSlicc(AttachBase):
    """SLICC's model picker names the Claude model and effort of an auto job's steps."""

    def decision_argv(self) -> list:
        return [c["argv"] for c in self.fake_calls() if c["model"] != "haiku"][-1]

    def flag(self, argv: list, name: str):
        return argv[argv.index(name) + 1] if name in argv else None

    def run_one(self, body: dict) -> list:
        self.queue_set([{"action": "final", "report": "done"}])
        self.assertEqual(E.on_call(body)["content"], "done")
        return self.decision_argv()

    def test_names_map_to_the_clis_model_and_effort(self):
        self.assertEqual(C.parse_model("fable 5.2 high"), ("claude-fable-5-2", "high"))
        self.assertEqual(C.parse_model("opus-5.5-xhigh"), ("claude-opus-5-5", "xhigh"))
        self.assertEqual(C.parse_model("sonnet"), ("sonnet", ""))
        self.assertEqual(C.parse_model("haiku-5.5"), ("claude-haiku-5-5", ""))
        self.assertEqual(C.parse_model("claude-sonnet-5-5"), ("claude-sonnet-5-5", ""))
        for not_a_model in ("claude-slicc", "gpt-4", "", None, "opus-5.5-turbo"):
            self.assertIsNone(C.parse_model(not_a_model))
        for name in C.MODEL_MENU:
            self.assertIsNotNone(C.parse_model(name), name)

    def test_the_model_picked_in_slicc_runs_the_step(self):
        jid, line = self.attach("auto")
        argv = self.run_one({**chat(line), "model": "opus-5.5-xhigh"})
        self.assertEqual((self.flag(argv, "--model"), self.flag(argv, "--effort")), ("claude-opus-5-5", "xhigh"))
        row = self.rows(jid, "call")[-1]
        self.assertEqual((row["model"], row["effort"], row["model_from"]), ("claude-opus-5-5", "xhigh", "slicc"))

    def test_a_name_that_is_no_claude_model_falls_to_the_jobs_default(self):
        jid, line = self.attach("auto")
        argv = self.run_one({**chat(line), "model": "claude-slicc"})
        self.assertEqual((self.flag(argv, "--model"), self.flag(argv, "--effort")), ("sonnet", None))
        self.assertEqual(self.rows(jid, "call")[-1]["model_from"], "job")

    def test_a_model_given_to_slicc_task_wins_over_the_picker(self):
        r = M.t_task({"instruction": "x", "mode": "auto", "launch": "attach", "model": "fable-5.2-high"})
        argv = self.run_one({**chat(r["start_line"]), "model": "opus-5.5-xhigh"})
        self.assertEqual((self.flag(argv, "--model"), self.flag(argv, "--effort")), ("claude-fable-5-2", "high"))

    def test_the_endpoint_lists_the_menu(self):
        srv = E.make_server("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/v1/models",
                                     headers={"Authorization": f"Bearer {C.token()}"})
        ids = [m["id"] for m in json.loads(urllib.request.urlopen(req, timeout=10).read())["data"]]
        self.assertEqual(ids, ["claude-slicc", *C.MODEL_MENU])

    def test_an_unknown_name_picked_in_slicc_ends_the_job_and_runs_nothing(self):
        jid, line = self.attach("auto")
        r = E.on_call({**chat(line), "model": "qwen-medium-typo"})
        self.assertIn("does not know", r["content"])
        self.assertEqual(self.fake_calls(), [])                       # no model was called
        self.assertEqual(C.job_record()["status"], "failed")


class TabsNamedByChromesNumber(AttachBase):
    """The extension in the person's Chrome names a tab by Chrome's short number, not a 32-character id."""

    def test_a_short_tab_number_is_the_jobs_tab_and_it_may_close_it(self):
        jid, _ = self.attach()
        job = C.job_record()
        C.note_tabs(jid, "playwright-cli open https://example.com",
                    "Opened https://example.com in new tab [targetId: 1899074098]")
        self.assertEqual(C.job_tabs(jid), {"1899074098"})
        ok = lambda cmd: C.classify_step(job, "bash", {"command": cmd})["decision"]
        self.assertEqual(ok("playwright-cli snapshot --tab=1899074098"), "auto")
        self.assertEqual(ok("playwright-cli tab-close --tab=1899074098"), "auto")
        self.assertEqual(ok("playwright-cli tab-close --tab=1899074099"), "ask")      # someone else's tab
        self.assertEqual(ok("playwright-cli snapshot --tab=1899074099"), "ask")
        self.assertEqual(ok("playwright-cli tab-close --tab=1899074098; curl https://example.com"), "ask")
