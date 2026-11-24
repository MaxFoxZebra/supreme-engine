/* Settings panels, updates/release notes, and Ctrl+K command palette. */
/* =========================================================================
   Settings and updates
   ========================================================================= */
/* Preferences are per-machine conveniences, so they live beside the app
   rather than in the workspace: a workspace copied to another machine should
   carry documents, not window preferences. The server keeps them in a file
   and hands them over with the page (see load_prefs); this browser's storage
   keeps a copy for a page served without them. */
const PREFS_KEY="cvstudio.prefs";
function prefs(){ return window.CVS_PREFS||(window.CVS_PREFS={}) }
function setPref(k,v){
  const p=prefs(); p[k]=v;
  try{ localStorage.setItem(PREFS_KEY,JSON.stringify(p)) }catch(e){}
  post("/api/prefs",{set:{[k]:v}}).catch(()=>{});
}
if(window.CVS_PREFS_MOVE&&Object.keys(prefs()).length)
  setTimeout(()=>post("/api/prefs",{replace:prefs()}).catch(()=>{}),0);
/* "system" means take the attribute off and let prefers-color-scheme decide;
   anything else pins it. Everything downstream is a CSS variable, so nothing
   needs redrawing -- including the funnel, which is styled rather than filled. */
const ACCENTS=[["ochre","Ochre"],["indigo","Indigo"],["teal","Teal"],
  ["rose","Rose"],["moss","Moss"]];
function applyAppearance(){
  const p=prefs(), a=p.appearance||"system";
  if(a==="system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme=a;
  /* Ochre is what :root already defines, so it is the absence of an override
     rather than one more rule to keep in step with the others. */
  const acc=p.accent||"ochre";
  if(acc==="ochre") delete document.documentElement.dataset.accent;
  else document.documentElement.dataset.accent=acc;
}
applyAppearance();

/* A settings row says what its control is in the words beside it: tie them
   together, so a screen reader reads the title, then the explanation. */
function nameRows(){
  document.querySelectorAll(".srow").forEach((row,i)=>{
    const b=row.querySelector(":scope>div>b"), d=row.querySelector(":scope>div>span");
    if(!b) return;
    b.id=b.id||"srow-t"+i; if(d) d.id=d.id||"srow-d"+i;
    row.querySelectorAll("select,input,textarea").forEach(c=>{
      if(c.getAttribute("aria-label")||c.getAttribute("aria-labelledby")) return;
      c.setAttribute("aria-labelledby",b.id); if(d) c.setAttribute("aria-describedby",d.id);
    });
  });
}
function openSettings(pane){
  if(!openSettings.named){ nameRows(); openSettings.named=true }
  $("#ovl-design").hidden=true;
  $("#ovl-settings").hidden=false;
  fillSettings();
  showSettingsPane(pane||"workspace");
}
$("#btn-settings").onclick=()=>{
  if($("#ovl-settings").hidden) openSettings("workspace");
  else closeOverlays();
};
/* Something drawn as a button that is not a <button> answers Enter and
   Space the way one would, unless it already handles them itself. */
document.addEventListener("keydown",e=>{
  if(e.defaultPrevented||(e.key!=="Enter"&&e.key!==" ")) return;
  const el=e.target;
  if(!el.matches||!el.matches('[role=button]:not(button)')) return;
  e.preventDefault(); el.dispatchEvent(new MouseEvent("click",{bubbles:true}));
});
/* The one shortcut every desktop user tries. */
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&e.key===","){
    e.preventDefault();
    if($("#ovl-settings").hidden) openSettings("workspace"); else closeOverlays();
  }
});
function showSettingsPane(which){
  $$("#set-rail button").forEach(x=>
    x.setAttribute("aria-selected",String(x.dataset.s===which)));
  ["workspace","editor","region","notify","browser","scans","ai","api","updates","about"].forEach(k=>
    $("#sp-"+k).hidden = k!==which);
  if(which==="updates") checkUpdates(true);
  if(which==="ai") loadAI();
  if(which==="browser") fillClip();
  if(which==="about") $("#s-logs").onclick=()=>post("/api/reveal",{logs:true}).catch(e=>toast(e.message,true));
}
/* Save to CV Studio: the bookmark is made by the server, for the port it
   listens on for it. Clicking it here, rather than dragging, would run it on
   this page: say what to do instead. */
