#!/usr/bin/env node
// mcp-slicc's hands on SLICC seven's page (2026-11-18, README "SLICC seven"). Node 22 or newer, no packages.
//
//   node seven.mjs seed --cdp <http://127.0.0.1:port> --endpoint <url> [--model local/claude-slicc]
//                       [--owner-tier user|off] [--agent-major 8] [--timeout 300]
//
// The launcher's seven has no account, model or approval policy until a person sets them in Settings, and the `slicc`
// CLI has no command for any of them. mcp-slicc holds the CDP endpoint of the Chrome it launched, so it sets them on
// the page itself, through the same objects seven's own Settings use: the page's settings store (`connect`, which the
// "Connect without a key" button calls, and `update({ model })`, the model picker) and the agent connection's
// `approvalPolicySet`, an owner-device method (slicc-agent's README, "Approval policy"). They are found on the page's
// heap by the methods they offer (CDP Runtime.queryObjects), since seven keeps them in module scope.
//
// It prints one JSON line: {"ok": true, ...} or {"ok": false, "error": "<one line>"} with exit status 1. Nothing is
// clicked or typed, and no key is entered anywhere: the `local` account is keyless and the endpoint's token is in its
// path (common.seven_endpoint).
//
// An attached job (2026-11-19, README "Attached jobs on seven"), on the page of mcp-slicc's follower seven, a Chrome of
// its own that follows the person's seven with View (attach_seven.sh):
//
//   node seven.mjs follow --cdp <url> --follow-file <path> [--timeout s]   follow the person's seven, wait for View
//   node seven.mjs policy --cdp <url> --owner-tier user|agent|off           set owner.tools; prints it before and after
//   node seven.mjs approvals --cdp <url>     pending items as NDJSON; stdin lines {"id","answer","reason"?} decide them
//   node seven.mjs decide --cdp <url> <id> once|deny [--reason text]        one answer
//
// The follow link is read from its file and never printed. The only click is on the follower's own "Follow"
// confirmation, in mcp-slicc's Chrome; the person's seven is reached only through the follower's agent connection.

const args = process.argv.slice(2);
const cmd = args.shift();
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : dflt;
};
const SEVEN_ORIGIN = opt('origin', 'https://seven.sliccy.ai');

function done(obj, code = 0) {
  process.stdout.write(JSON.stringify(obj) + '\n');
  process.exit(code);
}
const fail = (error) => done({ ok: false, error }, 1);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const USAGE = `usage:
  seven.mjs seed --cdp <url> --endpoint <url> [--model id] [--owner-tier user|off]
  seven.mjs follow --cdp <url> --follow-file <path> [--timeout s]
  seven.mjs policy --cdp <url> --owner-tier user|agent|off
  seven.mjs approvals --cdp <url>
  seven.mjs decide --cdp <url> <id> once|deny [--reason text]`;
if (typeof WebSocket !== 'function') fail(`seven.mjs needs Node 22 or newer (this is ${process.version})`);
if (!['seed', 'follow', 'policy', 'approvals', 'decide'].includes(cmd)) fail(USAGE);

const cdpBase = String(opt('cdp', '')).replace(/\/+$/, '');
const endpoint = opt('endpoint', '');
const model = opt('model', 'local/claude-slicc');
const ownerTier = opt('owner-tier', cmd === 'seed' ? 'user' : '');
const agentMajor = Number(opt('agent-major', '8'));
const deadline = Date.now() + Number(opt('timeout', cmd === 'seed' ? '300' : '120')) * 1000;
if (!/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(cdpBase)) fail(`--cdp must be a loopback http URL: ${cdpBase}`);
if (cmd === 'seed' && !endpoint) fail('--endpoint is required');
if ((cmd === 'seed' || cmd === 'policy') && !['user', 'off', 'agent'].includes(ownerTier)) {
  fail(`--owner-tier must be user, agent or off: ${ownerTier}`);
}

let ws;
let nextId = 0;
const pending = new Map();
function send(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
}

