/* Applications loader, desktop notifications, and job scans swipe deck. */
/* =========================================================================
   Jobs
   ========================================================================= */
async function loadJobs(quiet){
  try{
    const d=await api("/api/jobs");
    /* With an application open, the list keeps the order it had: a reload
       from the poll sorted the edited row to the top under it. */
    if(S.jsel&&S.jobs&&S.jobs.length){
      const at=new Map(S.jobs.map((j,i)=>[j.id,i]));
      d.jobs.sort((a,b)=>(at.has(a.id)?at.get(a.id):-1)-(at.has(b.id)?at.get(b.id):-1));
    }
    S.jobs=d.jobs; S.statuses=d.statuses; S.nodes=d.nodes||{}; S.labels=d.labels||{};
    adoptLifecycle(d);
    S.jready=true;
  }catch(e){
    S.jready=false;
    if(!quiet) $("#jobrows").innerHTML='<div class="empty"><h3>Could not load</h3><p>'+
      esc(e.message)+'</p></div>';
    return;
  }
  S.funnel=null;
  if(S.view==="jobs") drawJobs();
  if(S.view==="docs") drawDocuments();
  if(S.view==="funnel") loadFunnel("still");
  if(S.view==="cal") drawCalendar(false);
  if(S.view==="cvs"&&S.path){
    paintTitle(); paintLink();
    if(!$("#ed").hidden) buildInspector();
  }
  /* The rail marks which documents belong to an application, so it has to
     be redrawn once we know what the applications are. */
  if(S.state) renderDocs(S.state.documents);
  paintStatus();
}

/* The sidebar is the filter. Statuses come from the store rather than a list
   written here, so a status added to jobs.py shows up without a UI change. */
function statusCounts(){
  const c={};
  S.jobs.forEach(j=>{ c[j.status]=(c[j.status]||0)+1 });
  return c;
}
const NO_LETTER=j=>!j.letter_path&&!DEAD_ST.has(j.status);
const SAVED={"Open, no letter":NO_LETTER};

/* Attention is computed by the server, not here. The same four rules answer
   the desktop notification and the digest an AI client reads out, and three
   copies of "what counts as overdue" would have drifted apart within a month.
   Needs-follow-up used to live in SAVED above and is now one of them. */
const ATTENTION=[
  ["interview_soon",   "Interviews in the next 7 days"],
  ["followup_due",     "Follow-up due"],
  ["interview_passed", "Interview, no outcome"],
  ["silent",           "No reply in 2 weeks"],
];

async function loadAlerts(){
  try{ S.alerts=await api("/api/alerts") }catch(e){ S.alerts=null; return }
  if(S.view==="jobs") drawRail();
  notifyAlerts();
}

/* ---- Notifications ---------------------------------------------------- */
/* Nothing outside this app knows when an interview is, so a reminder has to
   come from here. Opt-in, from Settings > Notifications: before each
   interview at the leads chosen there, and once a day for follow-ups, in one
   notification rather than one per application. Each is sent once: what was
   sent is remembered on this machine. They come while the app is open. */
const notifyOn=()=>!!prefs().notify;
const notifyIv=()=>{ const v=prefs().notify_iv??"60,10"; return v==="off"?[]:String(v).split(",").map(Number).sort((a,b)=>b-a) };
const notifyFu=()=>{ const v=prefs().notify_fu??"9"; return v==="off"?null:+v };
function notifyApi(){
  const N=window.__TAURI__&&window.__TAURI__.notification;
  if(N) return {
    granted:()=>N.isPermissionGranted(),
    ask:async()=>(await N.requestPermission())==="granted",
    send:(title,body)=>N.sendNotification({title,body})};
  if(!("Notification" in window)) return null;
  return {
    granted:async()=>Notification.permission==="granted",
    ask:async()=>(await Notification.requestPermission())==="granted",
    send:(title,body,onclick)=>{ const n=new Notification(title,{body,icon:"/static/brand-mark-256.png"});
      n.onclick=()=>{ window.focus(); if(onclick) onclick(); n.close() } }};
}
function notifySeen(){ return Object.assign({},prefs().notified||{}) }
function notifyMark(k){
  const m=notifySeen(), cut=Date.now()-14*864e5;
  for(const x in m) if(m[x]<cut) delete m[x];
  m[k]=Date.now(); setPref("notified",m);
}
async function notifySend(key,title,body,onclick){
  const A=notifyApi(); if(!A) return false;
  try{ if(!(await A.granted())) return false; A.send(title,body,onclick); if(key) notifyMark(key); return true }
  catch(e){ return false }
}
const inWords=ms=>{ const m=Math.max(1,Math.round(ms/6e4));
  if(m<60) return t(m===1?"in 1 minute":"in {n} minutes",{n:m});
  const h=Math.round(m/60);
  if(h<24) return t(h===1?"in 1 hour":"in {n} hours",{n:h});
  return t("tomorrow") };