async function fillClip(){
  const a=$("#s-clip-bm"), st=$("#s-clip-state");
  try{
    const r=await api("/api/clip");
    a.href=r.bookmarklet;
    st.textContent=r.ok?t("Ready"):t("Not available");
    st.className="clip-state "+(r.ok?"ok":"bad");
    st.title=r.ok?"":r.why;
    if(!r.ok) $("#s-clip-say").textContent=t("Another program is using the address the button needs ({why}). Quit it, or the other copy of CV Studio, then restart this one.",{why:r.why});
  }catch(e){ st.textContent=t("Not available"); st.className="clip-state bad" }
  a.onclick=e=>{ e.preventDefault(); toast(t("Drag it to your bookmarks bar, then click it there on a job page.")) };
  /* A bookmarklet cannot have an icon, so its name starts with one: the
     browser names the bookmark from the link's text, where the emoji is,
     hidden. The drag itself is left to the browser. */
  $("#s-clip-copy").onclick=async()=>{
    try{ await navigator.clipboard.writeText(a.href); toast(t("Copied")) }
    catch(e){ toast(t("Could not copy: select the text instead."),true) }
  };
}
$$("#set-rail button").forEach(b=>b.onclick=()=>showSettingsPane(b.dataset.s));
$$("[data-copy]").forEach(b=>b.onclick=async()=>{
  try{ await navigator.clipboard.writeText($("#"+b.dataset.copy).textContent);
       toast("Copied") }
  catch(e){ toast("Select the text and copy manually",true) }
});

