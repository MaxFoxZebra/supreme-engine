/* Window chrome, overlays/sheets, sample workspace switch, and boot(). */
/* ---- window chrome ------------------------------------------------------
   The page is served from the local server, so the Tauri API is only there
   when running inside the app. In a plain browser the traffic lights would be
   decoration that does nothing, so they stay hidden. */
(function(){
  const T=window.__TAURI__;
  if(!T||!T.window) return;
  const win=T.window.getCurrentWindow();
  $("#lights").hidden=false;
  $("#w-min").onclick=()=>win.minimize();
  $("#w-max").onclick=()=>win.toggleMaximize();
  $("#w-close").onclick=()=>win.close();
  $("#chrome").addEventListener("dblclick",e=>{
    if(e.target.closest("button,select,input,label")) return;
    win.toggleMaximize();
  });
})();

/* ---- overlays and sheets ------------------------------------------------ */
function closeOverlays(){
  $("#ovl-design").hidden=true; $("#ovl-settings").hidden=true;
  if(window.SC&&SC.open) closeScans();
}
$$("[data-close-ovl]").forEach(b=>b.onclick=closeOverlays);

let sheetOnClose=null;
function openSheet(html,onClose){
  $("#sheet").innerHTML=html;
  $("#sheet").hidden=false; $("#scrim").hidden=false;
  sheetOnClose=onClose||null;
  const first=$("#sheet input,#sheet select,#sheet button");
  if(first) first.focus();
}
function closeSheet(){
  $("#sheet").hidden=true; $("#scrim").hidden=true; $("#sheet").innerHTML="";
  if(sheetOnClose){ const f=sheetOnClose; sheetOnClose=null; f() }
}
$("#scrim").onclick=closeSheet;

/* The app's own dialogs, in place of the browser's confirm() and prompt():
   those looked like a system error, could not say which choice was the
   dangerous one, and could not offer a third way out ("save and continue").
   They sit above everything, sheets included, and take Enter and Escape for
   themselves while open. */
function askDialog({title,body,ok,cancel,danger,alt,input}){
  return new Promise(done=>{
    const back=document.createElement("div"); back.className="dlg-back";
    const box=document.createElement("div"); box.className="dlg"; box.setAttribute("role","alertdialog");
    box.setAttribute("aria-modal","true"); box.setAttribute("aria-labelledby","dlg-t");
    box.innerHTML='<h3 id="dlg-t">'+esc(title)+'</h3>'+(body?'<p>'+esc(body)+'</p>':'')+
      (input?'<input class="dlg-in" id="dlg-in" spellcheck="false" autocomplete="off" placeholder="'+esc(input.placeholder||"")+'">'+
        '<span class="dlg-err" id="dlg-err" role="alert"></span>':'')+
      '<div class="dlg-foot"><button class="sbtn" data-v="cancel">'+esc(cancel||t("Cancel"))+'</button>'+
        (alt?'<button class="sbtn'+(alt.danger?' danger':'')+'" data-v="alt">'+esc(alt.label)+'</button>':'')+
        '<button class="sbtn primary'+(danger?' danger':'')+'" data-v="ok">'+esc(ok||t("OK"))+'</button></div>';
    document.body.append(back,box);
    const inp=box.querySelector("#dlg-in"), prev=document.activeElement;
    if(inp){ inp.value=input.value||""; inp.focus(); inp.select() } else box.querySelector('[data-v="ok"]').focus();
    const finish=v=>{ document.removeEventListener("keydown",keys,true); back.remove(); box.remove();
      if(prev&&prev.focus&&prev.isConnected) try{ prev.focus({preventScroll:true}) }catch(e){}
      done(v) };
    const submit=()=>{ if(!inp) return finish("ok");
      const v=inp.value.trim(), bad=input.check&&input.check(v);
      if(bad){ box.querySelector("#dlg-err").textContent=bad; inp.setAttribute("aria-invalid","true"); inp.focus(); return }
      finish({value:v}) };
    const keys=e=>{
      if(e.key==="Escape"){ e.preventDefault(); e.stopImmediatePropagation(); finish(null) }
      else if(e.key==="Enter"&&(!e.target.closest||!e.target.closest(".dlg-foot"))){ e.preventDefault(); e.stopImmediatePropagation(); submit() }
      else if(e.key==="Tab"){ const f=[...box.querySelectorAll("input,button")]; const i=f.indexOf(document.activeElement);
        if(e.shiftKey&&i<=0){ e.preventDefault(); f[f.length-1].focus() } else if(!e.shiftKey&&i===f.length-1){ e.preventDefault(); f[0].focus() } }
    };
    document.addEventListener("keydown",keys,true);
    back.onclick=()=>finish(null);
    box.querySelector(".dlg-foot").onclick=e=>{ const b=e.target.closest("[data-v]"); if(!b) return;
      if(b.dataset.v==="ok") submit(); else finish(b.dataset.v==="alt"?"alt":null) };
  });
}
const askConfirm=async o=>(await askDialog(o))==="ok";
const askText=async o=>{ const r=await askDialog({...o,input:o.input||{}}); return r&&r.value!=null?r.value:null };
/* Leaving a CV with unsaved edits: keep editing, throw them away, or save
   them and go on -- the choice the browser's box could not offer. */
