/* Export both, interview prep workspace, people/email drafts, and boot trigger. */
/* ---- Export both: the CV and the letter for one application ------------- */
document.addEventListener("click",e=>{ const b=e.target.closest("#ap-pack"); if(b) packSheet(b.dataset.job) });
document.addEventListener("click",e=>{ const b=e.target.closest("#ap-versions"); if(b) historySheet({job:b.dataset.job},b.dataset.name) });
async function packSheet(id){
  const j=(S.jobs||[]).find(x=>x.id===id); if(!j) return;
  let info;
  try{ info=await api("/api/pack/info?job="+encodeURIComponent(id)) }catch(e){ return toast(e.message,true) }
  const both=info.cv&&info.letter, draft=j.status==="pending";
  /* What would go out wrong, said before it goes: a letter still holding its
     writing prompts, a CV that does not render, changes nobody has read. */
  const warn=[];
  if(j.letter_path){
    try{ const d=await api("/api/doc?path="+encodeURIComponent(j.letter_path));
      if((d.prompts||[]).length)
        warn.push(t("The letter still has its writing prompts in it."));
    }catch(e){}
  }
  const th=j.cv_path&&S.docThumbs[j.cv_path];
  if(th&&th.failed) warn.push(t("The CV does not render at the moment, so it cannot be exported."));
  for(const pth of [j.cv_path,j.letter_path]){ const d=pth&&docByPath(pth);
    if(d&&d.review) warn.push(t("{who} changed the {what} and you have not reviewed it.",{who:whoLabel(d.review),what:isLetterPath(pth)?t("letter"):t("CV")})) }
  openSheet('<h3>'+esc(t("Export for {co}",{co:j.company}))+'</h3>'+
    '<p>'+esc(both?t("The CV and the letter for this application, ready to upload or attach."):
      t("This application's document, ready to upload or attach."))+'</p>'+
    (warn.length?'<div class="pk-warn" role="alert"><b>'+esc(t("Before you send it"))+'</b><ul>'+warn.map(w=>'<li>'+esc(w)+'</li>').join("")+'</ul></div>':'')+
    '<div class="pk-opts" role="radiogroup" aria-label="'+esc(t("Format"))+'">'+
      '<label class="pk-opt"><span class="pk-h"><input type="radio" name="pk-f" value="pdf" checked><b>'+
        esc(both?t("One PDF"):t("PDF"))+'</b></span><small>'+
        esc(both?t("The CV, then the letter. For forms that take a single file."):t("Ready to attach."))+'</small></label>'+
      '<label class="pk-opt"><span class="pk-h"><input type="radio" name="pk-f" value="zip"><b>'+esc(t("A zip"))+'</b></span><small>'+
        esc(t("Separate files, for forms with one field each, or for your records."))+'</small></label>'+
    '</div>'+
    '<label class="pk-field">'+esc(t("File name"))+'<input id="pk-name" spellcheck="false" value="'+esc(info.name)+'"></label>'+
    (info.posting?'<label class="pk-check" id="pk-post-row" hidden><input type="checkbox" id="pk-post" checked>'+
      esc(t("Add the posting, as Markdown"))+'</label>':'')+
    /* Off until asked: an export to proofread is not an application sent. */
    (draft?'<label class="pk-check"><input type="checkbox" id="pk-applied">'+
      esc(t("I am sending it now: mark the application as sent today"))+'</label>':'')+
    '<p class="pk-note">'+esc(t("Rendered from what is saved now: unsaved edits are left out."))+'</p>'+
    '<div class="foot"><button class="sbtn" id="pk-cancel">'+esc(t("Cancel"))+'</button>'+
      '<button class="sbtn primary" id="pk-go">'+esc(t("Save PDF…"))+'</button></div>');
  const fmt=()=>($("#sheet [name=pk-f]:checked")||{}).value||"pdf";
  $$("#sheet [name=pk-f]").forEach(r=>r.onchange=()=>{
    const z=fmt()==="zip";
    $("#pk-go").textContent=t(z?"Save zip…":"Save PDF…");
    const pr=$("#pk-post-row"); if(pr) pr.hidden=!z;
    $$("#sheet .pk-opt").forEach(o=>o.classList.toggle("on",o.contains(r)&&r.checked||o.querySelector("input").checked));
  });
  $$("#sheet .pk-opt")[0].classList.add("on");
  $("#pk-cancel").onclick=closeSheet;
  $("#pk-go").onclick=()=>{
    const q="job="+encodeURIComponent(id)+"&format="+fmt()+"&name="+encodeURIComponent($("#pk-name").value)+
      "&posting="+($("#pk-post")&&$("#pk-post").checked?1:0);
    const mark=$("#pk-applied")&&$("#pk-applied").checked;
    download("/api/pack?"+q+tok());
    closeSheet();
    if(mark) statusWithUndo(j,"applied");
  };
}


/* ---- Interview prep ------------------------------------------------------
   On the application while a round is booked: how ready you are, what the
   posting asks for next to what your CV proves, and the likely questions to
   rehearse one at a time, out loud. Made here from the posting, the CV you
   sent and the kind of round; an AI client can write better ones, and your
   notes stay. */
const PREP={job:null,data:null,tick:null};
/* The round to prepare for: the first with no outcome. A round with no date
   yet is still one to prepare for -- the questions do not wait on the
   invitation -- so it gets the card, with "not scheduled" where the
   countdown goes. One whose date has passed is waiting for its outcome in
   Interviews instead. */
const prepRound=j=>{
  if(DEAD_ST.has(j.status)) return null;
  const rs=j.rounds||[], i=rs.findIndex(r=>!r.outcome);
  if(i<0) return null;
  if(!rs[i].at) return {r:rs[i],n:i+1,m:rs.length,at:null};
  const at=interviewMoment({interview_at:rs[i].at+":00",interview_tz:rs[i].tz||null});
  return at&&at>new Date()?{r:rs[i],n:i+1,m:rs.length,at}:null;
};
function prepParts(d,j){
  const qs=d.questions||[], got=qs.filter(q=>q.state==="got").length;
  const st=d.stories||[], proved=st.filter(s=>s.proof).length;
  const keep=(d.asks||[]).filter(a=>a.keep).length;
  const parts=[
    {k:"q",label:t("Questions rehearsed"),val:t("{n} of {m}",{n:got,m:qs.length}),f:qs.length?got/qs.length:0},
    {k:"s",label:t("Stories with proof"),val:t("{n} of {m}",{n:proved,m:st.length}),f:st.length?proved/st.length:0,none:!st.length},
    {k:"a",label:t("Questions to ask"),val:t("{n} ready",{n:keep}),f:Math.min(1,keep/2),none:!!d.local},
    {k:"c",label:t("The CV you sent"),val:j.cv_path?(d.cv_read?t("re-read"):""):t("not linked"),f:d.cv_read?1:0,none:!j.cv_path,
      act:j.cv_path&&!d.cv_read?t("Re-read →"):""},
  ];
  /* Readiness counts what there is to be ready with: no posting means no
     stories to prove, no CV linked means nothing to re-read, and neither is
     half of being ready. */
  const counted=parts.filter(p=>!p.none);
  return {parts,pct:Math.round(counted.reduce((a,p)=>a+p.f,0)/Math.max(1,counted.length)*100)};
}
/* The card grows above the interviews, so whatever the user is working on
   below it would be pushed down out of view as it appears. Keep the open
   round -- else the focused field -- where it was on screen. */
