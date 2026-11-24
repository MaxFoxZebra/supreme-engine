/* Applications table, detail view, tailoring, posting card, and rounds. */
/* ---- tailoring a CV for an application -----------------------------------
   The one action the home screen exists to offer. Not a dialog: a dialog is
   for choices, and every choice here has already been made -- the base is
   pinned, the name comes from the application, and the link is the whole
   point. The New document sheet stays for when you do want the choices. */
function uniqueDocName(stem){
  /* Two applications to one company for one role is ordinary -- re-applying, or
     two openings -- so the second one gets a suffix rather than a dead end. */
  const taken=p=>(S.state.documents||[]).some(d=>d.path==="profile/"+p+".yaml");
  if(!taken(stem)) return stem;
  for(let n=2;n<50;n++) if(!taken(stem+"-"+n)) return stem+"-"+n;
  return stem+"-"+Date.now();
}
async function tailorFor(id,force){
  const j=(S.jobs||[]).find(x=>x.id===id);
  if(!j||S.tailoring.has(id)) return;
  const b=S.state&&S.state.base;
  if(!b||b.missing){
    toast(b?"The base CV is missing, so there is nothing to copy."
           :"Choose a base CV first \u2014 the tailored copy starts from it.",true);
    return baseSheet();
  }
  /* In the posting's language, from the base CV in that language. Without
     one, ask: translating the base now makes it for every later posting. */
  const lang=j.language||j.language_guess;
  const fam=baseFamily(), from=(lang&&baseIn(lang))||fam[0];
  if(multiLang()&&lang&&!baseIn(lang)&&!force) return tailorLangSheet(j,lang);
  const name=uniqueDocName(derivedName(j.title,j.company));
  S.tailoring.add(id); drawJobs();
  try{
    /* /api/new already records what it was copied from, so the new document's
       vs-base marks work with nothing extra done here. */
    const r=await post("/api/new",{name:name,kind:"cv",from:from.path});
    const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
    try{
      await post("/api/jobs/update",{id:j.id,cv_path:r.path});
      await loadJobs();
    }catch(e){
      /* The document exists either way, and an unlinked document is the
         recoverable half: the link chip in the editor attaches it. A job
         pointing at a file that was never written would not be. */
      toast(t("Created {name}, but linking it to {co} failed: {err} Use the link chip in the editor.",
            {name,co:j.company,err:tx(e.message)}),true);
    }
    openDoc(r.path);
    toast(t("Tailored from {p}",{p:docLabel(from.path)}));
  }catch(e){
    toast(e.message,true);
  }finally{
    S.tailoring.delete(id);
    if(S.view==="jobs") drawJobs();
  }
}

