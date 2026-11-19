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

if (typeof WebSocket !== 'function') fail(`seven.mjs needs Node 22 or newer (this is ${process.version})`);
if (cmd !== 'seed') fail('usage: seven.mjs seed --cdp <url> --endpoint <url> [--model id] [--owner-tier user|off]');

const cdpBase = String(opt('cdp', '')).replace(/\/+$/, '');
const endpoint = opt('endpoint', '');
const model = opt('model', 'local/claude-slicc');
const ownerTier = opt('owner-tier', 'user');
const agentMajor = Number(opt('agent-major', '8'));
const deadline = Date.now() + Number(opt('timeout', '300')) * 1000;
if (!/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(cdpBase)) fail(`--cdp must be a loopback http URL: ${cdpBase}`);
if (!endpoint) fail('--endpoint is required');
if (!['user', 'off', 'agent'].includes(ownerTier)) fail(`--owner-tier must be user, agent or off: ${ownerTier}`);

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
  globalThis.__mcpSlicc = { controls, stores, services };
  return [controls.length, stores.length, services.length];
}`;

async function findObjects(sid) {
  let last = [0, 0, 0];
  while (Date.now() < deadline) {
    const proto = await send('Runtime.evaluate', { expression: 'Object.prototype' }, sid);
    const all = await send('Runtime.queryObjects', { prototypeObjectId: proto.result.objectId }, sid);
    const r = await send('Runtime.callFunctionOn',
      { objectId: all.objects.objectId, functionDeclaration: FIND, returnByValue: true }, sid);
    await send('Runtime.releaseObject', { objectId: all.objects.objectId }, sid).catch(() => {});
    last = r.result.value;
    if (last.every((n) => n > 0)) return last;
    await sleep(3000);
  }
  throw new Error(`SLICC seven's agent did not start in time (found control ${last[0]}, settings ${last[1]}, accounts ${last[2]})`);
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

  const policy = await evaluate(sid, `(async () => {
    let last = 'no agent control';
    for (const c of globalThis.__mcpSlicc.controls) {
      try {
        const r = await c.approvalPolicySet({ owner: { tools: ${JSON.stringify(ownerTier)} } });
        if (r && !r.error && r.policy) return { owner: r.policy.owner, sudo: r.policy.sudo };
        last = (r && r.error) || 'no policy in the answer';
      } catch (e) { last = e.message; }
    }
    return { error: String(last) };
  })()`);
  if (policy.error || policy.owner?.tools !== ownerTier) {
    throw new Error(`SLICC seven did not take the approval policy owner.tools ${ownerTier}: ${policy.error || JSON.stringify(policy.owner)}`);
  }
  await evaluate(sid, 'delete globalThis.__mcpSlicc, true');
  done({ ok: true, agent, model: chosen, models: account.models, policy });
}

main().catch((e) => fail(String(e && e.message ? e.message : e).split('\n')[0]));
