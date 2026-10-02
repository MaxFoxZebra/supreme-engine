/* CV Studio's MCP Apps views in a browser, inside a stand-in host
   (checks/appshost.py), driven over the DevTools Protocol like flow.js.

     python checks/appshost.py --workspace DIR --port 8790 &
     chromium --headless=new --remote-debugging-port=9333 ... &
     node checks/appsflow.js http://127.0.0.1:8790 profile/my-cv.yaml

   Each view is loaded the way a host loads it, from the tool's own result,
   and then used: paged and downloaded, a theme switched, a change undone. */
const PORT = 9333;
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function main() {
  const [base, cv] = process.argv.slice(2);
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find(t => t.type === "page");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  ws.onmessage = e => { const m = JSON.parse(e.data);
    if (pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); } };
  await new Promise(r => (ws.onopen = r));
  const send = (method, params = {}) => new Promise(res => {
    const n = ++id; pending.set(n, res); ws.send(JSON.stringify({ id: n, method, params })); });
  const js = async expr => {
    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    return r.exceptionDetails ? { error: r.exceptionDetails.exception?.description || r.exceptionDetails.text }
      : r.result.value;
  };
  // Until `expr`, evaluated in the view's frame, is truthy.
  const until = async (expr, ms = 60000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const v = await js(`(()=>{try{const d=document.getElementById("f").contentDocument;return (${expr})}catch(e){return null}})()`);
      if (v && !v.error) return v;
      await sleep(400);
    }
    return null;
  };
  const rpc = (method, params) => js(`fetch("/rpc",{method:"POST",body:JSON.stringify(${JSON.stringify({ method, params })})}).then(r=>r.json())`);
  const open = async (tool, args) => {
    await send("Page.enable");
    await send("Page.navigate", { url: `${base}/?tool=${tool}&args=${encodeURIComponent(JSON.stringify(args))}` });
    await sleep(1500);
  };

  let fails = 0;
  const check = (name, ok, detail = "") => {
    console.log(`${ok ? "  ok  " : "  FAIL"}  ${name}${detail ? "  -> " + detail : ""}`);
    if (!ok) fails++;
  };

  // The page
  await open("render_cv", { path: cv });
  const img = await until(`d.querySelector("img.page") && d.querySelector("img.page").naturalWidth`);
  check("the page view shows the rendered page", img > 300, img);
  const stats = await until(`d.querySelector(".stats") && d.querySelector(".stats").textContent`);
  check("with its page count and how full the last page is", /page/.test(stats || "") && /full/.test(stats || ""), stats);
  check("the host sized the frame to the view", (await js(`parseInt(document.getElementById("f").style.height)`)) > 400);
  await js(`document.getElementById("f").contentDocument.getElementById("pdf").click()`);
  const dl = await until(`window.parent.HOST.downloads.length && window.parent.HOST.downloads[0].contents[0].resource`, 30000);
  check("Download PDF hands the host the PDF itself", dl && dl.mimeType === "application/pdf" && (dl.blob || "").startsWith("JVBER"),
    dl && dl.uri);

  // The themes
  await open("show_themes", { path: cv, themes: ["bold", "airy"] });
  const shown = await until(`d.querySelectorAll(".pic img").length === 2 && [...d.querySelectorAll(".pic img")].every(i => i.naturalWidth > 100)`, 120000);
  check("the theme view shows each theme's page", !!shown);
  const target = await js(`document.getElementById("f").contentDocument.querySelector(".use").closest(".card").dataset.t`);
  await js(`document.getElementById("f").contentDocument.querySelector(".use").click()`);
  const now = await until(`d.querySelector(".card.cur") && d.querySelector(".card.cur").dataset.t === ${JSON.stringify("__T__")} && "${"__T__"}"`.replaceAll("__T__", target), 30000);
  check("Use this theme switches the CV", now === target, `${now} / ${target}`);
  await until(`window.parent.HOST.context.length`, 10000);
  const said = await js(`JSON.stringify(HOST.context)`);
  check("and tells the model what the user chose", (said || "").includes(target) && said.includes("switched"));
  const src = await rpc("tools/call", { name: "read_cv", arguments: { path: cv } });
  check("the file now names that theme", src.result.content[0].text.includes(`theme: ${target}`));

  // The review
  await rpc("tools/call", { name: "edit_cv_fields", arguments: { path: cv, edits: [{ path: ["cv", "headline"], value: "Rewritten by a model" }] } });
  await rpc("tools/call", { name: "add_job", arguments: { company: "Viewco", title: "Platform Engineer", status: "applied", confirmed_new: true } });
  await open("review_changes", {});
  const cards = await until(`d.querySelectorAll(".card").length`);
  check("the review view lists the waiting changes", cards >= 2, cards);
  await js(`[...document.getElementById("f").contentDocument.querySelectorAll(".card")].find(c => c.textContent.includes(${JSON.stringify(cv)})).querySelector('[data-act="undo"]').click()`);
  const undone = await until(`[...d.querySelectorAll(".card.done")].some(c => c.textContent.includes("Undone"))`, 30000);
  check("Undo resolves the document's changes", !!undone);
  const after = await rpc("tools/call", { name: "read_cv", arguments: { path: cv } });
  check("and the file is as it was", !after.result.content[0].text.includes("Rewritten by a model"));
  await js(`document.getElementById("f").contentDocument.getElementById("keepall").click()`);
  const left = await until(`d.querySelectorAll(".card:not(.done)").length === 0`, 30000);
  check("Keep all keeps the rest", !!left);
  const review = await rpc("tools/call", { name: "review_changes", arguments: {} });
  check("leaving nothing to review", review.result.structuredContent.summary === "Nothing to review.",
    review.result.structuredContent.summary);

  ws.close();
  console.log(fails ? `\n${fails} failure(s)` : "\nevery MCP Apps view works");
  process.exit(fails ? 1 : 0);
}
main().catch(e => { console.error(e); process.exit(1); });
