/* Editor tabs/zoom/paging, divider, rendering, clickable page, and YAML editor. */
/* ---- tabs, zoom and paging ------------------------------------------------ */
function showTab(tab){
  /* The card belongs to the page on its own: it is opened by a block and
     points at one. Beside a form or the source there is a better place for
     those fields already -- the pane on the left, which follows the
     selection -- so the card goes away rather than stacking a third copy of
     the same entry on top of the second. */
  if(tab!=="page") closeEditor();
  S.tab=tab;
  $$("#edtabs button").forEach(x=>
    x.setAttribute("aria-selected",String(x.dataset.tab===tab)));
  $("#pane-form").hidden=tab!=="form";
  $("#pane-yaml").hidden=tab!=="yaml";
  $("#pane-diff").hidden=tab!=="diff";
  $("#split").hidden=tab==="page";
  if(tab==="yaml"){ paint(); markYamlSelection() }
  if(tab==="form") buildForm();   /* re-read the model, in case the page moved on */
  if(tab==="diff"){ paintDiff(); loadDiff() }
  cvRvBar();                      /* where its Review button goes depends on the tab */
  trackSelectionOnPage();         /* the selection may have moved while away */
  refit();                        /* the page just got a different amount of room */
  /* Not remembered: it only exists for a tailored CV, and the next document
     opened may not be one. */
  if(tab!=="diff") setPref("tab",tab);
}
$$("#edtabs button").forEach(b=>b.onclick=()=>showTab(b.dataset.tab));

/* ---- the divider ---------------------------------------------------------
   Where it sits is the one layout choice in the app, so it is remembered: a
   form you have to widen again every time you open it is a form you stop
   using. A share rather than a width, so it survives the window changing
   size. The stops keep both halves usable -- a 90% form with a sliver of page
   beside it is not a split view, it is the old tab with a decoration. */
function setSplit(pct){
  const v=Math.min(68,Math.max(26,pct));
  $("#panes").style.setProperty("--split",v.toFixed(2)+"%");
  $("#split").setAttribute("aria-valuenow",Math.round(v));
  return v;
}
function splitAt(clientX){
  const b=$("#panes").getBoundingClientRect();
  return setSplit((clientX-b.left)/b.width*100);
}
(()=>{
  const bar=$("#split");
  let on=false, frame=0;
  bar.addEventListener("pointerdown",e=>{
    on=true; bar.classList.add("on"); bar.setPointerCapture(e.pointerId);
    document.body.classList.add("dragging");
    e.preventDefault();
  });
  bar.addEventListener("pointermove",e=>{
    if(!on) return;
    /* One layout per frame. Dragging fires faster than the page can be
       re-fitted, and doing the work every event makes the drag feel heavier
       than the thing it is moving. */
    if(frame) return;
    frame=requestAnimationFrame(()=>{ frame=0; splitAt(e.clientX); refit() });
  });
  const drop=e=>{
    if(!on) return;
    on=false; bar.classList.remove("on");
    document.body.classList.remove("dragging");
    if(frame){ cancelAnimationFrame(frame); frame=0 }
    setPref("split",splitAt(e.clientX));
    refit();
  };
  bar.addEventListener("pointerup",drop);
  bar.addEventListener("pointercancel",drop);
  /* Reachable without a mouse, in the steps someone dragging would land on. */
  bar.addEventListener("keydown",e=>{
    const step=e.key==="ArrowLeft"?-2:e.key==="ArrowRight"?2:0;
    if(!step) return;
    e.preventDefault();
    const b=$("#panes").getBoundingClientRect();
    const now=$("#pane-form").hidden?$("#pane-yaml"):$("#pane-form");
    setPref("split",setSplit((now.offsetWidth/b.width*100)+step));
    refit();
  });
})();
setSplit(+prefs().split||46);
$("#z-in").onclick=()=>setZoom(S.zoom+.1);
$("#z-out").onclick=()=>setZoom(S.zoom-.1);
/* The readout is also the way back: once you have zoomed, one click refits. */
$("#z-lvl").onclick=()=>{ S.zoomAuto=true; refit() };
$("#pg-prev").onclick=()=>setPage(S.page-1);
$("#pg-next").onclick=()=>setPage(S.page+1);
function setZoom(z){ S.zoom=Math.min(3,Math.max(.2,z)); S.zoomAuto=false; refit() }
function setPage(i){
  const n=(S.render&&S.render.pngs.length)||0;
  S.page=Math.min(Math.max(0,i),Math.max(0,n-1)); paintPage();
}