async function connectBrowser() {
  const version = await (await fetch(`${cdpBase}/json/version`)).json();
  ws = new WebSocket(version.webSocketDebuggerUrl);
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    const p = d.id && pending.get(d.id);
    if (!p) return;
    pending.delete(d.id);
    if (d.error) p.reject(new Error(d.error.message));
    else p.resolve(d.result);
  };
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error('the browser refused the debugging connection'));
  });
}

// One page on seven's origin, attached once.
async function sevenSession() {
  while (Date.now() < deadline) {
    const { targetInfos } = await send('Target.getTargets');
    const tab = targetInfos.find((t) => t.type === 'page' && t.url.startsWith(SEVEN_ORIGIN + '/'));
    if (tab) {
      const { sessionId } = await send('Target.attachToTarget', { targetId: tab.targetId, flatten: true });
      await send('Runtime.enable', {}, sessionId);
      return sessionId;
    }
    await sleep(1000);
  }
  throw new Error(`no SLICC seven tab (${SEVEN_ORIGIN}) appeared`);
}

async function evaluate(sid, expression) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sid);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description?.split('\n')[0] || r.exceptionDetails.text);
  return r.result.value;
}

// Seven's own objects on the page's heap, kept as globalThis.__mcpSlicc for the evaluations below.
const FIND = `function () {
  const ok = (f) => this.filter((o) => { try { return f(o); } catch { return false; } });
  const fn = (o, k) => typeof o[k] === 'function';
  const controls = ok((o) => fn(o, 'approvalPolicySet') && fn(o, 'send') && fn(o, 'newChat'));
  // The page's settings adapter: an instance (own addServer) with providers; not the transport variant (connects).
  const stores = ok((o) => fn(o, 'update') && fn(o, 'get') && fn(o, 'models') && fn(o, 'accounts') && fn(o, 'connect')
    && fn(o, 'providers') && Object.prototype.hasOwnProperty.call(o, 'addServer') && !('connects' in o));
  const services = ok((o) => fn(o, 'connect') && fn(o, 'signIn') && o.state && Array.isArray(o.state.value?.accounts));
  // The agent's replicated transcript (slicc-agent's AgentTranscript): its approvals and the approval policy.
  const transcripts = ok((o) => o.approvals && o.approvalPolicy && Array.isArray(o.approvals.value)
    && typeof o.approvalPolicy.subscribe === 'function' && o.approvalPolicy.value && o.approvalPolicy.value.owner);
  globalThis.__mcpSlicc = { controls, stores, services, transcripts };
  return [controls.length, stores.length, services.length, transcripts.length];
}`;

// need: which of [controls, settings, accounts, transcripts] must be found (all for the launcher's own seven; a
// follower has no settings store or accounts of its own, only the owner's agent connection).
async function findObjects(sid, need = [0, 1, 2]) {
  let last = [0, 0, 0, 0];
  while (Date.now() < deadline) {
    const proto = await send('Runtime.evaluate', { expression: 'Object.prototype' }, sid);
    const all = await send('Runtime.queryObjects', { prototypeObjectId: proto.result.objectId }, sid);
    const r = await send('Runtime.callFunctionOn',
      { objectId: all.objects.objectId, functionDeclaration: FIND, returnByValue: true }, sid);
    await send('Runtime.releaseObject', { objectId: all.objects.objectId }, sid).catch(() => {});
    last = r.result.value;
    if (need.every((i) => last[i] > 0)) return last;
    await sleep(3000);
  }
  throw new Error(`SLICC seven's agent did not start in time (found control ${last[0]}, settings ${last[1]}, accounts ${last[2]}, transcript ${last[3]})`);
}

