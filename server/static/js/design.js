/* New document sheet, Design panel, theme previews, and photo uploader. */
/* =========================================================================
   New document
   ========================================================================= */
const slug=s=>String(s||"").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g,"")
  .replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"");
/* "Senior Engineer, Metrics" at Datadog becomes metrics-datadog: the part
   after the last comma is the bit that distinguishes one role from another. */
function derivedName(role,company){
  const tail=String(role||"").split(",").pop();
  const bits=[slug(tail),slug(company)].filter(Boolean);
  return bits.join("-")||"untitled";
}
function newDocumentSheet(){
  const all=(S.state&&S.state.documents)||[];
  const forKind=k=>all.filter(d=>(d.group==="Cover letters")===(k==="letter"));
  openSheet(
    '<div><h3 id="sheet-title">New document</h3><p id="nd-say"></p></div>'+
    '<div class="fg w88">'+
      '<label>Kind</label><div class="seg paper acc" id="nd-kind" role="tablist">'+
        '<button role="tab" data-kind="cv" aria-selected="true">CV</button>'+
        '<button role="tab" data-kind="letter" aria-selected="false">Cover letter</button>'+
      '</div>'+
      '<label>Base on</label><select id="nd-base"></select>'+
      '<label>Company</label><input id="nd-company" autocomplete="off">'+
      '<label>Role</label><input id="nd-role" autocomplete="off">'+
      '<label>File name</label><span id="nd-name" class="mono nd-name" data-noi18n></span>'+
      '<div></div><label class="check"><input type="checkbox" id="nd-draft" checked>'+
        '<i>✓</i>Also track it as a draft application</label>'+
    '</div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="nd-go">Create</button></div>');
  let kind=(S.path&&S.path.startsWith("letters/"))?"letter":"cv";
  /* What you get follows what it is based on, and so does the file name. */
  const sync=()=>{
    const base=$("#nd-base").value, doc=all.find(d=>d.path===base);
    $("#nd-name").textContent=derivedName($("#nd-role").value,$("#nd-company").value)+
      (kind==="letter"&&!/\.ya?ml$/.test(base)?".md":".yaml");
    $("#nd-say").textContent=doc?t("A copy of {doc}, comments and all. The original is untouched.",{doc:doc.label})
      :t(kind==="letter"?"A blank letter to write.":"A blank CV to fill in.");
  };
  /* Basing a letter on a CV produces nonsense, so the list follows the kind.
     The document you have open is the obvious thing to duplicate. */
  const fillBase=()=>{
    /* Tailoring starts from the base CV, so that is the default; the
       document you have open wins when it is of the same kind. */
    const list=forKind(kind), base=S.state&&S.state.base&&S.state.base.path;
    const pick=list.some(d=>d.path===S.path)?S.path:kind==="cv"&&list.some(d=>d.path===base)?base:"";
    $("#nd-base").innerHTML='<option value="">A blank starter</option>'+
      list.map(d=>'<option value="'+esc(d.path)+'"'+
        (d.path===pick?" selected":"")+'>'+esc(docTitle(d))+
        (S.pages[d.path]?" · "+S.pages[d.path]+" page"+(S.pages[d.path]===1?"":"s"):"")+
        '</option>').join("");
  };
  $$("#nd-kind button").forEach(b=>{
    b.setAttribute("aria-selected",String(b.dataset.kind===kind));
    b.onclick=()=>{
      kind=b.dataset.kind;
      $$("#nd-kind button").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
      $("#nd-draft").parentElement.style.opacity=kind==="cv"?"":".5";
      $("#nd-draft").disabled=kind!=="cv";
      fillBase(); sync();
    };
  });
  fillBase();
  $("#nd-company").oninput=sync; $("#nd-role").oninput=sync; $("#nd-base").onchange=sync; sync();
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#nd-go").onclick=async()=>{
    const company=$("#nd-company").value.trim(), role=$("#nd-role").value.trim();
    const name=derivedName(role,company);
    if(name==="untitled") return toast("Give it a company or a role to name it after",true);
    try{
      const r=await post("/api/new",{name,kind,from:$("#nd-base").value||null,
        theme:prefs().theme||null});
      if(kind==="cv"&&$("#nd-draft").checked&&company&&role){
        try{ await post("/api/jobs",{company,title:role,status:"pending",cv_path:r.path}) }
        catch(e){ toast(t("Document created, but the application row failed: {err}",{err:tx(e.message)}),true) }
      }
      closeSheet();
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      await loadJobs(true);
      openDoc(r.path); toast(t("Created {name}",{name}));
    }catch(e){ toast(e.message,true) }
  };
  $("#nd-company").focus();
}

/* =========================================================================
   Design
   ========================================================================= */
/* Thumbnails are drawn rather than screenshotted: a few bars in the shape of
   each theme's real layout. Anything RenderCV adds later falls back to the
   plain single-column sketch instead of showing nothing. */