/* ---- rendering ------------------------------------------------------------ */
/* The form, the block editor and the YAML source are rebuilt from the model
   after a save, and when the poll brings new marks. Rebuilt, the field you
   were typing in was a new element: focus went to the page body, the next
   keystrokes went nowhere, and the pane jumped. Whatever the rebuild, the
   field with the caret -- found again by its data path or id -- gets focus
   back, with its selection, and every pane keeps its scroll. */
function keepFocus(fn){
  const a=document.activeElement;
  const inEd=a&&a.closest&&a.closest("#pane-form,#ed,#pane-yaml");
  const key=inEd&&a.dataset&&a.dataset.p, id=inEd&&a.id, tag=inEd&&a.tagName;
  const s0=inEd&&a.selectionStart!=null?[a.selectionStart,a.selectionEnd,a.selectionDirection]:null;
  const scrolls=["#pane-form","#ed .ed-body","#ed","#yaml","#pane-page"].map(q=>[q,($(q)||{}).scrollTop]);
  const out=fn();
  const done=()=>{
    for(const [q,top] of scrolls){ const el=$(q); if(el&&top!=null&&el.scrollTop!==top) el.scrollTop=top }
    if(!inEd||document.activeElement===a&&a.isConnected) return;
    let el=id?document.getElementById(id):null;
    if(!el&&key){ const box=inEd.id?"#"+inEd.id+" ":"";
      el=document.querySelector(box+tag.toLowerCase()+'[data-p="'+CSS.escape(key)+'"]')||
         document.querySelector(tag.toLowerCase()+'[data-p="'+CSS.escape(key)+'"]') }
    if(!el||!el.isConnected) return;
    el.focus({preventScroll:true});
    if(s0&&el.setSelectionRange){ const n=(el.value||"").length; try{ el.setSelectionRange(Math.min(s0[0],n),Math.min(s0[1],n),s0[2]) }catch(e){} }
  };
  if(out&&typeof out.then==="function") return out.then(v=>{ done(); return v });
  done(); return out;
}
async function save(ops){
  if(!S.path||S.busy) return;
  S.busy=true; $("#btn-render").disabled=true;
  try{
    const body=S.tab==="yaml" ? {path:S.path,yaml:$("#yaml").value}
                              : {path:S.path,patches:collectPatches()};
    if(ops&&ops.length) body.ops=ops;
    const gen=S.gen;
    const r=await post("/api/save",body);
    if(S.gen!==gen&&!(ops&&ops.length)){
      /* Typed while it saved: the file has what was sent, the form has more.
         Keep the form's, still to save, and take only the file's time, so
         the poll does not read this write as somebody else's. (Not after an
         added or removed entry: the form's positions would be the old ones.) */
      S.docMtime=r.mtime; hideExternalChange();
      return;
    }
    S.doc=r; S.data=r.data?JSON.parse(JSON.stringify(r.data)):null; paintLang(); paintPhotoChip();
    /* Our own write, so take its timestamp: the poll must not read it back as
       somebody else having changed the file. */
    S.docMtime=r.mtime; hideExternalChange();
    S.dirty=false; S.savedAt=Date.now();
    keepFocus(()=>{
      setProv(r.prov);
      if($("#yaml").value!==r.yaml) $("#yaml").value=r.yaml;
      paint(); setYamlError(r.parse_error);
      buildOutline(); buildInspector(); if(S.tab==="form") buildForm();
    });
    /* The Design screen measures "changed" against the file, which just moved. */
    if(!$("#ovl-design").hidden&&S.schema){ paintAdvanced(); paintDesignNav() }
    /* A refused operation is not a failure of the save, so it cannot be left
       to the catch. Saying nothing would be worse: you press Add, the file is
       rewritten without it, and the form comes back looking untouched. */
    (r.missed||[]).filter(m=>m.op).forEach(m=>toast(
      t("Could not {op}: {why}",{op:String(m.op.op||"do that").replace(/_/g," "),why:tx(m.why)}),true));
    await doRender();
  }catch(e){ toast(e.message,true) }
  finally{ S.busy=false; $("#btn-render").disabled=false; paintStatus() }
}
$("#btn-render").onclick=()=>save();   /* not `save`: the click event is not ops */