function tailorLangSheet(j,lang){
  const to=langOf(lang), src=langOf((baseFamily()[0]||{}).lang);
  openSheet(
    '<div><h3 id="sheet-title">This posting is in '+esc(to.english)+'</h3><p>Your base CV has no '+
      esc(to.english)+' version, so a CV tailored now would start in '+esc(src.english)+'.</p></div>'+
    '<p>Adding '+esc(to.english)+' to the base CV makes a copy with the dates and section '+
      'titles already in '+esc(to.english)+', for this application and every later one. '+
      'Your AI client, or you, then translate the text.</p>'+
    '<div class="foot"><button class="sbtn left" data-cancel>Cancel</button>'+
      '<button class="sbtn" id="tl-anyway">Tailor in '+esc(src.english)+'</button>'+
      '<button class="sbtn primary" id="tl-add">Add '+esc(to.english)+' to the base CV</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#tl-anyway").onclick=()=>{ closeSheet(); tailorFor(j.id,true) };
  $("#tl-add").onclick=async()=>{
    $("#tl-add").disabled=true;
    try{
      const r=await post("/api/language/add",{path:baseFamily()[0].path,language:lang});
      if(!r.ok){ $("#tl-add").disabled=false; return toast(r.error,true) }
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      translateNextSheet(r);
    }catch(e){ $("#tl-add").disabled=false; toast(e.message,true) }
  };
}

/* What the list is showing, in words, for the header above it. */
function filterTitle(){
  const f=S.jfilter;
  if(f.kind==="status") return prettyStatus(f.value);
  if(f.kind==="node") return (S.labels&&S.labels[f.value])||"Applications";
  if(f.kind==="saved") return f.value;
  if(f.kind==="match") return MATCH_RAIL.find(([k])=>k===f.value)?.[1]||"Applications";
  if(f.kind==="alert"){
    const a=ATTENTION.find(([k])=>k===f.value);
    return a?a[1]:"Applications";
  }
  return "All applications";
}
/* The order of the table. By default, what needs you first: an offer to
   answer, a late follow-up, the interviews coming, the follow-ups coming,
   rounds to book; the rest by last change. A header click sorts by that
   column, a second click the other way. */
function nextKey(j){
  if(DEAD_ST.has(j.status)) return [9,0];
  const today=todayKey(), at=interviewMoment(j);
  if(j.status==="offer") return [0,0];
  if(j.followup_date&&j.followup_date<today) return [1,j.followup_date];
  if(at&&at>new Date()) return [2,+at];
  if(j.followup_date) return [3,j.followup_date];
  if(j.status==="interviewing") return [4,0];
  return [5,0];
}
const JOB_SORT_KEYS=new Set(["company","title","match","status","applied","next"]);
function savedJobSort(){
  let s=prefs().jsort;
  if(!s||typeof s!=="object"){ try{ s=JSON.parse(localStorage.getItem("cvs.jsort")||"null") }catch(e){} }
  if(s&&JOB_SORT_KEYS.has(s.key)&&(s.dir===1||s.dir===-1)) return {key:s.key,dir:s.dir};
  return {key:"next",dir:1};
}
function sortJobs(rows){
  const {key,dir}=S.jsort||(S.jsort=savedJobSort());
  const upd=j=>String(j.updated_at||"");
  const by={
    company:j=>String(j.company||"").toLowerCase(), title:j=>String(j.title||"").toLowerCase(),
    status:j=>(S.statuses||[]).indexOf(j.status), applied:j=>String(appliedAt(j)||""),
    match:j=>j.match.score,
  };
  const cmp=(a,b)=>a<b?-1:a>b?1:0;
  return rows.slice().sort((a,b)=>{
    if(key==="next"){ const x=nextKey(a), y=nextKey(b);
      return dir*(cmp(x[0],y[0])||cmp(x[1],y[1]))||cmp(upd(b),upd(a)) }
    /* Not scored yet sinks, whichever way the scores run. */
    if(key==="match"&&hasMatch(a)!==hasMatch(b)) return hasMatch(a)?-1:1;
    if(key==="match"&&!hasMatch(a)) return cmp(upd(b),upd(a));
    return dir*cmp(by[key](a),by[key](b))||cmp(upd(b),upd(a));
  });
}
function drawSortHead(){
  const {key,dir}=S.jsort||(S.jsort=savedJobSort());
  $$(".thead [data-sort]").forEach(b=>{
    const on=b.dataset.sort===key;
    b.classList.toggle("on",on);
    b.setAttribute("aria-sort",on?(dir>0?"ascending":"descending"):"none");
    b.dataset.arrow=on?(dir>0?"↑":"↓"):"";
  });
}
$$(".thead [data-sort]").forEach(b=>b.onclick=()=>{
  const k=b.dataset.sort, cur=S.jsort||savedJobSort();
  /* Dates read newest first on the first click, matches best first. */
  S.jsort={key:k,dir:cur.key===k?-cur.dir:(k==="applied"||k==="match"?-1:1)};
  try{ localStorage.setItem("cvs.jsort",JSON.stringify(S.jsort)) }catch(e){}
  setPref("jsort",S.jsort);
  drawJobs();
});
/* The next dated thing on an application: an interview coming, else the
   follow-up, late in red. What a job seeker scans the list for. */
function nextStep(j,due){
  const at=!DEAD_ST.has(j.status)&&interviewMoment(j);
  if(at&&at>new Date())
    return '<span class="when iv" title="'+esc(interviewLine(j))+'">'+esc(t("Interview"))+' · '+
      esc(fmtKey(dayIn(at),{weekday:"short",day:"numeric"}))+'</span>';
  if(j.status==="offer"){
    const h=(j.status_history||[]).filter(x=>x.status==="offer").pop();
    const days=h?dayDiff(String(h.at).slice(0,10),todayKey()):0;
    /* After a month the offer was answered and the list was not told. */
    if(days>30) return '<span class="when" title="'+esc(t("Offer since {d}",{d:shortDate(String(h.at).slice(0,10))}))+'">'+esc(t("Accepted or declined?"))+'</span>';
    /* Days since the offer, not a countdown: red once it has waited a while. */
    return '<span class="when'+(days>5?" due":"")+'" title="'+esc(t("Answer the offer"))+(h?' · '+esc(t("Offer since {d}",{d:shortDate(String(h.at).slice(0,10))})):'')+'">'+
      esc(t("Reply to offer"))+' · '+esc(days?t("{n} d ago",{n:days}):t("today"))+'</span>';
  }
  if(j.followup_date&&!DEAD_ST.has(j.status))
    return '<span class="when'+(due?" due":"")+'">'+esc(t("Follow up"))+' · '+esc(shortDate(j.followup_date))+'</span>';
  if(j.status==="interviewing") return '<span class="when">'+esc(t("Book the next round"))+'</span>';
  return '<span class="when none">–</span>';
}
/* The match column: the ring and the band, or a dash until it is scored. */
function matchCell(j){
  if(!hasMatch(j)) return '<span class="match none" title="'+esc(t("Not scored yet. Ask your AI client to score it against your base CV."))+'">–</span>';
  const m=j.match, b=matchBand(m.score);
  const tip=[t(b[3])+" · "+t("{n} out of 10",{n:matchNum(m.score)}),m.summary,matchStaleWords(m)].filter(Boolean).join("\n");
  return '<span class="match '+bandCls(b[0])+((m.stale||[]).length?' stale':'')+'" title="'+esc(tip)+'">'+matchRing(m)+
    '<span class="mlab">'+esc(t(b[2]))+'</span></span>';
}
/* Sent is the step that matters most, so from there on the status is a
   coloured badge: a tick and "Applied" once it is out, the stage's colour
   after. A draft stays a quiet dot, and so does an application that ended. */
const STATUS_PILL={applied:"sent",interviewing:"live",offer:"won",accepted:"won"};
const TICK='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
function statusCell(j){
  const pill=STATUS_PILL[j.status];
  if(!pill) return '<span class="st"><span class="dot '+statusTone(j.status)+'"></span>'+esc(prettyStatus(j.status))+'</span>';
  if(j.status==="applied")
    return '<span class="st"><span class="stp sent" title="'+esc(t("Awaiting reply"))+'">'+TICK+'<b>'+esc(t("Applied"))+'</b>'+
      '<i>'+esc(t("awaiting reply"))+'</i></span></span>';
  return '<span class="st"><span class="stp '+pill+'">'+(j.status==="accepted"?TICK:'<span class="dot '+statusTone(j.status)+'"></span>')+
    '<b>'+esc(prettyStatus(j.status))+'</b></span></span>';
}
function rowOrigin(j){
  const g=jobGeo(j), lc=j.language||j.language_guess, lg=lc?langOf(lc):null;
  const place=g.country||g.city||"";
  if(!place&&!lg) return "";
  const tip=[g.full||place,lg?t("Written in {lang}",{lang:lg.native}):""].filter(Boolean).join(" · ");
  return '<span class="jorig" title="'+esc(tip)+'">'+
    (g.flag||"")+(place?'<span class="jc">'+esc(place)+'</span>':"")+
    (lg?'<i class="jl">'+esc(lg.code.toUpperCase())+'</i>':"")+'</span>';
}
function drawJobs(){
  drawRail();
  drawSortHead();
  drawNextUp();
  const rows=visibleJobs();
  $("#jtitle").textContent=filterTitle();
  $("#jcount").textContent=S.jready?String(rows.length):"";
  const docName=p=>p?p.split("/").pop().replace(/\.ya?ml$/,""):null;
  $("#jobrows").innerHTML=rows.length?rows.map(j=>{
    const cv=docName(j.cv_path), letter=docName(j.letter_path);
    /* A row with a letter and no CV used to read "no CV yet" and drop the
       letter on the floor, which was wrong before and would now be worse: the
       offer to make one would be standing on top of a document that exists. */
    /* What there is, not what the files are called: the names are in the
       tooltip and one click away. */
    const docs=(cv?esc(t("CV"))+(letter?" + "+esc(t("letter")):""):(letter?esc(t("Letter only")):null));
    const rvd=[j.cv_path,j.letter_path].map(docByPath).find(d=>d&&d.review);
    const openable=cv?j.cv_path:j.letter_path;
    const ap=appliedAt(j);
    const due=j.followup_date&&j.followup_date<=isoToday();
    const jb=jobBoard(j);
    return '<button class="trow'+(DEAD_STATUS.has(j.status)?" dead":"")+
      (S.jsel===j.id?" sel":"")+'" data-id="'+esc(j.id)+'">'+
      '<span class="co">'+companyMark(j)+'<span class="con">'+
        esc(j.company)+'</span></span>'+
      '<span class="role"><b>'+esc(j.title)+'</b>'+
        (jb?'<span class="via" title="Found on '+esc(jb.label)+'">'+boardMark(jb)+'</span>':'')+rowOrigin(j)+
      '</span>'+matchCell(j)+
      '<span>'+(docs?'<span class="docs" data-open="'+esc(openable)+'" title="'+esc([cv,letter].filter(Boolean).join(" · "))+'">'+docs+'</span>'+rvPill(rvd)
               /* Sent without a CV linked: nothing to tailor any more, only
                  a CV to name, on the application. */
               :j.status!=="pending"?'<span class="docs none" title="'+esc(t("No CV linked"))+'">–</span>'
               :S.tailoring.has(j.id)
                 ?'<span class="docs busy">Tailoring\u2026</span>'
                 /* The busy label is rendered from state rather than written
                    onto the node, because the workspace poll can redraw this
                    whole table underneath a copy that is still running. */
                 /* Quiet until you are on the row. Six ochre offers down one
                    column was the loudest thing on the screen, and the least
                    urgent. */
                 :'<span class="docs make" data-tailor="'+esc(j.id)+'" title="'+
                  'Copy the base CV, name it after this application, and open it'+
                  '"><i class="nt">Not tailored</i><u class="tl">Tailor a CV</u></span>')+'</span>'+
      statusCell(j)+
      '<span class="when'+(ap?"":" none")+'">'+(ap?esc(shortDate(ap)):"–")+'</span>'+
      nextStep(j,due)+
      '</button>';
  }).join(""):(S.jobs.length
    ? '<div class="empty"><h3>Nothing matches</h3>'+
      '<p>Try another filter, or clear the search.</p></div>'
    /* The home screen of an empty workspace. This used to be the only place
       the app explained itself, on a CVs screen nobody lands on any more. */
    : '<div class="empty"><h3>No applications yet</h3>'+
      '<p>Add the roles you are applying for. Each one gets a CV tailored from '+
      'your base, in a click, and the funnel shows where they actually go.</p>'+
      '<p>'+(S.state&&S.state.base&&!S.state.base.missing
        ? "Your base CV is <b>"+esc(baseLabel())+"</b>. Every application starts "+
          "as a copy of it."
        : "Pick a base CV above and every application can start from it.")+'</p>'+
      '<div class="cta"><button class="sbtn primary" id="jb-first">Add an application'+
      '</button><button class="sbtn" id="jb-ai">Connect an AI client</button>'+
      '</div></div>');

  $$("#jobrows [data-id]").forEach(b=>b.onclick=e=>{
    if(e.target.closest("[data-open],[data-tailor]")) return;
    selectJob(b.dataset.id);
  });
  $$("#jobrows [data-tailor]").forEach(el=>el.onclick=e=>{
    e.stopPropagation();
    tailorFor(el.dataset.tailor);
  });
  const first=$("#jb-first"); if(first) first.onclick=()=>newJobSheet();
  const jai=$("#jb-ai"); if(jai) jai.onclick=()=>$("#btn-ai").click();
  $$("#jobrows [data-open]").forEach(a=>a.onclick=async e=>{
    e.stopPropagation();
    if(!await leaveEdits()) return;
    openDoc(a.dataset.open);
  });
  if(S.jsel&&!rows.some(j=>j.id===S.jsel)) S.jsel=null;
  drawJobInspector();
  paintStatus();
}
$("#jobq").addEventListener("input",()=>drawJobs());

function selectJob(id){
  if(S.jsel!==id) S.roundOpen=null;
  navNote();
  S.jsel=id;
  if(id) palRemember({job:id});
  const j=S.jobs.find(x=>x.id===id);
  /* A job reached from the editor or the funnel may be filtered out of the
     current view; widen the filter rather than selecting something invisible. */
  if(j&&!visibleJobs().some(x=>x.id===id)){
    S.jfilter={kind:"all",value:""}; $("#jobq").value="";
  }
  /* The row is already there: move the highlight, not the whole table. */
  if(!moveSel()) drawJobs();
  const row=$("#jobrows .trow.sel");
  if(row) row.scrollIntoView({block:"nearest"});
}
/* Selection touches one class on two rows and the panels around the table;
   rebuilding five hundred rows for it is what made opening one slow. False
   when the row is not drawn, and the table has to be. */
function moveSel(){
  const host=$("#jobrows");
  const want=S.jsel?host.querySelector('.trow[data-id="'+CSS.escape(S.jsel)+'"]'):null;
  if(S.jsel&&!want) return false;
  host.querySelectorAll(".trow.sel").forEach(r=>{ if(r!==want) r.classList.remove("sel") });
  if(want) want.classList.add("sel");
  drawNextUp(); drawJobInspector(); paintStatus();
  return true;
}

/* Everything you set once when the application is created and rarely touch
   again. Company and Role are here rather than at the top because the peek's
   own header already prints them, and they used to be the first two fields
   you read -- restating the title one line below itself. */
const JOB_MORE=[
  ["company","Company","text"],["title","Role","text"],
  ["location","Location","text"],["country","Country","text"],["url","Link","text"],
  ["salary_expected","Salary","number"],
];
/* The rows the arrows walk: what the table is currently showing, in the order
   it is showing it, so Down always means "the row under this one". */
const peekRows=()=>visibleJobs();
function peekStep(delta){
  const rows=peekRows();
  const i=rows.findIndex(x=>x.id===S.jsel);
  if(i<0) return;
  const next=rows[i+delta];
  if(!next) return;
  S.jsel=next.id;
  drawJobs();
  drawJobInspector();
  const row=$('#jobrows [data-id="'+next.id+'"]');
  if(row) row.scrollIntoView({block:"nearest"});
}
function closePeek(){
  S.jsel=null;
  navNote();
  if(S.jobs){ byRecent(); drawJobs() }
  $("#jpeek").hidden=true;
  $("#v-jobs").classList.remove("peeking");
  moveSel();
}


/* ---- an application's posting ---------------------------------------------
   Stored as Markdown when an AI client saved it, and as pasted text when a
   person did. Both read the same: headings from "#" lines, or from a short line
   ending in a colon; lists from "-", "*" or "•"; bold and links inline. */
function postInline(t){
  let out=esc(t);
  out=out.replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/(^|\W)\*(\S.*?\S|\S)\*(?=\W|$)/g,"$1<i>$2</i>");
  out=out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  out=out.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,'$1<a href="$2" target="_blank" rel="noreferrer">$2</a>');
  return out;
}
function postingHTML(text){
  const lines=String(text||"").replace(/\r/g,"").split("\n");
  let html="", list=null, para=[];
  /* A line break inside a pasted paragraph is usually meant: "Location: ..."
     under the title is its own line, not the rest of the title's sentence. */
  const flushP=()=>{ if(para.length){ html+="<p>"+para.map(postInline).join("<br>")+"</p>"; para=[] } };
  const flushL=()=>{ if(list){ html+="<ul>"+list.map(i=>"<li>"+postInline(i)+"</li>").join("")+"</ul>"; list=null } };
  for(const raw of lines){
    const l=raw.trim();
    if(!l){ flushP(); flushL(); continue }
    let m;
    if((m=l.match(/^#{1,6}\s+(.+)$/))||(l.length<60&&/:$/.test(l)&&!/^[-*•]/.test(l)&&!/https?:/.test(l))){
      flushP(); flushL();
      html+="<h4>"+postInline((m?m[1]:l.replace(/:$/,"")).replace(/\*\*/g,""))+"</h4>"; continue;
    }
    if((m=l.match(/^[-*•]\s+(.+)$/))||(m=l.match(/^\d+[.)]\s+(.+)$/))){ flushP(); (list=list||[]).push(m[1]); continue }
    flushL(); para.push(l);
  }
  flushP(); flushL();
  return html;
}
/* The facts a posting buries, pulled up as chips. Only what reads the same
   in every posting: a place, a salary range, travel, sponsorship. */
function postingChips(j){
  const txt=String(j.description||""), out=[];
  const where=j.location||((txt.match(/location\s*:\s*([^\n.]+)/i)||[])[1]||"").trim();
  const mode=(txt.match(/\b(remote|hybrid|on-?site)\b/i)||[])[1];
  /* The place is the posting's; how you work there is the app's word for it. */
  const how=mode&&t(mode[0].toUpperCase()+mode.slice(1).toLowerCase().replace(/^on-?site$/,"On-site"));
  if(where) out.push(where+(mode&&!new RegExp(mode,"i").test(where)?" · "+how:""));
  else if(mode) out.push(how);
  const pay=txt.match(/[$€£]\s?\d[\d,.]*\s?[kK]?\s?(?:[-–]|to)\s?[$€£]?\s?\d[\d,.]*\s?[kK]?(?:\s?(?:USD|EUR|GBP))?|\d[\d\s.,]*\s?(?:[kK]\s?)?€\s?(?:[-–]|à|to)\s?\d[\d\s.,]*\s?(?:[kK]\s?)?€/);
  if(pay) out.push(pay[0].replace(/\s+/g," ").trim());
  const tr=txt.match(/travel[^.\n]{0,40}?(\d{1,2}\s?(?:[-–]\s?\d{1,2}\s?)?%)/i);
  if(tr) out.push("Travel "+tr[1].replace(/\s/g,""));
  if(/visa sponsorship|sponsor(?:ship)? (?:a |your )?visa/i.test(txt)) out.push("Visa sponsorship");
  return out;
}
/* Where it was found, chosen rather than typed: the boards the app knows,
   with their marks, then the ways that are not a board. */
const SRC_FIRST=["linkedin","indeed","welcometothejungle","glassdoor","greenhouse","lever","wellfound"];
const SRC_OTHER=[["careers","Company’s careers page","↗"],["referral","Referral","♥"],
  ["recruiter","Recruiter","☎"]];
function sourceMark(j){
  const b=jobBoard(j);
  /* A long name shortens where the fact has only a little room; hover says it all. */
  if(b) return boardMark(b)+'<span class="nm" data-noi18n title="'+esc(b.label)+'">'+esc(b.short||b.label)+'</span>';
  const o=SRC_OTHER.find(x=>x[1]===j.source);
  if(j.source) return (o?'<span class="gl">'+o[2]+'</span>':'')+'<span class="nm">'+esc(j.source)+'</span>';
  return '<span class="nm" style="color:var(--t500)">Not set</span>';
}
function sourceMenu(j,btn,pick){
  pick=pick||(v=>saveJob(j.id,{source:v}));
  let m=$("#ap-menu"); if(m){ m.remove(); return }
  const cur=(jobBoard(j)||{}).id, said=String(j.source||"");
  const boards=SRC_FIRST.map(id=>BOARDS.find(b=>b.id===id)).filter(Boolean)
    .concat(BOARDS.filter(b=>!SRC_FIRST.includes(b.id)));
  m=document.createElement("div"); m.id="ap-menu"; m.className="ap-menu"; m.setAttribute("role","listbox");
  m.setAttribute("aria-label","Where you found it");
  m.innerHTML=boards.map(b=>'<button role="option" data-src="'+esc(b.label)+'" aria-selected="'+
      String(b.id===cur)+'">'+boardMark(b)+esc(b.label)+'</button>').join("")+'<hr>'+
    SRC_OTHER.map(([k,l,g])=>'<button role="option" data-src="'+esc(l)+'" aria-selected="'+
      String(!cur&&said===l)+'"><span class="gl">'+g+'</span>'+esc(l)+'</button>').join("")+
    '<button role="option" data-src=""><span class="gl">…</span>Other…</button>';
  document.body.append(m);
  const r=btn.getBoundingClientRect();
  m.style.left=Math.min(r.left,innerWidth-260)+"px";
  m.style.top=Math.min(r.bottom+6,innerHeight-370)+"px";
  m.onclick=async e=>{ const o=e.target.closest("[data-src]"); if(!o) return; m.remove();
    let v=o.dataset.src;
    if(!v){ v=(await askText({title:t("Where did you find it?"),body:t("A job board, a referral, a recruiter…"),ok:t("Save"),input:{value:jobBoard(j)?"":said}})||"").trim(); if(!v) return }
    pick(v) };
  setTimeout(()=>document.addEventListener("pointerdown",function off(ev){
    if(!m.contains(ev.target)){ m.remove(); document.removeEventListener("pointerdown",off,true) } },true),0);
}
/* A document on the application, as its first page. */
async function apThumb(el,path){
  let th=S.docThumbs[path];
  if(!th){
    try{ const t=await api("/api/thumb?path="+encodeURIComponent(path));
      th={png:t.png}; if(!t.fresh){ const r=await post("/api/render",{path});
        th=r.ok?{png:r.pngs[0]}:{png:t.png||null,failed:!t.png} } }catch(e){ th={png:null,failed:true} }
    S.docThumbs[path]=th;
  }
  if(el.isConnected) el.innerHTML=th&&th.png?'<img alt="" src="'+esc(th.png+tok())+'">'
    :'<span>'+(th&&th.failed?"Doesn’t render":"Rendering…")+'</span>';
}
function drawJobInspector(){
  const j=S.jobs.find(x=>x.id===S.jsel);
  const peek=$("#jpeek"), head=$("#jinsp-title"), body=$("#jinsp-body");
  $("#v-jobs").classList.toggle("peeking",!!j);
  if(!j){ peek.hidden=true; return }
  peek.hidden=false;
  head.textContent=j.title||j.company;
  head.title=j.title||j.company;
  const sub=[j.company,j.location].filter(Boolean).join(" \u00b7 ");
  const geo=jobGeo(j), oLangCode=j.language||j.language_guess, oLang=oLangCode?langOf(oLangCode):null;
  const subPlain=[j.company,geo.full||j.location,oLang?t("Written in {lang}",{lang:oLang.native}):""].filter(Boolean).join(" \u00b7 ");
  $("#jinsp-sub").title=subPlain;
  $("#jinsp-sub").innerHTML=[
    j.company?'<span>'+esc(j.company)+'</span>':"",
    (geo.full||j.location)?'<span class="ap-loc">'+(geo.flag||"")+esc(geo.full||j.location)+'</span>':"",
    oLang?'<span class="ap-olang" title="'+esc(t("Written in {lang}",{lang:oLang.native}))+'">'+
      '<i>'+esc(oLang.code.toUpperCase())+'</i>'+esc(oLang.native)+'</span>':""
  ].filter(Boolean).join('<span class="sep" aria-hidden="true">\u00b7</span>');
  $("#jinsp-logo").innerHTML=companyMark(j);
  const sTone=statusTone(j.status);
  $("#jinsp-status").className="ap-pill "+sTone;
  $("#jinsp-status").innerHTML='<span class="dot '+sTone+'"></span>'+esc(prettyStatus(j.status));

  const rows=peekRows(), at=rows.findIndex(x=>x.id===j.id);
  $("#jpk-idx").textContent=at<0?"":(at+1)+" of "+rows.length;
  $("#jpk-prev").disabled=at<=0;
  $("#jpk-next").disabled=at<0||at>=rows.length-1;

  const field=([k,label,kind])=>{
    let ctl;
    if(kind==="status") ctl='<span class="statusctl"><span class="dot '+statusTone(j.status)+'"></span><select data-j="status" aria-label="'+esc(label)+'">'+
      statusOptions(j.status)+'</select></span>';
    else if(kind==="lang"){
      /* Stored once said; until then a guess from the posting, offered. */
      const guess=j.language_guess, cur=j.language||"";
      ctl='<select data-j="language" aria-label="'+esc(label)+'">'+
        '<option value=""'+(cur?"":" selected")+'>'+(guess?"Looks like "+
          esc(langOf(guess).native):"Not set")+'</option>'+
        ((S.state&&S.state.languages)||[]).map(l=>'<option value="'+l.code+'"'+
          (l.code===cur?" selected":"")+'>'+esc(l.native)+'</option>').join("")+'</select>';
    }
    else ctl='<input data-j="'+k+'" aria-label="'+esc(label)+'"'+(kind==="date"?' type="date"':"")+
      (kind==="number"?' type="number" class="mono"':"")+' value="'+
      esc(j[k]==null?"":j[k])+'">';
    return '<label>'+esc(label)+'</label>'+ctl;
  };
  const more=JOB_MORE.map(field).join("");

  const docRow=(label,key,group)=>{
    const linked=j[key];
    return '<div class="drow"><select data-j="'+key+'" class="'+(linked?"":"empty")+'" aria-label="'+esc(label)+'">'+
      '<option value="">'+(key==="cv_path"?"no CV yet":"no cover letter")+'</option>'+
      ((S.state&&S.state.documents||[]).filter(d=>d.group===group).map(d=>
        '<option value="'+esc(d.path)+'"'+(d.path===linked?" selected":"")+'>'+
        esc(docTitle(d))+'</option>').join(""))+'</select>'+
      (linked?'<button class="alink" data-open-doc="'+esc(linked)+'">Open</button>':"")+
      '</div>';
  };

  /* One row of facts, the documents as pages, and the posting beside them,
     read as a posting. The fields set once when the application was made are
     folded away, with the delete under them. */
  const fact=(label,ctl)=>'<div class="ap-fact'+(label==="Interview"?" wide":label==="Language"?" lang":"")+
    '"><span>'+t(label)+'</span>'+ctl+'</div>';
  const statusCtl='<span class="statusctl"><span class="dot '+statusTone(j.status)+'"></span>'+
    '<select data-j="status" aria-label="Status">'+statusOptions(j.status)+'</select></span>';
  const guess=j.language_guess, curL=j.language||"";
  const langCtl='<span class="ap-lang">'+flag(curL||guess)+'<select data-j="language" aria-label="Language">'+
    '<option value=""'+(curL?"":" selected")+'>'+(guess?"Looks like "+esc(langOf(guess).native):"Not set")+'</option>'+
    ((S.state&&S.state.languages)||[]).map(l=>'<option value="'+l.code+'"'+(l.code===curL?" selected":"")+'>'+
      esc(l.native)+'</option>').join("")+'</select></span>';
  /* The match, read out: the ring, the band, the five ratings with why,
     what is strong and what is missing. Or how to get one. */
  const aiOn=(S.ai||[]).some(c=>c.state==="connected");
  let matchCard;
  if(hasMatch(j)){
    const m=j.match, b=matchBand(m.score), stale=matchStaleWords(m);
    const crit=(m.criteria||[]).map(c=>'<li><span class="mc-k">'+esc(t(MATCH_CRIT[c.key]||c.key))+'</span>'+
      '<span class="mc-bar" role="img" aria-label="'+esc(t("{n} of 5",{n:c.rating}))+'">'+[1,2,3,4,5].map(n=>'<i'+(n<=c.rating?' class="on"':'')+'></i>').join("")+'</span>'+
      '<span class="mc-why">'+esc(c.why||"")+'</span></li>').join("");
    const list=(title,xs,cls)=>xs&&xs.length?'<div class="mc-list '+cls+'"><small>'+esc(t(title))+'</small><ul>'+
      xs.map(x=>'<li>'+esc(x)+'</li>').join("")+'</ul></div>':'';
    const by=clientName({by:m.by||"ai"});
    matchCard='<section class="ap-match '+bandCls(b[0])+(stale?' stale':'')+'" aria-label="'+esc(t("Match"))+'">'+
      '<div class="mc-head">'+matchRing(m,"big")+'<div class="mc-tx"><b>'+esc(t(b[3]))+'</b>'+
        '<span>'+esc(t("{n} out of 10 against your base CV",{n:matchNum(m.score)}))+'</span></div></div>'+
      (m.summary?'<p class="mc-sum">'+esc(m.summary)+'</p>':'')+
      (stale?'<p class="mc-stale">'+esc(stale)+' <code data-noi18n>'+esc(matchAsk(j))+'</code><button type="button" class="linkbtn" data-match-copy>'+esc(t("Copy"))+'</button></p>':'')+
      '<ul class="mc-crit">'+crit+'</ul>'+
      '<div class="mc-lists">'+list("Strengths",m.strengths,"ok")+list("Gaps",m.gaps,"gap")+'</div>'+
      '<div class="mc-foot">'+esc(t("Scored by {who} · {d}",{who:by,d:m.at?fmtKey(String(m.at).slice(0,10),{day:"numeric",month:"short"}):""}))+'</div>'+
    '</section>';
  }else{
    matchCard='<section class="ap-match none" aria-label="'+esc(t("Match"))+'">'+
      '<div class="mc-head"><span class="mring none big"><b>?</b></span><div class="mc-tx"><b>'+esc(t("Not scored yet"))+'</b>'+
      '<span>'+esc(j.description?t("Your AI client can score how well your base CV matches this posting, out of 10."):
        t("Save the posting first: the score is read from it and your base CV."))+'</span></div></div>'+
      (aiOn?'<p class="mc-ask"><code data-noi18n>'+esc(matchAsk(j))+'</code><button type="button" class="obtn" data-match-copy>'+esc(t("Copy"))+'</button></p>'
        :'<p class="mc-ask"><button type="button" class="linkbtn" id="ap-match-ai">'+esc(t("Connect an AI client to score it"))+'</button></p>')+
    '</section>';
  }
  const facts='<div class="ap-facts">'+
    fact("Status",statusCtl)+
    fact("Follow-up",'<input data-j="followup_date" type="date" aria-label="Follow-up" value="'+esc(j.followup_date||"")+'">')+
    fact("Found on",'<button class="ap-src" id="ap-src" aria-haspopup="listbox" title="'+esc(j.source||"")+'">'+sourceMark(j)+
      '<span class="caret"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></span></button>')+
    fact("Language",langCtl)+'</div>';

  const cvName=j.cv_path?j.cv_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
  const ltName=j.letter_path?j.letter_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
  const tailoring=S.tailoring&&S.tailoring.has(j.id);
  /* A draft is where a CV is tailored; once the application is sent, the CV
     is the one they have, and the question is which one that was. */
  const sent=j.status!=="pending";
  const cvOpts=((S.state&&S.state.documents)||[]).filter(d=>d.group==="My CVs");
  const cvCard=j.cv_path
    ? '<div class="ap-doc"><div class="pg" data-open-doc="'+esc(j.cv_path)+'" data-thumb="'+esc(j.cv_path)+
        '" role="button" tabindex="0" aria-label="Open the CV"><span>Rendering…</span></div>'+
      '<div class="t"><b>'+esc(sent?t("The CV you sent"):t("CV"))+rvPill(docByPath(j.cv_path))+'</b><span>'+esc(cvName)+'</span></div>'+
      '<div class="a"><button class="obtn" data-open-doc="'+esc(j.cv_path)+'">Open</button>'+
        '<button class="obtn" id="job-ats" title="ATS check: what an applicant tracking system reads">ATS</button></div></div>'
    : sent
    ? '<div class="ap-doc"><div class="pg none"><label for="ap-cv-link">'+esc(t("Which CV did you send?"))+'</label>'+
        '<select id="ap-cv-link"><option value="">'+esc(t("Choose…"))+'</option>'+
        cvOpts.map(d=>'<option value="'+esc(d.path)+'">'+esc(docTitle(d))+'</option>').join("")+'</select></div>'+
      '<div class="t"><b>'+esc(t("The CV you sent"))+'</b><span>'+esc(t("Linked, it is there to re-read before an interview"))+'</span></div></div>'
    : '<div class="ap-doc"><div class="pg none"><span>No CV for this one yet</span>'+
        '<button class="obtn" data-tailor-here'+(tailoring?" disabled":"")+'>'+(tailoring?"Tailoring…":"Tailor a CV")+'</button></div>'+
      '<div class="t"><b>CV</b><span>Copied from your base CV</span></div></div>';
  const ltCard=j.letter_path
    ? '<div class="ap-doc"><div class="pg" data-open-doc="'+esc(j.letter_path)+'" data-thumb="'+esc(j.letter_path)+
        '" role="button" tabindex="0" aria-label="Open the cover letter"><span>Rendering…</span></div>'+
      '<div class="t"><b>Cover letter'+rvPill(docByPath(j.letter_path))+'</b><span>'+esc(ltName)+'</span></div>'+
      '<div class="a"><button class="obtn" data-open-doc="'+esc(j.letter_path)+'">Open</button></div></div>'
    : '<div class="ap-doc"><div class="pg none"><span>No cover letter</span>'+
        '<button class="obtn" id="ap-write">Write one</button></div>'+
      '<div class="t"><b>Cover letter</b><span>In the look of the CV</span></div></div>';
  const jb=jobBoard(j), words=(String(j.description||"").match(/\S+/g)||[]).length;
  /* The posting is not a document you send, so it is not a tile beside
     them: it is one card on the right, its link and its text together. */
  /* A board with a website is named; a referral or a recruiter is not a
     place, so the link says where it goes instead. */
  let site=""; try{ site=new URL(j.url).hostname.replace(/^www\./,"") }catch(e){}
  const web=jb&&jb.host?jb:null, who=web?web.label:site;

  const hist=(j.status_history||[]);
  const timeline=hist.length?'<div class="tl">'+hist.map(h=>
    '<div class="tli"><div class="spine"><i></i><u></u></div>'+
    '<div class="ev"><span>'+esc(prettyStatus(h.status))+'</span>'+
    '<span class="when mono">'+esc(shortDate(h.at))+'</span></div></div>').join("")+'</div>'
    :'<p class="note muted">No history yet.</p>';
  const chips=postingChips(j);
  const openLink=j.url
    ? '<a class="obtn" href="'+esc(j.url)+'" target="_blank" rel="noreferrer" title="'+esc(j.url)+'">'+
        (web?boardMark(web):'')+esc(who?t("Open on {site}",{site:who}):t("Open the posting"))+'<span class="ext">↗</span></a>'
    : '<button class="obtn" id="ap-addlink">'+t("Add the link")+'</button>';
  const pasteBox=(val,first)=>'<label class="ap-plabel" for="ap-paste">'+
      t(first?"Paste it here while it is up":"The posting, as text")+'</label>'+
    '<textarea class="posting-edit" id="ap-paste" placeholder="'+
      esc(t("Paste the posting. Headings and lists come through: a short line ending in a colon, or lines starting with -."))+'">'+
      esc(val||"")+'</textarea>'+
    '<div class="ap-pfoot">'+(first?'<span>'+t("Or ask your AI client to save it:")+' <code>'+
      esc(t("save the posting for {co}",{co:j.company}))+'</code></span>':'<span></span>')+
      '<span class="grow"></span>'+(first?'':'<button class="obtn" id="ap-post-cancel">'+t("Cancel")+'</button>')+
      '<button class="pbtn" id="ap-post-save">'+t(first?"Save the posting":"Save")+'</button></div>';
  const posting='<article class="ap-pcard" aria-label="'+esc(t("The posting"))+'">'+
    '<div class="ap-phead"><div class="tx"><b>'+t("The posting")+'</b><span>'+
      esc(words?t("{n} words · kept here in case the advert comes down",{n:words})
        :t("Not saved yet · keep a copy before the advert comes down"))+
      '</span></div><div class="ap-pacts">'+openLink+
      (words?'<button class="obtn" id="ap-post-edit">'+t("Edit")+'</button>':'')+'</div></div>'+
    '<div class="ap-pbody" id="ap-pbody">'+(words
      ? (chips.length?'<div class="ap-chips">'+chips.map(c=>'<span>'+esc(c)+'</span>').join("")+'</div>':'')+
        '<div class="ap-post" id="ap-post">'+postingHTML(j.description)+'</div>'
        /* posting expanded directly */
      : pasteBox("",true))+'</div></article>';

  body.innerHTML=
    '<div id="ap-prep" hidden></div>'+
    '<div class="peek-grid">'+
      '<div class="col">'+
        facts+matchCard+
        '<div class="block"><div class="bhead"><span class="blabel">Documents</span>'+
          (j.cv_path||j.letter_path?'<button class="obtn" id="ap-pack" data-job="'+esc(j.id)+'">'+
            (j.cv_path&&j.letter_path?"Export both…":"Export…")+'</button>':'')+
          '</div><div class="ap-docs">'+cvCard+ltCard+'</div></div>'+
        (j.cv_path?'<div class="block" id="jdiff-block" hidden><div class="bhead"><span class="blabel">'+
          'Changed from the base</span></div><div class="bdiff" id="jdiff" data-path="'+
          esc(j.cv_path)+'"></div></div>':'')+
        '<div class="block"><div class="bhead"><span class="blabel">History</span>'+
          '<button class="linkbtn" id="ap-versions" data-job="'+esc(j.id)+'" data-name="'+esc(j.company+", "+j.title)+'">'+
          esc(t("Every version"))+'</button></div>'+timeline+'</div>'+
        '<details class="fold"><summary>Company, role and the rest</summary>'+
          '<div class="fg2" style="margin-top:11px">'+more+'</div>'+
          '<div class="card" style="margin-top:12px">'+docRow("CV","cv_path","My CVs")+
            docRow("Cover letter","letter_path","Cover letters")+'</div></details>'+
        '<div class="block ruled foot-del"><button class="sbtn danger" id="job-del">'+
          'Delete this application</button></div>'+
      '</div>'+
      '<div class="col">'+
        roundsHTML(j)+
        peopleHTML(j)+
        '<div class="block"><div class="bhead"><span class="blabel">Notes</span></div>'+
          '<textarea data-j="notes" class="notes" aria-label="Notes">'+esc(j.notes||"")+'</textarea></div>'+
        '<div class="block grow">'+posting+'</div>'+
      '</div>'+
    '</div>';
  body.querySelectorAll("[data-thumb]").forEach(el=>apThumb(el,el.dataset.thumb));
  if(PREP.job!==j.id) PREP.data=null;
  drawPrep(j);
  $("#ap-src").onclick=e=>{ e.stopPropagation(); sourceMenu(j,$("#ap-src")) };
  body.querySelectorAll("[data-match-copy]").forEach(b=>b.onclick=()=>{
    try{ navigator.clipboard.writeText(matchAsk(j)).then(()=>toast(t("Copied. Paste it into your AI client."))) }catch(e){} });
  const mai=$("#ap-match-ai"); if(mai) mai.onclick=()=>openSettings("ai");
  const wr=$("#ap-write");
  if(wr) wr.onclick=async()=>{
    wr.disabled=true;
    try{ const r=await post("/api/letter/new",{job_id:j.id});
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      await loadJobs(true); openDoc(r.path) }
    catch(e){ wr.disabled=false; toast(e.message,true) }
  };
  body.querySelectorAll("[data-tailor-here]").forEach(th=>th.onclick=()=>tailorFor(j.id));
  const cvl=body.querySelector("#ap-cv-link");
  if(cvl) cvl.onchange=()=>{ if(cvl.value) saveJob(j.id,{cv_path:cvl.value}) };
  const wirePaste=()=>{
    const sv=$("#ap-post-save"), ta=$("#ap-paste");
    if(sv) sv.onclick=()=>{ const v=ta.value.trim(); if(!v&&!j.description) return ta.focus();
      saveJob(j.id,{description:v||null}) };
    const cn=$("#ap-post-cancel"); if(cn) cn.onclick=()=>selectJob(j.id);
  };
  wirePaste();
  const ed=$("#ap-post-edit");
  if(ed) ed.onclick=()=>{ ed.hidden=true; $("#ap-pbody").innerHTML=pasteBox(j.description,false);
    wirePaste(); $("#ap-paste").focus() };
  const al=$("#ap-addlink");
  if(al) al.onclick=async()=>{ const u=await askText({title:t("The link to the posting"),ok:t("Save link"),
      input:{value:"https://",placeholder:"https://",check:v=>/^https?:\/\/\S+\.\S+/.test(v)?"":t("That does not look like a web address.")}});
    if(u) saveJob(j.id,{url:u}) };
  const diff=$("#jdiff");
  if(diff) fillBaseDiff(diff,j.cv_path);
  const ab=$("#job-ats");
  if(ab) ab.onclick=()=>atsSheet(j.cv_path,j.description?j.id:"");
  body.querySelectorAll("[data-j]").forEach(el=>{
    el.onchange=()=>{
      let v=el.value;
      if(el.type==="number") v=v===""?null:Number(v);
      saveJob(j.id,{[el.dataset.j]:v===""?null:v});
    };
  });
  wireRounds(j);
  body.querySelectorAll("[data-open-doc]").forEach(b=>b.onclick=async ()=>{
    if(!await leaveEdits()) return;
    openDoc(b.dataset.openDoc);
  });
  /* No "are you sure": it goes to the trash, and the toast can bring it back. */
  $("#job-del").onclick=async()=>{
    try{
      await post("/api/jobs/delete",{id:j.id});
      S.jsel=null; await loadJobs();
      toast(t("Deleted {what}",{what:j.title+" · "+j.company}),false,{label:t("Undo"),fn:async()=>{
        try{ await post("/api/jobs/restore",{id:j.id}); await loadJobs(); selectJob(j.id); toast(t("Restored")) }
        catch(e){ toast(e.message,true) } }});
    }catch(e){ toast(e.message,true) }
  };
}
$("#jpk-prev").onclick=()=>peekStep(-1);
$("#jpk-next").onclick=()=>peekStep(1);
$("#jpk-close").onclick=closePeek;

/* Arrow keys walk the list with the peek open, which is the whole point of it
   being a peek rather than a page: you can read every application in the
   funnel without ever closing anything. They stay out of the way of a field
   being typed into, and of the select and date inputs, which use the arrows
   themselves. */
document.addEventListener("keydown",e=>{
  if(e.defaultPrevented||S.view!=="jobs"||$("#jpeek").hidden) return;
  if(!$("#sheet").hidden||!$("#ovl-settings").hidden||!$("#ovl-design").hidden) return;
  if(e.key==="Escape"){ e.preventDefault(); return closePeek() }
  if(e.key!=="ArrowUp"&&e.key!=="ArrowDown") return;
  const el=document.activeElement;
  if(el&&el.closest("#jinsp-body")&&
     /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
  e.preventDefault();
  peekStep(e.key==="ArrowDown"?1:-1);
});

/* ---- interview rounds ------------------------------------------------------
   An application's interviews, one row per round, in the order they happen.
   The next one not decided yet is open to edit; the server makes its time the
   application's interview time, which is what the calendar, the reminders and
   an AI client read. */
const ROUND_KINDS=["Recruiter screen","Technical","System design","Take-home","Team fit","Final"];
const roundJob=(j,r)=>({interview_at:r.at?r.at+":00":null,interview_tz:r.tz||null,location:j.location});
function roundWhen(j,r){
  if(!r.at) return t("Not scheduled yet");
  return interviewLine(roundJob(j,r));
}
function roundDue(j,r){
  if(r.outcome==="passed") return ['ok',t("Passed")];
  if(r.outcome==="failed") return ['bad',t("Didn't pass")];
  const at=r.at&&interviewMoment(roundJob(j,r)); if(!at) return ['',''];
  const days=dayDiff(todayKey(),dayIn(at));
  if(at<new Date()) return ['ask',t("How did it go?")];
  return ['soon',days<=0?t("today"):days===1?t("tomorrow"):t("in {n} days",{n:days})];
}
function roundsHTML(j){
  const rs=j.rounds||[], mine=userTz();
  /* The one you opened, else the next not decided; "__none" when you folded it. */
  const open=S.roundOpen==="__none"?null:S.roundOpen&&rs.some(r=>r.id===S.roundOpen)?S.roundOpen:(rs.find(r=>!r.outcome)||{}).id;
  const passed=rs.filter(r=>r.outcome==="passed").length, coming=rs.filter(r=>!r.outcome&&r.at).length,
    unset=rs.filter(r=>!r.outcome&&!r.at).length;
  /* The parts add up to the total, so the total is not said. */
  const failed=rs.filter(r=>r.outcome==="failed").length;
  const sum=rs.length?[passed?t("{n} passed",{n:passed}):"",failed?t("{n} not passed",{n:failed}):"",
    coming?t("{n} coming",{n:coming}):"",unset?t("{n} to schedule",{n:unset}):""].filter(Boolean).join(" · "):t("None yet");
  const people=(j.people||[]).filter(p=>p.name);
  const row=(r,i)=>{
    const [tone,label]=roundDue(j,r), isOpen=r.id===open;
    const mark=r.outcome==="passed"?"✓":r.outcome==="failed"?"×":String(i+1);
    let h='<div class="rd'+(isOpen?" open":"")+'" data-rd="'+esc(r.id)+'">'+
      '<button type="button" class="rd-hd" data-rd-toggle="'+esc(r.id)+'" aria-expanded="'+isOpen+'">'+
        '<span class="rd-n '+(r.outcome||(r.at?"":"unset"))+'" aria-hidden="true">'+mark+'</span>'+
        '<span class="rd-tx"><b>'+esc(r.kind?t(r.kind):t("Interview"))+
          (r.with?' <span>· '+esc(t("with {n}",{n:r.with}))+'</span>':'')+'</b>'+
          '<small>'+esc(roundWhen(j,r))+
            (S.roundSaved&&S.roundSaved.id===r.id&&Date.now()-S.roundSaved.at<2500?' <span class="rd-saved" role="status">✓ '+esc(t("Saved"))+'</span>':'')+'</small>'+
          /* Filled in by the prep card once it knows how ready you are. */
          (!r.outcome&&r.at?'<small class="rd-ready" data-rd-ready="'+esc(r.id)+'" hidden></small>':'')+'</span>'+
        (label?'<span class="rd-due '+tone+'">'+esc(label)+'</span>':'')+'</button>';
    if(isOpen){
      const zone=r.tz||"", guess=guessTz(j);
      h+='<div class="rd-ed">'+
        '<label>'+esc(t("Kind"))+'<input data-rf="kind" list="rd-kinds" value="'+esc(r.kind?t(r.kind):"")+'" placeholder="'+esc(t("Technical, team fit…"))+'"></label>'+
        '<label>'+esc(t("With"))+'<input data-rf="with" list="rd-people" value="'+esc(r.with)+'" placeholder="'+esc(t("Who you will meet"))+'"></label>'+
        /* Day and time apart: one field for both saved nothing until both
           were filled, and said nothing about it, so a day on its own was
           lost. A day now saves at once, at 10:00 until a time is given. */
        '<label>'+esc(t("When"))+'<span class="rd-when"><input type="date" data-rf="day" aria-label="'+esc(t("Day of the interview"))+'" value="'+esc((r.at||"").slice(0,10))+'">'+
          '<input type="time" data-rf="time" aria-label="'+esc(t("Time of the interview"))+'" value="'+esc((r.at||"").slice(11,16))+'">'+
          '<select id="rd-tz" data-rf="tz" aria-label="'+esc(t("Time zone of the interview"))+'"><option value=""'+(zone?"":" selected")+'>'+
          esc(t("Your time"))+' · '+esc(tzCity(mine))+'</option>'+tzOptions(zone,guess&&guess!==mine?guess:null)+'</select></span></label>'+
        '<div class="rd-acts">'+
          (r.at&&tone!=="ok"&&tone!=="bad"?'<button type="button" class="obtn" data-rd-ics>'+esc(t("Add to calendar"))+'</button>':'')+
          '<span class="grow"></span>'+
          '<button type="button" class="obtn" data-rd-out="passed" aria-pressed="'+(r.outcome==="passed")+'">'+esc(t("Passed"))+'</button>'+
          '<button type="button" class="obtn" data-rd-out="failed" aria-pressed="'+(r.outcome==="failed")+'">'+esc(t("Didn't pass"))+'</button>'+
          '<button type="button" class="obtn icon" data-rd-del aria-label="'+esc(t("Remove this round"))+'" title="'+esc(t("Remove this round"))+'">'+
            '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>'+
        '</div></div>';
    }
    return h+'</div>';
  };
  const last=rs.filter(r=>r.outcome).slice(-1)[0];
  const offer=last&&last.outcome==="failed"&&j.status==="interviewing";
  /* No "tailor a CV" here: by the time there is an interview the CV went out
     with the application. Which one it was is asked on the CV card. */
  return '<div class="block rounds" id="ap-rounds">'+
    '<div class="bhead"><span class="blabel">'+esc(t("Interviews"))+'</span>'+
      '<span class="rd-sum">'+esc(sum)+'</span><span class="grow"></span>'+
      '<button class="obtn" data-rd-add>'+esc(t("+ Add a round"))+'</button></div>'+
    (rs.length?'<div class="rd-list">'+rs.map(row).join("")+'</div>':'')+
    (offer?'<div class="rd-note">'+esc(t("That round did not go through."))+' <button type="button" class="linkbtn" data-rd-reject>'+
      esc(t("Mark the application Rejected after interview"))+'</button></div>':'')+
    '<datalist id="rd-kinds">'+ROUND_KINDS.map(k=>'<option value="'+esc(t(k))+'">').join("")+'</datalist>'+
    '<datalist id="rd-people">'+people.map(p=>'<option value="'+esc(p.name)+'">').join("")+'</datalist></div>';
}
function wireRounds(j){
  const box=$("#ap-rounds"); if(!box) return;
  const rs=()=>(j.rounds||[]).map(r=>Object.assign({},r));
  const save=(list,extra)=>{
    const patch=Object.assign({rounds:list},extra||{});
    /* Reminders are off until asked for; the first interview time is the
       moment to offer them, once. */
    if(list.some(r=>r.at)&&!notifyOn()&&!prefs().notify_offered&&notifyApi()){
      setPref("notify_offered",true);
      toast(t("Want a reminder before your interviews?"),false,{label:t("Turn on"),fn:async()=>{
        const A=notifyApi(); let g=await A.granted().catch(()=>false);
        if(!g) g=await A.ask().catch(()=>false);
        if(!g) return toast(t("Notifications are blocked for CV Studio in your system settings."),true);
        setPref("notify",true); notifyTick(); toast(t("Reminders on: 1 hour and 10 minutes before each interview."))
      }});
    }
    /* A first time for an application still waiting moves it to interviewing,
       and says so: a status changing on its own was news to nobody. */
    const was=j.status;
    if(list.some(r=>r.at)&&(j.status==="applied"||j.status==="pending")) patch.status="interviewing";
    return saveJob(j.id,patch).then(()=>{ if(patch.status&&patch.status!==was)
      toast(t("{co} is now {s}",{co:j.company,s:prettyStatus(patch.status)}),false,
        {label:t("Undo"),fn:()=>saveJob(j.id,{status:was})}) });
  };
  /* Kinds are stored in English, so they read in any language. */
  const kindKey=v=>ROUND_KINDS.find(k=>t(k)===v||k===v)||v;
  box.querySelectorAll("[data-rd-toggle]").forEach(b=>b.onclick=()=>{
    S.roundOpen=S.roundOpen===b.dataset.rdToggle?"__none":b.dataset.rdToggle; drawJobInspector() });
  box.querySelectorAll(".rd.open").forEach(el=>{
    const id=el.dataset.rd, edit=(f)=>{ const list=rs(), r=list.find(x=>x.id===id); f(r);
      S.roundSaved={id,at:Date.now()}; save(list);
      setTimeout(()=>{ const m=$("#ap-rounds .rd-saved"); if(m) m.remove() },2600) };
    el.querySelectorAll("[data-rf]").forEach(inp=>inp.onchange=()=>edit(r=>{
      const k=inp.dataset.rf; let v=inp.value.trim();
      if(k==="day"||k==="time"){
        const day=el.querySelector('[data-rf="day"]').value, tm=el.querySelector('[data-rf="time"]').value;
        v=day?day+"T"+(tm||"10:00"):"";
        /* A first time in a place elsewhere starts in that place's zone. */
        if(v&&!r.at&&!r.tz){ const g=guessTz(j); if(g&&g!==userTz()) r.tz=g }
        r.at=v; return;
      }
      if(k==="kind") v=kindKey(v);
      r[k]=v }));
    el.querySelectorAll("[data-rd-out]").forEach(b=>b.onclick=()=>edit(r=>{
      r.outcome=r.outcome===b.dataset.rdOut?"":b.dataset.rdOut;
      if(r.outcome) S.roundOpen=null }));
    const ics=el.querySelector("[data-rd-ics]");
    if(ics) ics.onclick=()=>download("/api/calendar.ics?id="+encodeURIComponent(j.id)+tok(),"interview.ics");
    el.querySelector("[data-rd-del]").onclick=()=>{ const prev=rs(); S.roundOpen=null;
      save(prev.filter(x=>x.id!==id)).then(()=>toast(t("Round removed"),false,{label:t("Undo"),fn:()=>save(prev)})) };
  });
  box.querySelector("[data-rd-add]").onclick=()=>{
    const list=rs(), id="r"+Math.random().toString(36).slice(2,8);
    list.push({id,kind:"",at:"",tz:"",with:"",outcome:"",note:""});
    S.roundOpen=id; save(list);
  };
  const rj=box.querySelector("[data-rd-reject]");
  if(rj) rj.onclick=()=>saveJob(j.id,{status:"rejected_interviewing"});
}

const byRecent=()=>S.jobs.sort((a,b)=>String(b.updated_at).localeCompare(String(a.updated_at)));
async function saveJob(id,patch){
  try{
    const updated=await post("/api/jobs/update",Object.assign({id},patch));
    const i=S.jobs.findIndex(x=>x.id===id);
    if(i>=0) S.jobs[i]=updated;
    /* Sorting is by updated_at, so an edit moves the row -- but not while
       that application is open: the list reordering under it made "9 of 66"
       jump to "3 of 66" and the arrows walk somewhere else. It settles when
       the application is closed. */
    if(!S.jsel) byRecent();
    drawJobs();
    if(S.view==="cvs") buildInspector();
    S.funnel=null;
  }catch(e){ toast(e.message,true) }
}

/* ---- new job sheet -------------------------------------------------------- */
function newJobSheet(seed){
  seed=seed||{};
  const docs=g=>(S.state&&S.state.documents||[]).filter(d=>d.group===g);
  const req='<span class="req" aria-hidden="true">*</span>';
  const err=id=>'<span class="nj-err" id="'+id+'-err" role="alert"></span>';
  /* What it takes to add one is two fields; the rest waits under "More
     details" rather than making a first entry look like a tax form. Errors
     are said beside the field they are about, not in a toast that covers
     the form and goes away. */
  openSheet(
    '<div><h3 id="sheet-title">'+esc(t("New application"))+'</h3><p>'+esc(t("Only the company and the role are required. Everything else can come later."))+'</p></div>'+
    '<div class="fg w88 nj">'+
      '<label for="nj-company">'+esc(t("Company"))+req+'</label><input id="nj-company" autocomplete="off" aria-required="true" aria-describedby="nj-company-err" value="'+esc(seed.company||"")+'">'+err("nj-company")+
      '<label for="nj-title">'+esc(t("Role"))+req+'</label><input id="nj-title" autocomplete="off" aria-required="true" aria-describedby="nj-title-err" value="'+esc(seed.title||"")+'">'+err("nj-title")+
      '<label for="nj-url">'+esc(t("Link"))+'</label><input id="nj-url" autocomplete="off" placeholder="https://" aria-describedby="nj-url-err" value="'+esc(seed.url||"")+'">'+err("nj-url")+
      '<label for="nj-status">'+esc(t("Status"))+'</label><select id="nj-status">'+S.statuses.map(s=>
        '<option value="'+s+'">'+esc(prettyStatus(s))+'</option>').join("")+'</select>'+
    '</div>'+
    '<details class="nj-more"><summary>'+esc(t("More details"))+' <span>'+esc(t("location, where you found it, salary, follow-up, documents, notes"))+'</span></summary>'+
    '<div class="fg w88 nj">'+
      '<label for="nj-location">'+esc(t("Location"))+'</label><input id="nj-location" autocomplete="off">'+
      /* The same places the application's own Found on menu offers. */
      '<label>'+esc(t("Found on"))+'</label><button type="button" class="ap-src nj-src" id="nj-source" data-v="'+esc(seed.source||"")+'" aria-haspopup="listbox">'+
        sourceMark({source:seed.source||""})+'</button>'+
      '<label for="nj-salary">'+esc(t("Salary"))+'</label><input id="nj-salary" type="number" min="0" class="mono" aria-describedby="nj-salary-err">'+err("nj-salary")+
      '<label for="nj-followup">'+esc(t("Follow-up"))+'</label><input id="nj-followup" type="date">'+
      '<label for="nj-cv">'+esc(t("CV"))+'</label><select id="nj-cv"><option value="">'+esc(t("Not linked"))+'</option>'+
        docs("My CVs").map(d=>'<option value="'+esc(d.path)+'"'+
          (d.path===seed.cv_path?" selected":"")+'>'+esc(docTitle(d))+'</option>').join("")+
        '</select>'+
      '<label for="nj-letter">'+esc(t("Cover letter"))+'</label><select id="nj-letter"><option value="">'+esc(t("Not linked"))+'</option>'+
        docs("Cover letters").map(d=>'<option value="'+esc(d.path)+'"'+(d.path===seed.letter_path?" selected":"")+'>'+esc(docTitle(d))+
          '</option>').join("")+'</select>'+
      '<label for="nj-notes">'+esc(t("Notes"))+'</label><textarea id="nj-notes" rows="3"></textarea>'+
    '</div></details>'+
    '<div class="nj-dup" id="nj-dup" role="alert" hidden></div>'+
    '<div class="foot"><button class="sbtn" data-cancel>'+esc(t("Cancel"))+'</button>'+
    '<button class="sbtn primary" id="nj-go">'+esc(t("Add application"))+'</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  const src=$("#nj-source");
  src.onclick=e=>{ e.stopPropagation(); sourceMenu({source:src.dataset.v},src,v=>{ src.dataset.v=v; src.innerHTML=sourceMark({source:v}) }) };
  const v=id=>$("#"+id).value.trim();
  const say=(id,msg)=>{ const f=$("#"+id), e=$("#"+id+"-err"); if(!f||!e) return;
    e.textContent=msg||""; if(msg) f.setAttribute("aria-invalid","true"); else f.removeAttribute("aria-invalid") };
  ["nj-company","nj-title","nj-url","nj-salary"].forEach(id=>$("#"+id).addEventListener("input",()=>{ say(id,""); dupOk=false; $("#nj-dup").hidden=true;
    $("#nj-go").textContent=t("Add application") }));
  let dupOk=false;
  const check=()=>{
    const bad=[];
    if(!v("nj-company")){ say("nj-company",t("Which company is it?")); bad.push("nj-company") }
    if(!v("nj-title")){ say("nj-title",t("Which role? The job title is enough.")); bad.push("nj-title") }
    if(v("nj-url")&&!/^https?:\/\/[^\s/]+\.[^\s]+$/i.test(v("nj-url"))){ say("nj-url",t("A web address, starting with https://")); bad.push("nj-url") }
    if(v("nj-salary")&&!(Number(v("nj-salary"))>=0)){ say("nj-salary",t("A number, without the currency.")); bad.push("nj-salary");
      $("#sheet .nj-more").open=true }
    if(bad.length){ $("#"+bad[0]).focus(); return false }
    /* The same role at the same company is usually the one already there. */
    const same=(S.jobs||[]).find(j=>j.company.trim().toLowerCase()===v("nj-company").toLowerCase()&&
      j.title.trim().toLowerCase()===v("nj-title").toLowerCase());
    if(same&&!dupOk){
      const d=$("#nj-dup"); d.hidden=false;
      d.innerHTML=esc(t("You already have {role} at {co} ({s}).",{role:same.title,co:same.company,s:prettyStatus(same.status)}))+
        ' <button type="button" class="linkbtn" id="nj-open">'+esc(t("Open it"))+'</button>';
      $("#nj-open").onclick=()=>{ closeSheet(); setView("jobs"); selectJob(same.id) };
      dupOk=true; $("#nj-go").textContent=t("Add it anyway"); return false;
    }
    return true;
  };
  const go=async()=>{
    if(!check()) return;
    const b=$("#nj-go"); b.disabled=true;
    try{
      const j=await post("/api/jobs",{
        company:v("nj-company"), title:v("nj-title"), status:$("#nj-status").value,
        location:v("nj-location")||null,
        source:$("#nj-source").dataset.v||null, url:v("nj-url")||null,
        salary_expected:v("nj-salary")?Number(v("nj-salary")):null,
        followup_date:v("nj-followup")||null, notes:v("nj-notes")||null,
        cv_path:$("#nj-cv").value||null, letter_path:$("#nj-letter").value||null});
      closeSheet(); await loadJobs(); S.funnel=null;
      setView("jobs"); selectJob(j.id); toast(t("Added {co}",{co:j.company}));
    }catch(e){ b.disabled=false; toast(e.message,true) }
  };
  $("#nj-go").onclick=go;
  /* Enter in any one-line field adds it. */
  $$("#sheet .nj input").forEach(i=>i.addEventListener("keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); go() } }));
  (seed.company?$("#nj-title"):$("#nj-company")).focus();
}
$("#btn-newjob").onclick=()=>newJobSheet();
$("#btn-newdoc").onclick=()=>newDocumentSheet();
/* Import makes a new CV from a file rather than overwriting one: the design is
   the base CV's, so it prints like everything else, and the base is left
   alone. What was read, and anything to check, is shown before it is made. */
$("#btn-importdoc").onclick=()=>{ $("#importdoc-file").value=""; $("#importdoc-file").click() };
$("#importdoc-file").onchange=async()=>{
  const file=$("#importdoc-file").files[0]; if(!file) return;
  const btn=$("#btn-importdoc"); btn.disabled=true; btn.textContent="Reading…";
  let imp;
  try{ imp=await importFile(file) }
  catch(e){ return toast(e.message,true) }
  finally{ btn.disabled=false; btn.innerHTML="Import&#8230;" }
  importSheet(imp);
};
function importSheet(imp){
  const b=S.state&&S.state.base, base=b&&!b.missing?b.path:null;
  const stem=slug(imp.cv.name&&imp.cv.name!=="Your Name"?imp.cv.name:
    file_stem(imp.name))||"imported";
  openSheet(
    '<div><h3 id="sheet-title">Import '+esc(imp.name)+'</h3><p>'+
      (base?'A new CV, in the base CV&#8217;s design. The base itself is not changed.'
           :'A new CV from what was read.')+'</p></div>'+
    '<ul class="imp-found">'+imp.found.map(f=>'<li><b>'+esc(f.what)+'</b><span>'+
      esc(String(f.count))+'</span></li>').join("")+'</ul>'+
    (imp.notes.length?'<div class="imp-notes"><b>To check</b><ul>'+imp.notes.map(n=>
      '<li>'+esc(n)+'</li>').join("")+'</ul></div>':'')+
    '<div class="fg w88"><label for="imp-name">Save as</label>'+
      '<input id="imp-name" class="mono" autocomplete="off" value="'+
        esc(uniqueDocName(stem+"-cv"))+'"></div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="imp-go">Create and open</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#imp-go").onclick=async()=>{
    const name=$("#imp-name").value.trim().replace(/\.ya?ml$/i,"");
    if(!name) return toast("Give it a name",true);
    $("#imp-go").disabled=true;
    try{
      const r=await post("/api/new",{name,kind:"cv",from:base});
      await post("/api/save",{path:r.path,patches:[{path:["cv"],value:imp.cv}]});
      closeSheet();
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      openDoc(r.path); toast(t("Imported {name}",{name:imp.name}));
    }catch(e){ $("#imp-go").disabled=false; toast(e.message,true) }
  };
  $("#imp-name").select();
}
const file_stem=n=>String(n||"").replace(/\.[^.]+$/,"");

