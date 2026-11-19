"""mcp-slicc's change gate: the rules every SLICC step is checked against before SLICC sees it. Stdlib only.

Each step gets a verdict: `auto` (it runs), `ask` (the approval setting decides, see README "Approvals") or `stop`
(it never runs, and the model is told). The rules are about SLICC's own shell and workspace, a virtual system in the
browser:

  * plainly read-only commands, and browser reads (`playwright-cli snapshot`, `tab-list` ...), run
  * a short list of SLICC commands never runs: its cloud, devices, credentials, its own controls and memory
  * everything else is asked: opening a page, `curl`, `node`, a click, a write outside the scratch space
  * reads in SLICC's workspace run; writes to its controls and memory files never happen; writes under the scratch
    paths run

The policy is slicc_gate.json beside this file (CHANGE_GATE_POLICY names another), read again when it changes.

Another gate can be used instead (CLAUDE_SLICC_GATE, or "gate" in ~/.claude-slicc/config.json). common.py needs it to
offer:

  slicc_classify(tool, args) -> {"decision": "auto" | "ask" | "stop", "reason": str, "detail": str}
  slicc7_classify(tool, args) -> the same for SLICC seven's tools (2026-11-18); a gate without it stops every seven
                                step. A stop of a tool on its stop_tools list carries "rule": "stop_tools"
  _segments(command)         -> the command's pipeline, ; and && parts as token lists, or None when it writes or
                                cannot be read plainly
  load_policy()              -> {"slicc": {...}} with at least browser_commands and browser_read_verbs; common.py sets
                                the module's POLICY to it after import
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex

HERE = os.path.dirname(os.path.realpath(__file__))
POLICY_PATH = os.environ.get("CHANGE_GATE_POLICY", os.path.join(HERE, "slicc_gate.json"))
POLICY: dict = {}


def load_policy() -> dict:
    with open(POLICY_PATH) as f:
        return json.load(f)


def _pol(section: str = "slicc") -> dict:
    return (POLICY or load_policy()).get(section) or {}


# ----------------------------------------------------------------------------------------- secrets in a step

SECRET_VALUE = [
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),              # JWT
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),                                                  # Anthropic keys
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(password|passwd|pwd|passphrase|token|secret|api[_-]?key)(\"?\s*[=:]\s*\"?)[^\s\",}]+"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),                                                       # redacted, not refused
    re.compile(r"\b[A-Za-z0-9+/_-]{48,}={0,2}"),                                               # redacted, not refused
]
REFUSED_SECRETS = SECRET_VALUE[:4]


def redact_text(s, limit=None) -> str:
    if not isinstance(s, str):
        s = json.dumps(s, ensure_ascii=False, default=str)
    for rx in SECRET_VALUE:
        if rx.groups >= 2:
            s = rx.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", s)
        else:
            s = rx.sub("[redacted]", s)
    if limit and len(s) > limit:
        s = s[:limit] + f"... [{len(s) - limit} more chars]"
    return s


def card_number(text) -> bool:
    """True when the text holds a 13-19 digit number that passes the Luhn check (a payment card)."""
    for m in re.finditer(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", text or ""):
        d = [int(c) for c in re.sub(r"\D", "", m.group(0))]
        s = sum(d[-1::-2]) + sum(sum(divmod(2 * x, 10)) for x in d[-2::-2])
        if s % 10 == 0:
            return True
    return False


# ----------------------------------------------------------------------------------------- reading a command

def _segments(cmd):
    """Pipeline/;/&& segments as token lists, or None when the command writes or cannot be judged
    (a > or >> except to /dev/null, substitution, here-doc, background, unparseable)."""
    if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 2000:
        return None
    c = " " + cmd
    c = re.sub(r"\s(?:\d?>|&>)\s*/dev/null", " ", c)
    c = re.sub(r"\s\d?>&\d", " ", c)
    if any(x in c for x in ("`", "$(", "<(", ">(", "<<")):
        return None
    c = c.replace("\n", " ; ")
    try:
        lex = shlex.shlex(c, posix=True, punctuation_chars=";&|<>()")
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return None
    segs, cur = [], []
    for t in toks:
        if t in (";", "&&", "||", "|"):
            segs.append(cur)
            cur = []
        elif t and set(t) <= set(";&|<>()"):
            if t != "<":
                return None
            cur.append(t)
        else:
            cur.append(t)
    segs.append(cur)
    return [[t for t in s if t != "<"] for s in segs if s]


SED_SCRIPT = re.compile(r"^((\d+|\$|/[^/]*/)(,(\d+|\$|/[^/]*/))?\s*p\s*;?\s*)+$")


def _readonly_args(seg: list) -> bool:
    """For the few read-only commands that have options that write or run something: whether this use has none."""
    name = seg[0]
    if "/" in name:
        return False
    if name == "date":
        return not any(t in ("-s", "--set") or t.startswith("--set=") for t in seg)
    if name == "rg":
        return not any(t == "--pre" or t.startswith("--pre=") for t in seg)
    if name == "find":
        return not any(t in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls")
                       for t in seg)
    if name == "sort":
        return not any(t.startswith("--output") or re.fullmatch(r"-[A-Za-z]*o\S*", t) for t in seg[1:])
    if name == "uniq":
        return len([t for t in seg[1:] if not t.startswith("-")]) <= 1
    if name == "awk":
        return not any(re.search(r"system|getline|>|\||-i\b|inplace|fflush\(\"", t) for t in seg[1:])
    if name == "sed":
        if "-n" not in seg or any(t.startswith("-i") or t.startswith("--in-place") for t in seg):
            return False
        scripts = [t for t in seg[1:] if not t.startswith("-")]
        return bool(scripts) and bool(SED_SCRIPT.match(scripts[0]))
    return True


def _first_words(cmd) -> set:
    return set(re.findall(r"(?:^|[;&|(]\s*|\s)([A-Za-z][\w.+-]*)", cmd or ""))


def url_kind(url) -> str:
    """blank (about:blank), scheme (not http(s): file:, javascript:, data:, chrome: ...) or web."""
    u = str(url or "").strip()
    if u == "about:blank":
        return "blank"
    if not re.match(r"(?i)^https?://", u) and re.match(r"(?i)^[a-z][a-z0-9+.-]*:(?!\d)", u):   # host:port is web
        return "scheme"
    return "web"


# ----------------------------------------------------------------------------------------- the verdicts

def slicc_command(cmd, section: str = "slicc") -> dict:
    """Verdict on one bash line SLICC's model wants to run: {decision: auto|ask|stop, reason, detail}. `section` is
    the policy's rule set: slicc (six) or slicc7 (seven)."""
    sp = _pol(section)
    detail = f"run in SLICC's shell: `{redact_text(cmd, 600)}`"
    if not isinstance(cmd, str) or not cmd.strip():
        return {"decision": "stop", "reason": "an empty command", "detail": detail}
    if any(p.search(cmd) for p in REFUSED_SECRETS) or card_number(cmd):
        return {"decision": "stop", "reason": "the command carries what looks like a secret or a card number",
                "detail": detail}
    never = set(sp.get("stop_commands", []))
    named = sorted(_first_words(cmd) & never)
    segs = _segments(cmd)
    if segs is None:
        if named:
            return {"decision": "stop", "reason": f"`{named[0]}` is never run by SLICC ({sp.get('stop_reason', '')})",
                    "detail": detail}
        touched = [w for w in sp.get("control_words", []) if w in cmd]
        if touched:
            return {"decision": "stop", "reason": f"the command writes or runs something and names {touched[0]}, one "
                    "of SLICC's controls, which SLICC never changes", "detail": detail}
        return {"decision": "ask", "reason": "the command writes, redirects or cannot be read plainly", "detail": detail}
    ro, browser = set(sp.get("readonly_commands", [])), set(sp.get("browser_commands", []))
    b_read, b_nav = set(sp.get("browser_read_verbs", [])), set(sp.get("browser_navigate_verbs", []))
    asks = []
    for seg in segs:
        name = seg[0]
        if name in never:
            return {"decision": "stop", "reason": f"`{name}` is never run by SLICC ({sp.get('stop_reason', '')})",
                    "detail": detail}
        if "--help" in seg[1:] and not any(t for t in seg[1:] if t.startswith("--") and t != "--help" and "=" in t):
            continue   # `<command> [verb] --help` prints usage and does nothing
        if name in browser or name == "open":
            verb = "open" if name == "open" else next((t for t in seg[1:] if not t.startswith("-")), "")
            if verb in b_read:
                continue
            if verb in b_nav:
                url = next((t for t in seg[1:] if re.match(r"(?i)^[a-z][a-z0-9+.-]*:", t)), "")
                if not url:
                    asks.append(f"{name} {verb} (no URL the gate can read)")
                    continue
                kind = url_kind(url)
                if kind == "scheme":
                    return {"decision": "stop", "reason": f"SLICC's browser only opens http(s) pages; "
                            f"{redact_text(url, 120)} is not one", "detail": detail}
                if kind == "blank":
                    continue
                asks.append(f"open {redact_text(url, 300)}")
                continue
            asks.append(f"{name} {verb}".strip())
            continue
        if name in ro and _readonly_args(seg):
            continue
        asks.append(name)
    if not asks:
        return {"decision": "auto", "reason": "read-only in SLICC's own workspace", "detail": detail}
    touched = [w for w in sp.get("control_words", []) if w in cmd]
    if touched:
        return {"decision": "stop", "reason": f"the command is not read-only and names {touched[0]}, one of SLICC's "
                "controls, which SLICC never changes", "detail": detail}
    return {"decision": "ask", "reason": "needs a person: " + "; ".join(dict.fromkeys(asks)), "detail": detail}