function collectPatches(){
  const out=leafPaths().map(p=>({path:p,value:getAt(S.data,p)}));
  return out.concat(designPatches());
}

async function doRender(){
  if(!S.path) return;
  const t0=performance.now();
  /* The page on screen stays until the new one is ready: swapping in a blank
     sheet first made the pane blink and lose its scroll on every save. */
  if(!$("#pane-page img.pg")) $("#pane-page").innerHTML='<div class="skel" style="width:472px;height:668px"></div>';
  const top=$("#pane-page").scrollTop;
  try{
    const r=await post("/api/render",{path:S.path});
    S.renderMs=Math.round(performance.now()-t0);
    if(!r.ok){
      S.pdf=null; $("#btn-pdf").disabled=true; $("#btn-pdf").title=t("The page did not render, so there is no PDF yet");
      renderFailed(r.hint,r.error);
      S.render=null; paintBudget(); paintStatus();
      return;
    }
    $("#btn-pdf").title="";
    await keepFocus(()=>adoptRender(r));
    if($("#pane-page").scrollTop!==top) $("#pane-page").scrollTop=top;
  }catch(e){
    S.render=null;
    renderFailed(null,e.message);
  }
  paintStatus();
}

/* A page that did not lay out: what happened in words, what you can do,
   and the log only if you ask for it. The document itself is saved and every
   other way of editing it still works, which is the thing to say first. */
function renderFailed(hint,log){
  S.renderFailed=true;
  const tab=S.tab||"page";
  $("#pane-page").innerHTML='<div class="err rfail" role="alert"><h4>'+esc(t("The page could not be laid out"))+'</h4>'+
    '<div class="hint">'+esc(hint?tx(hint):t("Something in the CV stopped the layout. Your CV is saved; the details below say where."))+'</div>'+
    '<div class="acts"><button class="obtn" data-rf-again>'+esc(t("Try again"))+'</button>'+
      (tab==="page"?'<button class="obtn" data-rf-form>'+esc(t("Edit in Form"))+'</button>':'')+'</div>'+
    (log?'<details><summary>'+esc(t("Show details"))+'</summary><pre>'+esc(log)+'</pre></details>':'')+'</div>';
  const pane=$("#pane-page");
  const again=pane.querySelector("[data-rf-again]"); if(again) again.onclick=()=>doRender();
  const form=pane.querySelector("[data-rf-form]"); if(form) form.onclick=()=>{ const b=$('#edtabs [data-tab="form"]'); if(b) b.click() };
}

/* Every good render updates the same three things: the pages on screen, the
   page budget, and what we know about this document and this theme. */
async function adoptRender(r){
  S.renderFailed=false;
  S.render=r; S.pdf=r.pdf; $("#btn-pdf").disabled=!r.pdf;
  const grew=S.pages[S.path]!==r.pages;
  S.pages[S.path]=r.pages;
  const b=S.state&&S.state.base;
  if(b&&b.path===S.path&&r.pngs.length){
    S.baseThumb={path:S.path,png:r.pngs[0],failed:false};
    mountBase($("#docbase"),"bhero"); mountBase($("#baserow"),"baserow");
  }
  if(DZ.theme) S.themePages[DZ.theme]=r.pages;
  if(S.page>=r.pngs.length) S.page=Math.max(0,r.pngs.length-1);
  paintPage();
  if(grew) renderDocs(S.state.documents);
  S.fill=r.pngs.length?await measureFill(r.pngs[r.pngs.length-1]+tok()):null;
  paintBudget();
  if(!$("#ovl-design").hidden){ paintThemes(); paintEffect() }
}

/* The page is the thing you came to look at, so it gets the room. "100%" means
   actual size -- the sheet at 96dpi, the width it would print -- rather than an
   arbitrary base that made the readout lie by a factor of 1.7. RenderCV renders
   at 144dpi, so a CSS pixel at 100% is two thirds of an image pixel. */
const PAGE_GUTTER=52;
function pageCssWidth(img){ return img.naturalWidth*(2/3) }
/* Fit to the width, not to the whole sheet. A CV is read top to bottom, and
   fitting its height into a laptop window puts the body type at about four
   pixels -- unreadable, on the one screen whose whole job is reading it.
   Capped at 150%, where the 144dpi render stops having pixels to spare. */
/* The width the card is holding, when it is out. Auto-fit has to subtract it:
   fitting the sheet to the whole pane produces a sheet exactly as wide as the
   pane, which leaves nothing to shift it into, so the page never made room and
   the card always ended up over the text. The page fits beside the card. */