async function notifyTick(){
  if(!notifyOn()||!S.jready) return;
  const now=Date.now(), seen=notifySeen();
  /* Interviews: the largest lead whose moment has come and not been sent.
     Opening the app half an hour before still gets one, saying so. */
  const leads=notifyIv();
  for(const j of S.jobs||[]){
    const at=interviewMoment(j); if(!at||at<=now||!leads.length) continue;
    const base="iv:"+j.id+":"+at.getTime()+":";
    const due=leads.filter(L=>now>=at-L*6e4);
    if(!due.length) continue;
    const L=due[due.length-1];
    if(seen[base+L]) continue;
    const ok=await notifySend(base+L,t("Interview {when} · {co}",{when:inWords(at-now),co:j.company}),
      j.title+" · "+interviewLine(j),()=>{ setView("jobs"); selectJob(j.id) });
    if(ok) due.forEach(x=>notifyMark(base+x));
  }
  /* Follow-ups: once a day, from the hour chosen. */
  const hr=notifyFu();
  if(hr!=null&&new Date().getHours()>=hr){
    const today=isoToday(), key="fu:"+today;
    if(!seen[key]){
      const list=(S.jobs||[]).filter(j=>j.followup_date&&!DEAD_ST.has(j.status)&&String(j.followup_date).slice(0,10)<=today);
      if(list.length){
        const late=list.filter(j=>String(j.followup_date).slice(0,10)<today).length;
        const names=list.map(j=>j.company); const shown=names.length>4?names.slice(0,4).join(", ")+" +"+(names.length-4):names.join(", ");
        await notifySend(key,list.length===1?t("Follow up with {co} today",{co:list[0].company})
            :t("{n} follow-ups to send",{n:list.length}),
          shown+(late?" · "+t(late===1?"1 is late":"{n} are late",{n:late}):""),
          ()=>{ S.jfilter={kind:"alert",value:"followup_due"}; setView("jobs") });
      }else notifyMark(key);
    }
  }
}
setInterval(notifyTick,30000);
async function notifyAlerts(){ notifyTick() }

/* =========================================================================
   Job scans
   ========================================================================= */
/* What a scheduled search found and nobody has said yes or no to yet, as a
   deck of cards. Right adds the job to the applications, its posting read
   from its link; left passes on it, and the spreadsheet remembers, so
   tomorrow's search cannot bring it back. Down leaves it for later, at the
   bottom of the deck. Every decision can be taken back, the last first. */