async function main() {
  await connectBrowser();
  const sid = await sevenSession();

  // The agent seven installed, which carries the approvals and the tools mcp-slicc's gate was written for.
  let agent = '';
  while (Date.now() < deadline) {
    agent = await evaluate(sid, `fetch('/opt/agent/node_modules/@ai-ecoverse/slicc-agent/package.json')
      .then((r) => (r.ok ? r.json() : null)).then((p) => (p && p.version) || '').catch(() => '')`);
    if (agent) break;
    await sleep(3000);
  }
  if (!agent) throw new Error("SLICC seven's agent was not installed in time");
  if (Number(agent.split('.')[0]) !== agentMajor) {
    throw new Error(`SLICC seven runs slicc-agent ${agent}; mcp-slicc is built for ${agentMajor}.x. Set "slicc": 6 in mcp-slicc's config to use SLICC six until mcp-slicc is updated`);
  }
  await findObjects(sid);

  // The local account: keyless, on this endpoint (the "Connect without a key" button's call).
  const account = await evaluate(sid, `(async () => {
    const { stores, services } = globalThis.__mcpSlicc;
    const store = stores[0];
    const service = services.find((s) => !('release' in s)) || services[0];
    const local = () => service.state.value.accounts.find((a) => a.id === 'local');
    const wanted = ${JSON.stringify(endpoint)};
    if (local()?.status !== 'connected' || local()?.fields?.find((f) => f.name === 'endpoint')?.value !== wanted) {
      await store.connect('local', undefined, { endpoint: wanted });
    }
    for (let i = 0; i < 40 && !(local()?.status === 'connected' && service.state.value.models.some((m) => m.id.startsWith('local/'))); i++) {
      await new Promise((r) => setTimeout(r, 500));
    }
    const a = local();
    return { status: a?.status, models: service.state.value.models.filter((m) => m.id.startsWith('local/')).map((m) => [m.id, m.contextWindow]),
             others: service.state.value.accounts.filter((x) => x.id !== 'local' && x.status === 'connected').map((x) => x.id) };
  })()`);
  if (account.status !== 'connected') throw new Error(`SLICC seven's local account did not connect (${account.status})`);
  if (account.others.length) throw new Error(`this SLICC seven has other accounts connected (${account.others.join(', ')}), whose calls would not reach the gate`);

  const chosen = await evaluate(sid, `(async () => {
    const store = globalThis.__mcpSlicc.stores[0];
    store.update({ model: ${JSON.stringify(model)} });
    for (let i = 0; i < 20 && store.get().model !== ${JSON.stringify(model)}; i++) await new Promise((r) => setTimeout(r, 250));
    return store.get().model;
  })()`);
  if (chosen !== model) throw new Error(`SLICC seven kept the model ${chosen || '(none)'} instead of ${model}`);

  const policy = await setPolicy(sid, ownerTier);
  await evaluate(sid, 'delete globalThis.__mcpSlicc, true');
  done({ ok: true, agent, model: chosen, models: account.models, policy });
}

// owner.tools through the agent connection's approvalPolicySet, an owner-device method: the owner's own page, or a
// follower with View (slicc-agent's README, "Approval policy"). Answers { owner, sudo } of the policy now in force.
async function setPolicy(sid, tier) {
  const policy = await evaluate(sid, `(async () => {
    let last = 'no agent control';
    for (const c of globalThis.__mcpSlicc.controls) {
      try {
        const r = await c.approvalPolicySet({ owner: { tools: ${JSON.stringify(tier)} } });
        if (r && !r.error && r.policy) return { owner: r.policy.owner, sudo: r.policy.sudo };
        last = (r && r.error) || 'no policy in the answer';
      } catch (e) { last = e.message; }
    }
    return { error: String(last) };
  })()`);
  if (policy.error || policy.owner?.tools !== tier) {
    throw new Error(`SLICC seven did not take the approval policy owner.tools ${tier}: ${policy.error || JSON.stringify(policy.owner)}`);
  }
  return policy;
}

// ------------------------------------------------------------------------------------------------- a follower seven
//
// An attached job runs in the seven open in the person's own Chrome, which no program on the host can reach without
// remote debugging. mcp-slicc instead keeps a follower seven of its own (2026-11-19, README "Attached jobs on seven"):
// a Chrome it launches on its own profile follows the person's seven through seven's join link (`#follow=`), and once
// the person allowed it View in Linked devices it is an owner device, with the owner's agent connection: it may set
// the approval policy and answer approvals, and nothing more (no Control: it cannot send, steer or abort).