def _seven_path(path) -> str:
    """A SLICC seven path as the kernel will resolve it: relative to the cone's home, with . and .. folded, so
    /tmp/../etc/x is /etc/x."""
    p = str(path or "")
    if not p.startswith("/"):
        p = _pol("slicc7").get("home", "/home/cone").rstrip("/") + "/" + (p[2:] if p.startswith("~/") else p)
    out = os.path.normpath(p)
    return out + "/" if p.endswith("/") and not out.endswith("/") else out


def _path_verdict(path, verb, section: str = "slicc") -> dict:
    sp = _pol(section)
    if section == "slicc7":
        path = _seven_path(path)
    detail = f"{verb} {redact_text(path, 300)} in SLICC's workspace"
    if any(fnmatch.fnmatch(path, g) for g in sp.get("host_paths", [])):
        return {"decision": "ask", "reason": f"{path} is in a folder shared from the host", "detail": detail}
    if verb == "read":   # its own workspace: no secrets live there, and the profile is fresh every job
        return {"decision": "auto", "reason": "a read in SLICC's workspace", "detail": detail}
    if any(fnmatch.fnmatch(path, g) for g in sp.get("control_paths", [])):
        return {"decision": "stop", "reason": f"{path} is one of SLICC's controls or memory files, which SLICC never "
                "changes", "detail": detail}
    if any(path.startswith(pfx) for pfx in sp.get("scratch_paths", [])):
        return {"decision": "auto", "reason": "scratch space", "detail": detail}
    return {"decision": "ask", "reason": "a write outside SLICC's scratch space", "detail": detail}