/* var, not const: closeOverlays, far above, asks whether the deck is open. */
var SC={st:null, deck:[], at:0, log:[], open:false, drag:null, anim:false};
const scanNew=()=>(SC.st&&SC.st.new)||[];
const SC_FLY={yes:"fly-yes",no:"fly-no",later:"fly-later"}, SC_BACK={yes:"back-yes",no:"back-no",later:"back-later"};
const SC_ICON={
  deck:'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '+
    'stroke-linejoin="round"><rect x="6" y="3" width="12" height="16" rx="2.5" transform="rotate(8 12 11)"/>'+
    '<rect x="5" y="5" width="12" height="16" rx="2.5"/></svg>',
  no:'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '+
    'stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  yes:'<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '+
    'stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  later:'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" '+
    'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
  undo:'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" '+
    'stroke-linecap="round" stroke-linejoin="round"><path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 010 12h-3"/></svg>',
};
async function loadScans(){
  try{ SC.st=await api("/api/scans") }catch(e){ SC.st=null }
  paintScanBadge();
  if(S.view==="jobs") drawScanBar();
  if(!$("#ovl-settings").hidden&&!$("#sp-scans").hidden) fillScans();
  notifyScans();
}
function paintScanBadge(){
  const b=$("#scan-badge"), n=scanNew().length;
  b.hidden=!n; b.textContent=n>99?"99+":String(n);
  b.title=n===1?t("1 new job from your scans"):t("{n} new jobs from your scans",{n});
}
const scanDayKey=d=>d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
function foundWords(k){
  k=String(k||"").slice(0,10);
  if(!k) return "";
  if(k===scanDayKey(new Date())) return t("Found today");
  if(k===scanDayKey(new Date(Date.now()-864e5))) return t("Found yesterday");
  return t("Found {d}",{d:/^\d{4}-\d\d-\d\d$/.test(k)?shortDate(k):k});
}
function postedWords(v){
  v=String(v||"").trim();
  if(/^\d{4}-\d\d-\d\d/.test(v)) return shortDate(v.slice(0,10));
  return /unknown|inconnue|desconocida|desconhecida/i.test(v)?t("date unknown"):v;
}
function drawScanBar(){
  const el=$("#scanbar"); if(!el) return;
  const list=scanNew(), q=($("#jobq").value||"").trim();
  if(!list.length||q||S.jsel){ el.hidden=true; return }
  const names=[...new Set(list.map(r=>r.scan).filter(Boolean))];
  const shown=names.length>2?names.slice(0,2).join(", ")+" +"+(names.length-2):names.join(", ");
  el.hidden=false;
  el.innerHTML='<span class="sb-ic" aria-hidden="true">'+SC_ICON.deck+'</span>'+
    '<span class="sb-tx"><b>'+esc(list.length===1?t("1 new job from your scans"):t("{n} new jobs from your scans",{n:list.length}))+'</b>'+
    '<small>'+esc([shown,foundWords(SC.st.latest)].filter(Boolean).join(" · "))+'</small></span>'+
    '<button type="button" class="sbtn primary" id="sb-go">'+esc(t("Review"))+'</button>';
  $("#sb-go").onclick=()=>openScans();
}