function fillSettings(){
  fillNotify();
  fillBackups();
  fillScans(); if(!SC.st) loadScans();
  const st=S.state||{}, base=location.origin, pr=prefs();
  $("#s-ws").textContent=st.workspace||"";
  $("#s-base").textContent=base;
  $("#s-spec").href=base+"/api/docs"+(st.api_token?"?token="+
    encodeURIComponent(st.api_token):"");
  $("#s-ver").textContent="CV Studio "+(st.version||"");
  $("#s-open").onclick=async()=>{
    try{ await post("/api/reveal",{}) }catch(e){ toast(e.message,true) }
  };
  $("#s-setup").onclick=()=>{ closeOverlays(); onboardingSheet() };
  $("#s-history").onclick=()=>{ closeOverlays(); recentChangesSheet() };
  const ul=$("#s-uilang");
  ul.value=pr.ui_lang||"";
  ul.onchange=()=>{ setPref("ui_lang",ul.value||null); location.reload() };
  const ml=$("#s-multilang"), cvl=$("#s-cvlang"), bcv=st.base;
  ml.checked=multiLang();
  ml.onchange=()=>{ setPref("multilang",ml.checked); applyMultiLang(); paintBase();
    if(S.view==="docs") drawDocuments(); if(S.jsel&&S.view==="jobs") drawJobs(); fillSettings() };
  /* With one language, which one it is, written onto the base CV. With
     several, each base CV already says its own on Documents. */
  $("#s-cvlang-row").hidden=ml.checked||!bcv||bcv.missing;
  const cur=(((st.documents||[]).find(d=>bcv&&d.path===bcv.path))||{}).lang||"en";
  cvl.innerHTML=(st.languages||[]).map(l=>'<option value="'+l.code+'"'+(l.code===cur?" selected":"")+'>'+
    esc(l.native)+(l.native!==l.english?' · '+esc(l.english):'')+'</option>').join("");
  cvl.onchange=async()=>{
    const l=(st.languages||[]).find(x=>x.code===cvl.value); if(!l||!bcv) return;
    try{
      const r=await post("/api/language/set",{path:bcv.path,language:l.code});
      if(r&&r.ok===false) throw new Error(r.error||"Could not save");
      const s2=await api("/api/state"); S.state=s2; renderDocs(s2.documents);
      S.baseThumb=null; paintBase(); toast(t("Saved"));
    }catch(e){ toast(e.message,true) }
  };
  const tzs=$("#s-tz");
  tzs.innerHTML='<option value="">'+esc(t("Match system"))+' · '+esc(tzCity(machineTz()))+'</option>'+
    tzOptions(pr.tz||"",null);
  tzs.value=pr.tz||"";
  tzs.onchange=()=>{ setPref("tz",tzs.value||null);
    try{ sessionStorage.setItem("cvs.reopen","region") }catch(e){}
    location.reload() };
  drawTzMap(tzs);
  /* Examples rather than names: "30/09/2026" says what "day first" means. */
  const sample=o=>{ const p={d:"30",m:"09",y:"2026"}; return DFX_ORDERS[o].map(k=>p[k]).join(dateSep(o)) };
  const df=$("#s-datefmt"), autoOrder=(()=>{ const keep=pr.date_format; delete prefs().date_format;
    const o=dateOrder(); if(keep) prefs().date_format=keep; return o })();
  df.innerHTML='<option value="">'+esc(t("Match system"))+' · '+esc(sample(autoOrder))+'</option>'+
    [["dmy","Day first"],["mdy","Month first"],["ymd","Year first"]].map(([k,l])=>
      '<option value="'+k+'">'+esc(t(l))+' · '+esc(sample(k))+'</option>').join("");
  df.value=pr.date_format||"";
  const ck=$("#s-clock"), autoClock=(()=>{ const keep=pr.clock; delete prefs().clock;
    const c=clock12(); if(keep) prefs().clock=keep; return c })();
  const at=c=>c?"2:30 PM":"14:30";
  ck.innerHTML='<option value="">'+esc(t("Match system"))+' · '+at(autoClock)+'</option>'+
    '<option value="24">'+esc(t("24-hour"))+' · 14:30</option><option value="12">'+esc(t("12-hour"))+' · 2:30 PM</option>';
  ck.value=pr.clock||"";
  const reopen=()=>{ try{ sessionStorage.setItem("cvs.reopen","region") }catch(e){} location.reload() };
  /* Saved before the reload, which would otherwise cut the request off. */
  const keep=(k,v)=>{ setPref(k,v); post("/api/prefs",{set:{[k]:v}}).catch(()=>{}).finally(reopen) };
  df.onchange=()=>keep("date_format",df.value||null);
  ck.onchange=()=>keep("clock",ck.value||null);
  const sb=$("#s-sample");
  sb.textContent=st.sample?"Back to my workspace":"Open sample data";
  sb.onclick=()=>setSample(!st.sample,sb);
  $("#s-exp").onclick=()=>download("/api/jobs/export?format=json"+tok(),"applications.json");
  $("#s-exp-csv").onclick=()=>download("/api/jobs/export?format=csv"+tok(),"applications.csv");
  /* Revealing is deliberate and one click; copying never needs it. */
  const key=$("#s-key");
  key.hidden=!(S.state&&S.state.api_token);
  key.textContent=S.keyShown?"Hide the key":"Show the key";
  key.onclick=()=>{ S.keyShown=!S.keyShown; fillSettings() };
  $("#s-check").onclick=()=>checkUpdates(true);

  const live=$("#s-live");
  live.checked=pr.live!==false;
  live.onchange=()=>setPref("live",live.checked);
  const delay=$("#s-delay");
  delay.value=String(pr.delay||700);
  delay.onchange=()=>setPref("delay",Number(delay.value));
  const acc=prefs().accent||"ochre";
  $("#s-accent").innerHTML=ACCENTS.map(([id,label])=>
    '<button data-accent="'+id+'" title="'+label+'" aria-label="'+label+
    '" aria-pressed="'+String(id===acc)+'"></button>').join("");
  $$("#s-accent button").forEach(b=>{
    /* Painted from the theme's own token rather than a colour repeated here,
       so a swatch can never drift from what it selects. */
    b.style.background=getComputedStyle(document.documentElement)
      .getPropertyValue("--sw-"+b.dataset.accent).trim();
    b.onclick=()=>{ setPref("accent",b.dataset.accent); applyAppearance();
      $$("#s-accent button").forEach(x=>
        x.setAttribute("aria-pressed",String(x===b))); };
  });
  const ap=$("#s-appearance");
  ap.value=pr.appearance||"system";
  ap.onchange=()=>{ setPref("appearance",ap.value); applyAppearance() };
  const dt=$("#s-deftheme");
  if(dt&&!dt.dataset.filled){
    dt.innerHTML=(st.themes||[]).map(t=>"<option value=\""+esc(t)+"\">"+esc(themeLabel(t))+"</option>").join("");
    dt.dataset.filled="1";
  }
  if(dt){ dt.value=pr.theme||(st.themes||[])[0]||"";
          dt.onchange=()=>setPref("theme",dt.value) }

  /* The hand-setup snippets come from the server: each client has its own
     config format, and there is no reason for two places to know both. */
  fillAIPanel();

  /* Masked by default: this pane ends up in screenshots and screen shares,
     and the key in it is live. Copy still copies the real thing. */
  const shown=st.api_token&&S.keyShown?st.api_token
    :st.api_token?"•".repeat(16):"";
  const auth=st.api_token?' \\\n  -H "X-API-Key: '+shown+'"':"";
  $("#s-curl").textContent=
    "curl "+base+"/api/state"+auth+"\n\n"+
    "curl -X POST "+base+"/api/render"+auth+" \\\n"+
    '  -H "Content-Type: application/json" \\\n'+
    "  -d '{\"path\":\"profile/my-cv.yaml\"}'";
  $("#s-auth").textContent=st.api_token
    ? "An X-API-Key header is required; the key is shown in the example below."
    : "None needed. The server accepts local connections only. Start it with "+
      "--token to require a key, or --host to expose it, which forces one.";
}

