#!/usr/bin/env python3
"""mcp-slicc's operator CLI (2026-11-14): the MCP tools' functions from a shell, one call per run, JSON out.

For an operator that is not Claude Code, such as a broker that checks a person's approval before it calls (approval
external, README "Approvals"). The functions are slicc_mcp.py's, so a job started here is the same job the MCP tools
see. Usage (--home before the command; it is the state folder, CLAUDE_SLICC_HOME, whose config.json is read):

  slicc_op.py [--home DIR] task "<instruction>" [--mode auto|manual] [--launch launcher|attach] [--model M] [--ref R]
  slicc_op.py [--home DIR] wait <job> [--seconds N]                     up to 600 s; default 40
  slicc_op.py [--home DIR] decide <ask> allow|deny [--wants TEXT] [--ref R]
  slicc_op.py [--home DIR] tell <job> "<note>" [--ref R]
  slicc_op.py [--home DIR] stop <job>
  slicc_op.py [--home DIR] status
  slicc_op.py [--home DIR] bash "<command>" [--purpose P]                  manual jobs, as slicc_bash ...
  slicc_op.py [--home DIR] read-file <path>
  slicc_op.py [--home DIR] write-file <path> [--content TEXT]              without --content: stdin
  slicc_op.py [--home DIR] edit <path> --edits '[{"oldText": …, "newText": …}]'
  slicc_op.py [--home DIR] report "<text>"

Prints the tool's result object as JSON and exits 0, or prints {"error": "..."} and exits 1. A usage error exits 2.
--ref is recorded with the job, the note or the answer (for example the id of the approval the caller checked).

There is no hook here: in every approval mode the call runs as the MCP tool would after its hook allowed it. With
approval external that is the point; with the others it is the person at the shell deciding.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

WAIT_CAP_S = 600.0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slicc_op.py", description="mcp-slicc's operator: the MCP tools from a shell.")
    p.add_argument("--home", help="the state folder (CLAUDE_SLICC_HOME); its config.json is read")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("task", help="start a job")
    t.add_argument("instruction")
    t.add_argument("--mode", choices=("auto", "manual"))
    t.add_argument("--launch", choices=("launcher", "attach"))
    t.add_argument("--model")
    t.add_argument("--ref")
    w = sub.add_parser("wait", help="follow the job")
    w.add_argument("job")
    w.add_argument("--seconds", type=float)
    d = sub.add_parser("decide", help="answer one open question")
    d.add_argument("ask")
    d.add_argument("answer", choices=("allow", "deny"))
    d.add_argument("--wants")
    d.add_argument("--ref")
    n = sub.add_parser("tell", help="a note for the running job")
    n.add_argument("job")
    n.add_argument("note")
    n.add_argument("--ref")
    s = sub.add_parser("stop", help="stop the job and SLICC")
    s.add_argument("job")
    sub.add_parser("status", help="the current job")
    b = sub.add_parser("bash", help="manual job: one command in SLICC's shell")
    b.add_argument("command")
    b.add_argument("--purpose")
    r = sub.add_parser("read-file", help="manual job: read a file in SLICC's workspace")
    r.add_argument("path")
    wf = sub.add_parser("write-file", help="manual job: write a file in SLICC's workspace")
    wf.add_argument("path")
    wf.add_argument("--content", help="the content; without it, stdin")
    e = sub.add_parser("edit", help="manual job: exact replacements in a file in SLICC's workspace")
    e.add_argument("path")
    e.add_argument("--edits", required=True, help='a JSON list of {"oldText": …, "newText": …}')
    rp = sub.add_parser("report", help="manual job: end it with your report")
    rp.add_argument("text")
    return p


def call(argv: list) -> tuple:
    """(exit code, the object to print) for one command line."""
    args = parser().parse_args(argv)
    if args.home:
        os.environ["CLAUDE_SLICC_HOME"] = args.home      # for the scripts the host runs, too
    import common as C
    import slicc_mcp as M
    if args.home:
        C.HOME = Path(args.home)
    M.WAIT_CAP_S = WAIT_CAP_S
    c = args.cmd
    if c == "task":
        name, a = "slicc_task", {"instruction": args.instruction, "mode": args.mode, "launch": args.launch,
                                 "model": args.model, "ref": args.ref}
    elif c == "wait":
        name, a = "slicc_wait", {"job": args.job, "seconds": args.seconds}
    elif c == "decide":
        name, a = "slicc_decide", {"ask": args.ask, "answer": args.answer, "wants": args.wants, "ref": args.ref}
    elif c == "tell":
        name, a = "slicc_tell", {"job": args.job, "note": args.note, "ref": args.ref}
    elif c == "stop":
        name, a = "slicc_stop", {"job": args.job}
    elif c == "status":
        name, a = "slicc_status", {}
    elif c == "bash":
        name, a = "slicc_bash", {"command": args.command, "purpose": args.purpose}
    elif c == "read-file":
        name, a = "slicc_read_file", {"path": args.path}
    elif c == "write-file":
        name, a = "slicc_write_file", {"path": args.path,
                                       "content": args.content if args.content is not None else sys.stdin.read()}
    elif c == "edit":
        try:
            edits = json.loads(args.edits)
        except ValueError:
            return 1, {"error": "--edits is not JSON"}
        name, a = "slicc_edit", {"path": args.path, "edits": edits}
    else:
        name, a = "slicc_report", {"text": args.text}
    try:
        return 0, M.HANDLERS[name]({k: v for k, v in a.items() if v is not None})
    except M.Err as e:
        return 1, {"error": str(e)}
    except Exception as e:   # noqa: BLE001 - a failure is a JSON error, not a traceback
        return 1, {"error": f"{name} failed ({type(e).__name__}: {e})"}


def main() -> None:
    code, obj = call(sys.argv[1:])
    print(json.dumps(obj, ensure_ascii=False, indent=1))
    sys.exit(code)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # also under python3 -I
    main()