function keepPlace(fn){
  const a=$(".rd.open")||(document.activeElement&&document.activeElement.closest&&document.activeElement.closest("#jinsp-body .block"));
  const y0=a&&a.isConnected?a.getBoundingClientRect().top:null;
  fn();
  if(y0==null||!a.isConnected) return;
  let sc=a.parentElement; while(sc&&sc!==document.body&&!(sc.scrollHeight>sc.clientHeight&&/(auto|scroll)/.test(getComputedStyle(sc).overflowY))) sc=sc.parentElement;
  if(sc&&sc!==document.body) sc.scrollTop+=a.getBoundingClientRect().top-y0;
}
async function drawPrep(j){
  const host=$("#ap-prep"); if(!host) return;
  clearInterval(PREP.tick);
  const nr=prepRound(j);
  if(!nr){ host.innerHTML=""; host.hidden=true; return }
  keepPlace(()=>{ host.hidden=false });
  if(PREP.job!==j.id||!PREP.data){
    PREP.job=j.id; PREP.data=null;
    keepPlace(()=>{ host.innerHTML='<section class="pp-stage pp-wait"><span class="spin"></span></section>' });
    try{ PREP.data=await api("/api/jobs/prep?id="+encodeURIComponent(j.id)) }
    catch(e){ host.innerHTML=""; host.hidden=true; return }
    if(S.jsel!==j.id) return;
  }
  const d=PREP.data, r=nr.r, {parts,pct}=prepParts(d,j);
  const who=(j.people||[]).find(p=>p.name&&p.name===r.with);
  const two=nr.at&&r.tz&&r.tz!==userTz();
  const when=nr.at?fmtKey(dayIn(nr.at),{weekday:"long"})+", "+hmShow(nr.at,userTz()):t("Not scheduled yet");
  const sub=[two?t("{t} in {city}",{t:hmShow(nr.at,r.tz),city:tzCity(r.tz)}):"",
    who&&who.role?t(who.role):""].filter(Boolean).join(" · ");
  const ring=415, off=Math.round(ring*(1-pct/100));
  const cd=()=>{ if(!nr.at) return '<button type="button" class="pp-go pp-date" id="pp-date">'+esc(t("Set the date"))+'</button>';
    const left=Math.max(0,nr.at-new Date()), D=Math.floor(left/864e5), H=Math.floor(left/36e5)%24, M=Math.floor(left/6e4)%60;
    return [[D,t("days")],[String(H).padStart(2,"0"),t("hours")],[String(M).padStart(2,"0"),t("min")]]
      .map(([n,u])=>'<span><b>'+n+'</b><small>'+esc(u)+'</small></span>').join("") };
  const st=d.stories||[], qs=d.questions||[], proved=st.filter(s=>s.proof).length;
  const by=d.by&&!d.local?(aiClient(d.by)||{}).label||t("your AI client"):"";
  const aiOn=(S.ai||[]).some(c=>c.state==="connected");
  const deck=qs.slice(0,3).reverse();
  const card='<section class="pp-stage" aria-labelledby="pp-h">'+
    '<div class="pp-top">'+
      '<div class="pp-when"><span class="pp-kick"><i></i>'+esc(t("Round {n} of {m}",{n:nr.n,m:nr.m})+(r.kind?" · "+t(r.kind):""))+'</span>'+
        '<h2 id="pp-h">'+esc(r.with?(nr.at?t("{when} with {who}",{when,who:r.with}):t("With {who}, not scheduled yet",{who:r.with})):when)+'</h2>'+
        (sub?'<span class="pp-sub">'+esc(sub)+'</span>':'')+
        '<span class="pp-cd" id="pp-cd">'+cd()+'</span></div>'+
      '<div class="pp-ready"><div class="pp-ring" role="img" aria-label="'+esc(t("{n}% ready",{n:pct}))+'">'+
        '<svg viewBox="0 0 168 168" aria-hidden="true"><defs><linearGradient id="pp-rg" x1="0" y1="0" x2="1" y2="1">'+
          '<stop offset="0" stop-color="var(--pp-ring-a)"/><stop offset="1" stop-color="var(--pp-ring-b)"/></linearGradient></defs>'+
          '<circle cx="84" cy="84" r="66" class="trk"/><circle cx="84" cy="84" r="66" class="val" style="--off:'+off+'"/></svg>'+
        '<span class="pp-pct"><b>'+pct+'<i>%</i></b><small>'+esc(t("ready"))+'</small></span></div>'+
        '<div class="pp-parts">'+parts.map(p=>'<div class="pp-part"><span><span>'+esc(p.label)+'</span><b>'+esc(p.val)+
          (p.act?' <button type="button" class="linkbtn" data-pp-cv>'+esc(p.act)+'</button>':'')+'</b></span>'+
          '<i><i class="'+(p.f>=1?"done":"")+'" style="width:'+Math.max(3,Math.round(p.f*100))+'%"></i></i></div>').join("")+'</div></div>'+
    '</div>'+
    (st.length?'<div class="pp-asks"><div class="pp-h3"><h3>'+esc(t("What they ask for"))+'</h3><span>'+
        esc(t("{n} of {m} backed by a line of your CV",{n:proved,m:st.length}))+'</span></div>'+
      st.map((s,i)=>'<div class="pp-row'+(s.proof?"":" gap")+'">'+
        (s.proof?'<i class="ok" aria-hidden="true">✓</i>':'<i class="dot" aria-hidden="true"></i>')+
        '<span class="rq" data-noi18n>'+esc(s.req)+'</span>'+
        (s.proof?'<span class="pf" data-noi18n>'+esc(s.proof)+(s.where?' <em>· '+esc(s.where)+'</em>':'')+'</span>'
          :'<span class="pf miss"><b>'+esc(t("Nothing in your CV proves it yet"))+'</b> <button type="button" class="linkbtn" data-pp-story="'+i+'">'+
            esc(t("Write the story →"))+'</button></span>')+'</div>').join("")+'</div>':'')+
    '<div class="pp-deck"><div class="cards" aria-hidden="true">'+deck.map((q,i)=>'<div class="pp-card" data-i="'+(deck.length-1-i)+'">'+
        '<span class="pp-src" data-src="'+esc(q.src)+'">'+esc(prepSrc(q.src,r))+'</span><b data-noi18n>'+esc(q.q)+'</b>'+
        (i===deck.length-1?'<small>'+esc(t("Question {n} of {m}",{n:1,m:qs.length}))+'</small>':'')+'</div>').join("")+'</div>'+
      '<div class="cta"><span class="kick">'+esc(t("Rehearse"))+'</span>'+
        '<b>'+esc(t("{n} questions they are likely to ask, one at a time, out loud.",{n:qs.length}))+'</b>'+
        '<span class="lede">'+esc(t("Two minutes each. The ones you mark “needs work” come back first next time."))+'</span>'+
        '<span class="acts"><button type="button" class="pp-go" id="pp-go">'+esc(t("Start rehearsing"))+'</button>'+
          '<button type="button" class="linkbtn" id="pp-all">'+esc(t("See all questions"))+'</button></span></div></div>'+
    /* An AI client does this properly: say so first, with the words to use. */
    (d.local&&aiOn?'<div class="pp-ask"><span>'+esc(t("These come from the posting's words alone. Your AI client can write the real ones, from the posting, the CV you sent and the company."))+
      '</span><code data-noi18n>'+esc(t("prepare my {co} interview",{co:j.company}))+'</code><button type="button" class="obtn" id="pp-copy">'+esc(t("Copy"))+'</button></div>':'')+
    '<div class="pp-foot">'+(by?esc(t("Prepared by {who} from the posting and your CV · {d}",{who:by,d:d.at?fmtKey(d.at,{day:"numeric",month:"short"}):""}))
        :esc(j.description&&j.cv_path?t("Made on this computer from the posting, the CV you sent and the kind of round.")
          :j.description?t("Made on this computer from the posting and the kind of round. Link the CV you sent for questions about it.")
          :j.cv_path?t("Made on this computer from the CV you sent and the kind of round. Save the posting for questions about the role.")
          :t("General questions for this kind of round. Save the posting and link your CV for questions about this role."))+
          (aiOn?''
            :' <button type="button" class="linkbtn" id="pp-ai">'+esc(t("Connect an AI client for better questions"))+'</button>'))+
      '<span class="grow"></span><button type="button" class="linkbtn" id="pp-print">'+esc(t("Print a sheet"))+'</button>'+
      '<button type="button" class="linkbtn" id="pp-askl">'+
        esc(r.with?t("Questions to ask {who}",{who:r.with.split(" ")[0]}):t("Questions to ask them"))+'</button></div>'+
  '</section>';
  keepPlace(()=>{ host.innerHTML=card });
  /* The round you were just editing, back in view after the redraw. */
  if(S.roundSaved&&Date.now()-S.roundSaved.at<4000){
    const rd=$('.rd[data-rd="'+CSS.escape(S.roundSaved.id)+'"]');
    if(rd){ const b=rd.getBoundingClientRect(); if(b.top<60||b.bottom>innerHeight-20) rd.scrollIntoView({block:"center"}) }
  }
  const rr=document.querySelector('[data-rd-ready="'+CSS.escape(r.id)+'"]');
  if(rr){ rr.hidden=false; rr.textContent=t("Prep: {n}% ready",{n:pct}) }
  if(nr.at) PREP.tick=setInterval(()=>{ const el=$("#pp-cd"); if(el) el.innerHTML=cd(); else clearInterval(PREP.tick) },30000);
  /* Undated: the date goes in the round's own editor, just below. */
  const pd=$("#pp-date");
  if(pd) pd.onclick=()=>{ const f=$('.rd-ed input[data-rf="day"]');
    if(f){ f.scrollIntoView({block:"center",behavior:"smooth"}); f.focus({preventScroll:true}) } };
  /* The card can land after you have moved on; nothing to wire then. */
  const on=(id,f)=>{ const e=$(id); if(e) e.onclick=f };
  on("#pp-go",()=>rehearse(j));
  on("#pp-all",()=>prepSheet(j,"q"));
  on("#pp-askl",()=>prepSheet(j,"a"));
  on("#pp-print",()=>prepPrint(j));
  const ai=$("#pp-ai"); if(ai) ai.onclick=()=>openSettings("ai");
  const cp=$("#pp-copy"); if(cp) cp.onclick=()=>{ navigator.clipboard.writeText(t("prepare my {co} interview",{co:j.company})).then(()=>toast(t("Copied"))) };
  $$("#ap-prep [data-pp-cv]").forEach(b=>b.onclick=()=>{ prepSave(j,{...PREP.data,cv_read:true}); openDoc(j.cv_path) });
  $$("#ap-prep [data-pp-story]").forEach(b=>b.onclick=()=>{
    const s=st[+b.dataset.ppStory], q={id:Math.random().toString(36).slice(2,10),q:t("Tell us about a time you showed this: “{req}”.",{req:s.req}),
      src:"posting",why:s.req,cv:"",note:"",state:"work"};
    prepSave(j,{...PREP.data,questions:[q,...(PREP.data.questions||[])]}).then(()=>rehearse(j,q.id));
  });
}
const prepSrc=(src,r)=>src==="posting"?t("From the posting"):src==="cv"?t("From your CV"):src==="you"?t("Yours"):(r&&r.kind?t(r.kind):t("This round"));
async function prepSave(j,data){
  PREP.data={...data};
  try{ const r=await post("/api/jobs/prep",{id:j.id,prep:data}); if(PREP.job===j.id) PREP.data=r }
  catch(e){ toast(e.message,true) }
  if(S.jsel===j.id&&!$("#rh")) drawPrep(j);
}
/* One page for the day: when and with whom, the questions with your notes,
   what backs each ask of the posting, and what to ask them. Printed from
   this window, with everything else hidden. */