const THUMBS={
  classic:{bars:[[5,"66%",1],[1,"100%",1],[3,"100%"],[3,"88%"],[3,"94%"],[4,"40%",1,3],
    [3,"92%"]]},
  sb2nov:{centre:true,bars:[[5,"56%",0,0,"#27496d"],[3,"70%"],[6,"100%",0,3,"#e7ecf2"],
    [3,"100%"],[3,"86%"]]},
  engineeringclassic:{bars:[[5,"60%",1],[3,"100%",0,3],[3,"82%"],[3,"96%"]]},
  engineeringresumes:{bars:[[7,"100%",1],[3,"74%",0,3],[3,"92%"]]},
  moderncv:{split:true},
  ember:{bars:[[5,"52%",1,0,"#8a4b2a"],[2,"100%",0,2,"#d8c3b0"],[3,"94%"],
    [3,"80%"]]},
  harvard:{centre:true,bars:[[5,"64%",1],[2,"84%",0,3],[3,"100%"],[3,"90%"]]},
  ink:{bars:[[6,"46%",1],[3,"100%",0,4,"#2b2b2b"],[3,"92%"],[3,"76%"]]},
  opal:{bars:[[5,"58%",1,0,"#2f6f6b"],[3,"88%",0,3],[3,"96%"],[3,"84%"]]},
  _default:{bars:[[5,"60%",1],[3,"100%",0,3],[3,"86%"],[3,"94%"]]},
};
function thumbHTML(theme){
  const t=THUMBS[theme]||THUMBS._default;
  if(t.split) return '<div class="thumb" style="flex-direction:row;gap:7px">'+
    '<div style="width:32%;display:flex;flex-direction:column;gap:4px">'+
    '<i style="height:4px;background:#3f6b4d"></i><i style="height:3px"></i>'+
    '<i style="height:3px"></i></div>'+
    '<div style="flex:1;display:flex;flex-direction:column;gap:4px">'+
    '<i class="ink" style="height:5px;width:80%"></i><i style="height:3px"></i>'+
    '<i style="height:3px"></i></div></div>';
  return '<div class="thumb"'+(t.centre?' style="align-items:center"':"")+'>'+
    t.bars.map(([h,w,ink,mt,bg])=>'<i'+(ink?' class="ink"':"")+' style="height:'+h+
      'px;width:'+w+(mt?';margin-top:'+mt+"px":"")+(bg?";background:"+bg:"")+'"></i>').join("")+
    '</div>';
}
const themeLabel=t=>t.replace(/^engineeringclassic$/,"Engineering")
  .replace(/^engineeringresumes$/,"Compact")
  .replace(/^(.)/,c=>c.toUpperCase());

/* One design for every language of a CV, kept on the source: a design edited
   on a translation would be overwritten the next time the source changed. */
$("#btn-design").onclick=async()=>{
  const fam=S.doc&&S.doc.family;
  if(fam&&fam.source&&fam.source!==S.path){
    if(!await leaveEdits()) return;
    toast(t("Every language of this CV shares one design, so it is edited on {p}.",{p:docLabel(fam.source)}));
    await openDoc(fam.source);
  }
  openDesign();
};
/* What each group is for, in a line. RenderCV's own group names are kept as
   the titles; anything it adds later just goes without a line. */
const DZ_GROUPS={
  photo:"One photo for your CV Studio folder. Each CV shows it or not, so you can leave it off where recruiters expect CVs without one.",
  page:"Paper size, margins, and what prints around the edges.",
  colors:"Every colour on the page. Links and section titles are the ones people notice.",
  typography:"Fonts, sizes and weight for each part of the page.",
  links:"How links look on the page and in the PDF.",
  header:"Your name, headline and contact line at the top of page one. Icons are drawn as pictures, so an ATS reads only the text beside them.",
  section_titles:"How each section heading is drawn.",
  sections:"Spacing between entries, and whether a section may break across pages.",
  entries:"The layout inside each entry: the date column, bullets and spacing.",
  templates:"The text RenderCV fills in. Words in capitals are replaced with your data.",
};
const human=k=>{ const t=String(k).replace(/_/g," "); return t.charAt(0).toUpperCase()+t.slice(1) };
async function openDesign(){
  if(!S.path) return toast("Open a document first");
  $("#ovl-settings").hidden=true; $("#ovl-design").hidden=false;
  DZ.section=DZ.section||"theme";
  paintDesignHead(); paintThemes(); paintEffect();
  await ensureSchema();
  paintAdvanced(); paintDesignNav();
  fillThemePreviews();
}
/* Whose design this is. The base CV's is the one every tailored copy starts
   from, so it says so; any other document's is its own. */
function paintDesignHead(){
  const doc=(S.state.documents||[]).find(d=>d.path===S.path);
  const base=S.state.base&&S.state.base.path===S.path;
  $("#dz-list").textContent=$("#back").textContent;
  $("#dz-doc").textContent=(doc&&doc.label)||S.path.split("/").pop();
  $("#dz-base").hidden=!base;
  $("#dz-note").textContent=base
    ? "Tailored CVs copied from it start with this design"
    : "Only this document. The base CV keeps its own.";
}
$("#dz-list").onclick=()=>{ closeOverlays(); goBack() };
$("#dz-pdf").onclick=()=>$("#btn-pdf").click();
async function ensureSchema(){
  const theme=DZ.theme||(S.state.themes||[])[0];
  if(S.schema&&S.schemaTheme===theme) return;
  try{ S.schema=await api("/api/design-schema?theme="+encodeURIComponent(theme));
       S.schemaTheme=theme }
  catch(e){ S.schema={groups:[]}; S.schemaTheme=theme }
}

/* The theme tiles are this document, rendered in each theme -- not a sample
   CV and not a sketch. The current theme's tile is the live render; the rest
   are rendered one at a time in the background when Design opens, and again
   only once the content has changed since. A drawn sketch holds the place
   until a tile's render lands. */
function thumbKey(){ return S.path+"|"+JSON.stringify(contentPatches()) }
function contentPatches(){
  return collectPatches().filter(p=>p.path[0]!=="design");
}
let thumbRun=0;
async function fillThemePreviews(){
  const run=++thumbRun, key=thumbKey();
  if(!S.thumbs||S.thumbs.key!==key) S.thumbs={key:key,by:{}};
  const cur=DZ.theme||(S.state.themes||[])[0];
  for(const t of (S.state.themes||[])){
    if(run!==thumbRun||$("#ovl-design").hidden||S.thumbs.key!==key) return;
    if(t===cur||S.thumbs.by[t]) continue;
    try{
      const r=await post("/api/theme-preview",{path:S.path,theme:t,patches:contentPatches()});
      if(r.ok&&S.thumbs.key===key){
        S.thumbs.by[t]={png:r.png,pages:r.pages};
        S.themePages[t]=r.pages;
        paintThemes();
      }
    }catch(e){ return }
  }
}
/* Each tile is a theme and, under it, colours to try it in: the theme's own
   first, then four that read well on paper. A colour sets the name, headline,
   contact line, section titles and links together; the wheel opens Colors for
   anything else. "Suits" is a hint about where a theme is usually seen. */
