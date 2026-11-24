/* Document rail, outline, block editor, inspector, and Form tab. */
/* =========================================================================
   Editor
   ========================================================================= */

/* The form, the inspector and the live preview all read and write one working
   copy of the document rather than each other's DOM. That is what makes the
   inspector and the Form tab edit the same field without fighting: whichever
   is on screen renders from the model, and both write back into it. */
const getAt=(o,p)=>p.reduce((x,k)=>(x==null?undefined:x[k]),o);
function setAt(o,p,v){
  let n=o;
  for(let i=0;i<p.length-1;i++){ if(n==null) return; n=n[p[i]] }
  if(n!=null) n[p[p.length-1]]=v;
}
const HEADER_KEYS=["name","headline","location","email","phone","website"];

/* Every leaf the form exposes, in one place, so a save writes exactly the
   fields the user could have edited -- no more, no less. Arrays of scalars
   count as one leaf, which is what lets a bullet be added or removed. */
function leafPaths(){
  const cv=S.data&&S.data.cv; if(!cv) return [];
  const out=[];
  HEADER_KEYS.forEach(k=>{
    if(k in cv||["name","headline","location","email"].includes(k)) out.push(["cv",k]);
  });
  if("photo" in cv) out.push(["cv","photo"]);
  const sections=cv.sections||{};
  for(const name of Object.keys(sections)){
    (sections[name]||[]).forEach((it,i)=>{
      if(it===null||typeof it!=="object") out.push(["cv","sections",name,i]);
      else for(const k of Object.keys(it)) out.push(["cv","sections",name,i,k]);
    });
  }
  return out;
}
/* RenderCV types a section by what is in it: every section is a
   list[OneLineEntry] or a list[ExperienceEntry] and never a mixture, so a new
   entry has to have the shape of its neighbours and a new section has to pick
   one. These are RenderCV's own entry models, with every required field blank
   and the useful optional ones set to null.

   null and not "": an empty string is a valid str, but it is not a valid date,
   and `start_date: ""` fails validation outright. An absent value has to look
   absent. */
const ENTRY_TYPES=[
  ["ExperienceEntry","Experience","A job: employer, role, dates and bullets",
    ()=>({company:"",position:"",location:null,start_date:null,end_date:null,
          highlights:[""]})],
  ["EducationEntry","Education","A degree: school, subject, dates and bullets",
    ()=>({institution:"",area:"",degree:null,location:null,start_date:null,
          end_date:null,highlights:[""]})],
  ["NormalEntry","Project","Anything with a name, dates and bullets",
    ()=>({name:"",location:null,start_date:null,end_date:null,highlights:[""]})],
  ["OneLineEntry","One line","A label and its details, side by side",
    ()=>({label:"",details:""})],
  ["BulletEntry","Bullet","A single bullet point",()=>({bullet:""})],
  ["TextEntry","Text","A paragraph of prose",()=>""],
  ["PublicationEntry","Publication","Title, authors, journal, DOI",
    ()=>({title:"",authors:[""],journal:null,doi:null,date:null})],
  ["NumberedEntry","Numbered","A numbered line",()=>({number:""})],
  ["ReversedNumberedEntry","Numbered, counting down","A numbered line, in reverse",
    ()=>({reversed_number:""})],
];
/* What a section already holds, so "add" means "another one of these" rather
   than a question you have to answer every time. Keys rather than a guess at
   the model name: two types can share a shape, and the keys are what the form
   is going to draw anyway. */
