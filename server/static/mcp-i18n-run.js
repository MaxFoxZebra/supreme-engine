/* The views in the language of the chat they are shown in.

   The client says its locale in the view's host context ("fr-FR"); this
   translates the view's own words into French, Spanish or Brazilian
   Portuguese the way the app does: by the English text exactly as shown,
   then by patterns for the ones with a number or a name in them, as text
   lands in the page. CV text, postings, names and notes are never touched:
   anything under [data-noi18n] is skipped.

   The catalogue (window.CVS_I18N, CVS_I18N_RX) is written by i18n/build.py
   from i18n/views.py; the server puts both scripts into each view. */
window.cvsI18n = (function () {
  const LANGS = {fr: 1, es: 1, pt: 1};
  let lang = "en", locale = undefined, D = null, P = [], watching = false;
  const SKIP = "[data-noi18n],textarea,input,script,style,code,pre";
  const ATTRS = ["placeholder", "title", "aria-label"];

  /* "envoyée(s)" settled by the count in front of it; French counts 0 and 1
     as singular. */
  const plur = r => {
    if (r.indexOf("(s)") < 0) return r;
    let n = null;
    return r.replace(/(\d+)|\(s\)/g, (m, d) => d != null ? (n = +d, m) : n == null ? m : (lang === "fr" ? n < 2 : n === 1) ? "" : "s");
  };
  function tr(str) {
    if (!D) return null;
    const key = str.replace(/\s+/g, " ").trim();
    if (!key || key.length > 600) return null;
    let out = D[key];
    if (out == null) for (const [re, rep] of P) if (re.test(key)) { out = key.replace(re, rep); break; }
    if (out == null || out === key) return null;
    out = plur(out);
    if (out === key) return null;
    return str.match(/^\s*/)[0] + out + str.match(/\s*$/)[0];
  }
  function attrs(el) {
    for (const a of ATTRS) { const v = el.getAttribute(a); if (v) { const r = tr(v); if (r != null) el.setAttribute(a, r); } }
  }
  function node(n) {
    if (!D) return;
    if (n.nodeType === 3) {
      const p = n.parentElement; if (!p || p.closest(SKIP)) return;
      const r = tr(n.nodeValue); if (r != null) n.nodeValue = r;
      return;
    }
    if (n.nodeType !== 1 || n.closest(SKIP)) return;
    attrs(n);
    const w = document.createTreeWalker(n, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {acceptNode: x =>
      (x.nodeType === 1 ? x : x.parentElement).closest(SKIP) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
    let x;
    while ((x = w.nextNode())) {
      if (x.nodeType === 3) { const r = tr(x.nodeValue); if (r != null) x.nodeValue = r; }
      else attrs(x);
    }
  }
  function watch() {
    if (watching) return;
    watching = true;
    new MutationObserver(ms => { for (const m of ms) {
      if (m.type === "childList") m.addedNodes.forEach(node);
      else node(m.target);
    } }).observe(document.body, {childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ATTRS});
  }
  return {
    /* From the host context: a BCP 47 tag, or nothing. */
    set(tag) {
      const t = String(tag || navigator.language || "en");
      locale = t;
      const l = t.slice(0, 2).toLowerCase();
      lang = LANGS[l] ? l : "en";
      D = lang === "en" ? null : (window.CVS_I18N || {})[lang] || null;
      P = lang === "en" ? [] : (window.CVS_I18N_RX || {})[lang] || [];
      document.documentElement.lang = lang;
      if (D) { watch(); node(document.body); }
    },
    /* A string put together in code, translated before it is shown. */
    t(s) { const r = tr(String(s ?? "")); return r == null ? String(s ?? "") : r; },
    lang: () => lang,
    /* For Intl: the client's own locale, so "en-GB" gets 8 Oct, "en-US" Oct 8. */
    locale: () => locale,
  };
})();