const TH_INK={vivid:"rgb(220, 53, 34)",swiss:"rgb(255, 94, 14)",crisp:"rgb(0, 116, 217)",duo:"rgb(0, 116, 217)",aurora:"rgb(79, 70, 229)",editorial:"rgb(122, 31, 43)",timeline:"rgb(13, 148, 136)",
  studio:"rgb(31, 78, 121)",ledger:"rgb(176, 74, 44)",sidebar:"rgb(31, 111, 107)",
  classic:"rgb(0, 79, 144)",engineeringclassic:"rgb(0, 79, 144)",moderncv:"rgb(0, 79, 144)",
  ember:"rgb(155, 35, 25)",opal:"rgb(0, 100, 90)",ink:"rgb(42, 24, 82)",
  harvard:"rgb(0, 0, 0)",sb2nov:"rgb(0, 0, 0)",engineeringresumes:"rgb(0, 0, 0)"};
const TH_SUITS={vivid:"Tech, engineering, data",swiss:"Almost anywhere, clean and plain",
  crisp:"Product, sales, operations",duo:"Read by people first: referrals, a hiring manager, small companies",aurora:"Tech, product, startups",editorial:"Consulting, law, publishing",
  timeline:"A career with a clear story",studio:"Almost anywhere, with presence",ledger:"Consulting, product, design",
  sidebar:"Read by people: referrals, small companies",classic:"Almost anywhere",ember:"Education, health, non-profits",
  engineeringclassic:"Engineering, science",engineeringresumes:"A long career on one page",
  harvard:"Finance, law, consulting",ink:"Design, media, writing",moderncv:"Academia, research",
  opal:"Tech, product, startups",sb2nov:"Software, US style"};
const SWATCHES=[["Navy","rgb(0, 79, 144)"],["Teal","rgb(0, 100, 90)"],
  ["Burgundy","rgb(122, 31, 43)"],["Graphite","rgb(40, 40, 40)"]];
const INK_KEYS=["name","headline","connections","section_titles","links"];
/* Studio's name sits white on the colour band, so its colour is the band's:
   the section titles. The others keep a dark name beside the accent. */
const inkKeys=t=>["studio","aurora"].includes(t)?["section_titles","links"]
  :["crisp","duo"].includes(t)?["headline"]
  :["sidebar","editorial","timeline","ledger","vivid","swiss"].includes(t)?["headline","section_titles","links"]:INK_KEYS;
/* The colour a theme's swatches set and show as chosen: Crisp's accent is
   its headline (and companies); its section titles stay near-black. */
const accentKey=t=>["crisp","duo"].includes(t)?"headline":"section_titles";
const rgbOf=v=>(/(\d+)\D+(\d+)\D+(\d+)/.exec(v||"")||[0,0,0,0]).slice(1).map(Number);
const near=(a,b)=>{ const x=rgbOf(a),y=rgbOf(b); return Math.max(...x.map((v,i)=>Math.abs(v-y[i])))<40 };
/* What an ATS reads cleanly, applied to a theme you switch to unless the
   document already says otherwise: new CVs carry these already. Icons are
   pictures in every theme, not text, so an ATS has no say in them. */
const ATS_SAFE=[[["header","connections","display_urls_instead_of_usernames"],true],
  [["sections","show_time_spans_in"],[]],[["typography","alignment"],"left"],
  [["page","show_top_note"],false],[["page","show_footer"],false]];
const dzRow=path=>$$("#dz-advanced .dz-row").find(r=>
  r.querySelector("[data-d]").dataset.d===JSON.stringify(path));
function setRow(row,v){
  const el=row.querySelector("[data-d]"), kind=el.dataset.kind;
  if(kind==="color"){ el.value=rgb2hex(v); const sp=row.querySelector(".hex"); if(sp) sp.textContent=v }
  else if(kind==="bool") el.checked=!!v;
  else if(kind==="list") el.value=(v||[]).join(", ");
  else el.value=v==null?"":v;
}
function inkNow(theme){
  const r=dzRow(["colors",accentKey(theme)]);
  return r&&S.schemaTheme===theme?rowValue(r):TH_INK[theme]||"";
}
/* The icons in the contact line go with you to the next theme, whichever
   way they were. Left to each theme's own default, picking Timeline after
   Vivid took them off a CV that had them, and nothing on screen said they
   were a setting rather than something the new theme could not draw. */
