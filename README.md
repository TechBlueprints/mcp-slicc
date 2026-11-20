# mcp-slicc

Formerly `claude-slicc`. Claude Code driving [SLICC](https://github.com/ai-ecoverse/slicc) (the `sliccy` npm package,
hosted at www.sliccy.ai), a browser-and-shell agent, on your Mac, with the local `claude` CLI as SLICC's model. Two
layers:

- **The operator**: the `slicc` MCP server. A Claude Code session starts a job, follows it, answers its questions or
  takes the steps itself, and reads the report.
- **The model**: an OpenAI-compatible endpoint on `127.0.0.1:8660` that SLICC's own model provider points at. Each
  step SLICC asks for is chosen by `claude -p` (or by Qwen on a local model server), checked by a change gate, and
  only then handed to SLICC.

No sandbox: SLICC runs as you, on your Mac. SLICC's shell, file tools and `node` work in its own virtual filesystem
(checked with a canary file on 2026-11-09), but its Chrome is a real browser on your network.

Since 2026-11-18 a launcher job runs on SLICC seven (`npx sliccy@next`, the `slicc` CLI and the app at
`seven.sliccy.ai`), with SLICC six kept as the fallback: see [SLICC seven](#slicc-seven).

That is the default install. Since 2026-11-14 an install can instead hand every SLICC call to another agent (backend
`forward`), be driven from a shell instead of Claude Code (the operator CLI) and start SLICC through systemd (host
`systemd`): see [Backends](#backends-one-per-install).

Python 3.9 or newer, standard library only, and Node.js for SLICC itself. macOS: the scripts use Google Chrome,
`lsof` and SLICC's macOS profile folder.

## Parts

| File | What |
|---|---|
| `common.py` | settings, state under `~/.claude-slicc/`, the approval setting, the change gate (`gate_path()`, reloaded when it changes), and the host (starting and stopping SLICC) |
| `slicc_gate.py` / `slicc_gate.json` | the change gate: its rules and their policy (below). Any gate with the same interface can be set instead |
| `slicc_mcp.py` | the operator tools for Claude Code (below). Makes no model calls |
| `slicc_op.py` | the operator CLI: the same functions as the tools, from a shell, printing JSON (below) |
| `gate_hook.py` | PreToolUse hook for the slicc tools: allows, denies, asks, or stays silent so Claude Code's own permission system decides |
| `endpoint.py` | SLICC's OpenAI-compatible model on `127.0.0.1:8660`: picks steps with `claude -p` (auto jobs) or holds SLICC's call for the operator's steps (manual jobs). Screens results. Watches for SLICC hanging up. With backend `forward`, hands every call to the upstream agent instead |
| `forward.py` | backend `forward`'s transport: one call to the upstream agent, over a Unix socket or http(s), held until it answers and cut when SLICC hangs up |
| `claude_turn.py` | one `claude -p` per decision: no tools, no MCP servers, your login. The transcript renderer and the screen. In classifier mode, the headless step session (below) |
| `step_mcp.py` / `step_hook.py` | the headless step session's only MCP server (`slicc-step`: SLICC's four step tools, which record the step for the endpoint) and its PreToolUse hook (the change gate) |
| `spark_turn.py` | one Qwen call on the Spark per decision, for the `qwen-3.8-*` names: native tool calls, and requests built to keep the Spark's KV cache (below) |
| `job-brief.md` / `chat-brief.md` | what a job's model is told: a job handed over by the operator, or a chat typed in SLICC |
| `start_slicc.sh` / `stop_slicc.sh` | start SLICC for one job (own Chrome profile, wiped; `--prompt`; local UI) and stop it and its Chrome. They touch only what mcp-slicc started. `servers` starts or stops just the endpoint and the UI |
| `ui_server.py` | SLICC six: serves its bundled UI on `127.0.0.1:5711`, seeded to use the endpoint, telemetry off |
| `start_seven.sh` | SLICC seven: launches, links and seeds seven for one job and sends the prompt (run by `start_slicc.sh`) |
| `seven.mjs` | SLICC seven: sets the `local` account, the model and the approval policy on seven's page over CDP; for an attached job, follows your seven, sets its policy and lists and answers its approvals (Node 22+) |
| `owner_watch.py` | SLICC seven: answers seven's owner-tier approvals with the change gate, through `slicc approvals` and `slicc decide`, or through mcp-slicc's follower in an attached job |
| `attach_seven.sh` | SLICC seven, an attached job: runs mcp-slicc's follower for the length of the job, sets your seven's owner tier and puts it back |
| `tests/` | 195 tests with a stand-in `claude` (`fake_claude.py`), a fake Spark (`test_qwen.py`), a fake upstream agent (`fake_upstream.py`), a stand-in `systemctl` and temporary folders: no login, no spend, no SLICC, no Spark |

Run the tests from the repo: `python3 -m unittest discover -s tests -t tests` (Python 3.9 and 3.12 checked).

## SLICC seven

Built 2026-11-18. SLICC seven is the `slicc` CLI (Go, from the npm package `sliccy@next`) driving the app hosted at
`https://seven.sliccy.ai` in a Chrome it launches. Seven's agent has pi's coding tools (`bash`, `read`, `write`,
`edit`) on a Linux-like kernel in the browser with users and `sudo`, plus `codemode`, `memory_write`, `workflow`,
`tool_search` and licks. Its `local` provider takes any OpenAI-compatible server, and this endpoint is that server.

### Install and config

```bash
npm pack sliccy@7.0.0-next.1.69.1       # check the tarball's sha256 before you install it
npm install --prefix ~/.claude-slicc/slicc7 --ignore-scripts sliccy-7.0.0-next.1.69.1.tgz
```

That brings `slicc` 1.69.1 for this platform (an optional dependency of the package). Six stays where it is, under
`~/.claude-slicc/slicc/`. Node 22 or newer runs `seven.mjs`. `"slicc": 7` in `config.json` (the default) makes a
launcher job start seven; `"slicc": 6` brings six back, with nothing else to change.

### What a launcher job does on seven

`start_slicc.sh` stops the SLICC of an earlier job, checks the endpoint, and runs `start_seven.sh`:

1. **Version check.** `slicc capabilities --json` must say contract 1 and a 1.x CLI, and the agent seven installs must
   be slicc-agent 8.x. Anything else ends the job with one line saying which, and nothing runs.
2. **A fresh profile.** mcp-slicc's own seven profile (`mcp-slicc`, the folder `slicc` reports, under
   `~/Library/Application Support/slicc/seven-profiles/` on a Mac) is wiped, so no chat, file, memory or account of an
   earlier job is left. The first start installs seven's agent and tools: about two minutes from the job's start to
   its first step on this Mac.
3. **Launch and link.** `slicc --profile mcp-slicc --browser chrome --json --yes --no-fallback --no-kernel --no-jerk`,
   with loopback and the private ranges denied except the endpoint's port. Seven's browser commands
   (`playwright-cli`) need the link: without one they answer "no browser to drive". The link offers `net`, `http`,
   `credentialed`, `ws`, `udp` and `cdp`; no kernel services, no slicc-jerk, no SSH runner, no host folders.
