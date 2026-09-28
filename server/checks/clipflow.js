/* Save to CV Studio on simulated job pages, in a real browser: the bookmark
   clicked on each page, what its window reads, and what is saved.

     node checks/clipflow.js

   clipsim.py runs CV Studio with the job boards' feeds answered locally and
   serves the pages as the browser's proxy, so each keeps its real address
   (job-boards.greenhouse.io, fr.indeed.com...). Needs Chrome or Chromium:
   CHROME=/path/to/it, or one found in the usual places. */
const { spawn, execSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const sleep = ms => new Promise(r => setTimeout(r, ms));

let fails = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "  ok  " : "  FAIL"}  ${name}${detail && !ok ? "  -> " + detail : ""}`);
  if (!ok) fails++;
};

function findChrome() {
  const c = [process.env.CHROME, "/opt/pw-browsers/chromium", "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"].filter(Boolean);
  for (const p of c) if (fs.existsSync(p)) return p;
  try { return execSync("which google-chrome chromium chromium-browser 2>/dev/null").toString().split("\n")[0].trim() || null }
  catch (e) { return null }
}

/* A small Chrome DevTools client: one socket per page. */
async function attach(wsUrl) {
  const ws = new WebSocket(wsUrl); let id = 0; const pend = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data); if (pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id) } };
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j });
  const send = (method, params = {}) => new Promise(r => { const n = ++id; pend.set(n, r); ws.send(JSON.stringify({ id: n, method, params })) });
  const ev = async (expr, gesture = false) => {
    const m = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true, userGesture: gesture });
    if (m.result && m.result.exceptionDetails) throw new Error(m.result.exceptionDetails.exception?.description || "script error");
    return m.result && m.result.result ? m.result.result.value : undefined;
  };
  return { send, ev, close: () => ws.close() };
}

async function main() {
  const chrome = findChrome();
  if (!chrome) { console.log("No Chrome or Chromium found: set CHROME=/path/to/it."); process.exit(1) }

  const sim = spawn(process.env.PYTHON || "python", [path.join(__dirname, "clipsim.py")], { stdio: ["ignore", "pipe", "inherit"] });
  const info = await new Promise((res, rej) => {
    let buf = "";
    sim.stdout.on("data", d => { buf += d; for (const l of buf.split("\n")) if (l.startsWith("{")) { try { res(JSON.parse(l)) } catch (e) {} } });
    sim.on("exit", c => rej(new Error("clipsim.py exited " + c)));
    setTimeout(() => rej(new Error("clipsim.py did not start")), 60000);
  });
  const API = `http://127.0.0.1:${info.api}`, CLIP = `http://127.0.0.1:${info.clip}`;
  const H = { "X-API-Key": info.token, "Content-Type": "application/json" };
  const api = async (p, o = {}) => (await fetch(API + p, { ...o, headers: H })).json();

  const prof = fs.mkdtempSync(path.join(require("os").tmpdir(), "clipflow-"));
  const port = 9000 + Math.floor(Math.random() * 500);
  const br = spawn(chrome, ["--headless=new", "--no-sandbox", "--disable-gpu", `--remote-debugging-port=${port}`,
    `--user-data-dir=${prof}`, `--proxy-server=http://127.0.0.1:${info.pages}`,
    "--disable-features=HttpsUpgrades,HttpsFirstBalancedModeAutoEnable", "--no-first-run", "about:blank"], { stdio: "ignore" });
  const done = code => { try { br.kill("SIGKILL") } catch (e) {} try { sim.kill("SIGKILL") } catch (e) {} process.exit(code) };
  let list = null;
  for (let i = 0; i < 60 && !list; i++) { try { list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json() } catch (e) { await sleep(250) } }
  if (!list) { console.log("Chrome did not start."); done(1) }
  const tab = await attach(list.find(t => t.type === "page").webSocketDebuggerUrl);
  await tab.send("Page.enable");

  const bm = (await api("/api/clip")).bookmarklet;
  const code = decodeURIComponent(bm.replace(/^javascript:/, ""));

  /* Open a page, click the bookmark (optionally with text selected), and
     return the window's reading and a handle on it. */
  async function clip(url, select) {
    await tab.send("Page.navigate", { url });
    for (let i = 0; i < 40; i++) { await sleep(150); if (await tab.ev("document.readyState") === "complete") break }
    if (select) await tab.ev(`(()=>{const r=document.createRange();r.selectNodeContents(document.querySelector(${JSON.stringify(select)}));getSelection().removeAllRanges();getSelection().addRange(r)})()`);
    await tab.ev(code, true);
    let win = null;
    for (let i = 0; i < 60 && !win; i++) {
      await sleep(150);
      const t = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.url.startsWith(CLIP + "/clip"));
      if (t) win = await attach(t.webSocketDebuggerUrl);
    }
    if (!win) throw new Error("the window did not open");
    // Ready once the form shows and any reading from the link is over.
    for (let i = 0; i < 80; i++) {
      await sleep(150);
      const s = await win.ev(`(()=>{const f=document.querySelector("#form");return !!f&&!f.hidden&&getComputedStyle(f).display!=="none"&&!/Reading/.test(document.querySelector("#post-line").innerText)})()`).catch(() => false);
      if (s) break;
    }
    const read = await win.ev(`(()=>{const q=s=>document.querySelector(s);return {title:q("#f-title").value,company:q("#f-company").value,
      location:q("#f-location").value,source:q("#f-source").value,line:q("#post-line").innerText.replace(/\\s+/g," ").trim(),
      known:!q("#known").hidden,button:q("#f-save").innerText,disabled:q("#f-save").disabled}})()`);
    return { read, win };
  }
  async function save(win, fill = {}) {
    for (const [k, v] of Object.entries(fill))
      await win.ev(`(()=>{const e=document.querySelector("#f-${k}");e.value=${JSON.stringify(v)};e.dispatchEvent(new Event("input"))})()`);
    await win.ev(`document.querySelector("#f-save").click()`);
    let said = "";
    for (let i = 0; i < 40; i++) { await sleep(150); said = await win.ev(`(()=>{const d=document.querySelector("#done");return d&&!d.hidden&&getComputedStyle(d).display!=="none"?document.querySelector("#d-say").innerText:""})()`).catch(() => "done"); if (said) break }
    win.close();
    return said;
  }
  const job = async url => ((await api("/api/jobs")).jobs || []).find(j => j.url === url) || {};
  const words = s => (s || "").split(/\s+/).filter(Boolean).length;

  const cases = [
    ["Greenhouse, drawn by script: read from the board's feed", async () => {
      const url = "http://job-boards.greenhouse.io/vtex/jobs/5856357004";
      const { read, win } = await clip(url);
      check("title, company and place from the feed", read.title === "Partner Technical Account Engineer"
        && read.company === "VTEX" && read.location === "São Paulo", JSON.stringify(read));
      check("the window says where the posting came from", /read from Greenhouse/.test(read.line), read.line);
      check("the source is the board", read.source === "Greenhouse", read.source);
      await save(win);
      const j = await job(url), d = j.description || "";
      check("the posting is saved in full", words(d) > 150 && d.includes("Payment Provider Homologation"), words(d) + " words");
      check("its lists and bold kept", /^- \*\*Technical Advisory:\*\*/m.test(d), d.slice(0, 200));
      check("and nothing of the application form", !/First Name|Privacy Notice|Resume\/CV/.test(d));
    }],
    ["A company page describing its job in full", async () => {
      const url = "http://careers.example.com/jobs/chef-de-projet";
      const { read, win } = await clip(url);
      check("title without who may apply", read.title === "Chef de Projet Digital", read.title);
      check("company and place", read.company === "Example SA" && /Lyon/.test(read.location), read.company + " / " + read.location);
      check("read from the page, not the link", !/read from/.test(read.line) && /headings and lists kept/.test(read.line), read.line);
      await save(win);
      const d = (await job(url)).description || "";
      check("headings and lists as Markdown", d.includes("## Vos missions") && d.includes("- Piloter les projets"), d.slice(0, 120));
      check("the salary on top", /^\*\*Salary:\*\*.*45/.test(d), d.split("\n")[0]);
    }],
    ["Job data with only the first lines: the rest from Lever", async () => {
      const url = "http://jobs.lever.co/scaleway/e21a04e5-bad7-4077-9d82-c1b58a8bd4ee";
      const { read, win } = await clip(url);
      check("the page's title and company kept", read.title === "Internal AI Lead" && read.company === "Scaleway", JSON.stringify(read));
      check("the place filled from the feed", read.location === "Paris", read.location);
      check("the posting read from Lever", /read from Lever/.test(read.line), read.line);
      await save(win);
      const d = (await job(url)).description || "";
      check("its lists included", d.includes("## Requirements") && d.includes("- AI evangelist mindset"), d.slice(-120));
    }],
    ["Ashby, nothing on the page and no company in the feed", async () => {
      const url = "http://jobs.ashbyhq.com/acme/0f7c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b";
      const { read, win } = await clip(url);
      check("title and place from the feed", read.title === "Staff Platform Engineer" && read.location === "Remote, Europe", JSON.stringify(read));
      check("the company left for you to type", read.company === "");
      const said = await save(win, { company: "Acme" });
      check("saved once you type it", /Acme/.test(said), said);
      const d = (await job(url)).description || "";
      check("with the posting", d.includes("## About the role") && d.includes("- Go or Rust"), d.slice(0, 80));
    }],
    ["Indeed, read from the page itself", async () => {
      const url = "http://fr.indeed.com/viewjob";
      const { read, win } = await clip(url);
      check("title without the site's suffix", read.title === "Data Engineer", read.title);
      check("company and place", read.company === "Decathlon" && read.location === "Lille (59)", read.company + " / " + read.location);
      await save(win);
      const d = (await job(url)).description || "";
      check("the posting", d.includes("## Missions") && d.includes("- Spark"), d.slice(0, 80));
    }],
    ["LinkedIn, read from the page itself", async () => {
      const url = "http://www.linkedin.com/jobs/view/4012345678/";
      const { read, win } = await clip(url);
      check("title, company, place", read.title === "Solutions Architect" && read.company === "Returnista"
        && /Amsterdam/.test(read.location), JSON.stringify(read));
      await save(win);
      check("the posting", words((await job(url)).description) > 150);
    }],
    ["A page with nothing to read, then with its text selected", async () => {
      const url = "http://jobs.example.org/opening/42";
      let { read, win } = await clip(url);
      check("says no posting was found", /No posting found/.test(read.line), read.line);
      await save(win, { company: "Example Org", title: "Support Engineer" });
      check("the job is saved without one", (await job(url)).title === "Support Engineer" && !(await job(url)).description);
      ({ read, win } = await clip(url, "#body"));
      check("the second time, it knows the job", read.known && /Save the posting to it/.test(read.button), JSON.stringify(read));
      check("and takes the selected text", /the text you selected/.test(read.line), read.line);
      await save(win);
      const d = (await job(url)).description || "";
      check("which is saved to it", words(d) > 150 && d.startsWith("Design, build and run"), d.slice(0, 60));
    }],
    ["Clicking again on a posting already saved", async () => {
      const url = "http://job-boards.greenhouse.io/vtex/jobs/5856357004";
      const { read, win } = await clip(url);
      check("offers to replace it", read.known && /Replace the saved posting/.test(read.button), read.button);
      win.close();
      check("no second application", ((await api("/api/jobs")).jobs || []).filter(j => j.url === url).length === 1);
    }],
  ];
  for (const [name, run] of cases) {
    console.log(name);
    try { await run() } catch (e) { check("runs", false, e.message) }
  }
  console.log(fails ? `\n${fails} failure(s)` : "\nevery page is read as it should be");
  done(fails ? 1 : 0);
}
main().catch(e => { console.error(e); process.exit(1) });
