/* Find text in the views in the conversation with no translation.

   Starts the views' preview (checks/viewdev.js, over the sample data), shows
   every view it has in French in each frame, and opens what a click opens:
   a block, the review list, the page before, the edit form, the funnel's
   lists, a card, its move. Text the translator would see that is still the
   same as in English is listed. French stands for the three languages: the
   catalogue (i18n/views.py) is written for all of them at once.

     node checks/i18nviews.js

   Exits 1 when something is missing. */
const {spawn} = require("child_process");
const path = require("path");
const {browser} = require("./viewhost");
const sleep = ms => new Promise(r => setTimeout(r, ms));
const PORT = 5188;

/* The same in every language: names, codes, and what the data says. */
const SAME = new Set(["CV Studio", "Claude", "PDF", "ATS", "CV", "LinkedIn", "Indeed", "Welcome to the Jungle",
  "Glassdoor", "Wellfound", "Otta", "Referral", "Email", "OK", "Monzo", "Doctolib", "Datadog", "Stripe"]);

async function main() {
  const dev = spawn(process.execPath, [path.join(__dirname, "viewdev.js"), "--port", String(PORT)],
    {stdio: ["ignore", "pipe", "inherit"]});
  let out = "";
  dev.stdout.on("data", d => { out += d; });
  for (let i = 0; i < 300 && !/preview\s+http/.test(out); i++) await sleep(500);
  const home = await (await fetch(`http://127.0.0.1:${PORT}/`)).text();
  const views = [...home.matchAll(/<option>([^<]+)<\/option>/g)].map(m => m[1].replace(/&amp;/g, "&"));
  const b = await browser(9466, "1400,1000");
  const doc = `document.getElementById("v").contentDocument`;
  /* What the translator would see: text, and the attributes it reads, except
     a title that only repeats CV text shown inside it. */
  const seen = () => b.evalJs(`(()=>{const d=${doc}; if(!d) return [];
      const SKIP="[data-noi18n],textarea,input,script,style,code,pre,svg";
      const out=[], w=d.createTreeWalker(d.body,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode:x=>
        (x.nodeType===1?x:x.parentElement).closest(SKIP)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
      let x; while((x=w.nextNode())){
        if(x.nodeType===3){ const t=x.nodeValue.replace(/\\s+/g," ").trim(); if(t) out.push(t); }
        else for(const a of ["title","aria-label","placeholder"]){ const v=x.getAttribute(a); if(!v) continue;
          if([...x.querySelectorAll("[data-noi18n]")].some(u=>u.textContent.trim()===v.trim())) continue;
          out.push(v.replace(/\\s+/g," ").trim()); }
      }
      return out})()`);
  /* Shown in French, then in English: what reads the same in both was not
     translated (dates and numbers, written the locale's way, differ). */
  const missing = new Map();
  const state = async (url, act, where) => {
    const texts = {};
    for (const loc of ["fr-FR", "en-GB"]) {
      await b.send("Page.navigate", {url: url + "&locale=" + loc});
      await sleep(1600);
      if (act && !(await act())) return;
      await sleep(act ? 700 : 0);
      texts[loc] = await seen();
    }
    const en = new Set(texts["en-GB"]);
    /* In the catalogue already, though the same in French ("Date", "page"). */
    const known = await b.evalJs(`(${JSON.stringify(texts["fr-FR"])}).filter(t=>{const w=${doc}.defaultView;
      return Object.prototype.hasOwnProperty.call(w.CVS_I18N.fr, t) || w.CVS_I18N_RX.fr.some(([re])=>re.test(t))})`);
    for (const t of texts["fr-FR"]) {
      if (!en.has(t) || known.includes(t) || !/[A-Za-z]{2}/.test(t) || SAME.has(t) || /^[\w./ -]+\.(ya?ml|md|pdf)$/.test(t)) continue;
      if (!missing.has(t)) missing.set(t, where);
    }
  };
  const click = sel => () => b.evalJs(`(()=>{const e=${doc}.querySelector(${JSON.stringify(sel)}); if(e){e.click(); return true} return false})()`);
  const then = (...steps) => async () => { for (const f of steps) { if (!(await f())) return false; await sleep(700); } return true; };
  const dbl = () => b.evalJs(`(()=>{const h=${doc}.querySelector(".hit"); if(h){h.dispatchEvent(new MouseEvent("dblclick",{bubbles:true})); return true} return false})()`);
  for (const v of views) for (const frame of ["light-inline", "light-full", "dark-pip"]) {
    const url = `http://127.0.0.1:${PORT}/host?v=${frame}&cv=${encodeURIComponent(v)}`, where = v + " / " + frame;
    await state(url, null, where);
    if (/\(job_stats\)/.test(v)) {
      await state(url, click(".fst"), where + " / list");
      await state(url, then(click(".fst"), click(".job")), where + " / card");
    } else if (/\(show_application\)|\(today\)/.test(v)) {
      await state(url, click("[data-move]"), where + " / move");
      await state(url, click(".iopen"), where + " / card");
    } else {
      await state(url, click(".hit"), where + " / block");
      await state(url, click("#revlist"), where + " / review");
      await state(url, click('[data-cmp="before"]'), where + " / before");
      await state(url, dbl, where + " / edit");
    }
  }
  b.close(); dev.kill();
  for (const [t, w] of [...missing].sort()) console.log(JSON.stringify(t) + "   // " + w);
  console.log(missing.size ? `\n${missing.size} string(s) without a translation` : "every view string is translated");
  process.exit(missing.size ? 1 : 0);
}
main().catch(e => { console.error(e); process.exit(1); });