function edRoom(host){
  return host.classList.contains("ed-open")
    ? parseFloat(getComputedStyle(host).getPropertyValue("--ed-room"))||0 : 0;
}
function fitZoom(host,img){
  const w=(host.clientWidth-PAGE_GUTTER*2-edRoom(host))/pageCssWidth(img);
  return Math.max(.2,Math.min(1.5,w));
}
/* Resizing the sheet is not a new render, and rebuilding the pane for it made
   the page blink every time you dragged the divider or touched the zoom. The
   hits and the margin marks are placed in percentages of the wrapper, so they
   follow the image on their own and only a new render has to redraw them. */
function refit(){
  const host=$("#pane-page"), img=host.querySelector(".pg");
  if(!img||!img.naturalHeight) return;
  if(S.zoomAuto) S.zoom=fitZoom(host,img);
  img.style.width=Math.round(pageCssWidth(img)*S.zoom)+"px";
  $("#z-lvl").textContent=Math.round(S.zoom*100)+"%";
  $("#z-lvl").classList.toggle("auto",!!S.zoomAuto);
}
function paintPage(){
  const host=$("#pane-page"), r=S.render;
  if(!r||!r.pngs.length){ $("#pg-idx").textContent="–"; return }
  const url=r.pngs[S.page]+tok();
  const draw=()=>{ refit(); paintHits() };
  host.innerHTML='<div class="pgwrap"><img class="pg" src="'+url+'" alt="Page '+
    (S.page+1)+'"></div>';
  const img=host.querySelector(".pg");
  if(img.complete&&img.naturalHeight) draw();
  else img.onload=()=>{ if(host.querySelector(".pg")===img) draw() };
  const nomap=$("#nomap");
  if(r.map&&r.map.length){ nomap.hidden=true }
  else{
    nomap.hidden=false;
    nomap.textContent="page not clickable";
    nomap.title=(r.map_why||"The render could not be mapped back to the "+
      "document.")+"\n\nEverything else works: the page is real, and the "+
      "outline and the form still select.";
  }
  $("#pg-idx").textContent=(S.page+1)+" / "+r.pngs.length;
  $("#pg-prev").disabled=S.page===0;
  $("#pg-next").disabled=S.page>=r.pngs.length-1;
}
/* Re-fit while the window is being resized, but only while nobody has chosen a
   zoom of their own. */
/* The page is on screen in every tab now, so it refits in every tab. */
addEventListener("resize",()=>{
  /* In this order: the room the card holds decides the fit, the fit decides
     where the block is, and the block decides where the card goes. */
  if(!$("#ed").hidden) makeRoom(true);
  refit();
  if(!$("#ed").hidden) placeEditor();
});

/* ---- clicking the page --------------------------------------------------
   Every block of the rendered page is a band, and the bands tile the page, so
   a click always lands on something rather than between two things. They are
   the same selection the outline makes: point at a job on the page and the
   inspector is editing that job. */
const bandsOn=page=>((S.render&&S.render.map)||[]).filter(b=>b.page===page+1);
const sameBlock=(b,sel)=>!!sel&&(
  b.k==="header" ? sel.kind==="header"
  : b.k==="section" ? sel.kind==="section"&&sel.name===b.name
  : sel.kind==="entry"&&sel.name===b.name&&sel.i===b.i);

function bandLabel(b){
  if(b.k==="header") return "Header";
  const cv=(S.data&&S.data.cv)||{};
  const label=sectionLabel(b.name);
  if(b.k==="section") return label;
  const it=((cv.sections||{})[b.name]||[])[b.i];
  return label+" · "+(it===undefined?("entry "+(b.i+1)):entryTitle(it,b.i));
}

