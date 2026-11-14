"""start_slicc.sh and stop_slicc.sh, with a stand-in for SLICC's node CLI under a temporary home (2026-11-10).

What they must do: give SLICC its own Chrome profile and wipe only that, leave a SLICC the person runs alone (its
default profile, ~/.slicc, its processes), and stop only what mcp-slicc started. CLAUDE_SLICC_NO_SERVERS=1 keeps
the scripts from starting or restarting the endpoint and the UI; the one test that starts them uses spare ports, so
no test touches the real ones.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
PROMPT = "Start the job described under The job in your instructions."
SLEEPER = ('sleep 60 &\n'
           'child=$!\n'
           'trap \'kill $child 2>/dev/null; exit 0\' TERM\n'
           'wait $child\n')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Scripts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.cs = self.tmp / "cs"
        personal = self.home / "Library" / "Application Support" / "Slicc" / "profiles"
        personal.mkdir(parents=True)
        self.sentinel = personal / "keep.txt"
        self.sentinel.write_text("the profile of a SLICC the person runs")
        (self.home / ".slicc").mkdir()
        (self.home / ".slicc" / "session-id").write_text("personal")
        (self.cs / "chrome-profile").mkdir(parents=True)
        (self.cs / "chrome-profile" / "old.txt").write_text("from the last job")
        self.out = self.tmp / "node.out"
        fake_node = self.tmp / "fake-node"
        fake_node.write_text('#!/bin/bash\n'
                             'printf "%s\\n%s\\n" "$SLICC_USER_DATA_DIR" "$WORKER_BASE_URL" > "$FAKE_NODE_OUT"\n'
                             'mkdir -p "$SLICC_USER_DATA_DIR"\n' + SLEEPER)
        fake_node.chmod(0o755)
        # A SLICC the person started: the same file name in another install, with the same --prompt shape.
        decoy = self.tmp / "other" / "sliccy" / "dist" / "node-server" / "index.js"
        decoy.parent.mkdir(parents=True)
        decoy.write_text(SLEEPER)
        self.decoy = subprocess.Popen(["/bin/bash", str(decoy), "--prompt", "something of their own"])
        self.addCleanup(self._end, self.decoy)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_SLICC_")}
        self.env.update(HOME=str(self.home), CLAUDE_SLICC_HOME=str(self.cs), CLAUDE_SLICC_NODE=str(fake_node),
                        CLAUDE_SLICC_NO_SERVERS="1", FAKE_NODE_OUT=str(self.out))

    @staticmethod
    def _end(proc) -> None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def run_script(self, name: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(SCRIPTS / name), *args], env=self.env, capture_output=True, text=True, timeout=60)

    def wait_for(self, cond, timeout: float = 10.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for the condition")

    def test_start_uses_its_own_profile_and_stop_ends_only_what_it_started(self):
        p = self.run_script("start_slicc.sh", "job-0000aaaa", PROMPT)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.wait_for(self.out.exists)
        self.wait_for(lambda: len(self.out.read_text().splitlines()) == 2)
        profile, worker = self.out.read_text().splitlines()
        self.assertEqual(profile, str(self.cs / "chrome-profile"))        # SLICC_USER_DATA_DIR
        self.assertEqual(worker, "http://localhost:5711")                 # the local UI, not the hosted app
        self.assertFalse((self.cs / "chrome-profile" / "old.txt").exists())   # claude-slicc's profile was wiped
        self.assertTrue(self.sentinel.exists())                           # the person's SLICC profile was not
        self.assertEqual((self.home / ".slicc" / "session-id").read_text(), "personal")
        self.assertEqual((self.cs / "run" / "slicc.job").read_text().strip(), "job-0000aaaa")
        pid = int((self.cs / "run" / "slicc.pid").read_text())
        self.assertTrue(alive(pid))

        p = self.run_script("stop_slicc.sh", "quiet")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.wait_for(lambda: not alive(pid))
        self.assertIsNone(self.decoy.poll())                              # the person's own SLICC keeps running
        self.assertFalse((self.cs / "run" / "slicc.pid").exists())
        self.assertTrue(self.sentinel.exists())

    def test_a_second_start_replaces_the_first_and_stop_without_anything_running_is_quiet(self):
        self.run_script("start_slicc.sh", "job-0000aaaa", PROMPT)
        self.wait_for(lambda: (self.cs / "run" / "slicc.pid").exists())
        first = int((self.cs / "run" / "slicc.pid").read_text())
        self.run_script("start_slicc.sh", "job-0000bbbb", PROMPT)
        second = int((self.cs / "run" / "slicc.pid").read_text())
        self.wait_for(lambda: not alive(first))
        self.assertTrue(alive(second))
        self.run_script("stop_slicc.sh", "quiet")
        self.wait_for(lambda: not alive(second))
        p = self.run_script("stop_slicc.sh")
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "stopped"))
        self.assertIsNone(self.decoy.poll())


    def test_servers_starts_the_endpoint_and_the_ui_and_stop_servers_ends_them(self):
        """On spare ports, with a stand-in UI: nothing else is launched, and only those two processes are stopped."""
        ui_root = self.tmp / "ui"
        ui_root.mkdir()
        (ui_root / "index.html").write_text("<html><head></head><body>SLICC</body></html>")
        port, ui_port = free_port(), free_port()
        env = dict(self.env, CLAUDE_SLICC_NO_SERVERS="0", CLAUDE_SLICC_PORT=str(port),
                   CLAUDE_SLICC_UI_PORT=str(ui_port), CLAUDE_SLICC_UI_ROOT=str(ui_root))
        stop = [str(SCRIPTS / "stop_slicc.sh"), "quiet", "servers"]
        self.addCleanup(subprocess.run, stop, env=env, capture_output=True, timeout=60)
        p = subprocess.run([str(SCRIPTS / "start_slicc.sh"), "servers"], env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        token = (self.cs / "token").read_text().strip()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(json.loads(urllib.request.urlopen(req, timeout=10).read())["data"][0]["id"], "claude-slicc")
        self.wait_for(lambda: listening(ui_port))
        with urllib.request.urlopen(f"http://127.0.0.1:{ui_port}/", timeout=10) as r:
            self.assertIn(b"claude-slicc-seed.js", r.read())                  # the UI, seeded for the endpoint
        self.assertFalse((self.cs / "run" / "slicc.pid").exists())             # no SLICC launched
        self.assertFalse((self.cs / "jobs").exists())                          # and no job folder made
        p = subprocess.run(stop, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.wait_for(lambda: not listening(port) and not listening(ui_port))
        self.assertIsNone(self.decoy.poll())

    def test_an_attached_start_launches_nothing_and_touches_no_profile(self):
        p = self.run_script("start_slicc.sh", "job-0000cccc", PROMPT, "attach")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("nothing launched", p.stdout)
        time.sleep(0.5)
        self.assertFalse(self.out.exists())                                   # the node CLI was not started
        self.assertFalse((self.cs / "run" / "slicc.pid").exists())
        self.assertTrue((self.cs / "chrome-profile" / "old.txt").exists())    # no wipe: no SLICC of ours runs
        self.assertTrue(self.sentinel.exists())
        self.assertIsNone(self.decoy.poll())


if __name__ == "__main__":
    unittest.main()
