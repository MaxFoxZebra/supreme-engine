/* The CV page view (MCP Apps) end to end, with this script as the AI client.

     node checks/pageview.js

   It starts the MCP server over stdio the way Claude Desktop does, saying it
   can show views, renders the starter CV, and then plays the client's half of
   the view protocol in Chrome: it frames the view, answers ui/initialize,
   hands it the tool's input and result, and records everything the view asks
   for. Then it clicks a block on the page and checks what would reach the
   chat. Needs Chrome on port 9333 (CI starts one) or CHROME=, and python
   (PYTHON= to choose which). */
const fs = require("fs"), os = require("os"), path = require("path");
const {spawn} = require("child_process");

const HERE = path.resolve(__dirname, "..");
const PORT = 9333;
const sleep = ms => new Promise(r => setTimeout(r, ms));
let fails = 0;
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${detail ? "  -> " + detail : ""}`);
  if (!ok) fails++;
};

/* ---- the MCP side: a client over stdio ------------------------------- */
function mcp(workspace) {
  const p = spawn(process.env.PYTHON || "python",
    ["server_main.py", "--mcp", "--workspace", workspace, "--client", "claude"],
    {cwd: HERE, env: {...process.env, PYTHONIOENCODING: "utf-8"}});
  let buf = "", n = 0;
  const waiting = new Map();
  p.stdout.on("data", d => {
    buf += d;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
      if (!line) continue;
      try { const m = JSON.parse(line); if (waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); } }
      catch (e) { /* not ours */ }
    }
  });
  p.stderr.on("data", () => {});
  return {
    send(method, params, notify) {
      const msg = {jsonrpc: "2.0", method, params};
      if (!notify) msg.id = ++n;
      p.stdin.write(JSON.stringify(msg) + "\n");
      if (notify) return Promise.resolve();
      return new Promise((res, rej) => {
        waiting.set(msg.id, res);
        setTimeout(() => rej(new Error("timed out: " + method)), 180000);
      });
    },
    close() { try { p.stdin.end(); } catch (e) {} setTimeout(() => p.kill(), 2000); },
  };
}

const APPS = {"io.modelcontextprotocol/ui": {mimeTypes: ["text/html;profile=mcp-app"]}};

async function session(workspace, withApps) {
  const c = mcp(workspace);
  await c.send("initialize", {protocolVersion: "2025-06-18",
    capabilities: withApps ? {extensions: APPS} : {},
    clientInfo: {name: "claude-ai", version: "1"}});
  await c.send("notifications/initialized", {}, true);
  return c;
}

/* ---- the browser side: Chrome over the DevTools protocol ------------- */
async function browser() {
  let list;
  try { list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); } catch (e) {}
  if (!list) {
    const chrome = process.env.CHROME || ["/usr/bin/google-chrome", "/usr/bin/chromium",
      "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"].find(f => fs.existsSync(f));
    spawn(chrome, [`--remote-debugging-port=${PORT}`, "--headless=new", "--disable-gpu", "--no-sandbox",
      "--window-size=900,1400", `--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(), "pv-"))}`,
      "about:blank"], {detached: true, stdio: "ignore"}).unref();
    for (let i = 0; i < 40 && !list; i++) {
      await sleep(400);
      try { list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); } catch (e) {}
    }
  }
  const page = list.find(t => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pend = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data); if (pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); } };
  await new Promise(r => (ws.onopen = r));
  const send = (method, params = {}) => new Promise(res => { const k = ++id; pend.set(k, res);
    ws.send(JSON.stringify({id: k, method, params})); });
  const evalJs = async expr => {
    const r = await send("Runtime.evaluate", {expression: expr, awaitPromise: true, returnByValue: true});
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  };
  return {send, evalJs, close: () => ws.close()};
}

/* JSON safe inside a <script>: the view itself contains "</script>". */
const js = v => JSON.stringify(v).replace(/</g, "\\u003c");

/* The client's half of the view protocol, as small as it can be. */
const HOST = (view, args, result) => `<!doctype html><html><body style="margin:0;background:#fafafa">
<iframe id="v" sandbox="allow-scripts allow-same-origin" style="width:820px;height:400px;border:0"></iframe>
<script>
window.LOG = [];
const view = ${js(view)}, args = ${js(args)}, result = ${js(result)};
const f = document.getElementById("v");
const post = m => f.contentWindow.postMessage({jsonrpc: "2.0", ...m}, "*");
window.addEventListener("message", e => {
  const m = e.data; if (!m || m.jsonrpc !== "2.0" || e.source !== f.contentWindow) return;
  LOG.push(m);
  if (m.method === "ui/initialize") post({id: m.id, result: {protocolVersion: "2026-01-26",
    hostInfo: {name: "pageview-check", version: "1"},
    hostCapabilities: {message: {text: {}}, updateModelContext: {text: {}}, serverTools: {}},
    hostContext: {theme: "light", displayMode: "inline", availableDisplayModes: ["inline", "fullscreen"],
                  containerDimensions: {width: 820, maxHeight: 2000}}}});
  else if (m.method === "ui/notifications/initialized") {
    post({method: "ui/notifications/tool-input", params: {arguments: args}});
    post({method: "ui/notifications/tool-result", params: result});
  } else if (m.method === "ui/notifications/size-changed") f.style.height = m.params.height + "px";
  else if (m.method === "ui/request-display-mode") post({id: m.id, result: {mode: m.params.mode}});
  else if (m.id != null) post({id: m.id, result: {}});
});
f.srcdoc = view;
</script></body></html>`;

async function main() {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), "pv-ws-"));
  console.log("The connector, to a client that shows views");
  const c = await session(ws, true);
  const tools = (await c.send("tools/list", {})).result.tools;
  const rc = tools.find(t => t.name === "render_cv");
  const uri = rc && rc._meta && rc._meta.ui && rc._meta.ui.resourceUri;
  check("render_cv names its view", uri === "ui://cv-studio/page.html", String(uri));
  const res = (await c.send("resources/read", {uri})).result;
  const item = res && res.contents && res.contents[0];
  check("the view is served as an MCP App", item && item.mimeType === "text/html;profile=mcp-app" &&
    /<html/.test(item.text || ""), item && item.mimeType);
  const cvPath = "profile/my-cv.yaml";
  const call = (await c.send("tools/call", {name: "render_cv", arguments: {path: cvPath}})).result;
  c.close();
  const sc = call.structuredContent;
  check("the render succeeds", !call.isError, call.isError ? JSON.stringify(call.content).slice(0, 300) : "");
  check("the model still gets the summary and the page image",
    call.content.some(b => b.type === "text") && call.content.some(b => b.type === "image"));
  check("the view gets every page", sc && sc.images && sc.images.length === sc.pages &&
    sc.images[0].startsWith("data:image/png;base64,"), sc ? `${sc.images.length} of ${sc.pages}` : "none");
  check("and where each block is, by name", sc && sc.map && sc.map.length > 0 &&
    sc.map.every(b => b.label) && sc.map.some(b => b.k === "entry" && b.label.includes(" · ")),
    sc ? JSON.stringify(sc.map.slice(0, 2)) : "");

  console.log("The connector, to a client without views");
  const c2 = await session(ws, false);
  const plain = (await c2.send("tools/call", {name: "render_cv", arguments: {path: cvPath}})).result;
  c2.close();
  check("gets no view data, so none of it can land in a model's context",
    !plain.structuredContent && plain.content.some(b => b.type === "image"));

  if (!sc) { console.log(`\n${fails} failure(s)`); process.exit(1); }

  console.log("The view, in a client");
  const b = await browser();
  await b.send("Page.enable"); await b.send("Runtime.enable");
  const hostFile = path.join(ws, "host.html");
  fs.writeFileSync(hostFile, HOST(item.text, {path: cvPath}, call));
  /* Served rather than opened as a file: a file:// page is its own opaque
     origin, and the view would not count as the same one. */
  const http = require("http");
  const srv = http.createServer((q, r) => { r.writeHead(200, {"Content-Type": "text/html; charset=utf-8"});
    r.end(fs.readFileSync(hostFile)); }).listen(0, "127.0.0.1");
  await new Promise(r => srv.once("listening", r));
  await b.send("Page.navigate", {url: `http://127.0.0.1:${srv.address().port}/`});
  await sleep(2500);
  const doc = `document.getElementById("v").contentDocument`;
  const init = await b.evalJs(`LOG.filter(m=>m.method==="ui/initialize").map(m=>m.params)`);
  check("it introduces itself with ui/initialize", init.length === 1 && init[0].appInfo &&
    init[0].protocolVersion, JSON.stringify(init[0] || {}));
  check("then says it is ready", await b.evalJs(`LOG.some(m=>m.method==="ui/notifications/initialized")`));
  const shown = await b.evalJs(`(()=>{const d=${doc}, i=d.getElementById("pg");
    return {img: !!i && i.naturalWidth>0, hits: d.querySelectorAll(".hit").length,
            bar: (d.querySelector(".bar")||{}).textContent||""}})()`);
  check("it shows the page", shown.img, shown.bar);
  check("with a click target on every block", shown.hits >= sc.map.filter(x => x.page === 1).length - 1 &&
    shown.hits > 0, String(shown.hits));
  check("and asks to be sized to its content",
    await b.evalJs(`LOG.some(m=>m.method==="ui/notifications/size-changed"&&m.params.height>200)`));

  const target = sc.map.find(x => x.k === "entry" && x.page === 1);
  await b.evalJs(`(()=>{const d=${doc}; const hit=[...d.querySelectorAll(".hit")]
    .find(h=>h.getAttribute("aria-label")===${JSON.stringify("Change " + target.label)}); hit.click()})()`);
  await sleep(400);
  const ctx = await b.evalJs(`LOG.filter(m=>m.method==="ui/update-model-context").map(m=>m.params.content[0].text)`);
  const where = `cv.sections.${target.name}[${target.i}]`;
  check("a click tells the model which block the user means",
    ctx.length === 1 && ctx[0].includes(where) && ctx[0].includes(cvPath), ctx[0] || "none");
  const ask = await b.evalJs(`(()=>{const a=${doc}.getElementById("ask"); return a && !a.hidden ? a.textContent : ""})()`);
  check("and asks what should change there", ask.includes(target.label), ask.slice(0, 80));
  if (process.env.SHOT) {
    await b.evalJs(`${doc}.getElementById("msg").value="Lead with the platform migration"`);
    const s = await b.send("Page.captureScreenshot", {format: "png", captureBeyondViewport: true});
    fs.writeFileSync(process.env.SHOT, Buffer.from(s.data, "base64"));
    console.log("  screenshot: " + process.env.SHOT);
  }

  await b.evalJs(`(()=>{const d=${doc}; d.getElementById("msg").value="Cut it to two bullets";
    d.getElementById("go").click()})()`);
  await sleep(400);
  const msg = await b.evalJs(`LOG.filter(m=>m.method==="ui/message").map(m=>m.params)`);
  const text = msg[0] && msg[0].content && msg[0].content[0] && msg[0].content[0].text || "";
  check("Send puts one message in the chat, as the user",
    msg.length === 1 && msg[0].role === "user", JSON.stringify(msg).slice(0, 120));
  check("naming the file, the block and the change",
    text.includes(cvPath) && text.includes(where) && text.includes("Cut it to two bullets"), text);
  await b.evalJs(`(()=>{const d=${doc}; [...d.querySelectorAll(".hit")][0].click()})()`);
  await sleep(300);
  await b.evalJs(`(()=>{const d=${doc}; d.querySelector("[data-chip]").click()})()`);
  await sleep(300);
  check("a suggestion chip sends in one click",
    (await b.evalJs(`LOG.filter(m=>m.method==="ui/message").length`)) === 2);

  b.close(); srv.close();
  console.log();
  console.log(fails ? `${fails} failure(s)` : "every page view check passes");
  process.exit(fails ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