/* ---- what a new version says about itself ------------------------------
   The release notes are assembled from CHANGELOG.md and land in the updater
   manifest, so `body` is a real account of what changed rather than the same
   install note every time. The install note is still on the end of it, inside
   a <details>, which is for someone downloading the file by hand -- this app
   is already installed. Cut it off.

   Rendered rather than printed: the notes are markdown, and a wall of "- **"
   reads worse than nothing. Escaped first, then the three marks the notes
   actually use are put back, so nothing in a release body can inject markup. */
function updateNotes(body){
  const head=String(body||"").split(/\n---\s*\n|<details/)[0].trim();
  if(!head) return "";
  const inline=t=>esc(t)
    .replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>")
    .replace(/`([^`]+)`/g,'<code>$1</code>');
  /* Blocks, not lines. The notes are hard-wrapped, so a bullet is its "- "
     line plus every continuation under it; taking each line as its own block
     turned one bullet into a list item followed by a stray paragraph. */
  const blocks=[];
  for(const raw of head.split("\n")){
    const item=raw.match(/^\s*[-*]\s+(.*)$/);
    const last=blocks.length?blocks[blocks.length-1]:null;
    if(item) blocks.push({list:true,text:item[1]});
    else if(!raw.trim()){ if(last) blocks.push(null) }
    else if(last) last.text+=" "+raw.trim();
    else blocks.push({list:false,text:raw.trim()});
  }
  let html="", list=false;
  for(const b of blocks){
    if(!b){ if(list){ html+="</ul>"; list=false } continue }
    if(b.list&&!list){ html+="<ul>"; list=true }
    if(!b.list&&list){ html+="</ul>"; list=false }
    html+=b.list?"<li>"+inline(b.text)+"</li>":"<p>"+inline(b.text)+"</p>";
  }
  return html+(list?"</ul>":"");
}

/* One download, wherever it was started from: the panel in Settings and the
   panel that comes to you both report into `say`. */
async function installUpdate(up,say,done){
  const T=window.__TAURI__;
  let total=0, got=0;
  try{
    await up.downloadAndInstall(e=>{
      if(e.event==="Started") total=e.data.contentLength||0;
      if(e.event==="Progress"){
        got+=e.data.chunkLength||0;
        say(total?"Downloading "+Math.round(got/total*100)+"%":"Downloading\u2026");
      }
      if(e.event==="Finished") say("Installing\u2026");
    });
    say("Restarting\u2026");
    if(T.process&&T.process.relaunch) await T.process.relaunch();
  }catch(err){ say("Update failed: "+err); if(done) done(err) }
}

/* An update used to arrive as a toast saying to go and look in Settings, which
   is a notification about a notification. It comes to you now. Once per
   version: saying Later means later, not at every launch until you give in --
   Settings still has it whenever you want it. */
function updateSheet(up){
  if(!$("#sheet").hidden) return;      /* never over something being filled in */
  const have=(S.state&&S.state.version)||"";
  const notes=updateNotes(up.body);
  openSheet(
    '<div><h3 id="sheet-title">CV Studio '+esc(up.version)+' is ready</h3>'+
    (have?'<p>You have '+esc(have)+'. It installs and restarts in one step; '+
          'nothing in your workspace is touched.</p>':"")+'</div>'+
    (notes?'<div class="relnotes" id="u-notes">'+notes+'</div>':"")+
    '<div class="foot"><span class="mono" id="u-say"></span>'+
    '<button class="sbtn" id="u-later">Later</button>'+
    '<button class="sbtn primary" id="u-now">Install and restart</button></div>');
  $("#u-later").onclick=()=>{ setPref("skipUpdate",up.version); closeSheet() };
  $("#u-now").onclick=()=>{
    $("#u-now").disabled=true; $("#u-later").disabled=true;
    installUpdate(up,t=>{ $("#u-say").textContent=t },
      ()=>{ $("#u-now").disabled=false; $("#u-later").disabled=false });
  };
}

/* Tauri's updater verifies a signature against the public key baked into the
   build, so a compromised release host still cannot push a package this app
   will install. */
async function checkUpdates(loud){
  const T=window.__TAURI__;
  const st=$("#u-state"), act=$("#u-actions");
  if(!T||!T.updater){
    if(st) st.textContent="Updates are available in the desktop app only.";
    const b=$("#s-check"); if(b) b.hidden=true;
    return;
  }
  if(st) st.innerHTML='<span class="spin"></span> Checking for updates…';
  if(act) act.innerHTML="";
  try{
    const up=await T.updater.check();
    if(!up){ if(st) st.textContent="You are on the latest version."; return }
    if(st) st.innerHTML="Version <b>"+esc(up.version)+"</b> is available."+
      (up.body?'<br>'+esc(up.body).slice(0,300):"");
    if(act){
      act.innerHTML='<button class="sbtn primary" id="u-go">Download and install</button>';
      $("#u-go").onclick=()=>{
        $("#u-go").disabled=true;
        installUpdate(up,t=>{ st.textContent=t },
          ()=>{ $("#u-go").disabled=false });
      };
    }
    if(!loud&&prefs().skipUpdate!==up.version) updateSheet(up);
  }catch(e){
    /* A 404 here almost always means no release has been published yet, or the
       repository is private so the asset cannot be fetched without credentials.
       Saying that is more useful than relaying the transport error. */
    const raw=String(e);
    if(st) st.textContent=/release JSON|404|not found/i.test(raw)
      ? "No published release to update to yet. Updates begin working once a version "+
        "is tagged and the release is publicly downloadable."
      : "Could not check for updates: "+raw;
  }
}
setTimeout(()=>{ if(window.__TAURI__&&window.__TAURI__.updater) checkUpdates(false) },4000);

/* The YAML tab is where the model actually writes, so it is the one view where
   losing the selection hurts most. The source lines come from ruamel, which
   knows exactly where it parsed each node -- no guessing, no string search.
   Drawn as a band behind the text rather than by re-marking the highlighted
   HTML, so syntax colouring and the invisible textarea both stay untouched. */
function selectedLines(){
  const m=(S.doc&&S.doc.lines)||{}, sel=S.sel;
  if(!sel) return null;
  if(sel.kind==="header") return m.header||null;
  if(sel.kind==="entry"){
    return m[sel.name+"/"+sel.i]||m[sel.name]||null;
  }
  return m[sel.name]||null;
}
/* The other direction of the same thread. S.doc.lines maps every block to the
   span of source that holds it; this reads it backwards, so the line the caret
   is on says which entry you are in and the page can follow the source the way
   it follows the form. Narrowest span wins, or an entry would always lose to
   the section around it. */
function blockAtLine(line){
  const m=(S.doc&&S.doc.lines)||{};
  let best=null, width=Infinity;
  for(const key of Object.keys(m)){
    const span=m[key];
    if(!span) continue;
    const end=Math.max(span[0]+1,span[1]);
    if(line<span[0]||line>=end) continue;
    const w=end-span[0];
    if(w>=width) continue;
    width=w;
    const cut=key.lastIndexOf("/");
    best=key==="header" ? {kind:"header"}
       : cut<0 ? {kind:"section",name:key}
       : {kind:"entry",name:key.slice(0,cut),i:+key.slice(cut+1)};
  }
  return best;
}
/* selectionchange rather than click: the caret arrives by arrow key and by
   typing at least as often as by mouse. Nothing happens unless it has crossed
   into a different block, so walking within one entry costs nothing. */
document.addEventListener("selectionchange",()=>{
  if(S.view!=="cvs"||S.tab!=="yaml") return;
  const ta=$("#yaml");
  if(document.activeElement!==ta) return;
  const line=ta.value.slice(0,ta.selectionStart).split("\n").length-1;
  const at=blockAtLine(line);
  if(at&&!sameSel(at,S.sel)) select(at,"yaml");
});
function markYamlSelection(){
  const wrap=$(".edwrap"), ta=$("#yaml");
  if(!wrap||!ta) return;
  let band=wrap.querySelector(".yband");
  const span=selectedLines();
  if(!span){ if(band) band.remove(); return }
  if(!band){
    band=document.createElement("div");
    band.className="yband";
    wrap.insertBefore(band,wrap.firstChild);
  }
  /* Line height and padding come from the computed style rather than repeating
     the numbers here, so the band cannot drift if the type changes. */
  const cs=getComputedStyle(ta);
  const lh=parseFloat(cs.lineHeight), top=parseFloat(cs.paddingTop);
  band.style.top=(top+span[0]*lh)+"px";
  band.style.height=(Math.max(1,span[1]-span[0])*lh)+"px";
  band.style.transform="translateY("+(-ta.scrollTop)+"px)";
}
/* Keep it pinned while the source scrolls under it. */
$("#yaml").addEventListener("scroll",()=>{
  const band=$(".edwrap .yband");
  if(band) band.style.transform="translateY("+(-$("#yaml").scrollTop)+"px)";
});

/* ---- Search: everything, from anywhere ------------------------------------
   Ctrl K (⌘K on a Mac), or the magnifier in the top bar. Before you type: what
   you opened last, and the places people go. As you type: applications by
   company and role, documents by name and by what is in them (the server reads
   those), the words inside postings and notes, and the places again. */
const PAL={sel:0,items:[],q:"",docs:null,seq:0};
const PAL_ICONS={
  doc:'<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/>',
  letter:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  list:'<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  cal:'<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
  funnel:'<path d="M4 5h16l-6 8v6l-4-2v-4z"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
};
const palIcon=k=>'<span class="ic"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" '+
  'stroke-width="2" aria-hidden="true">'+PAL_ICONS[k]+'</svg></span>';
const PAL_MAC=/Mac|iPhone|iPad/.test(navigator.platform||"");
/* The places, in the order people want them. */
const palPlaces=()=>[
  {label:t("New application"),icon:"plus",run:()=>{ setView("jobs"); newJobSheet() }},
  {label:t("New document"),sub:t("CV or cover letter"),icon:"plus",run:()=>newDocumentSheet()},
  {label:t("Applications"),icon:"list",run:()=>{ S.jfilter={kind:"all",value:""}; setView("jobs"); drawJobs() }},
  {label:t("Documents"),icon:"doc",run:()=>setView("docs")},
  {label:t("Funnel"),icon:"funnel",run:()=>setView("funnel")},
  {label:t("Calendar"),icon:"cal",run:()=>setView("cal")},
  {label:t("Follow-up due"),icon:"list",run:()=>{ S.jfilter={kind:"alert",value:"followup_due"}; S.fnode=null; setView("jobs"); drawJobs() }},
  ...[["workspace","Workspace"],["editor","Editor"],["region","Language & region"],["notify","Notifications"],["browser","Save from the browser"],
      ["ai","AI clients"],["api","API"],["updates","Updates"],["about","About"]]
    .map(([k,l])=>({label:t("Settings")+" › "+t(l),icon:"gear",stay:true,run:()=>openSettings(k)})),
];
/* Lower case without accents, one character for one, so a position in the
   folded text is a position in the original. */
const palFold=s=>Array.from(String(s||""),c=>(c.normalize("NFD")[0]||c).toLowerCase()).join("");
const palWords=q=>palFold(q).split(/\s+/).filter(Boolean);
const palHas=(text,words)=>{ const f=palFold(text); return words.every(w=>f.includes(w)) };
/* The text with every word found in it marked. */
function palMark(text,words){
  text=String(text||""); const f=palFold(text), spans=[];
  words.forEach(w=>{ let i=f.indexOf(w); while(i>=0){ spans.push([i,i+w.length]); i=f.indexOf(w,i+w.length) } });
  spans.sort((a,b)=>a[0]-b[0]);
  let out="", at=0;
  spans.forEach(([a,b])=>{ if(a<at) a=at; if(b<=a) return;
    out+=esc(text.slice(at,a))+"<mark>"+esc(text.slice(a,b))+"</mark>"; at=b });
  return out+esc(text.slice(at));
}
/* A few words either side of the first match, on one line. */
function palSnip(text,words){
  const flat=String(text||"").replace(/[#*_>`\[\]]/g,"").replace(/\s+/g," ").trim();
  const at=palFold(flat).indexOf(words[0]); if(at<0) return flat.slice(0,110);
  const a=Math.max(0,at-48), b=Math.min(flat.length,at+80);
  return (a?"…":"")+flat.slice(a,b).trim()+(b<flat.length?"…":"");
}
const palJobLine=j=>[prettyStatus(j.status)?t(prettyStatus(j.status)):"",j.location].filter(Boolean).join(" · ");

