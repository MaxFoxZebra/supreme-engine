/* AI clients panel, provenance marks, and workspace change polling. */
/* ---- Claude -------------------------------------------------------------
   The MCP server is this same program in another process, started and owned by
   Claude Desktop, so the app cannot talk to it. What the two halves do share is
   the workspace folder and Claude's config file, and those answer the only two
   questions worth asking: is it wired up, and what has it been doing. */
const AI_STATE={
  connected:"Connected to this workspace.",
  absent:"Not set up yet. One click adds it to the config.",
  elsewhere:"Configured, but pointing at a different copy of CV Studio.",
  "other-workspace":"Configured, but pointing at a different workspace.",
  unreadable:"The config file could not be read.",
  extension:"Installed as a Claude Desktop extension.",
  unknown:"Checking…",
};
const aiClient=id=>(S.ai||[]).find(c=>c.id===id)||null;
/* Every tool call now says which client made it, so naming one is a lookup
   rather than a deduction. loneClient stays as the answer for a call recorded
   before any of this existed, and for a client that names itself something
   nobody here recognises. */
function loneClient(){
  const on=(S.ai||[]).filter(c=>c.state==="connected"||c.state==="extension");
  return on.length===1?on[0].label:null;
}
function clientName(entry){
  if(entry&&entry.by&&entry.by!=="ai"){
    const c=aiClient(entry.by);
    if(c) return c.label;
  }
  return (entry&&entry.agent)||loneClient()||"An AI client";
}

async function loadAI(){
  try{ S.ai=(await api("/api/ai")).clients }
  catch(e){ S.ai=null }
  paintAI();
}
function paintAI(){
  const bits=[];
  $$("#btn-ai .aic").forEach(el=>{
    const c=aiClient(el.dataset.client), st=(c&&c.state)||"unknown";
    el.dataset.state=st;
    bits.push((c?c.label:el.dataset.client)+": "+
      (c&&(st==="connected"||st==="extension")&&c.last_seen
        ? t("Connected. Last heard from {when} ago.",{when:ago(c.last_seen*1000)})
        : c&&st==="connected" ? t("Set up, but it has not called in yet.")
        : t(AI_STATE[st])));
  });
  $("#btn-ai").title=bits.join("\n");
  if(!$("#ovl-settings").hidden) fillAIPanel();
}
/* The marks in the title bar are the AI surface's own entry point, so they go
   straight to it rather than opening Settings and then navigating. */
$("#btn-ai").onclick=()=>openSettings("ai");

/* A short label for the pill, and a single line of plain English under the
   name. The long version of any of this belongs in the title, not the card. */
const AI_PILL={
  connected:"Connected", absent:"Not set up", elsewhere:"Another copy",
  "other-workspace":"Another workspace", unreadable:"Unreadable",
  extension:"Extension", unknown:"Checking",
};
/* The client last called in on a connector of another version: it has not
   restarted since the update, or it reads a config the update did not reach.
   Its tools still answer, so nothing else here would say so, and the CV page
   in the chat is the first thing an old connector lacks. */
const aiStale=c=>c.state==="connected"&&c.last_seen&&c.stale;
/* "Configured" and "Connected" are different claims and the card should not
   make the second on the strength of the first. */
