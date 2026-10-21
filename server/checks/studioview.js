/* CV Studio itself beside the chat (open_studio and its view), end to end.

     node checks/studioview.js

   It starts the MCP server over stdio as Claude Desktop does, asks it to open
   CV Studio on a CV, and frames the view the way a client does, sandbox and
   all: the view should frame CV Studio's own page, served by a copy of the
   server the connector started, and ask to sit beside the chat. Needs Chrome
   on port 9333 (CI starts one) or CHROME=, and python (PYTHON= to choose). */
const fs = require("fs"), os = require("os"), path = require("path");
const {session, hostPage, browser, proxyCall} = require("./viewhost");

const sleep = ms => new Promise(r => setTimeout(r, ms));
let fails = 0;
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${detail ? "  -> " + detail : ""}`);
  if (!ok) fails++;
};

async function main() {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), "studio-ws-"));
  console.log("The connector");
  const c = await session(ws, true);
  const tools = (await c.send("tools/list", {})).result.tools;
  const t = tools.find(x => x.name === "open_studio");
  const uri = t && t._meta && t._meta.ui && t._meta.ui.resourceUri;
  check("open_studio is offered, with a view of its own", uri === "ui://cv-studio/studio.html", String(uri));
  const res = (await c.send("resources/read", {uri})).result;
  const item = res && res.contents && res.contents[0];
  const frames = (((item || {})._meta || {}).ui || {}).csp || {};
  check("the view says which page it frames, so the client lets it",
    item && item.mimeType === "text/html;profile=mcp-app" && (frames.frameDomains || []).length === 1,
    JSON.stringify(frames));
  const call = (await c.send("tools/call", {name: "open_studio", arguments: {path: "profile/my-cv.yaml"}})).result;
  const sc = call.structuredContent || {};
  check("it opens on the CV asked for", !call.isError && /#\/doc\/profile\/my-cv\.yaml$/.test(sc.url || ""),
    call.isError ? JSON.stringify(call.content).slice(0, 200) : sc.url && sc.url.replace(/token=[^&#]+/, "token=…"));
  check("and the key stays out of what the model reads",
    !call.content.some(x => (x.text || "").includes("token=")), call.content[0].text);
  const origin = new URL(sc.url).origin;
  check("on the origin the view declared", (frames.frameDomains || [])[0] === origin, origin);
  const page = await fetch(sc.url);
  check("CV Studio's own page is served there", page.ok && /<html/i.test(await page.text()), String(page.status));
  const bare = await fetch(origin + "/api/state");
  check("and nothing of it without the key", bare.status === 401, String(bare.status));
  const job = (await c.send("tools/call", {name: "open_studio", arguments: {}})).result.structuredContent || {};
  check("with nothing named, it opens on the applications", /#\/jobs$/.test(job.url || ""), job.label);
  const again = (await c.send("tools/call", {name: "open_studio", arguments: {path: "profile/nothing.yaml"}})).result;
  check("a document that is not there is refused", again.isError === true);

  console.log("The view, in a client");
  const b = await browser();
  const hostFile = path.join(ws, "host.html");
  fs.writeFileSync(hostFile, hostPage(item.text, {path: "profile/my-cv.yaml"}, call,
    {modes: process.env.NOPIP ? ["inline", "fullscreen"] : ["inline", "fullscreen", "pip"], locale: "fr-FR", width: 1100, height: 760,
     caps: {message: {text: {}}, updateModelContext: {text: {}}, serverTools: {}, openLinks: {}}}));
  const http = require("http");
  const srv = http.createServer((q, r) => {
    if (q.url === "/mcp") return proxyCall(c, q, r);
    r.writeHead(200, {"Content-Type": "text/html; charset=utf-8"});
    r.end(fs.readFileSync(hostFile)); }).listen(0, "127.0.0.1");
  await new Promise(r => srv.once("listening", r));
  await b.send("Page.navigate", {url: `http://127.0.0.1:${srv.address().port}/`});
  const doc = `document.getElementById("v").contentDocument`;
  let up = false;
  for (let i = 0; i < 100 && !up; i++) { await sleep(300); up = await b.evalJs(`${doc}.documentElement.dataset.ready === "1"`); }
  check("CV Studio loads in it, sandboxed as a client frames a view", up);
  const asked = await b.evalJs(`LOG.filter(m=>m.method==="ui/request-display-mode").map(m=>m.params.mode)`);
  check(process.env.NOPIP ? "and, not offered beside the chat, asks for full screen" : "and it asks to sit beside the chat",
    asked[0] === (process.env.NOPIP ? "fullscreen" : "pip"), JSON.stringify(asked));
  const src = await b.evalJs(`(${doc}.getElementById("studio")||{}).src||""`);
  check("in the chat's language, on the document", /&lang=fr#\/doc\/profile\/my-cv\.yaml$/.test(src),
    src.replace(/token=[^&#]+/, "token=…"));
  const bar = await b.evalJs(`[...${doc}.querySelectorAll(".bar .btn")].map(x=>x.textContent)`);
  check("with the ways to the other places, and to the browser",
    bar.includes(process.env.NOPIP ? "Dans la conversation" : "Plein écran") && bar.includes("Dans la conversation") &&
      bar.includes("Ouvrir dans le navigateur"), JSON.stringify(bar));
  if (process.env.SHOT) {
    await sleep(2500);
    const s = await b.send("Page.captureScreenshot", {format: "png"});
    fs.writeFileSync(process.env.SHOT, Buffer.from(s.data, "base64"));
    console.log("  screenshot: " + process.env.SHOT);
  }
  b.close(); srv.close();

  console.log("When the conversation ends");
  c.close();
  let gone = false;
  for (let i = 0; i < 30 && !gone; i++) {
    await sleep(500);
    try { await fetch(origin + "/api/pulse"); } catch (e) { gone = true; }
  }
  check("the page goes with the connector", gone);

  console.log();
  console.log(fails ? `${fails} failure(s)` : "every studio view check passes");
  process.exit(fails ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