function paintHits(){
  const wrap=$("#pane-page .pgwrap"), img=wrap&&wrap.querySelector(".pg");
  if(!wrap||!img||!img.naturalHeight) return;
  wrap.querySelectorAll(".hit").forEach(el=>el.remove());
  const box=(S.render&&S.render.map_box)||null;
  /* The sheet in points, taken from the same measurement the bands are in.
     Falling back to the image only when the map could not report it: "the PNG
     is 144dpi, so two pixels to a point" was a guess about somebody else's
     renderer, in a file whose every other number is measured. */
  const pageH=(box&&box.page_height)||img.naturalHeight/2;
  const pageW=(box&&box.page_width)||img.naturalWidth/2;
  /* Hug the text column when we know where it is. Spanning the whole sheet
     reads as a band laid across the paper rather than a mark on the entry. */
  const pad=6;
  const left=box?Math.max(0,(box.x0-pad)/pageW*100):0;
  const right=box?Math.max(0,(pageW-box.x1-pad)/pageW*100):0;
  const pct=y=>(Math.max(0,Math.min(pageH,y))/pageH)*100;
  wrap.insertAdjacentHTML("beforeend", bandsOn(S.page).map(b=>{
    const top=pct(b.y0), bottom=b.y1==null?100:pct(b.y1);
    if(bottom-top<=0) return "";
    const name=bandLabel(b);
    return '<button class="hit'+(sameBlock(b,S.sel)?" sel":"")+'" tabindex="-1"'+
      ' style="top:'+top.toFixed(3)+'%;height:'+(bottom-top).toFixed(3)+'%;'+
      'left:'+left.toFixed(3)+'%;right:'+right.toFixed(3)+'%"'+
      ' data-k="'+b.k+'" data-name="'+esc(b.name==null?"":b.name)+'"'+
      ' data-i="'+(b.i==null?"":b.i)+'" title="'+esc(name)+'"'+
      ' aria-label="Edit '+esc(name)+'"></button>';
  }).join(""));
  wrap.querySelectorAll(".hit").forEach(el=>el.onclick=()=>{
    const k=el.dataset.k;
    if(k==="header") select({kind:"header"});
    else if(k==="section") select({kind:"section",name:el.dataset.name});
    else select({kind:"entry",name:el.dataset.name,i:+el.dataset.i});
    /* Beside a form or the source, clicking a block is navigation: select()
       has already scrolled the left pane to it and marked it. */
    if(S.tab==="page") openEditor();
    if(S.tab==="diff") dfCard(el.dataset.us);
  });
  if(S.tab==="diff"){
    wrap.onmouseover=e=>{ const h=e.target.closest(".hit.dfchg"); dfHot(h&&h.dataset.us.split(" ")[0]) };
    wrap.onmouseleave=()=>dfHot(null);
  }else{ wrap.onmouseover=null; wrap.onmouseleave=null }
  paintPageMarks(wrap,img,left);
}

/* The mark in the margin, beside the block it belongs to.

   It goes to the left of the text column, which the band map already measures
   as box.x0 -- that strip is blank on every theme, so the mark reads as an
   annotation on the page rather than something printed on it. Which it is: the
   page underneath is the PNG RenderCV produced, and this is a div on top. What
   you export has never been near it.

   Entry granularity, because that is the granularity of the bands: the probes
   in cv_map sit at column 0 and a theme nests its bullets inside the entry
   call, so there is nothing to hang a per-bullet mark on yet. A mark against
   the job you rewrote is the useful half of that anyway. */
/* Entries and the header only. A section heading is not a thing anybody edits
   -- its mark would come from its entries, and it sits directly above the
   first of them, so marking both puts two marks a few millimetres apart
   saying the same thing. The outline is where a section answers for itself. */
function bandProv(b){
  if(b.k==="header") return provHeader();
  if(b.k==="section") return null;
  return provUnder(["cv","sections",b.name,b.i]);
}
function paintPageMarks(wrap,img,leftPct){
  wrap.querySelectorAll(".pgmark,.pgchg").forEach(el=>el.remove());
  if(!S.prov) return;
  const pageH=img.naturalHeight/2;
  if(S.tab==="diff") return paintDiffMarks(wrap,leftPct,pageH);
  const seen=new Set();
  const html=bandsOn(S.page).map(b=>{
    const p=bandProv(b);
    if(!p) return "";
    /* A block pushed over a page break owns a band on each page it touches;
       one mark per block per page is the honest count. */
    const id=b.k+"/"+b.name+"/"+b.i;
    if(seen.has(id)) return "";
    seen.add(id);
    /* The first block of the page reaches up into the top margin, so its y0
       is zero and a mark placed there hangs off the sheet. Hold it far enough
       down to sit beside the text it marks. */
    const top=(Math.max(14,Math.min(pageH,b.y0))/pageH)*100;
    return '<span class="pgmark" data-by="'+esc(p.by)+'" title="'+
      esc(whoLabel(p)+" wrote something in "+bandLabel(b)+", "+
          ago(p.at*1000)+" ago")+
      '" style="top:'+top.toFixed(3)+'%;left:'+leftPct.toFixed(3)+'%">'+
      (p.by==="ai"?"&#9679;":'<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#'+
        esc(p.by)+'-mark"/></svg>')+'</span>';
  }).join("");
  wrap.insertAdjacentHTML("beforeend",html);
}

