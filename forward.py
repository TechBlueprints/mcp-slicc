"""mcp-slicc's forward brain: the transport to one upstream agent endpoint (2026-11-14). Stdlib only, Python 3.9+.

With backend forward (README "The forward backend") every SLICC call is sent as it came, byte for byte, to one
upstream OpenAI-compatible endpoint, which picks SLICC's steps and gates them. endpoint.py decides what to do with the
answer (the hard stops, the job's report); this module only carries the call there and back:

  * the upstream is a Unix socket (forward_url "unix:/path/to.sock"; calls go to /v1/chat/completions and /v1/models
    on it) or an http(s) base URL ending in /v1
  * the call carries `Authorization: Bearer <forward token>` and the X-Slicc-* headers endpoint.py gives it
  * it is held until the upstream answers, up to forward_timeout seconds. When SLICC hangs up or the job is stopped,
    the upstream connection is shut down, which is how the upstream learns that its answer is no longer wanted
  * an answer is JSON or a server-sent event stream; either way it comes back as {content, tool_calls}
"""
from __future__ import annotations

import http.client
import json
import secrets
import socket
import ssl
import threading
import time
import urllib.parse
from typing import Callable, Optional

import common as C

CONNECT_TRIES = 3      # an upstream that is starting (a socket not there yet, a refused connect) gets 3 tries, 1 s apart
CONNECT_TIMEOUT_S = 10


class UpstreamError(Exception):
    """The upstream could not be reached, refused the call, or answered something that is not a completion."""