const ICONS=["header","connections","show_icons"];
async function pickTheme(theme,ink){
  const cur=DZ.theme||(S.state.themes||[])[0];
  if(theme!==cur){
    const had=dzRow(ICONS), icons=had?rowValue(had):undefined;
    DZ.theme=theme; S.schema=null;
    await ensureSchema(); paintAdvanced();
    const saved=(S.data&&S.data.design)||{};
    ATS_SAFE.forEach(([p,v])=>{ const r=dzRow(p); if(r&&getAt(saved,p)===undefined) setRow(r,v) });
    const ir=dzRow(ICONS); if(ir&&icons!==undefined) setRow(ir,icons);
    setTimeout(fillThemePreviews,0);
  }
  if(ink!==undefined) inkKeys(theme).forEach(k=>{ const r=dzRow(["colors",k]);
    if(r) ink?setRow(r,ink):resetRow(r) });
  markChanged(); paintDesignNav(); paintThemes(); touch();
}
function paintThemes(){
  const themes=(S.state&&S.state.themes)||[];
  const cur=DZ.theme||themes[0];
  const by=(S.thumbs&&S.thumbs.by)||{};
  const live=S.render&&S.render.pngs&&S.render.pngs[0];
  const ink=inkNow(cur);
  $("#themegrid").innerHTML=themes.map(t=>{
    const img=t===cur&&live?live+tok():(by[t]?by[t].png+tok():null);
    const pp=S.themePages[t], own=TH_INK[t];
    const sw=(own?[[t==="harvard"||own==="rgb(0, 0, 0)"?"Black":"Theme colour","",own]]:[])
      .concat(SWATCHES.filter(([,c])=>!own||!near(c,own)).map(([n,c])=>[n,c,c]));
    return '<div class="thcard'+(t===cur?" sel":"")+'">'+
      '<button class="thumbwrap" role="radio" aria-checked="'+String(t===cur)+'" data-theme="'+esc(t)+'">'+
      (img?'<img src="'+esc(img)+'" alt="">':thumbHTML(t))+
      '<span class="thumbcap"><span>'+esc(themeLabel(t))+'</span>'+
      (t==="sidebar"||t==="duo"
        ?'<b class="th-ats warn" title="'+esc("Two columns: parsers can read them out of order, and in one recruiter study cluttered multi-column layouts ranked lowest. Best for a CV handed straight to a person; for anything sent through a job portal, pick a one-column theme.")+'">ATS ~</b>'
        :t==="ledger"
        ?'<b class="th-ats warn" title="'+esc("Most ATS read it in order; some read the column of dates on its own, apart from each job")+'">ATS ~</b>'
        :'<b class="th-ats" title="'+esc("Read in order by both kinds of PDF reader ATS use, in our tests; no icon characters in the text")+'">ATS ✓</b>')+
      '<em>'+(pp?pp+" page"+(pp===1?"":"s"):"")+'</em></span>'+
      (TH_SUITS[t]?'<span class="th-suits">'+esc(TH_SUITS[t])+'</span>':"")+'</button>'+
      '<div class="th-sw" role="group" aria-label="'+esc("Colour")+'">'+
      sw.map(([n,v,c])=>'<button class="sw'+(t===cur&&ink&&near(ink,c)?" on":"")+'" data-t="'+esc(t)+
        '" data-ink="'+esc(v)+'" style="--c:'+c+'" title="'+esc(n)+'" aria-label="'+
        esc(n)+'"></button>').join("")+
      '<button class="sw any" data-t="'+esc(t)+'" data-any="1" title="'+esc("Any colour")+
        '" aria-label="'+"Any colour"+'"></button></div></div>';
  }).join("");
  $$("#themegrid [data-theme]").forEach(b=>b.onclick=()=>{
    if(b.dataset.theme!==cur) pickTheme(b.dataset.theme);
  });
  $$("#themegrid .sw").forEach(b=>b.onclick=async()=>{
    if(b.dataset.any){
      if(b.dataset.t!==cur) await pickTheme(b.dataset.t);
      DZ.section="colors"; paintDesignNav(); showDesignSection(); return;
    }
    pickTheme(b.dataset.t,b.dataset.ink||null);
  });
}

/* Every design option, generated from RenderCV's own schema rather than a
   hand-written list, so it stays correct when RenderCV adds or renames one.
   All groups are built at once and only the chosen one is shown: the patch
   list is read off these inputs, so a group that was never opened still has
   to be there to say what it holds. */
const UNITS=["cm","mm","in","pt","em","px"];
const rgb2hex=v=>{
  const m=/rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)/.exec(v||"");
  if(!m) return (v&&v[0]==="#")?v:"#000000";
  return "#"+[1,2,3].map(i=>(+m[i]).toString(16).padStart(2,"0")).join("");
};
const hex2rgb=h=>{
  const n=parseInt((h||"#000000").slice(1),16);
  return "rgb("+((n>>16)&255)+", "+((n>>8)&255)+", "+(n&255)+")";
};
const splitDim=v=>{
  const m=/^\s*(-?[\d.]+)\s*([a-z%]*)\s*$/i.exec(String(v==null?"":v));
  return m?{n:m[1],u:m[2]||"cm"}:{n:"",u:"cm"};
};
function controlHTML(f,v){
  const dp=esc(JSON.stringify(f.path)), lbl=esc(human(f.path[f.path.length-1]));
  const at=' data-d="'+dp+'" aria-label="'+lbl+'"';
  if(f.kind==="color")
    return '<input type="color"'+at+' data-kind="color" value="'+rgb2hex(v)+
      '"><span class="hex mono">'+esc(String(v==null?"":v))+'</span>';
  if(f.kind==="dimension"){
    const d=splitDim(v);
    return '<input type="number" step="0.05"'+at+' data-kind="dimension" value="'+
      esc(d.n)+'"><select class="unit" aria-label="Unit">'+
      UNITS.map(x=>'<option'+(x===d.u?" selected":"")+'>'+x+'</option>').join("")+'</select>';
  }
  if(f.kind==="enum")
    return '<select'+at+' data-kind="enum">'+(f.options||[]).map(o=>
      '<option'+(String(o)===String(v)?" selected":"")+'>'+esc(o)+'</option>').join("")+
      '</select>';
  if(f.kind==="bool")
    return '<input type="checkbox"'+at+' data-kind="bool"'+(v?" checked":"")+'>';
  if(f.kind==="number")
    return '<input type="number"'+at+' data-kind="number" value="'+esc(v==null?"":v)+'">';
  if(f.kind==="list")
    return '<input type="text"'+at+' data-kind="list" value="'+esc((v||[]).join(", "))+
      '" placeholder="comma separated">';
  /* Templates are several lines -- the main column of an entry is a title
     line, a summary and the highlights -- and a one-line input silently
     drops the line breaks, so writing it back would flatten the template. */
  if(f.path[0]==="templates"){
    const t=v==null?"":String(v);
    return '<textarea'+at+' data-kind="text" rows="'+Math.max(1,t.split("\n").length)+
      '" spellcheck="false">'+esc(t)+'</textarea>';
  }
  return '<input type="text"'+at+' data-kind="text" value="'+esc(v==null?"":v)+'">';
}
/* ---- photo -----------------------------------------------------------------
   One photo per workspace, cropped square and shrunk here before it is sent,
   so a phone photo never reaches every PDF at full size. A CV shows it by
   pointing cv.photo at it; that is an edit like any other, saved with Save. */