const aiPill=c=>aiStale(c)?"Restart needed":c.state==="connected"&&!c.last_seen?"Configured":AI_PILL[c.state];
function aiSay(c){
  /* Installed in Claude Desktop as an extension: Claude Desktop starts it and
     updates it, and the app has nothing to write. */
  if(c.state==="extension"){
    if(c.also_in_config)
      return t("It is also in Claude Desktop's config file, so Claude sees every tool twice. Remove the cv-studio entry from the config.");
    if(c.other_workspace)
      return t("Installed as a Claude Desktop extension, pointing at {path}, so it is editing CVs you are not looking at. Change its folder in Claude Desktop's Extensions settings.",{path:shortPath(c.workspace)});
    return c.last_seen
      ? t("Installed as a Claude Desktop extension, which keeps itself up to date. Last heard from {when} ago.",{when:ago(c.last_seen*1000)})
      : t("Installed as a Claude Desktop extension, which keeps itself up to date.");
  }
  if(c.state==="connected"){
    /* A config file says a client has been *told* where the server is, not
       that it ever started it. A tool call is the only evidence the handshake
       actually happened, so the card reports that instead of implying it. */
    if(aiStale(c))
      return (c.connector_version
        ? t("{c} is still running the connector from CV Studio {v}, not this version's.",{c:c.label,v:c.connector_version})
        : t("{c} is still running a connector from an earlier version of CV Studio.",{c:c.label}))+" "+
        (c.id==="claude"
          ? t("Quit Claude Desktop completely (on Windows, right-click its icon by the clock and choose Quit), then open it again.")
          : tx(c.restart));
    if(c.last_seen)
      return "Last heard from "+ago(c.last_seen*1000)+" ago"+
        (c.agent&&c.agent!==c.label?" ("+c.agent+")":"")+".";
    return "Set up, but it has not called in yet. Restart it: "+
      c.restart.replace(/^Restart /,"restart ").replace(/^Start /,"start ");
  }
  /* Never "one click". The click writes a config file; the client only picks
     it up when it is restarted, and until then nothing is connected. Promising
     one click and then putting the step that completes it in a toast -- the
     most disposable container in the app -- is most of why this felt clunky. */
  if(c.state==="absent") return "";
  if(c.state==="elsewhere") return "Pointing at another copy of CV Studio, so "+
    "it is editing CVs you are not looking at.";
  if(c.state==="other-workspace")
    return "Pointing at "+shortPath(c.workspace)+", so it is editing CVs you "+
      "are not looking at.";
  if(c.state==="unreadable") return c.error||"Its config file could not be read.";
  return "Checking…";
}
/* The two steps, on the card, before the first click rather than after it.
   Step 2 is the one that actually connects anything, and it used to exist only
   in a toast that fired once and vanished. */
function aiSteps(c){
  if(c.state==="unreadable"||c.state==="extension") return "";
  const wrote=c.state==="connected";
  const live=wrote&&c.last_seen;
  const step=(n,done,text)=>'<li'+(done?' class="done"':"")+'><i>'+
    (done?"&#10003;":n)+'</i><span>'+text+'</span></li>';
  return '<ol class="aisteps">'+
    step(1,wrote,wrote?"Added to its config":"Add this workspace to its config")+
    step(2,live,esc(c.restart))+
    /* Only a client that has been configured is actually waiting on anything.
       One that was never set up is not pending, it is untouched. */
    '<li'+(live?' class="done"':wrote?' class="wait"':"")+'><i>'+(live?"&#10003;":"3")+
      '</i><span>'+(live
        ? "Heard from it "+ago(c.last_seen*1000)+" ago"
        : wrote ? "Waiting for its first call\u2026"
                : "It calls in, and this turns green")+'</span></li>'+
    '</ol>';
}

/* Paths here are long enough to swallow the card, and the end is the part that
   identifies them, so keep the tail and let CSS trim the head. */
function shortPath(p){
  const bits=String(p||"").split(/[\\/]/).filter(Boolean);
  return bits.length<=2?String(p||""):"…/"+bits.slice(-2).join("/");
}

function fillAIPanel(){
  const clients=S.ai||[];
  $("#s-ai-clients").innerHTML=clients.map(c=>{
    const st=c.state;
    const wrong=st==="elsewhere"||st==="other-workspace"||st==="unreadable"||aiStale(c)||
      (st==="extension"&&(c.also_in_config||c.other_workspace));
    return '<div class="client'+(wrong?" wrong":"")+'" data-client="'+c.id+'">'+
      '<span class="badge"><svg width="19" height="19" viewBox="0 0 24 24"'+
        ' aria-hidden="true"><use href="#'+c.id+'-mark"/></svg></span>'+
      '<div class="who"><b>'+esc(c.label)+'</b>'+
        '<span class="pill" data-state="'+(aiStale(c)?"stale":st==="connected"&&!c.last_seen?"unknown":st)+
          '"><i></i>'+aiPill(c)+'</span></div>'+
      '<div class="say">'+esc(aiSay(c))+'</div>'+
      (st==="extension"?"":'<div class="go"><button class="obtn" data-connect="'+c.id+'">'+
        (st==="connected"?"Set up again"
          :st==="absent"?"Add to its config":"Point it at this workspace")+
        '</button></div>')+
      aiSteps(c)+
      '<div class="path"><span title="'+esc(c.config_path)+'">'+
        esc(shortPath(c.config_path))+'</span>'+
        '<button data-copy-path="'+esc(c.config_path)+
        '" title="Copy the full path">Copy path</button>'+
      '</div></div>';
  }).join("")||'<p class="sp-note">Checking…</p>';

  $$("#s-ai-clients [data-connect]").forEach(b=>b.onclick=async()=>{
    const c=aiClient(b.dataset.connect);
    b.disabled=true; b.textContent="Setting up…";
    try{
      const r=await post("/api/ai/connect",{client:b.dataset.connect});
      await loadAI();
      const sk=r.skills&&r.skills.installed&&r.skills.installed.length
        ? " "+t("Its skills are installed too.") : "";
      toast((r.action==="unchanged" ? t("Already set up.")
        : t("{c} is connected.",{c:c?c.label:"CV Studio"})+" "+tx(r.restart||""))+sk);
    }catch(e){ toast(e.message,true); await loadAI() }
  });
  /* Not a reveal: these files live outside the workspace, and /api/reveal is
     deliberately confined to it. The path itself is the useful thing. */
  $$("#s-ai-clients [data-copy-path]").forEach(b=>b.onclick=async()=>{
    try{ await navigator.clipboard.writeText(b.dataset.copyPath); toast("Copied") }
    catch(e){ toast("Select the path and copy manually",true) }
  });

  $("#s-ai-manual").innerHTML=clients.map(c=>
    '<h4>'+esc(c.label)+'</h4><p class="sp-note" style="margin:0 0 8px">'+
    esc(c.manual)+' Put this in <code>'+esc(c.config_path)+'</code>, then '+
    esc(c.restart)+'</p><pre class="code">'+esc(c.snippet)+'</pre>').join("");
  paintAILog();
  loadSkills();
}
/* The skills ship with the app. Claude Desktop takes them as one plugin, from
   the repository or a file; the clients that read a skills folder get them
   copied there, by Connect or by the buttons here. */
