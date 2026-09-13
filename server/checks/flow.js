/* Exercise real user flows in the running app and assert on what happens. */
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
  const evalJs = async expr => {
    const r = await send("Runtime.evaluate",
      { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) return { error: r.exceptionDetails.exception?.description
      || r.exceptionDetails.text };
    return r.result.value;
  };

  await send("Page.enable");
  await send("Page.navigate", { url: process.argv[2] });
  await sleep(4000);

  let fails = 0;
  const check = (name, ok, detail = "") => {
    console.log(`${ok ? "  ok  " : "  FAIL"}  ${name}${detail ? "  -> " + detail : ""}`);
    if (!ok) fails++;
  };

  // --- clicking a block on the page moves the selection -------------------
  let r = await evalJs(`(() => {
    const hits=[...document.querySelectorAll(".hit")];
    const target=hits.find(h=>h.dataset.k==="entry"&&h.dataset.name==="experience"&&h.dataset.i==="2");
    if(!target) return {err:"no experience[2] band"};
    target.click();
    return {sel:JSON.stringify(S.sel), insp:document.querySelector("#insp-title").textContent,
            outlineSel:document.querySelector(".okid.sel")?.textContent||null,
            marked:document.querySelectorAll(".hit.sel").length};
  })()`);
  check("click a page block selects that entry",
    r && r.sel === '{"kind":"entry","name":"experience","i":2}', r.sel || r.err);
  check("inspector follows the page click", r && /Initech/.test(r.insp || ""), r.insp);
  check("outline row follows the page click", r && /Initech/.test(r.outlineSel || ""), r.outlineSel);
  check("exactly one band is marked selected", r && r.marked === 1, String(r.marked));

  // --- selecting elsewhere marks the page --------------------------------
  r = await evalJs(`(() => {
    select({kind:"entry",name:"publications",i:0});
    return {page:S.page, marked:[...document.querySelectorAll(".hit.sel")].map(h=>h.title)};
  })()`);
  check("selecting an entry on page 2 flips the page", r && r.page === 1, "page index " + r.page);
  check("that entry's band is marked on page 2",
    r && r.marked.length === 1 && /Publications/.test(r.marked[0]), JSON.stringify(r.marked));

  // --- the link sheet ----------------------------------------------------
  r = await evalJs(`(() => {
    linkJobSheet();
    const opts=[...document.querySelectorAll("#lj-job option")].map(o=>o.textContent);
    return {open:!document.querySelector("#sheet").hidden, opts};
  })()`);
  check("Link… offers applications with no CV yet",
    r && r.open && r.opts.length > 0, `${r.opts.length} offered`);

  r = await evalJs(`(async () => {
    const sel=document.querySelector("#lj-job");
    const chosenId=sel.value;                 // whichever the sheet actually offers first
    document.querySelector("#lj-ok").click();
    await new Promise(r=>setTimeout(r,1500));
    const j=S.jobs.find(x=>x.id===chosenId);
    return {cv:j&&j.cv_path, company:j&&j.company,
            sheetClosed:document.querySelector("#sheet").hidden,
            tie:!!document.querySelector("#doclist .row .tie")};
  })()`);
  check("linking writes the CV onto that application",
    r && r.cv === "profile/hard.yaml", `${r.company} -> ${r.cv}`);
  check("the sheet closes afterwards", r && r.sheetClosed === true);
  check("the rail shows a tie on linked documents", r && r.tie === true);

  // --- unlink ------------------------------------------------------------
  r = await evalJs(`(async () => {
    const linked=S.jobs.find(x=>x.cv_path==="profile/hard.yaml");
    const b=document.querySelector("[data-unlink-job]");
    if(!b) return {err:"no unlink button in the inspector"};
    b.click();
    await new Promise(r=>setTimeout(r,1500));
    const j=S.jobs.find(x=>x.id===(linked&&linked.id));
    return {cv:j&&j.cv_path};
  })()`);
  check("Unlink clears it again", r && !r.cv, r.err || String(r.cv));

  // --- documents rail grouping -------------------------------------------
  r = await evalJs(`(() => ({
    groups:[...document.querySelectorAll("#doclist .rail-sub")].map(e=>e.textContent),
    rows:[...document.querySelectorAll("#doclist .row .lbl")].map(e=>e.textContent)
  }))()`);
  check("rail is grouped by kind",
    r && JSON.stringify(r.groups) === '["CVs","Cover letters"]', JSON.stringify(r.groups));
  check("letters are no longer prefixed in the label",
    r && !r.rows.some(x => /^Letter /.test(x)), JSON.stringify(r.rows));

  console.log(fails ? `\n${fails} failure(s)` : "\nall flows pass");
  ws.close(); process.exit(fails ? 1 : 0);
}
main();