const photoOn=()=>!!(S.data&&S.data.cv&&S.data.cv.photo);
const PH_ICON='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '+
  'stroke-width="2" aria-hidden="true"><circle cx="12" cy="9" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/></svg>';
const PH_TIP='<div class="ph-note"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" '+
  'stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/>'+
  '<path d="M12 8v5M12 16v.5"/></svg><span>Expected on CVs in much of Europe, often left off in '+
  'the UK, the US and Ireland. The ATS check says so when an application is in one of those.'+
  '</span></div>';
function setPhoto(on){
  if(!S.data||!S.data.cv) return;
  const ref=S.doc&&S.doc.photo_ref;
  if(on&&!ref) return;
  S.data.cv.photo=on?ref:null;
  touch(); paintPhotoChip();
  if(!$("#ovl-design").hidden){ paintPhoto(); paintDesignNav() }
}
function paintPhoto(){
  const host=$("#dz-photo"), ph=S.state&&S.state.photo;
  if(!ph){
    host.innerHTML='<div class="ph-drop" id="ph-drop"><span class="ic">'+
      PH_ICON.replace(/16/g,"24")+'</span><b>Add a photo</b>'+
      '<span>Drop a JPEG or PNG here. You crop it to a square next, and only the cropped '+
      'copy is kept.</span><button class="obtn" id="ph-pick">Choose a photo&#8230;</button>'+
      '<input type="file" id="ph-file" accept="image/jpeg,image/png,image/webp" hidden></div>'+PH_TIP+
      '<p class="sp-note">Its size and place on the page appear here once there is a photo.</p>';
    const drop=$("#ph-drop"), file=$("#ph-file");
    $("#ph-pick").onclick=()=>file.click();
    file.onchange=()=>file.files[0]&&cropSheet(file.files[0]);
    drop.ondragover=e=>{ e.preventDefault(); drop.classList.add("over") };
    drop.ondragleave=()=>drop.classList.remove("over");
    drop.ondrop=e=>{ e.preventDefault(); drop.classList.remove("over");
      const f=e.dataTransfer.files[0]; if(f) cropSheet(f) };
    return;
  }
  const on=photoOn(), kb=Math.max(1,Math.round(ph.bytes/1024));
  host.innerHTML='<div class="ph-card"><div class="ph-top"><img alt="Your photo" src="'+
      esc(ph.url+tok())+'"><div class="m"><b>photo.jpg</b><span>Square · '+kb+
      ' KB · in your CV Studio folder</span><span class="acts">'+
      '<button class="obtn" id="ph-replace">Replace&#8230;</button>'+
      '<button class="obtn" id="ph-crop">Crop&#8230;</button>'+
      '<button class="del" id="ph-del">Remove</button></span></div>'+
      '<input type="file" id="ph-file" accept="image/jpeg,image/png,image/webp" hidden></div>'+
    '<label class="sw"><input type="checkbox" id="ph-on"'+(on?" checked":"")+'><span class="tr"></span>'+
      '<span class="tx"><b>Show it on this CV</b><span>CVs you tailor from this one start the '+
      'same way. Turn it off on any of them.</span></span></label></div>'+PH_TIP;
  const file=$("#ph-file");
  $("#ph-replace").onclick=()=>file.click();
  file.onchange=()=>file.files[0]&&cropSheet(file.files[0]);
  $("#ph-crop").onclick=()=>cropSheet(ph.url+tok());
  $("#ph-on").onchange=e=>setPhoto(e.target.checked);
  $("#ph-del").onclick=removePhoto;
}
async function removePhoto(){
  if(!await askConfirm({title:t("Remove the photo?"),body:t("It is removed from your CV Studio folder and comes off every CV that shows it."),
    ok:t("Remove photo"),danger:true})) return;
  try{
    const r=await post("/api/photo/remove",{});
    S.state.photo=null;
    if(S.path){
      const d=await api("/api/doc?path="+encodeURIComponent(S.path));
      if(S.dirty&&S.data&&S.data.cv){
        /* Unsaved edits are in S.data: take only what the removal changed. */
        const cv=(d.data&&d.data.cv)||{};
        if("photo" in cv) S.data.cv.photo=cv.photo; else delete S.data.cv.photo;
        if(S.doc) S.doc.photo_ref=d.photo_ref;
      }else{ S.doc=d; S.data=d.data?JSON.parse(JSON.stringify(d.data)):null }
      S.docMtime=d.mtime;
      paint(); scheduleLive();
    }
    paintPhotoChip();
    if(!$("#ovl-design").hidden){ paintAdvanced(); paintDesignNav() }
    toast(r.cleared&&r.cleared.length?t("Photo removed from {n} CV(s)",{n:r.cleared.length}):t("Photo removed"));
  }catch(e){ toast(e.message,true) }
}
/* Drag to move, zoom to fill the square; 600 x 600 JPEG out. */
function cropSheet(src){
  const url=typeof src==="string"?src:URL.createObjectURL(src);
  openSheet(
    '<div><h3 id="sheet-title">Crop your photo</h3><p>Drag to move it, and zoom until your '+
      'face fills most of the square.</p></div>'+
    '<div class="crop" id="crop"><img class="dim" alt=""><div class="win"><img alt="The part '+
      'of the photo that will be kept"></div><div class="win grid" aria-hidden="true"></div></div>'+
    '<label class="cropzoom">Zoom <input type="range" id="crop-z" min="1" max="4" step="0.01" '+
      'value="1"></label>'+
    '<div class="foot"><span class="left sp-note">Saved as a 600 × 600 JPEG in your CV Studio '+
      'folder. The original is not kept.</span><button class="sbtn" data-cancel>Cancel</button>'+
      '<button class="sbtn primary" id="crop-go" disabled>Use this photo</button></div>');
  const box=$("#crop"), imgs=box.querySelectorAll("img"), z=$("#crop-z");
  const st={w:0,h:0,base:1,s:1,ox:0,oy:0};
  const clamp=()=>{
    const mx=Math.max(0,st.w*st.s/2-130), my=Math.max(0,st.h*st.s/2-130);
    st.ox=Math.min(mx,Math.max(-mx,st.ox)); st.oy=Math.min(my,Math.max(-my,st.oy));
  };
  const draw=()=>{
    clamp();
    imgs.forEach(im=>{ im.style.width=(st.w*st.s)+"px";
      im.style.transform="translate(calc(-50% + "+st.ox+"px), calc(-50% + "+st.oy+"px))" });
  };
  const im=new Image();
  im.onload=()=>{
    st.w=im.naturalWidth; st.h=im.naturalHeight;
    st.base=260/Math.min(st.w,st.h); st.s=st.base;
    imgs.forEach(x=>x.src=url); draw(); $("#crop-go").disabled=false;
  };
  im.onerror=()=>{ toast("That image could not be opened.",true); closeSheet() };
  im.src=url;
  z.oninput=()=>{ st.s=st.base*Number(z.value); draw() };
  let drag=null;
  box.onpointerdown=e=>{ drag={x:e.clientX,y:e.clientY,ox:st.ox,oy:st.oy};
    box.setPointerCapture(e.pointerId); box.classList.add("dragging") };
  box.onpointermove=e=>{ if(!drag) return;
    st.ox=drag.ox+e.clientX-drag.x; st.oy=drag.oy+e.clientY-drag.y; draw() };
  box.onpointerup=box.onpointercancel=()=>{ drag=null; box.classList.remove("dragging") };
  box.onwheel=e=>{ e.preventDefault();
    z.value=String(Math.min(4,Math.max(1,Number(z.value)-e.deltaY/400))); z.oninput() };
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#crop-go").onclick=async()=>{
    const side=260/st.s, sx=st.w/2-(130+st.ox)/st.s, sy=st.h/2-(130+st.oy)/st.s;
    const cv=document.createElement("canvas"); cv.width=cv.height=600;
    const g=cv.getContext("2d"); g.fillStyle="#fff"; g.fillRect(0,0,600,600);
    g.drawImage(im,sx,sy,side,side,0,0,600,600);
    const data=cv.toDataURL("image/jpeg",0.85).replace(/^data:[^,]*,/,"");
    $("#crop-go").disabled=true;
    try{
      const r=await post("/api/photo",{data,path:S.path||null});
      if(!r.ok){ $("#crop-go").disabled=false; return toast(r.error,true) }
      S.state.photo=r.photo;
      if(S.doc&&r.ref) S.doc.photo_ref=r.ref;
      closeSheet();
      /* A first photo goes straight onto the CV it was added from. */
      if(S.data&&S.data.cv&&!photoOn()) setPhoto(true); else scheduleLive();
      if(!$("#ovl-design").hidden){ paintAdvanced(); paintDesignNav() }
      paintPhotoChip();
      toast("Photo saved");
    }catch(e){ $("#crop-go").disabled=false; toast(e.message,true) }
    if(typeof src!=="string") URL.revokeObjectURL(url);
  };
}
/* The chip in the editor's bar: whether this CV shows the photo, and a switch
   for it, with a word when the application it is for is somewhere CVs usually
   go without one. */