async function loadSkills(){
  try{ S.skills=await api("/api/skills") }catch(e){ S.skills=null }
  paintSkills();
}
function paintSkills(){
  const d=S.skills||{}, list=d.skills||[];
  $("#s-skills").innerHTML=list.length
    /* Their names and descriptions are the skills' own words, in English
       like the skills themselves: not interface text to translate. */
    ? list.map(k=>'<div data-noi18n><span class="nm">'+esc(k.name)+'</span>'+
        '<span class="ds" title="'+esc(k.description)+'">'+esc(k.description)+'</span></div>').join("")
    : '<div><span class="ds">This build has no skills.</span></div>';
  const pack=$("#s-skill-pack"), show=$("#s-skill-show");
  show.hidden=!d.plugin;
  pack.disabled=!list.length;
  pack.textContent=d.plugin?"Make it again":"Make the plugin file";
  pack.onclick=async()=>{
    pack.disabled=true;
    try{
      const r=await post("/api/skills/package",{});
      await loadSkills();
      toast(t("{file} is ready in {dir}. Upload it in Claude.",{file:"cv-studio.plugin",dir:r.dir}));
    }catch(e){ toast(e.message,true); await loadSkills() }
  };
  show.onclick=async()=>{
    try{ await post("/api/reveal",{path:d.plugin||d.out_dir||""}) }
    catch(e){ toast(e.message,true) }
  };
  $("#s-mk-copy").onclick=async()=>{
    try{ await navigator.clipboard.writeText(d.marketplace||$("#s-mk").textContent); toast("Copied") }
    catch(e){ toast("Select the path and copy manually",true) }
  };
  const SAY={installed:"Installed",outdated:"An older version",absent:"Not installed"};
  const DO={installed:"Install again",outdated:"Update",absent:"Install"};
  $("#s-skill-clients").innerHTML=Object.entries(d.clients||{}).map(([id,c])=>
    '<div><span class="nm">'+esc((aiClient(id)||{}).label||id)+'</span>'+
    '<span class="ds"><span>'+SAY[c.state]+'</span>'+
    ' <span class="mono" title="'+esc(c.dir)+'" data-noi18n>'+esc(shortPath(c.dir))+'</span></span>'+
    '<button class="obtn" data-skills-for="'+esc(id)+'">'+DO[c.state]+'</button></div>').join("");
  $$("#s-skill-clients [data-skills-for]").forEach(b=>b.onclick=async()=>{
    const id=b.dataset.skillsFor;
    b.disabled=true;
    try{
      const r=await post("/api/skills/install",{client:id});
      await loadSkills();
      toast(r.kept_yours&&r.kept_yours.length
        ? t("Installed. Left your own {names} as they were.",{names:r.kept_yours.join(", ")})
        : t("Skills installed for {c}.",{c:(aiClient(id)||{}).label||id}));
    }catch(e){ toast(e.message,true); await loadSkills() }
  });
}
/* Which tool calls changed something. The prose above this log warns that
   these write immediately and there is no undo, so the log has to tell the two
   kinds apart rather than styling a read like a write. */