function prepPrint(j){
  const d=PREP.data, nr=prepRound(j)||{}, r=nr.r||{};
  const el=document.createElement("div"); el.id="pp-print";
  el.innerHTML='<h1>'+esc(j.company)+' · '+esc(j.title)+'</h1>'+
    '<p class="when">'+esc([t("Round {n} of {m}",{n:nr.n||1,m:nr.m||1}),r.kind?t(r.kind):"",r.with?t("with {n}",{n:r.with}):"",
      nr.at?fmtKey(dayIn(nr.at),{weekday:"long",day:"numeric",month:"long"})+", "+hmShow(nr.at,userTz()):""].filter(Boolean).join(" · "))+'</p>'+
    '<h2>'+esc(t("Likely questions"))+'</h2><ol>'+(d.questions||[]).map(q=>'<li><b>'+esc(q.q)+'</b>'+
      (q.note?'<p>'+esc(q.note)+'</p>':'<p class="blank"></p>')+'</li>').join("")+'</ol>'+
    ((d.stories||[]).length?'<h2>'+esc(t("What they ask for"))+'</h2><ul>'+d.stories.map(s=>'<li><b>'+esc(s.req)+'</b> — '+
      (s.proof?esc(s.proof):'<em>'+esc(t("Nothing in your CV proves it yet"))+'</em>')+'</li>').join("")+'</ul>':'')+
    '<h2>'+esc(t("Questions to ask them"))+'</h2><ul>'+(d.asks||[]).filter(a=>a.keep).map(a=>'<li>'+esc(a.q)+'</li>').join("")+'</ul>';
  document.body.append(el); document.body.classList.add("pp-printing");
  const done=()=>{ el.remove(); document.body.classList.remove("pp-printing"); removeEventListener("afterprint",done) };
  addEventListener("afterprint",done);
  window.print();
  setTimeout(()=>{ if(document.body.contains(el)&&!matchMedia("print").matches) done() },1500);
}
/* Every question with its notes, your own added, and the questions to ask. */
function prepSheet(j,focus){
  const d=PREP.data, r=(prepRound(j)||{}).r;
  const qrow=(q,i)=>'<details class="pp-q"'+(i===0&&focus==="q"?' open':'')+'><summary><span class="pp-src" data-src="'+esc(q.src)+'">'+esc(prepSrc(q.src,r))+'</span>'+
    '<span class="tx" data-noi18n>'+esc(q.q)+'</span>'+(q.state==="got"?'<span class="st got">'+esc(t("Got it"))+'</span>':q.state==="work"?'<span class="st work">'+esc(t("Needs work"))+'</span>':'')+'</summary>'+
    (q.why?'<p class="why">'+esc(t("The posting"))+': “'+esc(q.why)+'”</p>':'')+(q.cv?'<p class="why">'+esc(t("Your CV"))+': '+esc(q.cv)+'</p>':'')+
    '<label class="pk-field">'+esc(t("Your answer, in a few lines"))+'<textarea rows="3" data-note="'+esc(q.id)+'" placeholder="'+
      esc(t("What was going on, what you did, the number that shows it worked."))+'">'+esc(q.note||"")+'</textarea></label></details>';
  openSheet('<h3>'+esc(focus==="a"?t("Questions to ask them"):t("Likely questions"))+'</h3>'+
    (focus==="a"?'<div class="pp-asklist">'+(d.asks||[]).map(a=>'<label class="pk-check"><input type="checkbox" data-ask="'+esc(a.id)+'"'+(a.keep?' checked':'')+'>'+esc(a.q)+'</label>').join("")+
        '<div class="pp-add"><input id="pp-newa" placeholder="'+esc(t("A question of your own"))+'"><button class="sbtn" id="pp-adda">'+esc(t("Add"))+'</button></div></div>'
      :'<div class="pp-qlist">'+(d.questions||[]).map(qrow).join("")+
        '<div class="pp-add"><input id="pp-newq" placeholder="'+esc(t("A question you expect"))+'"><button class="sbtn" id="pp-addq">'+esc(t("Add"))+'</button></div></div>')+
    '<div class="foot"><button class="sbtn primary" id="pp-done">'+esc(t("Done"))+'</button></div>');
  const cur=()=>JSON.parse(JSON.stringify(PREP.data));
  $$("#sheet [data-note]").forEach(ta=>ta.onchange=()=>{ const x=cur(); const q=x.questions.find(q=>q.id===ta.dataset.note); if(q){ q.note=ta.value; prepSave(j,x) } });
  $$("#sheet [data-ask]").forEach(cb=>cb.onchange=()=>{ const x=cur(); const a=x.asks.find(a=>a.id===cb.dataset.ask); if(a){ a.keep=cb.checked; prepSave(j,x) } });
  const addq=$("#pp-addq"); if(addq) addq.onclick=()=>{ const v=$("#pp-newq").value.trim(); if(!v) return;
    const x=cur(); x.questions.push({id:Math.random().toString(36).slice(2,10),q:v,src:"you",why:"",cv:"",note:"",state:""}); prepSave(j,x).then(()=>prepSheet(j,"q")) };
  const adda=$("#pp-adda"); if(adda) adda.onclick=()=>{ const v=$("#pp-newa").value.trim(); if(!v) return;
    const x=cur(); x.asks.push({id:Math.random().toString(36).slice(2,10),q:v,keep:true}); prepSave(j,x).then(()=>prepSheet(j,"a")) };
  $("#pp-done").onclick=()=>{ closeSheet(); drawPrep(j) };
}
/* Rehearse: one question at a time, full screen, two minutes each, out loud.
   The ones that need work come first; your notes stay hidden until you have
   answered. */