/* ---- the deck ---- */
function openScans(){
  SC.deck=scanNew().slice(); SC.at=0; SC.log=[]; SC.open=true;
  if(SC.deck.length) setPref("scans_seen",SC.deck.map(r=>r.key).slice(0,500));
  $("#ovl-scans").hidden=false;
  drawDeck();
  document.addEventListener("keydown",deckKeys,true);
}
async function closeScans(){
  if(!SC.open) return;
  SC.open=false; $("#ovl-scans").hidden=true;
  document.removeEventListener("keydown",deckKeys,true);
  /* An add still reading its posting finishes first, so the list shows it. */
  await Promise.all(SC.log.map(e=>e.wait).filter(Boolean)).catch(()=>{});
  await loadScans();
  if(SC.log.some(e=>e.dir==="yes")) loadJobs(true);
}
$("#sc-done").onclick=closeScans;
function scanAnnounce(msg){ const l=$("#sc-live"); l.textContent=""; setTimeout(()=>{ l.textContent=msg },30) }
const SC_YES=/^(yes|y|true|oui|s[ií]|sim|x|✓|✔|1)$/i, SC_NO=/^(no|n|false|non|n[aã]o|0|-)$/i;
function scanCard(r,depth){
  const top=depth===0, chips=[], facts=[];
  if(r.scan) chips.push('<span class="sc-chip scan">'+esc(r.scan)+'</span>');
  for(const [k,v] of Object.entries(r.details||{})){
    const s=String(v).trim();
    if(SC_YES.test(s)) chips.push('<span class="sc-chip hi">'+esc(k)+'</span>');
    else if(SC_NO.test(s)) continue;
    else if(s.length<=40) chips.push('<span class="sc-chip"><i>'+esc(k)+'</i> '+esc(s)+'</span>');
    else facts.push('<div class="sc-fact"><small>'+esc(k)+'</small><p>'+esc(s)+'</p></div>');
  }
  if(r.posted) chips.push('<span class="sc-chip"><i>'+esc(t("Posted"))+'</i> '+esc(postedWords(r.posted))+'</span>');
  let host=""; try{ host=new URL(r.link).hostname.replace(/^www\./,"") }catch(e){}
  const board=r.link?jobBoard({url:r.link,source:""}):null;
  const co=r.company||host||"?";
  /* A company you have applied to before already has its logo. */
  const known=(S.jobs||[]).find(j=>j.logo_url&&String(j.company||"").toLowerCase()===co.toLowerCase());
  return '<article class="sc-card'+(top?' top':'')+'" data-noi18n data-depth="'+depth+'"'+
    (top?' tabindex="0" aria-roledescription="'+esc(t("job card"))+'" aria-label="'+esc((r.role||"")+", "+co)+'"'
      :' aria-hidden="true"')+'>'+
    (top?'<span class="sc-stamp yes" aria-hidden="true">'+esc(t("Add"))+'</span>'+
      '<span class="sc-stamp no" aria-hidden="true">'+esc(t("Pass"))+'</span>':'')+
    '<header class="sc-co">'+companyMark(known||{company:co})+
      '<span><b>'+esc(co)+'</b>'+(r.location?'<small>'+esc(r.location)+'</small>':'')+'</span>'+
      (hasMatch(r)?matchRing(r.match):'')+'</header>'+
    '<h2 class="sc-role">'+esc(r.role||t("A job"))+'</h2>'+
    (chips.length?'<div class="sc-chips">'+chips.join("")+'</div>':'')+
    (r.why?'<div class="sc-why"><small>'+esc(t("Why it fits"))+'</small><p>'+esc(r.why)+'</p></div>':'')+
    (hasMatch(r)&&r.match.summary?'<div class="sc-fact"><small>'+esc(t(matchBand(r.match.score)[3])+" · "+t("{n} out of 10",{n:matchNum(r.match.score)}))+
      '</small><p>'+esc(r.match.summary)+'</p></div>':'')+
    (r.notes?'<div class="sc-fact"><small>'+esc(t("Notes"))+'</small><p>'+esc(r.notes)+'</p></div>':'')+
    facts.join("")+
    '<footer class="sc-foot">'+(r.link?'<a class="sc-link" href="'+esc(r.link)+'" target="_blank" rel="noopener"'+
        (top?'':' tabindex="-1"')+'>'+(board?boardMark(board):'')+
        '<span>'+esc(t("Open the posting"))+'</span><small>'+esc(host)+'</small></a>'
      :'<span class="sc-nolink">'+esc(t("No link to the posting"))+'</span>')+
      (r.found?'<span class="sc-found">'+esc(foundWords(r.found))+'</span>':'')+'</footer></article>';
}
function scanBtn(kind,label,keys,hint){
  return '<span class="sc-b"><button type="button" class="sc-btn '+kind+'" data-sc="'+kind+'" aria-keyshortcuts="'+keys+'" '+
    'title="'+esc(label+" ("+hint+")")+'">'+SC_ICON[kind]+'</button><small aria-hidden="true">'+esc(label)+'</small></span>';
}
function drawDeck(back){
  const left=SC.deck.length-SC.at, body=$("#sc-body");
  $("#sc-count").textContent=left?t("{n} of {m}",{n:SC.at+1,m:SC.deck.length}):"";
  if(!left){
    body.innerHTML='<div class="sc-main">'+scanEnd()+'</div><aside class="sc-log" id="sc-log"></aside>';
    const u=$("#sc-end-undo"); if(u) u.onclick=undoLast;
    const s=$("#sc-end-see"); if(s) s.onclick=async()=>{ await closeScans(); setView("jobs") };
    const d=$("#sc-end-done"); if(d) d.onclick=closeScans;
    const o=$("#sc-end-set"); if(o) o.onclick=async()=>{ await closeScans(); openSettings("scans") };
    drawLog();
    const f=$("#sc-end-done")||$("#sc-done"); if(f) f.focus();
    return;
  }
  const cards=SC.deck.slice(SC.at,SC.at+3).map((r,i)=>scanCard(r,i)).reverse().join("");
  body.innerHTML='<div class="sc-main"><div class="sc-stack" id="sc-stack">'+cards+'</div>'+
    '<div class="sc-acts">'+scanBtn("no",t("Not interested"),"ArrowLeft","←")+
      scanBtn("undo",t("Undo"),"Z","Z")+scanBtn("later",t("Later"),"ArrowDown","↓")+
      scanBtn("yes",t("Add to applications"),"ArrowRight","→")+'</div>'+
    '<p class="sc-keys">'+esc(t("Drag the card, or use the arrow keys: ← not interested, → add, ↓ later, Z undo."))+'</p></div>'+
    '<aside class="sc-log" id="sc-log"></aside>';
  const btn=k=>body.querySelector('[data-sc="'+k+'"]');
  btn("no").onclick=()=>decide("no");
  btn("yes").onclick=()=>decide("yes");
  btn("later").onclick=()=>decide("later");
  const u=btn("undo"); u.onclick=undoLast; u.disabled=!SC.log.length;
  const card=$("#sc-stack .sc-card.top");
  if(back) card.classList.add(SC_BACK[back]);
  else if(SC.log.length) card.classList.add("rise");
  scanDrag(card);
  drawLog();
  card.focus({preventScroll:true});
}
function scanEnd(){
  if(!SC.deck.length) return '<div class="sc-end"><span class="sc-end-ic" aria-hidden="true">'+SC_ICON.deck+'</span>'+
    '<h2>'+esc(t("Nothing new from your scans"))+'</h2>'+
    '<p>'+esc(t("When a search leaves jobs in the scans folder, they wait here for a yes or a no."))+'</p>'+
    '<div class="sc-end-acts"><button type="button" class="sbtn" id="sc-end-set">'+esc(t("Job scans settings"))+'</button>'+
    '<button type="button" class="sbtn primary" id="sc-end-done">'+esc(t("Done"))+'</button></div></div>';
  const yes=SC.log.filter(e=>e.dir==="yes"&&e.state!=="err").length, no=SC.log.filter(e=>e.dir==="no"&&e.state!=="err").length;
  return '<div class="sc-end"><span class="sc-end-ic done" aria-hidden="true">'+SC_ICON.yes+'</span>'+
    '<h2>'+esc(t("That is everything for now"))+'</h2>'+
    '<p>'+esc(t("{a} added to your applications, {b} passed on.",{a:yes,b:no}))+'</p>'+
    '<div class="sc-end-acts">'+(SC.log.length?'<button type="button" class="sbtn" id="sc-end-undo">'+esc(t("Undo the last one"))+'</button>':'')+
    (yes?'<button type="button" class="sbtn" id="sc-end-see">'+esc(t("See them in Applications"))+'</button>':'')+
    '<button type="button" class="sbtn primary" id="sc-end-done">'+esc(t("Done"))+'</button></div></div>';
}
function scanEventWords(e){
  if(e.state==="err") return tx(e.err||"")||t("Could not save that.");
  if(e.dir==="no") return e.state==="busy"?t("Passing…"):t("Not interested");
  if(e.state==="busy") return t("Adding, reading the posting…");
  if(e.already) return t("Already in your applications");
  if(e.posting==="unread") return t("Added. Its posting could not be read from the link: paste it on the application.");
  return t("Added to your applications");
}
function drawLog(){
  const el=$("#sc-log"); if(!el) return;
  const items=SC.log.map((e,i)=>({e,i})).filter(x=>x.e.dir!=="later").slice(-6).reverse();
  if(!items.length){ el.innerHTML='<p class="sc-hint">'+esc(t("What you decide shows here, and can be taken back."))+'</p>'; return }
  el.innerHTML='<h4>'+esc(t("Just now"))+'</h4>'+items.map(({e,i})=>'<div class="sc-ev '+e.dir+' '+e.state+'">'+
    '<span class="sc-ev-ic" aria-hidden="true">'+(e.dir==="yes"?SC_ICON.yes:SC_ICON.no)+'</span>'+
    '<span class="sc-ev-tx"><b data-noi18n>'+esc(e.row.company||"")+'</b><small data-noi18n>'+esc(e.row.role||"")+'</small>'+
      '<em>'+esc(scanEventWords(e))+'</em></span>'+
    (e.dir==="yes"&&e.state==="ok"&&e.job?'<button type="button" class="obtn" data-ev-open="'+esc(e.job.id)+'">'+esc(t("Open"))+'</button>':'')+
    (e.state==="err"?'<button type="button" class="obtn" data-ev-retry="'+i+'">'+esc(t("Try again"))+'</button>':'')+
    '</div>').join("");
  el.querySelectorAll("[data-ev-open]").forEach(b=>b.onclick=async()=>{
    const id=b.dataset.evOpen; await closeScans(); setView("jobs"); selectJob(id) });
  el.querySelectorAll("[data-ev-retry]").forEach(b=>b.onclick=()=>runDecision(SC.log[+b.dataset.evRetry]));
}
function runDecision(e){
  e.state="busy"; e.err=null; drawLog();
  e.wait=(async()=>{
    try{
      if(e.dir==="yes"){
        const r=await post("/api/scans/add",{key:e.row.key});
        e.job=r.job; e.posting=r.posting; e.already=!!r.already;
      }else await post("/api/scans/decide",{keys:[e.row.key],status:"Skipped"});
      e.state="ok";
    }catch(err){ e.state="err"; e.err=err.message }
    if(SC.open) drawLog();
    if(e.dir==="yes"&&e.state==="ok") loadJobs(true);
  })();
  return e.wait;
}
function flyOff(dir){
  const card=$("#sc-stack .sc-card.top");
  if(!card) return Promise.resolve();
  SC.anim=true;
  card.style.transform=""; card.classList.add(SC_FLY[dir]);
  const ms=matchMedia("(prefers-reduced-motion: reduce)").matches?90:240;
  return new Promise(r=>setTimeout(()=>{ SC.anim=false; r() },ms));
}
async function decide(dir){
  const row=SC.deck[SC.at];
  if(!row||SC.anim) return;
  await flyOff(dir);
  if(dir==="later"){
    SC.deck.push(SC.deck.splice(SC.at,1)[0]);
    SC.log.push({row,dir});
    drawDeck();
    scanAnnounce(t("Later: {co}",{co:row.company||row.role||""}));
    return;
  }
  const e={row,dir,state:"busy"};
  SC.at++; SC.log.push(e);
  drawDeck();
  scanAnnounce(dir==="yes"?t("Adding {co}",{co:row.company||""}):t("Passed on {co}",{co:row.company||""}));
  runDecision(e);
}
async function undoLast(){
  const e=SC.log.pop();
  if(!e||SC.anim) { if(e) SC.log.push(e); return }
  if(e.dir==="later"){
    SC.deck.splice(SC.deck.lastIndexOf(e.row),1); SC.deck.splice(SC.at,0,e.row);
    drawDeck("later"); return;
  }
  SC.at--;
  drawDeck(e.dir);
  scanAnnounce(t("Back: {co}",{co:e.row.company||""}));
  if(e.wait) await e.wait;
  if(e.state!=="ok") return;
  try{ await post("/api/scans/undo",{key:e.row.key,job_id:e.dir==="yes"&&!e.already&&e.job?e.job.id:null}) }
  catch(err){ toast(err.message,true) }
  if(e.dir==="yes") loadJobs(true);
}
function scanDrag(card){
  const reset=()=>{ card.style.transform=""; card.style.setProperty("--yes",0); card.style.setProperty("--no",0) };
  card.addEventListener("pointerdown",e=>{
    if(e.button!==0||SC.anim||e.target.closest("a,button")) return;
    SC.drag={x:e.clientX,y:e.clientY,t:performance.now(),id:e.pointerId,dx:0,dy:0};
    card.setPointerCapture(e.pointerId); card.classList.add("dragging");
  });
  card.addEventListener("pointermove",e=>{
    const d=SC.drag; if(!d||e.pointerId!==d.id) return;
    d.dx=e.clientX-d.x; d.dy=e.clientY-d.y;
    card.style.transform="translate("+d.dx+"px,"+(d.dy*0.2)+"px) rotate("+(d.dx/20)+"deg)";
    const k=Math.min(1,Math.abs(d.dx)/110);
    card.style.setProperty("--yes",d.dx>0?k:0); card.style.setProperty("--no",d.dx<0?k:0);
  });
  const end=e=>{
    const d=SC.drag; if(!d||e.pointerId!==d.id) return;
    SC.drag=null; card.classList.remove("dragging");
    const fast=Math.abs(d.dx)/Math.max(1,performance.now()-d.t)>0.5;
    if(Math.abs(d.dx)>120||(Math.abs(d.dx)>40&&fast)) decide(d.dx>0?"yes":"no");
    else reset();
  };
  card.addEventListener("pointerup",end);
  card.addEventListener("pointercancel",end);
}
function deckKeys(e){
  if(!SC.open||e.altKey||document.querySelector(".dlg")) return;
  const k=e.key, el=e.target;
  if(k==="Escape"){ e.preventDefault(); e.stopPropagation(); closeScans(); return }
  if(el&&el.closest&&el.closest("input,textarea,select")) return;
  if((e.ctrlKey||e.metaKey)&&k.toLowerCase()!=="z") return;
  const dir={ArrowRight:"yes",ArrowLeft:"no",ArrowDown:"later"}[k];
  if(dir){ if(SC.at<SC.deck.length){ e.preventDefault(); e.stopPropagation(); decide(dir) } return }
  if(k==="z"||k==="Z"||(k==="Backspace"&&!(el&&el.closest&&el.closest("button")))){
    e.preventDefault(); e.stopPropagation(); undoLast(); return }
  if(k==="Enter"&&el&&el.classList&&el.classList.contains("sc-card")){
    const a=el.querySelector(".sc-link"); if(a){ e.preventDefault(); a.click() } }
}