const WRITE_TOOLS=/^(write_cv|edit_cv_fields|create_cv|set_company_logo|set_job_status|update_job_tracking|add_job)$/;

/* Kept apart from the rest of the panel so the poll can refresh it without
   rebuilding the buttons under the cursor. */
function paintAILog(){
  const log=(S.pulse&&S.pulse.mcp&&S.pulse.mcp.recent)||[];
  $("#s-cl-log").innerHTML=log.length
    /* Only the calls that wrote something carry the mark. Putting it on
       read_cv and list_cvs made the glyph mean "a client called a tool", which
       is not what it means anywhere else in the app, and with one client
       connected the column was constant anyway. */
    ? log.map(r=>{
        const wrote=WRITE_TOOLS.test(r.tool);
        return '<div'+(r.ok===false?' class="no"':"")+'>'+
        (wrote ? markHTML({by:r.by||"ai",at:r.at,agent:r.agent},null)
               : '<i class="roi" aria-hidden="true"></i>')+
        '<span class="t">'+(r.ok===false?"refused ":"")+esc(r.tool)+'</span>'+
        '<span class="p">'+esc(r.path||"")+'</span>'+
        '<span class="w" title="'+esc(clientName(r))+'">'+
        ago(r.at*1000)+' ago</span></div>';
      }).join("")
    : '<div><span class="none">Nothing yet. What a model does in this workspace '+
      'shows up here.</span></div>';
}

/* ---- provenance ---------------------------------------------------------
   Who last wrote each field, and which fields no longer say what the base CV
   says. Both arrive on the document itself, from a sidecar the server keeps;
   neither is in the YAML, so neither can reach the rendered page.

   Field addresses are the dotted form of the same path the inspector already
   binds its inputs to, so a mark is a lookup rather than a search. */
const PROV_LABEL={claude:"Claude",openai:"OpenAI",mistral:"Mistral",
  hermes:"Hermes",gemini:"Gemini",ai:"An AI client",you:"You"};
const provKey=path=>path.join(".");
function provOf(path){
  const f=S.prov&&S.prov.fields;
  return (f&&f[provKey(path)])||null;
}
function fromBase(path){
  return !!(S.prov&&S.prov.baseSet&&S.prov.baseSet.has(provKey(path)));
}
/* True when anything *under* this path has been touched, which is what an
   outline row needs: a section is marked because one of its bullets was. */
function provUnder(prefix){
  const f=S.prov&&S.prov.fields;
  if(!f) return null;
  const head=provKey(prefix)+".";
  let best=null;
  for(const k in f){
    if(k!==provKey(prefix)&&k.indexOf(head)!==0) continue;
    if(f[k].by==="you") continue;
    if(!best||(f[k].at||0)>(best.at||0)) best=f[k];
  }
  return best;
}
function provHeader(){
  let best=null;
  for(const k of HEADER_KEYS){
    const p=provOf(["cv",k]);
    if(p&&p.by!=="you"&&(!best||(p.at||0)>(best.at||0))) best=p;
  }
  return best;
}
function baseHeader(){
  return HEADER_KEYS.some(k=>fromBase(["cv",k]));
}
function baseUnder(prefix){
  const set=S.prov&&S.prov.baseSet;
  if(!set) return false;
  const head=provKey(prefix)+".";
  for(const k of set) if(k===provKey(prefix)||k.indexOf(head)===0) return true;
  return false;
}
function whoLabel(p){
  return (p&&(PROV_LABEL[p.by]||p.agent||"An AI client"))||"";
}
/* The mark itself. Your own edits draw nothing: the whole point is to pick out
   what you did not write, and marking everything marks nothing. */
/* `rolled` means this mark stands for something underneath rather than for the
   field it sits on: a section is marked because one of its bullets was. The
   distinction matters because the two are not the same claim -- a rolled-up
   block usually also contains your own writing -- and drawing them identically
   made the feature true at the leaf and wrong at every level above it. So a
   rolled mark is hollow and quieter, and says "contains" rather than "changed
   this". */