/* Keep the page in step with a selection made anywhere else, following it to
   whichever page it is actually on. */
function trackSelectionOnPage(){
  if(!S.render||!S.render.map) return;
  const hit=S.render.map.find(b=>sameBlock(b,S.sel));
  if(hit&&hit.page-1!==S.page){ S.page=hit.page-1; paintPage() }
  else paintHits();
}

/* How full the last page is, measured off the rendered image rather than
   guessed from a word count. The top margin tells us where the bottom margin
   is, which keeps page numbering in the footer from reading as content. */
function measureFill(url){
  return new Promise(res=>{
    const img=new Image();
    img.onload=()=>{
      try{
        const w=Math.min(200,img.naturalWidth);
        const h=Math.max(1,Math.round(img.naturalHeight*w/img.naturalWidth));
        const c=document.createElement("canvas"); c.width=w; c.height=h;
        const x=c.getContext("2d",{willReadFrequently:true});
        x.fillStyle="#fff"; x.fillRect(0,0,w,h);
        x.drawImage(img,0,0,w,h);
        const d=x.getImageData(0,0,w,h).data;
        const inked=[];
        for(let y=0;y<h;y++){
          for(let px=0;px<w;px++){
            const i=(y*w+px)*4;
            if(d[i]<212||d[i+1]<212||d[i+2]<212){ inked.push(y); break }
          }
        }
        if(!inked.length) return res(0);
        const top=inked[0];
        const limit=h-top;                       /* the matching bottom margin */
        let bottom=top;
        for(const y of inked){ if(y<=limit) bottom=y }
        const usable=Math.max(1,h-2*top);
        res(Math.max(0,Math.min(1,(bottom-top)/usable)));
      }catch(err){ res(null) }
    };
    img.onerror=()=>res(null);
    img.src=url;
  });
}

function paintBudget(){
  const b=$("#budget"), r=S.render;
  if(!r){ b.hidden=true; return }
  b.hidden=false;
  b.querySelector(".pp").textContent=r.pages+" page"+(r.pages===1?"":"s");
  b.querySelector(".ww").textContent=(r.ats_words||0)+" words";
  const pct=S.fill==null?null:Math.round(S.fill*100);
  /* Any ink on the page should light a segment: rounding 8% to zero made a
     nearly-empty page and a blank one look the same. */
  const on=pct==null?0:(S.fill>0?Math.max(1,Math.ceil(S.fill*6)):0);
  [...b.querySelectorAll(".bar i")].forEach((el,i)=>el.classList.toggle("on",i<on));
  b.querySelector(".bar").style.visibility=pct==null?"hidden":"";
  b.querySelector(".cap").textContent=fillCaption(r.pages,pct);
}
/* The sentence changes with the number but never claims more than the
   measurement supports. */
function fillCaption(pages,pct){
  if(pct==null) return "Page fill could not be measured.";
  const p="Page "+pages+" is "+pct+"% full";
  if(pct>=96) return p+". No room left on it.";
  if(pages>1&&pct<=35) return p+". Most of the last page is empty.";
  return p+".";
}

/* ---- live preview ---------------------------------------------------------
   A debounce so a render starts only once typing pauses, and a token so a slow
   render finishing after a newer one cannot overwrite the fresher result.
   Errors while mid-edit leave the last good page on screen rather than
   flashing a red panel at every keystroke. */
let liveTimer=null, liveToken=0;
function scheduleLive(){
  if(prefs().live===false||!S.path) return;
  clearTimeout(liveTimer);
  liveTimer=setTimeout(runLive, prefs().delay||700);
}
async function runLive(){
  if(!S.path||prefs().live===false) return;
  const token=++liveToken;
  S.live="working"; paintStatus();
  const body=S.tab==="yaml" ? {path:S.path,yaml:$("#yaml").value}
                            : {path:S.path,patches:collectPatches()};
  const t0=performance.now();
  try{
    const r=await post("/api/preview",body);
    if(token!==liveToken) return;
    if(r.ok){
      S.renderMs=Math.round(performance.now()-t0);
      S.live="ok"; await keepFocus(()=>adoptRender(r));
    }else{ S.live="bad"; S.liveMsg=r.hint||"not valid yet" }
  }catch(e){ if(token===liveToken){ S.live="bad"; S.liveMsg=e.message } }
  paintStatus();
}

