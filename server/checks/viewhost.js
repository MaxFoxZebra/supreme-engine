/* A stand-in for an AI client that shows MCP Apps views, shared by the page
   view's check (pageview.js) and its preview (viewdev.js).

   Two halves. mcp() speaks to the CV Studio connector over stdio the way
   Claude Desktop does, saying it can show views. hostPage() is the client's
   half of the view protocol in a web page: it frames a view, answers
   ui/initialize with the theme, size and display mode asked for, hands the
   view the tool's input and result, and keeps a log of everything the view
   asks for (window.LOG), so a check or a person can see what would reach the
   chat. */
const {spawn} = require("child_process");
const path = require("path");

const SERVER = path.resolve(__dirname, "..");
const APPS = {"io.modelcontextprotocol/ui": {mimeTypes: ["text/html;profile=mcp-app"]}};

function mcp(workspace) {
  const p = spawn(process.env.PYTHON || "python",
    ["server_main.py", "--mcp", "--workspace", workspace, "--client", "claude"],
    {cwd: SERVER, env: {...process.env, PYTHONIOENCODING: "utf-8"}});
  let buf = "", n = 0;
  const waiting = new Map();
  p.stdout.on("data", d => {
    buf += d;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
      if (!line) continue;
      try {
        const m = JSON.parse(line);
        if (waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
      } catch (e) { /* not a message of ours */ }
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

async function session(workspace, withApps = true) {
  const c = mcp(workspace);
  await c.send("initialize", {protocolVersion: "2025-06-18",
    capabilities: withApps ? {extensions: APPS} : {},
    clientInfo: {name: "claude-ai", version: "1"}});
  await c.send("notifications/initialized", {}, true);
  return c;
}

/* JSON safe inside a <script>: a view itself contains "</script>". */
const js = v => JSON.stringify(v).replace(/</g, "\\u003c");

/* What a client tells a view about itself. Claude's own variables are not
   published, so these are plausible ones in the shape the spec names: enough
   to see that the view takes the client's colours and type rather than its
   fallbacks. */
const THEMES = {
  light: {"--color-background-primary": "#faf9f5", "--color-background-secondary": "#f0eee6",
    "--color-background-tertiary": "#eeece5", "--color-text-primary": "#1f1e1d",
    "--color-text-secondary": "#5e5d59", "--color-text-tertiary": "#87867f",
    "--color-border-primary": "#c9c7bf", "--color-border-secondary": "#e2e0d9",
    "--color-ring-primary": "#c96442", "--font-sans": "ui-sans-serif, system-ui, sans-serif",
    "--border-radius-md": "8px"},
  dark: {"--color-background-primary": "#262624", "--color-background-secondary": "#30302e",
    "--color-background-tertiary": "#1f1e1d", "--color-text-primary": "#faf9f5",
    "--color-text-secondary": "#c2c0b6", "--color-text-tertiary": "#9a9890",
    "--color-border-primary": "#4a4945", "--color-border-secondary": "#3a3936",
    "--color-ring-primary": "#d97757", "--font-sans": "ui-sans-serif, system-ui, sans-serif",
    "--border-radius-md": "8px"},
};

/* opts: theme ("light" | "dark"), mode ("inline" | "fullscreen" | "pip"), width,
   height (fullscreen), caps (host capabilities), styled (send the theme's
   variables), proxy (where the page's server takes the view's tool calls),
   modes (the display modes the host offers), locale and timeZone, partials
   (the tool's input as it streams in, sent before the whole of it) and delay
   (ms before the result, as a render takes). */
/* A pinned view's frame: a small window beside the chat. */
const PIP = {width: 360, height: 520};

function hostPage(view, args, result, opts = {}) {
  const theme = opts.theme || "light", mode = opts.mode || "inline";
  const width = opts.width || 720, height = opts.height || 820;
  const caps = opts.caps || {message: {text: {}}, updateModelContext: {text: {}}, serverTools: {}, downloadFile: {}};
  const ctx = {theme, displayMode: mode, availableDisplayModes: opts.modes || ["inline", "fullscreen"],
    containerDimensions: mode === "fullscreen" ? {width, height} : mode === "pip" ? PIP : {width, maxHeight: 2400},
    ...(opts.locale ? {locale: opts.locale} : {}), ...(opts.timeZone ? {timeZone: opts.timeZone} : {}),
    styles: opts.styled === false ? undefined : {variables: THEMES[theme]}, platform: "desktop"};
  /* The chat's own background, which is the colour the view is told is
     primary: a view that blends in is one that cannot be told from it. */
  const bg = THEMES[theme]["--color-background-primary"];
  return `<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;color-scheme:${theme};background:${bg};font:13px system-ui;color:${theme === "dark" ? "#eee" : "#222"}">
<iframe id="v" sandbox="allow-scripts allow-same-origin"
  style="display:block;width:${mode === "pip" ? PIP.width : width}px;height:${mode === "fullscreen" ? height + "px" : mode === "pip" ? PIP.height + "px" : "300px"};border:0;background:transparent"></iframe>
<script>
window.LOG = [];
const view = ${js(view)}, args = ${js(args)}, result = ${js(result)}, partials = ${js(opts.partials || [])};
let ctx = ${js(ctx)};
const f = document.getElementById("v");
const post = m => f.contentWindow.postMessage({jsonrpc: "2.0", ...m}, "*");
window.addEventListener("message", e => {
  const m = e.data; if (!m || m.jsonrpc !== "2.0" || e.source !== f.contentWindow) return;
  LOG.push(m);
  if (window.onViewMessage) window.onViewMessage(m);
  if (m.method === "ui/initialize") post({id: m.id, result: {protocolVersion: "2026-01-26",
    hostInfo: {name: "cv-studio-preview", version: "1"}, hostCapabilities: ${js(caps)}, hostContext: ctx}});
  else if (m.method === "ui/notifications/initialized") {
    const later = (ms, fn) => new Promise(r => setTimeout(() => { fn(); r(); }, ms));
    (async () => {
      for (const p of partials) await later(${js(opts.every || 120)}, () => post({method: "ui/notifications/tool-input-partial", params: {arguments: p}}));
      post({method: "ui/notifications/tool-input", params: {arguments: args}});
      if (result) await later(${js(opts.delay || 0)}, () => post({method: "ui/notifications/tool-result", params: result}));
    })();
  } else if (m.method === "ui/notifications/size-changed") {
    if (ctx.displayMode === "inline") f.style.height = m.params.height + "px";
  } else if (m.method === "tools/call") {
    /* As Claude does: the view's tool calls go to the server, through the
       page that serves this (opts.proxy), and come back as the view's answer. */
    fetch(${js(opts.proxy || "/mcp")}, {method: "POST", body: JSON.stringify(m.params)})
      .then(r => r.json()).then(res => post({id: m.id, result: res}))
      .catch(e => post({id: m.id, error: {code: -32000, message: String(e)}}));
  } else if (m.method === "ui/request-display-mode") {
    ctx = {...ctx, displayMode: m.params.mode};
    const fs = m.params.mode === "fullscreen", pp = m.params.mode === "pip";
    f.style.width = (pp ? ${js(PIP.width)} : ${js(width)}) + "px";
    if (fs || pp) f.style.height = (pp ? ${js(PIP.height)} : ${js(height)}) + "px";
    post({id: m.id, result: {mode: m.params.mode}});
  } else if (m.id != null) post({id: m.id, result: {}});
});
f.srcdoc = view;
</script></body></html>`;
}

/* Chrome over the DevTools protocol: the one on PORT if there is one,
   else a headless one started here. */
async function browser(PORT = 9333, size = "1500,1300") {
  const fs = require("fs"), os = require("os");
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const list = async () => { try { return await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); }
                             catch (e) { return null; } };
  let pages = await list();
  if (!pages) {
    const chrome = process.env.CHROME || ["/usr/bin/google-chrome", "/usr/bin/chromium",
      "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"].find(f => fs.existsSync(f));
    spawn(chrome, [`--remote-debugging-port=${PORT}`, "--headless=new", "--disable-gpu", "--no-sandbox",
      "--hide-scrollbars", `--window-size=${size}`,
      `--user-data-dir=${fs.mkdtempSync(path.join(os.tmpdir(), "view-"))}`, "about:blank"],
      {detached: true, stdio: "ignore"}).unref();
    for (let i = 0; i < 40 && !pages; i++) { await sleep(400); pages = await list(); }
  }
  const page = pages.find(t => t.type === "page");
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
  await send("Page.enable"); await send("Runtime.enable");
  return {send, evalJs, close: () => ws.close()};
}

/* The server side of the proxy: a POST of tools/call params, answered with
   the connector's result. */
async function proxyCall(c, q, r) {
  let body = "";
  for await (const chunk of q) body += chunk;
  const res = await c.send("tools/call", JSON.parse(body || "{}"));
  r.writeHead(200, {"Content-Type": "application/json"});
  r.end(JSON.stringify(res.result || {isError: true, content: [{type: "text", text: (res.error || {}).message || "error"}]}));
}

module.exports = {mcp, session, hostPage, browser, proxyCall, js, THEMES, APPS, SERVER};