function markHTML(p,path,rolled){
  let out="";
  if(p&&p.by&&p.by!=="you"){
    const who=whoLabel(p);
    const said=rolled
      ? who+" wrote something in here, "+ago(p.at*1000)+" ago"
      : who+" changed this "+ago(p.at*1000)+" ago"+
        (p.from==null?"":"\nwas: "+String(p.from));
    out+='<span class="pmark'+(rolled?" rolled":"")+'" data-by="'+esc(p.by)+
      '" role="img" aria-label="'+esc(said)+'" title="'+esc(said)+
      '">'+(p.by==="ai"?"&#9679;":
        '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#'+
        esc(p.by)+'-mark"/></svg>')+'</span>';
  }
  if(path&&fromBase(path))
    out+=basebar();
  return out;
}
/* Announced, not just hovered: the rule is the only carrier of its meaning, so
   leaving it as an empty <i> told a screen reader nothing at all. */
const basebar=()=>'<i class="fromb" role="img" aria-label="Differs from '+
  esc(baseName())+'" title="Differs from '+esc(baseName())+'"></i>';
function valueText(v){
  if(v==null) return "(empty)";
  if(Array.isArray(v)) return v.join(" · ");
  if(typeof v==="object") return JSON.stringify(v);
  return String(v);
}
const baseName=()=>{
  const b=S.prov&&S.prov.base;
  return b?b.path.split("/").pop().replace(/\.ya?ml$/,""):"the base CV";
};

function setProv(prov){
  S.prov=prov||null;
  if(S.prov) S.prov.baseSet=new Set(S.prov.from_base||[]);
  paintProv();
  cvRvBar();
  paintDiffTab();
  loadDiff();
}

/* The base says so. A tailored copy has always said what it came from; the
   document it came from said nothing, so the one CV every other one is copied
   from looked like any other file while you edited it. */
const isBase=p=>!!(p&&S.state&&S.state.base&&S.state.base.path===p);
function paintBaseChip(){
  const chip=$("#basechip"), tb=$("#btn-tailor");
  if(tb){ tb.hidden=!isBase(S.path); tb.onclick=()=>newDocumentSheet() }
  if(!chip) return;
  if(!isBase(S.path)){ chip.hidden=true; return }
  const n=((S.state&&S.state.documents)||[]).filter(d=>d.base===S.path).length;
  chip.innerHTML='<span class="btag">Base CV</span>'+
    '<span>'+(n?'<b>'+n+'</b> tailored from it':'every tailored CV starts as a copy of it')+
    '</span>';
  chip.title="Changes here reach the next CV you tailor, not the ones already "+
    "copied. Click to see every document.";
  chip.hidden=false;
  chip.onclick=async ()=>{
    if(!await leaveEdits()) return;
    setView("docs");
  };
}

/* The chip: the whole document's answer, on every tab. */
/* One chip for the whole document, beside the provenance one, rather than a
   card repeated inside every block's editor. */
function paintLink(){
  const chip=$("#linkchip"), j=linkedJob();
  /* The same answer the chip is about to draw decides where Back goes, and it
     is only knowable once the document and the applications have both landed
     -- which is here, not in setView. */
  paintBackLabel();
  paintBaseChip();
  if(!S.path||!S.jready){ chip.hidden=true; return }
  /* The base CV is what tailored copies start from; it is not sent itself. */
  if(!j&&isBase(S.path)){ chip.hidden=true; return }
  if(!j){
    chip.innerHTML='<span>Link to an application</span>';
    chip.title="Attach this document to the application it was written for.";
    chip.hidden=false;
    chip.onclick=()=>linkJobSheet();
    return;
  }
  chip.innerHTML='<span class="dot '+statusTone(j.status)+'"></span>'+
    '<span>for <b>'+esc(j.company)+'</b></span>'+
    '<span class="dot"></span><span>'+esc(prettyStatus(j.status))+'</span>'+
    (sentAt(j)?'<span class="dot"></span><span>'+esc(t("sent {d}",{d:shortDate(sentAt(j))}))+'</span>':'');
  chip.title="Linked to an application. Click to open it, move it or unlink.";
  chip.hidden=false;
  chip.onclick=()=>linkJobSheet();
}

function paintProv(){
  const chip=$("#provchip"), pv=S.prov;
  if(!pv||(!pv.base&&!pv.last_ai)){ chip.hidden=true; return }
  const bits=[];
  if(pv.base)
    bits.push('<span>from <b>'+esc(baseName())+'</b></span>',
      '<span class="dot"></span>',
      /* The logo teaches itself by sitting next to the word "Claude". The
         divergence rule had no such anchor anywhere in the product, so it gets
         one here: this is the only place both marks appear beside the words
         that define them. */
      '<span><i class="fromb"></i> '+esc(pv.from_base.length===1?t("1 field differs from the base")
        :t("{n} fields differ from the base",{n:pv.from_base.length}))+'</span>');
  if(pv.last_ai){
    if(bits.length) bits.push('<span class="dot"></span>');
    bits.push(markHTML(pv.last_ai,null,true)+'<span>'+esc(whoLabel(pv.last_ai))+', '+
      ago(pv.last_ai.at*1000)+' ago</span>');
  }
  chip.innerHTML=bits.join("");
  chip.title="What this document owes to something other than your own typing";
  chip.hidden=false;
}
$("#provchip").onclick=()=>provSheet();