$("#btn-pdf").onclick=()=>{ if(S.pdf)
  download("/api/asset?path="+encodeURIComponent(S.pdf)+tok(),S.pdf.split("/").pop()) };

/* ---- YAML: highlighting painted behind a transparent-text textarea, so
   native undo, selection and IME keep working ---- */
function commentAt(s){
  let q=null;
  for(let i=0;i<s.length;i++){
    const c=s[i];
    if(q){ if(c===q) q=null; continue }
    if(c==='"'||c==="'"){ q=c; continue }
    if(c==="#"&&(i===0||/\s/.test(s[i-1]))) return i;
  }
  return -1;
}
function hlScalar(v){
  if(!v) return "";
  const t=v.trim(); if(!t) return v;
  const lead=v.slice(0,v.indexOf(t[0])), tail=v.slice(lead.length+t.length);
  let inner;
  if(/^[|>][-+]?\d*$/.test(t))                     inner='<span class="t-blk">'+t+'</span>';
  else if(/^".*"$/.test(t)||/^'.*'$/.test(t))      inner='<span class="t-str">'+t+'</span>';
  else if(/^(true|false|null|~|yes|no)$/i.test(t)) inner='<span class="t-bool">'+t+'</span>';
  else if(/^-?\d+(\.\d+)?$/.test(t))               inner='<span class="t-num">'+t+'</span>';
  else if(/^\d{4}-\d{2}(-\d{2})?$/.test(t))        inner='<span class="t-num">'+t+'</span>';
  else                                             inner=t;
  return lead+inner+tail;
}
function hlLine(line){
  const s=line.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  const whole=s.match(/^(\s*)(#.*)$/);
  if(whole) return whole[1]+'<span class="t-com">'+whole[2]+'</span>';
  let code=s, comment="";
  const ci=commentAt(s);
  if(ci>=0){ code=s.slice(0,ci); comment='<span class="t-com">'+s.slice(ci)+'</span>' }
  const m=code.match(/^(\s*)((?:-\s+)?)(.*)$/);
  let out=m[1]+(m[2]?'<span class="t-punc">'+m[2]+'</span>':"");
  const kv=m[3].match(/^([^:\s][^:]*?)(:)(\s*)(.*)$/);
  out += kv ? '<span class="t-key">'+kv[1]+'</span><span class="t-punc">:</span>'+kv[3]+
              hlScalar(kv[4])
            : hlScalar(m[3]);
  return out+comment;
}
function paint(){
  const ta=$("#yaml");
  /* trailing spacer keeps both layers the same height so the caret stays put */
  $("#hl").innerHTML=ta.value.split("\n").map(hlLine).join("\n")+"\n ";
  $("#hl").scrollTop=ta.scrollTop; $("#hl").scrollLeft=ta.scrollLeft;
}
$("#yaml").addEventListener("input",()=>{ paint(); touch() });
$("#yaml").addEventListener("scroll",()=>{
  $("#hl").scrollTop=$("#yaml").scrollTop; $("#hl").scrollLeft=$("#yaml").scrollLeft });

/* A parse error names a line; saying which one, and being able to jump to it,
   is most of the fix. The preview keeps the last good page meanwhile. */
function setYamlError(err){
  const box=$("#yamlerr");
  if(!err){ box.hidden=true; return }
  const line=(/line (\d+)/i.exec(err)||[])[1];
  box.hidden=false;
  box.querySelector("span").textContent=
    (line?"Line "+line+": ":"")+String(err).split("\n")[0].slice(0,180);
  const go=box.querySelector("button");
  go.hidden=!line;
  go.onclick=()=>{
    const ta=$("#yaml"), lines=ta.value.split("\n");
    const at=lines.slice(0,Math.max(0,+line-1)).join("\n").length+(line>1?1:0);
    ta.focus(); ta.setSelectionRange(at,at+(lines[+line-1]||"").length);
    ta.scrollTop=Math.max(0,(+line-4)*21);
  };
  if(S.tab!=="yaml") toast("This file has a YAML error. Open the YAML tab to fix it.",true);
}

