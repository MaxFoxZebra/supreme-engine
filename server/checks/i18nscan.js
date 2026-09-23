/* Find interface text with no translation.

   Goes through every screen over the sample data twice, in English and in
   French, collecting the text the translator would see (user content, the
   posting and the like are skipped the same way it skips them). Text that is
   the same in both, once the words that come from the data are set aside,
   was never translated, and is listed. French stands for the three: the
   catalogue is generated for all of them at once.

     node checks/i18nscan.js "http://127.0.0.1:8750/?token=t"

   Needs Chromium on port 9333 and a server whose workspace can switch to
   sample data. Exits 1 when something is missing. */
const PORT = 9333;
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* Names and codes that are the same in every language. */
const SAME = new Set(["CV Studio", "LinkedIn", "Indeed", "Glassdoor", "Welcome to the Jungle", "Greenhouse",
  "Wellfound", "XING", "Claude", "Claude Desktop", "OpenAI", "ChatGPT", "Codex", "Hermes Agent", "Mistral Vibe",
  "YAML", "PDF", "JSON", "CSV", "API", "ATS", "Markdown", "MCP", "OK", "UTC", "English", "Français", "Español",
  "Português", "Português (Brasil)", "Deutsch", "Italiano", "Nederlands", "Typst", "RenderCV", "GitHub",
  ".zip", ".ics", "Export .ics", "h", "min", "s", "d", "j", "Notifications", "Documents", "Classic", "Ember",
  "Engineering", "Engineering résumés", "Harvard", "Ink", "Sb2nov", "Moderncv", "Engineeringclassic", "Base CV"]);

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data);
    /* A confirm() left open blocks the page, and with it every later step. */
    if (m.method === "Page.javascriptDialogOpening")
      ws.send(JSON.stringify({ id: ++id, method: "Page.handleJavaScriptDialog", params: { accept: true } }));
    if (pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); } };
  await new Promise(r => (ws.onopen = r));
  const send = (method, params = {}) => new Promise(res => {
    const n = ++id; pending.set(n, res); ws.send(JSON.stringify({ id: n, method, params })); });
  const evalJs = async expr => {
    const r = await Promise.race([send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }),
      sleep(20000).then(() => ({ exceptionDetails: { text: "timed out" } }))]);
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  };
  await send("Page.enable");
  const base = process.argv[2].replace(/[?&]lang=\w+/, "");
  const url = lang => base + (base.includes("?") ? "&" : "?") + "lang=" + lang;
  await send("Page.navigate", { url: url("en") });
  await sleep(4000);
  await evalJs(`fetch("/api/sample",{method:"POST",
    headers:{"Content-Type":"application/json","X-API-Key":API_TOKEN},body:JSON.stringify({on:true})})`);

  /* The text the translator would look at, on whatever is showing. */
  /* The CV's own words (its sections, entries and RenderCV's field names in
     the form and the outline) and the page itself are not the interface. */
  const collect = `(() => {
    const OWN = "#pane-page,.pg,.hits,svg,script,style,.mono,code,.dfx-v,kbd,#outline,#pane-form summary,#pane-form .entry-hd,#pane-form label,#doctitle,.tool .n,.blk,.lchip,.langs";
    const out = new Set();
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: x => { const el = x.nodeType === 1 ? x : x.parentElement;
        if (!el || el.closest(I18N_SKIP) || el.closest(OWN)) return NodeFilter.FILTER_REJECT;
        /* Time zones are city names, languages are named in themselves. */
        const sel = el.closest("select"); if (sel && /tz|lang/i.test(sel.id + " " + (sel.dataset.j || ""))) return NodeFilter.FILTER_REJECT;
        if (x.nodeType === 1) { const cs = getComputedStyle(x); if (cs.display === "none" || cs.visibility === "hidden") return NodeFilter.FILTER_REJECT; return NodeFilter.FILTER_SKIP }
        return NodeFilter.FILTER_ACCEPT } });
    let n; while ((n = w.nextNode())) { const s = n.nodeValue.replace(/\\s+/g, " ").trim(); if (s) out.add(s) }
    document.querySelectorAll("[placeholder],[title],[aria-label]").forEach(el => {
      if (el.closest("[data-noi18n],[contenteditable],#yaml,.ap-post,.ap-notes") || el.closest(OWN)) return;
      for (const a of ["placeholder", "title", "aria-label"]) { const v = el.getAttribute(a); if (v) out.add(v.replace(/\\s+/g, " ").trim()) } });
    return [...out];
  })()`;
  const steps = [
    ["applications", `setView("jobs")`],
    ["application", `selectJob(S.jobs.find(j=>j.interview_at&&j.cv_path).id)`],
    ["documents", `closePeek&&closePeek(); setView("docs")`],
    ["editor", `openDoc(S.state.base.path)`],
    ["design", `document.querySelector("#btn-design").click()`],
    ["letter", `closeOverlays(); openLetter((S.state.documents.find(d=>d.group==="Cover letters")||{}).path)`],
    ["funnel", `setView("funnel")`],
    ["calendar", `setView("cal")`],
    ["month", `document.querySelector('#cal-views [data-cv=month]').click()`],
    ["week", `document.querySelector('#cal-views [data-cv=week]').click()`],
    ...["workspace", "editor", "region", "notify", "ai", "api", "updates", "about"].map(p =>
      ["settings " + p, `openSettings("${p}")`]),
  ];
  let broken = 0;                                  /* a screen that could not be opened was not checked */
  async function scan(lang) {
    await send("Page.navigate", { url: url(lang) });
    await sleep(5000);
    const seen = new Map();
    for (const [name, js] of steps) {
      try { await evalJs(`(async()=>{ ${js}; await new Promise(r=>setTimeout(r,2500)) })()`) }
      catch (e) { broken++; console.log(`  !    ${lang} ${name}: ${e.message.split("\n")[0]}`); continue }
      for (const s of await evalJs(collect)) if (!seen.has(s)) seen.set(s, name);
    }
    return seen;
  }
  const en = await scan("en"), fr = await scan("fr");
  /* Words that come from the data, not the interface: companies, roles,
     places, sources, document names. */
  const data = new Set((await evalJs(`(() => { const w = [];
    for (const j of S.jobs) for (const k of ["company","title","location","source","notes"]) if (j[k]) w.push(String(j[k]));
    for (const d of S.state.documents) w.push(d.label, d.path);
    w.push(...Object.values(TZ_NAMES||{}), ...(S.state.themes||[]));
    return w.join(" ").toLowerCase().match(/[a-zà-ÿ]{3,}/g) || [] })()`)));
  for (const s of SAME) for (const w of s.toLowerCase().match(/[a-zà-ÿ]{3,}/g) || []) data.add(w);
  /* Words that are the same in French on purpose are in the catalogue. */
  const known = new Set(await evalJs(`Object.keys((window.I18N||{}).fr||{})`));
  let count = 0;
  for (const [s, where] of en) {
    if (!fr.has(s) || known.has(s)) continue;      /* it changed, or is the same on purpose */
    /* Paths, links, tool names: every word has a slash, a dot or an underscore. */
    if (s.split(/\s+/).every(w => /[_./…]/.test(w))) continue;
    /* Language names, written in themselves. */
    if (/^(Français|Español|Português|English|Deutsch|Italiano)$/.test(s)) continue;
    const words = (s.toLowerCase().match(/[a-zà-ÿ]{3,}/g) || []).filter(w => !data.has(w));
    if (!words.length) continue;
    count++;
    console.log(`  MISS  [${where}] ${JSON.stringify(s)}`);
  }
  await evalJs(`fetch("/api/sample",{method:"POST",headers:{"Content-Type":"application/json","X-API-Key":API_TOKEN},body:JSON.stringify({on:false})})`);
  console.log(count ? `\n${count} string(s) still in English on the French screens` : "\nevery string on every screen is translated");
  if (broken) console.log(`${broken} screen(s) could not be opened`);
  ws.close(); process.exit(count || broken ? 1 : 0);
}
main().catch(e => { console.error(e); process.exit(2) });