const FOLLOW_STATE = `(async () => {
  const app = document.querySelector('slicc-app');
  const f = app && app.model && app.model.follow;
  if (!f) return { role: app ? 'own' : 'booting' };
  const s = await f.state();
  return { role: 'follower', link: s.link, access: s.access, name: s.name };
})()`;

// The "Follow a SLICC on another computer?" confirmation is seven's own; a click it trusts is a real mouse event.
async function clickButton(sid, label) {
  const expr = `(() => {
    const hits = [];
    const walk = (root) => {
      for (const el of root.querySelectorAll('button, sp-button, swc-button, [role=button]')) {
        if ((el.textContent || '').trim() === ${JSON.stringify(label)}) hits.push(el);
      }
      for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
    };
    walk(document);
    const el = hits.find((e) => e.getClientRects().length);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  })()`;
  const at = await evaluate(sid, expr);
  if (!at) return false;
  for (const type of ['mousePressed', 'mouseReleased']) {
    await send('Input.dispatchMouseEvent', { type, x: at.x, y: at.y, button: 'left', clickCount: 1 }, sid);
  }
  return true;
}

async function anyPage() {
  const { targetInfos } = await send('Target.getTargets');
  const tab = targetInfos.find((t) => t.type === 'page' && t.url.startsWith(SEVEN_ORIGIN + '/'))
    || targetInfos.find((t) => t.type === 'page');
  if (!tab) throw new Error('the follower browser has no tab');
  const { sessionId } = await send('Target.attachToTarget', { targetId: tab.targetId, flatten: true });
  await send('Runtime.enable', {}, sessionId);
  await send('Page.enable', {}, sessionId);
  return { sid: sessionId, url: tab.url };
}

async function follow() {
  const file = opt('follow-file', '');
  if (!file) fail('--follow-file is required');
  const fs = await import('node:fs');
  let link;
  try { link = fs.readFileSync(file, 'utf8').trim(); } catch { fail(`no follow link in ${file}`); }
  if (!link.startsWith(`${SEVEN_ORIGIN}/#follow=`) && !link.startsWith(`${SEVEN_ORIGIN}/os/#follow=`)) {
    fail(`${file} does not hold a ${SEVEN_ORIGIN} follow link`);
  }
  await connectBrowser();
  const { sid, url } = await anyPage();
  // A profile that followed before goes on following after a restart; only a new one is sent the link.
  if (!url.startsWith(SEVEN_ORIGIN + '/')) await send('Page.navigate', { url: `${SEVEN_ORIGIN}/os/` }, sid);
  let st = { role: 'booting' };
  let sentLink = false;
  let since = Date.now();
  while (Date.now() < deadline) {
    await sleep(1500);
    try { st = await evaluate(sid, FOLLOW_STATE); } catch { st = { role: 'booting' }; }
    if (st.role === 'follower' && st.link === 'connected' && st.access?.view) break;   // View may come while it waits
    if (await clickButton(sid, 'Follow').catch(() => false)) { since = Date.now(); continue; }
    // Not following after 20 s (a new profile, or one that left): open the link. It is never printed.
    if (!sentLink && st.role !== 'follower' && Date.now() - since > 20000) {
      await send('Page.navigate', { url: link }, sid);
      sentLink = true;
      since = Date.now();
    }
  }
  if (!(st.role === 'follower' && st.link === 'connected') && !(st.role === 'follower' && st.access && !st.access.view)) {
    throw new Error(`mcp-slicc's follower did not link to your seven (${st.role}${st.link ? ', ' + st.link : ''})`);
  }
  if (!st.access?.view) {
    done({ ok: false, code: 'no-view', name: st.name,
      error: `mcp-slicc's follower ("${st.name}") waits for View: allow it in your seven's Linked devices` }, 1);
  }
  await findObjects(sid, [0, 3]);
  const policy = await evaluate(sid, `(() => { const t = globalThis.__mcpSlicc.transcripts[0]; return t.approvalPolicy.value.owner; })()`);
  await evaluate(sid, 'delete globalThis.__mcpSlicc, true');
  done({ ok: true, name: st.name, access: st.access, owner: policy });
}

