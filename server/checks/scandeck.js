/* The deck of jobs a morning search found, driven the way a person drives it:
   the arrow keys, a drag of the card with the mouse, undo, and the end.

     node checks/scandeck.js "http://127.0.0.1:8750/?token=t&lang=en"

   Needs Chromium on port 9333 and the app's server; it hands the jobs over
   through the API itself, so the workspace needs nothing prepared. */
const PORT = 9333;
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const url = process.argv[2];
  const base = new URL(url), token = base.searchParams.get("token") || "";
  const api = (path, body) => fetch(base.origin + path, {
    method: body ? "POST" : "GET",
    headers: { "X-API-Key": token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined }).then(r => r.json());

  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data);
    if (pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); } };
  await new Promise(r => (ws.onopen = r));
  const send = (method, params = {}) => new Promise(res => {
    const n = ++id; pending.set(n, res); ws.send(JSON.stringify({ id: n, method, params })); });
  const evalJs = async expr => {
    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) return { error: r.exceptionDetails.exception?.description || r.exceptionDetails.text };
    return r.result.value;
  };
  const KEYS = { ArrowLeft: 37, ArrowRight: 39, ArrowDown: 40, z: 90 };
  const press = async key => {
    for (const type of ["keyDown", "keyUp"])
      await send("Input.dispatchKeyEvent", { type, key, code: key.length === 1 ? "Key" + key.toUpperCase() : key,
        windowsVirtualKeyCode: KEYS[key], nativeVirtualKeyCode: KEYS[key] });
    await sleep(700);
  };
  let fails = 0;
  const check = (name, ok, detail = "") => {
    console.log(`${ok ? "  ok  " : "  FAIL"}  ${name}${detail ? "  -> " + detail : ""}`);
    if (!ok) fails++;
  };
  const counts = async () => (await api("/api/scans")).counts;
  const top = () => evalJs(`(document.querySelector("#sc-stack .sc-card.top .sc-co b")||{}).textContent||""`);

  // --- what a morning search found --------------------------------------------
  // New ones each run: a job decided once is never offered again, by design.
  const run = Date.now().toString(36);
  const companies = ["Alpha", "Beta", "Gamma", "Delta"].map(n => `Deckcheck ${n} ${run}`);
  const r = await api("/api/scans/record", { scan: "Deck check", jobs: companies.map((c, i) => ({
    company: c, role: "Platform Engineer", link: `http://127.0.0.1:9/deckcheck/${run}/${i}`,
    why: "Made up for the check", posted: "2026-10-1" + i })) });
  check("a search's jobs are handed over", r.added === 4, JSON.stringify(r));
  const c0 = await counts();      // what this workspace held before, to count from

  await send("Page.enable");
  await send("Page.navigate", { url });
  await sleep(4500);
  await evalJs(`(()=>{ const o=document.querySelector("#onb"); if(o&&!o.hidden){ setPref("onboarded",true); location.reload() } })()`);
  await sleep(3500);

  // --- they show without asking ------------------------------------------------
  check("the Applications tab counts them", await evalJs(`document.querySelector("#scan-badge").textContent`) === String(c0.new));
  check("the list says so at the top", await evalJs(`!document.querySelector("#scanbar").hidden&&
    /${c0.new} new jobs/.test(document.querySelector("#scanbar").textContent)`));

  // --- one card at a time ------------------------------------------------------
  await evalJs(`document.querySelector("#sb-go").click()`);
  await sleep(600);
  check("Review opens the deck", await evalJs(`!document.querySelector("#ovl-scans").hidden`));
  check("one card on top, the next ones behind", await evalJs(
    `document.querySelectorAll("#sc-stack .sc-card").length===3&&document.querySelectorAll("#sc-stack .sc-card.top").length===1`));
  const first = await top();
  await press("ArrowLeft");
  let c = await counts();
  check("← passes on it, in the spreadsheet", c.skipped === c0.skipped + 1 && c.new === c0.new - 1, JSON.stringify(c));
  check("and the next card is on top", (await top()) !== first);

  // A drag to the right, with the mouse.
  const box = await evalJs(`(()=>{ const r=document.querySelector("#sc-stack .sc-card.top").getBoundingClientRect();
    return {x:r.left+r.width/2, y:r.top+r.height/2} })()`);
  const second = await top();
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.x, y: box.y, button: "left", clickCount: 1 });
  for (let dx = 20; dx <= 220; dx += 20) {
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: box.x + dx, y: box.y + 4, button: "left" });
    await sleep(16);
  }
  check("dragging shows what letting go will do", Number(await evalJs(
    `getComputedStyle(document.querySelector("#sc-stack .sc-card.top .sc-stamp.yes")).opacity`)) > 0.5);
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.x + 220, y: box.y + 4, button: "left", clickCount: 1 });
  await sleep(2500);
  c = await counts();
  check("a drag to the right adds it", c.added === c0.added + 1, JSON.stringify(c));
  const jobs = (await api("/api/jobs")).jobs;
  const added = jobs.find(j => j.company === second);
  check("as an application, with why it fits in its notes", !!added && /Made up for the check/.test(added.notes || ""),
    added ? added.notes : "not found");
  check("and says so beside the deck", await evalJs(`/Added/.test(document.querySelector("#sc-log").textContent)`));

  // Later, and taking things back.
  const third = await top();
  await press("ArrowDown");
  check("↓ leaves it for later, at the bottom", (await top()) !== third
    && await evalJs(`SC.deck[SC.deck.length-1].company`) === third);
  await press("z");
  check("Z brings it back", (await top()) === third);
  await press("z");
  await sleep(1200);
  c = await counts();
  check("Z again takes the add back", c.added === c0.added && c.new === c0.new - 1 && (await top()) === second, JSON.stringify(c));
  const trash = (await api("/api/jobs/trash")).trash;
  check("and its application goes to the trash", trash.some(j => j.company === second)
    && !(await api("/api/jobs")).jobs.some(j => j.company === second));

  // To the end.
  for (const k of ["ArrowRight", "ArrowLeft", "ArrowLeft"]) await press(k);
  await sleep(2000);
  check("the end says what was done", await evalJs(
    `/everything for now/.test(document.querySelector("#sc-body").textContent)&&/1 added/.test(document.querySelector("#sc-body").textContent)`));
  await evalJs(`document.querySelector("#sc-end-done").click()`);
  await sleep(1500);
  check("Done closes it", await evalJs(`document.querySelector("#ovl-scans").hidden`));
  check("and nothing is left waiting", await evalJs(`document.querySelector("#scan-badge").hidden&&document.querySelector("#scanbar").hidden`));
  c = await counts();
  check("the spreadsheet agrees", c.new === c0.new - 4 && c.added === c0.added + 1
    && c.skipped === c0.skipped + 3, JSON.stringify(c));

  // Tidy up after itself: the other checks share this workspace.
  for (const j of (await api("/api/jobs")).jobs.filter(j => /^Deckcheck/.test(j.company)))
    await api("/api/jobs/delete", { id: j.id });

  console.log(fails ? `\n${fails} deck check(s) failed` : "\nevery deck check passes");
  ws.close(); process.exit(fails ? 1 : 0);
}
main().catch(e => { console.error(e); process.exit(2) });