function rehearse(j,first){
  const d=PREP.data; if(!d||!(d.questions||[]).length) return;
  const nr=prepRound(j)||{}, r=nr.r||{};
  const rank={work:0,"":1,got:2};
  let order=[...d.questions].sort((a,b)=>rank[a.state||""]-rank[b.state||""]);
  if(first) order=[order.find(q=>q.id===first),...order.filter(q=>q.id!==first)].filter(Boolean);
  let i=0, t0=Date.now(), shown=false, timer=null;
  const el=document.createElement("div"); el.id="rh"; el.setAttribute("role","dialog"); el.setAttribute("aria-modal","true");
  el.setAttribute("aria-label",t("Rehearsing"));
  document.body.append(el);
  const close=()=>{ clearInterval(timer); document.removeEventListener("keydown",keys,true); el.remove(); drawPrep(j) };
  const mark=state=>{ const x=JSON.parse(JSON.stringify(PREP.data)); const q=x.questions.find(q=>q.id===order[i].id);
    if(q&&state!=null) q.state=state; prepSave(j,x); order[i]={...order[i],state:state==null?order[i].state:state};
    if(i<order.length-1){ i++; draw() } else done() };
  const done=()=>{ clearInterval(timer); const got=order.filter(q=>q.state==="got").length;
    el.innerHTML='<div class="rh-done"><b>'+esc(t("{n} of {m} you have got",{n:got,m:order.length}))+'</b><span>'+
      esc(t("The ones that need work come back first next time."))+'</span><button type="button" class="pp-go" id="rh-x">'+esc(t("Back to the application"))+'</button></div>';
    $("#rh-x").onclick=close; $("#rh-x").focus() };
  const draw=()=>{
    const q=order[i]; t0=Date.now(); shown=false;
    const segs=order.map((x,k)=>'<i class="'+(k===i?"now":x.state||"")+'"></i>').join("");
    const work=order.filter(x=>x.state==="work").length;
    el.innerHTML='<header class="rh-top">'+companyMark(j)+'<span class="rh-who"><b>'+esc(t("Rehearsing · {co}, round {n}",{co:j.company,n:nr.n||1}))+'</b>'+
        '<small>'+esc([r.kind?t(r.kind):"",r.with?t("with {n}",{n:r.with}):""].filter(Boolean).join(" · "))+'</small></span><span class="grow"></span>'+
        '<span class="rh-prog"><small>'+esc(t("Question {n} of {m}",{n:i+1,m:order.length})+(work?" · "+t("{n} need(s) work",{n:work}):""))+'</small><span>'+segs+'</span></span>'+
        '<button type="button" class="rh-close" id="rh-close">'+esc(t("Done for now"))+' <kbd>Esc</kbd></button></header>'+
      '<main class="rh-main"><div class="rh-col"><div class="rh-card"><span class="rh-src"><span class="pp-src" data-src="'+esc(q.src)+'">'+esc(prepSrc(q.src,r))+'</span></span>'+
          '<b class="rh-q" data-noi18n>'+esc(q.q)+'</b>'+
          /* Where the question comes from: the CV line they will test, or
             the posting's; your line against a posting ask is for the notes. */
          (q.src==="cv"&&q.cv?'<span class="rh-line"><span>'+esc(t("The line"))+'</span>'+esc(q.cv)+'</span>'
            :q.why?'<span class="rh-line"><span>'+esc(t("The posting"))+'</span>'+esc(q.why)+'</span>':'')+'</div>'+
        '<div class="rh-notes"><button type="button" id="rh-nt" aria-expanded="false">'+esc(t("My notes for this one"))+
          ' <small>'+esc(q.note?t("hidden until you have answered"):t("none yet"))+'</small><kbd>N</kbd></button>'+
          '<textarea id="rh-ta" rows="3" hidden placeholder="'+esc(t("What was going on, what you did, the number that shows it worked."))+'">'+esc(q.note||"")+'</textarea></div>'+
        '<div class="rh-acts"><button type="button" class="rh-work" id="rh-w"><kbd>←</kbd>'+esc(t("Needs work"))+'</button>'+
          '<button type="button" class="rh-skip" id="rh-s">'+esc(t("Skip"))+'</button>'+
          '<button type="button" class="rh-got" id="rh-g">'+esc(t("I've got it"))+'<kbd>→</kbd></button></div></div>'+
        '<aside class="rh-side"><div class="rh-timer"><svg viewBox="0 0 200 200" aria-hidden="true"><circle cx="100" cy="100" r="86" class="trk"/>'+
            '<circle cx="100" cy="100" r="86" class="val" id="rh-arc"/></svg><span><b id="rh-t">2:00</b><small>'+esc(t("out loud"))+'</small></span></div>'+
          '<div class="rh-next"><small>'+esc(t("Up next"))+'</small>'+order.slice(i+1,i+4).map(x=>'<span data-noi18n>'+esc(x.q)+'</span>').join("")+'</div>'+
          '<div class="rh-tip"><b>'+esc(t("Say it in this order"))+'</b><span>'+esc(t("What was going on · what you did · the number that shows it worked."))+'</span></div></aside></main>';
    $("#rh-close").onclick=close; $("#rh-w").onclick=()=>mark("work"); $("#rh-g").onclick=()=>mark("got"); $("#rh-s").onclick=()=>mark(null);
    const ta=$("#rh-ta");
    $("#rh-nt").onclick=()=>{ shown=!shown; ta.hidden=!shown; $("#rh-nt").setAttribute("aria-expanded",String(shown)); if(shown) ta.focus() };
    ta.onchange=()=>{ const x=JSON.parse(JSON.stringify(PREP.data)); const qq=x.questions.find(z=>z.id===q.id); if(qq){ qq.note=ta.value; order[i].note=ta.value; prepSave(j,x) } };
    $("#rh-g").focus();
  };
  timer=setInterval(()=>{ const left=Math.max(0,120-(Date.now()-t0)/1000), tt=$("#rh-t"), arc=$("#rh-arc");
    if(tt) tt.textContent=Math.floor(left/60)+":"+String(Math.floor(left%60)).padStart(2,"0");
    if(arc) arc.style.strokeDashoffset=String(540*(1-left/120)); if(arc) arc.classList.toggle("over",left===0) },250);
  const keys=e=>{ if(!$("#rh")) return;
    if(e.target&&e.target.id==="rh-ta"){ if(e.key==="Escape"){ e.preventDefault(); e.target.blur() } return }
    if(e.key==="Escape"){ e.preventDefault(); e.stopPropagation(); close() }
    else if(e.key==="ArrowLeft"&&$("#rh-w")){ e.preventDefault(); mark("work") }
    else if(e.key==="ArrowRight"&&$("#rh-g")){ e.preventDefault(); mark("got") }
    else if((e.key==="n"||e.key==="N")&&$("#rh-nt")){ e.preventDefault(); $("#rh-nt").click() } };
  document.addEventListener("keydown",keys,true);
  draw();
}