function palRemember(item){
  const key=item.job?"job:"+item.job:"doc:"+item.doc;
  const list=(prefs().recent||[]).filter(x=>(x.job?"job:"+x.job:"doc:"+x.doc)!==key);
  list.unshift(item);
  prefs().recent=list.slice(0,6);
  clearTimeout(PAL.saveT);
  /* Walking the list with the arrows opens one application after another:
     remember where it settled, not every stop. */
  PAL.saveT=setTimeout(()=>setPref("recent",prefs().recent),900);
}

function palBuild(){
  const q=PAL.q.trim(), words=palWords(q), groups=[];
  const docs=(S.state&&S.state.documents)||[];
  /* A document written for an application is named by it: "CV · Qwant". */
  const docJob=d=>(S.jobs||[]).find(x=>x.cv_path===d.path||x.letter_path===d.path);
  const docItem=(d,sub,kind)=>{ const j=docJob(d), letter=isLetterPath(d.path);
    return {html:palIcon(letter?"letter":"doc"),
      title:j?esc(t(letter?"Cover letter":"CV"))+" · "+esc(j.company)+(d.lang?" ("+esc(d.lang.toUpperCase())+")":""):esc(d.label||d.path),
      sub, kind:kind||t(letter?"Letter":"CV"), run:()=>openDoc(d.path)} };
  /* A document is described by what it is for, not by its path. */
  const docSub=d=>esc(docJob(d)?d.label:d.lang?langName(d.lang):"");
  const jobItem=(j,sub,kind,title)=>({html:'<span class="ic">'+companyMark(j)+'</span>',
    title:title||esc(j.company)+" · "+esc(j.title), sub:sub==null?esc(palJobLine(j)):sub,
    kind:kind==null?t("Application"):kind, run:()=>openJob(j.id)});
  if(!words.length){
    const recent=(prefs().recent||[]).map(r=>{
      if(r.job){ const j=(S.jobs||[]).find(x=>x.id===r.job); return j&&jobItem(j) }
      const d=docs.find(x=>x.path===r.doc); return d&&docItem(d,docSub(d))
    }).filter(Boolean).slice(0,4);
    if(recent.length) groups.push([t("Recent"),recent]);
    /* New application, New document, Calendar, Funnel, Settings › Notifications. */
    const pl=palPlaces();
    groups.push([t("Go to"),[0,6,1,5,4].map(i=>pl[i])
      .map(p=>({html:palIcon(p.icon),title:esc(p.label),sub:p.sub?esc(p.sub):"",kind:"",stay:p.stay,run:p.run}))]);
  } else {
    const jobs=S.jobs||[], apps=[], inside=[];
    jobs.forEach(j=>{
      const head=[j.company,j.title,j.location].join(" ");
      if(palHas(head,words)) apps.push(jobItem(j,null,"",palMark(j.company,words)+" · "+palMark(j.title,words)));
      else for(const [field,label] of [["notes","Notes"],["description","Posting"]]){
        if(j[field]&&palHas(head+" "+j[field],words)&&palHas(j[field],words.slice(0,1))){
          inside.push(jobItem(j,esc(t(label))+": “"+palMark(palSnip(j[field],words),words)+"”",t(label)));
          break;
        }
      }
    });
    /* The rest are one step away, in the table with the same words. */
    if(apps.length>6) apps.splice(6,0,{html:palIcon("list"),title:esc(t("Show all {n} in Applications",{n:apps.length})),sub:"",kind:"",
      run:()=>{ S.jfilter={kind:"all",value:""}; S.fnode=null; S.jsel=null; $("#jobq").value=PAL.q.trim(); setView("jobs"); drawJobs() }});
    if(apps.length) groups.push([t("Applications"),apps.slice(0,7),apps.length>6?apps.length-1:apps.length]);
    const seen=new Set(), dl=[];
    docs.forEach(d=>{ if(palHas(d.label+" "+d.path,words)){ seen.add(d.path); dl.push(docItem(d,docSub(d))) } });
    (PAL.docs||[]).forEach(d=>{ if(seen.has(d.path)) return;
      /* The line as it reads, without the YAML key or list dash in front. */
      dl.push(docItem(d,palMark(String(d.line||"").replace(/^\s*(?:-\s+)?(?:[\w-]+:\s+)?/,""),words))) });
    if(dl.length) groups.push([t("Documents"),dl.slice(0,6),dl.length]);
    if(inside.length) groups.push([t("In postings and notes"),inside.slice(0,5),inside.length]);
    const places=palPlaces().filter(p=>palHas(p.label,words));
    if(places.length) groups.push([t("Go to"),places.slice(0,4).map(p=>({html:palIcon(p.icon),
      title:palMark(p.label,words),sub:p.sub?esc(p.sub):"",kind:"",stay:p.stay,run:p.run}))]);
  }
  return groups;
}