async function leaveEdits(){
  if(!S.dirty) return true;
  const r=await askDialog({title:t("Unsaved changes"),body:t("This CV has changes you have not saved."),
    ok:t("Save and continue"),cancel:t("Keep editing"),alt:{label:t("Discard them"),danger:true}});
  if(r==="alt"){ S.dirty=false; return true }
  if(r==="ok"){ await save(); return !S.dirty }
  return false;
}
document.addEventListener("keydown",e=>{
  /* One Escape, one step back. Whatever handles it claims it
     (preventDefault), and the handlers after this one leave a claimed key
     alone: before, closing a sheet also closed the application behind it. */
  if(e.key==="Escape"&&!e.defaultPrevented){
    if(!$("#sheet").hidden){ e.preventDefault(); return closeSheet() }
    if(!$("#ovl-design").hidden||!$("#ovl-settings").hidden){ e.preventDefault(); return closeOverlays() }
    if(S.view==="cvs"&&!$("#ed").hidden){ e.preventDefault(); return closeEditor() }
    /* In a letter, back to where you came from; it saves on the way. */
    if(S.view==="letter"){
      const el=document.activeElement;
      if(el&&/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      e.preventDefault();
      return $("#lt-back").click();
    }
    /* Last in the chain: once the sheet, the overlays and the block editor
       have each had their turn, Escape in the editor is the way back out of
       it. Not while typing -- Escape in a field belongs to the field. */
    if(S.view==="cvs"){
      const el=document.activeElement;
      if(el&&/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      e.preventDefault();
      return goBack();
    }
  }
  /* Arrows walk the document while the editor is open, so you can read a CV
     block by block without reaching for the outline. Out of the way of a
     field being typed in. */
  if(S.view==="cvs"&&!$("#ed").hidden&&
     (e.key==="ArrowUp"||e.key==="ArrowDown")){
    const el=document.activeElement;
    if(el&&el.closest("#ed")&&/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
    e.preventDefault();
    return editorStep(e.key==="ArrowDown"?1:-1);
  }
  /* The letter has its own; elsewhere there is nothing on screen to save,
     and the last CV opened would be written from what it held then. */
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){ e.preventDefault(); if(S.view==="cvs") save() }
});
window.addEventListener("beforeunload",e=>{
  if(LT.dirty&&LT.path) ltSave();
  if(S.dirty||LT.dirty){e.preventDefault();e.returnValue=""}});

/* ---- boot --------------------------------------------------------------- */
async function boot(){
  let d;
  try{ d=await api("/api/state") }catch(e){ return window.studioError(e.message) }
  S.state=d;
  /* Back where you left off. Which half of the split you work in is a habit,
     not a per-document setting, so it is remembered with the other
     per-machine conveniences. */
  const was=prefs().tab;
  if(was==="form"||was==="yaml") showTab(was);
  renderDocs(d.documents);
  /* Home is the applications list. setView makes the jobs calls itself, and
     letting it do so un-quieted is the point: a jobs store that will not open
     is now a broken home screen, which should say so rather than wait to be
     visited. */
  setView("jobs");
  loadScans();
  paintBase();
  /* No document is open, and none can be reached except by opening one, so
     there is no empty editor to write anything into. */
  $("#btn-render").disabled=true;
  buildOutline();   /* nothing is open, so the Outline heading goes too */
  loadAI();
  pulse();
  setInterval(pulse,2500);
  paintStatus();
  const pill=$("#samp-pill");
  pill.hidden=!d.sample; pill.onclick=()=>setSample(false,pill);
  if(!d.sample&&shouldOnboard(d)) onboardingSheet();
  /* A time zone change reloads the page; come back to where it was made. */
  let back=null; try{ back=sessionStorage.getItem("cvs.reopen"); sessionStorage.removeItem("cvs.reopen") }catch(e){}
  if(back) openSettings(back);
  /* Reloaded on a document or an application: back there. */
  if(/^#\/(doc|letter|jobs(\/|$)|docs|funnel|cal)/.test(location.hash)) navGo(location.hash);
  else history.replaceState(null,"",location.pathname+location.search+navHere());
  /* Framed beside the chat by CV Studio's view in Claude: say it is up. */
  if(window.parent!==window) try{ window.parent.postMessage({cvsApp:"ready"},"*") }catch(e){}
}
/* Into the sample folder, or back to your own. Everything on screen belongs
   to one workspace, so the page starts over in the other one. */
async function setSample(on,btn){
  if(btn){ btn.disabled=true; if(on) btn.textContent="Making it…" }
  try{
    const r=await post("/api/sample",{on});
    if(!r.ok) throw new Error(r.error||"Could not switch");
    location.reload();
  }catch(e){ if(btn) btn.disabled=false; toast(e.message,true) }
}