/* ---- People on an application, and what to write them --------------------
   A recruiter, a manager, whoever referred you: kept with the application,
   with when you last wrote. The emails are drafted here from what the
   application knows, in its language, and sent from your own mail app. */
const PP_ROLES=["Recruiter","Hiring manager","Interviewer","Referral"];
const ppInitials=n=>String(n||"").split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0].toUpperCase()).join("")||"@";
const ppTint=n=>["#f3dfb8","#dbe6f2","#dcecd9","#f2dcdc","#e6dff2"][[...String(n||"")].reduce((a,c)=>a+c.charCodeAt(0),0)%5];
/* After an interview in the last few days, the email to write is a thank-you. */
function ppKind(j,p){
  /* The person who referred you is kept posted, not sent an offer reply or
     a thank-you for an interview they were not in. */
  if(p&&p.role==="Referral") return "followup";
  /* What there is to say depends on where the application is: an offer is
     answered; a round in the last two days is thanked for (by whoever was
     in it, or anyone when nobody is named); with the next round already
     booked, it is confirmed; with none booked, ask what is next. */
  if(j.status==="offer") return "offer";
  const now=new Date(), rs=(j.rounds||[]).map(r=>({r,at:r.at&&interviewMoment({interview_at:r.at+":00",interview_tz:r.tz||null})}));
  const recent=rs.filter(x=>x.at&&x.at<now&&now-x.at<2*DAY).pop();
  if(recent&&(!recent.r.with||!p||recent.r.with===p.name)) return "thanks";
  if(rs.some(x=>x.at&&x.at>now&&!x.r.outcome)) return "confirm";
  return j.status==="interviewing"&&rs.some(x=>x.at&&x.at<now)?"next":"followup";
}
const PP_KIND_LABEL={confirm:"Confirm the interview",followup:"Follow-up email",thanks:"Thank-you note",next:"Ask about next steps",offer:"Reply to the offer"};
function ppLast(p){
  if(!p.last) return t("No emails yet");
  const d=dayDiff(p.last,todayKey());
  return d<=0?t("Last email today"):t("Last email {d}",{d:fmtKey(p.last,{day:"numeric",month:"short"})});
}
function ppAgo(p){
  const d=p.last?dayDiff(p.last,todayKey()):0;
  return d>0?t("{n} day(s) ago",{n:d}):"";
}
function peopleHTML(j){
  const ps=j.people||[];
  return '<div class="block" id="ap-people"><div class="bhead"><span class="blabel">'+esc(t("People"))+'</span>'+
    '<button class="obtn" data-pp-add="'+esc(j.id)+'">'+esc(t("+ Add someone"))+'</button></div>'+
    (ps.length?'<div class="pp-list">'+ps.map(p=>{
      const kind=ppKind(j,p);
      return '<div class="pp-row"><span class="pp-av" aria-hidden="true" style="background:'+ppTint(p.name||p.email)+'">'+
        esc(ppInitials(p.name||p.email))+'</span>'+
        '<span class="pp-tx"><b><span data-noi18n>'+esc(p.name||p.email)+'</span>'+
          (p.role?' <span class="pp-role">· '+(PP_ROLES.includes(p.role)?esc(t(p.role)):'<span data-noi18n>'+esc(p.role)+'</span>')+'</span>':'')+'</b>'+
        '<small>'+(p.email&&p.name?'<span data-noi18n>'+esc(p.email)+'</span> · ':'')+(ppAgo(p)?'<span title="'+esc(ppAgo(p))+'">'+esc(ppLast(p))+'</span>':esc(ppLast(p)))+'</small></span>'+
        '<span class="pp-acts"><button class="obtn" data-pp-write="'+esc(p.id)+'" data-kind="'+kind+'">'+esc(t(PP_KIND_LABEL[kind]))+'</button>'+
        '<button class="obtn icon" data-pp-edit="'+esc(p.id)+'" aria-label="'+esc(t("Edit {n}",{n:p.name||p.email}))+'">'+
          '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'+
          '<path d="M4 20h4L19 9l-4-4L4 16z"/></svg></button></span></div>';
    }).join("")+'</div>'
    :'<p class="pp-empty">'+esc(t("Who you are talking to about this role: the recruiter, the manager, whoever referred you."))+'</p>')+
  '</div>';
}
const ppJob=()=>(S.jobs||[]).find(x=>x.id===S.jsel);
document.addEventListener("click",e=>{
  const a=e.target.closest("[data-pp-add],[data-pp-edit],[data-pp-write]"); if(!a) return;
  const j=ppJob(); if(!j) return;
  if(a.dataset.ppAdd!=null) return personSheet(j,null);
  const p=(j.people||[]).find(x=>x.id===(a.dataset.ppEdit||a.dataset.ppWrite)); if(!p) return;
  if(a.dataset.ppEdit!=null) return personSheet(j,p);
  draftSheet(j,p,a.dataset.kind||"followup");
});