/* The long answer. Every field that differs from the base, and every field an
   AI client wrote, as the names you are actually looking at rather than as
   paths. Clicking one selects it, which is the point of listing them. */
function provSheet(){
  const pv=S.prov||{};
  const seen=new Set(), rows=[];
  const add=key=>{
    if(seen.has(key)) return;
    seen.add(key);
    const path=key.split(".").map(k=>/^\d+$/.test(k)?+k:k);
    const p=(pv.fields||{})[key];
    rows.push({key,path,p});
  };
  (pv.from_base||[]).forEach(add);
  Object.keys(pv.fields||{}).forEach(k=>{ if(pv.fields[k].by!=="you") add(k) });
  rows.sort((a,b)=>((b.p&&b.p.at)||0)-((a.p&&a.p.at)||0));

  /* A row carries both facts, because they are independent: a field can be
     Claude's and match the base, or yours and differ from it. Showing only
     whichever one happened to be true first hid the divergence rule from the
     one screen whose job is to explain it. */
  const body=rows.length?rows.map(r=>{
    const now=getAt(S.data,r.path);
    const was=r.p&&r.p.from!=null?String(r.p.from):null;
    const who=r.p?markHTML(r.p,null)+esc(whoLabel(r.p))+", "+ago(r.p.at*1000)+" ago"
                 :'<span class="mine">your own edit</span>';
    return '<button class="r" type="button" data-sel="'+
        esc(JSON.stringify(r.path))+'">'+
      '<span class="w">'+who+
        (pv.baseSet&&pv.baseSet.has(r.key)?basebar()+'<span>differs</span>':"")+
      '</span>'+
      '<span class="f"><b>'+esc(fieldLabel(r.path.slice(1),S.data))+'</b>'+
      /* The new value first and in full weight. Showing only the struck-out
         old one answered "what did it used to say", which is not the question
         the list is titled after. */
      '<em>'+esc(valueText(now))+'</em>'+
      (was!==null&&was!==""&&was!==valueText(now)?'<s>'+esc(was)+'</s>':"")+
      '</span><span class="go" aria-hidden="true">&#8250;</span></button>';
  }).join("")
    :'<div class="none">Nothing but your own typing.</div>';

  openSheet('<div><h3 id="sheet-title">What is not your own typing</h3><p>'+
    (pv.base?'Tailored from <b>'+esc(baseName())+'</b>'+
      (pv.base.missing?', which is no longer there, so the comparison is '+
        'missing and only the edits below are shown.':'. ')
      :'')+
    'None of this is written into the YAML, so none of it prints.</p></div>'+
    '<div class="provlist">'+body+'</div>'+
    '<div class="foot">'+(hasBase()?'<button class="sbtn" data-df-tab>'+esc(t("See every change beside the page"))+'</button>':'')+
      '<div class="grow"></div><button class="sbtn primary" data-cancel>Close</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  const dt=$("#sheet [data-df-tab]");
  if(dt) dt.onclick=()=>{ closeSheet(); showTab("diff") };
  $$("#sheet [data-sel]").forEach(el=>el.onclick=()=>{
    const path=JSON.parse(el.dataset.sel);
    if(path[1]==="sections"&&path.length>3)
      select({kind:"entry",name:path[2],i:+path[3]});
    else select({kind:"header"});
    /* The sheet stays open. Closing it after every row made the list a
       one-shot: you could go to one change, and then you were back where you
       started with nothing to compare against. */
  });
}

/* Every block of the document, in the order it renders, so the arrows mean
   "the one after this" rather than "the next thing in some map". */
function blockOrder(){
  const cv=S.data&&S.data.cv;
  if(!cv) return [];
  const out=[{kind:"header"}];
  for(const name of Object.keys(cv.sections||{})){
    const list=cv.sections[name]||[];
    if(!list.length) out.push({kind:"section",name:name});
    else list.forEach((_,i)=>out.push({kind:"entry",name:name,i:i}));
  }
  return out;
}
const sameSel=(a,b)=>!!a&&!!b&&a.kind===b.kind&&a.name===b.name&&a.i===b.i;
function editorStep(delta){
  const all=blockOrder();
  const at=all.findIndex(x=>sameSel(x,S.sel));
  const next=all[at+delta];
  if(at<0||!next) return;
  select(next);
  if($("#ed").hidden) openEditor();
}
$("#ed-prev").onclick=()=>editorStep(-1);
$("#ed-next").onclick=()=>editorStep(1);
$("#ed-close").onclick=closeEditor;

/* Dismissal. A click inside the editor, on a block, or on the outline is not
   a dismissal -- those are all ways of carrying on editing. */
document.addEventListener("pointerdown",e=>{
  if($("#ed").hidden||S.view!=="cvs") return;
  if(e.target.closest("#ed,.hit,#outline,#doclist,#edtabs")) return;
  closeEditor();
});

$("#pane-page").addEventListener("scroll",()=>{
  /* Fixed to the window, so scrolling the sheet moves the block out from under
     it; this walks the card back alongside. */
  if(!$("#ed").hidden) placeEditor();
});

/* ---- the workspace changing underneath us -------------------------------
   Claude edits the same files this app has open, so the editor has to assume
   it is not the only writer. Polling one stat per document is cheap, and it is
   the difference between picking up the model's work and silently saving over
   it. */
async function pulse(){
  if(document.hidden) return;
  let p;
  try{ p=await api("/api/pulse") }catch(e){ return }
  const before=S.pulse;
  S.pulse=p;
  paintStatus();
  if(!$("#ovl-settings").hidden&&!$("#sp-ai").hidden) paintAILog();
  if(!before) return;

  /* An application changed in another process, which means an AI client moved
     a status while the table was open. Reload rather than leaving it stale:
     without this the row still reads "applied" until the user navigates. */
  if(before.jobs!==p.jobs){
    loadJobs(true);
    loadAlerts();
  }
  /* A morning search left new jobs in the scans folder. */
  if(before.scans!==p.scans) loadScans();

  /* A new mark without a new file: an AI client can change the base CV this
     one is compared against, which moves what "differs from the base" means
     here without touching this file at all. */
  /* Changes waiting on review: the badges everywhere, and the bar over the
     open document, whether it was a write or a review somewhere else. */
  if(before.review!==p.review){
    refreshDocBadges();
    try{
      if(S.view==="letter"&&LT.path&&!LT.dirty){
        const d=await api("/api/doc?path="+encodeURIComponent(LT.path));
        if(JSON.stringify(d.review)!==JSON.stringify(LT.doc.review)){ LT.doc.review=d.review; if(LT.rv) ltPaint(); else ltRvBar() }
      }else if(S.path&&S.doc&&!S.dirty){
        const d=await api("/api/doc?path="+encodeURIComponent(S.path));
        S.doc.review=d.review; cvRvBar();
      }
    }catch(e){}
  }
  if(before.edits!==p.edits&&S.path&&!S.dirty){
    try{ const pv=(await api("/api/doc?path="+encodeURIComponent(S.path))).prov;
         keepFocus(()=>{ setProv(pv); buildOutline(); buildInspector() }) }catch(e){}
  }

  /* A document appearing or disappearing means Claude created or removed one.
     The edits stamp covers the base as well, since the nomination lives in the
     same sidecar -- so a base set from an AI client shows up here within a
     poll rather than waiting for a reload. */
  const names=o=>JSON.stringify(Object.keys(o.docs).sort());
  if(names(before)!==names(p)||before.edits!==p.edits){
    try{
      const st=await api("/api/state");
      S.state=st; renderDocs(st.documents); paintBase();
    }catch(e){}
  }
  const bp=S.state&&S.state.base&&S.state.base.path;
  if(bp&&bp!==S.path&&before.docs[bp]!==p.docs[bp]) baseThumb(true);
  if(LT.path&&before.docs[LT.path]!==p.docs[LT.path]) ltExternal(p.docs[LT.path]);
  if(!S.path) return;
  const now=p.docs[S.path];
  if(now===undefined||S.docMtime==null||now<=S.docMtime+1e-6) return;
  if(S.view!=="cvs"){
    if(!S.dirty){ S.docMtime=now; await reopenInPlace() }
    return;
  }
  if(S.dirty){
    /* Fetch their version so the bar can say which fields moved rather than
       just that the file did. Failing that, still warn -- silently losing the
       user's work would be far worse than a vaguer message. */
    let theirs=null;
    try{ theirs=await api("/api/doc?path="+encodeURIComponent(S.path)) }catch(e){}
    showExternalChange(now,theirs);
    return;
  }
  S.docMtime=now;
  await reopenInPlace();
  toast(t("{who} updated this file",{who:t(whoChanged(p))}));
}
/* Which client wrote the file that just moved underneath us. The tool call
   that did it carries its own name now, so this is no longer a guess hedged
   behind "an AI client" the moment two were configured. */
function byAI(p){
  const last=p&&p.mcp&&p.mcp.last;
  return !!last&&(Date.now()/1000-last.at)<20;
}
function whoChanged(p){
  if(!byAI(p)) return "Something else";
  return clientName(p.mcp.last);
}

/* Reload without losing your place: same selection, same page, same zoom. */
async function reopenInPlace(){
  const keep={sel:S.sel, open:S.openSection, page:S.page,
              zoom:S.zoom, zoomAuto:S.zoomAuto, tab:S.tab};
  const path=S.path;
  await openDoc(path);
  if(S.path!==path) return;      /* something else was opened meanwhile */
  S.page=keep.page; S.zoom=keep.zoom; S.zoomAuto=keep.zoomAuto;
  S.openSection=keep.open;
  if(keep.sel) select(keep.sel);
  hideExternalChange();
}

/* What the model actually changed, as field names rather than a file mtime.
   "Something changed" is not enough to choose between your work and its. */
function changedFields(mine,theirs){
  const out=[];
  const walk=(a,b,path)=>{
    if(out.length>6) return;
    const keys=new Set([...Object.keys(a||{}),...Object.keys(b||{})]);
    for(const k of keys){
      const av=(a||{})[k], bv=(b||{})[k];
      const here=path.concat(k);
      const obj=v=>v&&typeof v==="object";
      if(obj(av)&&obj(bv)&&!Array.isArray(av)&&!Array.isArray(bv)) walk(av,bv,here);
      else if(JSON.stringify(av)!==JSON.stringify(bv)) out.push(here);
    }
  };
  walk((mine||{}).cv,(theirs||{}).cv,[]);
  return out;
}
/* "sections.experience.0.company" is precise and unreadable; "Experience ·
   Northwind" is what the user is actually looking at. */
function fieldLabel(path,data){
  if(path[0]==="sections"){
    const [,name,i,key,at]=path;
    const it=(((data||{}).cv||{}).sections||{})[name];
    const entry=it&&it[i];
    const who=entry!==undefined?entryTitle(entry,+i||0):null;
    /* The position inside the list, when there is one. Without it two bullets
       of the same entry produce the same label, and a list of changes shows
       what looks like a duplicated row. */
    const nth=at==null?"":" "+(+at+1);
    return sectionLabel(name)+(who?" · "+who:"")+
      (key?" · "+String(key).replace(/_/g," ")+nth:"");
  }
  return String(path[path.length-1]).replace(/_/g," ");
}
function describeChange(mine,theirs){
  const fields=changedFields(mine,theirs);
  if(!fields.length) return "";
  const names=fields.slice(0,2).map(f=>fieldLabel(f,theirs));
  const rest=fields.length-names.length;
  return names.join(", ")+(rest>0?" and "+rest+" more":"");
}

function showExternalChange(mtime,theirs){
  S.extMtime=mtime;
  S.extTheirs=theirs||null;
  const who=whoChanged(S.pulse);
  const what=theirs?describeChange(S.data,theirs.data):"";
  $("#extbar-msg").innerHTML=esc(who)+" changed "+
    (what?"<b>"+esc(what)+"</b>":"this file")+" while you were editing.";
  $("#extbar").hidden=false;
}
function hideExternalChange(){
  S.extMtime=null; S.extTheirs=null; $("#extbar").hidden=true;
}
/* Taking theirs throws away work you have not saved, so it says so and is the
   quieter of the two. Keeping yours is the one that loses nothing. */
$("#ext-theirs").onclick=async()=>{
  S.dirty=false;
  await reopenInPlace();
  S.resolved={kept:"theirs", who:whoChanged(S.pulse), at:Date.now()};
  paintStatus();
};
$("#ext-keep").onclick=()=>{
  S.docMtime=S.extMtime;
  S.resolved={kept:"mine", who:whoChanged(S.pulse), at:Date.now()};
  hideExternalChange();
  paintStatus();
};

document.addEventListener("visibilitychange",()=>{ if(!document.hidden) pulse() });
window.addEventListener("focus",pulse);