/* ---- telling you, and the settings ---- */
async function notifyScans(){
  if(!notifyOn()||prefs().notify_scans===false||SC.open) return;
  const list=scanNew(), seen=new Set(prefs().scans_seen||[]);
  const fresh=list.filter(r=>!seen.has(r.key));
  if(!fresh.length) return;
  const names=[...new Set(fresh.map(r=>r.company).filter(Boolean))];
  const ok=await notifySend(null,fresh.length===1?t("1 new job from your scans"):t("{n} new jobs from your scans",{n:fresh.length}),
    names.length>4?names.slice(0,4).join(", ")+" +"+(names.length-4):names.join(", "),
    ()=>{ closeOverlays(); setView("jobs"); openScans() });
  if(ok) setPref("scans_seen",list.map(r=>r.key).slice(0,500));
}
const SCAN_TASK=`Every morning, find new job postings that fit me and hand them to CV Studio.

What I am looking for: [the roles, the places, remote or not, and anything to leave out].

1. Read my base CV with CV Studio's tools (workspace_info names it, read_cv reads it), so you know what I have done.
2. Search job boards and companies' own career pages for postings from the last few days that fit what I am looking for and that CV. Keep only postings with a permanent link to the posting itself: not a search page, not a tracking link.
3. Call CV Studio's record_job_scan with every job you kept: company, role, link, location, the day it was posted, one line on why it fits me, true to my CV, and its match against my base CV (the five ratings score_job describes). Name this search in "scan".
4. Do not add them as applications: I go through them in CV Studio and choose. [Or, to have the best ones added: add the ones scoring 8 or more with decide_job_scan, and leave the rest for me.]

End with one line saying how many jobs you handed over.`;
function fillScans(){
  const st=SC.st;
  if(!st) return;      /* loadScans fills this pane when it has an answer */
  const sel=$("#s-scan-folder"), cur=st.setting||st.default;
  const opts=[...new Set([st.default,...(st.choices||[]),cur].filter(Boolean))];
  sel.innerHTML=opts.map(o=>'<option value="'+esc(o)+'"'+(o===cur?' selected':'')+'>'+esc(o)+'</option>').join("");
  sel.onchange=async()=>{
    const v=sel.value===st.default?null:sel.value;
    prefs().scan_folder=v;
    try{ await post("/api/prefs",{set:{scan_folder:v}}) }catch(e){ toast(e.message,true) }
    loadScans();
  };
  const c=st.counts||{new:0,added:0,skipped:0};
  const bits=[];
  if(!st.exists) bits.push(t("The folder is made with the spreadsheet."));
  else if(!st.file_exists) bits.push(t("No spreadsheet yet: the first search makes one, or create it now."));
  else bits.push(t("{file}, {n} jobs in all.",{file:st.file,n:st.rows}));
  if((st.files||[]).length>1) bits.push(t("Read from {n} files.",{n:st.files.length}));
  if(st.pending) bits.push(t("Some changes wait to be written: the spreadsheet is open in another program."));
  (st.problems||[]).forEach(p=>bits.push(tx(p)));
  $("#s-scan-say").textContent=bits.join(" ");
  const cr=$("#s-scan-create");
  cr.disabled=!!st.file_exists;
  cr.onclick=async()=>{ cr.disabled=true;
    try{ await post("/api/scans/create",{}); toast(t("Spreadsheet created")); loadScans() }
    catch(e){ toast(e.message,true); cr.disabled=false } };
  $("#s-scan-open").onclick=async()=>{ try{ await post("/api/reveal",{scans:true}) }catch(e){ toast(e.message,true) } };
  $("#s-scan-new").textContent=t("{n} waiting, {a} added to your applications, {s} passed on.",{n:c.new,a:c.added,s:c.skipped});
  const rv=$("#s-scan-review");
  rv.disabled=!c.new;
  rv.onclick=()=>{ closeOverlays(); setView("jobs"); openScans() };
  $("#s-scan-task").onclick=async()=>{
    try{ await navigator.clipboard.writeText(SCAN_TASK); toast(t("Copied. Paste it into a new scheduled task in the Claude desktop app.")) }
    catch(e){ toast(t("Could not copy."),true) } };
}

