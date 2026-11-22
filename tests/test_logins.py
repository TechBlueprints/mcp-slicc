"""Logins on seven (2026-11-21, README "Logins on seven"): a broker's job with logins, the unit's SLICC_LOGINS, the gate's
credential-command rules and its rules for a job whose browser is signed in, and seven.mjs secret's policy files. Run
with the others: python3 -m unittest discover -s tests -t tests"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

from test_forward import SystemdHost
from test_claude_slicc import HERE

import common as C
import slicc_gate as G
import slicc_mcp as M


class JobsWithLogins(SystemdHost):
    def setUp(self):
        super().setUp()
        os.environ["CLAUDE_SLICC_SLICC"] = "7"
        self.addCleanup(os.environ.pop, "CLAUDE_SLICC_SLICC", None)
        C.SLICC = "7"
        self.addCleanup(setattr, C, "SLICC", "")

    def test_the_logins_are_recorded_and_handed_to_the_unit(self):
        out = M.t_task({"instruction": "Read my GitHub profile.", "logins": ["github-ziggy:token", "google-ziggy:session"]})
        rec = C.job_record()
        self.assertEqual(rec["logins"], [{"service": "github-ziggy", "route": "token"},
                                         {"service": "google-ziggy", "route": "session"}])
        env = (C.HOME / "run" / "slicc.env").read_text()
        self.assertIn('SLICC_LOGINS="github-ziggy:token,google-ziggy:session"\n', env)
        self.assertTrue(C.has_session(rec))
        self.assertEqual(M.t_status({})["logins"], rec["logins"])
        M.t_stop({"job": out["job"]})

    def test_a_job_without_logins_names_none(self):
        out = M.t_task({"instruction": "Find the docs."})
        self.assertNotIn("SLICC_LOGINS", (C.HOME / "run" / "slicc.env").read_text())
        self.assertNotIn("logins", C.job_record())
        self.assertFalse(C.has_session(C.job_record()))
        M.t_stop({"job": out["job"]})

    def test_bad_logins_and_the_wrong_host_are_refused(self):
        for bad in (["github-ziggy"], ["github-ziggy:cookie"], ["GitHub:token"], ["a:token", "a:session"]):
            with self.subTest(bad=bad), self.assertRaises(M.Err):
                M.t_task({"instruction": "x", "logins": bad})
        self.assertIsNone(C.job_record())
        C.APPROVAL = "operator"
        with self.assertRaises(M.Err) as e:
            M.t_task({"instruction": "x", "logins": ["github-ziggy:token"]})
        self.assertIn("approval external", str(e.exception))
        C.APPROVAL = "external"
        with self.assertRaises(M.Err):
            M.t_task({"instruction": "x", "launch": "attach", "logins": ["github-ziggy:token"]})
        C.SLICC = "6"
        with self.assertRaises(M.Err):
            M.t_task({"instruction": "x", "logins": ["github-ziggy:token"]})


class TheGateOnCredentials(unittest.TestCase):
    def setUp(self):
        G.POLICY = G.load_policy()

    def v(self, cmd: str, session: bool = False) -> str:
        return G.slicc7_classify("bash", {"command": cmd}, session=session)["decision"]

    def test_a_placeholder_is_not_a_secret(self):
        self.assertEqual(self.v('curl -s https://api.github.com/user -H "Authorization: Bearer '
                                'fnox_proxy_eb1f8195cfcbfd5926fbd8f8"'), "ask")
        self.assertEqual(self.v('curl -s https://api.github.com/user -H "Authorization: Bearer $GITHUB_TOKEN"'), "ask")
        self.assertEqual(self.v("echo $GITHUB_TOKEN"), "auto")
        self.assertEqual(self.v('curl -H "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123" https://x'), "stop")

    def test_credential_commands_only_read(self):
        for cmd in ("jerk list", "jerk github-ziggy get", "jerk github-ziggy get --json", "jerk github list",
                    "sealed list --json", "sealed placeholder GITHUB_TOKEN", "fnox get GITHUB_TOKEN", "fnox list",
                    "secret list"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.v(cmd), "auto")
        for cmd in ("jerk github get --reveal", "jerk github get --refresh", "jerk github create", "jerk tab list",
                    "jerk tab export --url mail.example.com", "jerk config show github", "jerk mcp get",
                    "sealed ask GITHUB_TOKEN --reveal", "fnox set X", "fnox env", "fnox exec -- curl x",
                    "secret add X", "oauth-login github"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.v(cmd), "stop")

    def test_a_signed_in_job_stops_page_scripts_and_cookie_commands(self):
        for cmd in ('playwright-cli eval "document.cookie"', "playwright-cli cookie-list", "playwright-cli state-save x",
                    "playwright-cli run-code x", "node x.js", "python3 -c 1", "jsh x.js", "curlwright https://x"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.v(cmd, session=True), "stop")
        for cmd, session, want in (("playwright-cli snapshot", True, "auto"),
                                   ("playwright-cli open https://myaccount.google.com", True, "auto"),
                                   ("playwright-cli click e12", True, "ask"), ("playwright-cli fill e3 hello", True, "ask"),
                                   ('playwright-cli eval "1"', False, "ask"), ("node x.js", False, "ask")):
            with self.subTest(cmd=cmd, session=session):
                self.assertEqual(self.v(cmd, session=session), want)

    def test_classify_step_tells_the_gate_about_a_session(self):
        job = {"id": "job-5e7e0000aaaa", "slicc": "7", "logins": [{"service": "google-ziggy", "route": "session"}]}
        self.assertEqual(C.classify_step(job, "bash", {"command": "node x.js"})["decision"], "stop")
        job["logins"] = [{"service": "github-ziggy", "route": "token"}]
        self.assertEqual(C.classify_step(job, "bash", {"command": "node x.js"})["decision"], "ask")

    def test_a_gate_without_the_session_rules_stops_a_signed_in_job(self):
        class OldGate:
            @staticmethod
            def slicc7_classify(tool, args):
                return {"decision": "auto", "reason": "", "detail": tool}
        orig = C.gate
        C.gate = lambda: OldGate
        try:
            self.assertEqual(C.classify("bash", {"command": "ls"}, seven=True, session=True)["decision"], "stop")
            self.assertEqual(C.classify("bash", {"command": "ls"}, seven=True)["decision"], "auto")
        finally:
            C.gate = orig


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SecretFiles(unittest.TestCase):
    def files(self, spec: dict) -> dict:
        js = ("const m = await import(process.argv[1]); "
              "process.stdout.write(JSON.stringify(m.secretFiles(JSON.parse(process.argv[2]))));")
        mod = HERE.parent / "seven_secret.mjs"
        p = subprocess.run(["node", "--input-type=module", "-e", js, str(mod), json.dumps(spec)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_the_policy_files(self):
        f = self.files({"GITHUB_TOKEN": {"placeholder": "fnox_proxy_eb1f8195cfcbfd5926fbd8f8",
                                         "domains": ["api.github.com"], "header": "authorization",
                                         "methods": ["GET"], "paths": []}})
        self.assertIn('GITHUB_TOKEN = { provider = "seven", value = "fnox_proxy_eb1f8195cfcbfd5926fbd8f8" }', f["fnox"])
        self.assertIn('[[proxy.rules]]\nsecret = "GITHUB_TOKEN"\ndomain = "api.github.com"\nheader = "authorization"\n'
                      'methods = ["GET"]\n', f["fnox"])
        self.assertNotIn("paths", f["fnox"])
        self.assertEqual(f["policy"], '[exits]\nGITHUB_TOKEN = ["page"]\n\n[reveal]\nagent = false\n')
        self.assertIn("export GITHUB_TOKEN=fnox_proxy_eb1f8195cfcbfd5926fbd8f8\n", f["profile"])
