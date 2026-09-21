/* Drive the running app over the Chrome DevTools Protocol: click through to a
   state, then photograph it. Node 22+ has WebSocket built in, so no deps. */
const fs = require("fs");
/* Any Chromium will do. Set CHROME if yours lives somewhere else. */
const CHROME = process.env.CHROME || [
  process.env.LOCALAPPDATA + "/ms-playwright/chromium-1223/chrome-win64/chrome.exe",
  process.env.LOCALAPPDATA + "/Google/Chrome/Application/chrome.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/chromium",
].find(c => { try { return require("fs").existsSync(c); } catch { return false; } });
const PORT = 9333;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function targets() {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
  return r.json();
}

async function main() {
  const [url, outDir, ...steps] = process.argv.slice(2);
  let list;
  try { list = await targets(); } catch { list = null; }
  if (!list) {
    require("child_process").spawn(CHROME, [
      `--remote-debugging-port=${PORT}`, "--headless=new", "--disable-gpu",
      "--no-sandbox", "--hide-scrollbars", "--window-size=2000,1130",
      `--user-data-dir=${outDir}/profile`, "about:blank",
    ], { detached: true, stdio: "ignore" }).unref();
    for (let i = 0; i < 40 && !list; i++) {
      await sleep(400);
      try { list = await targets(); } catch {}
    }
  }
  const page = list.find(t => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
  };
  await new Promise(r => (ws.onopen = r));
  const send = (method, params = {}) =>
    new Promise(res => { const n = ++id; pending.set(n, res);
      ws.send(JSON.stringify({ id: n, method, params })); });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Page.navigate", { url });
  await sleep(3500);

  for (const step of steps) {
    const [name, expr] = step.includes("::") ? step.split("::") : [step, null];
    if (expr) {
      const r = await send("Runtime.evaluate",
        { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) console.log("  ! " + name + ": " +
        (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
      await sleep(1400);
    }
    const shot = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(`${outDir}/${name}.png`, Buffer.from(shot.data, "base64"));
    console.log(`${outDir}/${name}.png`);
  }
  ws.close();
  process.exit(0);
}
main();