function blankLike(list){
  const first=(list||[])[0];
  if(first===undefined) return null;
  if(first===null||typeof first!=="object") return "";
  const out={};
  for(const k of Object.keys(first))
    out[k]=Array.isArray(first[k])?[""]:(typeof first[k]==="number"?null:
      (/(^|_)date$/.test(k)?null:""));
  return out;
}
const sectionLabel=n=>String(n).replace(/_/g," ").replace(/^./,c=>c.toUpperCase());
function entryTitle(it,i){
  if(it===null||typeof it!=="object")
  { /* A paragraph by its first sentence, cut at a word with an ellipsis. */
    const x=String(it||"").replace(/[*_`]/g,"").trim().split(/(?<=[.!?])\s/)[0];
    if(!x) return "item "+(i+1);
    if(x.length<=44) return x;
    return x.slice(0,44).replace(/\s+\S*$/,"")+"…";
  }
  /* Every RenderCV entry type keeps its headline under a different key, and
     a publication or a bullet reading "entry 3" in the outline is no use. */
  return it.company||it.institution||it.name||it.title||it.label||it.position||
    it.bullet||("entry "+(i+1));
}
/* The line under the title. entryTitle answers "what is this", usually with an
   employer or a school -- and two jobs at the same company then read
   identically in the editor and the outline alike. This answers "which one",
   with the role and the years that actually separate them. */
const SUB_KEYS=["position","degree","area","title","label","authors"];
function entrySub(it){
  if(!it||typeof it!=="object") return "";
  const top=entryTitle(it,0), out=[];
  for(const k of SUB_KEYS){
    const v=it[k];
    if(typeof v==="string"&&v.trim()&&v.trim()!==top){ out.push(v.trim()); break }
  }
  const span=it.date||[it.start_date,it.end_date].filter(Boolean).join(" – ");
  if(span) out.push(String(span));
  return out.join(" · ");
}
function wordsIn(v){
  if(v==null) return 0;
  if(Array.isArray(v)) return v.reduce((a,x)=>a+wordsIn(x),0);
  if(typeof v==="object") return Object.values(v).reduce((a,x)=>a+wordsIn(x),0);
  return String(v).trim()?String(v).trim().split(/\s+/).length:0;
}

/* ---- documents ---------------------------------------------------------- */
/* The rail is grouped, because a CV and a cover letter are different kinds of
   thing and reading them as one list means reading every label to find either.
   The groups come from the server, which already knows -- the folder a
   document sits in is what decides it. */
const DOC_GROUPS=["My CVs","Cover letters","Applications"];
function renderDocs(docs){
  S.state.documents=docs;
  const host=$("#doclist");
  const newRow='<button class="row" id="doc-new"><span class="mark"></span>'+
    '<span class="lbl" style="color:var(--t500)">+ New document…</span></button>';
  if(!docs.length){
    host.innerHTML='<p style="color:var(--c300);font-size:12.5px;padding:6px 8px">'+
      'Nothing here yet.</p>'+newRow;
  }else{
    const groups=DOC_GROUPS.filter(g=>docs.some(d=>d.group===g));
    /* Only a document in another language than the base CV says which. */
    const srcLang=(baseFamily()[0]||{}).lang||"en";
    /* The base CV leads its group with its translations tucked under it,
       each named by its language rather than a fourth "my-cv". */
    const fam=baseFamily(), famAt=new Map(fam.map((m,i)=>[m.path,i]));
    const order=d=>famAt.has(d.path)?famAt.get(d.path):fam.length;
    host.innerHTML=groups.map(g=>{
      const inG=docs.filter(d=>d.group===g).map((d,i)=>[d,i])
        .sort((a,b)=>order(a[0])-order(b[0])||a[1]-b[1]).map(x=>x[0]);
      const rows=inG.map(d=>{
        const tr=famAt.get(d.path)>0;
        const other=(d.lang||"en")!==srcLang;
        const label=tr?langOf(d.lang).native:docTitle(d,true);
        const pp=S.pages[d.path];
        const job=S.jobs.find(j=>j.cv_path===d.path||j.letter_path===d.path);
        return '<button class="row'+(tr?" tr":"")+(d.path===S.path?" sel":"")+
          '" data-path="'+esc(d.path)+'" title="'+esc(d.path)+
          (d.base?"\n"+esc(t("tailored from {p}",{p:d.base})):"")+
          (d.ai?"\n"+esc(whoLabel(d.ai))+" worked on this "+
            ago(d.ai.at*1000)+" ago":"")+
          (job?"\n"+esc(job.title+" · "+job.company):"")+'">'+
          '<span class="mark"></span>'+
          '<span class="lbl">'+esc(label)+'</span>'+
          (other&&!tr?lchip(d.lang):tr?'<span class="flg-only">'+flag(d.lang)+'</span>':'')+
          (isBase(d.path)?'<span class="btag" title="The base CV: every tailored CV '+
            'starts as a copy of it">base</span>':'')+
          markHTML(d.ai,null,true)+rvPill(d)+
          (job?'<span class="tie" title="'+esc(t("Linked to"))+' '+
            esc(job.title+" · "+job.company)+'"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" '+
            'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/>'+
            '<path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg></span>':"")+
          '<span class="ct mono">'+(pp>1?t("{n} pages",{n:pp}):"")+'</span></button>';
      }).join("");
      /* Only worth naming the groups once there is more than one of them. */
      return (groups.length>1
        ? '<div class="rail-sub">'+esc(g==="My CVs"?"CVs":g)+'</div>' : "")+rows;
    }).join("")+newRow;
  }
  $$("#doclist [data-path]").forEach(b=>b.onclick=async ()=>{
    if(b.dataset.path===S.path) return;
    if(!await leaveEdits()) return;
    openDoc(b.dataset.path);
  });
  $("#doc-new").onclick=()=>newDocumentSheet();
}

async function openDoc(path){
  if(isLetterPath(path)) return openLetter(path);
  if(path) palRemember({doc:path});
  closeOverlays();
  /* Which list you came from. Not a history stack -- one bit, read once on the
     way out. The application a document belongs to is still the better answer
     when there is one, and goBack asks for that first. */
  setView("cvs");
  S.path=path; S.dirty=false; S.savedAt=null; S.sel=null; S.openSection=null;
  S.prov=null; $("#provchip").hidden=true; $("#cv-rv").hidden=true;
  S.render=null; S.renderMs=null; S.fill=null; S.themePages={}; S.zoomAuto=true;
  hideExternalChange();
  /* The Design panel still holds the last document's controls, and its inputs
     are read straight into the patch list. Empty it until it is rebuilt. */
  $("#dz-advanced").innerHTML=""; S.thumbs=null;
  $("#pane-page").innerHTML='<div class="skel" style="width:472px;height:668px"></div>';
  $("#btn-render").disabled=false;
  renderDocs(S.state.documents);
  /* The newest open wins: an earlier one landing late would put its content
     under this one's name, and a save would then write it there. */
  const seq=S.openSeq=(S.openSeq||0)+1;
  try{
    const doc=await api("/api/doc?path="+encodeURIComponent(path));
    if(seq!==S.openSeq) return;
    S.doc=doc;
    S.docMtime=doc.mtime;
    S.data=doc.data?JSON.parse(JSON.stringify(doc.data)):null;
    setProv(doc.prov);
    $("#yaml").value=doc.yaml; paint();
    const dz=(doc.data&&doc.data.design)||{};
    DZ.theme=dz.theme||null;
    setYamlError(doc.parse_error);
    paintTitle(); paintLink(); paintLang(); paintPhotoChip();
    buildOutline();
    selectDefault();
    buildForm();
    doRender();
  }catch(e){ if(seq===S.openSeq) toast(e.message,true) }
}

/* Rename and delete, for the open document. Deleting takes a second click
   and goes to the workspace's .trash folder; the base CV and a document
   others are translated from are refused by the server, which says why. */
function docMenuSheet(path){
  const doc=(S.state.documents||[]).find(d=>d.path===path)||{};
  let stem=path.split("/").pop().replace(/\.(ya?ml|md)$/i,"");
  if(doc.translation_of) stem=stem.replace(/\.[a-z]{2}(-[a-z]{2})?$/i,"");
  openSheet('<div><h3>'+t("This document")+'</h3><p class="mono" style="font-size:12px">'+esc(path)+'</p></div>'+
    '<div class="fg w88"><label for="dm-name">'+t("Name")+'</label><input id="dm-name" value="'+esc(stem)+'"></div>'+
    '<div class="foot"><button class="sbtn danger" id="dm-del">'+t("Delete…")+'</button><div class="grow"></div>'+
    '<button class="sbtn" data-cancel>'+t("Cancel")+'</button>'+
    '<button class="sbtn primary" id="dm-ren">'+t("Rename")+'</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  const refresh=async()=>{ const st=await api("/api/state"); S.state=st; renderDocs(st.documents); paintBase();
    await loadJobs(true); if(S.view==="docs") drawDocuments() };
  $("#dm-ren").onclick=async()=>{
    if(S.dirty&&S.path===path) return toast(t("Save your changes first."),true);
    try{
      const r=await post("/api/doc/rename",{path,name:$("#dm-name").value});
      closeSheet(); await refresh();
      if(S.path===path&&r.path!==path){ S.dirty=false; openDoc(r.path) }
      toast(t("Renamed"));
    }catch(e){ toast(e.message,true) }
  };
  const del=$("#dm-del");
  del.onclick=async()=>{
    if(!del.dataset.sure){ del.dataset.sure="1"; del.textContent=t("Delete it: it goes to the .trash folder"); return }
    try{
      await post("/api/doc/delete",{path});
      closeSheet();
      if(S.path===path){ S.dirty=false; closeEditor() }
      await refresh(); toast(t("Moved to the .trash folder in your workspace"));
    }catch(e){ toast(e.message,true); del.dataset.sure=""; del.textContent=t("Delete…") }
  };
}
$("#doc-more").onclick=()=>{ if(S.path) docMenuSheet(S.path) };

function paintTitle(){
  /* The document by the name it has everywhere else (Documents, the Design
     crumb), after the company it was written for. */
  const doc=(S.state.documents||[]).find(d=>d.path===S.path);
  const link=linkedJob(), file=S.path?S.path.split("/").pop():"";
  const label=(doc&&doc.label)||file.replace(/\.(ya?ml|md)$/,"");
  $("#doctitle .t").textContent=link?link.company+" · "+label:label;
  $("#doctitle .f").textContent=link?file:"";
}

/* ---- outline ------------------------------------------------------------ */
function buildOutline(){
  const cv=S.data&&S.data.cv;
  const host=$("#outline");
  if(!cv){ host.innerHTML=''; $("#outline-label").hidden=true; return }
  $("#outline-label").hidden=false;
  const sections=cv.sections||{};
  let h='<button class="orow'+(S.sel&&S.sel.kind==="header"?" sel":"")+
        '" data-o="header"><span>Header</span>'+
        markHTML(provHeader(),null,true)+(baseHeader()?basebar():"")+
        '</button>';
  for(const name of Object.keys(sections)){
    const list=sections[name]||[];
    const on=S.openSection===name;
    const spath=["cv","sections",name];
    h+='<button class="orow'+(on?" sel":"")+'" data-o="section" data-name="'+esc(name)+'">'+
       '<span>'+esc(sectionLabel(name))+'</span>'+
       markHTML(provUnder(spath),null,true)+(baseUnder(spath)?basebar():"")+
       '<span class="ct mono">'+list.length+'</span></button>';
    if(on&&list.length){
      h+='<div class="okids">'+list.map((it,i)=>{
        const epath=["cv","sections",name,i];
        return '<button class="okid'+(S.sel&&S.sel.kind==="entry"&&S.sel.name===name&&S.sel.i===i
          ?" sel":"")+'" data-o="entry" data-name="'+esc(name)+'" data-i="'+i+'">'+
        esc(entryTitle(it,i))+markHTML(provUnder(epath),null,true)+
        (baseUnder(epath)?basebar():"")+'</button>';
      }).join("")+'</div>';
    }
  }
  host.innerHTML=h;
  host.querySelectorAll("[data-o]").forEach(b=>b.onclick=()=>{
    const k=b.dataset.o;
    if(k==="header") select({kind:"header"});
    else if(k==="section") select({kind:"section", name:b.dataset.name});
    else select({kind:"entry", name:b.dataset.name, i:+b.dataset.i});
  });
}

function selectDefault(){
  const cv=S.data&&S.data.cv;
  if(!cv) return select(null);
  const first=Object.keys(cv.sections||{})[0];
  if(first) select({kind:"section", name:first}); else select({kind:"header"});
}

/* Selecting anywhere -- outline, inspector, or the Form tab -- moves the same
   selection, so the three surfaces always agree on what is being edited. */
/* `from` says which pane the selection came from, so that pane is not rebuilt
   under the hands of whoever is using it. Everything else is the same work. */
function select(sel,from){
  if(sel&&sel.kind==="section"){
    const list=((S.data.cv.sections||{})[sel.name])||[];
    S.openSection=sel.name;
    sel=list.length?{kind:"entry",name:sel.name,i:0}:{kind:"section",name:sel.name};
  }else if(sel&&sel.kind==="entry"){ S.openSection=sel.name }
  S.sel=sel;
  buildOutline();
  /* The page first: the editor anchors itself to the highlighted block, and
     paintHits is what draws it. Placing the card before that happened left it
     pointing at whatever was selected a moment ago, which showed up as an
     editor that stayed put while the arrows walked the document. */
  trackSelectionOnPage();
  revealSelectedOnPage();
  /* Only refresh the editor if it is already open. Selecting from the outline
     is navigation -- it moves the highlight on the page and scrolls to it --
     and having that throw a form open over the page every time would put back
     the panel this replaced. */
  if(!$("#ed").hidden) openEditor();
  /* Rebuilding the form is how the mark gets into it, but not when the form
     is where the selection came from: that would tear out the field being
     typed in, taking the caret with it. Move the mark instead. */
  if(S.tab==="form") from==="form"?markFormBlock():buildForm();
  if(S.tab==="yaml") markYamlSelection();
}
function markFormBlock(){
  const sel=S.sel;
  $$("#pane-form .formblock").forEach(el=>el.classList.toggle("on",
    !!sel&&sel.kind==="entry"&&el.dataset.block===sel.name&&+el.dataset.bi===sel.i));
}

/* ---- the block editor ---------------------------------------------------
   Opened by clicking a block on the page, anchored beside it. */
function openEditor(){
  const ed=$("#ed");
  if(!S.sel||!(S.data&&S.data.cv)){ closeEditor(); return }
  ed.hidden=false;
  buildInspector();
  /* Nothing above the first block and nothing below the last, so say so rather
     than leaving two buttons that silently do nothing at the ends. */
  const all=blockOrder(), at=all.findIndex(x=>sameSel(x,S.sel));
  $("#ed-prev").disabled=at<=0;
  $("#ed-next").disabled=at<0||at>=all.length-1;
  /* The sheet changes size here, so it happens before anything is measured:
     a block measured first is measured where it used to be. */
  makeRoom(true); refit();
  revealSelectedOnPage();
  placeEditor();
}
/* Walking the document with the arrows has to bring the page along, or the
   editor fills with an entry you cannot see. Only when the block is actually
   out of the pane: if you clicked it, it is already in front of you, and
   scrolling under the click would be the app moving on its own. */
function revealSelectedOnPage(){
  const pane=$("#pane-page");
  const hit=pane&&pane.querySelector(".pgwrap .hit.sel");
  if(!hit) return;
  const p=pane.getBoundingClientRect(), b=hit.getBoundingClientRect();
  const pad=24;
  if(b.top>=p.top+pad&&b.bottom<=p.bottom-pad) return;
  /* Instant, because placeEditor measures the block straight after this and a
     smooth scroll would hand it a rectangle still in motion. */
  pane.scrollTop+=b.top-p.top-pad;
}
function closeEditor(){
  const ed=$("#ed");
  if(ed.hidden) return;
  ed.hidden=true;
  ed.removeAttribute("style");
  makeRoom(false); refit();
}
/* Beside the block, never on top of it: you have to be able to read what you
   are editing.

   Fixed to the viewport rather than absolute inside .pgwrap. Anchoring it to
   the page sounded right -- it would travel with the sheet for free -- but the
   pane scrolls and clips, so a block near the left edge produced a negative
   offset and the whole label column was cut off against the rail. Positioned
   against the window instead, it cannot be clipped, and the scroll handler
   re-places it. */
/* Whether the sheet can spare the width. It depends on the sheet and the pane,
   not on which block is selected, so it does not flip while the arrows walk
   the document -- the page shifts once when the card comes out and holds
   still. On a window too narrow for both, the card overlaps instead: a page
   pushed half out of its own pane would be worse. */
function makeRoom(on){
  const pane=$("#pane-page"), img=pane.querySelector(".pg");
  const room=($("#ed").offsetWidth||380)+26;
  pane.style.setProperty("--ed-room",room+"px");
  /* Auto-fit resizes the sheet into whatever is left, so there is always room
     for the card. A zoom you chose is a fixed size, and the card only stands
     beside it when it genuinely fits: shrinking your page to make space for a
     panel would be the app overruling a choice you made. */
  const fits=S.zoomAuto||(!!img&&pane.clientWidth-52-room>=img.offsetWidth);
  pane.classList.toggle("ed-open",!!on&&fits);
}
function placeEditor(){
  const ed=$("#ed");
  const pane=$("#pane-"+S.tab);
  if(!pane) return;
  const paneBox=pane.getBoundingClientRect();
  const hit=S.tab==="page"&&$("#pane-page .pgwrap .hit.sel");
  ed.style.position="fixed";
  const w=ed.offsetWidth||380, h=ed.offsetHeight||320, gap=14, edge=12;

  if(!hit){
    /* Off the page tab, or before a render, there is no block to point at, so
       it sits in the corner of the pane rather than at nothing. */
    ed.dataset.side="none";
    ed.style.left=Math.round(paneBox.right-w-edge)+"px";
    ed.style.top=Math.round(paneBox.top+edge)+"px";
    return;
  }
  const b=hit.getBoundingClientRect();
  /* Whichever side has room, preferring the right; never over the rail. */
  const fitsRight=b.right+gap+w<=paneBox.right+ (innerWidth-paneBox.right) - edge;
  const fitsLeft=b.left-gap-w>=paneBox.left+edge;
  let x, side;
  if(fitsRight){ x=b.right+gap; side="right" }
  else if(fitsLeft){ x=b.left-gap-w; side="left" }
  else{
    /* Neither margin is wide enough, so it overlaps the sheet on the side
       with the most room rather than being pushed off the pane. */
    const roomRight=innerWidth-b.right, roomLeft=b.left-paneBox.left;
    side=roomRight>=roomLeft?"right":"left";
    x=side==="right"?innerWidth-w-edge:paneBox.left+edge;
  }
  ed.dataset.side=side;
  ed.style.left=Math.round(Math.min(Math.max(x,paneBox.left+edge),
                                    innerWidth-w-edge))+"px";
  /* Level with the top of the block, held inside the pane so a block low on
     the sheet does not open an editor over the status bar. */
  const top=Math.min(Math.max(b.top,paneBox.top+edge),
                     Math.max(paneBox.top+edge,paneBox.bottom-h-edge));
  ed.style.top=Math.round(top)+"px";
  /* When it had to be held back like that, the card is no longer level with
     the block, so the pointer slides down the edge to keep aiming at it --
     otherwise it points confidently at the wrong paragraph. */
  ed.style.setProperty("--ptr",
    Math.round(Math.min(Math.max(b.top-top+6,10),Math.max(10,h-20)))+"px");
}

/* ---- shared field markup ------------------------------------------------ */
function inputFor(path,value,opts){
  opts=opts||{};
  const p=esc(JSON.stringify(path));
  const mono=opts.mono?" mono":"";
  if(Array.isArray(value))
    return '<textarea data-p='+"'"+p+"'"+' data-kind="lines" rows="3">'+
      esc(value.join("\n"))+'</textarea>';
  if(opts.multi)
    return '<textarea data-p='+"'"+p+"'"+' rows="3" class="'+mono.trim()+'">'+
      esc(value==null?"":value)+'</textarea>';
  return '<input data-p='+"'"+p+"'"+' class="'+mono.trim()+'" value="'+
    esc(value==null?"":value)+'">';
}
function fieldRow(label,path,value,opts){
  /* The mark sits in the label rather than beside the input: the input is
     where you type, and anything parked in it reads as part of the value.

     A list is one control here but many fields underneath, so it answers for
     everything it contains -- otherwise a rewritten bullet shows no mark in
     the Form tab, where the whole list is a single textarea. */
  const arr=Array.isArray(value);
  const mark=arr?markHTML(provUnder(path),null,true)+
                 (baseUnder(path)?basebar():"")
                :markHTML(provOf(path),path);
  return '<label title="'+esc(label)+'">'+esc(human(label))+
    mark+'</label>'+inputFor(path,value,opts);
}
/* Monospace is for things you read character by character -- a URL or a
   DOI. A phone number and a date are prose, and setting them in mono next
   to sans-set siblings looks like a bug rather than a decision. */
const MONO_KEYS=/^(url|website|doi)$/;

/* One handler for every bound control on the page: write into the working
   copy, mark dirty, and let the debounce decide when to re-render. */
function bindFields(root){
  root.addEventListener("input",e=>{
    const el=e.target;
    if(el.dataset.p!==undefined){
      const path=JSON.parse(el.dataset.p);
      let v=el.value;
      if(el.dataset.kind==="lines") v=v.split("\n").map(x=>x.trim()).filter(Boolean);
      else{
        const was=getAt(S.doc.data,path);
        if(typeof was==="number"&&v.trim()!==""&&!isNaN(v)) v=Number(v);
        /* An emptied date has to be absent, not empty. RenderCV accepts "" for
           any other optional string, but a date is parsed, and `start_date: ""`
           fails validation outright -- so clearing one broke the render of a
           document that was fine, and the blank entries Add creates start out
           with exactly these fields empty. */
        if(v==="") v=(/(^|_)date$/.test(String(path[path.length-1]))||was===null)
          ? null : v;
      }
      setAt(S.data,path,v);
      /* Keep growing as you type, or it clips again the moment the text runs
         past the bottom of the box. */
      if(el.tagName==="TEXTAREA") autoGrow(el);
      touch();
    }else if(el.closest("[data-arr]")){
      const card=el.closest("[data-arr]");
      setAt(S.data,JSON.parse(card.dataset.arr),
        [...card.querySelectorAll("textarea")].map(t=>t.value));
      autoGrow(el); touch();
    }
  });
}
function autoGrow(el){ el.style.height="0"; el.style.height=el.scrollHeight+"px" }
const touch=()=>{ if(!S.dirty) warnSent(); S.dirty=true; S.gen=(S.gen||0)+1; paintStatus(); scheduleLive() };
/* The day a CV's application went out, if it did: then the company has it. */
const sentAt=j=>j&&j.status!=="pending"?String(appliedAt(j)||"").slice(0,10)||null:null;
/* Once, the first time you change a CV a company already has: your edits
   change your copy, not theirs, and the CV to re-read before an interview. */
function warnSent(){
  const j=(S.jobs||[]).find(x=>x.cv_path===S.path), d=sentAt(j);
  S.warned=S.warned||new Set();
  if(!d||S.warned.has(S.path)) return;
  S.warned.add(S.path);
  toast(t("{co} has the version you sent on {d}. These edits change your copy, not theirs.",{co:j.company,d:shortDate(d)}));
}

/* ---- inspector ----------------------------------------------------------- */
function buildInspector(){
  const head=$("#insp-title"), meta=$("#insp-meta"), body=$("#insp-body");
  const cv=S.data&&S.data.cv;
  if(!cv){
    head.textContent="No form"; meta.textContent="";
    body.innerHTML='<p class="note">This file has a YAML error, so it cannot be parsed '+
      'into fields. Switch to the YAML tab to fix it.</p>';
    return;
  }
  const sel=S.sel;
  if(!sel){ head.textContent="Nothing selected"; meta.textContent=""; body.innerHTML=""; return }

  if(sel.kind==="header"){
    head.textContent="Header";
    meta.textContent=[cv.name,wordsIn(Object.fromEntries(
      HEADER_KEYS.map(k=>[k,cv[k]])))+" words"].filter(Boolean).join(" · ");
    body.innerHTML='<div class="fg">'+HEADER_KEYS.map(k=>
      fieldRow(k,["cv",k],cv[k],{mono:MONO_KEYS.test(k)})).join("")+'</div>';
    wireInspector(); return;
  }
  const list=(cv.sections||{})[sel.name]||[];
  if(sel.kind==="section"||!list.length){
    head.textContent=sectionLabel(sel.name);
    meta.textContent=list.length+" item"+(list.length===1?"":"s");
    body.innerHTML='<p class="note muted">This section is empty. Add entries in the '+
      'YAML tab.</p>';
    wireInspector(); return;
  }
  const it=list[sel.i];
  head.textContent=sectionLabel(sel.name)+" · "+entryTitle(it,sel.i);
  meta.textContent=[entrySub(it),wordsIn(it)+" words"].filter(Boolean).join(" · ");

  if(it===null||typeof it!=="object"){
    body.innerHTML='<div class="block"><span class="blabel">Text</span>'+
      inputFor(["cv","sections",sel.name,sel.i],it,{multi:true})+'</div>';
    wireInspector(); return;
  }
  const scalars=Object.keys(it).filter(k=>!Array.isArray(it[k]));
  const arrays=Object.keys(it).filter(k=>Array.isArray(it[k]));
  let h='';
  if(scalars.length) h+='<div class="fg">'+scalars.map(k=>
    fieldRow(k,["cv","sections",sel.name,sel.i,k],it[k],{mono:MONO_KEYS.test(k)})).join("")+'</div>';
  arrays.forEach(k=>{ h+=arrayBlock(k,["cv","sections",sel.name,sel.i,k],it[k]) });
  body.innerHTML=h;
  wireInspector();
}

/* Bullets are a card of rows rather than one blob of text: the row you are
   editing is the one highlighted, and it can be added to or taken away. */
function arrayBlock(label,path,list){
  const p=esc(JSON.stringify(path));
  return '<div class="block"><span class="blabel">'+esc(label.replace(/_/g," "))+
    markHTML(provUnder(path),null,true)+(baseUnder(path)?basebar():"")+'</span>'+
    '<div class="card" data-arr='+"'"+p+"'"+'>'+
    (list.length?list.map((x,i)=>
      '<div class="crow" data-i="'+i+'"><span class="cidx mono"><span>'+(i+1)+
      '</span>'+markHTML(provOf(path.concat(i)),path.concat(i))+'</span>'+
      '<textarea rows="1">'+esc(x==null?"":x)+'</textarea></div>').join("")
      :'<div class="crow"><span class="cidx mono">1</span><textarea rows="1"></textarea></div>')+
    '</div><div style="display:flex;gap:6px">'+
    '<button class="mini" data-arr-add title="Add">+</button>'+
    '<button class="mini" data-arr-del title="Remove the selected one">−</button></div></div>';
}
function wireInspector(){
  const body=$("#insp-body");
  body.querySelectorAll(".crow textarea").forEach(t=>{
    autoGrow(t);
    t.onfocus=()=>{
      body.querySelectorAll(".crow").forEach(r=>r.classList.remove("on"));
      t.closest(".crow").classList.add("on");
    };
  });
  body.querySelectorAll(".block").forEach(block=>{
    const card=block.querySelector("[data-arr]");
    if(!card) return;
    const path=JSON.parse(card.dataset.arr);
    const add=block.querySelector("[data-arr-add]"), del=block.querySelector("[data-arr-del]");
    if(add) add.onclick=()=>{
      setAt(S.data,path,(getAt(S.data,path)||[]).concat([""]));
      touch(); buildInspector();
      const rows=$("#insp-body").querySelectorAll("[data-arr] textarea");
      if(rows.length) rows[rows.length-1].focus();
    };
    if(del) del.onclick=()=>{
      const arr=(getAt(S.data,path)||[]).slice();
      if(arr.length<2) return toast("Keep at least one line, or clear its text.");
      const on=card.querySelector(".crow.on");
      arr.splice(on?+on.dataset.i:arr.length-1,1);
      setAt(S.data,path,arr); touch(); buildInspector();
    };
  });
}

/* Attaching a document to an application it was written for, from the document
   side. The Jobs screen can already pick a document for an application; this is
   the same join made from the end you are more often standing at. */
function linkJobSheet(){
  const isLetter=(S.state.documents||[]).some(
    d=>d.path===S.path&&d.group==="Cover letters");
  const key=isLetter?"letter_path":"cv_path";
  const open=S.jobs.filter(j=>!j[key]);
  const now=linkedJob();
  openSheet(
    '<div><h3>'+(now?"Linked application":"Link to an application")+'</h3><p>'+
    (now
      ? esc(docLabel(S.path))+' is the '+(isLetter?"cover letter":"CV")+
        ' on <b>'+esc(now.title)+' · '+esc(now.company)+'</b>. Pick another to '+
        'move it, or unlink it below.'
      : esc(docLabel(S.path))+' becomes the '+(isLetter?"cover letter":"CV")+
        ' on the application you pick. A document belongs to one application, '+
        'and an application takes one of each.')+'</p></div>'+
    (open.length
      ? '<div class="fg w88"><label>Application</label><select id="lj-job">'+
        open.map(j=>'<option value="'+esc(j.id)+'">'+esc(j.title)+' · '+
          esc(j.company)+'</option>').join("")+'</select></div>'
      : '<div class="fg w88"><p class="note muted">Every application already has '+
        'one. Start a new application, or swap the document over from the '+
        'Applications screen.</p></div>')+
    '<div class="foot">'+
    (now?'<button class="sbtn" id="lj-show">Show in Applications</button>'+
         '<button class="sbtn danger" id="lj-unlink">Unlink</button>':"")+
    '<div class="grow"></div>'+
    '<button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn" id="lj-new">New application…</button>'+
    (open.length?'<button class="sbtn primary" id="lj-ok">'+
      (now?"Move it":"Link")+'</button>':"")+
    '</div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  if(now){
    $("#lj-show").onclick=()=>{ closeSheet(); setView("jobs"); selectJob(now.id) };
    $("#lj-unlink").onclick=async()=>{
      const k=now.cv_path===S.path?"cv_path":"letter_path";
      try{
        await post("/api/jobs/update",{id:now.id,[k]:null});
        await loadJobs(); closeSheet(); renderDocs(S.state.documents);
        paintTitle(); paintLink(); toast("Unlinked");
      }catch(e){ toast(e.message,true) }
    };
  }
  $("#lj-new").onclick=()=>{ closeSheet(); newJobSheet({[key]:S.path}) };
  const ok=$("#lj-ok");
  if(ok) ok.onclick=async()=>{
    ok.disabled=true;
    try{
      await post("/api/jobs/update",{id:$("#lj-job").value,[key]:S.path});
      await loadJobs(); closeSheet(); buildInspector();
      renderDocs(S.state.documents); paintTitle(); paintLink();
      toast("Linked");
    }catch(e){ toast(e.message,true); ok.disabled=false }
  };
}
const docLabel=p=>{
  const d=(S.state.documents||[]).find(x=>x.path===p);
  return d?docTitle(d):String(p||"").split("/").pop();
};

/* The application this document was written for. Knowing it here is what
   makes "Show in Jobs" possible without hunting through the table. */
function linkedJob(){
  return S.jobs.find(j=>j.cv_path===S.path||j.letter_path===S.path)||null;
}
/* ---- the Form tab: the same fields, whole document at once ---------------- */
/* The selection is the thread through all three views. Losing it when you
   switch tabs turns one cockpit into three unrelated views of a YAML file:
   you spot something wrong on the page, switch to Form to fix it, and have
   to find the entry again by eye. */
const selMark=sel=>sameBlock({k:sel.kind,name:sel.name,i:sel.i},S.sel)?" on":"";
/* Bring the marked block into view without yanking the pane around when it
   is already on screen. */
function revealSelected(root){
  const el=root.querySelector(".formblock.on");
  if(!el) return;
  const box=el.getBoundingClientRect(), pane=root.getBoundingClientRect();
  if(box.top<pane.top||box.bottom>pane.bottom)
    el.scrollIntoView({block:"center",behavior:"auto"});
}
function buildForm(){
  const cv=S.data&&S.data.cv;
  if(!cv){ $("#pane-form").innerHTML='<div class="empty"><h3>Can\'t show a form</h3>'+
    '<p>This file has a YAML error, so it cannot be parsed into fields. Switch to the '+
    'YAML tab to fix it.</p></div>'; return }
  const chev='<svg class="chev" width="11" height="11" viewBox="0 0 24 24" fill="none" '+
    'stroke="currentColor" stroke-width="3"><path d="M9 18l6-6-6-6"/></svg>';
  let h='<details class="grp" open><summary>'+chev+'Header</summary><div class="body">'+
    '<div class="fg wide">'+HEADER_KEYS.filter(k=>k in cv||
      ["name","headline","location","email"].includes(k)).map(k=>
      fieldRow(k,["cv",k],cv[k],{mono:MONO_KEYS.test(k)})).join("")+'</div></div></details>';
  const sections=cv.sections||{};
  for(const name of Object.keys(sections)){
    const list=sections[name]||[];
    h+='<details class="grp" open><summary>'+chev+esc(sectionLabel(name))+
      '<span class="count">'+list.length+' item'+(list.length===1?"":"s")+
      '</span></summary><div class="body">';
    list.forEach((it,i)=>{
      /* Every entry gets the same head, a text entry included: it is where the
         remove lives, and an entry you can add but not take away again is half
         a control. */
      const hd='<div class="entry-hd"><b>'+esc(entryTitle(it,i))+'</b>'+
        '<span class="sub">'+esc(entrySub(it))+'</span>'+
        '<button class="mini rm" data-rm="'+esc(name)+'" data-i="'+i+
        '" title="Remove this entry" aria-label="Remove '+esc(entryTitle(it,i))+
        '">&#10005;</button></div>';
      const body=(it===null||typeof it!=="object")
        ? '<div class="fg wide">'+
            fieldRow("text",["cv","sections",name,i],it,{multi:true})+'</div>'
        : '<div class="fg wide">'+Object.keys(it).map(k=>
            fieldRow(k,["cv","sections",name,i,k],it[k],{mono:MONO_KEYS.test(k)})).join("")+
          '</div>';
      h+='<div class="entry formblock'+selMark({kind:"entry",name:name,i:i})+
        '" data-block="'+esc(name)+'" data-bi="'+i+'">'+hd+body+'</div>';
    });
    h+='<div class="addrow"><button class="obtn" data-add="'+esc(name)+
      '">Add to '+esc(sectionLabel(name))+'</button></div>';
    h+='</div></details>';
  }
  h+='<div class="addrow end"><button class="obtn" id="add-section">'+
    'Add a section</button></div>';
  $("#pane-form").innerHTML=h;
  /* The form's multiline fields ship at rows="3" and never grew, so a summary
     of four lines showed three and a half and the last one was cut through
     the middle of the letters. The inspector has always grown its bullets;
     the form simply never asked. */
  $$("#pane-form textarea").forEach(autoGrow);
  /* Each label names the field after it, for a screen reader and for a
     click on the label: they were siblings with nothing tying them. */
  $$("#pane-form .fg>label").forEach((l,i)=>{
    const f=l.nextElementSibling;
    if(!f||!/^(INPUT|TEXTAREA|SELECT)$/.test(f.tagName)||l.htmlFor) return;
    f.id=f.id||"pf-"+i; l.htmlFor=f.id;
  });
  revealSelected($("#pane-form"));
  $$("#pane-form [data-add]").forEach(b=>b.onclick=()=>addEntry(b.dataset.add));
  $$("#pane-form [data-rm]").forEach(b=>b.onclick=()=>removeEntry(b.dataset.rm,+b.dataset.i));
  $("#add-section").onclick=addSectionSheet;
}
/* ---- adding and removing whole entries -----------------------------------
   Structure cannot go through the working copy the way a field edit does. A
   patch names a path that is already in the document, and a new entry is by
   definition a path that is not, so this asks the server in as many words and
   takes back the file it wrote. Whatever is in the form travels with it -- see
   save(ops) -- or the reload would hand back the document as it stood before
   the last few keystrokes. */
async function addEntry(name){
  const list=((S.data&&S.data.cv&&S.data.cv.sections||{})[name])||[];
  const blank=blankLike(list);
  /* Neighbours to copy: adding is a button, not a questionnaire. Only an empty
     section has nothing to go on, and then there is a real question to ask. */
  if(blank===null)
    return typeSheet("Add to "+sectionLabel(name),
      "This section is empty, so there is nothing to copy the shape of.",
      null,(make)=>putEntry(name,list.length,make()));
  putEntry(name,list.length,blank);
}
async function putEntry(name,at,value){
  await save([{op:"add_entry",section:name,at:at,value:value}]);
  select({kind:"entry",name:name,i:at});
  /* Straight into the first field: you pressed Add because you have something
     to type, and a blank entry with the caret somewhere else is a second
     thing to do. */
  const first=$("#pane-form .formblock.on input,#pane-form .formblock.on textarea");
  if(first){ first.focus(); first.select&&first.select() }
}
async function removeEntry(name,at){
  const list=((S.data&&S.data.cv&&S.data.cv.sections||{})[name])||[];
  if(!list.length) return;
  const last=list.length===1;
  if(!await askConfirm({title:t("Remove “{e}” from {s}?",{e:entryTitle(list[at],at),s:sectionLabel(name)}),
    body:(last?t("It is the only entry, so the section goes with it: an empty section cannot be laid out.")+" ":"")+
      t("This is written to the file straight away."),ok:t("Remove"),danger:true}))
    return;
  await save([{op:"remove_entry",section:name,at:at}]);
  const left=(((S.data&&S.data.cv&&S.data.cv.sections)||{})[name])||[];
  select(left.length?{kind:"entry",name:name,i:Math.min(at,left.length-1)}:null);
}
function addSectionSheet(){
  typeSheet("New section",
    "A section is a list of one kind of entry -- RenderCV reads its type from "+
    "what is in it -- so it starts with one blank entry of the kind you pick.",
    "certifications",(make,name)=>addSection(name,make()));
}
async function addSection(name,value){
  await save([{op:"add_section",name:name,value:[value]}]);
  showTab("form");
  select({kind:"entry",name:name,i:0});
  const first=$("#pane-form .formblock.on input,#pane-form .formblock.on textarea");
  if(first) first.focus();
}
/* One sheet for both questions. A new section needs a name as well as a kind;
   an empty existing section only needs the kind, so the name row is left out
   rather than shown greyed. */
function typeSheet(title,blurb,namePlaceholder,go){
  const taken=Object.keys((S.data&&S.data.cv&&S.data.cv.sections)||{});
  openSheet(
    '<div><h3 id="sheet-title">'+esc(title)+'</h3><p>'+esc(blurb)+'</p></div>'+
    '<div class="fg w88">'+
      (namePlaceholder?'<label>Name</label><input id="ts-name" autocomplete="off" '+
        'placeholder="'+esc(namePlaceholder)+'">':"")+
      '<label>Entries</label><select id="ts-type">'+
        ENTRY_TYPES.map(([id,label,hint])=>'<option value="'+esc(id)+'">'+
          esc(label)+' \u2014 '+esc(hint)+'</option>').join("")+
      '</select>'+
    '</div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="ts-go">Add</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#ts-go").onclick=()=>{
    const make=(ENTRY_TYPES.find(t=>t[0]===$("#ts-type").value)||[])[3];
    if(!make) return;
    let name=null;
    if(namePlaceholder){
      /* A section name is a YAML key and a heading at once. Fold it to the
         shape the rest of the file uses and let sectionLabel put the capital
         back for display, rather than carrying "Certifications & Awards" into
         the source as a key. */
      name=$("#ts-name").value.trim().toLowerCase()
        .replace(/[^a-z0-9]+/g,"_").replace(/^_+|_+$/g,"");
      if(!name) return toast("Give the section a name.",true);
      if(taken.includes(name)) return toast(t("There is already a {s} section.",{s:sectionLabel(name)}),true);
    }
    closeSheet();
    go(make,name);
  };
}

/* Put the caret in a field and the page goes to that entry: highlights it, and
   scrolls to it if it had drifted off. This is what the split is for -- the
   two halves are one document, not a form and a picture of a form. It replaced
   a "Show on page" button, which made sense when the page was a tab away and
   read as furniture once it was sitting right there.

   focusin rather than click: the caret arrives by Tab as often as by mouse. */
$("#pane-form").addEventListener("focusin",e=>{
  const block=e.target.closest(".formblock");
  if(!block||!block.dataset.block) return;
  const at={kind:"entry",name:block.dataset.block,i:+block.dataset.bi};
  if(sameSel(at,S.sel)) return;
  select(at,"form");
});
bindFields($("#pane-form"));
bindFields($("#insp-body"));