class Cancelled(Exception):
    """The call was given up while it was held: `why` is dropped (SLICC hung up), stopped (the job was stopped) or
    timeout (forward_timeout passed)."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.unix_path)
        except OSError:
            s.close()
            raise
        self.sock = s


def target(url: str) -> tuple:
    """(make a connection with a timeout, the path prefix that /chat/completions goes after) for forward_url."""
    if url.startswith("unix:"):
        path = urllib.parse.urlparse(url).path if url.startswith("unix://") else url[len("unix:"):]
        if not path:
            raise UpstreamError(f"forward_url {url!r} names no socket")
        return (lambda t: _UnixHTTPConnection(path, t)), "/v1"
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UpstreamError(f"forward_url {url!r} is not unix:/path, http://… or https://…")
    port = u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        return (lambda t: http.client.HTTPSConnection(u.hostname, port, timeout=t, context=ctx)), u.path.rstrip("/")
    return (lambda t: http.client.HTTPConnection(u.hostname, port, timeout=t)), u.path.rstrip("/")


class _Held:
    """One upstream call in flight. abort() shuts its socket down from another thread, which ends a blocked read."""

    def __init__(self):
        self.conn: Optional[http.client.HTTPConnection] = None
        self.why: Optional[str] = None
        self.done = threading.Event()
        self.lock = threading.Lock()

    def abort(self, why: str) -> None:
        with self.lock:
            if self.why is None:
                self.why = why
            conn = self.conn
        sock = getattr(conn, "sock", None) if conn else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _watch(held: _Held, cancel: threading.Event, stopped: Callable[[], bool], deadline: float) -> None:
    while not held.done.wait(0.2):
        if cancel.is_set():
            return held.abort("dropped")
        if stopped():
            return held.abort("stopped")
        if time.time() >= deadline:
            return held.abort("timeout")


def request(method: str, path: str, body: Optional[bytes], headers: dict, cancel: threading.Event,
            stopped: Callable[[], bool] = lambda: False, timeout: Optional[float] = None,
            url: Optional[str] = None) -> tuple:
    """(HTTP status, response headers as a dict with lower-case names, the response) for one upstream call. The
    response is read whole; a server-sent event stream is read to its end and returned as its events' data, one
    parsed object each, under "events". Raises Cancelled while held, UpstreamError when the upstream cannot be
    reached."""
    url = url or C.forward_url()
    if not url:
        raise UpstreamError("backend forward has no forward_url (mcp-slicc config)")
    make, prefix = target(url)
    timeout = C.forward_timeout() if timeout is None else timeout
    held = _Held()
    deadline = time.time() + timeout
    watcher = threading.Thread(target=_watch, args=(held, cancel, stopped, deadline), daemon=True)
    watcher.start()
    try:
        for attempt in range(CONNECT_TRIES):
            conn = make(min(CONNECT_TIMEOUT_S, timeout))
            with held.lock:
                held.conn = conn
                why = held.why
            if why:
                raise Cancelled(why)
            try:
                conn.connect()
                break
            except OSError as e:
                conn.close()
                if attempt == CONNECT_TRIES - 1:
                    raise UpstreamError(f"the upstream at {url} could not be reached ({type(e).__name__})") from None
                end = time.time() + 1
                while time.time() < end:
                    if held.why:
                        raise Cancelled(held.why)
                    time.sleep(0.05)
        try:
            if held.why:                       # given up while it connected
                raise Cancelled(held.why)
            conn.sock.settimeout(timeout + 10)     # the watcher's deadline ends a held call, not a quiet socket
            conn.request(method, prefix + path, body=body, headers=headers)
            resp = conn.getresponse()
            rh = {k.lower(): v for k, v in resp.getheaders()}
            if "text/event-stream" in rh.get("content-type", ""):
                data = {"events": _read_events(resp)}
            else:
                data = resp.read()
            return resp.status, rh, data
        except (OSError, http.client.HTTPException, ValueError) as e:
            if held.why:
                raise Cancelled(held.why) from None
            if isinstance(e, socket.timeout):
                raise Cancelled("timeout") from None
            raise UpstreamError(f"the call to the upstream at {url} failed ({type(e).__name__}: {e})"[:300]) from None
        finally:
            conn.close()
    finally:
        held.done.set()


def _read_events(resp) -> list:
    """The data of each event in a server-sent event stream, parsed, up to [DONE]. Comments (keepalives) are skipped."""
    out = []
    while True:
        line = resp.readline()
        if not line:
            return out
        text = line.decode("utf-8", "replace").strip()
        if not text.startswith("data:"):
            continue
        data = text[5:].strip()
        if data == "[DONE]":
            return out
        try:
            out.append(json.loads(data))
        except ValueError:
            raise ValueError(f"an event that is not JSON: {data[:80]}") from None


def message_of(status: int, data) -> dict:
    """{content, tool_calls} from the upstream's answer to a chat completion, JSON or streamed. UpstreamError for an
    HTTP error, an error object, or anything else that is not a completion."""
    if status == 401 or status == 403:
        raise UpstreamError(f"the upstream refused mcp-slicc's token (HTTP {status})")
    if isinstance(data, dict) and "events" in data:
        return _from_events(status, data["events"])
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    try:
        obj = json.loads(text) if text.strip() else None
    except ValueError:
        obj = None
    if status >= 300:
        why = (obj or {}).get("error") if isinstance(obj, dict) else None
        why = why.get("message") if isinstance(why, dict) else why
        raise UpstreamError(f"the upstream answered HTTP {status}" + (f": {str(why or text)[:200]}" if why or text
                                                                          else ""))
    if not isinstance(obj, dict):
        raise UpstreamError("the upstream's answer is not JSON")
    if obj.get("error"):
        raise UpstreamError(f"the upstream answered an error: {str(obj['error'])[:200]}")
    choices = obj.get("choices") or []
    msg = (choices[0] or {}).get("message") if choices and isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        raise UpstreamError("the upstream's answer has no message")
    return {"content": C.text_of(msg.get("content")), "tool_calls": _tool_calls(msg.get("tool_calls"))}


def _from_events(status: int, events: list) -> dict:
    if status >= 300:
        raise UpstreamError(f"the upstream answered HTTP {status}")
    content, calls = [], {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("error"):
            raise UpstreamError(f"the upstream answered an error: {str(ev['error'])[:200]}")
        for ch in ev.get("choices") or []:
            d = (ch or {}).get("delta") or {}
            if isinstance(d.get("content"), str):
                content.append(d["content"])
            for tc in d.get("tool_calls") or []:
                i = tc.get("index", len(calls))
                cur = calls.setdefault(i, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    cur["id"] = tc["id"]
                fn = tc.get("function") or {}
                name = fn.get("name")
                if name and cur["function"]["name"] != name:
                    cur["function"]["name"] += name
                args = fn.get("arguments")
                if args:
                    cur["function"]["arguments"] += args if isinstance(args, str) else json.dumps(args)
    return {"content": "".join(content), "tool_calls": _tool_calls([calls[i] for i in sorted(calls)])}


def _tool_calls(raw) -> list:
    """Tool calls as SLICC takes them: an id, type function, a name and the arguments as a JSON string."""
    out = []
    for tc in raw or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        out.append({"id": str(tc.get("id") or "call_" + secrets.token_hex(8)), "type": "function",
                    "function": {"name": str(fn.get("name") or ""),
                                 "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
    return out


def models(cancel: Optional[threading.Event] = None) -> Optional[list]:
    """The upstream's /v1/models list, or None when it cannot be had in 10 s."""
    token = C.forward_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        status, _h, data = request("GET", "/models", None, headers, cancel or threading.Event(), timeout=10)
        obj = json.loads(data) if isinstance(data, bytes) and status == 200 else None
    except (UpstreamError, Cancelled, ValueError):
        return None
    items = obj.get("data") if isinstance(obj, dict) else None
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else None
