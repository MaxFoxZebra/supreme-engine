/* Reviewing AI client changes and tailored-vs-base Changes tab. */
/* =========================================================================
   Reviewing what an AI client changed
   ========================================================================= */
/* The first time an AI client writes a document, the server keeps what it
   said before (review.py). Until every change since has been kept or undone,
   the document carries them as units -- a paragraph, a header line, a CV
   entry -- each shown as the words it took out and the words it put in, with
   Keep and Undo beside it. The provenance marks say who touched a field; this
   says what they did to it, and lets you take it back. */

/* Words, spaces and punctuation, so a diff reads as words changing rather
   than letters. */
const rvTok=s=>String(s??"").match(/\s+|[\p{L}\p{N}_'’-]+|[^\s\p{L}\p{N}_]/gu)||[];
function wdiff(a,b){
  const A=rvTok(a), B=rvTok(b), n=A.length, m=B.length, W=m+1;
  if(n*m>600000) return [["del",String(a??"")],["ins",String(b??"")]];
  const T=new Uint32Array((n+1)*(m+1));
  for(let i=n-1;i>=0;i--) for(let j=m-1;j>=0;j--)
    T[i*W+j]=A[i]===B[j]?T[(i+1)*W+j+1]+1:Math.max(T[(i+1)*W+j],T[i*W+j+1]);
  const raw=[]; let i=0, j=0;
  while(i<n&&j<m){
    if(A[i]===B[j]){ raw.push(["eq",A[i++]]); j++ }
    else if(T[(i+1)*W+j]>=T[i*W+j+1]) raw.push(["del",A[i++]]);
    else raw.push(["ins",B[j++]]);
  }
  while(i<n) raw.push(["del",A[i++]]); while(j<m) raw.push(["ins",B[j++]]);
  /* A run of changes reads as what went, then what came: the spaces and
     one-letter words the two happen to share inside it are part of the
     change, not islands of sameness breaking it up. */
  const out=[]; let del="", ins="";
  const flush=()=>{ if(del) out.push(["del",del]); if(ins) out.push(["ins",ins]); del=ins="" };
  for(let k=0;k<raw.length;k++){
    const [op,tx]=raw[k];
    const inside=op==="eq"&&(del||ins)&&/^\s*\S?\s*$/.test(tx)&&raw.slice(k+1).some(x=>x[0]!=="eq")&&
      raw[k+1]&&raw[k+1][0]!=="eq";
    if(op==="del"||inside) del+=tx;
    if(op==="ins"||inside) ins+=tx;
    if(op==="eq"&&!inside){ flush(); const l=out[out.length-1];
      if(l&&l[0]==="eq") l[1]+=tx; else out.push(["eq",tx]) }
  }
  flush();
  return out;
}
const rvText=s=>esc(s).replace(/\n/g,"<br>");
function diffHTML(a,b){
  return wdiff(a,b).map(([op,tx])=>op==="eq"?rvText(tx)
    :op==="del"?'<del class="rvd">'+rvText(tx)+'</del>':'<ins class="rvi">'+rvText(tx)+'</ins>').join("");
}
/* Where a paragraph changed, with a few words either side: the start of a
   paragraph says nothing when the edit is at its end. */
function rvExcerpt(a,b,room=48){
  const ops=wdiff(a,b), first=ops.findIndex(o=>o[0]!=="eq");
  if(first<0) return esc(b.slice(0,110));
  let last=first; for(let k=first;k<ops.length;k++) if(ops[k][0]!=="eq") last=k;
  const before=first>0?ops[first-1][1]:"", after=last+1<ops.length?ops[last+1][1]:"";
  const head=before.length>room?"…"+before.slice(-room).replace(/^\S*\s/,""):before;
  const tail=after.length>room?after.slice(0,room).replace(/\s\S*$/,"")+"…":after;
  const mid=ops.slice(first,last+1).map(([op,tx])=>op==="eq"?(tx.length>room*2?rvText(tx.slice(0,room))+" … "+rvText(tx.slice(-room)):rvText(tx))
    :op==="del"?'<del class="rvd">'+rvText(tx)+'</del>':'<ins class="rvi">'+rvText(tx)+'</ins>').join("");
  return rvText(head)+mid+rvText(tail);
}
function diffStats(a,b){
  let plus=0, minus=0;
  const words=s=>(s.match(/[\p{L}\p{N}]+/gu)||[]).length;
  wdiff(a,b).forEach(([op,tx])=>{ if(op==="ins") plus+=words(tx); else if(op==="del") minus+=words(tx) });
  return {plus,minus};
}
/* Markdown as it reads, for comparing what a paragraph says. */
const rvPlain=md=>String(md??"").replace(/^\s*#{1,6}\s+/gm,"").replace(/^\s*>\s?/gm,"").replace(/```/g,"")
  .replace(/\*\*(.+?)\*\*|__(.+?)__/g,"$1$2").replace(/~~(.+?)~~/g,"$1").replace(/`([^`]+)`/g,"$1")
  .replace(/\*(.+?)\*|_(.+?)_/g,"$1$2").replace(/\[([^\]]+)\]\([^)]+\)/g,"$1").replace(/^\s*[-*+] /gm,"• ")
  .replace(/\\([\\`*_{}\[\]()#+\-.!~|>])/g,"$1");
function rvVal(v){
  if(v==null||v==="") return "";
  if(Array.isArray(v)) return v.map(rvVal).join(" · ");
  if(typeof v==="object") return Object.entries(v).filter(([,x])=>x!=null&&x!=="")
    .map(([k,x])=>k.replace(/_/g," ")+": "+rvVal(x)).join(" · ");
  return String(v);
}
function rvIcon(by){
  return by&&by!=="ai"&&by!=="you"
    ?'<svg class="rvlogo" viewBox="0 0 24 24" aria-hidden="true"><use href="#'+esc(by)+'-mark"/></svg>'
    :'<span class="rvlogo dot" aria-hidden="true"></span>';
}
async function rvCall(path,ids,action,sig){
  try{ return await post("/api/review",{path,ids,action,sig}) }
  catch(e){ toast(e.message,true); return null }
}
/* The bar above a document with changes waiting: who, when, how many, and
   the three things to do about them. */
function rvBar(el,r,kind,on){
  if(!r){ el.hidden=true; el.innerHTML=""; return }
  const who=whoLabel(r), n=r.units.length;
  const what=kind==="letter"?t("this letter"):t("this CV");
  /* Made by copying the base: what it is compared with, by name, rather
     than "wrote this CV", which said nothing of what it changed. */
  const from=r.base?r.base.split("/").pop().replace(/\.ya?ml$/,""):null;
  const all=(r.created?'<button class="obtn" data-rv="undo">'+esc(t("Delete it"))+'</button>'
      :'<button class="obtn" data-rv="undo">'+esc(t("Undo all"))+'</button>')+
    '<button class="obtn" data-rv="keep">'+esc(t("Keep all"))+'</button>';
  el.innerHTML=rvIcon(r.by)+
    '<div class="rvtx"><b>'+esc(from?t("{who} tailored this CV from {base}",{who,base:from})
        :r.created?t("{who} wrote {what}",{who,what}):t("{who} changed {what}",{who,what}))+'</b>'+
      '<span>'+esc(from?t("{t} ago · {n} change(s) from it, not reviewed yet",{t:ago(r.at*1000),n})
        :r.created?t("{t} ago. Not reviewed yet.",{t:ago(r.at*1000)})
        :t("{t} ago · {n} change(s) to review",{t:ago(r.at*1000),n}))+'</span></div>'+
    '<div class="grow"></div>'+
    (r.created&&!from
      ?'<button class="obtn" data-rv="undo">'+esc(t("Delete it"))+'</button>'+
       '<button class="pbtn" data-rv="keep">'+esc(t("Keep it"))+'</button>'
      :on.open?all+'<button class="pbtn" data-rv="open">'+esc(on.label||t("Review changes"))+'</button>'
      :on.note?'<span class="rvnote">'+esc(on.note)+'</span>'+all
      :'<span class="rvnote">'+esc(t("Decide on each change beside the page, or all at once at the bottom."))+'</span>');
  el.hidden=false;
  el.onclick=async e=>{ const b=e.target.closest("[data-rv]"); if(!b) return;
    if(b.dataset.rv==="open") return on.open();
    if(b.dataset.rv==="undo"&&!await askConfirm(r.created
      ?{title:t("Delete this document?"),body:t("It goes to the trash folder."),ok:t("Delete it"),danger:true}
      :{title:t("Undo all of {who}’s changes?",{who}),body:t("Everything it changed that you have not kept goes back to how it was."),ok:t("Undo all"),danger:true})) return;
    on.all(b.dataset.rv);
  };
}
/* One change, as a card: what it is, the words, and Keep / Undo. */
function rvCard(u,i,body,extra){
  return '<div class="rvc" data-u="'+esc(u.id)+'"><div class="rvh"><span class="rvn">'+(i+1)+'</span>'+
      '<b>'+esc(t(u.label))+'</b>'+(u.where?'<span class="rvw">'+esc(u.where)+'</span>':'')+'</div>'+
    (body?'<div class="rvx">'+body+'</div>':'')+
    '<div class="rva">'+(extra||"")+'<div class="grow"></div>'+
      '<button class="obtn" data-act="undo" data-id="'+esc(u.id)+'">'+esc(t("Undo"))+'</button>'+
      '<button class="pbtn rvkeep" data-act="keep" data-id="'+esc(u.id)+'">'+esc(t("Keep"))+'</button></div></div>';
}

/* ---- letters ------------------------------------------------------------- */
const ltRv=()=>LT.doc&&LT.doc.review;
function ltRvBar(){
  const r=ltRv();
  if(r&&LT.rv&&r.created) LT.rv=false;
  rvBar($("#lt-rv"),r,"letter",{
    open:LT.rv?null:()=>ltRvEnter(),
    all:a=>ltRvDo(["*"],a)});
}
async function ltRvEnter(){
  if(LT.dirty) await ltSave();
  if(!ltRv()) return;
  LT.rv=true; if(LTF.open) ltFindClose();
  LT.tab="write"; ltPaint();
}
function ltRvLeave(){ LT.rv=false; ltPaint() }
async function ltRvDo(ids,action){
  const r=ltRv(); if(!r) return;
  if(LT.dirty) await ltSave();
  const out=await rvCall(LT.path,ids,action,ltRv().sig);
  if(!out){ try{ const d=await api("/api/doc?path="+encodeURIComponent(LT.path));
    Object.assign(LT,{doc:d,meta:Object.assign({},d.meta),body:d.body}) }catch(e){} ltPaint(); return }
  if(out.deleted){ toast(t("Deleted. It is in the trash folder.")); LT.path=null; LT.rv=false;
    try{ const st=await api("/api/state"); S.state=st; renderDocs(st.documents) }catch(e){}
    return setView(ltJob()?"jobs":"docs") }
  const d=out.doc;
  Object.assign(LT,{doc:d,meta:Object.assign({},d.meta),body:d.body,dirty:false,mdText:null});
  if(!d.review){ LT.rv=false; toast(ids[0]==="*"?(action==="keep"?t("Kept"):t("Undone")):t("All reviewed")) }
  ltPaint();
  if(action==="undo") ltRender();
  refreshDocBadges();
}
/* The page while reviewing: the letter as it is now, read-only, with each
   change drawn where it is -- the words taken out struck through, the words
   put in underlined, a paragraph removed shown where it was -- and numbered
   to match its card. */
function ltRvPage(){
  const r=ltRv(); if(!r) return;
  const paper=$("#lt-paper"); if(!paper) return;
  paper.classList.add("reviewing");
  paper.querySelectorAll("[contenteditable]").forEach(el=>el.setAttribute("contenteditable","false"));
  const units=r.units, num=new Map(units.map((u,i)=>[u.id,i+1]));
  const chunks=String(LT.body||"").trim().split(/\n\s*\n/).filter(c=>c.trim());
  const paras=units.filter(u=>u.kind==="para");
  const badge=u=>'<span class="rvnum" aria-hidden="true">'+num.get(u.id)+'</span>';
  const block=(u,html)=>'<div class="rvb" data-tag="'+u.tag+'" data-u="'+esc(u.id)+'">'+badge(u)+html+'</div>';
  const para=u=>{
    if(u.tag==="del") return block(u,'<p><del class="rvd">'+rvText(rvPlain(u.before))+'</del></p>');
    if(u.tag==="ins") return block(u,ltToHTML(u.after));
    const a=rvPlain(u.before), b=rvPlain(u.after);
    return block(u,a===b?ltToHTML(u.after)+'<p class="rvfmt">'+esc(t("Only the formatting changed."))+'</p>'
      :'<p>'+diffHTML(a,b)+'</p>');
  };
  let html="";
  for(let k=0;k<=chunks.length;k++){
    paras.filter(u=>u.tag==="del"&&u.at===k).forEach(u=>html+=para(u));
    if(k===chunks.length) break;
    const u=paras.find(x=>x.tag!=="del"&&x.at===k);
    html+=u?para(u):ltToHTML(chunks[k]);
  }
  $("#lt-edit").innerHTML=html;
  const sub=units.find(u=>u.id==="meta:subject");
  if(sub){ const sj=$("#lt-subj"); sj.classList.add("rvb"); sj.dataset.u=sub.id;
    sj.innerHTML=badge(sub)+diffHTML(sub.before||"",sub.after||"") }
  const lh=units.find(u=>u.id==="meta:letterhead");
  if(lh){ const el=$("#lt-lh"); el.classList.add("rvb"); el.dataset.u=lh.id;
    el.insertAdjacentHTML("afterbegin",badge(lh)) }
  const dt=units.find(u=>u.id==="meta:date"||u.id==="meta:place");
  if(dt){ const el=$("#lt-dl"); el.classList.add("rvb"); el.dataset.u=dt.id;
    el.insertAdjacentHTML("afterbegin",badge(dt)) }
  paper.onmouseover=e=>{ const b=e.target.closest(".rvb"); rvHover(b&&b.dataset.u) };
  paper.onmouseleave=()=>rvHover(null);
  /* Not editable here, so its links would be followed: in the app's own
     window, wherever an AI client pointed them. */
  paper.onclick=e=>{ if(e.target.closest("a")) e.preventDefault();
    const b=e.target.closest(".rvb"); if(b) rvFocus(b.dataset.u,"card") };
}
function rvHover(id){
  $$(".rvb.hot,.rvc.hot").forEach(x=>x.classList.remove("hot"));
  if(id) $$('[data-u="'+CSS.escape(id)+'"]').forEach(x=>x.classList.add("hot"));
}
function rvFocus(id,where){
  const sel='[data-u="'+CSS.escape(id)+'"]';
  const el=where==="card"?$("#lt-panel "+sel):$("#lt-paper "+sel);
  if(!el) return;
  el.scrollIntoView({block:"center",behavior:"smooth"});
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
}
function ltRvPanel(){
  const r=ltRv(), units=r.units;
  const card=(u,i)=>{
    let body="";
    if(u.kind==="para"){
      const a=rvPlain(u.before||""), b=rvPlain(u.after||"");
      const st=diffStats(a,b);
      body='<span class="rvs">'+(u.tag==="del"?esc(t("{n} word(s) removed",{n:st.minus}))
        :u.tag==="ins"?esc(t("{n} word(s) added",{n:st.plus}))
        :(st.plus||st.minus)?(st.plus?'<ins class="rvi">+'+st.plus+'</ins> ':'')+(st.minus?'<del class="rvd">−'+st.minus+'</del> ':'')+esc(t("words"))
        :esc(t("Formatting only")))+'</span>'+
        '<span class="rvq">'+(u.tag==="chg"?rvExcerpt(a,b):esc((u.tag==="del"?a:b).slice(0,110))+((u.tag==="del"?a:b).length>110?"…":""))+'</span>';
    }else body=diffHTML(rvVal(u.before),rvVal(u.after))||'<span class="rvs">'+esc(t("(empty)"))+'</span>';
    return rvCard(u,i,body);
  };
  $("#lt-panel").innerHTML=
    '<div class="rvhead">'+rvIcon(r.by)+'<div><h4>'+esc(t("{who}’s changes",{who:whoLabel(r)}))+'</h4>'+
      '<span class="muted2">'+esc(t("Since {t} · {n} write(s)",{t:new Date(r.since*1000).toLocaleTimeString(uiLocale(),{hour:"2-digit",minute:"2-digit",hourCycle:clock12()?"h12":"h23"}),n:r.writes||1}))+'</span></div></div>'+
    '<p class="muted2 rvhelp">'+esc(t("Struck through is what it took out, underlined is what it put in. Keep a change to accept it, undo it to put back what was there."))+'</p>'+
    '<div class="rvlist">'+units.map(card).join("")+'</div>'+
    '<div class="rvfoot"><button class="obtn" data-all="undo">'+esc(t("Undo all"))+'</button>'+
      '<button class="obtn" data-all="keep">'+esc(t("Keep all"))+'</button><div class="grow"></div>'+
      '<button class="obtn" data-all="done">'+esc(t("Done for now"))+'</button></div>';
  const pn=$("#lt-panel");
  pn.onclick=async e=>{
    const b=e.target.closest("[data-act]");
    if(b) return ltRvDo([b.dataset.id],b.dataset.act);
    const a=e.target.closest("[data-all]");
    if(a){ if(a.dataset.all==="done") return ltRvLeave();
      if(a.dataset.all==="undo"&&!await askConfirm({title:t("Undo all of {who}’s changes?",{who:whoLabel(r)}),body:t("Everything it changed that you have not kept goes back to how it was."),ok:t("Undo all"),danger:true})) return;
      return ltRvDo(["*"],a.dataset.all) }
    const c=e.target.closest(".rvc"); if(c) rvFocus(c.dataset.u,"page");
  };
  pn.onmouseover=e=>{ const c=e.target.closest(".rvc"); rvHover(c&&c.dataset.u) };
  pn.onmouseleave=()=>rvHover(null);
}

/* ---- CVs ----------------------------------------------------------------- */
function cvRvBar(){
  const r=S.doc&&S.doc.review;
  const beside=!!r&&dfShowsAll(r);
  rvBar($("#cv-rv"),r,"cv",{
    open:!beside?()=>cvRvSheet():S.tab==="diff"?null:()=>showTab("diff"),
    label:beside?t("See what changed"):null,
    note:beside?t("Keep or undo each change beside the page."):null,
    all:a=>cvRvDo(["*"],a)});
}
async function cvRvDo(ids,action){
  const r=S.doc&&S.doc.review; if(!r) return;
  if(S.dirty){ toast(t("Save your edits first (Ctrl S), then decide on the changes."),true); return }
  /* "Delete it" on a copy of the base: the document goes, which undoing its
     changes would not do -- that leaves the copy, back as the base has it. */
  if(ids[0]==="*"&&action==="undo"&&r.created&&r.base){
    try{ await post("/api/doc/delete",{path:S.path}) }catch(e){ toast(e.message,true); return }
    closeSheet(); toast(t("Deleted. It is in the trash folder."));
    try{ const st=await api("/api/state"); S.state=st; renderDocs(st.documents) }catch(e){}
    S.path=null; refreshDocBadges(); return setView("docs");
  }
  const out=await rvCall(S.path,ids,action,r.sig);
  if(!out){ await reopenInPlace(); return }
  if(out.deleted){ closeSheet(); toast(t("Deleted. It is in the trash folder."));
    try{ const st=await api("/api/state"); S.state=st; renderDocs(st.documents) }catch(e){}
    S.path=null; return setView("docs") }
  if(action==="undo") await reopenInPlace();
  else { S.doc.review=out.doc.review; cvRvBar(); if(S.tab==="diff"){ paintDiff(); paintHits() } }
  refreshDocBadges();
  if(!S.doc.review){ closeSheet(); toast(ids[0]==="*"?(action==="keep"?t("Kept"):t("Undone")):t("All reviewed")) }
  else if(!$("#sheet").hidden&&$("#sheet .rvsheet")) cvRvSheet();
}
/* An entry, field by field: what stayed plainly, what changed as words, a
   list of bullets as bullets added, removed and reworded. */
/* `compact` folds the fields that stayed into one line naming them, for the
   Changes tab, where the card sits beside the page that already shows them. */
function rvEntryHTML(a,b,tag,compact){
  if(tag==="ins"||tag==="del"){
    const e=tag==="ins"?b:a, cls=tag==="ins"?"rvi":"rvd", el=tag==="ins"?"ins":"del";
    if(e==null||typeof e!=="object") return '<'+el+' class="'+cls+'">'+rvText(rvVal(e))+'</'+el+'>';
    return '<dl>'+Object.entries(e).map(([k,v])=>'<dt>'+esc(k.replace(/_/g," "))+'</dt><dd>'+
      (Array.isArray(v)?'<ul>'+v.map(x=>'<li><'+el+' class="'+cls+'">'+rvText(rvVal(x))+'</'+el+'></li>').join("")+'</ul>'
        :'<'+el+' class="'+cls+'">'+rvText(rvVal(v))+'</'+el+'>')+'</dd>').join("")+'</dl>';
  }
  if(typeof a!=="object"||typeof b!=="object"||!a||!b) return diffHTML(rvVal(a),rvVal(b));
  const keys=[...new Set([...Object.keys(a),...Object.keys(b)])];
  const kept=compact?keys.filter(k=>JSON.stringify(a[k])===JSON.stringify(b[k])):[];
  return '<dl>'+keys.filter(k=>!kept.includes(k)).map(k=>{
    const x=a[k], y=b[k], same=JSON.stringify(x)===JSON.stringify(y);
    let v;
    if(same) v='<span class="rvsame">'+rvText(rvVal(y))+'</span>';
    else if(Array.isArray(x)||Array.isArray(y)) v=rvListHTML(x||[],y||[]);
    else v=diffHTML(rvVal(x),rvVal(y));
    return '<dt>'+esc(k.replace(/_/g," "))+'</dt><dd'+(same?' class="same"':'')+'>'+v+'</dd>';
  }).join("")+'</dl>'+
  (kept.length?'<p class="rvkept">'+esc(t("Unchanged: {list}",{list:kept.map(k=>k.replace(/_/g," ")).join(", ")}))+'</p>':'');
}
function rvListHTML(x,y){
  const A=x.map(rvVal), B=y.map(rvVal), n=A.length, m=B.length, W=m+1;
  const T=new Uint32Array((n+1)*(m+1));
  for(let i=n-1;i>=0;i--) for(let j=m-1;j>=0;j--)
    T[i*W+j]=A[i]===B[j]?T[(i+1)*W+j+1]+1:Math.max(T[(i+1)*W+j],T[i*W+j+1]);
  const ops=[]; let i=0,j=0;
  while(i<n&&j<m){ if(A[i]===B[j]){ ops.push(["eq",A[i++]]); j++ }
    else if(T[(i+1)*W+j]>=T[i*W+j+1]) ops.push(["del",A[i++]]); else ops.push(["ins",B[j++]]) }
  while(i<n) ops.push(["del",A[i++]]); while(j<m) ops.push(["ins",B[j++]]);
  /* A bullet taken out and one put in where it was is a bullet reworded --
     but only paired with the one that says most nearly the same thing. Pairing
     whatever happened to be adjacent turned two bullets rewritten in a row into
     the first one deleted and the second "reworded" into the first, which is
     a diff that lies. Too little in common, and they are a bullet removed and
     a bullet added, shown as that. */
  const words=s=>(String(s).match(/[\p{L}\p{N}]+/gu)||[]).length;
  const sim=(a,b)=>{ let same=0; wdiff(a,b).forEach(([op,tx])=>{ if(op==="eq") same+=words(tx) });
    return same/Math.max(1,words(a),words(b)) };
  const gone=tx=>'<li class="del"><del class="rvd">'+rvText(tx)+'</del></li>';
  const came=tx=>'<li class="ins"><ins class="rvi">'+rvText(tx)+'</ins></li>';
  const li=[];
  for(let k=0;k<ops.length;){
    if(ops[k][0]==="eq"){ li.push('<li class="same">'+rvText(ops[k][1])+'</li>'); k++; continue }
    const D=[], I=[];
    while(k<ops.length&&ops[k][0]!=="eq"){ (ops[k][0]==="del"?D:I).push(ops[k][1]); k++ }
    const cand=[];
    D.forEach((a,i)=>I.forEach((b,j)=>{ const v=sim(a,b); if(v>=0.3) cand.push([v,i,j]) }));
    cand.sort((p,q)=>q[0]-p[0]||p[1]-q[1]);
    const pairOf=new Map(), taken=new Set();
    for(const [,i,j] of cand) if(!pairOf.has(i)&&!taken.has(j)){ pairOf.set(i,j); taken.add(j) }
    const from=new Map([...pairOf].map(([i,j])=>[j,i]));
    D.forEach((a,i)=>{ if(!pairOf.has(i)) li.push(gone(a)) });
    I.forEach((b,j)=>li.push(from.has(j)?'<li class="chg">'+diffHTML(D[from.get(j)],b)+'</li>':came(b)));
  }
  return '<ul>'+li.join("")+'</ul>';
}
/* One change to a CV, written out: the sheet and the Changes tab draw the
   same thing. */
function cvUnitHTML(u,compact){
  if(u.kind==="entry") return rvEntryHTML(u.before,u.after,u.tag,compact);
  if(u.kind==="section") return u.before==null?'<span class="rvs">'+esc(t("{n} entry(s) added",{n:(u.after||[]).length}))+'</span>'
    :u.after==null?'<span class="rvs">'+esc(t("{n} entry(s) removed",{n:(u.before||[]).length}))+'</span>':rvListHTML(u.before||[],u.after||[]);
  if(u.kind==="text") return '<span class="rvs">'+esc(t("The file no longer reads as a CV, so it is shown whole."))+'</span>';
  if(Array.isArray(u.before)||Array.isArray(u.after)) return rvListHTML(u.before||[],u.after||[]);
  /* The design is a tree of settings, most of them untouched: only the ones
     that moved, each where it sits, rather than the whole tree as one line. */
  const obj=v=>v&&typeof v==="object"&&!Array.isArray(v);
  if(u.kind==="block"&&obj(u.before)&&obj(u.after)){
    const rows=[];
    const walk=(a,b,at)=>{
      for(const k of new Set([...Object.keys(a||{}),...Object.keys(b||{})])){
        const x=(a||{})[k], y=(b||{})[k];
        if(JSON.stringify(x)===JSON.stringify(y)) continue;
        if(obj(x)&&obj(y)) walk(x,y,at.concat(k));
        else rows.push('<dt>'+esc(at.concat(k).join(" › ").replace(/_/g," "))+'</dt><dd>'+diffHTML(rvVal(x),rvVal(y))+'</dd>');
      }
    };
    walk(u.before,u.after,[]);
    if(rows.length) return '<dl>'+rows.join("")+'</dl>';
  }
  return diffHTML(rvVal(u.before),rvVal(u.after));
}
function cvRvSheet(){
  const r=S.doc&&S.doc.review; if(!r) return;
  const body=r.units.map((u,i)=>{
    const x=cvUnitHTML(u);
    const show=u.kind==="entry"&&u.tag!=="del"||u.kind==="field";
    return rvCard(u,i,x,show?'<button class="obtn" data-go="'+esc(u.id)+'">'+esc(t("Show on the page"))+'</button>':'');
  }).join("");
  openSheet('<div class="rvsheet"><div class="rvhead">'+rvIcon(r.by)+'<div><h3 id="sheet-title">'+
      esc(t("{who}’s changes",{who:whoLabel(r)}))+'</h3><p>'+
      esc(t("Since {t} · {n} write(s)",{t:new Date(r.since*1000).toLocaleTimeString(uiLocale(),{hour:"2-digit",minute:"2-digit",hourCycle:clock12()?"h12":"h23"}),n:r.writes||1}))+
      ' · '+esc(t("Struck through is what it took out, underlined is what it put in."))+'</p></div></div>'+
    '<div class="rvlist">'+body+'</div>'+
    '<div class="foot"><button class="obtn" data-all="undo">'+esc(t("Undo all"))+'</button>'+
      '<button class="obtn" data-all="keep">'+esc(t("Keep all"))+'</button><div class="grow"></div>'+
      '<button class="sbtn primary" data-cancel>'+esc(t("Close"))+'</button></div></div>',
    ()=>$("#sheet").classList.remove("wide"));
  const sh=$("#sheet"); sh.classList.add("wide");
  $("#sheet [data-cancel]").onclick=closeSheet;
  sh.querySelector(".rvsheet").onclick=async e=>{
    const b=e.target.closest("[data-act]"); if(b) return cvRvDo([b.dataset.id],b.dataset.act);
    const a=e.target.closest("[data-all]");
    if(a){ if(a.dataset.all==="undo"&&!await askConfirm({title:t("Undo all of {who}’s changes?",{who:whoLabel(r)}),body:t("Everything it changed that you have not kept goes back to how it was."),ok:t("Undo all"),danger:true})) return;
      return cvRvDo(["*"],a.dataset.all) }
    const g=e.target.closest("[data-go]");
    if(g){ const u=r.units.find(x=>x.id===g.dataset.go); if(!u) return;
      if(u.kind==="entry"&&u.i!=null) select({kind:"entry",name:u.section,i:u.i}); else select({kind:"header"});
      closeSheet() }
  };
}
/* Badges on every list that names documents, so changes waiting are seen
   from wherever you are, not only once the document is open. */
async function refreshDocBadges(){
  try{ const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
    if(S.view==="docs") drawDocuments() }catch(e){}
}
/* Open a document straight into its review. */
async function openForReview(path){
  if(isLetterPath(path)){ await openLetter(path); if(ltRv()) ltRvEnter() }
  else{ await openDoc(path); const r=S.doc&&S.doc.review;
    if(r) r.base&&hasBase()?showTab("diff"):cvRvSheet() }
}
const docByPath=p=>((S.state&&S.state.documents)||[]).find(d=>d.path===p)||null;
const rvPill=d=>d&&d.review?'<span class="rvpill" title="'+esc(t("{who} changed this. Not reviewed yet.",{who:whoLabel(d.review)}))+'">'+
  esc(t("Review"))+'</span>':"";

/* =========================================================================
   Changes: what a tailored CV changed from its base
   ========================================================================= */
/* The question a tailored CV has to answer before it is sent: what exactly is
   different from the CV it was copied from. The page cannot say -- it is the
   new version, finished -- and "13 fields differ" says how much, not what.
   So this tab writes every difference out beside the page: an entry field by
   field, a reworded bullet as the words taken out and the words put in, an
   entry added or removed whole, each numbered, with the same number on the
   block it changed on the page. What is not listed is said to be the base's
   word for word, and is listed too, so the account is the whole CV rather
   than a highlight reel.

   Inline rather than two pages side by side: beside each other, the pages
   leave the comparing to your eyes, and a word changed in one bullet of
   forty lines is exactly what eyes miss.

   Against the base as it is now, the same comparison the marks make. A change
   an AI client made that you have not reviewed carries Keep and Undo; any
   other difference can be put back as the base has it. */
const DF={data:null,path:null,seq:0};
function hasBase(){ return !!(S.prov&&S.prov.base&&!S.prov.base.missing) }
function dfOn(){ return !!DF.data&&DF.path===S.path }
/* The same change, whichever comparison found it: a review compares with
   what the CV said before the model wrote, this with the base, and while
   those agree on a change it is one change with one pair of buttons. */
const dfKey=u=>[u.kind,u.kind==="entry"?u.section+"|"+u.tag:u.id,
  JSON.stringify(u.before??null),JSON.stringify(u.after??null)].join("\u0001");
function dfPending(){
  const r=S.doc&&S.doc.review, m=new Map();
  if(r&&r.units) r.units.forEach(u=>m.set(dfKey(u),u));
  return m;
}
/* Whether every change waiting on review is one of the differences shown
   here, so the review can happen beside the page instead of in a sheet. */
function dfShowsAll(r){
  if(!hasBase()||!dfOn()||!r||!r.units||r.units.some(u=>u.kind==="created")) return false;
  const here=new Set(DF.data.units.map(dfKey));
  return r.units.every(u=>here.has(dfKey(u)));
}
/* The blocks of the page a change shows on. A removed entry is on no block:
   it is not on the page any more, which is the point of it. */
function dfBlocks(u){
  if(/^cv:/.test(u.id)) return [{k:"header",name:"",i:""}];
  if(u.kind==="entry"&&u.i!=null&&u.tag!=="del") return [{k:"entry",name:u.section,i:u.i}];
  if(u.kind==="section"&&Array.isArray(u.after)) return u.after.map((_,i)=>({k:"entry",name:u.section,i}));
  return [];
}
const dfBlockId=b=>b.k+"/"+(b.name??"")+"/"+(b.i??"");

function restTab(){ const p=prefs().tab; return p==="form"||p==="yaml"?p:"page" }
function paintDiffTab(){
  const b=$("#tab-diff"), on=hasBase();
  b.hidden=!on;
  const n=on&&dfOn()?DF.data.units.length:0;
  $("#tab-diff-n").textContent=n?String(n):"";
  b.title=on?t("What this CV changed from {base}, beside the page",{base:baseName()}):"";
  if(!on&&S.tab==="diff") showTab(restTab());
}
async function loadDiff(){
  const path=S.path, seq=++DF.seq;
  if(!path||!hasBase()){ DF.data=null; DF.path=null; paintDiffTab(); return }
  let d;
  try{ d=await api("/api/basediff?path="+encodeURIComponent(path)) }catch(e){ return }
  if(seq!==DF.seq||path!==S.path) return;
  DF.data=d; DF.path=path;
  paintDiffTab(); cvRvBar();
  if(S.tab==="diff"){ paintDiff(); paintHits() }
}

/* Every block the comparison found nothing in, by the names the outline
   uses: what you are told is the same, rather than left to assume it. */
function dfSame(units){
  const cv=(S.data&&S.data.cv)||{}, touched=new Set(), out=[];
  units.forEach(u=>{
    if(u.kind==="entry"&&u.i!=null&&u.tag!=="del") touched.add(u.section+"/"+u.i);
    if(u.kind==="section") touched.add(u.section+"/*");
  });
  if(!units.some(u=>/^cv:/.test(u.id))) out.push({label:t("Header"),sel:{kind:"header"}});
  for(const name of Object.keys(cv.sections||{})){
    if(touched.has(name+"/*")) continue;
    (cv.sections[name]||[]).forEach((it,i)=>{
      if(!touched.has(name+"/"+i))
        out.push({label:sectionLabel(name)+" › "+entryTitle(it,i),sel:{kind:"entry",name,i}});
    });
  }
  return out;
}

function paintDiff(){
  const pane=$("#pane-diff");
  if(!dfOn()){ pane.innerHTML='<p class="muted">'+esc(t("Loading…"))+'</p>'; return }
  const d=DF.data, units=d.units||[], base=baseName(), wait=dfPending(), r=S.doc&&S.doc.review;
  const same=dfSame(units);
  const card=(u,n)=>{
    const mine=wait.get(dfKey(u));
    const acts=mine
      ?'<span class="dfpend">'+rvIcon(r.by)+esc(t("{who}’s, not reviewed yet",{who:whoLabel(r)}))+'</span>'+
       '<div class="grow"></div>'+
       '<button class="obtn" data-df="undo" data-id="'+esc(mine.id)+'" title="'+
         esc(t("Put back what {base} says",{base}))+'">'+esc(t("Undo"))+'</button>'+
       '<button class="pbtn rvkeep" data-df="keep" data-id="'+esc(mine.id)+'">'+esc(t("Keep"))+'</button>'
      :'<div class="grow"></div>'+
       '<button class="obtn" data-df="revert" data-id="'+esc(u.id)+'" title="'+
         esc(t("Put back what {base} says",{base}))+'">'+esc(t("Undo"))+'</button>';
    return '<div class="rvc dfc" data-u="'+esc(u.id)+'" data-tag="'+esc(u.tag||"chg")+'"'+
        (dfBlocks(u).length||/^cv:/.test(u.id)?' data-go="1"':'')+'>'+
      '<div class="rvh"><span class="rvn">'+n+'</span><b>'+esc(t(u.label))+'</b>'+
        /* "Header" and "Document" are the app's words; the rest is the CV's. */
        (u.where?'<span class="rvw" data-noi18n>'+esc(/^(Header|Document)$/.test(u.where)?t(u.where):u.where)+'</span>':'')+'</div>'+
      '<div class="rvx" data-noi18n>'+cvUnitHTML(u,true)+'</div>'+
      '<div class="rva">'+acts+'</div></div>';
  };
  pane.innerHTML='<div class="df">'+
    '<div class="dfhead"><h4>'+esc(units.length
        ?t("{n} change(s) from {base}",{n:units.length,base})
        :t("The same as {base}, word for word",{base}))+'</h4>'+
      '<p class="muted2">'+(units.length
        ?'<del class="rvd">'+esc(t("Struck through"))+'</del> '+esc(t("is what {base} says and this CV does not;",{base}))+' '+
         '<ins class="rvi">'+esc(t("underlined"))+'</ins> '+esc(t("is what this CV says instead. Everything not listed here is word for word the same."))
        :esc(t("Nothing has been tailored yet.")))+'</p>'+
      (S.dirty?'<p class="dfwarn">'+esc(t("Your unsaved edits are not in this yet. Save (Ctrl S) to see them here."))+'</p>':'')+
    '</div>'+
    (units.length?'<div class="rvlist">'+units.map((u,i)=>card(u,i+1)).join("")+'</div>':'')+
    (same.length&&units.length?'<details class="dfsame"><summary>'+
        esc(t("{n} part(s) the same as {base}",{n:same.length,base}))+'</summary><ul>'+
      same.map((x,i)=>'<li><button class="linkbtn" data-same="'+i+'" data-noi18n>'+esc(x.label)+'</button></li>').join("")+
      '</ul></details>':'')+
  '</div>';
  pane.onclick=async e=>{
    const b=e.target.closest("[data-df]");
    if(b) return dfAct(b.dataset.df,b.dataset.id);
    const s=e.target.closest("[data-same]");
    if(s){ select(same[+s.dataset.same].sel); return }
    const c=e.target.closest(".dfc[data-go]");
    if(c&&!e.target.closest("button,a,summary")) dfGo(c.dataset.u);
  };
  pane.onmouseover=e=>{ const c=e.target.closest(".dfc"); dfHot(c&&c.dataset.u) };
  pane.onmouseleave=()=>dfHot(null);
}
/* A card selects its block, which brings the page to it; a block on the
   page brings its card into view. Either way the two light up together. */
function dfGo(id){
  const u=dfOn()&&DF.data.units.find(x=>x.id===id); if(!u) return;
  const b=dfBlocks(u)[0];
  if(!b) return;
  select(b.k==="header"?{kind:"header"}:{kind:"entry",name:b.name,i:+b.i});
  dfHot(id);
}
function dfHot(id){
  $$("#pane-page .hit.hot,#pane-page .pgchg.hot,#pane-diff .dfc.hot").forEach(x=>x.classList.remove("hot"));
  if(!id) return;
  $$('#pane-diff .dfc[data-u="'+CSS.escape(id)+'"]').forEach(x=>x.classList.add("hot"));
  $$('#pane-page [data-us~="'+CSS.escape(id)+'"]').forEach(x=>x.classList.add("hot"));
}
function dfCard(ids){
  const id=(ids||"").split(" ")[0]; if(!id) return;
  const c=$('#pane-diff .dfc[data-u="'+CSS.escape(id)+'"]'); if(!c) return;
  c.scrollIntoView({block:"nearest",behavior:"smooth"});
  c.classList.remove("flash"); void c.offsetWidth; c.classList.add("flash");
}
async function dfAct(kind,id){
  if(S.dirty){ toast(t("Save your edits first (Ctrl S), then decide on the changes."),true); return }
  if(kind==="keep"||kind==="undo") return cvRvDo([id],kind);
  try{ await post("/api/basediff/revert",{path:S.path,ids:[id],sig:DF.data&&DF.data.sig}) }
  catch(e){ toast(e.message,true); return loadDiff() }
  toast(t("Put back as {base} has it",{base:baseName()}));
  await reopenInPlace();
  refreshDocBadges();
}
/* On the page: the changed blocks tinted, a number beside each matching its
   card. Drawn over the PNG like the other marks, so nothing reaches the PDF. */
function paintDiffMarks(wrap,leftPct,pageH){
  if(!dfOn()) return;
  const by=new Map();
  DF.data.units.forEach((u,i)=>dfBlocks(u).forEach(b=>{
    const id=dfBlockId(b);
    if(!by.has(id)) by.set(id,{n:[],ids:[],tag:u.tag||"chg"});
    const m=by.get(id); m.n.push(i+1); m.ids.push(u.id);
    if(m.tag!==(u.tag||"chg")) m.tag="chg";
  }));
  wrap.querySelectorAll(".hit").forEach(el=>{
    const m=by.get(el.dataset.k+"/"+el.dataset.name+"/"+el.dataset.i);
    if(!m) return;
    el.classList.add("dfchg"); el.dataset.dftag=m.tag; el.dataset.us=m.ids.join(" ");
  });
  const seen=new Set();
  wrap.insertAdjacentHTML("beforeend",bandsOn(S.page).map(b=>{
    const id=dfBlockId({k:b.k,name:b.name,i:b.i}), m=by.get(id);
    if(!m||seen.has(id)) return "";
    seen.add(id);
    const top=(Math.max(14,Math.min(pageH,b.y0))/pageH)*100;
    const n=m.n.length>3?m.n[0]+"–"+m.n[m.n.length-1]:m.n.join(",");
    return '<span class="pgchg" aria-hidden="true" data-tag="'+esc(m.tag)+'" data-us="'+esc(m.ids.join(" "))+
      '" style="top:'+top.toFixed(3)+'%;left:'+leftPct.toFixed(3)+'%">'+esc(n)+'</span>';
  }).join(""));
}

