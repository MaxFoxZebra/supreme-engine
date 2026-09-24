/* Accessibility, measured on every screen, in light and in dark.

   Over the sample data, on each screen it lists:
     contrast  text below WCAG AA against what is actually behind it
               (4.5:1, or 3:1 for large text), in both themes;
     keyboard  something with a pointer cursor that cannot take focus, so
               a keyboard never reaches it (the page itself aside: its
               blocks are reached through the outline and the form);
     name      a button, link or field a screen reader would announce with
               no name (no text, no aria-label, no title, no label);
     focus     pressing Tab, eighty times on each screen: a stop with no
               visible ring, or one on something that is not showing.

     node checks/a11yscan.js "http://127.0.0.1:8750/?token=t"

   Needs Chromium on port 9333 and a server whose workspace can switch to
   sample data. Exits 1 when it finds something. */
const PORT = 9333;
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data);
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
  await send("DOM.enable");
  const base = process.argv[2].replace(/[?&]lang=\w+/, "");
  const url = base + (base.includes("?") ? "&" : "?") + "lang=en";
  await send("Page.navigate", { url });
  await sleep(4000);
  await evalJs(`fetch("/api/sample",{method:"POST",
    headers:{"Content-Type":"application/json","X-API-Key":API_TOKEN},body:JSON.stringify({on:true})})`);

  /* Runs in the page: what is wrong with what is showing. */
  const probe = `(() => {
    /* Measure where things settle, not halfway through an entrance. */
    /* One that never ends is held at its start, so a result never depends
       on the moment it was taken. */
    document.getAnimations().forEach(a => { try {
      if (a.effect.getComputedTiming().iterations !== Infinity) a.finish(); else { a.pause(); a.currentTime = 0 } } catch (e) {} });
    const out = [];
    const vis = el => { const r = el.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return false;
      for (let e = el; e; e = e.parentElement) { const cs = getComputedStyle(e);
        if (cs.display === "none" || cs.visibility === "hidden" || +cs.opacity === 0) return false }
      return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth };
    const where = el => { let s = el.tagName.toLowerCase(); if (el.id) s += "#" + el.id;
      const c = [...el.classList].slice(0, 2).join("."); if (c) s += "." + c;
      const p = el.parentElement && el.parentElement.closest("[id]"); if (p && !el.id) s = "#" + p.id + " " + s;
      return s };
    const rgba = s => {
      /* color-mix() computes to color(srgb r g b / a), in 0..1. */
      const k = s.match(/color\\(srgb ([^)]+)\\)/);
      if (k) { const v = k[1].split(/[ \\/]+/).filter(Boolean).map(Number);
        return [v[0] * 255, v[1] * 255, v[2] * 255, v.length > 3 ? v[3] : 1] }
      const m = s.match(/rgba?\\(([^)]+)\\)/); if (!m) return null;
      const v = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1] };
    const lum = ([r, g, b]) => { const f = c => (c /= 255) <= .03928 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
      return .2126 * f(r) + .7152 * f(g) + .0722 * f(b) };
    const over = (top, under) => { const a = top[3]; return [0, 1, 2].map(i => top[i] * a + under[i] * (1 - a)).concat(1) };
    /* What is behind an element: its ancestors' backgrounds, composited.
       A background image or gradient makes it unknowable, so it is skipped. */
    const behind = el => { const stack = [];
      for (let e = el; e; e = e.parentElement) { const cs = getComputedStyle(e);
        if (cs.backgroundImage !== "none") return null;
        const c = rgba(cs.backgroundColor); if (c && c[3] > 0) { stack.push(c); if (c[3] >= 1) break } }
      let bg = [255, 255, 255, 1]; for (let i = stack.length - 1; i >= 0; i--) bg = over(stack[i], bg); return bg };
    /* contrast */
    const seen = new Set();
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n; while ((n = w.nextNode())) {
      const el = n.parentElement; if (!el || !n.nodeValue.trim() || seen.has(el)) continue; seen.add(el);
      if (el.closest("#pane-page,.pg,svg,script,style,[aria-hidden=true],.lt-stage,canvas,[disabled],[aria-disabled=true],.ph")) continue;
      if (!vis(el)) continue;
      const cs = getComputedStyle(el); const fg = rgba(cs.color); const bg = behind(el);
      if (!fg || !bg) continue;
      let op = 1; for (let e = el; e; e = e.parentElement) op *= +getComputedStyle(e).opacity;
      const f = over([fg[0], fg[1], fg[2], fg[3] * op], bg);
      const L1 = lum(f), L2 = lum(bg); const ratio = (Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05);
      const px = parseFloat(cs.fontSize), bold = +cs.fontWeight >= 700;
      /* Large text, and a mark with no words (a tick, a dot), need 3:1. */
      const need = px >= 24 || (bold && px >= 18.66) || !/[\\p{L}\\p{N}]/u.test(n.nodeValue) ? 3 : 4.5;
      const hex = c => "#" + c.slice(0, 3).map(v => Math.round(v).toString(16).padStart(2, "0")).join("");
      if (ratio < need) out.push(["contrast", where(el), ratio.toFixed(2) + " < " + need + " (" + hex(f) + " on " + hex(bg) + ")", n.nodeValue.trim().slice(0, 40)]);
    }
    /* keyboard */
    const focusable = el => el.matches("a[href],button,input,select,textarea,summary,[tabindex]:not([tabindex='-1']),[contenteditable]") && !el.disabled;
    for (const el of document.querySelectorAll("body *")) {
      if (getComputedStyle(el).cursor !== "pointer" || !vis(el) || el.disabled || el.closest("[aria-disabled=true]")) continue;
      if (focusable(el) || el.closest("a[href],button,summary,label,[tabindex]:not([tabindex='-1']),[contenteditable]")) continue;
      /* Only the outermost: a card's words inherit its pointer. */
      const up = el.parentElement; if (up && getComputedStyle(up).cursor === "pointer") continue;
      if (el.closest("#pane-page,.pg,svg")) continue;
      out.push(["keyboard", where(el), "clickable, not focusable", (el.textContent || "").trim().slice(0, 40)]);
    }
    /* name */
    for (const el of document.querySelectorAll("button,a[href],input:not([type=hidden]),select,textarea,[role=button]")) {
      /* A switch's checkbox is see-through; the switch beside it is what shows. */
      const sw = el.matches("input[type=checkbox],input[type=radio]") && el.nextElementSibling;
      if (!vis(el) && !(sw && vis(sw))) continue;
      const lab = el.labels && [...el.labels].some(l => l.textContent.trim());
      const name = (el.getAttribute("aria-label") || el.getAttribute("title") || el.getAttribute("aria-labelledby") ||
        (el.matches("input,textarea") ? el.getAttribute("placeholder") : "") || (el.matches("input,select,textarea") ? "" : el.textContent) ||
        [...el.querySelectorAll("img[alt]")].map(i => i.alt).join("") || "").trim();
      if (!name && !lab && !(el.type === "submit" || el.type === "button") ) out.push(["name", where(el), "no accessible name", ""]);
      else if (!name && !lab && el.matches("button,[role=button]")) out.push(["name", where(el), "no accessible name", ""]);
    }
    return out;
  })()`;

  /* Runs in the page after each Tab: can you see where you are? */
  const focusProbe = `(() => {
    const el = document.activeElement; if (!el || el === document.body) return [];
    const where = el => { let s = el.tagName.toLowerCase(); if (el.id) s += "#" + el.id;
      const c = [...el.classList].slice(0, 2).join("."); if (c) s += "." + c;
      const p = el.parentElement && el.parentElement.closest("[id]"); if (p && !el.id) s = "#" + p.id + " " + s;
      return s };
    const ringOn = e => { if (!e) return false; const c = getComputedStyle(e);
      return (c.outlineStyle !== "none" && parseFloat(c.outlineWidth) > 0) || c.boxShadow !== "none" };
    /* A switch: the checkbox is see-through, the switch beside it shows the ring. */
    if (el.matches("input[type=checkbox],input[type=radio]") && ringOn(el.nextElementSibling)) return [];
    /* A funnel stage: the ring is its outline drawn thicker. */
    if (el instanceof SVGElement && [...el.querySelectorAll("*")].some(d => parseFloat(getComputedStyle(d).strokeWidth) >= 2)) return [];
    const r = el.getBoundingClientRect();
    let shown = r.width > 1 && r.height > 1;
    for (let e = el; e && shown; e = e.parentElement) { const cs = getComputedStyle(e);
      if (cs.display === "none" || cs.visibility === "hidden" || +cs.opacity === 0) shown = false }
    if (!shown) return [["focus", where(el), "focus lands on something hidden", (el.textContent || "").trim().slice(0, 30)]];
    if (el.closest("#pane-page,.pg,.lt-paper") || el.isContentEditable) return [];
    const cs = getComputedStyle(el);
    const ring = (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0 && !/rgba\([^)]*,\s*0\)$/.test(cs.outlineColor))
      || cs.boxShadow !== "none" || el.matches("input:not([type=checkbox]):not([type=radio]):not([type=range]),textarea,select");
    /* Or on something inside it: a document card rings its page. */
    return ring || [...el.querySelectorAll("*")].some(ringOn) ? [] : [["focus", where(el), "no visible focus ring", (el.textContent || el.value || "").trim().slice(0, 30)]];
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
    ["people", `closeOverlays(); setView("jobs"); selectJob(S.jobs.find(j=>j.people&&j.people.length>1).id)`],
    ["email draft", `document.querySelector("[data-pp-write]").click(); await new Promise(r=>setTimeout(r,600))`],
    ["pack", `closeSheet(); closeOverlays(); setView("jobs"); selectJob(S.jobs.find(j=>j.cv_path&&j.letter_path).id);
      await new Promise(r=>setTimeout(r,600)); document.querySelector("#ap-pack").click()`],
    ["search", `closeSheet(); closeOverlays(); openPal()`],
    ["search results", `(()=>{ const i=document.querySelector("#pal-in"); i.value="platform";
      i.dispatchEvent(new Event("input",{bubbles:true})) })()`],
  ];
  const found = new Map(); let broken = 0;
  for (const theme of ["light", "dark"]) {
    await send("Page.navigate", { url });
    await sleep(4000);
    await evalJs(`document.documentElement.dataset.theme=${JSON.stringify(theme)}`);
    for (const [name, js] of steps) {
      try { await evalJs(`(async()=>{ ${js}; await new Promise(r=>setTimeout(r,2000)) })()`) }
      catch (e) { broken++; console.log(`  !    ${theme} ${name}: ${e.message.split("\n")[0]}`); continue }
      /* Tab through the screen as a keyboard would. */
      const tabbed = [];
      await evalJs(`document.activeElement&&document.activeElement.blur()`);
      for (let i = 0; i < 80; i++) {
        await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
        await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
        tabbed.push(...await evalJs(focusProbe));
      }
      for (const [kind, where, why, text] of [...await evalJs(probe), ...tabbed]) {
        const key = kind + " " + where + " " + (kind === "contrast" ? theme : "");
        if (!found.has(key)) found.set(key, `  ${kind.padEnd(8)} [${theme} ${name}] ${where}  ${why}${text ? "  " + JSON.stringify(text) : ""}`);
      }
    }
  }
  await evalJs(`fetch("/api/sample",{method:"POST",headers:{"Content-Type":"application/json","X-API-Key":API_TOKEN},body:JSON.stringify({on:false})})`);
  const lines = [...found.values()].sort();
  console.log(lines.join("\n"));
  if (broken) console.log(`${broken} screen(s) could not be opened`);
  console.log(lines.length ? `\n${lines.length} problem(s)` : "\nnothing found");
  ws.close(); process.exit(lines.length || broken ? 1 : 0);
}
main().catch(e => { console.error(e); process.exit(2) });