const PHOTO_UNUSUAL=/\b(united kingdom|uk|u\.k\.|england|scotland|wales|northern ireland|london|manchester|edinburgh|glasgow|bristol|united states|usa|u\.s\.a?\.?|us|new york|san francisco|seattle|boston|austin|chicago|los angeles|ireland|dublin|cork)\b/i;
function paintPhotoChip(){
  const chip=$("#photochip");
  const letter=S.path&&S.path.startsWith("letters/");
  if(!S.path||letter||!(S.state&&S.state.photo)||!S.data){ chip.hidden=true; return }
  const on=photoOn();
  chip.hidden=false;
  chip.classList.toggle("off",!on);
  chip.innerHTML=PH_ICON.replace(/16/g,"12")+'<span>Photo '+(on?"on":"off")+'</span>';
  chip.title="Whether this CV shows your photo";
  chip.onclick=e=>{ e.stopPropagation(); photoPop() };
}
function photoPop(){
  let pop=$("#photopop");
  if(pop){ pop.remove(); return }
  const j=typeof linkedJob==="function"?linkedJob():null;
  const unusual=j&&PHOTO_UNUSUAL.test((j.country||"")+" "+(j.location||""));
  pop=document.createElement("div");
  pop.className="photopop"; pop.id="photopop"; pop.setAttribute("role","dialog");
  pop.setAttribute("aria-label","Photo on this CV");
  const base=S.doc&&S.doc.prov&&S.doc.prov.base&&S.doc.prov.base.path;
  pop.innerHTML='<label class="sw"><input type="checkbox" id="pp-on"'+(photoOn()?" checked":"")+
    '><span class="tr"></span><span class="tx"><b>Show the photo on this CV</b><span>'+
    (base?'For this CV only. '+esc(docLabel(base))+' keeps its own setting.'
      :'For this CV only.')+'</span></span></label>'+
    (unusual?'<div class="why"><i></i><span>This application is in '+esc(j.location||j.country)+
      '. Recruiters there usually ask for CVs without a photo.</span></div>':'');
  document.body.append(pop);
  const r=$("#photochip").getBoundingClientRect();
  pop.style.left=Math.min(r.left,innerWidth-330)+"px"; pop.style.top=(r.bottom+6)+"px";
  $("#pp-on").onchange=e=>setPhoto(e.target.checked);
  $("#pp-on").focus();
  const off=ev=>{ if(!pop.contains(ev.target)&&ev.target!==$("#photochip")){ pop.remove();
    document.removeEventListener("pointerdown",off,true) } };
  document.addEventListener("pointerdown",off,true);
}

