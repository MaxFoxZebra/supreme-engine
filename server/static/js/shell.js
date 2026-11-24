/* View switching, hash navigation history, and status bar. */
/* ---- view switching ---------------------------------------------------- */
/* The app has two kinds of screen -- a list you navigate, and one document you
   work on -- so the chrome has two shapes rather than six elements hidden
   independently of each other. The gap between them is what pins the cluster
   to the right edge, and that has to hold in both or the gear moves when you
   switch. */
/* ---- back and forward ----------------------------------------------------
   Every place you can be has an address in the hash -- #/jobs, #/jobs/<id>,
   #/doc/<path>, #/letter/<path>, #/docs, #/funnel, #/cal -- so the window's
   back and forward, the mouse's side buttons and Alt+arrows walk where you
   have been. Navigation code does not know about any of this: once it has
   settled, where the app now is gets written down, unless it got there by
   going back or forward, which only reads. */
const NAV={busy:false,t:null};
function navHere(){
  const v=S.view;
  if(v==="letter"&&LT.path) return "#/letter/"+encodeURIComponent(LT.path);
  if(v==="cvs"&&S.path) return "#/doc/"+encodeURIComponent(S.path);
  if(v==="jobs") return S.jsel?"#/jobs/"+encodeURIComponent(S.jsel):"#/jobs";
  return "#/"+(v||"jobs");
}
function navNote(){
  if(NAV.busy) return;
  clearTimeout(NAV.t);
  NAV.t=setTimeout(()=>{
    if(NAV.busy) return;
    const h=navHere();
    if(h!==location.hash) history.pushState(null,"",location.pathname+location.search+h);
  },0);
}
async function navGo(hash){
  const m=/^#\/(\w+)(?:\/(.+))?$/.exec(hash||""); if(!m) return;
  const [,where,arg]=m, id=arg?decodeURIComponent(arg):null;
  if(S.view==="cvs"&&where!=="doc"||where==="doc"&&id!==S.path){
    if(!await leaveEdits()){ history.pushState(null,"",location.pathname+location.search+navHere()); return }
  }
  NAV.busy=true;
  try{
    closeSheet(); closeOverlays();
    if(where==="doc"&&id){ if(id!==S.path||S.view!=="cvs") await openDoc(id) }
    else if(where==="letter"&&id){ if(id!==LT.path||S.view!=="letter") await openLetter(id) }
    else if(where==="jobs"){
      if(S.view!=="jobs") setView("jobs");
      if(!S.jready) await loadJobs(true);
      if(id&&S.jobs.some(j=>j.id===id)){ if(S.jsel!==id) selectJob(id) }
      else if(S.jsel) closePeek();
    }
    else if(["docs","funnel","cal"].includes(where)) setView(where);
  }finally{ setTimeout(()=>{ NAV.busy=false },0) }
}
window.addEventListener("popstate",()=>navGo(location.hash));
/* The side buttons on a mouse, where the window does not map them itself. */
window.addEventListener("mouseup",e=>{
  if(e.button===3){ e.preventDefault(); history.back() }
  else if(e.button===4){ e.preventDefault(); history.forward() }
});
document.addEventListener("keydown",e=>{
  if(!e.altKey||e.ctrlKey||e.metaKey||e.shiftKey) return;
  const el=document.activeElement;
  if(el&&(/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)||el.isContentEditable)) return;
  if(e.key==="ArrowLeft"){ e.preventDefault(); history.back() }
  else if(e.key==="ArrowRight"){ e.preventDefault(); history.forward() }
  else if(e.key==="1"){ e.preventDefault(); closeOverlays(); setView("jobs") }
  else if(e.key==="2"){ e.preventDefault(); closeOverlays(); setView("docs") }
  else if(e.key==="3"){ e.preventDefault(); closeOverlays(); setView("funnel") }
  else if(e.key==="4"){ e.preventDefault(); closeOverlays(); setView("cal") }
  else if(e.key==="5"&&S.path){ e.preventDefault(); closeOverlays(); setView("cvs") }
});

