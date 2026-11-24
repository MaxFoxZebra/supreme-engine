/* Core state, i18n bridge, preferences, DOM/API helpers, and formatters. */
/* The interface. Served as /static/app.js at the end of the page (INDEX_HTML
   in studio.py), after i18n.js and worldmap.js; API_TOKEN and the
   preferences are set by the page's first, inline script. */
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* ---- the interface's language ---------------------------------------------
   English, French, Spanish or Brazilian Portuguese: the computer's language
   unless Settings says otherwise. Two ways in. t("...") for text built in
   code, with {name} holes for what changes. And a watcher that translates
   the interface's text as it lands in the page, so a label written once in
   markup needs no call at all. It never touches what is yours: CVs, letters,
   postings, notes and anything you are typing (I18N_SKIP). */
const UI_LANGS={en:"en-GB",fr:"fr-FR",es:"es-ES",pt:"pt-BR"};
function uiLang(){
  let p=new URLSearchParams(location.search).get("lang");
  if(!p||!UI_LANGS[p]) p=(window.CVS_PREFS||{}).ui_lang;
  if(p&&UI_LANGS[p]) return p;
  const n=String(navigator.language||"en").slice(0,2).toLowerCase();
  return UI_LANGS[n]?n:"en";
}
const UI_LANG=uiLang();
const uiLocale=()=>UI_LANGS[UI_LANG];
const I18N_D=(window.I18N||{})[UI_LANG]||null, I18N_P=(window.I18N_RX||{})[UI_LANG]||[];
/* The catalogue writes a plural as "envoyée(s)"; with the count in front of
   it, it becomes the right form. French counts 0 and 1 as singular. */
const plur=r=>{
  if(r.indexOf("(s)")<0) return r;
  /* Every "(s)" follows the nearest number before it: "2 choses prévues". */
  let n=null;
  return r.replace(/(\d+)|\(s\)/g,(m,d)=>d!=null?(n=+d,m):n==null?m:(UI_LANG==="fr"?n<2:n===1)?"":"s");
};
function t(s,v){
  let r=(I18N_D&&I18N_D[s])||s;
  if(v) r=r.replace(/\{(\w+)\}/g,(m,k)=>v[k]??m);
  return plur(r);
}
function trString(str){
  if(!I18N_D) return null;
  const key=str.replace(/\s+/g," ").trim();
  if(!key||key.length>600) return null;
  let out=I18N_D[key];
  if(out==null) for(const [re,rep] of I18N_P){ if(re.test(key)){ out=key.replace(re,rep); break } }
  if(out==null||out===key) return null;
  /* A pattern can turn "1 page" into "1 page(s)", which plur turns back into
     "1 page": writing that would be a change of nothing, which the watcher
     sees as a change, forever. */
  out=plur(out);
  if(out===key) return null;
  const lead=str.match(/^\s*/)[0], trail=str.match(/\s*$/)[0];
  return lead+out+trail;
}
/* Text the server wrote (an error, a hint), put into a sentence of ours:
   the watcher only knows a text node whole, so the part is translated here. */
const tx=s=>{ s=String(s??""); const r=trString(s); return r==null?s:r };
const I18N_SKIP="[data-noi18n],[contenteditable],textarea,input,script,style,code,pre,.ap-post,"+
  ".ap-notes,.lt-stage,.fj-who,.fn-src .sn,.co,.con,.sk-tip .who,#yaml,.yamlerr,.drift-list .then";