function personSheet(j,p){
  const v=k=>esc(p&&p[k]||"");
  openSheet('<h3>'+esc(p?t("Edit {n}",{n:p.name||p.email}):t("Someone at {co}",{co:j.company}))+'</h3>'+
    '<div class="pp-form">'+
      '<label>'+esc(t("Name"))+'<input id="pp-name" value="'+v("name")+'" autocomplete="off"></label>'+
      '<label>'+esc(t("Role"))+'<input id="pp-role" list="pp-roles" value="'+esc(p&&p.role?t(p.role):"")+'" autocomplete="off">'+
        '<datalist id="pp-roles">'+PP_ROLES.map(r=>'<option value="'+esc(t(r))+'">').join("")+'</datalist></label>'+
      '<label>'+esc(t("Email"))+'<input id="pp-email" type="email" value="'+v("email")+'" autocomplete="off"></label>'+
      '<label>'+esc(t("Link"))+'<input id="pp-link" type="url" value="'+v("link")+'" placeholder="https://" autocomplete="off"></label>'+
    '</div>'+
    '<div class="foot">'+(p?'<button class="sbtn danger left" id="pp-del">'+esc(t("Remove"))+'</button>':'')+
      '<button class="sbtn" id="pp-cancel">'+esc(t("Cancel"))+'</button>'+
      '<button class="sbtn primary" id="pp-save">'+esc(t(p?"Save":"Add"))+'</button></div>');
  $("#pp-cancel").onclick=closeSheet;
  /* A role picked from the list is stored in English, so it reads in every language. */
  const roleIn=()=>{ const r=$("#pp-role").value.trim(); return PP_ROLES.find(x=>t(x)===r)||r };
  $("#pp-save").onclick=async()=>{
    const q={id:p&&p.id,name:$("#pp-name").value,role:roleIn(),email:$("#pp-email").value,
             link:$("#pp-link").value,last:p&&p.last||""};
    if(!q.name.trim()&&!q.email.trim()) return toast(t("Give them a name or an email."),true);
    const list=j.people||[];
    const people=p?list.map(x=>x.id===p.id?q:x):[...list,q];
    closeSheet(); await saveJob(j.id,{people});
  };
  const del=$("#pp-del");
  if(del) del.onclick=async()=>{ closeSheet(); await saveJob(j.id,{people:(j.people||[]).filter(x=>x.id!==p.id)}) };
}

/* The drafts, in the application's language. {first}: their first name,
   {role} and {co}: the application, {applied} and {met}: dates, {duty}: the
   posting's first duty, {you}: your name. */
