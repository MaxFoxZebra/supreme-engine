/* The editors under real typing: keys and clicks sent the way a person
   sends them, through the browser's input pipeline, with the app's own
   saves, renders and polls running underneath.

     node checks/editortest.js "http://127.0.0.1:8750/?token=t"

   What must hold, in the cover letter and in the CV editor alike: the caret
   stays where you put it, in the field you are typing in, while autosave,
   the live preview, the render after it and the workspace poll all land;
   the pane you are looking at does not scroll away; what you typed ends up
   in the file, in the place you typed it; formatting a word leaves the
   selection on that word and the next keystroke right after it; undo still
   undoes after a save. Each of these has, at some point, been the bug.

   Needs a server started on an empty workspace and Chrome with
   --remote-debugging-port=9333, as flow.js does. CV pages have to render,
   so Typst must be able to fetch its packages (or have them cached). */
const PORT = 9333;
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data);
    if (pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); } };
  await new Promise(r => (ws.onopen = r));
  const send = (method, params = {}) => new Promise(res => {
    const n = ++id; pending.set(n, res);
    ws.send(JSON.stringify({ id: n, method, params })); });
  const js = async expr => {
    const r = await send("Runtime.evaluate",
      { expression: `(async()=>{ ${expr} })()`, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  };
  let fails = 0;
  const check = (name, ok, detail = "") => {
    console.log(`${ok ? "  ok  " : "  FAIL"}  ${name}${detail !== "" && !ok ? "  -> " + detail : ""}`);
    if (!ok) fails++;
  };

  /* ---- input, as the browser receives it from a keyboard and a mouse ---- */
  const MOD = { alt: 1, ctrl: 2, meta: 4, shift: 8 };
  const KEYS = { Enter: [13, "Enter", "\r"], Tab: [9, "Tab", ""], Backspace: [8, "Backspace", ""],
    Escape: [27, "Escape", ""], ArrowLeft: [37, "ArrowLeft", ""], ArrowRight: [39, "ArrowRight", ""],
    ArrowUp: [38, "ArrowUp", ""], ArrowDown: [40, "ArrowDown", ""], Home: [36, "Home", ""], End: [35, "End", ""] };
  async function key(k, mods = []) {
    const modifiers = mods.reduce((a, m) => a | MOD[m], 0);
    /* A letter or digit has its own key code; any other character is sent
       as the text it types, with no key code: "." as code 46 would be Delete,
       "!" Page Up, "(" the down arrow and "#" End. */
    let [code, name, text] = KEYS[k] || (/^[a-z]$/i.test(k) ? [k.toUpperCase().charCodeAt(0), "Key" + k.toUpperCase(), k]
      : /^\d$/.test(k) ? [k.charCodeAt(0), "Digit" + k, k] : k === " " ? [32, "Space", " "] : [0, "", k]);
    const plainText = modifiers & (MOD.ctrl | MOD.meta | MOD.alt) ? "" : text;
    await send("Input.dispatchKeyEvent", { type: plainText ? "keyDown" : "rawKeyDown", key: k, code: name,
      windowsVirtualKeyCode: code, modifiers, text: plainText, unmodifiedText: plainText });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code: name, windowsVirtualKeyCode: code, modifiers });
  }
  async function type(s) { for (const ch of s) { await key(ch); await sleep(12); } }
  async function click(selector, where = "center") {
    const b = await js(`const el=document.querySelector(${JSON.stringify(selector)}); if(!el) return null;
      el.scrollIntoView({block:"nearest"}); const r=el.getBoundingClientRect(); return {x:r.left,y:r.top,w:r.width,h:r.height}`);
    if (!b) throw new Error("nothing to click at " + selector);
    const x = where === "end" ? b.x + b.w - 3 : where === "start" ? b.x + 3 : b.x + b.w / 2, y = b.y + Math.min(b.h / 2, 9);
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
    await sleep(80);
  }
  /* Click into a block, then put the caret at its very end: End alone only
     reaches the end of the line on screen, and a paragraph wraps. */
  async function caretAtEnd(selector) {
    await click(selector, "end");
    await js(`const el=document.querySelector(${JSON.stringify(selector)}); const r=document.createRange();
      r.selectNodeContents(el); r.collapse(false); const s=getSelection(); s.removeAllRanges(); s.addRange(r);`);
  }
  /* Where the caret is, named by the field it is in and what sits before it,
     and how far each pane is scrolled. */
  const where = () => js(`
    const a=document.activeElement, s=getSelection();
    const name=el=>!el?null:el.dataset&&el.dataset.p?"[data-p]"+el.dataset.p:el.id?"#"+el.id:el.dataset&&el.dataset.k?"[data-k]"+el.dataset.k:el.tagName;
    let before=null, block=null;
    if(a&&(a.tagName==="INPUT"||a.tagName==="TEXTAREA")) before=a.value.slice(0,a.selectionStart);
    else if(s.rangeCount&&a&&a.isContentEditable){ const n=s.anchorNode, el=n.nodeType===1?n:n.parentElement;
      block=el.closest("p,li,h1,h2,h3,blockquote,td,th,pre,div"); if(block===a) block=null;
      const r=document.createRange(); r.setStart(block||a,0); r.setEnd(s.anchorNode,s.anchorOffset); before=r.toString(); }
    const top=sel=>{ const e=document.querySelector(sel); return e?Math.round(e.scrollTop):null };
    return {field:name(a), block:block?[...block.parentElement.children].indexOf(block)+":"+block.tagName:null, before,
      stage:top("#lt-stage"), form:top("#pane-form"), pg:top("#pane-page"), yaml:top("#yaml"),
      sel:typeof S!=="undefined"&&S.sel?JSON.stringify(S.sel):null};`);
  const same = (a, b, keys) => keys.every(k => JSON.stringify(a[k]) === JSON.stringify(b[k]));
  const settle = ms => sleep(ms);

  await send("Page.enable");
  await send("Page.navigate", { url: process.argv[2] });
  await sleep(3500);
  await js(`window.__errs=[]; addEventListener("error",e=>__errs.push(e.message)); addEventListener("unhandledrejection",e=>__errs.push(String(e.reason)));
    if(typeof onbOpen==="function"&&onbOpen()) obClose(); setPref("delay",400); setPref("live",true);`);

  /* ======================================================================
     The cover letter
     ====================================================================== */
  console.log("Cover letter");
  const LONG = Array.from({ length: 9 }, (_, i) =>
    `Paragraph ${i + 1} says something about platform work, deploys and the people who run them, at some length so the page fills.`).join("\n\n");
  await js(`await post("/api/jobs",{company:"Northwind",title:"Platform Engineer",status:"pending"}); await loadJobs(true);
    const j=S.jobs.find(x=>x.company==="Northwind"); const r=await post("/api/letter/new",{job_id:j.id});
    await post("/api/save",{path:r.path,body:"Dear team,\\n\\n"+${JSON.stringify(LONG)}+"\\n\\nKind regards,"});
    await openLetter(r.path); window.__lt=r.path;`);
  await sleep(1500);

  // Typing in the middle of a paragraph, through autosave, render and poll.
  await caretAtEnd("#lt-edit p:nth-of-type(3)");
  const w0 = await where();
  await type(" Added words.");
  const w1 = await where();
  check("typing keeps the caret in the paragraph it started in", w1.block === w0.block && w1.before.endsWith("Added words."), JSON.stringify([w0, w1]));
  await settle(4200);                   // autosave (1.2s), its render, two polls
  const w2 = await where();
  check("…through autosave, the render and the poll", same(w1, w2, ["field", "block", "before", "stage"]), JSON.stringify([w1, w2]));
  await type(" More.");
  const saved = await js(`return LT.body`);
  check("the next keystrokes land right after the last ones", /Added words\. More\./.test(saved), saved.slice(0, 200));
  await settle(2500);
  const onDisk = await js(`return (await api("/api/doc?path="+encodeURIComponent(window.__lt))).body`);
  check("and reach the file, in that paragraph", /Paragraph 2 says[^\n]*Added words\. More\./.test(onDisk), onDisk.slice(0, 300));

  // Bold with the keyboard: the selection stays on the word, typing follows it.
  await js(`const p=[...document.querySelectorAll("#lt-edit p")][4], tn=[...p.childNodes].find(n=>n.nodeType===3);
    const i=tn.nodeValue.indexOf("deploys"); const r=document.createRange(); r.setStart(tn,i); r.setEnd(tn,i+7);
    $("#lt-edit").focus(); const s=getSelection(); s.removeAllRanges(); s.addRange(r);`);
  const b0 = await where();
  await key("b", ["ctrl"]);
  const sel1 = await js(`return getSelection().toString()`);
  const b1 = await where();
  check("Ctrl+B leaves the word selected", sel1 === "deploys", sel1);
  check("…in the same paragraph, without scrolling", b1.block === b0.block && b1.stage === b0.stage, JSON.stringify([b0, b1]));
  await key("ArrowRight");
  await type("!");
  await settle(2600);
  const afterBold = await js(`return LT.body`);
  check("the next character goes right after the bold word", /\*\*deploys\*\*!/.test(afterBold) || /\*\*deploys!\*\*/.test(afterBold), afterBold.match(/.{20}deploys.{20}/)?.[0] || "");
  const b2 = await where();
  check("…and the caret is still there after the save", b2.block === b0.block && b2.stage === b0.stage, JSON.stringify([b0, b2]));

  // Bold from the toolbar, with the mouse, far down the page.
  await js(`$("#lt-stage").scrollTop=$("#lt-stage").scrollHeight`);
  await sleep(150);
  await js(`const p=[...document.querySelectorAll("#lt-edit p")][8], tn=[...p.childNodes].find(n=>n.nodeType===3);
    const i=tn.nodeValue.indexOf("people"); const r=document.createRange(); r.setStart(tn,i); r.setEnd(tn,i+6);
    $("#lt-edit").focus(); const s=getSelection(); s.removeAllRanges(); s.addRange(r);`);
  const t0 = await where();
  await click('#lt-tools [data-c="bold"]');
  const t1 = await where();
  check("the toolbar's Bold keeps the selection", (await js(`return getSelection().toString()`)) === "people");
  check("…and the page where it was", t1.stage === t0.stage && t1.block === t0.block, JSON.stringify([t0, t1]));
  await settle(3500);
  const t2 = await where();
  check("…after the save and render too", t2.stage === t0.stage && t2.block === t0.block, JSON.stringify([t0, t2]));
  check("the word is bold in the file", /\*\*people\*\*/.test(await js(`return (await api("/api/doc?path="+encodeURIComponent(window.__lt))).body`)));

  // Italic, strikethrough and code on the same word keep hold of it.
  for (const [c, mark] of [["italic", "*"], ["strikeThrough", "~~"], ["code", "`"]]) {
    await js(`const p=[...document.querySelectorAll("#lt-edit p")][6], tn=[...p.childNodes].find(n=>n.nodeType===3&&n.nodeValue.includes("platform"));
      const i=tn.nodeValue.indexOf("platform"); const r=document.createRange(); r.setStart(tn,i); r.setEnd(tn,i+8);
      $("#lt-edit").focus(); const s=getSelection(); s.removeAllRanges(); s.addRange(r);`);
    const x0 = await where();
    await click(`#lt-tools [data-c="${c}"]`);
    const x1 = await where();
    check(`${c} stays in its paragraph and does not scroll`, x1.block === x0.block && x1.stage === x0.stage, JSON.stringify([x0, x1]));
    const body = await js(`return LT.body`);
    check(`${c} writes ${mark}platform${mark}`, body.includes(mark + "platform" + mark), body.match(/.{15}platform.{15}/)?.[0] || "");
    await key("z", ["ctrl"]);
    await sleep(100);
  }

  // Enter splits the paragraph; typing carries on in the new one.
  await caretAtEnd("#lt-edit p:nth-of-type(4)");
  const count0 = await js(`return document.querySelectorAll("#lt-edit p").length`);
  await key("Enter"); await type("A new paragraph.");
  const e1 = await where();
  check("Enter makes a paragraph and the caret goes into it",
    (await js(`return document.querySelectorAll("#lt-edit p").length`)) === count0 + 1 && e1.before === "A new paragraph.", JSON.stringify(e1));
  await settle(3000);
  const e2 = await where();
  check("…and stays there through the save", e2.block === e1.block && e2.before === e1.before, JSON.stringify([e1, e2]));
  check("the file has it as its own paragraph", /\n\nA new paragraph\.\n\n/.test(await js(`return (await api("/api/doc?path="+encodeURIComponent(window.__lt))).body`)));

  // Undo after the save still undoes.
  await key("z", ["ctrl"]);
  await settle(2600);
  check("Ctrl+Z after a save takes the typing back", !(await js(`return LT.body`)).includes("A new paragraph."));

  // Markdown-looking text in the middle of a line stays text.
  await caretAtEnd("#lt-edit p:nth-of-type(2)");
  await type(" # not a heading - not a list 1. no > no");
  const lit = await js(`return LT.body`);
  check("“# ” or “- ” mid-line is just text", !/\n#{1,3} not a heading/.test(lit) && /# not a heading - not a list 1\. no > no/.test(lit.replace(/\\/g, "")), lit.slice(0, 200));
  check("…and no block was made of it", (await js(`return document.querySelectorAll("#lt-edit h1,#lt-edit h2,#lt-edit h3,#lt-edit blockquote").length`)) === 0);

  // The subject and the letterhead: typing through a save keeps the field.
  await click("#lt-subj", "end"); await key("End");
  await type(", Platform");
  await settle(3000);
  const s1 = await where();
  check("typing in the subject keeps focus there through the save", s1.field === "#lt-subj" && s1.before.endsWith(", Platform"), JSON.stringify(s1));
  await click('#lt-paper .lhf[data-k="headline"]', "end"); await key("End");
  await type(" (letter)");
  await settle(3000);
  const h1 = await where();
  check("typing in the letterhead keeps focus there through the save", h1.field === "[data-k]headline" && h1.before.endsWith("(letter)"), JSON.stringify(h1));
  const head = await js(`return (await api("/api/doc?path="+encodeURIComponent(window.__lt))).head`);
  check("…and it is this letter's headline now", /\(letter\)$/.test(head.headline), head.headline);

  // A list: Tab nests, typing continues in the nested item through a save.
  await caretAtEnd("#lt-edit p:nth-of-type(5)");
  await key("Enter"); await type("- first"); await key("Enter"); await type("second"); await key("Tab"); await type(" nested");
  await settle(3000);
  const l1 = await where();
  check("a nested list item keeps the caret through the save", l1.before && l1.before.endsWith("second nested"), JSON.stringify(l1));
  check("…and the file has it nested", /\n- first\n {2,}- second nested/.test(await js(`return (await api("/api/doc?path="+encodeURIComponent(window.__lt))).body`)));

  /* ======================================================================
     The CV editor
     ====================================================================== */
  console.log("CV editor");
  await js(`const st=await api("/api/state"); S.state=st; await openDoc(st.documents.find(d=>!d.letter).path);
    if(S.tab!=="page") $('#edtabs [data-tab="page"]').click();`);
  await sleep(3500);
  const rendered = await js(`return !!document.querySelector("#pane-page img.pg")`);
  check("the CV page renders (needed for the rest)", rendered);

  // Form tab: type in the headline through the live preview.
  await js(`$('#edtabs [data-tab="form"]').click()`);
  await sleep(600);
  await click('#pane-form input[data-p=\'["cv","headline"]\']', "end"); await key("End");
  const f0 = await where();
  await type(" · typed");
  await settle(3200);                   // live preview (0.4s idle) and its render
  const f1 = await where();
  check("Form: the headline keeps focus through the live preview", f1.field === f0.field && f1.before.endsWith(" · typed"), JSON.stringify([f0, f1]));
  check("…and the form does not scroll", f1.form === f0.form, JSON.stringify([f0.form, f1.form]));

  // Form tab, a bullet far down: the pane must not jump back up.
  await js(`const ta=[...document.querySelectorAll('#pane-form textarea')].pop(); ta.scrollIntoView({block:"center"}); ta.id="__deep";`);
  await sleep(200);
  await click("#__deep", "end"); await key("End", ["ctrl"]);
  const d0 = await where();
  await type(" and more");
  await settle(3200);
  const d1 = await where();
  check("Form: a field far down keeps focus through the preview", d1.field === d0.field && d1.before.endsWith(" and more"), JSON.stringify([d0, d1]));
  check("…and the form stays scrolled where it was", Math.abs((d1.form || 0) - (d0.form || 0)) < 4, JSON.stringify([d0.form, d1.form]));

  // Save with Ctrl+S from inside a field: focus and place are kept.
  await type(" saved");
  await key("s", ["ctrl"]);
  await settle(3500);
  const v1 = await where();
  check("Ctrl+S from a field keeps focus in it", v1.field === d0.field && v1.before.endsWith(" saved"), JSON.stringify(v1));
  check("…and the form where it was", Math.abs((v1.form || 0) - (d0.form || 0)) < 4, JSON.stringify([d0.form, v1.form]));
  check("…and the typing is in the file", /and more saved/.test(await js(`return (await api("/api/doc?path="+encodeURIComponent(S.path))).yaml`)));
  // The save changes the provenance marks, and the next poll rebuilds the
  // block editor for them: the caret has to survive that as well.
  await settle(6000);
  await type(" z");
  const v2 = await where();
  check("typing after the poll that follows a save still lands in the field", v2.field === v1.field && v2.before && v2.before.endsWith("saved z"), JSON.stringify(v2));

  // Page tab: click an entry, type in its editor through the preview.
  await js(`$('#edtabs [data-tab="page"]').click()`);
  await sleep(800);
  const hit = await js(`for(let i=0;i<60&&!document.querySelector("#pane-page .hit[data-k=entry]");i++) await new Promise(r=>setTimeout(r,200));
    const hs=[...document.querySelectorAll("#pane-page .hit[data-k=entry]")];
    const h=hs.find(x=>x.dataset.name==="experience")||hs[0];
    if(!h) return null; h.id="__hit"; return true`);
  check("the page has blocks to click", !!hit);
  if (hit) {
    await click("#__hit");
    await sleep(700);
    const sel0 = await js(`return JSON.stringify(S.sel)`);
    const field = await js(`const f=document.querySelector("#ed input:not([type=checkbox]),#ed textarea"); if(!f) return null; f.id=f.id||"__edf"; return "#"+f.id`);
    const fieldName = await js(`const f=document.querySelector(${JSON.stringify("#ed input:not([type=checkbox]),#ed textarea")}); return f&&f.dataset.p?"[data-p]"+f.dataset.p:"#"+f.id`);
    check("clicking a block opens its editor", !!field, sel0);
    if (field) {
      await click(field, "end"); await key("End", ["ctrl"]);
      await type(" x");
      await settle(3200);
      const p1 = await where();
      check("Page: the block editor keeps the same entry through the preview", p1.sel === sel0, JSON.stringify([sel0, p1.sel]));
      check("…and focus in the same field", p1.field === fieldName && p1.before.endsWith(" x"), JSON.stringify(p1));
      await key("s", ["ctrl"]);
      await settle(6500);
      await type("y");
      const p2 = await where();
      check("Page: Ctrl+S and the poll after it keep the block editor and the caret",
        p2.sel === sel0 && p2.field === fieldName && p2.before.endsWith(" xy"), JSON.stringify(p2));
      await key("ArrowDown");
      check("ArrowDown in a field moves the caret, not the entry", (await js(`return JSON.stringify(S.sel)`)) === sel0);
    }
  }

  // YAML tab: typing through the preview keeps the caret and the scroll.
  await js(`$('#edtabs [data-tab="yaml"]').click()`);
  await sleep(600);
  await js(`const ta=$("#yaml"); ta.focus(); const i=ta.value.indexOf("headline:"); const e=ta.value.indexOf("\\n",i); ta.setSelectionRange(e,e);`);
  await type(" ");
  const y0 = await where();
  await type("y");
  await settle(3200);
  const y1 = await where();
  check("YAML: the caret stays after what was typed", y1.field === "#yaml" && y1.before.endsWith(" y") && y1.before.length === y0.before.length + 1, JSON.stringify([y0.before?.length, y1.before?.length]));
  check("…and the source does not scroll", y1.yaml === y0.yaml, JSON.stringify([y0.yaml, y1.yaml]));

  await key("s", ["ctrl"]);
  await settle(2500);
  // Leave the editor on the page, which is where it opens for everyone else.
  await js(`$('#edtabs [data-tab="page"]').click()`);
  const errs = await js(`return window.__errs||[]`);
  check("no errors on the page", !errs.length, errs.join(" | "));

  console.log(fails ? `\n${fails} failure(s)` : "\nevery editor check passes");
  ws.close();
  process.exit(fails ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
