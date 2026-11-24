/* Version history timeline/diff, base CV management, and languages. */
/* ---- History: every version of a document or an application ---------------
   What each was, when, and who made it: you, an AI client (and which tool),
   or something outside CV Studio. A version shows what it changed against the
   one before it, or against the file now; restoring one is a version of its
   own, so nothing in between is lost. */
const HV_WHO={you:"You",start:"When history began",elsewhere:"Outside CV Studio"};
const HV_TOOL={write_cv:"rewrote the whole file",edit_cv_fields:"changed fields",write_letter:"rewrote the letter",
  edit_on_page:"edited on the page",review_change:"kept or undid a change",restore:"restored a version",
  restore_version:"restored a version",add_language:"added a translation",update_job_tracking:"updated it",
  set_job_status:"changed the status",add_job:"added it",save_person:"saved a contact",
  save_interview_prep:"saved interview prep",save:"saved","jobs/update":"saved","jobs/prep":"saved interview prep",
  "backups/restore":"restored a backup","doc/rename":"renamed it",import:"imported it"};
function hvWho(v){ return HV_WHO[v.who]?t(HV_WHO[v.who]):(v.agent||PROV_LABEL[v.who]||t("An AI client")) }
function hvWhen(at){ try{ return DTF(uiLocale(),{weekday:"short",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit",
  hourCycle:clock12()?"h12":"h23"}).format(new Date(at)) }catch(e){ return String(at||"").slice(0,16).replace("T"," ") } }
