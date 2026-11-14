"""A stand-in upstream agent endpoint for backend forward's tests (2026-11-14): OpenAI-compatible, on a Unix socket or
a TCP port, answering each POST /v1/chat/completions from a script.

Each script item is one call's answer:
  {"message": {"content": ..., "tool_calls": [...]}}          JSON, or with "sse": true a chunked event stream
  {"status": 500, "body": "..."}                               an HTTP error
  {"hold": 1.5, ...}                                           waits that long first
  {"hold_until_gone": true}                                    answers nothing, and notes when the caller hangs up
  {"headers": {"X-Slicc-Report": "failed"}, ...}               extra response headers
Every request is kept (path, headers, the body's bytes). A call without the right bearer token gets 401.
"""
from __future__ import annotations

import json
import select
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def step(command: str, cid: str = "call_up1") -> dict:
    return {"message": {"content": "", "tool_calls": [{"id": cid, "type": "function", "function": {
        "name": "bash", "arguments": json.dumps({"command": command})}}]}}


def final(text: str) -> dict:
    return {"message": {"content": text, "tool_calls": []}}


class _Unix(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class FakeUpstream:
    def __init__(self, unix_path: str = "", token: str = "up-token"):
        self.unix_path, self.token = unix_path, token
        self.script: list = []
        self.requests: list = []
        self.gone = threading.Event()        # a held caller hung up
        self.received = threading.Event()    # a chat call came in
        self.models = [{"id": "upstream-agent", "object": "model", "owned_by": "upstream"}]
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                return

            def address_string(self):     # a Unix socket has no client address
                return "upstream-client"

            def _send(self, code: int, obj, extra: dict = None) -> None:
                data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                return self.headers.get("Authorization", "") == f"Bearer {up.token}"

            def do_GET(self):
                up.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "raw": b""})
                if not self._authorized():
                    return self._send(401, {"error": {"message": "bad token"}})
                self._send(200, {"object": "list", "data": up.models})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                up.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "raw": raw})
                up.received.set()
                if not self._authorized():
                    return self._send(401, {"error": {"message": "bad token"}})
                item = up.script.pop(0) if up.script else final("")
                if item.get("hold_until_gone"):
                    end = time.time() + 30
                    while time.time() < end:
                        r, _, _ = select.select([self.connection], [], [], 0.1)
                        if r:
                            try:
                                if self.connection.recv(1, socket.MSG_PEEK) == b"":
                                    up.gone.set()
                                    return
                            except OSError:
                                up.gone.set()
                                return
                    return
                if item.get("hold"):
                    time.sleep(item["hold"])
                if "status" in item:
                    return self._send(item["status"], str(item.get("body", "")).encode())
                msg = item["message"]
                if item.get("sse"):
                    return self._sse(msg, item.get("headers"))
                self._send(200, {"id": "chatcmpl-up", "object": "chat.completion", "model": "upstream-agent",
                                 "choices": [{"index": 0, "message": {"role": "assistant", **msg},
                                              "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}]},
                           item.get("headers"))

            def _sse(self, msg: dict, extra: dict = None) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()

                def chunk(b: bytes) -> None:
                    self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n")
                    self.wfile.flush()

                def event(delta: dict, finish=None) -> None:
                    chunk(b"data: " + json.dumps({"object": "chat.completion.chunk", "choices": [
                        {"index": 0, "delta": delta, "finish_reason": finish}]}).encode() + b"\n\n")

                chunk(b": the upstream is working\n\n")
                text = msg.get("content") or ""
                event({"role": "assistant", "content": text[: len(text) // 2]})
                event({"content": text[len(text) // 2:]})
                for i, tc in enumerate(msg.get("tool_calls") or []):
                    args = tc["function"]["arguments"]
                    event({"tool_calls": [{"index": i, "id": tc["id"], "type": "function",
                                           "function": {"name": tc["function"]["name"],
                                                        "arguments": args[: len(args) // 2]}}]})
                    event({"tool_calls": [{"index": i, "function": {"arguments": args[len(args) // 2:]}}]})
                event({}, "tool_calls" if msg.get("tool_calls") else "stop")
                chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        if unix_path:
            self.srv = _Unix(unix_path, H)
        else:
            self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
            self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"unix:{self.unix_path}" if self.unix_path else f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def chat_requests(self) -> list:
        return [r for r in self.requests if r["method"] == "POST"]

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