4. **Seeding, without a person** (`seven.mjs`). The CLI has no command for an account, the model or the approval
   policy, so they are set over CDP on seven's page, through the objects seven's own Settings use (found on the page's
   heap by the methods they offer): the page's settings store `connect('local', …)` (what "Connect without a key"
   calls), `update({ model: 'local/claude-slicc' })`, and the agent connection's
   `approvalPolicySet({ owner: { tools: 'user' } })`. Nothing is clicked or typed. The account is keyless: seven sends
   a keyless account's calls with `Authorization: Bearer local`, and a key would have to be typed into the vault's own
   field. So the endpoint's token is in the account's address instead, `http://127.0.0.1:8660/t/<token>/v1`, and the
   endpoint accepts its token in the path or in the header.
5. **The owner tier.** `owner_watch.py` follows `slicc approvals --watch --json` and answers every `owner-tool` item
   (below), and the job is marked `owner_tier`.
6. **The prompt.** `slicc prompt --cdp <url> --json` sends the launch prompt; its events go to the job's `slicc.log`.

`slicc_stop` runs `slicc abort`, ends `slicc prompt`, the watcher and `slicc`, which closes the Chrome it launched,
and ends a Chrome still on that profile folder. Only processes this install started are touched (by the pids in
`run/` and their command lines); a seven you run yourself, on another profile or in your own Chrome, is never matched.

### What is gated

- **Every step on the `local` account** goes through the endpoint and the change gate, as on six. A seven step is
  judged by the gate's `slicc7_classify` with seven's tool names (`slicc_gate.json` section `slicc7`); a gate without
  seven's rules stops every seven step. mcp-slicc keeps six's names inside (`read_file`, `write_file`), and a step
  goes to seven as `read` or `write`.