function palDraw(){
  const groups=palBuild(), list=$("#pal-list");
  PAL.items=[]; let html="";
  groups.forEach(([head,items,n])=>{
    html+='<div class="pal-h" role="presentation">'+esc(head)+(n>1?'<span>'+n+'</span>':"")+'</div>';
    items.forEach(it=>{
      const i=PAL.items.push(it)-1;
      html+='<button type="button" class="pal-it" role="option" id="pal-o'+i+'" data-i="'+i+'" tabindex="-1" '+
        'aria-selected="'+(i===PAL.sel)+'">'+it.html+
        '<span class="tx"><b data-noi18n>'+it.title+'</b>'+(it.sub?'<small data-noi18n>'+it.sub+'</small>':"")+'</span>'+
        (it.kind?'<span class="kd">'+esc(it.kind)+'</span>':"")+'</button>';
    });
  });
  if(!PAL.items.length) html='<div class="pal-none">'+esc(t("Nothing matches “{q}”.",{q:PAL.q.trim()}))+'</div>';
  list.innerHTML=html;
  PAL.sel=Math.min(PAL.sel,Math.max(0,PAL.items.length-1));
  /* The footer agrees with the group counts: each group lists its first few. */
  const n=PAL.items.length, total=groups.reduce((a,[,items,c])=>a+(c||items.length),0);
  $("#pal-count").textContent=PAL.q.trim()?t("{n} result(s)",{n:total}):(PAL_MAC?t("⌘K, from anywhere"):t("Ctrl K, from anywhere"));
  $("#pal-in").setAttribute("aria-activedescendant",n?"pal-o"+PAL.sel:"");
  $$("#pal-list .pal-it").forEach(b=>{
    b.onclick=()=>palRun(+b.dataset.i);
    b.onmousemove=()=>{ if(PAL.sel!==+b.dataset.i){ PAL.sel=+b.dataset.i; palMarkSel() } };
  });
}
function palMarkSel(){
  $$("#pal-list .pal-it").forEach(b=>b.setAttribute("aria-selected",String(+b.dataset.i===PAL.sel)));
  $("#pal-in").setAttribute("aria-activedescendant",PAL.items.length?"pal-o"+PAL.sel:"");
  const el=$("#pal-o"+PAL.sel); if(el) el.scrollIntoView({block:"nearest"});
}
async function palRun(i){
  const it=PAL.items[i]; if(!it) return;
  /* Settings opens over the editor; everything else leaves it. */
  if(!it.stay&&!await leaveEdits()) return;
  closePal();
  it.run();
}
async function palFetch(){
  const q=PAL.q.trim(), seq=++PAL.seq;
  if(!q){ PAL.docs=null; return }
  try{
    const r=await api("/api/search?q="+encodeURIComponent(q));
    if(seq!==PAL.seq) return;       /* a newer query already answered */
    PAL.docs=r.documents||[]; palDraw();
  }catch(e){}
}
function openPal(){
  if(onbOpen()) return;
  if(!S.jready) loadJobs(true);
  PAL.q=""; PAL.sel=0; PAL.docs=null;
  $("#pal").hidden=false; $("#pal-scrim").hidden=false;
  const inp=$("#pal-in"); inp.value=""; palDraw(); inp.focus();
}
function closePal(){ $("#pal").hidden=true; $("#pal-scrim").hidden=true }
$("#pal-scrim").onclick=closePal;
$("#btn-search").onclick=()=>$("#pal").hidden?openPal():closePal();
if(PAL_MAC){ $("#tsearch-kbd").textContent="⌘K"; $("#btn-search").title=t("Search everything (⌘K)") }
$("#pal-in").addEventListener("input",e=>{
  PAL.q=e.target.value; PAL.sel=0; palDraw();
  clearTimeout(PAL.fetchT); PAL.fetchT=setTimeout(palFetch,140);
});
$("#pal-in").addEventListener("keydown",e=>{
  const n=PAL.items.length;
  if(e.key==="ArrowDown"||e.key==="ArrowUp"){
    e.preventDefault(); if(!n) return;
    PAL.sel=(PAL.sel+(e.key==="ArrowDown"?1:-1)+n)%n; palMarkSel();
  }else if(e.key==="Enter"){ e.preventDefault(); palRun(PAL.sel) }
  else if(e.key==="Escape"){ e.preventDefault(); e.stopPropagation(); closePal() }
});
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&!e.altKey&&e.key.toLowerCase()==="k"){
    e.preventDefault(); $("#pal").hidden?openPal():closePal();
  }
});