const I18N_ATTRS=["placeholder","title","aria-label","data-ph"];
const I18N_SKIP_FIELD="[data-noi18n],[contenteditable],#yaml,.ap-post,.ap-notes";
function trNode(n){
  if(n.nodeType===3){
    const p=n.parentElement; if(!p||p.closest(I18N_SKIP)) return;
    const r=trString(n.nodeValue); if(r!=null) n.nodeValue=r;
  }else if(n.nodeType===1){
    /* A field's own words (its placeholder, its label) are the app's, even
       though what is typed in it never is. */
    (n.matches("input,textarea")?[n]:[...n.querySelectorAll("input,textarea")]).forEach(f=>{
      if(f.closest(I18N_SKIP_FIELD)) return;
      for(const a of I18N_ATTRS){ const v=f.getAttribute(a); if(v){ const r=trString(v); if(r!=null) f.setAttribute(a,r) } }
    });
    if(n.closest(I18N_SKIP)) return;
    for(const a of I18N_ATTRS){ const v=n.getAttribute(a); if(v){ const r=trString(v); if(r!=null) n.setAttribute(a,r) } }
    const w=document.createTreeWalker(n,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode:x=>
      (x.nodeType===1?x:x.parentElement).closest(I18N_SKIP)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
    let x; while((x=w.nextNode())){
      if(x.nodeType===3){ const r=trString(x.nodeValue); if(r!=null) x.nodeValue=r }
      else for(const a of I18N_ATTRS){ const v=x.getAttribute(a); if(v){ const r=trString(v); if(r!=null) x.setAttribute(a,r) } }
    }
  }
}
if(I18N_D){
  /* Native dialogs are outside the page, where the watcher cannot see. */
  for(const k of ["confirm","prompt","alert"]){
    const f=window[k].bind(window);
    window[k]=(m,...a)=>f(m==null?m:(trString(String(m))??String(m)),...a);
  }
  document.documentElement.lang=UI_LANG;
  trNode(document.body);
  new MutationObserver(ms=>{ for(const m of ms){
    if(m.type==="childList") m.addedNodes.forEach(trNode);
    else if(m.type==="characterData") trNode(m.target);
    else if(m.type==="attributes") trNode(m.target);
  } }).observe(document.body,{childList:true,subtree:true,characterData:true,
    attributes:true,attributeFilter:I18N_ATTRS});
}
/* Date and time fields in the format you chose (Settings › Language &
   region). A native date field edits in the operating system's format --
   mm/dd/yyyy on an English Windows, whatever language the app is in -- and no
   attribute changes that. So each one gets a text box you type into in your
   format, and a button that opens the native picker; the native field stays
   underneath as the value every other piece of code reads and sets. */
const DFX_ORDERS={dmy:["d","m","y"],mdy:["m","d","y"],ymd:["y","m","d"]};
function dateOrder(){
  const p=prefs().date_format;
  if(DFX_ORDERS[p]) return p;
  /* Automatic: the order your system's own locale writes dates in. */
  try{
    const parts=new Intl.DateTimeFormat(navigator.language||uiLocale()).formatToParts(new Date(2026,10,23))
      .filter(x=>/day|month|year/.test(x.type)).map(x=>x.type[0]);
    const k=parts.join("");
    return DFX_ORDERS[k]?k:"dmy";
  }catch(e){ return "dmy" }
}
function clock12(){
  const p=prefs().clock;
  if(p==="12"||p==="24") return p==="12";
  try{ return new Intl.DateTimeFormat(navigator.language||uiLocale(),{hour:"numeric"}).resolvedOptions().hour12===true }
  catch(e){ return false }
}
const dateSep=o=>o==="ymd"?"-":o==="mdy"?"/":"/";
function datePattern(){
  const o=dateOrder(), w={d:t("dd"),m:t("mm"),y:t("yyyy")};
  return DFX_ORDERS[o].map(k=>w[k]).join(dateSep(o));
}
function fmtDateISO(v){
  const m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(v||""); if(!m) return "";
  const o=dateOrder(), part={y:m[1],m:m[2],d:m[3]};
  return DFX_ORDERS[o].map(k=>part[k]).join(dateSep(o));
}
function parseDate(txt){
  const s=String(txt||"").trim(); if(!s) return "";
  let m=/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/.exec(s), y, mo, d;
  if(m){ [y,mo,d]=[+m[1],+m[2],+m[3]] }
  else{
    m=/^(\d{1,4})[-/. ](\d{1,2})[-/. ](\d{1,4})$/.exec(s); if(!m) return null;
    const o=DFX_ORDERS[dateOrder()], got={};
    o.forEach((k,i)=>got[k]=+m[i+1]);
    [y,mo,d]=[got.y,got.m,got.d];
    if(y<100) y+=2000;
  }
  const dt=new Date(y,mo-1,d);
  if(dt.getFullYear()!==y||dt.getMonth()!==mo-1||dt.getDate()!==d) return null;
  return String(y).padStart(4,"0")+"-"+String(mo).padStart(2,"0")+"-"+String(d).padStart(2,"0");
}
function fmtTime(v){
  const m=/^(\d{2}):(\d{2})/.exec(v||""); if(!m) return "";
  if(!clock12()) return m[1]+":"+m[2];
  const h=+m[1]; return ((h+11)%12+1)+":"+m[2]+" "+(h<12?"AM":"PM");
}
function parseTime(txt){
  const s=String(txt||"").trim().toLowerCase(); if(!s) return "";
  const m=/^(\d{1,2})(?:[:h.](\d{2}))?\s*(a|p|am|pm)?\.?$/.exec(s.replace(/\s+/g," "));
  if(!m) return null;
  let h=+m[1]; const mi=+(m[2]||0);
  if(m[3]){ if(h<1||h>12) return null; h=h%12+(m[3][0]==="p"?12:0) }
  if(h>23||mi>59) return null;
  return String(h).padStart(2,"0")+":"+String(mi).padStart(2,"0");
}
const DFX={date:{fmt:fmtDateISO,parse:parseDate,ph:datePattern,bad:()=>t("Write it as {p}",{p:datePattern()})},
  time:{fmt:fmtTime,parse:parseTime,ph:()=>clock12()?"h:mm AM":"hh:mm",bad:()=>t("Write it as {p}",{p:clock12()?"2:30 PM":"14:30"})}};
const DFX_VALUE=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value");
function dfxPaint(inp){
  const tx=inp._dfx; if(!tx) return;
  if(document.activeElement===tx&&tx.dataset.typing) return;
  tx.value=DFX[inp.type].fmt(DFX_VALUE.get.call(inp));
  tx.placeholder=DFX[inp.type].ph();
  tx.removeAttribute("aria-invalid"); tx.title="";
}
function dfxWrap(inp){
  if(inp.dataset.dfx||!DFX[inp.type]) return;
  inp.dataset.dfx="1";
  const kind=DFX[inp.type], w=document.createElement("span"), tx=document.createElement("input"),
    btn=document.createElement("button");
  w.className="dfx";
  tx.type="text"; tx.className="dfx-t "+inp.className; tx.autocomplete="off"; tx.spellcheck=false;
  tx.inputMode="numeric";
  const name=inp.getAttribute("aria-label")||(inp.labels&&inp.labels[0]&&inp.labels[0].textContent.trim())||"";
  if(name) tx.setAttribute("aria-label",name);
  btn.type="button"; btn.className="dfx-b"; btn.tabIndex=-1;
  btn.setAttribute("aria-label",inp.type==="time"?t("Choose a time"):t("Open the calendar"));
  btn.innerHTML=inp.type==="time"
    ?'<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.5 2"/></svg>'
    :'<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5.5" width="16" height="14" rx="2"/><path d="M4 10h16M9 3.5v4M15 3.5v4"/></svg>';
  inp.tabIndex=-1; inp.setAttribute("aria-hidden","true");
  inp.replaceWith(w); w.append(inp,tx,btn);
  inp._dfx=tx;
  /* Code that sets the value directly fires no event; repaint on that too. */
  Object.defineProperty(inp,"value",{configurable:true,get(){ return DFX_VALUE.get.call(this) },
    set(v){ DFX_VALUE.set.call(this,v); dfxPaint(this) }});
  /* Focus asked of the native field (a label, a "set the date" button) lands
     in the one you type into. */
  inp.addEventListener("focus",()=>tx.focus());
  inp.addEventListener("input",()=>dfxPaint(inp)); inp.addEventListener("change",()=>dfxPaint(inp));
  const commit=()=>{
    delete tx.dataset.typing;
    const v=kind.parse(tx.value);
    if(v===null){ tx.setAttribute("aria-invalid","true"); tx.title=kind.bad(); toast(kind.bad(),true); return }
    tx.removeAttribute("aria-invalid"); tx.title="";
    if(v!==DFX_VALUE.get.call(inp)){
      DFX_VALUE.set.call(inp,v);
      inp.dispatchEvent(new Event("input",{bubbles:true}));
      inp.dispatchEvent(new Event("change",{bubbles:true}));
    }
    dfxPaint(inp);
  };
  tx.addEventListener("input",()=>{ tx.dataset.typing="1" });
  tx.addEventListener("change",commit);
  tx.addEventListener("keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); commit() } });
  btn.addEventListener("mousedown",e=>e.preventDefault());
  btn.addEventListener("click",()=>{ try{ inp.showPicker() }catch(e){ tx.focus() } });
  /* Disabled or hidden with its field. */
  new MutationObserver(()=>{ tx.disabled=btn.disabled=inp.disabled }).observe(inp,{attributes:true,attributeFilter:["disabled"]});
  tx.disabled=btn.disabled=inp.disabled;
  dfxPaint(inp);
}
const DFX_SEL='input[type="date"],input[type="time"]';
new MutationObserver(ms=>{ for(const m of ms) for(const n of m.addedNodes){ if(n.nodeType!==1) continue;
  if(n.matches(DFX_SEL)) dfxWrap(n); else n.querySelectorAll&&n.querySelectorAll(DFX_SEL).forEach(dfxWrap) } })
  .observe(document.body,{childList:true,subtree:true});
document.querySelectorAll(DFX_SEL).forEach(dfxWrap);

const tok=()=>API_TOKEN?"&token="+encodeURIComponent(API_TOKEN):"";

/* One object holds everything the three screens share. Selection, filters and
   the last good render all live here so that switching views never throws work
   away: the funnel can hand a status filter to Jobs, and Jobs can hand a
   document to the editor, without either reloading. */
const S={
  view:"jobs", state:null,
  path:null, doc:null, data:null, tab:"page",
  dirty:false, savedAt:null, busy:false,
  pdf:null, render:null, renderMs:null, live:"idle", liveMsg:"",
  page:0, zoom:1, zoomAuto:true, fill:null,
  sel:null, openSection:null,
  docThumbs:{},             /* each document's first page, for Documents */
  baseLang:null,            /* which language of the base CV Documents shows */
  docLang:null,             /* the Documents language filter, null for all */
  driftBy:null,             /* how far behind each translation of the base is */
  ai:null,                  /* which AI clients are wired up to us */
  prov:null,                /* who wrote each field, and what differs from the base */
  pulse:null,               /* last workspace poll: file stamps and AI activity */
  skills:null,              /* the cv-studio skills on this machine */
  keyShown:false,           /* the API key is masked until asked for */
  docMtime:null,            /* the open file as we last read or wrote it */
  extMtime:null,            /* a newer version on disk we have not taken */
  extTheirs:null,           /* their version, so the bar can name the fields */
  resolved:null,            /* how the last conflict was settled, and when */
  pages:{},                 /* path -> page count, learned as things render */
  themePages:{},            /* theme -> page count for the open document */
  jobs:[], statuses:[], nodes:{}, labels:{}, next:{}, jready:false,
  tailoring:new Set(),      /* applications whose CV is being copied right now */
  jfilter:{kind:"all", value:""}, jsel:null,
  funnel:null, since:"", fnode:null,
  baseThumb:null,           /* {path, png, failed}: the base's first page */
  schema:null, schemaTheme:null,
};
const DZ={theme:null, section:"theme"};

/* act = {label, fn}: one button in the toast, and a longer life to reach it. */
function toast(msg,bad,act){
  const t=document.createElement("div");
  t.className="toast"+(bad?" bad":"")+(act?" has-act":""); t.textContent=msg;
  if(act){ const b=document.createElement("button"); b.className="act"; b.textContent=act.label;
    b.onclick=()=>{ t.remove(); act.fn() }; t.append(b) }
  $("#toasts").append(t);
  setTimeout(()=>{t.style.transition="opacity .3s";t.style.opacity="0";
    setTimeout(()=>t.remove(),320)}, act?9000:bad?5200:2200);
}
window.studioError=m=>{$("#pane-page").innerHTML=
  '<div class="err"><h4>Could not start</h4><p>'+esc(m)+'</p></div>'};

const api=async(u,o)=>{
  o=o||{};
  if(API_TOKEN){ o.headers=Object.assign({},o.headers,{"X-API-Key":API_TOKEN}) }
  const r=await fetch(u,o);
  const j=await r.json().catch(()=>({error:"The renderer sent an unreadable response."}));
  if(j&&j.error&&!("ok"in j)) throw new Error(j.error);
  return j;
};
const post=(u,body)=>api(u,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(body)});

/* ---- small shared formatters ---- */
const MONTHS=Array.from({length:12},(_,i)=>{ try{ return new Intl.DateTimeFormat(uiLocale(),{month:"short"})
  .format(new Date(2026,i,15)).replace(".","") }catch(e){ return ["Jan","Feb","Mar","Apr","May","Jun","Jul",
  "Aug","Sep","Oct","Nov","Dec"][i] } });
function shortDate(iso){
  if(!iso) return "";
  /* The tracker stores ISO strings; a document's age arrives as an mtime that
     has already been turned into a Date. Both want the same "14 Sep". */
  const d=iso instanceof Date?iso:new Date(String(iso).slice(0,19));
  if(isNaN(d)) return String(iso).slice(0,10);
  return d.getDate()+" "+MONTHS[d.getMonth()]+
    (d.getFullYear()!==new Date().getFullYear()?" "+String(d.getFullYear()).slice(2):"");
}
