/* Does the app stay quick with a long search behind it?

   Switches to sample data made with 500 applications, then times what you
   do most, from the call until the next frame is painted: the list,
   opening and closing an application, searching, the funnel, the calendar.
   Each is timed three times and the middle one kept. Anything slower than
   BUDGET fails; on a laptop everything here takes under 60 ms, so the
   budget leaves room for a slow CI runner while still catching a screen
   that does per-row work it should not (the calendar took 220 ms at 500
   applications before its date formatters were cached).

     node checks/scaletest.js "http://127.0.0.1:8750/?token=t"

   Needs Chromium on port 9333. Puts the server back on its own workspace
   when done. */
const PORT = 9333, COUNT = 500, BUDGET = +(process.env.SCALE_BUDGET || 250);
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const ws = new WebSocket(list.find(t => t.type === "page").webSocketDebuggerUrl);
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
      sleep(60000).then(() => ({ exceptionDetails: { text: "timed out" } }))]);
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  };
  await send("Page.enable");
  const url = process.argv[2].replace(/[?&]lang=\w+/, "") + "&lang=en";
  await send("Page.navigate", { url });
  await sleep(4000);
  const made = await evalJs(`post("/api/sample",{on:true,applications:${COUNT}}).then(r=>r.applications)`);
  await send("Page.navigate", { url });
  await sleep(6000);
  const loaded = await evalJs(`S.jobs.length`);
  console.log(`  ${loaded} applications (${made} made)`);

  const search = v => `(()=>{ const q=document.querySelector("#jobq"); q.value=${JSON.stringify(v)};
    q.dispatchEvent(new Event("input",{bubbles:true})) })()`;
  const steps = [
    ["applications list", `setView("docs"); setView("jobs")`],
    ["open an application", `selectJob(S.jobs[Math.floor(S.jobs.length/2)].id)`],
    ["close it", `closePeek()`],
    ["search", search("eng")],
    ["clear the search", search("")],
    ["funnel", `setView("funnel")`],
    ["pick a funnel stage", `document.querySelector(".sk-hit[role=button]").dispatchEvent(new MouseEvent("click",{bubbles:true}))`],
    ["calendar", `setView("jobs"); setView("cal")`],
    ["calendar month", `document.querySelector('#cal-views [data-cv=month]').click()`],
    ["calendar week", `document.querySelector('#cal-views [data-cv=week]').click()`],
    ["documents", `setView("docs")`],
  ];
  let slow = 0;
  for (const [name, js] of steps) {
    const took = [];
    for (let i = 0; i < 3; i++) {
      took.push(await evalJs(`(async()=>{ const a=performance.now(); ${js};
        await new Promise(r=>requestAnimationFrame(()=>setTimeout(r,0))); return performance.now()-a })()`));
      await sleep(300);
    }
    const mid = took.sort((a, b) => a - b)[1];
    const over = mid > BUDGET; if (over) slow++;
    console.log(`  ${over ? "SLOW" : "ok  "}  ${name.padEnd(22)} ${mid.toFixed(0).padStart(4)} ms`);
  }
  await evalJs(`post("/api/sample",{on:false})`);
  if (loaded < COUNT) { console.log(`\nonly ${loaded} applications loaded`); slow++ }
  console.log(slow ? `\n${slow} over ${BUDGET} ms` : `\neverything under ${BUDGET} ms with ${loaded} applications`);
  ws.close(); process.exit(slow ? 1 : 0);
}
main().catch(e => { console.error(e); process.exit(2) });
