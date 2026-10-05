/* Preview the CV page view (static/mcp-page.html) the way Claude shows it,
   without a release or a Claude restart.

     node checks/viewdev.js                       # sample data, http://127.0.0.1:5180
     node checks/viewdev.js --workspace ~/Documents/CV\ Studio
     node checks/viewdev.js --shots out/          # screenshots of every variant, then exit

   It starts the connector, renders the CVs once (a few of them, the longest
   first), and serves a page with the view side by side as Claude would frame
   it: inline in light and in dark, and full screen. Edit mcp-page.html and
   the frames reload by themselves. Under each frame is what the view would
   have sent to the chat. "Render again" picks up an edited CV.

   The frames are checks/viewhost.js's stand-in for Claude, so what Claude
   itself does around a view (its frame chrome, its exact colours) is not
   here; check those in Claude with the connector pointed at this checkout. */
const fs = require("fs"), os = require("os"), path = require("path"), http = require("http");
const {execFileSync} = require("child_process");
const {session, hostPage, browser, proxyCall, SERVER} = require("./viewhost");

const argv = process.argv.slice(2);
const opt = k => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : null; };
const PORT = +(opt("--port") || 5180);
const VIEW = path.join(SERVER, "static", "mcp-page.html");
const sleep = ms => new Promise(r => setTimeout(r, ms));

function sampleWorkspace() {
  /* The app's sample data: CVs in four languages, one of them two pages. */
  /* Plus one CV long enough for two pages, which none of the sample's are:
     the page break is what the view is for. */
  const py = [
    "import copy, studio",
    "studio.open_sample(12)",
    "ws = studio.WORKSPACE",
    "src = sorted((ws / 'profile').glob('cv-*.yaml'))[0]",
    "data = studio.yaml_rt.load(src.read_text(encoding='utf-8'))",
    "xp = data['cv']['sections']['experience']",
    "xp.extend(copy.deepcopy(list(xp)) + copy.deepcopy(list(xp)))",
    "out = ws / 'profile' / 'preview-two-pages.yaml'",
    "with open(out, 'w', encoding='utf-8') as f: studio.yaml_rt.dump(data, f)",
    "print(ws)",
  ].join("\n");
  const out = execFileSync(process.env.PYTHON || "python", ["-c", py],
    {cwd: SERVER, encoding: "utf-8", env: {...process.env, PYTHONIOENCODING: "utf-8"}});
  return out.trim().split("\n").pop();
}

