#!/usr/bin/env python3
"""Serves SLICC's bundled web app (dist/ui of the pinned sliccy 6.258.2) on 127.0.0.1:5711, in place of www.sliccy.ai
(mcp-slicc, 2026-11-09).

The CLI opens Chrome at this origin when WORKER_BASE_URL=http://localhost:5711 is set; without it the CLI opens the
hosted app, which sends telemetry (seen on 2026-11-09). Two changes are made on the fly; the files on disk stay as shipped:

  * index.html gets /claude-slicc-seed.js first in <head>. It points SLICC's model provider at this repo's endpoint
    (local-llm, http://localhost:8660/v1, model "claude-slicc", the endpoint's token) and turns telemetry off, before the
    app reads its settings.
  * nothing else. SLICC's own default sudoers stays (see README: the endpoint's gate is the check here).

Static files only; unknown paths get index.html (the app routes itself); /api/ paths are 404 (the CLI serves those on 5710).
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

import common as C

ROOT = Path(os.environ.get("CLAUDE_SLICC_UI_ROOT",
                           str(C.HOME / "slicc" / "node_modules" / "sliccy" / "dist" / "ui"))).resolve()
PORT = int(os.environ.get("CLAUDE_SLICC_UI_PORT", "5711"))
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def seed_js() -> bytes:
    account = {"providerId": "local-llm", "apiKey": C.token(), "baseUrl": f"http://localhost:{C.PORT}/v1",
               "deployment": "claude-slicc"}
    return ("(function(){try{var L=window.localStorage;"
            f"L.setItem('slicc_accounts',{json.dumps(json.dumps([account]))});"
            "L.setItem('selected-model','local-llm:claude-slicc');"
            "L.setItem('telemetry-disabled','true');"
            "}catch(e){console.error('claude-slicc seed failed',e)}})();\n").encode()


def patched_index(path: Path) -> bytes:
    html = path.read_bytes().decode()
    return re.sub(r"<head>", '<head>\n    <script src="/claude-slicc-seed.js"></script>', html, count=1).encode()


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def _body(self, data: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_head(self):
        bare = self.path.split("?", 1)[0]
        if bare == "/claude-slicc-seed.js":
            self._body(seed_js(), "text/javascript")
            return None
        real = Path(os.path.realpath(self.translate_path(bare)))
        if not str(real).startswith(str(ROOT)) or not real.is_file():
            if bare.startswith(("/api/", "/rel/", "/tray/", "/join/")):
                self.send_error(404)
                return None
            real = ROOT / "index.html"
        if real == ROOT / "index.html":
            self._body(patched_index(real), "text/html; charset=utf-8")
            return None
        self.path = "/" + os.path.relpath(real, ROOT)
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("ui %s\n" % (fmt % args))


def main() -> None:
    if not (ROOT / "index.html").is_file():
        sys.exit(f"no SLICC UI at {ROOT}: install sliccy first (see README)")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