const PP_TPL={
  en:{greet:n=>n?"Hi "+n+",":"Hello,", sign:"Best regards,",
    confirm:{s:"{role}: confirming our interview",
      b:"Thank you for setting up the next interview. I am writing to confirm it{next}.\n\nIs there anything I should prepare or read beforehand, and who will I be speaking with?\n\nI look forward to it."},
    followup:{s:"{role}: following up on my application",
      b:"I applied for the {role} role{applied} and wanted to check in on where things stand.\n\nI am still very interested{duty}.\n\nIs there anything else I can send you?"},
    thanks:{s:"Thank you: {role} interview",
      b:"Thank you for your time{met}. I enjoyed hearing about the team and the work, and it made me more keen on the {role} role.\n\n[One thing you talked about that stayed with you.]\n\nI look forward to hearing about the next steps."},
    next:{s:"{role}: next steps",
      b:"Thank you again for the conversation{met}. Could you tell me what the next steps are, and roughly when I might hear back?\n\nI remain very interested in the {role} role at {co}."},
    offer:{s:"{role}: your offer",
      b:"Thank you very much for the offer for the {role} role at {co}. I am delighted, and I would like to take a few days to look at it properly.\n\nCould you send me the full details in writing (salary, start date, benefits) and let me know by when you need my answer?\n\nI will come back to you shortly."},
    applied:d=>" on "+d, met:d=>" on "+d, next:d=>" on "+d, duty:d=>", not least in this part of the role: “"+d+"”"},
  fr:{greet:n=>n?"Bonjour "+n+",":"Bonjour,", sign:"Bien cordialement,",
    confirm:{s:"{role} : confirmation de l'entretien",
      b:"Merci pour l'organisation du prochain entretien. Je vous confirme ma présence{next}.\n\nY a-t-il quelque chose à préparer ou à lire en amont, et avec qui vais-je échanger ?\n\nAu plaisir d'échanger avec vous."},
    followup:{s:"{role} : suivi de ma candidature",
      b:"J'ai postulé au poste de {role}{applied} et je me permets de revenir vers vous pour savoir où en est le processus.\n\nLe poste m'intéresse toujours beaucoup{duty}.\n\nPuis-je vous transmettre autre chose ?"},
    thanks:{s:"Merci pour l'entretien : {role}",
      b:"Merci pour le temps que vous m'avez accordé{met}. J'ai beaucoup apprécié d'en apprendre plus sur l'équipe et le travail, et le poste de {role} m'intéresse d'autant plus.\n\n[Un point de l'échange qui vous a marqué.]\n\nJe reste à votre disposition pour la suite."},
    next:{s:"{role} : prochaines étapes",
      b:"Merci encore pour notre échange{met}. Pourriez-vous m'indiquer les prochaines étapes, et à peu près quand je pourrai avoir un retour ?\n\nLe poste de {role} chez {co} m'intéresse toujours beaucoup."},
    offer:{s:"{role} : votre proposition",
      b:"Merci beaucoup pour votre proposition pour le poste de {role} chez {co}. J'en suis ravi(e), et je souhaiterais prendre quelques jours pour l'étudier sereinement.\n\nPourriez-vous m'envoyer le détail par écrit (salaire, date de début, avantages) et m'indiquer d'ici quand vous attendez ma réponse ?\n\nJe reviens vers vous très vite."},
    applied:d=>" le "+d, met:d=>" le "+d, next:d=>" le "+d, duty:d=>", en particulier pour cette partie du poste : « "+d+" »"},
  es:{greet:n=>n?"Hola, "+n+":":"Hola:", sign:"Un saludo,",
    confirm:{s:"{role}: confirmación de la entrevista",
      b:"Gracias por organizar la próxima entrevista. Te confirmo mi asistencia{next}.\n\n¿Hay algo que deba preparar o leer antes, y con quién voy a hablar?\n\n¡Hasta entonces!"},
    followup:{s:"{role}: seguimiento de mi candidatura",
      b:"Presenté mi candidatura al puesto de {role}{applied} y quería saber en qué punto está el proceso.\n\nEl puesto me sigue interesando mucho{duty}.\n\n¿Puedo enviarte algo más?"},
    thanks:{s:"Gracias por la entrevista: {role}",
      b:"Gracias por tu tiempo{met}. Me gustó mucho conocer mejor el equipo y el trabajo, y el puesto de {role} me interesa todavía más.\n\n[Algo de la conversación que te quedó.]\n\nQuedo a la espera de los próximos pasos."},
    next:{s:"{role}: próximos pasos",
      b:"Gracias de nuevo por la conversación{met}. ¿Podrías decirme cuáles son los próximos pasos y cuándo podría tener noticias?\n\nEl puesto de {role} en {co} me sigue interesando mucho."},
    offer:{s:"{role}: vuestra oferta",
      b:"Muchas gracias por la oferta para el puesto de {role} en {co}. Me hace mucha ilusión y me gustaría tomarme unos días para estudiarla con calma.\n\n¿Podríais enviarme los detalles por escrito (salario, fecha de incorporación, beneficios) y decirme para cuándo necesitáis mi respuesta?\n\nOs respondo muy pronto."},
    applied:d=>" el "+d, met:d=>" el "+d, next:d=>" el "+d, duty:d=>", sobre todo esta parte del puesto: «"+d+"»"},
  pt:{greet:n=>n?"Olá, "+n+",":"Olá,", sign:"Atenciosamente,",
    confirm:{s:"{role}: confirmação da entrevista",
      b:"Obrigado(a) por agendar a próxima entrevista. Confirmo minha presença{next}.\n\nHá algo que eu deva preparar ou ler antes, e com quem vou conversar?\n\nAté lá!"},
    followup:{s:"{role}: acompanhamento da minha candidatura",
      b:"Candidatei-me à vaga de {role}{applied} e gostaria de saber em que ponto está o processo.\n\nA vaga continua me interessando muito{duty}.\n\nPosso enviar mais alguma coisa?"},
    thanks:{s:"Agradecimento pela entrevista: {role}",
      b:"Agradeço pelo seu tempo{met}. Gostei muito de conhecer melhor o time e o trabalho, e a vaga de {role} me interessa ainda mais.\n\n[Algo da conversa que ficou com você.]\n\nFico no aguardo dos próximos passos."},
    next:{s:"{role}: próximos passos",
      b:"Agradeço novamente pela conversa{met}. Poderia me dizer quais são os próximos passos e quando devo ter um retorno?\n\nA vaga de {role} na {co} continua me interessando muito."},
    offer:{s:"{role}: sua proposta",
      b:"Muito obrigado(a) pela proposta para a vaga de {role} na {co}. Fiquei muito feliz e gostaria de alguns dias para avaliá-la com calma.\n\nVocê poderia me enviar os detalhes por escrito (salário, data de início, benefícios) e me dizer até quando precisa da minha resposta?\n\nRetorno em breve."},
    applied:d=>" em "+d, met:d=>" em "+d, next:d=>" em "+d, duty:d=>", principalmente esta parte da vaga: “"+d+"”"},
};
const PP_LOCALE={en:"en-GB",fr:"fr-FR",es:"es-ES",pt:"pt-BR"};
function ppDraft(kind,lang,j,p,ctx){
  const L=PP_TPL[lang]||PP_TPL.en, T=L[kind];
  const day=d=>{ try{ return DTF(PP_LOCALE[lang]||"en-GB",{day:"numeric",month:"long",timeZone:"UTC"}).format(keyDate(d.slice(0,10))) }catch(e){ return d } };
  const fill=s=>s.replace(/\{role\}/g,j.title).replace(/\{co\}/g,j.company)
    .replace(/\{applied\}/g,ctx.applied?L.applied(day(ctx.applied)):"")
    .replace(/\{met\}/g,ctx.interviewed?L.met(day(ctx.interviewed)):"")
    .replace(/\{next\}/g,()=>{ const at=interviewMoment(j); if(!(at>new Date())) return "";
      try{ return L.next(DTF(PP_LOCALE[lang]||"en-GB",{weekday:"long",day:"numeric",month:"long",hour:"2-digit",minute:"2-digit",
        timeZone:j.interview_tz||userTz(),timeZoneName:j.interview_tz&&j.interview_tz!==userTz()?"short":undefined}).format(at)) }catch(e){ return "" } })
    .replace(/\{duty\}/g,ctx.duties&&ctx.duties[0]?L.duty(ctx.duties[0]):"");
  const first=String(p.name||"").trim().split(/\s+/)[0]||"";
  return {subject:fill(T.s), body:L.greet(first)+"\n\n"+fill(T.b)+"\n\n"+L.sign+(ctx.you?"\n"+ctx.you:"")};
}
/* A language named in the interface's own language: "anglais", not "English". */
function langName(code){
  try{ return new Intl.DisplayNames([uiLocale()],{type:"language"}).of(code) }catch(e){ return code }
}
/* A follow-up set for a Saturday or a Sunday moves to the Monday. */
function workday(k){
  const wd=keyDate(k).getUTCDay();
  return wd===6?addDays(k,2):wd===0?addDays(k,1):k;
}
async function draftSheet(j,p,kind){
  /* No one with an email on this application yet: the draft is still worth
     writing, and the address is asked for here rather than the button
     quietly opening the application instead. */
  const ask=!p||!p.email;
  p=p?Object.assign({},p):{name:"",email:""};
  let ctx={};
  try{ ctx=await api("/api/jobs/draft?id="+encodeURIComponent(j.id)) }catch(e){}
  const lang=PP_TPL[j.language]?j.language:(PP_TPL[UI_LANG]?UI_LANG:"en");
  openSheet('<h3>'+esc(t(PP_KIND_LABEL[kind]))+'</h3>'+
    '<p>'+esc(ask?t("Written from this application: edit anything. Add who it goes to below."):
      t("To {n}, at {co}. Written from this application: edit anything.",{n:p.name&&p.email?p.name+" <"+p.email+">":p.name||p.email,co:j.company}))+
      (lang!==UI_LANG?' '+esc(t("In {lang}, the application's language.",{lang:langName(lang)})):'')+'</p>'+
    '<div class="pp-kinds" role="radiogroup" aria-label="'+esc(t("Kind of email"))+'">'+
      Object.keys(PP_KIND_LABEL).filter(k=>(k!=="offer"||j.status==="offer"||kind==="offer")&&(k!=="confirm"||interviewMoment(j)>new Date()||kind==="confirm")).map(k=>'<button class="pp-kind" role="radio" aria-checked="'+(k===kind)+'" data-k="'+k+'">'+
        esc(t(PP_KIND_LABEL[k]))+'</button>').join("")+'</div>'+
    (ask?'<label class="pk-field">'+esc(t("To"))+'<input id="dr-to" type="email" autocomplete="email" placeholder="'+esc(t("their email, e.g. name@company.com"))+'"></label>':'')+
    '<label class="pk-field">'+esc(t("Subject"))+'<input id="dr-sub" spellcheck="true"></label>'+
    '<label class="pk-field">'+esc(t("Message"))+'<textarea id="dr-body" rows="12" spellcheck="true"></textarea></label>'+
    /* With an interview booked, that date is the next step, not a follow-up. */
    '<label class="pk-check" id="dr-move-row"><input type="checkbox" id="dr-move"'+(interviewMoment(j)>new Date()?'':' checked')+'>'+
      esc(t("Afterwards, move the follow-up to a week from today"))+'</label>'+
    '<div class="foot"><button class="sbtn" id="dr-cancel">'+esc(t("Cancel"))+'</button>'+
      '<button class="sbtn" id="dr-copy">'+esc(t("Copy"))+'</button>'+
      '<button class="sbtn primary" id="dr-mail"'+(p.email?'':' disabled title="'+esc(t("No email address for them yet"))+'"')+'>'+
        esc(t("Open in my mail app"))+'</button></div>');
  const fillIn=k=>{
    kind=k;
    const d=ppDraft(k,lang,j,p,ctx);
    $("#dr-sub").value=d.subject; $("#dr-body").value=d.body;
    $$("#sheet .pp-kind").forEach(b=>b.setAttribute("aria-checked",String(b.dataset.k===k)));
    $("#sheet h3").textContent=t(PP_KIND_LABEL[k]);
    $("#dr-move-row").hidden=k!=="followup";
  };
  fillIn(kind);
  const to=$("#dr-to");
  if(to) to.oninput=()=>{ p.email=to.value.trim(); const ok=/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(p.email);
    $("#dr-mail").disabled=!ok; $("#dr-mail").title=ok?"":t("No email address for them yet") };
  $$("#sheet .pp-kind").forEach(b=>b.onclick=()=>fillIn(b.dataset.k));
  $("#dr-cancel").onclick=closeSheet;
  $("#dr-copy").onclick=async()=>{
    try{ await navigator.clipboard.writeText($("#dr-sub").value+"\n\n"+$("#dr-body").value); toast(t("Copied")) }
    catch(e){ toast(t("Could not copy: select the text instead."),true) }
  };
  $("#dr-mail").onclick=async()=>{
    const url="mailto:"+encodeURIComponent(p.email).replace(/%40/g,"@")+"?subject="+encodeURIComponent($("#dr-sub").value)+
      "&body="+encodeURIComponent($("#dr-body").value);
    const move=kind==="followup"&&$("#dr-move").checked;
    closeSheet();
    if(window.__TAURI__) post("/api/open",{url}).catch(err=>toast(err.message,true));
    else location.href=url;
    const people=(j.people||[]).map(x=>x.id===p.id?Object.assign({},x,{last:todayKey()}):x);
    await saveJob(j.id,Object.assign({people},move?{followup_date:workday(addDays(todayKey(),7))}:{}));
  };
}

boot();