/* RenderCV keeps the photo's size and place under Header, where they mean
   nothing until there is a photo. They get a group of their own, beside the
   photo itself, second after Theme. */
function designGroups(){
  const raw=(S.schema&&S.schema.groups)||[];
  const isPhoto=f=>f.path[0]==="header"&&/^photo_/.test(f.path[f.path.length-1]);
  const photo=raw.flatMap(g=>g.fields.filter(isPhoto));
  const rest=raw.map(g=>({...g,fields:g.fields.filter(f=>!isPhoto(f))})).filter(g=>g.fields.length);
  return [{name:"photo",fields:photo}].concat(rest);
}
function paintAdvanced(){
  const host=$("#dz-advanced"), groups=designGroups();
  if(!groups.length){
    host.innerHTML='<p class="sp-note">RenderCV did not offer a schema for this theme, '+
      'so only the theme can be chosen here. Everything else can still be edited '+
      'in the YAML tab.</p>';
    return;
  }
  const cur=(S.data&&S.data.design)||{};
  host.innerHTML=groups.map(g=>{
    /* Deeper paths get a heading of their own -- typography.font_family.body
       sits under "Font family" as "Body" -- in the order RenderCV lists them. */
    let h='', open=null, rows='';
    const flush=()=>{ if(rows) h+=(open?'<h3 class="dz-sub">'+esc(human(open))+'</h3>':"")+
      '<div class="dz-card">'+rows+'</div>'; rows="" };
    g.fields.forEach(f=>{
      const sub=f.path.length>2?f.path[1]:null;
      if(sub!==open){ flush(); open=sub }
      const raw=getAt(cur,f.path);
      const v=(raw===undefined||raw===null)?f.default:raw;
      rows+='<div class="dz-row" data-def="'+esc(JSON.stringify(f.default==null?null:f.default))+
        '" data-was="'+esc(JSON.stringify(v==null?null:v))+'"><label><i class="dz-dot" title="Changed from the theme default"></i><span>'+
        esc(human(g.name==="photo"?String(f.path[f.path.length-1]).replace(/^photo_/,"")
          :f.path[f.path.length-1]))+'</span></label>'+
        '<div class="dctl">'+controlHTML(f,v)+'</div>'+
        '<button class="dreset" title="Back to the theme default">Reset</button></div>';
    });
    flush();
    return '<div class="dz-group" data-group="'+esc(g.name)+'" hidden>'+h+'</div>';
  }).join("");
  markChanged();
  showDesignSection();
}
/* Read a row's control the way the patch list will, so "changed" means what
   would actually be written differs from the theme's own value. */