def slicc_classify(tool, args) -> dict:
    """Verdict on one tool call SLICC's model returned (the model boundary)."""
    sp = _pol()
    args = args if isinstance(args, dict) else {}
    if tool == "bash":
        return slicc_command(str(args.get("command") or args.get("cmd") or ""))
    if tool in sp.get("internal_tools", []):
        return {"decision": "auto", "reason": "SLICC's own bookkeeping", "detail": f"SLICC tool {tool}"}
    if tool in sp.get("stop_tools", []):
        return {"decision": "stop", "reason": f"{tool} is never used by SLICC ({sp.get('stop_reason', '')})",
                "detail": f"SLICC tool {tool}"}
    if tool in ("write_file", "edit"):
        return _path_verdict(str(args.get("path") or args.get("file_path") or ""), "write")
    if tool == "read_file":
        return _path_verdict(str(args.get("path") or args.get("file_path") or ""), "read")
    return {"decision": "ask", "reason": f"{tool} is not a SLICC tool the gate knows",
            "detail": f"SLICC tool {tool}: {redact_text(json.dumps(args, ensure_ascii=False), 400)}"}


def slicc7_classify(tool, args) -> dict:
    """Verdict on one tool call of SLICC seven (2026-11-18): pi's bash, read, write and edit on seven's kernel, and
    seven's own tools. Every tool without a rule is asked. codemode is stopped here (rule stop_tools); mcp-slicc lets
    it run only where seven's owner tier files its nested calls for this gate one by one."""
    sp = _pol("slicc7")
    args = args if isinstance(args, dict) else {}
    if tool == "bash":
        return slicc_command(str(args.get("command") or args.get("cmd") or ""), "slicc7")
    if tool in sp.get("internal_tools", []):
        return {"decision": "auto", "reason": "SLICC's own bookkeeping", "detail": f"SLICC tool {tool}"}
    if tool in sp.get("stop_tools", []):
        return {"decision": "stop", "rule": "stop_tools", "reason": f"{tool} is never used by SLICC "
                f"({sp.get('stop_reason', '')})", "detail": f"SLICC tool {tool}: "
                f"{redact_text(json.dumps(args, ensure_ascii=False), 400)}"}
    if tool in ("write", "edit"):
        return _path_verdict(str(args.get("path") or args.get("file_path") or ""), "write", "slicc7")
    if tool == "read":
        return _path_verdict(str(args.get("path") or args.get("file_path") or ""), "read", "slicc7")
    return {"decision": "ask", "reason": f"{tool} is not a SLICC seven tool the gate knows",
            "detail": f"SLICC tool {tool}: {redact_text(json.dumps(args, ensure_ascii=False), 400)}"}