async function followerSession() {
  await connectBrowser();
  const sid = await sevenSession();
  await findObjects(sid, [0, 3]);
  return sid;
}

async function policyCmd() {
  const sid = await followerSession();
  const before = await evaluate(sid, `globalThis.__mcpSlicc.transcripts[0].approvalPolicy.value.owner`);
  const policy = await setPolicy(sid, ownerTier);
  done({ ok: true, before, policy });
}

const decideExpr = (id, answer, reason) => `(async () => {
  let last = 'no agent control';
  for (const c of globalThis.__mcpSlicc.controls) {
    try { await c.decide(${JSON.stringify(id)}, ${JSON.stringify(answer)}, ${JSON.stringify(reason)}); return { ok: true }; }
    catch (e) { last = e.message; }
  }
  return { ok: false, error: String(last) };
})()`;

// Prints {"schema":1,"event":"approvals","approvals":[...]} with every pending item, as `slicc approvals --watch
// --json` does, each time the pending set changes; runs until the browser goes away (exit 1) or stdin closes (exit 0).
// Each stdin line {"id","answer":"once"|"deny","reason"?} is decided on the same connection, answered with a
// {"event":"decided",...} or {"event":"error",...} line: one process per job, not one per item.
async function approvalsCmd() {
  const sid = await followerSession();
  let last = null;
  ws.onclose = () => process.exit(1);
  const out = (o) => process.stdout.write(JSON.stringify({ schema: 1, ...o }) + '\n');
  const readline = await import('node:readline');
  const rl = readline.createInterface({ input: process.stdin });
  rl.on('line', async (line) => {
    let d;
    try { d = JSON.parse(line); } catch { return; }
    if (!d || typeof d.id !== 'string' || !['once', 'deny'].includes(d.answer)) {
      out({ event: 'error', id: d && d.id, message: 'a decision is {"id", "answer": "once"|"deny", "reason"?}' });
      return;
    }
    try {
      const r = await evaluate(sid, decideExpr(d.id, d.answer, typeof d.reason === 'string' ? d.reason : null));
      out(r.ok ? { event: 'decided', id: d.id, answer: d.answer } : { event: 'error', id: d.id, message: r.error });
    } catch (e) {
      out({ event: 'error', id: d.id, message: String(e.message || e) });
    }
  });
  rl.on('close', () => process.exit(0));
  for (;;) {
    const items = await evaluate(sid, `globalThis.__mcpSlicc.transcripts[0].approvals.value
      .filter((a) => a.state === 'pending')
      .map((a) => ({ id: a.id, kind: a.kind, detail: a.detail, tier: a.tier, requester: a.requester,
                     createdAt: a.createdAt, expiresAt: a.expiresAt }))`);
    const key = items.map((a) => a.id).join(',');
    if (key !== last) {
      last = key;
      process.stdout.write(JSON.stringify({ schema: 1, event: 'approvals', approvals: items }) + '\n');
    }
    await sleep(250);
  }
}

async function decideCmd() {
  const pos = args.filter((a, i) => !a.startsWith('--') && !(i > 0 && args[i - 1].startsWith('--')));
  const [id, answer] = pos;
  if (!id || !['once', 'deny'].includes(answer)) fail('usage: seven.mjs decide --cdp <url> <id> once|deny [--reason text]');
  const reason = opt('reason', null);
  const sid = await followerSession();
  const r = await evaluate(sid, decideExpr(id, answer, reason));
  if (!r.ok) fail(r.error);
  done({ ok: true, schema: 1, event: 'decided', id, answer });
}

const COMMANDS = { seed: main, follow, policy: policyCmd, approvals: approvalsCmd, decide: decideCmd };
COMMANDS[cmd]().catch((e) => fail(String(e && e.message ? e.message : e).split('\n')[0]));