function hvWhat(v){
  if(v.deleted) return t("Deleted");
  if(v.who==="start") return t("The file as it was");
  const parts=[];
  if(v.tool&&HV_TOOL[v.tool]) parts.push(t(HV_TOOL[v.tool])); else if(v.note) parts.push(v.note);
  if(v.lines) parts.push("+"+v.lines.added+" −"+v.lines.removed+" "+t("lines"));
  return parts.join(" · ");
}
function hvLines(lines){
  if(!lines.length) return '<p class="muted">'+esc(t("No difference."))+'</p>';
  const cls={add:"hv-add",del:"hv-del",skip:"hv-skip",same:"hv-same"};
  return '<pre class="hv-diff mono">'+lines.map(l=>'<span class="'+(cls[l.t]||"hv-same")+'">'+
    esc((l.t==="add"?"+ ":l.t==="del"?"− ":l.t==="skip"?"⋯ ":"  ")+l.text)+'</span>').join("")+'</pre>';
}
/* q is {path} or {job}. */
async function historySheet(q,title){
  let d; try{ d=await api("/api/history?"+new URLSearchParams(q)) }catch(e){ return toast(e.message,true) }
  const vs=d.versions||[];
  let sel=vs.length>1?vs[0].id:(vs[0]&&vs[0].id), against="before";
  const head='<div><h3>'+esc(t("History"))+(title?' · '+esc(title):'')+'</h3><p>'+
    esc(vs.length?t("Every version CV Studio kept of this file, newest first, and who made it. Restoring one is a version of its own: nothing in between is lost."):
      t("No versions yet. Every change from now on is kept here."))+'</p></div>';
  const draw=async()=>{
    const list='<div class="bk-list hv-list" role="listbox" aria-label="'+esc(t("Versions"))+'">'+vs.map((v,i)=>
      '<button class="bk-row hv-row'+(v.id===sel?' sel':'')+'" role="option" aria-selected="'+(v.id===sel)+'" data-hv="'+esc(v.id)+'">'+
        '<span><b>'+esc(hvWhen(v.since||v.at))+(i===0&&d.exists?' · '+esc(t("now")):'')+'</b>'+
        '<small>'+esc(hvWho(v))+(hvWhat(v)?' · '+esc(hvWhat(v)):'')+'</small></span></button>').join("")+'</div>';
    const v=vs.find(x=>x.id===sel);
    openSheet(head+(vs.length?list+'<div class="hv-pane" id="hv-pane"><p class="muted">'+esc(t("Loading…"))+'</p></div>':'')+
      '<div class="foot">'+(v&&!v.deleted?'<div class="seg light hv-seg" role="group" aria-label="'+esc(t("Compare"))+'">'+
        '<button data-ag="before" aria-pressed="'+(against==="before")+'" aria-selected="'+(against==="before")+'">'+esc(t("What it changed"))+'</button>'+
        '<button data-ag="now" aria-pressed="'+(against==="now")+'" aria-selected="'+(against==="now")+'">'+esc(t("Compared with now"))+'</button></div>':'')+
        '<div class="grow"></div><button class="sbtn" data-cancel>'+esc(t("Close"))+'</button>'+
        (v&&!v.deleted&&!(vs[0]&&v.id===vs[0].id&&d.exists)?'<button class="sbtn primary" id="hv-restore">'+esc(t("Restore this version"))+'</button>':'')+
      '</div>');
    $("#sheet [data-cancel]").onclick=closeSheet;
    $$("#sheet [data-hv]").forEach(b=>b.onclick=()=>{ sel=b.dataset.hv; draw() });
    $$("#sheet [data-ag]").forEach(b=>b.onclick=()=>{ against=b.dataset.ag; draw() });
    const rs=$("#hv-restore"); if(rs) rs.onclick=()=>hvRestore(d.path,v);
    if(!v) return;
    const pane=$("#hv-pane");
    if(v.deleted){ pane.innerHTML='<p class="muted">'+esc(t("The file was deleted here. Restore the version before it to bring it back."))+'</p>'; return }
    try{
      const r=await api("/api/history/diff?"+new URLSearchParams({path:d.path,id:v.id,against}));
      if(pane.isConnected) pane.innerHTML=hvLines(r.lines||[]);
    }catch(e){ if(pane.isConnected) pane.innerHTML='<p class="muted">'+esc(e.message)+'</p>' }
  };
  draw();
}
async function hvRestore(path,v){
  const open=path===S.path&&S.view==="cvs", openLt=path===LT.path&&S.view==="letter";
  if(open&&!(await leaveEdits())) return;
  if(openLt&&LT.dirty){ try{ await ltSave() }catch(e){} }
  if(!(await askConfirm({title:t("Restore this version?"),
      body:t("The file goes back to how it was on {d}. The version it replaces stays in the history.",{d:hvWhen(v.since||v.at)}),
      ok:t("Restore")}))) return;
  try{ await post("/api/history/restore",{path,id:v.id}) }catch(e){ return toast(e.message,true) }
  closeSheet(); toast(t("Restored the version of {d}",{d:hvWhen(v.since||v.at)}));
  if(open) openDoc(path); else if(openLt){ LT.path=null; openLetter(path) }
  if(/^tracker\//.test(path)) loadJobs(true).then(()=>{ if(S.view==="jobs") drawJobs() });
}
/* The latest changes across the workspace; each opens its file's history. */
async function recentChangesSheet(){
  let r; try{ r=await api("/api/history/recent") }catch(e){ return toast(e.message,true) }
  const ch=r.changes||[];
  const docs=(S.state&&S.state.documents)||[];
  const named=Object.fromEntries(ch.filter(c=>c.label).map(c=>[c.path,c.label]));
  const label=p=>{ const d=docs.find(x=>x.path===p); return d?docTitle(d):named[p]||p };
  openSheet('<div><h3>'+esc(t("Recent changes"))+'</h3><p>'+esc(t("Every change to a CV, letter or application, newest first, and who made it. Open one to see what it changed or to restore an earlier version."))+'</p></div>'+
    (ch.length?'<div class="bk-list hv-list">'+ch.map(c=>'<button class="bk-row hv-row" data-hp="'+esc(c.path)+'">'+
      '<span><b>'+esc(label(c.path))+'</b><small>'+esc(hvWhen(c.since||c.at))+' · '+esc(hvWho(c))+(hvWhat(c)?' · '+esc(hvWhat(c)):'')+'</small></span></button>').join("")+'</div>'
      :'<p class="muted">'+esc(t("No changes yet. Every change from now on is kept here."))+'</p>')+
    '<div class="foot"><div class="grow"></div><button class="sbtn" data-cancel>'+esc(t("Close"))+'</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $$("#sheet [data-hp]").forEach(b=>b.onclick=()=>historySheet({path:b.dataset.hp},label(b.dataset.hp)));
}
function fillNotify(){
  const on=$("#s-notify"), iv=$("#s-notify-iv"), fu=$("#s-notify-fu"), st=$("#s-notify-state"), A=notifyApi();
  const pr=prefs();
  on.checked=notifyOn(); iv.value=pr.notify_iv??"60,10"; fu.value=String(pr.notify_fu??"9");
  /* Off means off for the keyboard too, not only for the mouse. */
  $$("#sp-notify .nf-sub").forEach(r=>{ r.classList.toggle("off",!on.checked);
    r.setAttribute("aria-disabled",String(!on.checked));
    r.querySelectorAll("select,input,button").forEach(c=>c.disabled=!on.checked) });
  const say=async()=>{
    if(!A){ st.textContent=t("This window cannot show notifications."); on.disabled=true; return }
    const g=await A.granted().catch(()=>false);
    st.textContent=!on.checked?t("Off. Turn them on to be reminded of interviews and follow-ups.")
      :g?t("On, while CV Studio is open."):t("Blocked by your system. Allow CV Studio in your notification settings.");
  };
  say();
  on.onchange=async()=>{
    if(on.checked&&A){
      let g=await A.granted().catch(()=>false);
      if(!g) g=await A.ask().catch(()=>false);
      if(!g){ on.checked=false; toast(t("Notifications are blocked for CV Studio in your system settings."),true) }
    }
    setPref("notify",on.checked);
    fillNotify(); notifyTick();
  };
  iv.onchange=()=>{ setPref("notify_iv",iv.value); notifyTick() };
  fu.onchange=()=>{ setPref("notify_fu",fu.value); notifyTick() };
  const ns=$("#s-notify-scans");
  ns.checked=pr.notify_scans!==false;
  ns.onchange=()=>{ setPref("notify_scans",ns.checked); notifyScans() };
  /* The desktop app only: the window can close to the tray, and the app can
     open at login. The shell reads keep_running from the same file when the
     window closes. */
  const T=window.__TAURI__, AS=T&&T.autostart;
  $$("#sp-notify [data-desk]").forEach(r=>r.hidden=!T);
  const keep=$("#s-keep"), auto=$("#s-autostart");
  keep.checked=pr.keep_running!==false;
  keep.onchange=()=>setPref("keep_running",keep.checked);
  if(AS){
    AS.isEnabled().then(v=>{ auto.checked=!!v }).catch(()=>{ auto.closest(".srow").hidden=true });
    auto.onchange=async()=>{
      try{ await (auto.checked?AS.enable():AS.disable()) }
      catch(e){ auto.checked=!auto.checked; toast(t("Could not change that: {e}",{e:String(e&&e.message||e)}),true) }
    };
  }else if(auto) auto.closest(".srow").hidden=true;
  $("#s-notify-test").onclick=async()=>{
    const ok=await notifySend(null,t("CV Studio"),t("This is how a reminder will look."));
    if(!ok) toast(t("Notifications are blocked for CV Studio in your system settings."),true);
  };
}

const MATCH_RAIL=[["top","Top match · 9+"],["strong","Strong match · 7+"],["good","Good match · 5+"],
  ["fair","Fair match · 3+"],["weak","Weak match"],["none","Not scored"]];
function drawRail(){
  const c=statusCounts(), f=S.jfilter;
  /* The .mark span is what .row.sel paints ochre. Without it this rail marked
     its selection with a background lift alone, at 1.28:1 -- the documents rail
     next door emits the span and gets the bar, so the two rails disagreed about
     what "selected" looks like. */
  const row=(label,count,kind,value,lead)=>{
    const on=f.kind===kind&&f.value===value;
    return '<button class="row'+(on?" sel":"")+'" aria-current="'+(on?"true":"false")+
      '" data-k="'+kind+'" data-v="'+esc(value)+'"><span class="mark"></span>'+(lead||"")+
      '<span class="lbl">'+esc(label)+'</span>'+
      (count==null?"":'<span class="ct mono">'+count+'</span>')+'</button>';
  };
  let h=row("All",S.jobs.length,"all","");
  S.statuses.forEach(s=>{ if(c[s]) h+=row(prettyStatus(s),c[s],"status",s) });
  /* The funnel hands over a node to filter by, and its label often matches a
     status already listed above -- "Draft" under "Draft", the second one with
     no count. Only add it when it is actually saying something new. */
  const shown=new Set(S.statuses.filter(x=>c[x]).map(prettyStatus));
  if(S.fnode&&S.labels[S.fnode]&&!shown.has(S.labels[S.fnode]))
    h+=row(S.labels[S.fnode],null,"node",S.fnode);
  $("#statuslist").innerHTML=h;

  /* Only buckets with something in them. An Attention list showing four zeroes
     is worse than no list: it trains you to stop looking at it. */
  const a=S.alerts;
  const live=a?ATTENTION.filter(([k])=>a.counts[k]>0):[];
  $("#attentionwrap").hidden=!live.length;
  $("#attentionlist").innerHTML=live.map(([k,label])=>
    row(label,a.counts[k],"alert",k)).join("");

  $("#savedlist").innerHTML=Object.keys(SAVED).map(k=>
    row(k,S.jobs.filter(SAVED[k]).length,"saved",k)).join("");

  /* By match: the bands with something in them, then what is not scored,
     with the words that get it scored. */
  const mc={};
  S.jobs.forEach(j=>{ const k=matchKey(j); mc[k]=(mc[k]||0)+1 });
  $("#matchwrap").hidden=!S.jobs.length;
  $("#matchlist").innerHTML=MATCH_RAIL.filter(([k])=>mc[k]).map(([k,label])=>
    row(label,mc[k],"match",k,'<i class="mdot '+bandCls(k)+'" aria-hidden="true"></i>')).join("")+
    (mc.none?'<button type="button" class="rail-ask" id="match-ask" title="'+esc(t("Copies the words to say to your AI client"))+'">'+
      esc(mc.none===S.jobs.length?t("Ask your AI client to score them"):t("Ask your AI client to score the rest"))+'</button>':'');
  const ma=$("#match-ask");
  if(ma) ma.onclick=()=>{ try{ navigator.clipboard.writeText(matchAsk()).then(()=>toast(t("Copied. Paste it into your AI client."))) }catch(e){} };
  $$("#statuslist [data-k],#attentionlist [data-k],#savedlist [data-k],#matchlist [data-k]").forEach(b=>b.onclick=()=>{
    S.jfilter={kind:b.dataset.k,value:b.dataset.v};
    if(b.dataset.k!=="node") S.fnode=null;
    drawJobs();
  });
}

function visibleJobs(){
  const f=S.jfilter, q=($("#jobq").value||"").trim().toLowerCase();
  let rows=S.jobs;
  if(f.kind==="status") rows=rows.filter(j=>j.status===f.value);
  else if(f.kind==="node"){
    const want=new Set(S.nodes[f.value]||[]);
    rows=rows.filter(j=>want.has(j.status));
  }else if(f.kind==="saved"&&SAVED[f.value]) rows=rows.filter(SAVED[f.value]);
  else if(f.kind==="match") rows=rows.filter(j=>matchKey(j)===f.value);
  else if(f.kind==="alert"){
    /* Filter against the server's answer rather than re-deriving the rule
       here, which is the whole point of computing it in one place. */
    const ids=new Set(((S.alerts&&S.alerts[f.value])||[]).map(x=>x.id));
    rows=rows.filter(j=>ids.has(j.id));
  }
  /* Word by word, accents aside, as the search palette matches. */
  if(q){ const w=palWords(q);
    rows=rows.filter(j=>palHas([j.company,j.title,j.location,j.notes,j.source].join(" "),w)) }
  return sortJobs(rows);
}

/* ---- the base CV --------------------------------------------------------
   One document every tailored CV is copied from. It is pinned above the
   applications rather than listed among the documents because it is what they
   are all made of: the question "which CV is this one a version of" now has
   one answer instead of one per file. */
/* Not baseName(): that one already means "the file this document was copied
   from" a few hundred lines up, and two answers to one name is how the two
   ideas get confused in the first place. */
function baseLabel(){ const b=S.state&&S.state.base;
  return b?humanSlug(b.path.split("/").pop().replace(/\.ya?ml$/,"")):null }
/* A document by what it is, not its file name: the one written for an
   application is "CV · Monzo" or "Letter · iFood"; any other reads as words
   ("my-cv" is "My CV"). The file name stays in the tooltip and the path. */
function humanSlug(s){
  return String(s||"").replace(/\.[a-z]{2}(-[a-z]{2})?$/i,"").replace(/[-_]+/g," ").trim()
    .replace(/\bcv\b/gi,"CV").replace(/^./,c=>c.toUpperCase());
}
/* bare: under a "CVs" or "Cover letters" heading the kind is already said. */
function docTitle(d,bare){
  if(!d) return "";
  const letter=d.letter||d.group==="Cover letters";
  const j=(S.jobs||[]).find(j=>(letter?j.letter_path:j.cv_path)===d.path);
  if(!j) return humanSlug(d.label);
  return bare?j.company:(letter?t("Letter"):t("CV"))+" · "+j.company;
}
/* The band above the applications and the card on the Documents screen are the
   same statement about the same document, so it is written once and mounted
   twice. The handlers hang off data attributes rather than ids: both elements
   are in the DOM whichever screen is showing, and one id in two places is one
   id too many. */
function baseHTML(b){
  if(!b)
    return '<span class="bl">Base CV</span>'+
      '<span class="bsub">Not chosen yet. Every tailored CV starts as a copy '+
      'of one.</span>'+
      '<button class="obtn" data-base-pick>Choose\u2026</button>'+
      '<div class="grow"></div>';
  if(b.missing)
    return '<span class="bl">Base CV</span>'+
      '<span class="bn">'+esc(baseLabel())+'</span>'+
      '<span class="bsub">is no longer in the workspace</span>'+
      '<button class="obtn" data-base-pick>Choose another\u2026</button>'+
      '<div class="grow"></div>';
  const pages=S.pages[b.path];
  const th=S.baseThumb&&S.baseThumb.path===b.path?S.baseThumb:null;
  const fam=baseFamily();
  const tailored=((S.state&&S.state.documents)||[]).filter(d=>d.base===b.path).length;
  const ico=d=>'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" '+
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+d+'</svg>';
  /* The page on its own little stage, the way Documents shows it, with what
     it is, the languages it comes in and how much has been made from it. */
  return '<div class="bstage">'+
      '<span class="btag">'+t("Base CV")+'</span>'+
      '<button class="bthumb'+(th&&th.png?"":" empty")+'" data-base-open aria-label="'+
        t("Open the base CV")+'">'+
      (th&&th.png?'<img alt="" src="'+esc(th.png+tok())+'">'
        :'<span>'+(th&&th.failed?"Doesn\u2019t render":"Rendering\u2026")+'</span>')+
      '</button></div>'+
    '<div class="bmeta"><span class="bn">'+esc(baseLabel())+'</span>'+
      (fam.length>1?'<span class="bflags">'+fam.map(m=>flag(m.lang)||lchip(m.lang)).join("")+'</span>':'')+
    '</div>'+
    '<span class="bsub">'+[pages?pages+" page"+(pages===1?"":"s"):"",
      tailored?tailored+" tailored from it":"Every tailored CV starts here"].filter(Boolean).join(" · ")+'</span>'+
    '<div class="bacts"><button class="obtn bopen" data-base-open>Open</button>'+
      '<button class="obtn bico" data-base-design title="Design" aria-label="Design">'+
        ico('<circle cx="13.5" cy="6.5" r="1"/><circle cx="17.5" cy="10.5" r="1"/><circle cx="8.5" cy="7.5" r="1"/>'+
          '<circle cx="6.5" cy="12.5" r="1"/><path d="M12 2a10 10 0 0 0 0 20c1.1 0 2-.9 2-2 0-.5-.2-1-.5-1.3-.3-.4-.5-.8-.5-1.3 0-1.1.9-2 2-2h2.4A5.6 5.6 0 0 0 22 9.8C22 5.5 17.5 2 12 2z"/>')+'</button>'+
      '<button class="obtn bico" data-base-pick title="Change base…" aria-label="Change base…">'+
        ico('<path d="M17 3l4 4-4 4"/><path d="M3 7h18"/><path d="M7 21l-4-4 4-4"/><path d="M21 17H3"/>')+'</button>'+
    '</div>';
}
/* The same base, on Documents, at the size of a page you can read. */
function baseHeroHTML(b){
  if(!b||b.missing) return '<div class="bmain"><div class="bbody"><span class="bl">Base CV</span>'+
    (b?'<h2 class="bn">'+esc(baseLabel())+'</h2><span class="bmeta">is no longer in the '+
      'workspace</span>':'<h2 class="bn">Not chosen yet</h2>')+
    '<p class="bwhy">Every CV you tailor for an application starts as a copy of the base.</p>'+
    '<div class="bacts"><button class="pbtn" data-base-pick>'+(b?"Choose another":"Choose")+
      '&#8230;</button></div></div></div>';
  const fam=multiLang()?baseFamily():baseFamily().slice(0,1);
  let m=fam.find(x=>x.lang===S.baseLang)||fam[0];
  const docs=(S.state&&S.state.documents)||[];
  const me=docs.find(d=>d.path===m.path);
  const pages=S.pages[m.path];
  const th=m.source?(S.baseThumb&&S.baseThumb.path===b.path?S.baseThumb:null):S.docThumbs[m.path];
  const kids=docs.filter(d=>d.base===m.path);
  const used=new Set((S.jobs||[]).map(j=>j.cv_path).filter(Boolean));
  const sent=kids.filter(d=>used.has(d.path)).length;
  const L=langOf(m.lang), src=langOf(fam[0].lang);
  const behind=p=>{ const d=S.driftBy&&S.driftBy[p]; return d&&d.changes&&d.changes.length };
  const tabs='<div class="btabs" role="tablist" aria-label="Base CV language">'+fam.map(x=>
      '<button role="tab" data-blang="'+esc(x.lang)+'" aria-selected="'+String(x===m)+'">'+
        lchip(x.lang,x===m)+esc(langOf(x.lang).native)+
        (x.source&&fam.length>1?'<span class="src">source</span>':'')+
        (behind(x.path)?'<span class="behind"><i></i>'+behind(x.path)+' behind</span>':'')+
      '</button>').join("")+
    '<button class="add" data-blang-add>+ Add a language</button></div>';
  const why=m.source
    ? (fam.length>1?'Every CV you tailor in '+esc(L.english)+' starts as a copy of this one. '+
        'Its translations say what they are missing when it changes.'
       :'Every CV you tailor for an application starts as a copy of this one, and shows '+
        'what it changed from it. Improving the base improves every CV you tailor from now on.')
    : 'The '+esc(L.english)+' translation of <b>'+esc(docLabel(fam[0].path))+'</b>. CVs for '+
      'postings in '+esc(L.english)+' start from it. Its design follows the '+esc(src.english)+' one.';
  return tabs+'<div class="bmain"><button class="bthumb'+(th&&th.png?"":" empty")+'" data-base-open'+
      ' aria-label="Open the base CV">'+
      (th&&th.png?'<img alt="Page one of the base CV" src="'+esc(th.png+tok())+'">'
        :'<span>'+(th&&th.failed?"Doesn’t render":"Rendering…")+'</span>')+
    '</button>'+
    '<div class="bbody"><span class="bl">Base CV'+(fam.length>1?' · '+esc(L.native):'')+'</span>'+
      '<h2 class="bn">'+esc(m.source?baseLabel():docLabel(m.path))+'</h2>'+
      '<span class="bmeta">'+[pages?pages+" page"+(pages===1?"":"s"):"",
        me&&me.mtime?"updated "+mtimeLabel(me.mtime):""].filter(Boolean).join(" · ")+
      '</span>'+
      '<p class="bwhy">'+why+'</p>'+
      '<div class="bstats"><div><b>'+kids.length+'</b><span>tailored from it</span></div>'+
        '<div><b>'+sent+'</b><span>attached to applications</span></div>'+
        (!m.source&&behind(m.path)?'<div><b>'+behind(m.path)+'</b><span>changes in '+
          esc(src.english)+' to carry over</span></div>':'')+'</div>'+
      '<div class="bacts"><button class="obtn" data-base-open>Open</button>'+
        (!m.source&&behind(m.path)?'<button class="obtn" data-base-drift>What changed</button>':'')+
        '<button class="obtn" data-base-design>Design</button>'+
        (m.source?'<button class="obtn" data-base-pick>Change base&#8230;</button>':'')+'</div>'+
    '</div></div>';
}

function mountBase(el,cls){
  if(!el) return;
  const b=S.state&&S.state.base;
  el.className=cls+(b&&b.missing?" gone":"")+(cls==="bhero"&&!b?" none":"");
  el.innerHTML=cls==="bhero"?baseHeroHTML(b):baseHTML(b);
  /* On Documents the card shows whichever language tab is picked. */
  const shown=cls==="bhero"&&b&&!b.missing
    ?((baseFamily().find(x=>x.lang===S.baseLang)||{}).path||b.path):b&&b.path;
  el.querySelectorAll("[data-base-open]").forEach(open=>open.onclick=async ()=>{
    if(!await leaveEdits()) return;
    openDoc(shown);
  });
  const design=el.querySelector("[data-base-design]");
  if(design) design.onclick=async()=>{
    if(!await leaveEdits()) return;
    await openDoc(b.path); openDesign();
  };
  const pick=el.querySelector("[data-base-pick]"); if(pick) pick.onclick=baseSheet;
  el.querySelectorAll("[data-blang]").forEach(t=>t.onclick=()=>{
    S.baseLang=t.dataset.blang; mountBase(el,cls);
  });
  const add=el.querySelector("[data-blang-add]");
  if(add) add.onclick=()=>addLanguageSheet(b.path);
  const dr=el.querySelector("[data-base-drift]");
  if(dr) dr.onclick=()=>driftSheet(shown,S.driftBy[shown]);
}
function paintBase(){
  applyMultiLang();
  mountBase($("#baserow"),"baserow");
  mountBase($("#docbase"),"bhero");
  paintBaseChip();
  baseThumb(false);
}

/* The base as it actually prints, not a sketch of its theme: the sketches in
   Design say what a theme looks like, and this card is about one document.
   Whatever is on disk is shown at once; a page older than the YAML is shown
   while a fresh one renders, then swapped, so the card never waits on Typst
   and never settles on a page that is out of date. */
let thumbBusy=null;
async function baseThumb(force){
  const b=S.state&&S.state.base;
  if(!b||b.missing) return;
  if(!force&&S.baseThumb&&S.baseThumb.path===b.path) return;
  if(thumbBusy===b.path) return;
  thumbBusy=b.path;
  const put=(png,failed)=>{
    S.baseThumb={path:b.path,png:png||(S.baseThumb&&S.baseThumb.path===b.path
      ?S.baseThumb.png:null),failed:!!failed};
    mountBase($("#baserow"),"baserow"); mountBase($("#docbase"),"bhero");
  };
  try{
    const t=await api("/api/thumb?path="+encodeURIComponent(b.path));
    put(t.png);
    if(!t.fresh){
      const r=await post("/api/render",{path:b.path});
      if(r.ok){ S.pages[b.path]=r.pages; put(r.pngs[0]) }
      else put(null,true);
    }
  }catch(e){ put(null,true) }
  finally{ thumbBusy=null }
}

/* A document's age. "720h" is what ago() would say about a CV last touched in
   the spring, which is true and useless, so anything older than yesterday gets
   the same date the applications table uses. */
function mtimeLabel(t){
  if(!t) return "";
  const d=new Date(t*1000), now=new Date();
  const same=(a,b)=>a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()
    &&a.getDate()===b.getDate();
  if(same(d,now)) return "today";
  const y=new Date(now); y.setDate(y.getDate()-1);
  return same(d,y)?"yesterday":shortDate(d);
}

/* ---- languages ------------------------------------------------------------
   A CV's language is RenderCV's locale; a translation is its own document,
   linked to the one it was translated from. CV Studio writes the part with
   one right answer -- dates, month names, section titles -- and the prose is
   translated by you or by your AI client. Nothing here asks a client to do
   anything: a client acts when you ask it to, so the app says what to ask. */
const langOf=c=>((S.state&&S.state.languages)||[]).find(l=>l.code===c)||
  {code:c||"en",native:String(c||"en").toUpperCase(),english:String(c||"en").toUpperCase()};
/* Flags for the languages people most often write a CV in here, drawn rather
   than emoji: Windows prints a flag emoji as two letters. English is the US
   flag and Portuguese the Brazilian one, as asked; the code is printed beside
   the flag either way, so a flag is never the only thing saying which. */
const FLAGS={
  fr:'<rect width="10" height="20" fill="#0055A4"/><rect x="10" width="10" height="20" fill="#fff"/>'+
    '<rect x="20" width="10" height="20" fill="#EF4135"/>',
  es:'<rect width="30" height="20" fill="#AA151B"/><rect y="5" width="30" height="10" fill="#F1BF00"/>',
  en:'<rect width="30" height="20" fill="#fff"/>'+[0,2,4,6,8,10,12].map(i=>'<rect y="'+(i*20/13).toFixed(2)+
    '" width="30" height="'+(20/13).toFixed(2)+'" fill="#B22234"/>').join("")+
    '<rect width="13" height="10.77" fill="#3C3B6E"/>'+[[2.5,2.2],[6.5,2.2],[10.5,2.2],[4.5,5.4],[8.5,5.4],
    [2.5,8.6],[6.5,8.6],[10.5,8.6]].map(([x,y])=>'<circle cx="'+x+'" cy="'+y+'" r=".75" fill="#fff"/>').join(""),
  pt:'<rect width="30" height="20" fill="#009B3A"/><path d="M15 2.2 27.6 10 15 17.8 2.4 10z" fill="#FEDF00"/>'+
    '<circle cx="15" cy="10" r="4.6" fill="#002776"/>',
};
Object.assign(FLAGS,{
  br:FLAGS.pt,
  pt_c:'<rect width="12" height="20" fill="#006600"/><rect x="12" width="18" height="20" fill="#FF0000"/><circle cx="12" cy="10" r="4.2" fill="#FFCC00"/>',
  gb:'<rect width="30" height="20" fill="#012169"/><path d="M0 0 30 20M30 0 0 20" stroke="#fff" stroke-width="3.6"/><path d="M0 0 30 20M30 0 0 20" stroke="#C8102E" stroke-width="2"/><path d="M15 0v20M0 10h30" stroke="#fff" stroke-width="6"/><path d="M15 0v20M0 10h30" stroke="#C8102E" stroke-width="3.6"/>',
  de:'<rect width="30" height="6.67" fill="#000"/><rect y="6.67" width="30" height="6.67" fill="#DD0000"/><rect y="13.33" width="30" height="6.67" fill="#FFCE00"/>',
  nl:'<rect width="30" height="6.67" fill="#AE1C28"/><rect y="6.67" width="30" height="6.67" fill="#fff"/><rect y="13.33" width="30" height="6.67" fill="#21468B"/>',
  be:'<rect width="10" height="20" fill="#000"/><rect x="10" width="10" height="20" fill="#FDDA24"/><rect x="20" width="10" height="20" fill="#EF3340"/>',
  it:'<rect width="10" height="20" fill="#009246"/><rect x="10" width="10" height="20" fill="#fff"/><rect x="20" width="10" height="20" fill="#CE2B37"/>',
  ie:'<rect width="10" height="20" fill="#169B62"/><rect x="10" width="10" height="20" fill="#fff"/><rect x="20" width="10" height="20" fill="#FF883E"/>',
  ch:'<rect width="30" height="20" fill="#D52B1E"/><rect x="13" y="4" width="4" height="12" fill="#fff"/><rect x="9" y="8" width="12" height="4" fill="#fff"/>',
  se:'<rect width="30" height="20" fill="#006AA7"/><rect x="9" width="4" height="20" fill="#FECC00"/><rect y="8" width="30" height="4" fill="#FECC00"/>',
  dk:'<rect width="30" height="20" fill="#C8102E"/><rect x="9" width="3.5" height="20" fill="#fff"/><rect y="8.25" width="30" height="3.5" fill="#fff"/>',
  no:'<rect width="30" height="20" fill="#BA0C2F"/><rect x="8.5" width="5" height="20" fill="#fff"/><rect y="7.5" width="30" height="5" fill="#fff"/><rect x="9.75" width="2.5" height="20" fill="#00205B"/><rect y="8.75" width="30" height="2.5" fill="#00205B"/>',
  fi:'<rect width="30" height="20" fill="#fff"/><rect x="9" width="4.5" height="20" fill="#003580"/><rect y="7.75" width="30" height="4.5" fill="#003580"/>',
  pl:'<rect width="30" height="10" fill="#fff"/><rect y="10" width="30" height="10" fill="#DC143C"/>',
  at:'<rect width="30" height="6.67" fill="#ED2939"/><rect y="6.67" width="30" height="6.67" fill="#fff"/><rect y="13.33" width="30" height="6.67" fill="#ED2939"/>',
  lu:'<rect width="30" height="6.67" fill="#EA141D"/><rect y="6.67" width="30" height="6.67" fill="#fff"/><rect y="13.33" width="30" height="6.67" fill="#51ADDA"/>',
  ca:'<rect width="7.5" height="20" fill="#FF0000"/><rect x="7.5" width="15" height="20" fill="#fff"/><rect x="22.5" width="7.5" height="20" fill="#FF0000"/><path d="M15 4.5 17 8.5l3-.5-2 3 1.5 2.5h-3.8v3h-1.4v-3H10.5L12 11l-2-3 3 .5z" fill="#FF0000"/>',
  mx:'<rect width="10" height="20" fill="#006847"/><rect x="10" width="10" height="20" fill="#fff"/><rect x="20" width="10" height="20" fill="#CE1126"/><circle cx="15" cy="10" r="2.2" fill="#6b4e16"/>',
  ar:'<rect width="30" height="6.67" fill="#74ACDF"/><rect y="6.67" width="30" height="6.67" fill="#fff"/><rect y="13.33" width="30" height="6.67" fill="#74ACDF"/><circle cx="15" cy="10" r="2.2" fill="#F6B40E"/>',
  sg:'<rect width="30" height="10" fill="#EF3340"/><rect y="10" width="30" height="10" fill="#fff"/><circle cx="7" cy="5" r="3.2" fill="#fff"/><circle cx="8" cy="5" r="2.8" fill="#EF3340"/>',
  jp:'<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="6" fill="#BC002D"/>',
  au:'<rect width="30" height="20" fill="#012169"/><path d="M0 0 14 10M14 0 0 10" stroke="#fff" stroke-width="2"/><path d="M7 0v10M0 5h14" stroke="#fff" stroke-width="3"/><path d="M7 0v10M0 5h14" stroke="#C8102E" stroke-width="1.8"/><circle cx="22" cy="6" r="1.1" fill="#fff"/><circle cx="25" cy="10" r="1.1" fill="#fff"/><circle cx="22" cy="15" r="1.1" fill="#fff"/><circle cx="18" cy="11" r="1.1" fill="#fff"/><circle cx="7" cy="15" r="1.6" fill="#fff"/>',
  ae:'<rect x="7.5" width="22.5" height="6.67" fill="#00732F"/><rect x="7.5" y="6.67" width="22.5" height="6.67" fill="#fff"/><rect x="7.5" y="13.33" width="22.5" height="6.67" fill="#000"/><rect width="7.5" height="20" fill="#FF0000"/>',
  in:'<rect width="30" height="6.67" fill="#FF9933"/><rect y="6.67" width="30" height="6.67" fill="#fff"/><rect y="13.33" width="30" height="6.67" fill="#138808"/><circle cx="15" cy="10" r="2.3" fill="none" stroke="#000080" stroke-width=".9"/>',
});
FLAGS.us=FLAGS.en; FLAGS.uk=FLAGS.gb;
const flag=c=>FLAGS[c]?'<svg class="flg" viewBox="0 0 30 20" aria-hidden="true">'+FLAGS[c]+'</svg>':"";
const COUNTRY_GUESS=[
  [/paris|lyon|marseille|toulouse|bordeaux|lille|nantes|montpellier|strasbourg|rennes|nice|grenoble|france|\bfr\b/i,"fr","France"],
  [/london|manchester|edinburgh|bristol|cambridge|oxford|birmingham|glasgow|leeds|belfast|\buk\b|united kingdom|england|scotland|wales|great britain/i,"gb","United Kingdom"],
  [/dublin|cork|galway|ireland/i,"ie","Ireland"],
  [/madrid|barcelona|valencia|sevilla|seville|bilbao|malaga|málaga|spain|españa/i,"es","Spain"],
  [/lisbon|lisboa|porto|braga|portugal/i,"pt_c","Portugal"],
  [/berlin|munich|münchen|hamburg|frankfurt|cologne|köln|stuttgart|düsseldorf|leipzig|germany|deutschland/i,"de","Germany"],
  [/amsterdam|rotterdam|utrecht|eindhoven|the hague|den haag|netherlands|holland/i,"nl","Netherlands"],
  [/brussels|bruxelles|antwerp|ghent|belgium|belgique/i,"be","Belgium"],
  [/zurich|zürich|geneva|genève|lausanne|basel|bern|switzerland|suisse|schweiz/i,"ch","Switzerland"],
  [/luxembourg/i,"lu","Luxembourg"],
  [/vienna|wien|graz|linz|austria|österreich/i,"at","Austria"],
  [/milan|milano|rome|roma|turin|torino|bologna|florence|firenze|italy|italia/i,"it","Italy"],
  [/stockholm|gothenburg|göteborg|malmö|malmo|uppsala|sweden/i,"se","Sweden"],
  [/copenhagen|københavn|aarhus|denmark/i,"dk","Denmark"],
  [/oslo|bergen|trondheim|norway/i,"no","Norway"],
  [/helsinki|espoo|tampere|finland/i,"fi","Finland"],
  [/warsaw|warszawa|krakow|kraków|wroclaw|wrocław|gdansk|gdańsk|poland|polska/i,"pl","Poland"],
  [/toronto|montreal|montréal|vancouver|ottawa|calgary|waterloo|canada/i,"ca","Canada"],
  [/são paulo|sao paulo|rio de janeiro|belo horizonte|curitiba|florianópolis|brazil|brasil/i,"br","Brazil"],
  [/mexico|méxico|guadalajara|monterrey/i,"mx","Mexico"],
  [/buenos aires|cordoba|córdoba|argentina/i,"ar","Argentina"],
  [/singapore/i,"sg","Singapore"],
  [/tokyo|osaka|kyoto|yokohama|japan/i,"jp","Japan"],
  [/sydney|melbourne|brisbane|perth|australia/i,"au","Australia"],
  [/dubai|abu dhabi|\buae\b|emirates/i,"ae","UAE"],
  [/bangalore|bengaluru|mumbai|delhi|hyderabad|pune|chennai|india/i,"in","India"],
  [/new york|\bnyc\b|boston|miami|atlanta|washington|chicago|austin|dallas|houston|denver|boulder|san francisco|\bsf\b|bay area|seattle|los angeles|palo alto|mountain view|california|texas|\busa\b|\bu\.s\.a?\b|united states/i,"us","United States"],
];
const _regionNames={};
function regionName(code,fallback){
  if(!code) return fallback||"";
  const lang=typeof uiLang==="function"?uiLang():"en";
  try{
    if(!_regionNames[lang]) _regionNames[lang]=new Intl.DisplayNames([lang],{type:"region"});
    return _regionNames[lang].of(code.toUpperCase())||fallback||"";
  }catch(e){ return fallback||"" }
}
function jobGeo(j){
  const rawLoc=String(j.location||((String(j.description||"").match(/location\s*:\s*([^\n.]+)/i)||[])[1]||"")).trim();
  const rawCountry=String(j.country||"").trim();
  const probe=[rawCountry,rawLoc].filter(Boolean).join(", ");
  const hit=probe?COUNTRY_GUESS.find(([re])=>re.test(probe)):null;
  const code=hit?hit[1]:null, country=rawCountry||(hit?regionName(hit[1],hit[2]):"");
  const isRemote=!country&&/\b(remote|télétravail|teletrabajo|remoto)\b/i.test(rawLoc);
  const escRe=s=>s.replace(/[.*+?^${}()|[\]\\]/g,"\\$&");
  const rawName=hit?hit[2]:country;
  const city=rawLoc&&rawName?rawLoc.replace(new RegExp("\\s*[,·-]\\s*(?:"+escRe(rawName)+"|"+escRe(country)+")$","i"),"").trim():rawLoc;
  const full=city&&country&&city.toLowerCase()!==country.toLowerCase()?city+", "+country:(city||country);
  return {code,country,city,full,isRemote,flag:code?flag(code):""};
}
/* The flag beside an application's language follows the choice at once. */
document.addEventListener("change",e=>{
  const sel=e.target.closest&&e.target.closest(".ap-lang select"); if(!sel) return;
  sel.parentNode.querySelector(".flg")?.remove();
  sel.insertAdjacentHTML("beforebegin",flag(sel.value));
});
const lchip=(c,on)=>{ const k=String(c||"en").toLowerCase();
  return '<span class="lchip'+(on?' on':'')+(FLAGS[k]?' fl':'')+'">'+flag(k)+esc(k.toUpperCase())+'</span>' };
/* The base CV and its translations: the set the Documents tabs move between. */
function baseFamily(){
  const b=S.state&&S.state.base; if(!b||b.missing) return [];
  const docs=(S.state&&S.state.documents)||[], me=docs.find(d=>d.path===b.path);
  return [{path:b.path,lang:(me&&me.lang)||"en",source:true}].concat(docs
    .filter(d=>d.translation_of===b.path)
    .map(d=>({path:d.path,lang:d.lang,source:false})));
}
const baseIn=lang=>baseFamily().find(m=>m.lang===lang)||null;
/* Whether CVs come in more than one language here. Most people apply in one
   country, and for them every tab, flag and language question is noise, so it
   is off until a translation exists or Settings turns it on. Off hides the
   language machinery; nothing is deleted, and translations come back as they
   were when it is turned on again. */
function multiLang(){
  const p=prefs().multilang;
  return p==null?baseFamily().length>1:!!p;
}
function applyMultiLang(){ document.documentElement.classList.toggle("mono",!multiLang()) }
/* Which AI clients could do the translating, in words, for the prompts. */
function clientsSay(){
  const on=(S.ai||[]).filter(c=>c.state==="connected").map(c=>c.label);
  return on.length?on.join(" or "):null;
}
function copyText(text,btn){
  const done=()=>{ if(btn){ const t=btn.textContent; btn.textContent="Copied";
    setTimeout(()=>btn.textContent=t,1400) } };
  try{ navigator.clipboard.writeText(text).then(done,()=>toast("Select the text and copy it",true)) }
  catch(e){ toast("Select the text and copy it",true) }
}
/* What to say to a client, ready to copy, with who could hear it. */
function sayBox(title,text){
  const who=clientsSay();
  return '<div class="say-box"><b>'+title+'</b><div class="say" id="say-text">'+esc(text)+'</div>'+
    '<div class="row"><span class="grow">'+(who?"Paste it into "+esc(who)+
      ". It works through CV Studio, so you can watch it land here."
      :"No AI client is connected yet. Settings → AI clients sets one up.")+'</span>'+
    (who?'':'<button class="obtn" id="say-setup">AI clients</button>')+
    '<button class="obtn" id="say-copy">Copy</button></div></div>';
}
function wireSay(text){
  const c=$("#say-copy"); if(c) c.onclick=()=>copyText(text,c);
  const st=$("#say-setup"); if(st) st.onclick=()=>{ closeSheet(); openSettings("ai") };
}

function addLanguageSheet(src){
  const have=new Set(((S.state&&S.state.documents)||[])
    .filter(d=>d.path===src||d.translation_of===src).map(d=>d.lang));
  const from=langOf((((S.state&&S.state.documents)||[]).find(d=>d.path===src)||{}).lang);
  const all=((S.state&&S.state.languages)||[]).filter(l=>!have.has(l.code));
  let pick=(all.find(l=>l.code==="fr")||all[0]||{}).code;
  openSheet(
    '<div><h3 id="sheet-title">Add a language to '+esc(docLabel(src))+'</h3>'+
      '<p>A copy in another language, saved beside it and linked to it. CVs you tailor '+
      'for a posting in that language start from it.</p></div>'+
    '<div class="fg w88"><label for="al-q">Language</label>'+
      '<input id="al-q" autocomplete="off" placeholder="Search '+all.length+' languages"></div>'+
    '<div class="al-list" id="al-list" role="radiogroup" aria-label="Language"></div>'+
    '<ul class="al-does">'+
      '<li><span>Dates, month names and “present” print in the new language.</span></li>'+
      '<li><span>Common section titles are translated: Experience, Education, Skills and the like.</span></li>'+
      '<li><span>Your name, contact details, links, company names and dates stay exactly as they are, '+
        'and the design follows '+esc(from.english)+'.</span></li>'+
    '</ul>'+
    '<p>Your text stays in '+esc(from.english)+' until you, or your AI client, translate it.</p>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
      '<button class="sbtn primary" id="al-go">Add</button></div>');
  const paint=()=>{
    const q=$("#al-q").value.trim().toLowerCase();
    const list=all.filter(l=>!q||(l.native+" "+l.english+" "+l.code).toLowerCase().includes(q));
    $("#al-list").innerHTML=list.map(l=>'<button role="radio" data-l="'+l.code+'" aria-checked="'+
      String(l.code===pick)+'">'+lchip(l.code,l.code===pick)+'<span><b>'+esc(l.native)+'</b> '+
      (l.english!==l.native?'<em>'+esc(l.english)+'</em>':'')+'</span></button>').join("")||
      '<p class="note muted">No language by that name.</p>';
    $$("#al-list [data-l]").forEach(b=>b.onclick=()=>{ pick=b.dataset.l; paint() });
    $("#al-go").textContent="Add "+(pick?langOf(pick).english:"");
    $("#al-go").disabled=!pick;
  };
  $("#al-q").oninput=paint; paint();
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#al-go").onclick=async()=>{
    $("#al-go").disabled=true;
    try{
      const r=await post("/api/language/add",{path:src,language:pick});
      if(!r.ok){ $("#al-go").disabled=false; return toast(r.error,true) }
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      S.baseLang=r.lang;
      if(S.view==="docs") drawDocuments();
      translateNextSheet(r);
    }catch(e){ $("#al-go").disabled=false; toast(e.message,true) }
  };
}
/* After the copy is written: what is done, and what to ask a client for. */
function translateNextSheet(r){
  const to=langOf(r.lang), from=langOf(r.from_lang);
  const titled=Object.keys(r.sections_titled||{}).length, left=r.sections_to_title||[];
  const text="In CV Studio, translate "+r.path+" into "+to.english+". It is the "+
    to.english+" copy of "+r.of+".";
  openSheet(
    '<div><h3 id="sheet-title">'+esc(to.english)+' copy made</h3><p>'+
      'Dates and month names are in '+esc(to.english)+(titled?', and '+titled+' section title'+
      (titled===1?' is':'s are')+' translated':'')+'. The rest of the text is still in '+
      esc(from.english)+'.'+(left.length?' Still to title: '+left.map(esc).join(", ")+'.':'')+
    '</p>'+(r.font?'<p class="muted">Its font, '+esc(r.font.language)+' Noto Sans, is '+
      'downloading now ('+r.font.mb+' MB, once). Until it arrives the page prints in a font '+
      'your computer has.</p>':'')+'</div>'+
    sayBox("To have your AI client translate it, ask it:",text)+
    '<p>Or open it and translate it yourself, field by field.</p>'+
    '<div class="foot"><button class="sbtn" data-cancel>Close</button>'+
      '<button class="sbtn primary" id="tn-open">Open the '+esc(to.english)+' CV</button></div>');
  wireSay(text);
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#tn-open").onclick=()=>{ closeSheet(); openDoc(r.path) };
}
/* What the source changed since this translation last caught up. */
function driftSheet(path,drift){
  const to=langOf(drift.lang), from=langOf(drift.from_lang||"en");
  const text="In CV Studio, bring "+path+" up to date with "+drift.of+
    ": translate what changed into "+to.english+".";
  openSheet(
    '<div><h3 id="sheet-title">What '+esc(docLabel(drift.of))+' changed</h3><p>'+
      'Since this '+esc(to.english)+' version was last brought up to date. Nothing '+
      'here has been changed for you.</p></div>'+
    '<ul class="drift-list">'+drift.changes.map(c=>'<li><span class="w">'+esc(c.where)+'</span>'+
      (c.kind!=="added"&&c.before?'<span class="l">'+lchip(from.code)+'<span class="then">'+
        esc(c.before)+'</span></span>':'')+
      (c.kind!=="removed"?'<span class="l">'+lchip(from.code,true)+'<span>'+esc(c.after||"")+
        '</span></span>':'<span class="l">'+lchip(from.code,true)+'<span class="muted">removed</span></span>')+
      (c.translation?'<span class="l">'+lchip(to.code)+'<span>'+esc(c.translation)+
        '</span></span>':'')+'</li>').join("")+'</ul>'+
    sayBox("To have your AI client carry these over, ask it:",text)+
    '<div class="foot"><button class="sbtn left" data-cancel>Close</button>'+
      '<button class="sbtn primary" id="ds-done">Mark as done</button></div>');
  wireSay(text);
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#ds-done").onclick=()=>{ closeSheet(); markTranslationDone(path) };
}
async function markTranslationDone(path){
  try{
    await post("/api/language/done",{path});
    if(S.path===path){ const d=await api("/api/doc?path="+encodeURIComponent(path));
      S.doc.drift=d.drift; paintLang() }
    S.driftBy=null;
    if(S.view==="docs") drawDocuments();
    toast("Marked as up to date");
  }catch(e){ toast(e.message,true) }
}
/* The editor's bar: which language this is, the others to switch to, and
   what it is missing from its source. */
function paintLang(){
  const sw=$("#langsw"), bar=$("#driftbar");
  const fam=S.doc&&S.doc.family, members=(fam&&fam.members)||[];
  if(members.length<2){ sw.hidden=true }
  else{
    sw.hidden=false;
    sw.innerHTML=members.map(m=>'<button data-lp="'+esc(m.path)+'"'+
      (m.path===S.path?' aria-current="true"':'')+' title="'+esc(m.path)+
      (m.source?" · the source":" · translated from "+docLabel(fam.source))+'">'+
      lchip(m.lang,m.path===S.path)+'<span class="ln">'+esc(langOf(m.lang).native)+'</span></button>').join("");
    $$("#langsw [data-lp]").forEach(b=>b.onclick=async ()=>{
      if(b.dataset.lp===S.path) return;
      if(!await leaveEdits()) return;
      openDoc(b.dataset.lp);
    });
  }
  const dr=S.doc&&S.doc.drift;
  if(!dr||(!dr.missing&&!dr.changes.length)){ bar.hidden=true; return }
  bar.hidden=false;
  const from=langOf(dr.from_lang||"en"), n=dr.changes.length;
  $("#driftbar-msg").innerHTML=dr.missing
    ? 'The '+esc(from.english)+' CV this was translated from, <b>'+esc(dr.of)+'</b>, is missing.'
    : '<b>'+esc(docLabel(dr.of))+' changed since this was translated.</b> '+n+' thing'+
      (n===1?'':'s')+' in it '+(n===1?'is':'are')+' not in this '+esc(langOf(dr.lang).english)+
      ' version yet.';
  $("#drift-show").hidden=!!dr.missing; $("#drift-done").hidden=!!dr.missing;
  $("#drift-show").onclick=()=>driftSheet(S.path,dr);
  $("#drift-done").onclick=()=>markTranslationDone(S.path);
}

/* Two lanes, and the second is the reason this screen exists. A CV written for
   an application is reachable from that application's row; one written for
   nothing was reachable from nowhere, because the only list of documents lived
   inside the editor and you needed a document open to see it. Each is shown as
   its page, since two CVs are told apart by looking at them. */
$("#dq").oninput=()=>{ clearTimeout(S.dqT); S.dqT=setTimeout(drawDocuments,120) };
$("#dq").onkeydown=e=>{ if(e.key==="Escape"&&$("#dq").value){ e.preventDefault(); $("#dq").value=""; drawDocuments() } };
$$("#dkind [data-k]").forEach(b=>b.onclick=()=>{ S.docKind=b.dataset.k; drawDocuments() });
$("#dsort").onchange=e=>{ S.docSort=e.target.value; try{ localStorage.setItem("cvs.docsort",S.docSort) }catch(x){} drawDocuments() };
try{ S.docSort=localStorage.getItem("cvs.docsort")||"recent"; $("#dsort").value=S.docSort }catch(e){}
function drawDocuments(){
  mountBase($("#docbase"),"bhero");
  const docs=(S.state&&S.state.documents)||[];
  $("#dcount").textContent=String(docs.length);
  const basePath=(S.state&&S.state.base&&S.state.base.path)||null;
  const owner={};
  for(const j of (S.jobs||[])){
    if(j.cv_path) owner[j.cv_path]=j;
    if(j.letter_path) owner[j.letter_path]=j;
  }
  /* The base's translations live in its tabs, not in the lanes. */
  const family=new Set(baseFamily().map(m=>m.path));
  /* The base CV's language first, then the rest by how many documents use it. */
  const srcLang=(baseFamily()[0]||{}).lang||"en", nOf={};
  docs.forEach(d=>{ const c=d.lang||"en"; nOf[c]=(nOf[c]||0)+1 });
  const langs=Object.keys(nOf).sort((a,b)=>(b===srcLang)-(a===srcLang)||nOf[b]-nOf[a]);
  const filt=$("#dfilter");
  if(langs.length<2||!multiLang()){ filt.hidden=true; S.docLang=null }
  else{
    filt.hidden=false;
    filt.innerHTML=[null,...langs].map(c=>'<button data-dl="'+(c||"")+'" aria-pressed="'+
      String((S.docLang||null)===c)+'">'+(c?flag(c)+esc(langOf(c).native):"All languages")+
      '</button>').join("");
    $$("#dfilter [data-dl]").forEach(b=>b.onclick=()=>{ S.docLang=b.dataset.dl||null;
      drawDocuments() });
  }
  const attached=[], loose=[];
  const q=palFold(($("#dq").value||"").trim()), kind=S.docKind||"";
  const words=q.split(/\s+/).filter(Boolean);
  const hay=d=>{ const j=owner[d.path]; return palFold([docTitle(d),d.path,j&&j.company,j&&j.title].filter(Boolean).join(" ")) };
  for(const d of docs){
    if(family.has(d.path)) continue;
    if(S.docLang&&(d.lang||"en")!==S.docLang) continue;
    const isL=d.group==="Cover letters"||d.letter;
    if(kind==="cv"&&isL||kind==="letter"&&!isL) continue;
    if(words.length){ const h=hay(d); if(!words.every(w=>h.includes(w))) continue }
    (owner[d.path]?attached:loose).push(d);
  }
  baseDrift();
  const by=S.docSort||"recent", name=d=>docTitle(d).toLocaleLowerCase(),
    co=d=>((owner[d.path]||{}).company||"\uffff").toLocaleLowerCase();
  const order=by==="name"?(a,b)=>name(a).localeCompare(name(b))
    :by==="company"?(a,b)=>co(a).localeCompare(co(b))||name(a).localeCompare(name(b))
    :(a,b)=>(b.mtime||0)-(a.mtime||0);
  attached.sort(order); loose.sort(order);

  const card=d=>{
    const j=owner[d.path], letter=d.group==="Cover letters";
    const copied=d.base?d.base.split("/").pop().replace(/\.ya?ml$/,""):null;
    const about=j
      ? '<span class="dot '+statusTone(j.status)+'"></span>'+esc(j.company)+' \u00b7 '+esc(j.title)
      : copied?'Copied from '+esc(copied):letter?'Cover letter':'Not attached';
    const th=S.docThumbs[d.path];
    return '<div class="dcw"><button class="dcard" data-open="'+esc(d.path)+'" title="'+esc(d.path)+'">'+
      '<span class="pg">'+(th&&th.png?'<img alt="" loading="lazy" src="'+esc(th.png+tok())+'">'
        :'<span>'+(th&&th.failed?"Doesn\u2019t render":"Rendering\u2026")+'</span>')+
        ((d.lang||"en")!==srcLang?'<span class="langs">'+lchip(d.lang,true)+'</span>':'')+
        (letter?'<span class="tag">Letter</span>':'')+'</span>'+
      '<span class="meta"><b>'+esc(docTitle(d))+rvPill(d)+'</b><span>'+about+'</span>'+
        '<em>'+esc(mtimeLabel(d.mtime))+(S.pages[d.path]?" \u00b7 "+S.pages[d.path]+
          " page"+(S.pages[d.path]===1?"":"s"):"")+'</em></span></button>'+
      '<button class="dmore" data-more="'+esc(d.path)+'" aria-label="'+esc(t("Rename or delete {name}",{name:docTitle(d)}))+
        '" title="'+esc(t("Rename or delete"))+'">&#8943;</button></div>';
  };
  const lane=(title,list,why,empty)=>
    '<section class="dsec"><h2>'+title+'<span class="n">'+list.length+'</span></h2>'+
    '<p class="why">'+why+'</p>'+
    (list.length?'<div class="dgrid">'+list.map(card).join("")+'</div>'
      :'<div class="dempty">'+empty+'</div>')+'</section>';

  const found=attached.length+loose.length, filtered=words.length||kind;
  $("#doclanes").innerHTML=filtered&&!found
    ? '<div class="dempty">'+esc(t("No document matches."))+' <button class="linkbtn" id="dq-clear">'+esc(t("Show them all"))+'</button></div>'
    : lane("Written for an application",attached,
      "Each is attached to the application it was tailored for. Opening one here is the "+
      "same as opening it from that row.",
      "None yet. On Applications, <b>Tailor a CV</b> on a row copies the base CV for it.")+
    lane("Everything else",loose,
      "CVs and letters no application points at: a master copy, an old version, a draft "+
      "you have not attached yet.",
      "Nothing here. <b>New document</b> or <b>Import</b> puts a CV here.");
  paintStatus();
  $$("#doclanes .dcard").forEach(b=>{
    b.onclick=async ()=>{
      if(!await leaveEdits()) return;
      openDoc(b.dataset.open);
    };
  });
  $$("#doclanes [data-more]").forEach(b=>b.onclick=e=>{ e.stopPropagation(); docMenuSheet(b.dataset.more) });
  const clr=$("#dq-clear"); if(clr) clr.onclick=()=>{ $("#dq").value=""; S.docKind=""; drawDocuments() };
  $$("#dkind [data-k]").forEach(b=>b.setAttribute("aria-checked",String((S.docKind||"")===b.dataset.k)));
  docThumbs();
}
/* Every card's page. What is on disk is shown at once; anything missing or
   older than its YAML is rendered one at a time behind it, and only while
   Documents is on screen, so a big workspace never queues up Typst for a
   screen nobody is looking at. */
let docThumbRun=0;
async function docThumbs(){
  const run=++docThumbRun;
  const basePath=(S.state&&S.state.base&&S.state.base.path)||null;
  const docs=((S.state&&S.state.documents)||[]).filter(d=>d.path!==basePath);
  const stale=[];
  for(const d of docs){
    const have=S.docThumbs[d.path];
    if(have&&have.mtime===d.mtime) continue;
    try{
      const t=await api("/api/thumb?path="+encodeURIComponent(d.path));
      if(run!==docThumbRun) return;
      S.docThumbs[d.path]={png:t.png,mtime:t.fresh?d.mtime:null};
      if(t.png) docPaintThumb(d.path);
      if(!t.fresh) stale.push(d);
    }catch(e){ return }
  }
  for(const d of stale){
    if(run!==docThumbRun||S.view!=="docs") return;
    try{
      const r=await post("/api/render",{path:d.path});
      if(r.ok){ S.pages[d.path]=r.pages; S.docThumbs[d.path]={png:r.pngs[0],mtime:d.mtime} }
      else S.docThumbs[d.path]={png:(S.docThumbs[d.path]||{}).png,failed:true,mtime:d.mtime};
      docPaintThumb(d.path);
    }catch(e){ return }
  }
}
/* How far behind each translation of the base is, for its tab. Fetched once
   per visit to Documents: a translation only falls behind when its source is
   saved, and the tabs are not worth a request per save. */
async function baseDrift(){
  if(S.driftBy) return;
  S.driftBy={};
  for(const m of baseFamily().filter(x=>!x.source)){
    try{ const d=await api("/api/doc?path="+encodeURIComponent(m.path)); S.driftBy[m.path]=d.drift }
    catch(e){}
  }
  if(S.view==="docs") mountBase($("#docbase"),"bhero");
}
function docPaintThumb(path){
  if(baseFamily().some(m=>m.path===path&&!m.source)) mountBase($("#docbase"),"bhero");
  const b=$('#doclanes .dcard[data-open="'+CSS.escape(path)+'"] .pg'); if(!b) return;
  const th=S.docThumbs[path], tag=b.querySelector(".tag"), lg=b.querySelector(".langs");
  b.innerHTML=th&&th.png?'<img alt="" src="'+esc(th.png+tok())+'">'
    :'<span>'+(th&&th.failed?"Doesn\u2019t render":"Rendering\u2026")+'</span>';
  if(lg) b.append(lg);
  if(tag) b.append(tag);
  const em=b.parentElement.querySelector(".meta em"), d=((S.state&&S.state.documents)||[])
    .find(x=>x.path===path);
  if(em&&d) em.textContent=mtimeLabel(d.mtime)+(S.pages[path]?" \u00b7 "+S.pages[path]+
    " page"+(S.pages[path]===1?"":"s"):"");
}
function baseSheet(){
  const b=S.state&&S.state.base;
  /* Letters are excluded for the same reason the New document sheet filters
     its Base on list: a cover letter as the thing every CV is copied from
     produces nonsense. */
  const docs=(S.state.documents||[]).filter(d=>d.group!=="Cover letters");
  if(!docs.length) return toast("There are no CVs in the workspace yet.",true);
  openSheet(
    '<div><h3 id="sheet-title">Base CV</h3><p>Every new CV starts as a copy of '+
    'this one, and a tailored CV is measured against whatever it was copied '+
    'from. Changing it touches no document: CVs already tailored keep the base '+
    'they were made from.</p></div>'+
    '<div class="fg w88"><label>Use</label><select id="bs-doc">'+
      docs.map(d=>'<option value="'+esc(d.path)+'"'+
        (b&&b.path===d.path?" selected":"")+'>'+esc(docTitle(d))+'</option>').join("")+
    '</select></div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="bs-go">Set as base</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#bs-go").onclick=async()=>{
    const path=$("#bs-doc").value;
    try{
      const r=await post("/api/base",{path:path});
      S.state.base=r.base;
      closeSheet(); paintBase();
      toast(t("{name} is the base CV",{name:baseLabel()}));
    }catch(e){ toast(e.message,true) }
  };
}