async function main() {
  const workspace = opt("--workspace") || sampleWorkspace();
  console.log("workspace  " + workspace);
  const c = await session(workspace, true);
  /* One text block per document; letters too, which render as pages without
     a block map. */
  const listed = (await c.send("tools/call", {name: "list_cvs", arguments: {}})).result.content
    .map(b => { try { return JSON.parse(b.text); } catch (e) { return null; } }).filter(Boolean);
  const cvs = listed.filter(d => d.group === "My CVs").map(d => d.path)
    .sort((a, b) => (b.includes("preview-two-pages") ? 1 : 0) - (a.includes("preview-two-pages") ? 1 : 0))
    .slice(0, +(opt("--max") || 6))
    .concat(listed.filter(d => d.group === "Cover letters").map(d => d.path).slice(0, 1));
  const results = {};
  async function render(p) {
    const r = (await c.send("tools/call", {name: "render_cv", arguments: {path: p}})).result;
    results[p] = r;
    const sc = r.structuredContent;
    console.log(`rendered   ${p}  ${sc ? sc.pages + " page(s), " + (sc.map || []).length + " blocks" : "FAILED"}`);
  }
  for (const p of cvs) await render(p);
  let ok = Object.keys(results).filter(p => results[p].structuredContent);
  ok.sort((a, b) => results[b].structuredContent.pages - results[a].structuredContent.pages);
  /* The state after a change, which is what the view is mostly looked at in:
     the longest CV with its summary and a bullet rewritten, rendered again.
     Only in the sample, which is made fresh each time. */
  const CHANGED = " (after a change)";
  if (!opt("--workspace") && ok.length) {
    const p = ok[0];
    await c.send("tools/call", {name: "edit_cv_fields", arguments: {path: p, edits: [
      {path: ["cv", "sections", "summary", 0], value: "Platform engineer who cut cloud spend by 31% and " +
        "deploy time from 3 hours to 11 minutes. I build the infrastructure product teams ship on."},
      {path: ["cv", "sections", "experience", 1, "highlights", 0], value: "Owned the ledger service: " +
        "2 million transactions a day, 99.99% available over three years"}]}});
    const before = results[p];
    await render(p);
    results[p + CHANGED] = results[p]; results[p] = before;
    ok = [p + CHANGED, ...ok];
  }
  if (!ok.length) { console.error("Nothing rendered."); process.exit(1); }

  const VARIANTS = [
    {id: "light-inline", theme: "light", mode: "inline", width: 720, label: "Inline, light"},
    {id: "dark-inline", theme: "dark", mode: "inline", width: 720, label: "Inline, dark"},
    {id: "light-full", theme: "light", mode: "fullscreen", width: 1280, height: 860, label: "Full screen"},
  ];

  const index = () => `<!doctype html><html><head><meta charset="utf-8"><title>CV page view</title>
<style>body{margin:0;font:13px system-ui;background:#e9e7e1;color:#222}
header{display:flex;gap:10px;align-items:center;padding:10px 16px;background:#fff;border-bottom:1px solid #ddd;position:sticky;top:0;z-index:1}
main{display:flex;flex-wrap:wrap;gap:22px;padding:18px;align-items:flex-start}
figure{margin:0}figcaption{font-weight:600;margin:0 0 6px}
iframe{border:0;display:block;box-shadow:0 1px 4px rgba(0,0,0,.15)}
.log{max-width:720px;font:12px ui-monospace,monospace;white-space:pre-wrap;color:#555;margin-top:6px}</style></head>
<body><header><b>CV page view</b>
<select id="cv">${ok.map(p => `<option>${p}</option>`).join("")}</select>
<button id="again">Render again</button><span id="st" style="color:#777"></span></header>
<main>${VARIANTS.map(v => `<figure><figcaption>${v.label}</figcaption>
<iframe data-v="${v.id}" style="width:${v.width}px;height:${v.mode === "fullscreen" ? v.height : 900}px"></iframe>
<div class="log" data-log="${v.id}"></div></figure>`).join("")}</main>
<script>
const frames = [...document.querySelectorAll("iframe")];
const load = () => frames.forEach(f => f.src = "/host?v=" + f.dataset.v + "&cv=" + encodeURIComponent(cv.value) + "&t=" + Date.now());
cv.onchange = load;
again.onclick = async () => { st.textContent = "rendering…"; await fetch("/rerender?cv=" + encodeURIComponent(cv.value)); st.textContent = ""; load(); };
let stamp = null;
setInterval(async () => {
  const s = await (await fetch("/version")).text();
  if (stamp && s !== stamp) load();
  stamp = s;
  frames.forEach(f => { try {
    const log = (f.contentWindow.LOG || []).filter(m => m.method === "ui/message" || m.method === "ui/update-model-context");
    document.querySelector('[data-log="' + f.dataset.v + '"]').textContent = log.slice(-3).map(m =>
      (m.method === "ui/message" ? "to the chat: " : "to the model: ") + m.params.content[0].text).join("\\n");
    if (f.dataset.v.indexOf("full") < 0) { const h = f.contentDocument.getElementById("v"); if (h) f.style.height = (h.offsetHeight + 8) + "px"; }
  } catch (e) {} });
}, 700);
load();
</script></body></html>`;

  const server = http.createServer(async (q, r) => {
    const u = new URL(q.url, "http://x");
    const html = s => { r.writeHead(200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}); r.end(s); };
    if (u.pathname === "/") return html(index());
    if (u.pathname === "/mcp") return proxyCall(c, q, r);
    if (u.pathname === "/version") { r.end(String(fs.statSync(VIEW).mtimeMs)); return; }
    if (u.pathname === "/rerender") { await render(u.searchParams.get("cv").replace(CHANGED, "")); r.end("ok"); return; }
    if (u.pathname === "/host") {
      const v = VARIANTS.find(x => x.id === u.searchParams.get("v")) || VARIANTS[0];
      const p = u.searchParams.get("cv") || ok[0];
      return html(hostPage(fs.readFileSync(VIEW, "utf-8"), {path: p.replace(CHANGED, "")}, results[p], v));
    }
    r.writeHead(404); r.end();
  }).listen(PORT, "127.0.0.1");
  console.log(`preview    http://127.0.0.1:${PORT}/   (${ok[0]} first)`);

  const shots = opt("--shots");
  if (!shots) return;
  /* One picture per variant, then the same with a block selected and a
     change typed, which is the state worth looking at most. */
  fs.mkdirSync(shots, {recursive: true});
  const b = await browser();
  const cvPath = opt("--cv") || ok[0];
  for (const v of VARIANTS) {
    for (const state of ["", "selected", "sent", "before", "review"]) {
      await b.send("Emulation.setDeviceMetricsOverride", {width: v.width + 40,
        height: v.mode === "fullscreen" ? v.height + 40 : 1400, deviceScaleFactor: 1, mobile: false});
      await b.send("Page.navigate", {url: `http://127.0.0.1:${PORT}/host?v=${v.id}&cv=${encodeURIComponent(cvPath)}`});
      await sleep(1800);
      if (state === "review") {
        await b.evalJs(`(()=>{const x=document.getElementById("v").contentDocument.getElementById("revlist"); if(x) x.click()})()`);
        await sleep(300);
      } else if (state === "before") {
        await b.evalJs(`(()=>{const x=document.getElementById("v").contentDocument.querySelector('[data-cmp="before"]'); if(x) x.click()})()`);
        await sleep(300);
      } else if (state) {
        await b.evalJs(`(()=>{const d=document.getElementById("v").contentDocument;
          const hits=[...d.querySelectorAll(".hit")]; const h=hits.find(x=>x.classList.contains("new")&&/Experience ·/.test(x.getAttribute("aria-label")||""))||hits.find(x=>/Experience ·/.test(x.getAttribute("aria-label")||""))||hits.find(x=>/·/.test(x.getAttribute("aria-label")||""))||hits[0];
          if (h) h.click(); })()`);
        await sleep(300);
        await b.evalJs(`(()=>{const d=document.getElementById("v").contentDocument; const m=d.getElementById("msg");
          if(m){ m.value="Lead with the platform migration, and cut the last bullet"; m.dispatchEvent(new Event("input",{bubbles:true})) } })()`);
        await sleep(300);
        if (state === "sent") {
          await b.evalJs(`(()=>{const m=document.getElementById("v").contentDocument.getElementById("msg");
            m.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}))})()`);
          await sleep(300);
        }
      }
      const shot = await b.send("Page.captureScreenshot", {format: "png", captureBeyondViewport: true});
      const file = path.join(shots, `${v.id}${state ? "-" + state : ""}.png`);
      fs.writeFileSync(file, Buffer.from(shot.data, "base64"));
      console.log("shot       " + file);
    }
  }
  b.close(); server.close(); c.close();
  process.exit(0);
}

main().catch(e => { console.error(e); process.exit(1); });