- **Seven's rules.** Read-only commands and browser reads run; `sudo` and the rest are asked (seven's own `sudo` dialog
  stays as a second lock, and nothing here answers it). Never run: seven's link (`slicc`), scoops (`agent`,
  `subagent`, which can run on another model), skills and installs (`upskill`, `pnpm`, `npm`, `npx`), secrets
  (`fnox`), devices, mounts, SSH, Tailscale, users and `crontab`; any command that writes and names a control
  (`/etc/slicc`, `/etc/sudoers`, `/etc/fnox`, `.pi/agent`, …); writes under `/etc`, `/opt`, `/usr`, `~/.pi`, skills,
  `MEMORY.md`, `CLAUDE.md`, `AGENTS.md` and workflows (paths are made absolute and `..` folded first); the tools
  `memory_write`, `workflow` and `lick_confirm`. Reads of host folders (`/mnt`) are asked. Every tool without a rule
  (an MCP server's, say) is asked.
- **The owner tier.** With `owner.tools: user`, seven files every tool call of its cones and scoops, nested `codemode`
  calls included, as an item that waits up to 5 minutes. `owner_watch.py` answers each: a call the endpoint itself
  handed seven is `once` without a second check; otherwise the gate decides (`auto` is `once`, `stop` is `deny` with
  the gate's reason, `ask` goes to the classifier or to `slicc_decide`, as the endpoint's own gated steps do).
  Arguments seven cut short are denied. `always` is never given. If the watcher is not running nobody answers, and
  seven denies the call after 5 minutes.
- **`codemode`** (one call whose script runs other tools, of which the model boundary sees only the script) is stopped
  by the gate, except in a job whose seven files its nested calls through the owner tier: there the script runs, and
  each nested call is judged as it comes (live on 2026-11-18: a script's `ls` and `read` ran, and its write to
  `~/.pi/agent/settings.json` was denied with the gate's reason, 0.7 s for the three items). One script files one item
  for itself and one per nested call. mcp-slicc's own models are offered only the four step tools, so they never pick
  it; an upstream agent (backend `forward`) can.
- **Attached jobs** (`launch: attach`, a seven in your own Chrome, since 2026-11-19): the job's chat is gated at the
  endpoint, and the rest of your seven through the owner tier, which mcp-slicc's follower sets for the length of the job
  and puts back after it. See [Attached jobs on seven](#attached-jobs-on-seven).

### What differs from six

| | six | seven |
| --- | --- | --- |
| The app | `dist/ui` of sliccy 6.258.2, served by `ui_server.py` | hosted at `seven.sliccy.ai`, not pinned; the version check stands in |
| Model setup | `/claude-slicc-seed.js` in localStorage | `seven.mjs` over CDP, keyless, token in the address |
| Shell | SLICC's virtual shell | a WebAssembly Linux-like kernel, as user `cone` (no node, no python) |
| Tools | `bash`, `read_file`, `write_file`, `edit`, SLICC's own | `bash`, `read`, `write`, `edit`, `codemode`, `memory_write`, `workflow`, `tool_search`, licks |
| Events in the chat | `[time] sender: …` | `<lick id="lk-…" channel="…">…</lick>`; neither starts a chat job |
| Gate | the endpoint, on every step | the endpoint, plus the owner tier for every call (nested ones too) |
| Network | your network, signed-out Chrome | public addresses only, through the link; loopback and the LAN refused except the endpoint |
| Stop | kill the node CLI and its Chrome | `slicc abort`, then end `slicc`, which closes its Chrome |
| A fresh job | wiped Chrome profile | wiped seven profile, about two minutes to the first step |

**Hosts the launcher's seven contacted** in a job from a fresh profile (2026-11-18, every page, worker and service
worker of its Chrome, over CDP): `seven.sliccy.ai`, `vault.sliccy.ai`, `registry.npmjs.org`, `api.github.com`,
`raw.githubusercontent.com`, `cdn.jsdelivr.net`, `use.typekit.net`, `static.cloudflareinsights.com` (Cloudflare's
analytics beacon, from the hosted page; seven has no switch for it), and the job's own pages. The model calls go over
the link to `127.0.0.1:8660`. `SLICC_NO_TELEMETRY=1` and `SLICC_NO_UPDATE_CHECK=1` are set wherever mcp-slicc runs
`slicc`.

**Not verified on seven:** chat jobs typed in seven (one started by itself once, and was fixed: see below), backend
`forward` and host `systemd` with mcp-slicc's own launcher scripts, and seven's own `sudo` dialog.

### Attached jobs on seven

Built and run live 2026-11-19. The job runs in the seven open in your own, signed-in Chrome; its browser commands
(`playwright-cli`) drive that Chrome through the SLICC extension. Nothing on the computer can reach that tab: Chrome
does not let a program attach to your everyday profile unless you turn on remote debugging, and asks you about each
connection when you do. So mcp-slicc keeps a **follower seven** of its own: a headless Chrome on a profile of its own
(`mcp-slicc-follow`, kept between jobs), which follows your seven through seven's follow link and which you allow
**View** once. View makes it an owner device: it can set the approval policy and answer approvals, and nothing else (no
Control: it cannot send, steer or stop anything; no Terminal). It can read your seven's chats.

**What you install and set up, once:**

1. **The SLICC extension** from the Chrome Web Store (`slicc`, id `akjjllgokmbgpbdbmafpiefnhidlmbgf`), 7.10.0 or later
   (browser automation in seven). It serves six and seven alike; the store updates it.
2. **Seven** at `https://seven.sliccy.ai/` in a tab of that Chrome. It needs no account and no sign-in. Six on
   `www.sliccy.ai` is a separate origin and stays as it is (seven's in-place upgrade on `www.sliccy.ai` would retire six
   in that browser; it is not needed).
3. **The `local` account** on this endpoint, keyless, with the token in the address: Settings → Accounts → Add a
   provider → Local or self-hosted, endpoint `http://127.0.0.1:8660/t/<token>/v1` (`python3 common.py --seven-endpoint |
   pbcopy` puts it on the clipboard without showing it), "Connect without a key"; then pick `claude-slicc` as the model.
4. **The follow link** in `~/.claude-slicc/seven-follow` (mode 0600): Network → Linked devices → Link another device →
   Open on another device, the `https://seven.sliccy.ai/#follow=…` link. It is a secret: anyone with it can ask to
   follow your seven (you still have to allow them).
5. **Allow View** for mcp-slicc's follower: start one attached job (below) and, while it waits, turn on **View** for
   "SLICC on macOS" under Network → Linked devices (leave Control and Terminal off). The grant is kept with the
   follower's profile, so later jobs need nothing from you. The first time, the follower also confirms seven's own
   "Follow a SLICC on another computer?" in its own page.

**A job.** `slicc_task` with `launch: "attach"` returns the `start_line`; `start_slicc.sh … attach` brings the endpoint
up at the code on disk and starts `attach_seven.sh`, which:

1. launches the follower (`slicc launch --profile mcp-slicc-follow --headless`) and waits until it follows with View
   (`seven.mjs follow`, a few seconds once its profile has the agent's client cached);
2. keeps your policy's `owner.tools` (`run/attach.policy.json`) and sets it to `user` (`seven.mjs policy`), through the
   follower's agent connection (`approvalPolicySet`, found on the follower page's heap over the follower's own CDP);
3. marks the job `owner_tier`, records `attach_tier` (`on: …` or `off: <why>`, shown by `slicc_status`), and runs
   `owner_watch.py --follower`: every `owner-tool` item of your seven (the job's steps, codemode's nested calls, any
   other cone or scoop, any provider) is answered with the change gate, through one `seven.mjs approvals` process that
   reads the items from the follower's replicated approvals and decides on the same connection;
4. when the job ends (done, failed, or `slicc_stop`, which also signals it), puts `owner.tools` back and closes the
   follower's Chrome. A restore that fails keeps `attach.policy.json`, so the next job puts back your own value, not
   `user`.

Then send `start_line` as a message in your seven's chat. The job is that chat from that message on, as on six.

**What is gated:** every step the endpoint gives the job's chat, by seven's rules (`slicc7`), with the attached-read
rule: `tab-list`, and a snapshot or screenshot of a tab the job did not open, are asked (live: three `tab-list` tries
by a job's model were refused by the classifier). With the tier on, everything else in your seven is filed and judged
by the same gate while the job runs. When the tier cannot be set (`attach_tier: off: …`: no follow link, no View, your
seven refused), nothing in your seven is changed: the job's chat is still gated at the endpoint, codemode is stopped,
and calls on another provider or in another chat are not gated. Seven's `sudo` stays seven's own dialog.

**After a job.** A call of the job's chat that comes after the job ended (seven finishing a turn after `slicc_stop`)
is answered "mcp-slicc's job … has ended" and starts nothing: since 2026-11-19 a chat job starts only from a message the
person just sent (the call ends with it) that is not a start line. Before that fix, a stopped job's next call started a
chat job whose instruction was the start line, which then ran without the owner tier.

**What you see in your seven:** the follower under Linked devices ("Watching"); for each step the follower answered, a
dialog "Answered elsewhere … wants to run bash for you" with Close, which stays until closed; the tabs the job opened,
which it leaves open unless told to close them; and seven's own side calls to the model (tool labels), which get an
empty answer.

**Stop** ends the job and the follower. Your seven and your Chrome are never stopped, reloaded or signalled.

Live on 2026-11-19 (seven in a person's own Chrome, extension 7.15.0, slicc-agent 8.1.1): a read-only job on
example.com; a gated `open` asked and allowed with `slicc_decide` (approvals `operator` for that job); a manual job
whose write to `~/.pi/agent/settings.json` was refused and never reached seven; `slicc_stop` after the first step;
each with the tier on during the job and `owner.tools` back to `off` after it, read again through a fresh follower.

## The tools

| Tool | What it does |
|---|---|
| `slicc_task` | start a job. `mode: auto` (default): `claude -p` picks SLICC's steps. `mode: manual`: you pick them. `launch: launcher` (default) or `attach` (below) |
| `slicc_wait` | follow the job for up to 40 s: `working`, `question`, `step_done` or `done` |
| `slicc_decide` | answer a question of an auto job. With an allow, repeat the question's `wants` text in `wants` |
| `slicc_tell` | add a note. An auto job's model reads it with its next step; in a manual job it comes back with your next result |
| `slicc_stop` | stop the job, and the SLICC mcp-slicc launched. The SLICC in your own Chrome is left alone |
| `slicc_status` | the job, open questions, the last five steps with their results, and all notes. Read a result again here when a call was cut off |
| `slicc_bash`, `slicc_read_file`, `slicc_write_file`, `slicc_edit` | manual job: one step in SLICC's shell or workspace. Returns the result (waits up to 30 s, then `slicc_wait` has it). `slicc_bash` takes a `purpose`, recorded with the step |
| `slicc_report` | manual job: end it with your report |

Every step goes through the change gate first. `auto` steps run. `stop` steps are refused and never reach SLICC.
`ask` steps are the ones the gate does not pass by itself: opening a page, `curl`, `node`, a write outside SLICC's
scratch space. Who decides those is the approval setting.

## The change gate

`slicc_gate.py` judges each step SLICC's model wants, with the lists in `slicc_gate.json`:

- **Runs:** plainly read-only commands (`ls`, `cat`, `grep`, `jq` ... in a pipeline or list, with no redirect,
  substitution or here-doc), browser reads (`playwright-cli snapshot`, `screenshot`, `tab-list`), `--help`, opening
  `about:blank`, reads in SLICC's workspace, and writes under `/tmp/` and `/workspace/jobs/`.
- **Never runs:** SLICC's own commands for its cloud, devices, credentials, controls and memory (`serve`, `mount`,
  `secret`, `sudo`, `npm`, `memory` and the rest of `stop_commands`), a command that carries something shaped like a
  secret or a card number, a page that is not http(s), and writes to SLICC's control and memory files.
- **Asked:** everything else: opening a page, a click, a fill, `curl`, `node`, a write elsewhere.

A broken or missing gate stops every step. The policy is read again when it changes on disk, so a rule change needs
no restart. To use another gate, set `"gate"` in `~/.claude-slicc/config.json` (or `CLAUDE_SLICC_GATE`) to a Python
file that offers `slicc_classify(tool, args)`, `_segments(command)` and `load_policy()`, as `slicc_gate.py`'s
docstring describes.

## Approvals

`"approval"` in `~/.claude-slicc/config.json`, or `CLAUDE_SLICC_APPROVAL`. The default is `classifier` (since
2026-11-10).

| Setting | A job start, a note, an allowed question, a gated manual step |
|---|---|
| `classifier` | the hook prints nothing, and Claude Code's own permission system decides. In a session in **auto mode** that is Claude Code's classifier, and nothing prompts. In a session in **default mode** Claude Code prompts you, as for any tool it has no rule for |
| `operator` | the hook allows it; the Claude session decides alone, and nothing else checks |
| `person` | the hook makes Claude Code ask you, showing the instruction, the exact step or the note |
| `external` | for the [operator CLI](#the-operator-cli): its caller checked the approval before it called (for example a broker that runs the CLI only after a person approved that exact job, note or answer), so the call runs. Through Claude Code the hook asks you, as in `person`, since nothing outside checked that call |

A permitted gated step in a manual job runs at once, and its result carries `"gate"` with the gate's reason and what
approved it. Reads, stops and reports are always allowed. A hook that cannot load or read the call asks a person.

**An auto job's gated steps in classifier mode** (2026-11-10): no question to the operator. A Claude model's step is a
real tool call in a headless `claude -p --permission-mode auto` session whose only tools are `slicc-step`'s four (no
built-in tools, no other MCP servers). Its PreToolUse hook runs the change gate: a step the gate passes is allowed, a
step it never allows is denied with the gate's reason, and a gated step gets no answer, so Claude Code's classifier
decides it with the step in front of it. A permitted call is recorded and handed to SLICC; a blocked one is shown to
the model, which takes another step or reports. A Qwen job's gated step gets one short headless session
(`CLAUDE_SLICC_APPROVE_MODEL`, sonnet) that makes that exact call, so the classifier judges it; blocked is stored and
replayed like a refusal. `calls.jsonl` has `approved_by` (`gate` or `classifier`) on each step, `step_blocked` rows,
and `approval` rows for Qwen. In `person`, `operator` and `external` mode an auto job's gated step still waits for
`slicc_decide`: in `operator` mode the headless path would let the job's own model approve its steps with nothing
checking. `CLAUDE_SLICC_AGENT_STEPS=0` brings the question back in classifier mode too.

The setting is a check on calls made through the tools. It is not a boundary: an approval is a file under
`~/.claude-slicc/`, and a session that writes files or runs shell commands can write it directly.

## Chat jobs: typing in SLICC starts a job

Built 2026-11-10. A message typed in a SLICC that uses this endpoint, while no job runs, starts an auto job for that
chat: `origin: chat`, `launch: attach`, the newest message as its instruction, the model picked in SLICC (or
`sonnet`). Its answer is the model's last message, in the chat. A second message in the same chat starts a new job
that still has the first exchange in front of it: the job is the chat from the message that started its first chat
job (`state/chat_anchors.json`), and nothing older is sent. While any job runs, another chat gets a short busy answer.
`slicc_status`, `slicc_wait` and `slicc_stop` work on a chat job from any Claude Code session.
`CLAUDE_SLICC_CHAT_JOBS=0` turns this off and brings back the "no job is running for this chat" hint. With approvals at
`classifier`, nobody is asked: the classifier decides each gated step, as above.

Live on 2026-11-10, launcher jobs that opened example.com and reported its title: on sonnet the gated
`playwright-cli open` went through with no operator and no prompt (`approved_by: classifier`, three decisions, $0.048
reported); on `qwen-3.8-medium` the approval session allowed it ($0.0102) and the job reported, with the Spark's
cache kept (1,088 of 1,253 on its second call).

### Chat jobs in your own Chrome (checked 2026-11-10)

Tried from a tab on SLICC's own join page, driven through Claude in Chrome, on `qwen-3.8-medium`. What it showed, and
what was changed the same day:

- **Only the person's messages start a job.** SLICC puts its own events in the chat as user messages
  (`[time] discovery:https://site/llms.txt: ...`, licks, scheduled tasks). Two discovery events had each started a job
  that fetched the site's `llms.txt`. Now a message starts a job only when its sender is `User`
  (`common.sent_by_person`); an event gets an empty answer and nothing runs. An event that lands in a chat while a
  job is already running is passed to the model marked as SLICC's own (`common.EVENT_LABEL`: data, not a request),
  never as the person's words (2026-11-17).
- **A chat job has its own brief,** [chat-brief.md](chat-brief.md): answer the person directly, work in tabs you
  opened, close them when done. With the job brief, the answers read as reports to an operator.
- **Every job is told how SLICC's shell drives the browser** (`common.TOOL_GUIDE`, SLICC's own usage lines). Without it
  the model fetched a page with `curl` when asked to open a tab.
- **Tabs are known by Chrome's short number there** (`1899074098`), not a 32-character id. The job now recognises
  them as its own, so a snapshot of its own tab runs without a check, and closing a tab it opened is free in every job.
- **The Spark's cache carries across the questions of one chat.** A chat job's request is the chat's newest message,
  so it is no longer copied into the fixed system message. A follow-up question then reused 1,856 of 1,932 prompt
  tokens on its first call; before, 1,088 of 5,745.
- **The classifier does block.** While the tab was not yet recognised, the snapshot went to the classifier and was
  refused; the model was told, closed its tab and said so in its answer.

End to end after the fixes: "Open https://example.com in a new tab and tell me the page title" opened the tab (the one
gated step, approved by the classifier), read it, closed it and answered; a follow-up `ls /workspace` answered in the
same chat. An older chat keeps its old habits: the model copies how it answered earlier in that chat, so start a new
chat in SLICC after a change like this.

## Models: what SLICC's picker chooses

An auto job's steps run on your local `claude` CLI, logged in as you, or on Qwen on the Spark (below). Which model
and effort is decided in this order:

1. the `model` given to `slicc_task`, when one is given
2. the model picked in SLICC, when its name is a known model
3. `sonnet` (`CLAUDE_SLICC_MODEL`)

A name is a family (`fable`, `opus`, `sonnet`, `haiku`), then an optional version, then an optional effort (`low`,
`medium`, `high`, `xhigh`, `max`): `opus-5.5-xhigh`, `fable 5.2 high`, `sonnet`. Without a version the CLI's alias is
used, which is that family's latest. The endpoint lists these under `/v1/models`, and they are what to put in SLICC's
model field:

```text
fable-5.2-high, fable-5.2-xhigh, opus-5.5-medium, opus-5.5-high, opus-5.5-xhigh, sonnet-5.5-medium, sonnet-5.5-high, haiku-5.5,
qwen-3.8-low, qwen-3.8-medium, qwen-3.8-xhigh
```

`claude-slicc` names no model: the job's own is used (the launcher's SLICC sends it). Any other name the endpoint does
not know ends the job with a failure report and runs nothing, so a typo never runs on another model without saying so.
A manual job runs no model, so the picker changes nothing there. The screen is Haiku, or Qwen on the Spark with
`CLAUDE_SLICC_SCREEN_MODEL=qwen`. `calls.jsonl` records the model, the effort, the backend and where the choice came
from for every step.

### Qwen on the Spark: `qwen-3.8-low`, `qwen-3.8-medium`, `qwen-3.8-xhigh`

Built 2026-11-10. "The Spark" is a local OpenAI-compatible model server: `spark_url` in `config.json` (default
`http://localhost:8000/v1`) serving `spark_model` (default `qwen3.8-27b`), for example SGLang on an NVIDIA DGX Spark.
The names are the same family-version-effort form as the Claude ones, each one `reasoning_effort`: `qwen-3.8-low`,
`qwen-3.8-medium`, `qwen-3.8-xhigh`. Qwen 3.8 takes only those three efforts. Without a version, as with `sonnet`:
`qwen-medium`, `qwen xhigh`. Free and local; `cost_usd` is empty.

The step is a native tool call (`spark_turn.py`). Each request is built so it starts with the previous one, byte for
byte, which is what lets the server reuse its KV cache:

- one system message per job (the brief and the job), fixed at the job's first call in `spark.json`. No step counter;
  near the end of the budget the model gets one note instead
- the same four tools, in the same order, every call
- one model and one effort per job. A different pick in SLICC mid-job is logged (`route_change_ignored`) and ignored,
  because the effort changes the prompt and throws the cache away. A job that started on a Claude model can still
  move to a Qwen route; from then on it stays there
- SLICC's conversation, in order. A refused or denied step never reaches SLICC, so the endpoint keeps it (the tool
  call and its answer, and every note) in `spark_inserts.jsonl` and puts it back in the same place on every call
- each result clipped to 6,000 characters and labelled with the screen's first verdict for it, kept in `screens.json`,
  so a restart or a failed screen never relabels an old result
- nothing is cut from the front. Near the context window (85 % of `CLAUDE_SLICC_SPARK_CONTEXT`, 262,144 tokens by
  default, estimated) the job ends with a failure report

`calls.jsonl` has `prompt_tokens`, `cached_tokens` and `completion_tokens` for every Spark call, and `slicc_status` and
`slicc_wait` show `spark_tokens`: the job's totals, the last call's, and `prefix_broken`. Each row also has
`prefix_kept`: whether the request started with the job's last one, message for message. `prefix_kept: false` is a
break on mcp-slicc's side; a low `cached` with `prefix_kept: true` is the server's.

The server decides how much it reuses. Qwen 3.8 is a hybrid model, and SGLang
(`--mamba-radix-cache-strategy extra_buffer_lazy`, `--max-mamba-cache-size 24`) reuses a prefix only up to a point
where its recurrent state was kept. On a short prompt it keeps only part of it: the same xhigh request of about 1,200
tokens, sent twice 2 s apart, had 576 of 1,195 cached. So a short job shows low and varying `cached` at either effort,
and a long one reuses almost all of it. Live on 2026-11-10, second calls: `qwen-3.8-xhigh` 1,152 of 1,230 on a short
job and 7,552 of 7,628 on a long one, medium 1,088 of 1,192; one earlier short xhigh job had only 64. SGLang's own
measure is `sglang:cache_hit_rate`; `cached_tokens` is what the response reports.

If the Spark cannot be reached, answers an HTTP error or times out (`CLAUDE_SLICC_SPARK_TIMEOUT`), the job ends with a
failure report that names the Spark. Nothing falls back to a Claude model. Thinking (`reasoning_content`) is dropped;
only the first tool call of a reply is taken.

## Attached jobs: the SLICC in your own Chrome

Built 2026-11-10 on six, for searches that need your sign-ins; on seven see [Attached jobs on seven](#attached-jobs-on-seven),
whose setup replaces the six setup below. `slicc_task` with `launch: "attach"` starts nothing. It
returns a `start_line`, for example `Start claude-slicc job job-1a2b3c4d5e6f.` Send that line as a message in the
SLICC that is open in Chrome. The job is that chat from that message on; an older chat in the same tab, or any chat
without the line, is not the job and gets a one-line hint instead of a step. `slicc_stop` ends the job and never
touches that SLICC or your Chrome. `"launch": "attach"` in `~/.claude-slicc/config.json` makes it the default.

**Set up once, by you:** in that SLICC, under Accounts, add Local LLM (OpenAI-compatible) with base URL
`http://localhost:8660/v1`, the model names above, and the key from `~/.claude-slicc/token`
(`pbcopy < ~/.claude-slicc/token` copies it). The key is a local password for the endpoint, not an Anthropic key;
Claude does not enter it for you. As of 2026-11-10 the app's telemetry switch was not off (`telemetry-disabled`).

**What is different from a launcher job.** The browser is yours, signed in. Clicks, fills and scripts are steps like
any other: gated, and decided by the approval setting. Two things are stricter:

- Reads of tabs the job did not open are gated too. `tab-list`, and a `snapshot` or `screenshot` without `--tab` or of
  another tab, go to the approval setting. Reads of tabs the job opened with `playwright-cli open` stay free.
- Nothing else. There is no site list, and no step is refused that a launcher job would allow.

**What nothing here can see.** A click is `playwright-cli click e12`. The gate, the hook and Claude Code's classifier
see that text and the `purpose` the operator wrote, not what `e12` is on the page. Signed in to a shop, a click can
buy. Page text from signed-in sites goes into model calls (the screen, and an auto job's steps) and is stored in plain
text under `~/.claude-slicc/jobs/`. The hosted SLICC app is not pinned to a version, and it holds the endpoint key.

Checked on 2026-11-10: from a page on `https://www.sliccy.ai`, a GET and a preflighted POST with an Authorization
header both reached the endpoint and could read its answer. Not checked: whether the app's `?prompt=` URL parameter
can deliver the start line.

## Backends: one per install

Built 2026-11-14. Each install has one backend, set in `config.json` (or the environment), and never mixes two. Three
seams:

| Seam | Default: the Mac install above | The other choice |
|---|---|---|
| **Brain** (`backend`): who picks SLICC's steps | `local`: `claude -p`, or Qwen on the Spark, with the gate and the approvals above | `forward`: every SLICC call goes to one upstream agent endpoint, which picks and gates the steps |
| **Operator**: who starts and follows jobs | the `slicc` MCP tools in Claude Code, with the hook | the operator CLI `slicc_op.py`, usually with approvals set to `external` |
| **Host** (`host`): how SLICC starts and stops | `macos`: `start_slicc.sh` and `stop_slicc.sh` | `systemd`: named units |

The defaults behave as before 2026-11-14. The seams are independent: a Mac can forward, and a systemd host can run
`local`.

### The forward backend

SLICC still calls this endpoint, with this endpoint's key (`token`). The endpoint sends each call on to the upstream
and holds SLICC's call until the upstream answers.

- **Where:** `forward_url`. `unix:/path/to/agent.sock` sends calls to `/v1/chat/completions` and `/v1/models` on that
  socket; an `http://` or `https://` base URL ends in `/v1`, as an OpenAI client takes it.
- **What the upstream gets:** SLICC's request body, byte for byte, as `POST …/v1/chat/completions`, with these headers:

  | Header | Value |
  |---|---|
  | `Authorization` | `Bearer <forward token>`, from `forward_token_file`. SLICC's key to this endpoint is never sent on |
  | `Content-Type` | `application/json` |
  | `X-Slicc-Call` | `fwd-` and 12 hex digits, new for every call, and in `calls.jsonl` |
  | `X-Slicc-Job` | the running job's id (`job-…`). Absent when no job runs |
  | `X-Slicc-Conversation` | `job` for the job's own conversation (a launcher job's: its first user message is the launch prompt), `other` for SLICC's side calls (titles, memory). Absent when no job runs |

- **What it answers:** an OpenAI chat completion, as JSON or as a server-sent event stream. SLICC gets the message's
  text and tool calls, in the form it asked for.
- **Held calls:** up to `forward_timeout` (3600 s). A streaming SLICC gets keepalives meanwhile.
- **Hang-ups:** when SLICC closes its connection, the job is stopped, or `forward_timeout` passes, the endpoint shuts
  the upstream connection down. That is the upstream's sign that its answer is no longer wanted; nothing it sends after
  that reaches SLICC. A hang-up ends nothing else: SLICC's next call is forwarded as usual.
- **Hard stops, and nothing else:** the upstream gates every step. The endpoint checks each tool call in an answer with
  the change gate and acts only on `stop` (the never-run list, a secret in the step, a page that is not http(s) ...),
  and on a tool call whose arguments are not a JSON object: that step is not handed to SLICC, and the job ends with a
  failure report. A step the gate would ask about is passed on. Nothing is ever asked here.
- **The job record:** in the job's own conversation, an answer without a tool call is the job's report (`report.json`,
  status `done`), as it is SLICC's final answer; with the response header `X-Slicc-Report: failed`, a failure report.
  An upstream that cannot be reached (3 tries, 1 s apart), refuses the token (HTTP 401 or 403), answers an HTTP error
  or no completion, or does not answer within `forward_timeout`, ends the job with a failure report that says which.
- **Notes:** `slicc_tell` appends to the job's `steer.jsonl`, `{"ts", "text", "ref"}` per line; the upstream reads it.
- **Questions:** the endpoint never asks, but the upstream may want a person's yes for a step. It writes the question
  as one file `<id>.json` in a `forward_asks` folder (`{job}` in the folder is the job id), with at least
  `{"id": "ma-<12 hex digits>", "job": "job-…"}` and, for the operator to show, `tool`, `detail` (what is to be
  allowed), `reason` and `ts`. The id is two lowercase letters, a dash and 12 hex digits, and is also the file's name.
  `slicc_wait` and `slicc_status` show it with `"from": "upstream"`. `slicc_decide` writes the answer to
  `jobs/<job>/decisions/<id>.json`, `{"answer": "allow" | "deny", "ts", "by", "ref"}`, which the upstream reads, and
  `slicc_stop` answers every open one `deny`. Removing the file withdraws the question. Only a running job has open
  questions.
- **Not in forward:** manual jobs (`slicc_task` refuses them), chat jobs started from SLICC (a call while no job runs
  is forwarded without `X-Slicc-Job`, and the upstream decides what to do with it), the screen, the step budget and the
  model names. `/v1/models` lists the upstream's models, each with this endpoint's `version`, or `claude-slicc` alone
  when the upstream cannot be asked.

### The operator CLI

`slicc_op.py` runs one operator function and prints its result as JSON: exit 0 with the object the tool returns,
exit 1 with `{"error": "..."}`, exit 2 for a usage error. `--home`, before the command, is the state folder.

```text
python3 slicc_op.py [--home DIR] task "<instruction>" [--mode auto|manual] [--launch launcher|attach] [--model M] [--ref R]
python3 slicc_op.py [--home DIR] wait <job> [--seconds N]                  default 40, up to 600
python3 slicc_op.py [--home DIR] decide <ask> allow|deny [--wants TEXT] [--ref R]
python3 slicc_op.py [--home DIR] tell <job> "<note>" [--ref R]
python3 slicc_op.py [--home DIR] stop <job>
python3 slicc_op.py [--home DIR] status
python3 slicc_op.py [--home DIR] bash "<command>" [--purpose P]              manual jobs, as slicc_bash
python3 slicc_op.py [--home DIR] read-file <path>
python3 slicc_op.py [--home DIR] write-file <path> [--content TEXT]          stdin without --content
python3 slicc_op.py [--home DIR] edit <path> --edits '[{"oldText": "…", "newText": "…"}]'
python3 slicc_op.py [--home DIR] report "<text>"
```

`--ref` is kept as `ref` with the job (`state/job.json`), the note or the answer: for example the id of the approval
the caller checked. The CLI works under `python3 -I`. There is no hook: whatever the approval setting, a call runs as
the tool would once the hook allowed it. With `external` that is the design; with the others, whoever runs the shell
decides.

### The systemd host

`"host": "systemd"`, with `systemd_units`. For a launcher job the operator writes `run/slicc.job` (the job id) and
`run/slicc.env` (`SLICC_JOB=…` and `SLICC_PROMPT="…"`, for a unit's `EnvironmentFile=`), then runs
`systemctl restart` on the units, so SLICC starts fresh. If that fails, the job ends `failed` and `slicc_task` says
why. `slicc_stop` runs `systemctl stop`, and `slicc_status` adds `"slicc"`: `systemctl is-active` of the first unit.
`systemctl` in `config.json` sets another command, for example `["sudo", "-n", "systemctl"]`.

The unit is yours: it starts SLICC with the launch prompt (`Start the job described under The job in your
instructions.`), on a fresh profile, with its model pointed at this endpoint. The operator starts nothing else here:
run the endpoint and the UI as services of their own, and restart the endpoint after an update (the version check in
`start_slicc.sh` is macOS only).

### The state folder

Everything is under `CLAUDE_SLICC_HOME` (`--home` for the CLI), `config.json` included, so the folder decides the
install. For an upstream that reads the job record as another user, or a root operator that writes job folders the
endpoint must write into:

- `state_mode` (octal, for example `"0644"`): the mode of the files written there; folders get `x` where they have
  `r`. Without it, JSON records are `0600` and the rest follows the umask, as before. `token` is always `0600`.
- `state_owner` (a user name): a process that runs as root makes what it writes there that user's. Set it when a root
  broker runs the CLI and the endpoint runs as that user.

Keep the folder and its `config.json` unwritable for the upstream's user: a job, a note and an answer are files there,
so whoever can write the folder can approve.

### Example: Linux with systemd and forward

Placeholders throughout: `/opt/mcp-slicc` is a clone of this repo at a pinned commit, `/var/lib/mcp-slicc` the state
folder, owned by `slicc-runner`, the user the endpoint runs as; `/run/agent/agent.sock` the upstream's socket,
`/etc/mcp-slicc/forward-token` its token, and `slicc-worker.service` the unit that runs SLICC.

`/var/lib/mcp-slicc/config.json`:

```json
{
  "backend": "forward",
  "forward_url": "unix:/run/agent/agent.sock",
  "forward_token_file": "/etc/mcp-slicc/forward-token",
  "forward_asks": "/var/lib/agent-asks/{job}",
  "host": "systemd",
  "systemd_units": ["slicc-worker.service"],
  "approval": "external",
  "state_mode": "0644",
  "state_owner": "slicc-runner"
}
```

The endpoint, as a service of its own:

```ini
[Service]
User=slicc-runner
Environment=CLAUDE_SLICC_HOME=/var/lib/mcp-slicc
ExecStart=/usr/bin/python3 -I /opt/mcp-slicc/endpoint.py
Restart=on-failure
```

A root broker, once it has checked a person's approval for exactly this job:

```bash
/usr/bin/python3 -I /opt/mcp-slicc/slicc_op.py --home /var/lib/mcp-slicc task "List the public repos of example-org." --ref approval-123
```

The upstream reads `state/job.json` and, per job, `steer.jsonl`, `stop`, `report.json` and `decisions/`, and writes
only its own questions folder.

## Setup

1. **Install SLICC seven** as in [SLICC seven](#slicc-seven), and **SLICC six** as the fallback, where the scripts look
   for it, with install scripts off. Six was built and tested with `sliccy` 6.258.2:
   ```bash
   npm pack sliccy@6.258.2        # check the tarball's sha256 before you install it
   npm install --prefix ~/.claude-slicc/slicc --ignore-scripts sliccy-6.258.2.tgz
   ```
2. **Get this repo**, for example `git clone https://github.com/TechBlueprints/mcp-slicc.git ~/mcp-slicc`.
3. **Register the tools** with Claude Code (`--scope user` puts them in every project):
   ```bash
   claude mcp add --scope user slicc -- python3 ~/mcp-slicc/slicc_mcp.py
   ```
4. **Add the hook** to `~/.claude/settings.json`, merged with what is there, with the absolute path of your clone:
   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "mcp__slicc__.*",
           "hooks": [
             {"type": "command", "command": "python3 /path/to/mcp-slicc/gate_hook.py", "timeout": 30}
           ]
         }
       ]
     }
   }
   ```
5. **Log the CLI in** (`claude auth login`). Auto jobs and the screen need it. A turn that fails with "OAuth session
   expired" ends the job with a failure report.
6. **Optional: `~/.claude-slicc/config.json`** for the approval, the launch default, your own gate or brief, and the
   Spark (below).

Nothing is started by hand. `start_slicc.sh` runs for each job: it stops the SLICC of an earlier job, starts the
endpoint and the UI if they are down, restarts an endpoint that runs older code than the files on disk, wipes
mcp-slicc's Chrome profile and starts SLICC with the job's prompt. To restart the endpoint and the UI yourself, for
example after moving the repo, when no job is running:

```bash
./stop_slicc.sh servers && ./start_slicc.sh servers
```

**After a change to `slicc_mcp.py`, restart Claude Code.** The server loads its code when a session starts. The hook
and the scripts are read fresh on every use, and the endpoint is restarted at the next job start.

## Settings

### `~/.claude-slicc/config.json`

Read on every use by every part: the MCP server, the operator CLI, the hook Claude Code runs, the endpoint and the
headless step session. Use it for anything they must agree on. An environment variable of the same setting wins over it, but the
`env` of the MCP entry reaches only the MCP server and the endpoint it starts, not the hook.

| Key | Default | Meaning |
|---|---|---|
| `approval` | `classifier` | who decides a gated call: `classifier`, `operator`, `person` or `external` (above) |
| `launch` | `launcher` | where a job's SLICC is when `slicc_task` does not say: `launcher` or `attach` |
| `gate` | `slicc_gate.py` in this repo | the change gate (above) |
| `brief` | `job-brief.md` in this repo | what the model of a job handed over by the operator is told |
| `spark_url` / `spark_key` / `spark_model` | `http://localhost:8000/v1` / `spark-no-auth` / `qwen3.8-27b` | the Spark, for the Qwen routes |
| `backend` | `local` | the brain: `local` or `forward` ([Backends](#backends-one-per-install)). Any other value runs nothing |
| `forward_url` | none | `forward`: the upstream, `unix:/path/to.sock` or an http(s) base URL ending in `/v1` |
| `forward_token_file` | none | `forward`: a file holding the token sent upstream |
| `forward_timeout` | `3600` | `forward`: seconds one call is held for the upstream |
| `forward_asks` | none | `forward`: the upstream's question folders, one path or a list; `{job}` is the job id |
| `host` | `macos` | how SLICC starts and stops: `macos` or `systemd` |
| `systemd_units` | none | `systemd`: the units restarted for a job and stopped by `slicc_stop`, one name or a list |
| `systemctl` | `systemctl` | `systemd`: the command, a string or a list |
| `state_mode` | none (`0600` records) | the mode of files written under `CLAUDE_SLICC_HOME`, octal |
| `state_owner` | none | the user that owns what a root process writes under `CLAUDE_SLICC_HOME` |
| `slicc` | `7` | which SLICC a launcher job starts: `7` ([SLICC seven](#slicc-seven)) or `6` (the fallback). Any other value runs nothing |

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_SLICC_HOME` | `~/.claude-slicc` | state, the token, the SLICC install and `config.json` (the CLI's `--home`) |
| `CLAUDE_SLICC_BACKEND` | `config.json`, then `local` | the brain |
| `CLAUDE_SLICC_FORWARD_URL` / `_TOKEN_FILE` / `_TIMEOUT` / `_ASKS` | `config.json` | `forward`'s settings (`_ASKS` is `:`-separated) |
| `CLAUDE_SLICC_FORWARD_TOKEN` | none | the token sent upstream itself, instead of a file |
| `CLAUDE_SLICC_HOST_KIND` | `config.json`'s `host`, then `macos` | the host (`CLAUDE_SLICC_HOST` is the endpoint's bind address) |
| `CLAUDE_SLICC_SYSTEMD_UNITS` / `_SYSTEMCTL` | `config.json` | `systemd`'s units (comma-separated) and command |
| `CLAUDE_SLICC_STATE_MODE` / `_STATE_OWNER` | `config.json` | the state folder's file mode and owner |
| `CLAUDE_SLICC_APPROVAL` | `classifier` | overrides `config.json`'s `approval` |
| `CLAUDE_SLICC_LAUNCH` | `launcher` | overrides `config.json`'s `launch` |
| `CLAUDE_SLICC_GATE` / `_BRIEF` / `_CHAT_BRIEF` | `config.json`, then this repo's files | the gate, the job brief, the chat brief |
| `CLAUDE_SLICC_BILLING` | `subscription` | `subscription` removes `ANTHROPIC_API_KEY` from the claude child, so it bills to the login. `api` keeps the key |
| `CLAUDE_SLICC_BIN` | `claude` | the claude CLI |
| `CLAUDE_SLICC_MODEL` | `sonnet` | model for an auto job's steps (`slicc_task` can pass `opus` or `haiku`) |
| `CLAUDE_SLICC_BUDGET` | `40` | steps per job; the next step after that ends the job with a report |
| `CLAUDE_SLICC_SCREEN` | `1` | label results that read as text addressed to an AI (one Haiku call per new result) |
| `CLAUDE_SLICC_CHAT_JOBS` | `1` | a message in a SLICC with no job starts a chat job; `0`: the hint |
| `CLAUDE_SLICC_AGENT_STEPS` | `1` | in classifier mode, a step is a tool call in a headless session judged by the classifier; `0`: the question path |
| `CLAUDE_SLICC_APPROVE_MODEL` | `sonnet` | the short session that puts a Qwen job's gated step to the classifier |
| `CLAUDE_SLICC_SCREEN_MODEL` | `haiku` | `qwen` screens on the Spark at medium effort, free, in its own short request |
| `CLAUDE_SLICC_SPARK_URL` / `_KEY` / `_MODEL` | `config.json`, then as above | the Spark, for the Qwen routes |
| `CLAUDE_SLICC_SPARK_TIMEOUT` / `_CONTEXT` | `900` / `262144` | seconds for one Spark call; the Spark's context window in tokens |
| `CLAUDE_SLICC_SCHEMA` | `1` | `--json-schema` for an auto job's step. `0` reads the step from the reply text |
| `CLAUDE_SLICC_ORIGINS` | `http://localhost:5711,https://www.sliccy.ai,https://seven.sliccy.ai` | the page origins that may call the endpoint |
| `CLAUDE_SLICC_SLICC` | `config.json`, then `7` | which SLICC a launcher job starts |
| `CLAUDE_SLICC_SEVEN_BIN` / `_SEVEN_PROFILE` / `_SEVEN_BROWSER` | the install under `CLAUDE_SLICC_HOME` / `mcp-slicc` / `chrome` | seven's `slicc`, its profile name and the browser it launches |
| `CLAUDE_SLICC_OWNER_TIER` | `user` | `off`: no owner tier and no watcher, and codemode stays stopped |
| `CLAUDE_SLICC_SEVEN_LINK_WAIT` / `_SEVEN_SEED_WAIT` | `180` / `300` | seconds for seven to link, and to install its agent and take the seeding |
| `CLAUDE_SLICC_SEVEN_FOLLOW` | `$CLAUDE_SLICC_HOME/seven-follow` | an attached job's follow link to your seven (mode 0600) |
| `CLAUDE_SLICC_SEVEN_FOLLOWER_PROFILE` / `_SEVEN_FOLLOWER_HEADLESS` / `_SEVEN_FOLLOW_WAIT` | `mcp-slicc-follow` / `1` / `180` | the follower's kept profile, whether its Chrome is headless, and seconds for it to follow with View |
| `CLAUDE_SLICC_CONTEXT` | `200000` | the context window `/v1/models` reports for the Claude names (the Qwen names report the Spark's) |
| `CLAUDE_SLICC_PORT` / `_UI_PORT` | `8660` / `5711` | the endpoint and the local UI |
| `CLAUDE_SLICC_START_CMD` / `_STOP_CMD` | `start_slicc.sh {job} {prompt}` / `stop_slicc.sh` | start and stop SLICC for a job; `""` disables |
| `CLAUDE_SLICC_NODE` / `_NODE_CLI` / `_UI_ROOT` | `node` / the install under `CLAUDE_SLICC_HOME` | SLICC's node, its CLI and its bundled UI |
| `CLAUDE_SLICC_ASK_WAIT` | `1800` | seconds an auto job's question waits before it expires |
| `CLAUDE_SLICC_STEP_WAIT` | `30` | seconds a manual step tool waits for SLICC before it returns `working` |
| `CLAUDE_SLICC_MANUAL_IDLE` | `3600` | seconds a manual job waits for a step before it ends with a failure report |

## When SLICC hangs up

The endpoint holds SLICC's call while a model decides, a question is open, or a manual job waits for a step. It
watches that connection. When SLICC closes it:

- a manual step is not handed to the caller that left; it goes to SLICC's next call
- an open question is withdrawn, so nobody is asked about a step that can no longer run. An unanswered question also
  expires after `CLAUDE_SLICC_ASK_WAIT`
- a step whose answer could not be written, or that SLICC's next call does not contain, is marked `lost`. The step tool
  or `slicc_wait` reports it as not run. If its result arrives after all, the step turns `done` and is reported again
- with backend `forward`, the upstream call is shut down, so the upstream knows (above)

## SLICC on your Mac

- **Its own profile.** SLICC's Chrome is your installed Google Chrome on `~/.claude-slicc/chrome-profile`
  (`SLICC_USER_DATA_DIR`), wiped before each job. Its window is visible. The default SLICC profile in
  `~/Library/Application Support/Slicc` is never touched.
- **Shared with a SLICC you run yourself:** `~/.slicc` (logs, a session id, and `secrets.env` if you create one) and
  port 5710.
- **Telemetry.** The seed sets `telemetry-disabled`, and the hosted app is never opened. On 2026-11-09 a run without
  the local UI sent requests to `rum.hlx.page` (Adobe's real-user monitoring) before it was closed; jobs since show
  only `localhost` and the pages they opened. To check a job:
  `grep -oE "https?://[a-zA-Z0-9.-]+" ~/.claude-slicc/jobs/<job>/slicc.log | sort | uniq -c`.
- **SLICC's own sudo** is at its default, which allows its shell. Here the gate and the approval setting are the only
  checks.
- **Left running.** The endpoint and the UI stay up between jobs. SLICC stops on `slicc_stop`; a finished job does not
  stop it.
- **Don't run SLICC's CLI by hand to see its help.** `node …/index.js --help` starts the server and a browser on the
  hosted app.

## Where things are

`~/.claude-slicc/`: `config.json` (settings, when set), `token` (the endpoint's key), `slicc/` (six's install),
`slicc7/` (seven's `slicc`), `seven-follow` (the follow link to your seven, for attached jobs),
`chrome-profile/`, `run/` (the running SLICC's job id and, with host `systemd`, `slicc.env`; for an attached seven job
`attach.pid`, `follower.pid`, `follower.cdp` and `attach.policy.json`, your `owner.tools` until it is put back), `side_calls.jsonl` (calls
that belong to no job), `state/job.json` (the current job), and `jobs/<job>/`.

`state/job.json` is the job record: `id`, `instruction`, `model`, `model_explicit`, `mode` (`auto` or `manual`),
`launch` (`launcher` or `attach`), `budget`, `status` (`running`, `done`, `failed` or `stopped`), `started`, `ended`,
and since 2026-11-14 `backend`, `approval` (the setting it was started under) and `ref` (when the CLI passed one). A
chat job also has `origin: chat` and `anchor`. A seven job has `slicc: "7"` and, once the tier is on, `owner_tier`; an
attached seven job also `attach_tier`. In a job's folder:

| File | What |
|---|---|
| `calls.jsonl` | every model decision and its reported cost, every step with its gate verdict and what approved it, screens, dropped calls, lost steps. With `forward`: each forwarded call (`call`, or `side_call`, with `backend: forward`, its `call` id, `action` and `duration_ms`) and each step it handed over (`decision`: `upstream` or `stop`, `gate`: the gate's own verdict) |
| `steps/` | a manual job's steps, one file each: the step, its status, its result and the screen's verdict |
| `asks/`, `decisions/` | questions and their answers (`allow`, `deny`, `withdrawn`, `expired`) |
| `steer.jsonl`, `stop`, `report.json`, `slicc.log` | notes, the stop flag, the report, SLICC's own log (launcher jobs) |
| `tabs.json` | the tabs the job opened, read from what its own `open` commands printed |
| `seven.events`, `seven.err`, `seven.seed.json`, `owner_watch.log` | SLICC seven: `slicc`'s lifecycle events, its errors, the seeding's answer, the watcher's errors |
| `handed.jsonl` | SLICC seven: each step the endpoint handed seven (tool and arguments), so the owner tier's item for it is answered without a second check |
| `attach.log`, `follower.events` | an attached seven job: what attach_seven.sh did (tier on or off and why, the policy put back, the follower closed) and `slicc launch`'s events for the follower |
| `spark.json`, `spark_inserts.jsonl`, `screens.json` | a Qwen job: its fixed route and system message, the endpoint's own messages with their places, the screen's verdict per result |

## Not verified

- **The classifier refusing a call.** Only calls it permitted were seen (2026-11-10). A block in the headless step
  session was tested with a stand-in classifier only, so what the model sees then is not checked.
- **Extra tool calls after a step.** Live, a headless session sometimes made further calls after its step was
  recorded; the step server refuses them, and they cost time and tokens.
- **Auto jobs on real pages.** Each result is cut to 6,000 characters in the model's transcript and images are dropped.
- **SLICC's own request timeout.** The hang-up handling was tested with a closed socket, not with SLICC giving up.
- **Long jobs.** The transcript goes to `claude -p` as an argument, capped at 120k characters.
- **Cost.** `calls.jsonl` records `cost_usd` per decision. On a subscription login it is an estimate, not a bill.
- **The Spark's cache on short prompts** is partial and varies (above). Not fixable from the request.
- **The Qwen routes beyond a two-call `ls` job,** and `CLAUDE_SLICC_SCREEN_MODEL=qwen`: tested with a fake Spark only.
- **The bundled gate live.** The live checks above ran with an earlier change gate whose SLICC rules `slicc_gate.py`
  copies; the two gave the same verdict on 72 sample steps (2026-11-14).
- **Backend `forward` against a real upstream, and host `systemd` on a real system.** Both were tested with stand-ins
  only (a fake upstream on a Unix socket and on TCP, a stand-in `systemctl`); `state_owner` with a stand-in for root.

## License

Apache License 2.0: see [LICENSE](LICENSE).