/* Backups: when the last one was, one now, and putting one back. */
async function fillBackups(){
  const say=$("#s-bk-say"), now=$("#s-bk-now"), list=$("#s-bk-list");
  let r; try{ r=await api("/api/backups") }catch(e){ return }
  const last=r.backups[0];
  if(r.sample){ say.textContent=t("Sample data is not backed up; it is made fresh each time."); now.disabled=list.disabled=true; return }
  now.disabled=list.disabled=false;
  const relT=at=>{ const sec=(at*1000-Date.now())/1000, f=new Intl.RelativeTimeFormat(uiLocale(),{numeric:"auto"});
    const a=Math.abs(sec); return a<3600?f.format(Math.round(sec/60),"minute"):a<86400?f.format(Math.round(sec/3600),"hour")
      :f.format(Math.round(sec/86400),"day") };
  say.textContent=last?t("Last one {when}. {n} kept, beside the app.",{when:relT(last.at),n:r.backups.length})
    :t("A copy of this workspace every day, beside the app, the last fourteen kept.");
  list.disabled=!r.backups.length;
  now.onclick=async()=>{ now.disabled=true;
    try{ await post("/api/backups/new",{}); toast(t("Backed up")); fillBackups() }
    catch(e){ toast(e.message,true); now.disabled=false } };
  list.onclick=()=>{
    const kb=n=>n>1048576?(n/1048576).toFixed(1)+" MB":Math.max(1,Math.round(n/1024))+" KB";
    const when=a=>DTF(uiLocale(),{weekday:"short",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit",hourCycle:clock12()?"h12":"h23"}).format(new Date(a*1000));
    openSheet('<div><h3>'+t("Restore a backup")+'</h3><p>'+t("Its files are put back into the workspace. What is there now is backed up first, so this can be undone the same way.")+'</p></div>'+
      '<div class="bk-list">'+r.backups.map(b=>'<div class="bk-row"><span><b>'+esc(when(b.at))+'</b>'+
        '<small>'+esc(kb(b.size))+(/before-restore/.test(b.name)?' · '+t("before a restore"):/manual/.test(b.name)?' · '+t("made by hand"):'')+'</small></span>'+
        '<button class="sbtn" data-bk="'+esc(b.name)+'">'+t("Restore")+'</button></div>').join("")+'</div>'+
      '<p class="note muted mono" style="font-size:11.5px">'+esc(r.folder)+'</p>'+
      '<div class="foot"><div class="grow"></div><button class="sbtn" data-cancel>'+t("Close")+'</button></div>');
    $("#sheet [data-cancel]").onclick=closeSheet;
    $$("#sheet [data-bk]").forEach(b=>b.onclick=async()=>{
      if(!b.dataset.sure){ b.dataset.sure="1"; b.textContent=t("Restore it"); b.classList.add("danger"); return }
      try{ await post("/api/backups/restore",{name:b.dataset.bk}); closeSheet();
        toast(t("Restored. Reloading…")); setTimeout(()=>location.reload(),900) }
      catch(e){ toast(e.message,true) }
    });
  };
}
