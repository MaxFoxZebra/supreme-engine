/* ATS check panel and first-run onboarding wizard. */
/* =========================================================================
   ATS check
   ========================================================================= */
/* What an applicant tracking system reads out of the PDF, beside the page it
   came from. The parsing checks read the real file; the keywords are a count
   of what the posting asks for and whether the CV says it, and are labelled
   as a heuristic because they are one. */
const ATS={path:null, job:null, paste:"", busy:false};
function atsJobs(){
  return (S.jobs||[]).filter(j=>j.description);
}
async function atsSheet(path,jobId){
  if(!path) return;
  if(!S.jready){ try{ await loadJobs(true) }catch(e){} }
  ATS.path=path; ATS.paste="";
  const linked=(S.jobs||[]).find(j=>j.cv_path===path);
  ATS.job=jobId||(linked&&linked.description?linked.id:"")||"";
  const withPosting=atsJobs();
  const name=path.split("/").pop().replace(/\.ya?ml$/,"");
  $("#sheet").classList.add("ats");
  openSheet(
    '<div class="ats-top"><div><h3 id="sheet-title">ATS check</h3>'+
      '<p>What an applicant tracking system reads from <b>'+esc(name)+'</b>’s PDF, '+
      'and which of a posting’s keywords it uses.</p></div>'+
      '<label class="ats-vs">Against <select id="ats-job">'+
        '<option value="">No posting</option>'+
        withPosting.map(j=>'<option value="'+esc(j.id)+'"'+(j.id===ATS.job?" selected":"")+'>'+
          esc(j.company)+' · '+esc(j.title)+'</option>').join("")+
        '<option value="__paste">Paste a posting…</option>'+
      '</select></label></div>'+
    '<textarea id="ats-paste" class="ats-paste" hidden placeholder="Paste the job posting here"></textarea>'+
    '<div class="ats-body" id="ats-body"><p class="sp-note">Reading the PDF…</p></div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Close</button>'+
      '<button class="sbtn" id="ats-run">Check again</button></div>',
    ()=>$("#sheet").classList.remove("ats"));
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#ats-job").onchange=e=>{
    const v=e.target.value, pasting=v==="__paste";
    $("#ats-paste").hidden=!pasting;
    if(pasting){ $("#ats-paste").focus(); return }
    ATS.job=v; ATS.paste=""; atsRun();
  };
  let t=null;
  $("#ats-paste").oninput=e=>{ clearTimeout(t); ATS.paste=e.target.value;
    t=setTimeout(()=>{ if(ATS.paste.trim().length>80) atsRun() },600) };
  $("#ats-run").onclick=()=>atsRun();
  atsRun();
}
async function atsRun(fix){
  if(ATS.busy) return;
  ATS.busy=true;
  const body=$("#ats-body"); if(!body){ ATS.busy=false; return }
  body.classList.add("busy");
  $("#ats-run").disabled=true;
  const req={path:ATS.path};
  if(ATS.paste.trim()) req.posting=ATS.paste;
  else if(ATS.job) req.job_id=ATS.job;
  /* "No posting" has to say so, or the server falls back to the application
     the CV is attached to. */
  else req.posting="";
  try{
    const r=await post(fix?"/api/ats/fix":"/api/ats",fix?Object.assign({fix},req):req);
    if($("#ats-body")) atsPaint(r);
    if(fix){
      toast("Changed the design. Check again to see it");
      if(S.path===ATS.path&&!S.dirty) openDoc(ATS.path);
      S.baseThumb=null; paintBase();
    }
  }catch(e){ if($("#ats-body")) body.innerHTML='<p class="note">'+esc(e.message)+'</p>' }
  ATS.busy=false;
  if($("#ats-body")){ body.classList.remove("busy"); $("#ats-run").disabled=false }
}
/* Private-use characters are what icon fonts extract as. Shown as a box, the
   way a parser that keeps them would print them, so the finding above can be
   seen in the text below. */
/* The PDF's lines are joined inside a paragraph, so the text reads as
   parsed rather than as wrapped on the page; bullets and headings keep
   their own lines. */