function setView(v){
  if(S.view==="letter"&&v!=="letter"&&LT.dirty) ltSave();
  S.view=v;
  const doc=v==="cvs";
  ["cvs","jobs","docs","funnel","cal","letter"].forEach(k=>{ $("#v-"+k).hidden = k!==v });
  if(v!=="cal") clearInterval(S.calTick);
  if(doc) paintBackLabel();
  else if(v==="letter") ltBackLabel();
  else $$("#nav button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.view===v)));
  if(v==="jobs"){ loadJobs(); loadAlerts() }
  /* Which application a document was written for is a fact about the jobs, so
     this screen needs them too -- and you can land on it without ever having
     opened the list. Draw what is known now, fill in the rest when it lands. */
  if(v==="docs"){ S.driftBy=null; drawDocuments(); if(!S.jready) loadJobs(true) }
  if(v==="funnel") loadFunnel();
  if(v==="cal") openCalendar();
  paintStatus();
  navNote();
}
/* Out of the editor, to the application the open document was written for.
   Derived from the link rather than remembered as history: a stack can go
   stale and this cannot, and "this document belongs to Acme" is the relation
   that is actually true. With nothing linked it is the list you left, which
   A document written for no application lives in Documents, however it was
   opened. */
function goBack(){
  const j=linkedJob();
  if(j){ setView("jobs"); selectJob(j.id); return }
  setView("docs");
}
/* The crumb says where it goes -- the application the document was written
   for, or Documents -- and that tab stays lit while you are in the editor. */
function paintBackLabel(){
  const to=linkedJob()?"jobs":"docs";
  $("#back").textContent = to==="docs" ? "Documents" : "Applications";
  $$("#nav button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.view===to)));
}
$("#back").onclick=goBack;
$$("#nav button").forEach(b=>b.onclick=()=>{ closeOverlays(); setView(b.dataset.view) });

/* ---- status bar --------------------------------------------------------- */
function paintStatus(){
  const L=$("#st-left"), R=$("#st-right");
  const last=S.pulse&&S.pulse.mcp&&S.pulse.mcp.last;
  const mcpLine=last&&(Date.now()/1000-last.at)<900
    ? clientName(last)+" · "+(last.ok===false?"refused ":"")+last.tool+(last.path?" · "+last.path:"")+
      " · "+ago(last.at*1000)+" ago"
    : null;
  R.classList.remove("said");
  R.classList.toggle("blocked",!!(mcpLine&&last.ok===false));
  if(S.view==="cvs"){
    const bits=[];
    if(S.live==="working") bits.push(t("rendering…"));
    else if(S.live==="bad") bits.push(S.liveMsg?tx(S.liveMsg):t("not valid yet"));
    if(S.dirty) bits.push(t("unsaved changes"));
    else if(S.savedAt) bits.push(t("saved {t} ago",{t:ago(S.savedAt)}));
    L.className=S.live==="bad"||S.dirty?"warn":"";
    L.textContent=bits.join(" · ")||t("all saved");
    /* Save is the primary action only while there is something to save. */
    $("#btn-render").classList.toggle("idle",!S.dirty);
    L.classList.add("mono");
    /* The right of the status bar is where the workspace lives, and what Claude
       last did in it belongs in the same place -- it is the other thing acting
       on these files. It gives way to the path once it goes stale. */
    /* A resolved conflict outranks the raw activity line: after you choose,
       the footer has to report your decision, not keep reporting their edit. */
    const done=S.resolved&&(Date.now()-S.resolved.at)<900000?S.resolved:null;
    if(done){
      R.classList.add("said");
      R.classList.remove("blocked");
      R.textContent=done.kept==="mine"
        ? "You kept your version over "+done.who+"'s change"
        : "You took "+done.who+"'s version";
      R.title="";
      return;
    }
    R.textContent=mcpLine||shortPath((S.state&&S.state.workspace)||"");
    R.title=(S.state&&S.state.workspace)||"";
  }else if(S.view==="jobs"){
    L.className="mono";
    L.textContent=S.jobs.length+" application"+(S.jobs.length===1?"":"s")+
      (S.jsel?" · 1 selected":"");
    R.textContent=mcpLine||""; R.title="";
  }else if(S.view==="docs"){
    /* Documents are files in the workspace folder: its short path says where. */
    const n=((S.state&&S.state.documents)||[]).length;
    L.className="mono";
    L.textContent=n+" document"+(n===1?"":"s");
    R.textContent=mcpLine||shortPath((S.state&&S.state.workspace)||"");
    R.title=(S.state&&S.state.workspace)||"";
  }else if(S.view==="letter"){
    L.className="mono";
    L.textContent=LT.saving||LT.dirty?t("saving…"):LT.savedAt?t("saved {t} ago",{t:ago(LT.savedAt)}):t("all saved");
    R.textContent=mcpLine||shortPath((S.state&&S.state.workspace)||"");
    R.title=(S.state&&S.state.workspace)||"";
  }else{
    /* The funnel and the calendar are drawn from the applications: the
       footer says how many, and how many are still open. */
    const js=S.jobs||[], open=js.filter(j=>LIVE_ST.has(j.status)).length;
    L.className="mono";
    L.textContent=js.length?t("{n} applications · {m} open",{n:js.length,m:open}):"";
    R.textContent=mcpLine||""; R.title="";
  }
}
function ago(t){
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<60) return s+"s";
  if(s<3600) return Math.round(s/60)+"m";
  return Math.round(s/3600)+"h";
}
setInterval(()=>{ if(S.view==="cvs"&&!S.dirty&&S.savedAt) paintStatus() },10000);