function rowValue(row){
  const el=row.querySelector("[data-d]"), kind=el.dataset.kind;
  if(kind==="color") return hex2rgb(el.value);
  if(kind==="bool") return el.checked;
  if(kind==="number") return el.value===""?null:Number(el.value);
  if(kind==="list") return el.value.split(",").map(x=>x.trim()).filter(Boolean);
  if(kind==="dimension"){
    const u=row.querySelector("select.unit");
    return el.value===""?null:String(el.value)+((u&&u.value)||"cm");
  }
  return el.value;
}
const sameVal=(a,b)=>{
  if(a==null||a==="") a=null; if(b==null||b==="") b=null;
  if(Array.isArray(a)||Array.isArray(b)) return JSON.stringify(a||[])===JSON.stringify(b||[]);
  if(typeof a==="string"&&typeof b==="string"&&/rgb\(/.test(a)&&/rgb\(/.test(b))
    return a.replace(/\s/g,"")===b.replace(/\s/g,"");
  return String(a)===String(b);
};
function markChanged(){
  $$("#dz-advanced .dz-row").forEach(r=>{
    r.classList.toggle("chg",!sameVal(rowValue(r),JSON.parse(r.dataset.def)));
  });
}
function resetRow(row){
  const def=JSON.parse(row.dataset.def), el=row.querySelector("[data-d]"), kind=el.dataset.kind;
  if(kind==="color"){ el.value=rgb2hex(def); const sp=row.querySelector(".hex");
    if(sp) sp.textContent=def||"" }
  else if(kind==="bool") el.checked=!!def;
  else if(kind==="dimension"){ const d=splitDim(def); el.value=d.n;
    const u=row.querySelector("select.unit"); if(u) u.value=d.u }
  else if(kind==="list") el.value=(def||[]).join(", ");
  else el.value=def==null?"":def;
}
function paintDesignNav(){
  const groups=designGroups();
  const cur=DZ.theme||(S.state.themes||[])[0];
  const items=[{k:"theme",t:"Theme",ct:themeLabel(cur||""),n:0}].concat(groups.map(g=>{
    const el=$('#dz-advanced [data-group="'+CSS.escape(g.name)+'"]');
    return {k:g.name,t:human(g.name),ct:g.name==="photo"
      ?(!(S.state&&S.state.photo)?"None":photoOn()?"On":"Off"):"",
      n:el?el.querySelectorAll(".dz-row.chg").length:0};
  }));
  $("#dz-nav").innerHTML=items.map(i=>
    '<button data-sec="'+esc(i.k)+'" aria-current="'+String(i.k===DZ.section)+'">'+
    '<span class="nm">'+esc(i.t)+'</span>'+
    (i.n?'<span class="chg" title="Changed from the theme default"><i class="dz-dot"></i>'+
      i.n+'</span>':"")+
    '<span class="ct">'+esc(i.ct)+'</span></button>').join("")+
    '<div class="dz-key"><i class="dz-dot"></i>Changed from the theme default</div>';
  $$("#dz-nav [data-sec]").forEach(b=>b.onclick=()=>{
    DZ.section=b.dataset.sec; paintDesignNav(); showDesignSection();
    $(".dz-scroll").scrollTop=0;
  });
}
function showDesignSection(){
  const k=DZ.section||"theme", theme=k==="theme";
  $("#themegrid").hidden=!theme;
  $$("#dz-advanced .dz-group").forEach(g=>{ g.hidden=g.dataset.group!==k||
    (k==="photo"&&!(S.state&&S.state.photo)) });
  $("#dz-photo").hidden=k!=="photo";
  if(k==="photo") paintPhoto();
  $("#dz-h").textContent=theme?"Theme":human(k);
  $("#dz-desc").textContent=theme
    ? "Each tile is this document in that theme. Pick a colour under it to try it in that colour."
    : (DZ_GROUPS[k]||"");
  const grp=$('#dz-advanced [data-group="'+CSS.escape(k)+'"]');
  $("#dz-reset").hidden=theme||!grp||!grp.querySelector(".dz-row.chg");
  if(grp) grp.querySelectorAll("textarea").forEach(fitArea);
}
/* A template box as tall as what it holds, so no line of it is hidden. */
function fitArea(t){ t.style.height="auto"; t.style.height=(t.scrollHeight+2)+"px" }
$("#dz-reset").onclick=()=>{
  const grp=$('#dz-advanced [data-group="'+CSS.escape(DZ.section)+'"]');
  if(!grp) return;
  grp.querySelectorAll(".dz-row.chg").forEach(resetRow);
  markChanged(); paintDesignNav(); showDesignSection(); touch();
};
/* Only what you actually changed is written. Writing every field back made
   a document carry eighty-odd copies of its theme's defaults, which then stop
   following the theme when you switch it. */
function advancedPatches(){
  return $$("#dz-advanced .dz-row").map(row=>{
    const el=row.querySelector("[data-d]"), v=rowValue(row);
    if(el.dataset.kind==="dimension"&&v===null) return null;
    if(sameVal(v,JSON.parse(row.dataset.was))) return null;
    return {path:["design"].concat(JSON.parse(el.dataset.d)),value:v};
  }).filter(Boolean);
}
function designPatches(){
  const out=[];
  if(DZ.theme) out.push({path:["design","theme"],value:DZ.theme});
  return out.concat(advancedPatches());
}
$("#dz-advanced").addEventListener("click",e=>{
  const b=e.target.closest(".dreset");
  if(!b) return;
  resetRow(b.closest(".dz-row"));
  markChanged(); paintDesignNav(); showDesignSection(); touch();
});
$("#dz-advanced").addEventListener("input",e=>{
  if(e.target.dataset&&e.target.dataset.kind==="color"){
    const sp=e.target.parentElement.querySelector(".hex");
    if(sp) sp.textContent=hex2rgb(e.target.value);
  }
  if(e.target.tagName==="TEXTAREA") fitArea(e.target);
  markChanged(); paintDesignNav(); showDesignSection();
  touch();
});
$("#dz-advanced").addEventListener("change",touch);

/* What the current combination actually costs, read off the last render
   rather than predicted. */
function paintEffect(){
  const r=S.render;
  $("#dz-pages").innerHTML=r&&r.pngs
    ? r.pngs.map((u,i)=>'<img src="'+esc(u+tok())+'" alt="Page '+(i+1)+'">').join("")
    : '<p class="note muted">'+esc(S.renderFailed?t("The page could not be laid out, so there is nothing to preview. The page view says why."):t("Rendering…"))+'</p>';
  if(!r){ $("#dz-effect").innerHTML='<span>'+esc(S.renderFailed?t("Not laid out"):t("Nothing rendered yet"))+'</span>'; return }
  const pct=S.fill==null?null:Math.round(S.fill*100);
  const known=Object.keys(S.themePages).filter(t=>t!==DZ.theme);
  const shortest=known.sort((a,b)=>S.themePages[a]-S.themePages[b])[0];
  let note="";
  if(shortest&&S.themePages[shortest]<r.pages)
    note=themeLabel(shortest)+" fits this on "+S.themePages[shortest]+
      " page"+(S.themePages[shortest]===1?"":"s");
  const sep='<span class="sep">·</span>';
  $("#dz-effect").innerHTML=
    '<span><b>'+r.pages+'</b> page'+(r.pages===1?"":"s")+'</span>'+sep+
    '<span>page '+r.pages+' is <b>'+(pct==null?"–":pct+"%")+'</b> full</span>'+sep+
    '<span><b>'+(r.ats_words||0)+'</b> words</span>'+
    (note?sep+'<span class="why">'+esc(note)+'</span>':"");
}