const atsReflow=t=>String(t||"").replace(/([^\n])\n(?=[a-zà-ÿ(,;])/g,"$1 ");
const atsReadable=t=>esc(atsReflow(t).replace(/ {2,}/g," ")).replace(/[-]/g,'<i class="pua" title="An icon, read as an unreadable character">□</i>');
function atsPaint(r){
  const body=$("#ats-body");
  if(!r.ok){ body.innerHTML='<p class="note">'+esc(r.error||"The check could not run.")+'</p>'; return }
  const kw=r.keywords, vs=r.against;
  let match;
  if(kw&&kw.total){
    const chip=(t,on)=>'<span class="kw'+(on?" on":"")+'">'+(on?'<i>✓</i>':'')+esc(t.term)+'</span>';
    match='<section class="ats-sec"><span class="blabel">Keywords from the posting</span>'+
      '<div class="ats-score"><b class="mono">'+kw.found.length+'</b><span>of '+kw.total+
        ' used in this CV'+(vs&&vs.company?' · '+esc(vs.company):'')+'</span></div>'+
      '<div class="meter"><i style="width:'+(kw.rate||0)+'%"></i></div>'+
      (kw.missing.length?'<div class="kwlabel">Not in the CV</div><div class="kws">'+
        kw.missing.map(t=>chip(t,false)).join("")+'</div>':'')+
      (kw.found.length?'<div class="kwlabel">In the CV</div><div class="kws">'+
        kw.found.map(t=>chip(t,true)).join("")+'</div>':'')+
      '<p class="sp-note">Picked out of the posting by how often, and where, it asks for '+
      'them: a prompt, not a verdict. Worth working in only where they are true of you, '+
      'in the words the posting uses.</p></section>';
  }else if(vs&&vs.company&&!vs.has_posting){
    match='<section class="ats-sec"><span class="blabel">Keywords from the posting</span>'+
      '<p class="note">'+esc(vs.company)+' has no saved posting. Paste one, or add it to the '+
      'application, to see which of its keywords this CV uses.</p></section>';
  }else{
    match='<section class="ats-sec"><span class="blabel">Keywords from the posting</span>'+
      '<p class="note">Choose an application with a saved posting, or paste one, to see '+
      'which of its keywords this CV uses.</p></section>';
  }
  const icon={ok:"✓",warn:"!",bad:"×"};
  const problems=r.checks.filter(c=>c.level!=="ok").length;
  const checks='<section class="ats-sec"><span class="blabel">How it parses'+
      '<em>'+(problems?problems+" to look at":"nothing to fix")+'</em></span>'+
    '<ul class="checks">'+r.checks.map(c=>'<li class="'+c.level+'"><i>'+icon[c.level]+'</i>'+
      '<div><b>'+esc(c.title)+'</b><span>'+esc(c.detail)+'</span></div>'+
      (c.fix?'<button class="obtn" data-fix="'+c.fix+'"'+
        (S.path===ATS.path&&S.dirty?' disabled title="Save your changes first"':'')+'>Fix</button>':'')+
      '</li>').join("")+'</ul></section>';
  /* With no posting to compare against, the parse comes first and the
     keywords are a line under it. */
  body.innerHTML='<div class="ats-main">'+(kw&&kw.total?match+checks:checks+match)+'</div>'+
    '<div class="ats-read"><span class="blabel">What it reads<em>'+r.pages+' page'+
      (r.pages===1?"":"s")+' · '+r.words+' words</em></span>'+
      '<pre class="mono">'+atsReadable(r.text)+'</pre></div>';
  $$("#ats-body [data-fix]").forEach(b=>b.onclick=()=>{ b.disabled=true; atsRun(b.dataset.fix) });
}
$("#btn-history").onclick=()=>{ if(S.path) historySheet({path:S.path},$("#doctitle .t").textContent) };
$("#lt-history").onclick=()=>{ if(LT.path) historySheet({path:LT.path},$("#lt-title").textContent) };
$("#btn-ats").onclick=()=>{
  if(!S.path) return;
  if(S.dirty) toast("Checking the saved file. Save to include your edits");
  atsSheet(S.path);
};

/* =========================================================================
   Setup
   ========================================================================= */
/* The whole window, on the first launch: a welcome, then four steps that each
   leave something real behind -- the CV you already had, the name at the top
   of the base CV, the theme it prints in, a client that can write to it.
   Nothing here is a tour of buttons, and every step can be skipped, because
   the defaults already render.

   It shows while the base is still the placeholder the app wrote, not only on
   the launch that wrote it: quitting halfway is not the same as having set up.
   Closing it counts as done, so it never nags; Settings brings it back. */
const OB_FIELDS=[["name","Name","Your Name"],["headline","Headline","Your Role"],
  ["location","Location","City, Country"],["email","Email","you@example.com"],
  ["phone","Phone","+33-6-12-34-56-78"]];
const OB_STEPS=[["Welcome","What this is"],["Start from","PDF, LinkedIn or blank"],
  ["You","The top of your CV"],["The page","Theme and paper"],["AI","Optional"]];
const OB={step:0, path:null, cv:{}, theme:null, size:null, png:null, busy:0, again:false,
  t:null, err:null, pages:null, src:"pdf", imp:null, importing:false, impErr:null,
  thumbs:null, thumbRun:0};

function shouldOnboard(d){
  return !prefs().onboarded&&!!(d.first_run||d.starter)&&!!(d.base&&!d.base.missing);
}
const onbOpen=()=>!$("#onb").hidden;
async function onboardingSheet(){
  const b=S.state&&S.state.base;
  if(!b||b.missing) return openSettings("workspace");
  Object.assign(OB,{step:0, path:b.path, png:null, cv:{}, err:null, pages:null,
    src:"pdf", imp:null, importing:false, impErr:null, thumbs:null});
  try{
    const doc=await api("/api/doc?path="+encodeURIComponent(b.path));
    const cv=(doc.data&&doc.data.cv)||{}, des=(doc.data&&doc.data.design)||{};
    OB.baseCv=cv; obFill(cv);
    OB.theme=des.theme||"engineeringclassic";
    OB.size=(des.page&&des.page.size)||"a4";
  }catch(e){ return toast(e.message,true) }
  closeOverlays(); if(!$("#sheet").hidden) closeSheet();
  $("#onb").hidden=false;
  obPaint();
}
/* A placeholder is not an answer, so it goes in as a hint instead. Only the
   starter's placeholders, though: what an import read is what you wrote. */
function obFill(cv,imported){
  OB_FIELDS.forEach(([k,,ph])=>{ const v=cv[k]==null?"":String(cv[k]);
    OB.cv[k]=!imported&&v===ph||v==="Your Name"?"":v });
}
/* Leaving part-way keeps what is on screen, the same as Next would have. */
function obClose(){
  const keep=OB.step>=2||(OB.step===1&&OB.imp);
  $("#onb").hidden=true; $("#onb").innerHTML=""; delete $("#onb").dataset.step;
  clearTimeout(OB.t); OB.thumbRun++;
  setPref("onboarded",true);
  if(keep&&OB.step!==-1)
    post("/api/save",{path:OB.path,patches:obPatches()})
      .then(obRefresh).catch(e=>toast(e.message,true));
}
async function obRefresh(){
  try{ const st=await api("/api/state"); S.state=st; renderDocs(st.documents) }catch(e){}
  S.baseThumb=null; paintBase();
}
/* The fields as patches, after the imported CV if there is one: the import is
   the body, the fields are what you corrected at the top of it. Blank means
   absent: RenderCV leaves a null out of the header, where an empty string
   would print a stray separator. */
function obContentPatches(){
  return (OB.imp?[{path:["cv"],value:OB.imp.cv}]:[]).concat(
    OB_FIELDS.map(([k])=>({path:["cv",k],value:OB.cv[k].trim()||(k==="name"?"Your Name":null)})));
}
function obPatches(){
  return obContentPatches().concat([{path:["design","theme"],value:OB.theme},
    {path:["design","page","size"],value:OB.size}]);
}
/* The page itself, rendered from the scratch copy the editor previews into, so
   nothing is written until Next. One at a time, because every preview shares
   that scratch file: a burst of typing is one render, and whatever changed
   while it ran is one more after it, not a second render racing the first. */
function obPreview(now){
  clearTimeout(OB.t);
  OB.t=setTimeout(async()=>{
    if(OB.busy){ OB.again=true; return }
    OB.busy=1; OB.again=false; obShot();
    try{
      const r=await post("/api/preview",{path:OB.path,patches:obPatches()});
      OB.png=r.ok&&r.pngs.length?r.pngs[0]:null; OB.err=r.ok?null:(r.hint||"It did not render.");
      OB.pages=r.ok?r.pages:null;
    }catch(e){ OB.png=null; OB.err=e.message }
    OB.busy=0;
    if(OB.again) obPreview(true); else { obShot(); obTile() }
  },now?0:350);
}
function obShot(){
  const el=$("#onb-shot"); if(!el) return;
  el.classList.toggle("busy",!!OB.busy);
  el.innerHTML=OB.png
    ? '<img alt="The first page of your CV" src="'+esc(OB.png+tok())+'">'
    : '<span>'+esc(OB.busy?"Rendering…":OB.err||"")+'</span>';
  const cap=$("#onb-cap"); if(!cap) return;
  cap.classList.toggle("busy",!!OB.busy);
  const what=OB.step===1&&OB.imp?"Imported from "+OB.imp.name:"Your page, rendered as you type";
  cap.innerHTML='<i></i>'+esc(OB.busy&&!OB.png?"Rendering…":what+(OB.pages?" · "+OB.pages+
    " page"+(OB.pages===1?"":"s")+" · "+themeLabel(OB.theme)+" · "+
    (OB.size==="a4"?"A4":"US Letter"):""));
}

/* The theme tiles are your CV in each theme, as on the Design screen: the
   current one is the live page, the rest render one at a time behind it. */
function obTile(){
  const t=OB.theme, b=$('#onb-themes [data-t="'+t+'"] .pic');
  if(b&&OB.png) b.innerHTML='<img alt="" src="'+esc(OB.png+tok())+'">';
}
async function obThumbs(){
  const run=++OB.thumbRun, key=JSON.stringify(obContentPatches());
  if(!OB.thumbs||OB.thumbs.key!==key) OB.thumbs={key:key,by:{}};
  for(const t of (S.state.themes||[])){
    if(run!==OB.thumbRun||OB.step!==3||!onbOpen()) return;
    if(OB.thumbs.by[t]) continue;
    try{
      const r=await post("/api/theme-preview",{path:OB.path,theme:t,patches:obContentPatches()});
      if(!r.ok||run!==OB.thumbRun) continue;
      OB.thumbs.by[t]=r;
      const b=$('#onb-themes [data-t="'+t+'"]');
      if(b&&t!==OB.theme) b.querySelector(".pic").innerHTML='<img alt="" src="'+esc(r.png+tok())+'">';
      if(b) b.querySelector(".nm em").textContent=r.pages+" page"+(r.pages===1?"":"s");
    }catch(e){ return }
  }
}

/* A PDF or a LinkedIn archive, read on this machine by /api/import: the file
   goes to the local server and no further. Resolves to what was read, or
   throws with a sentence to show. */
function importFile(file){
  return new Promise((ok,fail)=>{
    if(file.size>40*1024*1024) return fail(new Error("That file is over 40 MB."));
    const rd=new FileReader();
    rd.onerror=()=>fail(new Error("That file could not be read."));
    rd.onload=async()=>{
      try{
        const r=await post("/api/import",{name:file.name,
          data:String(rd.result).replace(/^data:[^,]*,/,"")});
        if(!r.ok) return fail(new Error(r.error||"Nothing could be read from that file."));
        ok({name:file.name,cv:r.cv,found:r.found||[],notes:r.notes||[],source:r.source});
      }catch(e){ fail(e) }
    };
    rd.readAsDataURL(file);
  });
}
async function obReadFile(file){
  if(!file) return;
  OB.importing=true; OB.impErr=null; obPaint();
  try{ OB.imp=await importFile(file) }catch(e){ return obImpFail(e.message) }
  obFill(OB.imp.cv,true); OB.importing=false; OB.png=null; OB.pages=null;
  if(onbOpen()){ obPaint(); obPreview(true) }
}
function obImpFail(msg){ OB.importing=false; OB.impErr=msg; if(onbOpen()) obPaint() }

const OB_ICON={
  folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  base:'<path d="M8 4h9l3 3v13H8z"/><path d="M4 8v12h11"/>',
  lock:'<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  up:'<path d="M12 15V3M7 8l5-5 5 5"/><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
  tick:'<path d="M5 12l5 5L20 7"/>',
  arrow:'<path d="M5 12h14M13 6l6 6-6 6"/>',
  spark:'<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/>'+
    '<path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
};
const obIcon=(k,s,w)=>'<svg width="'+s+'" height="'+s+'" viewBox="0 0 24 24" fill="none" '+
  'stroke="currentColor" stroke-width="'+(w||2)+'" aria-hidden="true">'+OB_ICON[k]+'</svg>';
/* The window has no frame of its own, so a screen that covers it all carries
   the lights and a strip to drag it by. */
function obBar(){
  return '<div class="onb-bar" data-tauri-drag-region>'+($("#lights").hidden?"":
    '<div class="lights"><button data-w="w-close" title="Close" aria-label="Close"></button>'+
    '<button data-w="w-min" title="Minimise" aria-label="Minimise"></button>'+
    '<button data-w="w-max" title="Maximise" aria-label="Maximise"></button></div>')+'</div>';
}

function obWelcome(){
  const st=S.state||{};
  const letters="CV Studio".split("").map((ch,i)=>'<span aria-hidden="true" style="animation-delay:'+
    (0.55+i*0.06).toFixed(2)+'s">'+esc(ch)+'</span>').join("");
  const facts=[
    ["folder","Your files, in a folder you own",'<span class="mono">'+esc(shortPath(st.workspace))+
      '</span>. Plain YAML you can open, copy and back up without this app. '+
      '<button class="linkish" id="onb-reveal">Open it</button>'],
    ["base","One base CV","Every CV you tailor for an application starts as a copy of it, "+
      "so it is worth two minutes now."],
    ["lock","Nothing leaves this machine","No account, no telemetry. A model only sees what "+
      "you connect it to."]];
  return '<div class="onb-hero">'+
    '<div class="onb-orbs" aria-hidden="true"><span></span><span></span><span></span><span></span></div>'+
    '<div class="onb-mark"><img src="/static/brand-mark-256.png" alt=""></div>'+
    '<h1 class="onb-word" id="onb-title" aria-label="Welcome to CV Studio">'+letters+'</h1>'+
    '<span class="onb-rule" aria-hidden="true"></span>'+
    '<p class="onb-lede">A CV tailored to every application, every follow-up on time, '+
      'and every interview in your calendar. Connect an AI client and it does the writing with you.</p>'+
    '<div class="onb-facts">'+facts.map(([ic,t,x],i)=>
      '<div class="onb-fact" style="animation-delay:'+(1.85+i*0.15).toFixed(2)+'s">'+
        '<span class="ic">'+obIcon(ic,17)+'</span><b>'+t+'</b><span>'+x+'</span></div>').join("")+
    '</div>'+
    '<div class="onb-go"><button class="onb-cta" id="onb-next">Get started '+
      obIcon("arrow",16,2.4)+'</button>'+
      '<button class="onb-skip" id="onb-skip">Skip setup, I\'ll look around first</button>'+
      '<div class="onb-or" aria-hidden="true"><span>or</span></div>'+
      '<button class="onb-demo" id="onb-demo"><span class="dm-ic">'+obIcon("spark",18,2)+'</span>'+
        '<span class="dm-tx"><b>Try it with sample data</b>'+
        '<small>66 made-up applications, CVs and letters. Your own folder stays untouched.</small></span>'+
        '<span class="dm-go">'+obIcon("arrow",15,2.4)+'</span></button></div>'+
    '<div class="onb-pips" aria-label="Step 1 of '+OB_STEPS.length+'">'+
      OB_STEPS.map((_,k)=>'<i'+(k===0?' class="on"':'')+'></i>').join("")+'</div>'+
  '</div>';
}

function obRail(){
  const i=OB.step;
  return '<aside class="onb-rail">'+
    '<div class="onb-brand"><img src="/static/brand-mark-256.png" alt="">CV Studio</div>'+
    '<p>Two minutes to set up your base CV. Every step can be skipped: the defaults '+
      'already render.</p>'+
    '<ol class="onb-steps" aria-label="Setup steps">'+OB_STEPS.map(([t,sub],k)=>
      '<li'+(k===i?' aria-current="step"':k<i?' class="done"':'')+'>'+
        '<span class="num"><b>'+(k<i?obIcon("tick",14,3):k+1)+'</b><i></i></span>'+
        '<span class="txt"><b>'+t+'</b><span>'+sub+'</span></span></li>').join("")+'</ol>'+
    '<div class="onb-later"><button id="onb-later">Finish later</button>'+
      '<span>What you have entered is kept. Settings → Workspace runs this again.</span></div>'+
  '</aside>';
}

const OB_CAN=["Tailor your base CV to a job posting, and show you what changed",
  "Look at the page it rendered, and fix what does not fit",
  "Track applications from your inbox: replies, interviews, rejections",
  "Check a CV the way an applicant tracking system reads it"];

function obStep(){
  const i=OB.step, st=S.state||{};
  let head, lede, main="", side="", say="";
  const shot='<div class="onb-cap" id="onb-cap"></div><div class="onb-shot" id="onb-shot"></div>';
  if(i===1&&OB.imp){
    head="Here is what we read";
    lede="From "+esc(OB.imp.name)+". The page beside this is your CV, rebuilt and rendered "+
      "in CV Studio.";
    main='<div class="onb-found">'+OB.imp.found.map(f=>'<div><i>'+obIcon("tick",13,3)+'</i>'+
        '<b>'+esc(f.what)+'</b><span>'+esc(String(f.count))+'</span></div>').join("")+'</div>'+
      (OB.imp.notes.length?'<div class="onb-note"><i>!</i><span><b>'+
        (OB.imp.notes.length===1?"One thing to check.":OB.imp.notes.length+" things to check.")+
        '</b>'+(OB.imp.notes.length===1?" "+esc(OB.imp.notes[0]):'<ul>'+OB.imp.notes.map(n=>
          '<li>'+esc(n)+'</li>').join("")+'</ul>')+' Fix it on the page, or ask a connected '+
        'AI client to tidy the import.</span></div>':'')+
      '<p class="onb-small">Nothing is saved until you press Next. The file stays where it is; '+
        'only what was read goes into your base CV. '+
        '<button class="linkish" id="onb-again">Import a different file</button></p>';
    side=shot;
  }else if(i===1){
    head="Start from what you have";
    lede="Bring the CV you already have, or start from a blank one. Either way you get a "+
      "base CV every tailored copy starts from.";
    const srcs=[
      ["pdf","PDF","Import your CV","A PDF, or an export from Reactive Resume or JSON "+
        "Resume. Exports come across field by field; from a PDF, jobs, education and skills "+
        "are sorted into sections, and a connected AI client can tidy what the rules miss."],
      ["linkedin",'<svg viewBox="0 0 448 512" aria-hidden="true"><use href="#board-linkedin"/></svg>',
        "Import from LinkedIn","Your data archive or your profile saved as PDF."],
      ["blank","+","Start from a blank CV","The starter layout, filled in as you go."]];
    main='<div class="onb-srcs" role="radiogroup" aria-label="Start from">'+srcs.map(([k,g,t,x])=>
      '<button class="onb-src" role="radio" data-src="'+k+'" aria-checked="'+String(OB.src===k)+'">'+
        '<span class="row"><span class="tile">'+g+'</span><span class="what"><b>'+t+'</b>'+
        '<span>'+x+'</span></span><span class="radio"></span></span>'+
        (k==="linkedin"&&OB.src==="linkedin"?'<span class="how">'+
          '<span><b>Your data archive (.zip), exact.</b> LinkedIn → Settings → Data privacy → '+
            'Get a copy of your data. Tick Profile, Positions, Education and Skills; it '+
            'arrives by email in about ten minutes.</span>'+
          '<span><b>Or your profile as a PDF.</b> On your profile, More → Save to PDF.</span>'+
          '<span class="quiet">CV Studio never signs in to LinkedIn. You bring the file.</span>'+
        '</span>':'')+
      '</button>').join("")+'</div>';
    if(OB.src==="blank") side=shot;
    else side='<div class="onb-drop'+(OB.importing?' busy':'')+'" id="onb-drop">'+
      '<span class="ic">'+obIcon("up",30,1.8)+'</span>'+
      '<b>'+(OB.importing?"Reading it…":"Drop your "+(OB.src==="linkedin"?"LinkedIn file":"CV")+
        " here")+'</b>'+
      '<span>'+(OB.src==="linkedin"?"Your LinkedIn data archive (.zip), or your profile "+
        "saved as PDF.":"A PDF of your CV, a Reactive Resume or JSON Resume export (.json), "+
        "or your LinkedIn profile as a PDF or data archive (.zip).")+'</span>'+
      (OB.impErr?'<span class="err" role="alert">'+esc(OB.impErr)+'</span>':'')+
      '<button class="onb-btn" id="onb-pick"'+(OB.importing?" disabled":"")+'>Choose a file…</button>'+
      '<small>Read on this machine. Nothing is uploaded.</small>'+
      '<input type="file" id="onb-file" accept=".pdf,.zip,.json,application/pdf,application/zip,application/json" hidden>'+
    '</div>';
    if(OB.src!=="blank") say="No file? Next starts from the blank CV.";
  }else if(i===2){
    head="The top of your CV";
    lede="What prints above everything else. The rest you can write in the editor, or ask "+
      "a model to write from what you already have.";
    main='<div class="onb-fields">'+OB_FIELDS.map(([k,l,ph])=>'<label>'+
      '<span>'+l+(k==="phone"?' <em>optional</em>':'')+'</span>'+
      '<input data-ob="'+k+'" autocomplete="off" placeholder="'+esc(ph)+'" value="'+
        esc(OB.cv[k])+'"></label>').join("")+'</div>';
    side=shot;
  }else if(i===3){
    head="How it prints";
    lede="Pick a theme and a paper size. Every other option is in Design later. The page "+
      "beside this is yours, rendered, not a sample.";
    const by=(OB.thumbs&&OB.thumbs.key===JSON.stringify(obContentPatches())&&OB.thumbs.by)||{};
    main='<div class="onb-lab" id="onb-pl">Paper</div>'+
      '<div class="onb-seg" id="onb-size" role="radiogroup" aria-labelledby="onb-pl">'+
        '<button role="radio" data-s="a4" aria-checked="'+String(OB.size==="a4")+'">A4</button>'+
        '<button role="radio" data-s="us-letter" aria-checked="'+String(OB.size!=="a4")+
          '">US Letter</button></div>'+
      '<div class="onb-lab" id="onb-tl">Theme</div>'+
      '<div class="onb-themes" id="onb-themes" role="radiogroup" aria-labelledby="onb-tl">'+
      (st.themes||[]).map(t=>{
        const img=t===OB.theme&&OB.png?OB.png:by[t]?by[t].png:null;
        return '<button role="radio" data-t="'+esc(t)+'" aria-checked="'+String(t===OB.theme)+'">'+
          '<span class="pic">'+(img?'<img alt="" src="'+esc(img+tok())+'">':thumbHTML(t))+'</span>'+
          '<span class="nm"><span>'+esc(themeLabel(t))+'</span><em>'+(by[t]?by[t].pages+" page"+
            (by[t].pages===1?"":"s"):"")+'</em></span></button>'}).join("")+'</div>';
    side=shot;
  }else{
    head="Connect an AI client";
    lede="Optional. Connected, it can tailor a CV to a posting, look at the page it rendered, "+
      "and keep your applications in step with your mail.";
    const cs=S.ai||[];
    main='<div class="onb-ai">'+(cs.length?cs.map(c=>{
      const live=c.state==="connected";
      return '<div class="onb-client" data-client="'+c.id+'">'+
        '<span class="badge"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#'+c.id+
          '-mark"/></svg></span>'+
        '<span class="nm"><b>'+esc(c.label)+'</b><span>'+esc(live?aiSay(c):c.state==="absent"
          ?"Connect, then "+c.restart.replace(/^./,x=>x.toLowerCase()):aiSay(c))+'</span></span>'+
        (live?'<span class="on"><i></i>'+esc(aiPill(c))+'</span>'
             :'<button class="onb-btn" data-ob-connect="'+c.id+'">Connect</button>')+
      '</div>'}).join(""):'<p class="onb-small">Checking…</p>')+'</div>';
    side='<div class="onb-can"><b>Once one is connected, you can ask it to</b>'+
      OB_CAN.map(x=>'<div><svg width="16" height="16" viewBox="0 0 24 24" fill="none" '+
        'stroke="#c08a3e" stroke-width="2.4" aria-hidden="true">'+OB_ICON.tick+'</svg>'+
        '<span>'+x+'</span></div>').join("")+
      '<span>It works on your CV Studio folder and nothing else.</span></div>';
  }
  const last=i===OB_STEPS.length-1;
  return obRail()+'<div class="onb-work">'+
    '<div class="onb-step"><section class="onb-main">'+
      '<span class="onb-kicker">Step '+(i+1)+' of '+OB_STEPS.length+'</span>'+
      '<h1 id="onb-title">'+head+'</h1><p>'+lede+'</p>'+main+'</section>'+
      '<aside class="onb-side">'+side+'</aside></div>'+
    '<footer class="onb-foot"><span class="say">'+say+'</span>'+
      '<button class="onb-btn" id="onb-back">Back</button>'+
      '<button class="onb-btn primary" id="onb-next">'+(last?"Open my CV":"Next")+'</button>'+
    '</footer></div>';
}

function obPaint(){
  const i=OB.step, root=$("#onb");
  /* Re-painting the step you are on keeps its place and skips the slide-in:
     only moving between steps should move. */
  const same=root.dataset.step===String(i), keep=same&&root.querySelector(".onb-step");
  const top=keep?keep.scrollTop:0;
  root.innerHTML=obBar()+(i===0?obWelcome():obStep());
  root.dataset.step=String(i);
  const stepEl=root.querySelector(".onb-step");
  if(stepEl&&same){ stepEl.style.animation="none"; stepEl.scrollTop=top }

  $$("#onb .onb-bar [data-w]").forEach(b=>b.onclick=()=>$("#"+b.dataset.w).click());
  const rv=$("#onb-reveal");
  if(rv) rv.onclick=async()=>{ try{ await post("/api/reveal",{}) }catch(e){ toast(e.message,true) } };
  const sk=$("#onb-skip"); if(sk) sk.onclick=obClose;
  const dm=$("#onb-demo");
  if(dm) dm.onclick=()=>{ dm.classList.add("busy"); dm.querySelector("b").textContent=t("Making it…");
    setSample(true,null); dm.disabled=true };
  const lt=$("#onb-later"); if(lt) lt.onclick=obClose;
  const bk=$("#onb-back"); if(bk) bk.onclick=()=>{ OB.step--; obPaint() };
  $("#onb-next").onclick=obNext;

  $$("#onb [data-src]").forEach(b=>b.onclick=()=>{
    if(OB.src===b.dataset.src) return;
    OB.src=b.dataset.src; OB.impErr=null; obPaint();
    if(OB.src==="blank"&&!OB.png) obPreview(true);
  });
  const drop=$("#onb-drop");
  if(drop){
    const file=$("#onb-file");
    $("#onb-pick").onclick=()=>file.click();
    file.onchange=()=>obReadFile(file.files[0]);
    drop.ondragover=e=>{ e.preventDefault(); drop.classList.add("over") };
    drop.ondragleave=()=>drop.classList.remove("over");
    drop.ondrop=e=>{ e.preventDefault(); drop.classList.remove("over");
      if(!OB.importing) obReadFile(e.dataTransfer.files[0]) };
  }
  const ag=$("#onb-again");
  if(ag) ag.onclick=()=>{ OB.imp=null; OB.impErr=null; OB.png=null; OB.pages=null;
    obFill(OB.baseCv||{}); obPaint() };
  $$("#onb [data-ob]").forEach(el=>el.oninput=()=>{ OB.cv[el.dataset.ob]=el.value; obPreview() });
  $$("#onb-themes [data-t]").forEach(bt=>bt.onclick=()=>{
    const was=OB.theme; OB.theme=bt.dataset.t;
    $$("#onb-themes [data-t]").forEach(x=>x.setAttribute("aria-checked",String(x===bt)));
    /* The tile you left gets its own render back, if it has one. */
    const old=$('#onb-themes [data-t="'+was+'"] .pic'), by=OB.thumbs&&OB.thumbs.by[was];
    if(old&&by) old.innerHTML='<img alt="" src="'+esc(by.png+tok())+'">';
    obPreview(true);
  });
  $$("#onb-size [data-s]").forEach(bt=>bt.onclick=()=>{
    OB.size=bt.dataset.s;
    $$("#onb-size [data-s]").forEach(x=>x.setAttribute("aria-checked",String(x===bt)));
    obPreview(true);
  });
  $$("#onb [data-ob-connect]").forEach(bt=>bt.onclick=async()=>{
    bt.disabled=true; bt.textContent="Connecting…";
    try{ await post("/api/ai/connect",{client:bt.dataset.obConnect}) }
    catch(e){ toast(e.message,true) }
    await loadAI(); if(onbOpen()&&OB.step===4) obPaint();
  });
  if(i===4&&!S.ai) loadAI().then(()=>{ if(OB.step===4&&onbOpen()) obPaint() });

  if($("#onb-shot")){ obShot(); if(!OB.png&&!OB.busy) obPreview(true) }
  if(i===3) obThumbs();
  const f=$("#onb input[data-ob]")||$("#onb-next"); if(f&&!same) f.focus();
}

async function obNext(){
  const btn=$("#onb-next");
  /* The page is written when you leave the step that changed it, so Back and
     Finish later both keep what you did. */
  if(OB.step>=2||(OB.step===1&&OB.imp)){
    btn.disabled=true;
    try{ await post("/api/save",{path:OB.path,patches:obPatches()}) }
    catch(e){ btn.disabled=false; return toast(e.message,true) }
    btn.disabled=false;
  }
  if(OB.step<OB_STEPS.length-1){ OB.step++; return obPaint() }
  OB.step=-1;   /* saved already: closing must not write it again */
  obClose();
  await obRefresh();
  openDoc(OB.path);
}
/* Nothing behind the setup should hear its keys: Escape is not a way out of a
   screen that has its own Finish later, and the arrows belong to its fields. */
window.addEventListener("keydown",e=>{ if(onbOpen()) e.stopPropagation() },true);


