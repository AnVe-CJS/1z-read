// 把一条选区推送进已打开的解析面板（通过 Chrome DevTools 协议）。
// 用法：node cdp_push.mjs <cdpPort>   ← payload 走 stdin（JSON）
//
// 为什么走 stdin 而不是临时文件：payload 里带云服务钥匙和用户选中的原文，
// 落盘就多一份泄露面，而且进程被强杀时 finally 来不及执行、文件会残留。
// 走管道则进程一死数据就没了。
//
// payload 里的保留字段 __secret（云服务钥匙）会先注入页面，再推选区；
// 没有 text 时只做解锁，不建会话。
// 依赖 Node 22+ 的全局 WebSocket。
const [, , portArg] = process.argv;
const cdpPort = Number(portArg);

const chunks = [];
for await (const c of process.stdin) chunks.push(c);
let payload;
try {
  payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
} catch (_) {
  console.error('bad payload');
  process.exit(1);
}

/* 钥匙取出来单独走，绝不出现在命令行参数里（否则 tasklist 就能看到） */
const secret = payload.__secret && payload.__secret.publishableKey ? payload.__secret : null;
delete payload.__secret;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (!cdpPort || Number.isNaN(cdpPort)) {
  console.error('bad port');
  process.exit(1);
}

const URL_MATCH = process.env.TEXTLENS_URL_MATCH || 'text-select-ai\\.app\\.workbuddy\\.host';

async function findTarget() {
  try {
    const res = await fetch(`http://127.0.0.1:${cdpPort}/json/list`, {
      signal: AbortSignal.timeout(1500),
    });
    const list = await res.json();
    const re = new RegExp(URL_MATCH);
    return list.find((t) => t.type === 'page' && re.test(t.url || '')) || null;
  } catch (_) {
    return null;
  }
}

let target = null;
for (let i = 0; i < 60; i += 1) {
  target = await findTarget();
  if (target) break;
  await sleep(500);
}
if (!target) {
  console.error('panel page not found');
  process.exit(1);
}

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.onopen = res;
  ws.onerror = () => rej(new Error('ws error'));
});

let seq = 0;
const pending = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    pending.get(m.id)(m);
    pending.delete(m.id);
  }
};
const send = (method, params = {}) => new Promise((res) => {
  const id = ++seq;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});

await send('Runtime.enable');

async function evalValue(expression) {
  try {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true });
    const v = r && r.result && r.result.result && r.result.result.value;
    return v === undefined || v === null ? 'not-ready' : v;
  } catch (_) {
    return 'not-ready';
  }
}

/* 页面里没有钥匙，先注入。页面的脚本可能还在加载，所以带重试。 */
let unlock = 'skip';
if (secret) {
  const unlockExpr = `(() => {
    if (typeof window.__lzrUnlock !== 'function') return 'not-ready';
    try {
      return window.__lzrUnlock(${JSON.stringify(secret)});
    } catch (e) {
      return 'error:' + (e && e.message);
    }
  })()`;
  for (let i = 0; i < 40; i += 1) {
    unlock = await evalValue(unlockExpr);
    if (unlock !== 'not-ready') break;
    await sleep(400);
  }
}

const unlockedOk = unlock === 'ok' || unlock === 'already';
let result;

if (!payload.text) {
  /* 只解锁（例如面板刚被打开、还没有选区） */
  result = unlockedOk ? 'unlocked' : (unlock === 'skip' ? 'no-secret' : unlock);
} else {
  const expression = `(() => {
    if (typeof window.__textLensDesktopPush !== 'function') return 'not-ready';
    try {
      return window.__textLensDesktopPush(${JSON.stringify(payload)}) ? 'ok' : 'no-text';
    } catch (e) {
      return 'error:' + (e && e.message);
    }
  })()`;

  result = 'not-ready';
  for (let i = 0; i < 40; i += 1) {
    result = await evalValue(expression);
    if (result !== 'not-ready') break;
    await sleep(400);
  }
}

/* 推送（或仅聚焦）后把面板窗口提到最前 */
if (result === 'ok' || result === 'no-text' || result === 'unlocked') {
  try { await send('Page.bringToFront'); } catch (_) { /* ignore */ }
}

console.log(result);
ws.close();
process.exit(result === 'ok' || result === 'unlocked' ? 0 : 2);
