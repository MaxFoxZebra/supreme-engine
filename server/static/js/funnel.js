/* Funnel D3 Sankey chart, conversion metrics, momentum, and sources. */
/* =========================================================================
   Funnel

   The layout is the real d3-sankey, vendored locally rather than approximated,
   so the ribbon geometry is correct. Scripts load on first open, so opening
   the editor pays nothing for a screen that may never be used.
   ========================================================================= */
let d3ready=null;
function loadScript(src){
  return new Promise((res,rej)=>{
    const el=document.createElement("script");
    el.src=src; el.onload=res; el.onerror=()=>rej(new Error("could not load "+src));
    document.head.append(el);
  });
}
function ensureD3(){
  if(!d3ready) d3ready=(async()=>{
    /* order matters: sankey needs array, shape needs path */
    for(const m of ["d3-array","d3-path","d3-shape","d3-sankey"])
      await loadScript("/static/"+m+".min.js");
  })();
  return d3ready;
}

/* One ochre path through the chart: the applications that are still worth
   something. Totals are dark; every other outcome is neutral. */
/* Five roles, not three. A rejection and a reply-you-are-waiting-on used to be
   the same grey, which is the one distinction the chart exists to make. The
   spine and the waiting stages stay recessive so the outcomes carry the colour;
   every node is labelled, so nothing here is colour alone. */
/* The funnel nodes mapped onto the same vocabulary the Jobs table uses, so a
   status cannot mean one thing in the table and another in the chart. The
   spine and the waiting stages stay recessive; outcomes carry the colour. */
const FN_TOTAL=new Set(["all","applied_s"]);
/* An outcome's colour. Two splits matter here and both used to be painted
   over: an offer is not the same state as "still interviewing" (they shared
   --fn-positive, so "I have an offer" and "nothing decided yet" were the same
   colour), and a rejection after three rounds is not a rejection after nobody
   read past page one -- the README says those say very different things, and
   all four dead ends were one tone. */
const FN_TONE={
  pending:"draft", awaiting:"waiting", still_iv:"live",
  interview_s:"live", offer_s:"offer", deciding:"offer",
  accepted:"won", refused:"closed",
  rejected:"lost", ghosted:"lost",
  rejected_iv:"lost-late", ghosted_iv:"lost-late",
};

$$("#range button").forEach(b=>b.onclick=()=>{
  $$("#range button").forEach(x=>x.setAttribute("aria-selected",String(x===b)));
  S.since=b.dataset.since; S.funnel=null; loadFunnel("morph");
});
function sinceDate(){
  if(!S.since) return null;
  const d=new Date();
  if(S.since==="6m") d.setMonth(d.getMonth()-6); else d.setDate(d.getDate()-30);
  return d.toISOString().slice(0,10);
}

/* The chart's colours are the stage's own variables (--sk-*), so they follow
   the theme with it: the funnel's light palette on white, the dark one on the
   dark stage. SVG only reads a variable from a style, never an attribute. */
const skTone=id=>FN_TOTAL.has(id)?"total":(FN_TONE[id]||"draft");
const skCol=id=>"var(--sk-"+skTone(id)+")";
/* Where an application can still change. These are the stages the lights
   travel to: what is in motion is what is still in play. */
const FN_LIVE=["awaiting","still_iv","deciding"];
const FN_OF_ALL=new Set(["all","pending","applied_s"]);
/* Filled by loadJobs, like DEAD_ST and LIVE_ST: every status that got as far
   as an interview (the server's interview_s node). */
const IV_STATUSES=new Set();
const reduceMotion=()=>matchMedia("(prefers-reduced-motion: reduce)").matches;

/* mode: "enter" plays the entrance, "morph" moves the old shape to the new
   one (a range change), "still" redraws in place (a click, a resize). */
async function loadFunnel(mode="enter"){
  const host=$("#chart");
  if(S.funnel){ return drawFunnel(mode) }
  if(mode!=="morph") host.innerHTML='<p class="note"><span class="spin"></span> Loading…</p>';
  try{
    await ensureD3();
    const since=sinceDate();
    S.funnel=await api("/api/funnel"+(since?"?since="+since:""));
    if(!S.jready) await loadJobs(true);
  }catch(e){
    host.innerHTML='<div class="empty"><h3>Could not load the funnel</h3><p>'+
      esc(e.message)+'</p></div>';
    return;
  }
  drawFunnel(mode);
}

/* The applications the range covers, from the list the Applications screen
   already has, so nothing here can disagree with it. */
function fnJobs(){
  const since=S.funnel&&S.funnel.since;
  return (S.jobs||[]).filter(j=>!since||String(j.created_at||"")>=since);
}
function fnEvent(j,status){
  const e=(j.status_history||[]).find(e=>e.status===status);
  return e&&e.at?new Date(String(e.at).slice(0,19)):null;
}
const DAY=86400000;

let fnTimer=null;
function drawFunnel(mode="still"){
  const f=S.funnel, t=f.totals, page=$("#fn-page");
  const animate=mode!=="still"&&!reduceMotion();
  const live=fnJobs().filter(j=>["applied","interviewing","offer"].includes(j.status)).length;
  const first=fnJobs().map(j=>j.created_at).filter(Boolean).sort()[0];
  $("#fn-sub").textContent=t.total?t.total+" application"+(t.total===1?"":"s")+
    (first&&!f.since?" since "+shortDate(first):"")+" · "+live+" still open":"";
  if(!t.total){
    page.classList.remove("fx-in");
    $("#fn-body").hidden=true;
    let e=$("#fn-none");
    if(!e){ e=document.createElement("div"); e.id="fn-none"; e.className="empty"; page.append(e) }
    e.innerHTML='<h3>Nothing tracked'+(f.since?' in this range':' yet')+'</h3><p>Add applications '+
      'and this shows how far they get: how many reach an interview, how many convert to '+
      'an offer, where the rest drop out, and which sources are worth your time.</p>';
    return;
  }
  const none=$("#fn-none"); if(none) none.remove();
  $("#fn-body").hidden=false;
  if(mode==="enter"&&animate){
    page.classList.remove("fx-in"); void page.offsetWidth; page.classList.add("fx-in");
    clearTimeout(fnTimer); fnTimer=setTimeout(()=>page.classList.remove("fx-in"),3400);
  }
  drawJourney(mode,animate);
  /* The column beside the chart first: the chart is sized to fill the
     height it sets. */
  drawMomentum();
  paintFunnelJobs();
  drawSankey(mode,animate);
  drawSources();
  drawReplies();
  drawRates();
}

/* Numbers that count up to their value, or from the one they showed before a
   range change. */
function countUp(root,animate,delay0=0){
  root.querySelectorAll("[data-to]").forEach((el,i)=>{
    const to=+el.dataset.to, from=+(el.dataset.from||0), suf=el.dataset.suf||"";
    if(!animate||to===from){ el.textContent=to+suf; return }
    const d=+(el.dataset.delay||delay0), dur=1100, t0=performance.now()+d;
    el.textContent=from+suf;
    const step=now=>{
      const k=Math.min(1,Math.max(0,(now-t0)/dur)), e=1-Math.pow(1-k,3);
      el.textContent=Math.round(from+(to-from)*e)+suf;
      if(k<1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

function fnStages(){
  const t=S.funnel.totals, c=S.funnel.by_status||{};
  const heard=t.interviewed+(c.rejected||0);
  return [["Sent",t.applied],["Heard back",heard],["Interviewed",t.interviewed],
          ["Offers",t.offers],["Accepted",c.accepted||0]];
}
function drawJourney(mode,animate){
  const host=$("#fn-journey"), st=fnStages(), t=S.funnel.totals, sent=st[0][1];
  const prev=S.fnPrevJourney||[];
  /* Each arrow is the share of the stage before it that made the next one. */
  const conv=[["replied"],["to interview"],["to offer"],["accepted"]];
  /* The accent marks the step that is actually leaking, once there are
     enough applications under it to call it a rate. */
  let worst=-1, low=101;
  st.forEach(([,n],i)=>{ if(!i) return; const d=st[i-1][1];
    if(d>=10){ const r=n/d*100; if(r<low){ low=r; worst=i } } });
  /* Each stage in the colour the chart below gives it. */
  const col=["var(--sk-waiting)","var(--sk-closed)","var(--sk-live)","var(--sk-offer)","var(--sk-won)"];
  /* The step into each stage. Under ten a rate is an anecdote, so it reads
     as a count; with nothing before it there is no step to show. */
  const chip=i=>{
    const d=st[i-1][1], n=st[i][1]; if(!d) return "";
    return '<span class="fj-conv'+(i===worst?" acc":d<10?" thin":"")+'" style="--x:'+(i*20)+
      '%;animation-delay:'+(1+i*.2)+'s"><b>'+(d<10?n+"/"+d:Math.round(n/d*100)+"%")+'</b> '+
      '<span>'+conv[i-1][0]+'</span>'+(i===worst?' <span class="fj-low">lowest step</span>':"")+'</span>';
  };
  let h='<div class="fj-cols">';
  st.forEach(([label,n],i)=>{
    const sub=i?(n&&sent?Math.round(n/sent*100)+"% of sent":"none yet"):
      /* What is left to do there: the drafts still to send. */
      (t.total-n>0?(t.total-n)+" draft"+(t.total-n===1?"":"s")+" not sent yet":t.total?"every draft sent":"");
    h+='<div class="fj-stg fx-r'+(n?"":" zero")+'" style="--c:'+col[i]+';animation-delay:'+(.3+i*.12)+'s">'+
      '<span class="sl"><i></i>'+label+'</span>'+
      '<b data-to="'+n+'" data-from="'+(mode==="morph"&&prev[i]!=null?prev[i]:0)+'" data-delay="'+
      (mode==="morph"?0:450+i*180)+'">'+n+'</b><small>'+sub+'</small>'+(i?chip(i):"")+'</div>';
  });
  h+='</div>';
  /* The ribbon: each stage as thick as its share of what was sent, necking
     down between them, over a track the full height of Sent so what fell
     away reads as the gap. Stretched to the card, so drawn in a 1000-wide
     box and never re-measured. */
  const Hh=84, M=Hh/2, R=64, seg=200, ease=60;
  const half=st.map(([,n])=>sent&&n?Math.max(2.5,R/2*n/sent):1);
  /* Half the thickness at x: flat through a stage, an eased neck across
     each boundary. */
  const at=x=>{
    const k=Math.round(x/seg), d=x-k*seg;
    if(k<1||k>4||Math.abs(d)>=ease) return half[Math.min(4,Math.floor(x/seg))];
    const u=(d+ease)/(2*ease), e=u*u*(3-2*u);
    return half[k-1]+(half[k]-half[k-1])*e;
  };
  const xs=[]; for(let x=0;x<=1000;x+=4) xs.push(x);
  const ribbon="M"+xs.map(x=>x+" "+(M-at(x)).toFixed(2)).join(" L")+
    " L"+xs.reverse().map(x=>x+" "+(M+at(x)).toFixed(2)).join(" L")+"Z";
  /* A stage nobody has reached yet stays grey: a thread, not progress. */
  const grad=col.map((c,i)=>'<stop offset="'+(i*20+10)+'%" style="stop-color:'+
    (st[i][1]?c:"var(--dot-dead)")+'"/>').join("");
  h+='<div class="fj-flow" aria-hidden="true"><svg viewBox="0 0 1000 '+Hh+'" preserveAspectRatio="none">'+
    '<defs><linearGradient id="fj-g" x1="0" x2="1" y1="0" y2="0">'+grad+'</linearGradient>'+
    '<linearGradient id="fj-sh" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".22"/>'+
    '<stop offset=".5" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".08"/></linearGradient></defs>'+
    '<rect class="fj-ghost" x="0" y="'+(M-R/2)+'" width="1000" height="'+R+'"/>'+
    '<g class="fj-rib"><path d="'+ribbon+'" fill="url(#fj-g)"/><path d="'+ribbon+'" fill="url(#fj-sh)"/></g>'+
    '</svg>'+st.map((s,i)=>i?chip(i):"").join("")+'</div>';
  host.innerHTML=h;
  countUp(host,animate);
  S.fnPrevJourney=st.map(s=>s[1]);
}

/* ---- the chart ---------------------------------------------------------- */
function skBand(x0,x1,y0,y1,w){
  const xm=(x0+x1)/2, a=y0-w/2, b=y1-w/2, c=y0+w/2, d=y1+w/2;
  return "M"+x0+" "+a+"C"+xm+" "+a+" "+xm+" "+b+" "+x1+" "+b+"L"+x1+" "+d+
    "C"+xm+" "+d+" "+xm+" "+c+" "+x0+" "+c+"Z";
}
function skCenter(l){
  const x0=l.source.x1, x1=l.target.x0, xm=(x0+x1)/2;
  return x0+" "+l.y0+"C"+xm+" "+l.y0+" "+xm+" "+l.y1+" "+x1+" "+l.y1;
}
function drawSankey(mode,animate){
  const f=S.funnel, host=$("#chart");
  const nodes=f.nodes.filter(n=>n.count>0);
  const idx=new Map(nodes.map((n,i)=>[n.id,i]));
  const links=f.links.filter(l=>idx.has(l.source)&&idx.has(l.target))
    .map(l=>({source:idx.get(l.source),target:idx.get(l.target),value:l.value,
              sid:l.source,tid:l.target}));
  if(!links.length){ host.innerHTML=""; return }
  const W=Math.max(560,host.clientWidth||900);
  const side=$(".fn-side").offsetHeight;
  /* Every final stage in view without scrolling: the chart stops at the
     bottom of the window rather than matching the column beside it. */
  const room=innerHeight-Math.max(0,host.getBoundingClientRect().top)-56;
  const H=Math.max(340,Math.min(640,room,side>200&&innerWidth>1180?side-86:nodes.length*42));
  /* The right-hand pad is where the last column's labels sit. */
  const PAD=Math.max(170,Math.min(220,W*0.22));
  const layout=d3.sankey().nodeWidth(10).nodePadding(22).nodeAlign(d3.sankeyLeft)
    .extent([[2,16],[W-PAD,H-6]]);
  const g=layout({nodes:nodes.map(n=>({...n})),links:links.map(l=>({...l}))});
  const total=f.totals.total||1;

  /* Every link on a path through a node: what lights up when you hover it. */
  const up=id=>g.links.filter(l=>l.tid===id).flatMap(l=>[l,...up(l.sid)]);
  const down=id=>g.links.filter(l=>l.sid===id).flatMap(l=>[l,...down(l.tid)]);
  const lineage=new Map(g.nodes.map(n=>[n.id,new Set([...up(n.id),...down(n.id)])]));
  const touches=l=>!S.fnode||lineage.get(S.fnode).has(l);

  const defs=g.links.map((l,i)=>'<linearGradient id="skg'+i+'" gradientUnits="userSpaceOnUse" x1="'+
    l.source.x1+'" x2="'+l.target.x0+'" y1="0" y2="0"><stop offset="0" style="stop-color:'+skCol(l.sid)+
    ';stop-opacity:var(--sk-a0)"/><stop offset="1" style="stop-color:'+skCol(l.tid)+';stop-opacity:var(--sk-a1)"/>'+
    '</linearGradient>').join("");
  const bands=g.links.map((l,i)=>'<path class="sk-link'+(touches(l)?"":" sk-dim")+'" data-i="'+i+
    '" d="'+skBand(l.source.x1,l.target.x0,l.y0,l.y1,Math.max(1,l.width))+'" fill="url(#skg'+i+')" '+
    'style="animation-delay:'+(.9+l.source.depth*.28).toFixed(2)+'s"><title>'+esc(l.source.label)+
    ' → '+esc(l.target.label)+': '+l.value+'</title></path>').join("");

  /* Lights along the whole path to each stage still in play, more of them
     where more applications are waiting. */
  const chain=id=>{ const l=g.links.find(l=>l.tid===id); return l?[...chain(l.sid),l]:[] };
  let parts="";
  FN_LIVE.forEach(id=>{
    const n=g.nodes.find(n=>n.id===id); if(!n) return;
    const ch=chain(id); if(!ch.length) return;
    const d="M"+skCenter(ch[0])+ch.slice(1).map(l=>" L"+skCenter(l)).join("");
    /* A stream rather than a line: more lights where more is waiting, spread
       across the narrowest band they pass through, each its own size, glow
       and pace, so they drift like a current instead of marching in step. */
    const band=Math.max(2,Math.min(...ch.map(l=>l.width)));
    const cnt=Math.max(4,Math.min(22,Math.round(n.count*1.2)));
    for(let k=0;k<cnt;k++){
      const r1=((k*73)%97)/97, r2=((k*41+13)%89)/89, r3=((k*29+7)%83)/83;
      const dur=6+r1*3.5, jit=(r2-.5)*band*.8;
      parts+='<circle r="'+(1.5+r3*1.3).toFixed(2)+'" style="fill:'+skCol(id)+';opacity:'+(.55+r1*.45).toFixed(2)+
        '" filter="url(#skglow)" transform="translate(0 '+jit.toFixed(1)+')"><animateMotion dur="'+dur.toFixed(2)+
        's" begin="'+(-(dur/cnt)*k-r3*dur).toFixed(2)+'s" repeatCount="indefinite" path="'+d+'" keyPoints="0;1" '+
        'keyTimes="0;1" calcMode="spline" keySplines=".35 0 .65 1"/></circle>';
    }
  });

  const bars=g.nodes.map(n=>{
    const h=Math.max(3,n.y1-n.y0);
    const off=S.fnode&&S.fnode!==n.id&&![...lineage.get(S.fnode)].some(l=>l.sid===n.id||l.tid===n.id);
    return '<g class="sk-hit'+(off?" sk-dim":"")+'" data-node="'+esc(n.id)+'"'+(n.id==="all"?'':' role="button" tabindex="0"')+' '+
      'aria-label="'+esc(t(n.label))+': '+n.count+'. '+esc(t("List them"))+'" style="animation-delay:'+
      (.7+n.depth*.28).toFixed(2)+'s">'+
      '<rect x="'+(n.x0-6)+'" y="'+(n.y0-10)+'" width="'+(n.x1-n.x0+12)+'" height="'+(h+20)+
      '" fill="transparent"/>'+
      '<rect class="sk-node'+(S.fnode===n.id?" sk-on":"")+'" data-n="'+esc(n.id)+'" x="'+n.x0+
      '" y="'+n.y0+'" width="'+(n.x1-n.x0)+'" height="'+h+'" rx="2.5" style="fill:'+skCol(n.id)+'"/>'+
      (FN_LIVE.includes(n.id)?'<rect class="sk-ring" x="'+(n.x0-3)+'" y="'+(n.y0-3)+'" width="'+
        (n.x1-n.x0+6)+'" height="'+(h+6)+'" rx="4" style="stroke:'+skCol(n.id)+'"/>':"")+'</g>';
  }).join("");

  /* Past the first split, a share is of what was sent, as in the steps above. */
  const sentBase=((g.nodes.find(x=>x.id==="applied_s")||{}).count)||total;
  /* One base on the chips and in the tooltip: past sending, a share of sent.
     Before it (tracked, drafts, sent itself) there is nothing to divide by. */
  const pct=n=>FN_OF_ALL.has(n.id)?null:Math.round(n.count/sentBase*100);
  const chips=g.nodes.map(n=>{
    const cy=(n.y0+n.y1)/2, off=S.fnode&&S.fnode!==n.id&&
      ![...lineage.get(S.fnode)].some(l=>l.sid===n.id||l.tid===n.id);
    return '<div class="sk-chip'+(n.id==="accepted"?" won":"")+(off?" sk-dim":"")+'" data-chip="'+
      esc(n.id)+'" style="left:'+(n.x1+8)+'px;top:'+(cy-12)+'px;animation-delay:'+
      (1.2+n.depth*.28).toFixed(2)+'s"><i style="background:'+skCol(n.id)+'"></i>'+esc(n.label)+
      ' <b>'+n.count+'</b>'+(pct(n)==null?'':'<span>'+pct(n)+'%</span>')+
      '</div>';
  }).join("");

  host.innerHTML='<svg width="'+W+'" height="'+H+'" viewBox="0 0 '+W+' '+H+'" role="img" '+
    'aria-label="Application funnel">'+
    '<defs>'+defs+'<filter id="skglow" x="-3" y="-3" width="7" height="7"><feGaussianBlur '+
    'stdDeviation="1.6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/>'+
    '</feMerge></filter></defs><g>'+bands+'</g><g class="sk-parts">'+parts+'</g><g>'+bars+
    '</g></svg>'+chips;
  $("#fn-legend").hidden=!FN_LIVE.some(id=>g.nodes.some(n=>n.id===id));

  /* Hover traces a stage's whole path; the rest steps back. */
  const paths=[...host.querySelectorAll(".sk-link")];
  const trace=id=>{
    const set=id?lineage.get(id):null;
    paths.forEach((p,i)=>p.classList.toggle("sk-dim",set?!set.has(g.links[i]):!touches(g.links[i])));
    host.querySelector(".sk-tip")?.remove();
    if(!id) return;
    const n=g.nodes.find(n=>n.id===id);
    const want=new Set((S.nodes&&S.nodes[id])||[]);
    const names=fnJobs().filter(j=>want.has(j.status)).map(j=>j.company);
    const who=names.slice(0,3).join(", ")+(names.length>3?" +"+(names.length-3):"");
    const tip=document.createElement("div");
    tip.className="sk-tip";
    tip.innerHTML='<b>'+esc(n.label)+' · '+n.count+'</b><span>'+(pct(n)==null?Math.round(n.count/total*100)+
      '% of everything tracked':pct(n)+'% of sent')+'</span>'+(who?'<span class="who">'+esc(who)+'</span>':'')+
      '<span class="go">'+(S.fnode===id?"Click to clear":"Click to list them")+'</span>';
    host.append(tip);
    const x=Math.min(n.x1+30,W-tip.offsetWidth-4), y=Math.max(-8,n.y0-tip.offsetHeight-8);
    tip.style.left=x+"px"; tip.style.top=(y<0?n.y1+10:y)+"px";
  };
  host.querySelectorAll("[data-node]").forEach(el=>{
    const id=el.dataset.node, pick=()=>fnPick(id);
    /* Every application is the list already: the root traces, it does not select. */
    if(id!=="all") el.onclick=pick;
    el.onmouseenter=el.onfocus=()=>trace(id);
    el.onmouseleave=el.onblur=()=>trace(null);
    el.onkeydown=e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); pick() } };
  });

  if(mode==="morph"&&animate&&S.fnGeom) skMorph(host,g,S.fnGeom);
  S.fnGeom={links:new Map(g.links.map(l=>[l.sid+">"+l.tid,{x0:l.source.x1,x1:l.target.x0,y0:l.y0,
    y1:l.y1,w:Math.max(1,l.width)}])),nodes:new Map(g.nodes.map(n=>[n.id,{x0:n.x0,y0:n.y0,y1:n.y1}]))};
}

/* A range change moves each band and stage from where it was to where it
   now is, rather than wiping the chart and drawing another. */
function skMorph(host,g,old){
  const paths=[...host.querySelectorAll(".sk-link")], rects=host.querySelectorAll(".sk-node");
  const chips=host.querySelectorAll(".sk-chip"), rings=host.querySelectorAll(".sk-ring");
  const parts=host.querySelector(".sk-parts");
  const from=g.links.map(l=>{
    const o=old.links.get(l.sid+">"+l.tid);
    return o||{x0:l.source.x1,x1:l.target.x0,y0:l.y0,y1:l.y1,w:0};
  });
  const nfrom=g.nodes.map(n=>old.nodes.get(n.id)||{x0:n.x0,y0:(n.y0+n.y1)/2,y1:(n.y0+n.y1)/2});
  const lerp=(a,b,k)=>a+(b-a)*k, dur=650, t0=performance.now();
  if(parts) parts.style.opacity="0";
  const frame=now=>{
    const k=Math.min(1,(now-t0)/dur), e=k<.5?4*k*k*k:1-Math.pow(-2*k+2,3)/2;
    g.links.forEach((l,i)=>{ const a=from[i];
      paths[i].setAttribute("d",skBand(lerp(a.x0,l.source.x1,e),lerp(a.x1,l.target.x0,e),
        lerp(a.y0,l.y0,e),lerp(a.y1,l.y1,e),lerp(a.w,Math.max(1,l.width),e))) });
    g.nodes.forEach((n,i)=>{ const a=nfrom[i], y0=lerp(a.y0,n.y0,e), y1=lerp(a.y1,n.y1,e);
      rects[i].setAttribute("y",y0); rects[i].setAttribute("height",Math.max(3,y1-y0));
      chips[i].style.top=((y0+y1)/2-12)+"px" });
    rings.forEach(r=>r.style.opacity=String(e));
    if(k<1) requestAnimationFrame(frame);
    else if(parts){ parts.style.transition="opacity .6s"; parts.style.opacity="1" }
  };
  requestAnimationFrame(frame);
}

/* Clicking a stage lists who is in it, beside the chart, in place of what is
   still in play; clicking it again goes back. */
function fnPick(id){
  S.fnode=S.fnode===id?null:id;
  paintFunnelJobs();
  drawSankey("still",false);
}

/* ---- beside the chart --------------------------------------------------- */
function drawMomentum(){
  const host=$("#fn-momentum"), jobs=fnJobs(), now=new Date();
  const monday=d=>{ const x=new Date(d); x.setHours(0,0,0,0); x.setDate(x.getDate()-((x.getDay()+6)%7)); return x };
  const sentAt=jobs.map(j=>fnEvent(j,"applied")||(j.status!=="pending"&&j.created_at?
    new Date(String(j.created_at).slice(0,19)):null)).filter(Boolean);
  const ivAt=jobs.map(j=>fnEvent(j,"interviewing")).filter(Boolean);
  const start=monday(S.funnel.since?new Date(S.funnel.since):
    (sentAt.length?new Date(Math.min(...sentAt)):now));
  const thisWk=monday(now);
  const weeks=Math.max(1,Math.min(26,Math.round((thisWk-start)/(7*DAY))+1));
  const w0=new Date(thisWk-(weeks-1)*7*DAY);
  const bucket=d=>Math.floor((monday(d)-w0)/(7*DAY));
  const sent=Array(weeks).fill(0), iv=Array(weeks).fill(0);
  sentAt.forEach(d=>{ const b=bucket(d); if(b>=0&&b<weeks) sent[b]++ });
  ivAt.forEach(d=>{ const b=bucket(d); if(b>=0&&b<weeks) iv[b]++ });
  const mx=Math.max(1,...sent), tot=sent.reduce((a,b)=>a+b,0), ivs=iv.reduce((a,b)=>a+b,0);
  const avg=(tot/weeks).toFixed(1).replace(/\.0$/,"");
  const mid=new Date(+w0+Math.floor(weeks/2)*7*DAY);
  host.innerHTML='<div class="fn-ch"><h2>Momentum</h2><span>applications sent, by week</span></div>'+
    '<div class="fn-mhead"><div><span class="fn-big" data-to="'+sent[weeks-1]+'" data-delay="900">'+
    sent[weeks-1]+'</span><span style="font-size:13px;color:var(--t500)"> this week</span></div>'+
    '<div class="sub">'+avg+' a week on average<br><b>● '+ivs+' reached interview</b> in '+
    weeks+' week'+(weeks===1?"":"s")+'</div></div>'+
    '<div class="fn-weeks" role="img" aria-label="'+esc(sent.join(", "))+' sent per week, oldest first">'+
    sent.map((s,i)=>'<div class="fn-wk" title="Week of '+shortDate(new Date(+w0+i*7*DAY))+': '+s+
      ' sent'+(iv[i]?", "+iv[i]+" interview"+(iv[i]===1?"":"s"):"")+'"><span class="iv">'+
      ('<i style="animation-delay:'+(1+i*.035).toFixed(3)+'s"></i>').repeat(Math.min(iv[i],4))+'</span>'+
      '<span class="bar'+(i===weeks-1?" now":"")+'" style="height:'+Math.max(2,Math.round(s/mx*118))+
      'px;animation-delay:'+(1+i*.035).toFixed(3)+'s"></span></div>').join("")+'</div>'+
    '<div class="fn-wax"><span>'+shortDate(w0)+'</span><span>'+(weeks>6?MONTHS_LONG[mid.getMonth()]:"")+
    '</span><span>This week</span></div>';
  countUp(host,$("#fn-page").classList.contains("fx-in"));
}
const MONTHS_LONG=Array.from({length:12},(_,i)=>{ try{ return new Intl.DateTimeFormat(uiLocale(),{month:"long"})
  .format(new Date(2026,i,15)) }catch(e){ return String(i+1) } });

function fnNext(j){
  const days=d=>Math.max(0,Math.round((Date.now()-d)/DAY));
  if(j.status==="interviewing"){
    const at=interviewMoment(j);
    if(at&&at>new Date()) return t("Interview")+" "+fmtWhen(at,userTz());
    const d=fnEvent(j,"interviewing"); return d?"Interviewing since "+shortDate(d):"";
  }
  if(j.status==="offer"){ const d=fnEvent(j,"offer"); return d?"Offer since "+shortDate(d):"" }
  if(j.status!=="applied"){
    const last=(j.status_history||[]).slice(-1)[0];
    return last&&last.at?shortDate(last.at):"";
  }
  const d=fnEvent(j,"applied");
  if(!d) return "";
  const n=days(d); return n?"Sent "+n+" day"+(n===1?"":"s")+" ago":"Sent today";
}
function paintFunnelJobs(){
  const host=$("#fn-jobs"), hint=$("#fn-hint");
  if(!host) return;
  const row=j=>'<button class="fn-jrow" data-id="'+esc(j.id)+'">'+companyMark(j)+
    '<span class="fj-who"><span class="con">'+esc(j.company)+'</span><span class="fj-role">'+
    esc(j.title)+'</span></span><span class="fj-st"><span class="st"><span class="dot '+
    statusTone(j.status)+'"></span><span>'+esc(prettyStatus(j.status))+'</span>'+
    (j.cv_path?'<span class="fj-cv" data-open-doc="'+esc(j.cv_path)+'">'+t("Open CV")+'</span>':'')+
    '</span><small>'+esc(fnNext(j))+'</small></span></button>';
  if(!S.fnode){
    hint.textContent="Hover a stage to trace its path · click to list them";
    const live=fnJobs().filter(j=>["applied","interviewing","offer"].includes(j.status))
      .sort((a,b)=>{ const r={offer:0,interviewing:1,applied:2};
        return r[a.status]-r[b.status]||String(b.updated_at).localeCompare(String(a.updated_at)) });
    host.innerHTML='<div class="fn-ch"><h2>Still open</h2><span>'+live.length+'</span></div>'+
      (live.length?live.slice(0,5).map(row).join("")
        :'<p class="note">Nothing is waiting on an answer. Everything here has an outcome.</p>')+
      (live.length>5?'<div class="fn-jfoot"><button class="obtn" id="fn-open">All '+live.length+
        ' in Applications</button></div>':'');
  }else{
    const want=new Set((S.nodes&&S.nodes[S.fnode])||[]);
    const rows=fnJobs().filter(j=>want.has(j.status));
    const label=(S.labels&&S.labels[S.fnode])||S.fnode;
    hint.innerHTML="Showing <b>"+esc(label)+"</b> · click it again to clear";
    /* The chart's palette lives on its stage; the swatch borrows the colour. */
    const stage=$(".fn-stage"), sw=stage?getComputedStyle(stage).getPropertyValue("--sk-"+skTone(S.fnode)).trim():"";
    host.innerHTML='<div class="fn-ch">'+(sw?'<span class="fsw" style="background:'+sw:'<span hidden style="')+
      '"></span><h2>'+esc(label)+'</h2><span>'+rows.length+'</span><span class="grow"></span>'+
      '<button class="x" id="fn-clear" title="Back to the open applications" aria-label="Clear">&#10005;</button></div>'+
      (rows.length?'<div class="fn-jlist">'+rows.map(row).join("")+'</div>'
        :'<p class="note">Nothing sits at this stage yet.</p>')+
      '<div class="fn-jfoot"><button class="obtn" id="fn-open">Show in Applications</button></div>';
    $("#fn-clear").onclick=()=>fnPick(S.fnode);
  }
  const open=$("#fn-open");
  if(open) open.onclick=()=>{
    S.jfilter=S.fnode?{kind:"node",value:S.fnode}:{kind:"all",value:""}; S.jsel=null;
    $("#jobq").value=""; setView("jobs");
  };
  host.querySelectorAll("[data-id]").forEach(b=>b.onclick=ev=>{
    const doc=ev.target.closest("[data-open-doc]");
    if(doc){ ev.stopPropagation(); openDoc(doc.dataset.openDoc); return }
    S.jfilter={kind:"all",value:""};
    setView("jobs"); selectJob(b.dataset.id);
  });
}

/* ---- underneath --------------------------------------------------------- */
/* Interview rate by where the posting was found: the number that changes
   what you do next week. Grouped by board, so "LinkedIn" and "linkedin" are
   one source. */
function fnSources(){
  const by=new Map();
  fnJobs().filter(j=>j.status!=="pending").forEach(j=>{
    const b=jobBoard(j), said=String(j.source||"").trim();
    const key=b?b.id:said.toLowerCase()||"-";
    const e=by.get(key)||{label:b?b.label:said||"Not set",board:b,said,sent:0,iv:0};
    e.sent++; if(IV_STATUSES.has(j.status)) e.iv++;
    by.set(key,e);
  });
  return [...by.values()].map(e=>({...e,rate:e.sent?e.iv/e.sent:0}))
    .sort((a,b)=>(a.label==="Not set")-(b.label==="Not set")||b.rate-a.rate||b.sent-a.sent);
}
function drawSources(){
  const host=$("#fn-sources"), src=fnSources(), known=src.filter(s=>s.label!=="Not set");
  let h='<div class="fn-ch"><h2>Where interviews come from</h2><span>interview rate by source</span></div>';
  if(known.length<2){
    host.innerHTML=h+'<p class="fn-empty">Set where you found each application (on the '+
      'application, under Found on) and this shows which sources turn into interviews.</p>';
    return;
  }
  const glyph=s=>{
    if(s.board) return boardMark(s.board);
    if(/referr/i.test(s.said)) return "♥";
    if(/recruit/i.test(s.said)) return "☎";
    if(/career|company|site/i.test(s.said)) return "↗";
    return esc(initials(s.label));
  };
  h+=src.slice(0,7).map((s,i)=>{
    const p=Math.round(s.rate*100), best=i<2&&s.iv>0&&s.sent>=3;
    return '<div class="fn-src"><span class="mk">'+glyph(s)+'</span><span class="sn">'+esc(s.label)+
      '</span><span class="track"><span class="fill'+(best?" best":"")+'" style="width:'+Math.max(p,1)+
      '%;animation-delay:'+(1.3+i*.07).toFixed(2)+'s"></span></span><span class="sv"><b>'+p+
      '%</b>'+s.iv+' of '+s.sent+'</span></div>';
  }).join("");
  host.innerHTML=h;
}

function fnReplyDays(){
  const days=[]; let never=0;
  fnJobs().forEach(j=>{
    if(j.status==="ghosted"){ never++; return }
    const h=j.status_history||[], i=h.findIndex(e=>e.status==="applied");
    if(i<0||!h[i+1]||String(h[i+1].status).startsWith("ghosted")) return;
    const d=Math.round((new Date(String(h[i+1].at).slice(0,19))-new Date(String(h[i].at).slice(0,19)))/DAY);
    if(!isNaN(d)) days.push(Math.max(0,d));
  });
  return {days:days.sort((a,b)=>a-b),never};
}
function drawReplies(){
  const host=$("#fn-reply"), {days,never}=fnReplyDays();
  let h='<div class="fn-ch"><h2>How long they take to answer</h2></div>';
  if(!days.length){
    host.innerHTML=h+'<p class="fn-empty">No answers yet. Each one shows here as a dot, by how '+
      'many days it took.</p>';
    return;
  }
  const med=S.funnel.totals.median_reply_days??days[days.length>>1];
  const cap=Math.max(21,Math.min(42,days[days.length-1]));
  const by=new Map(); days.forEach(d=>{ const k=Math.min(d,cap); by.set(k,(by.get(k)||0)+1) });
  const tall=Math.max(...by.values()), step=Math.min(11,Math.floor(92/Math.max(1,tall)));
  let dots="";
  by.forEach((n,d)=>{ for(let j=0;j<n;j++) dots+='<i style="left:'+(d/cap*100).toFixed(2)+'%;bottom:'+
    (j*step)+'px;animation-delay:'+(1.4+d*.03+j*.05).toFixed(2)+'s"></i>' });
  let ghost="";
  for(let j=0;j<Math.min(never,30);j++) ghost+='<i class="g" style="left:'+((j%3)*11)+'px;bottom:'+
    (Math.floor(j/3)*11)+'px;animation-delay:'+(1.9+j*.03).toFixed(2)+'s"></i>';
  const p90=days[Math.min(days.length-1,Math.floor(days.length*.9))];
  const ticks=[0,7,14,21,28,35,42].filter(d=>d<=cap);
  h+='<div style="display:flex;align-items:baseline;gap:8px;margin:0 0 14px"><span class="fn-big" data-to="'+
    med+'" data-delay="1200">'+med+'</span><span style="font-size:13px;color:var(--t500)">day'+
    (med===1?"":"s")+', median, for the '+days.length+' that answered</span></div>'+
    '<div style="display:flex;gap:18px;align-items:flex-end"><div style="flex:1;min-width:0">'+
    '<div class="fn-dots" role="img" aria-label="Days to a first answer: '+esc(days.join(", "))+'">'+dots+
    '<span class="med" style="left:'+(Math.min(med,cap)/cap*100).toFixed(2)+'%"></span></div>'+
    '<div class="fn-dax">'+ticks.map(d=>'<span>'+(d?d/7+" wk":"0")+'</span>').join("")+'</div></div>'+
    (never?'<div style="width:34px;flex:none"><div class="fn-dots" style="height:'+
      Math.min(100,Math.ceil(Math.min(never,30)/3)*11)+'px;margin:0" role="img" aria-label="'+esc(t("{n} never answered",{n:never}))+
      '">'+ghost+'</div><div class="fn-dax" style="justify-content:center">'+t("never")+'</div></div>':'')+
    '</div><p>'+(days.length<5?"A few more answers and this will say when to stop waiting."
      :p90<=21?"9 in 10 answers came within "+p90+" days. Past that, follow up once, or let it go."
      :"Answers can take a while here: 1 in 10 came after "+p90+" days.")+'</p>';
  host.className="fn-card fn-reply";
  host.innerHTML=h;
  countUp(host,$("#fn-page").classList.contains("fx-in"));
}

/* Up to three readings, each shown only when the numbers support it, so the
   panel says less on a thin month rather than inventing something. */
const FN_ICON={
  up:'<path d="M4 17l6-6 4 4 6-7"/><path d="M14 8h6v6"/>',
  leak:'<path d="M12 3v12M6 11l6 6 6-6M5 21h14"/>',
  lost:'<path d="M6 6l12 12M18 6L6 18"/>',
  wait:'<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>'};
function readings(){
  const t=S.funnel.totals, c=S.funnel.by_status||{}, out=[];
  const src=fnSources().filter(s=>s.label!=="Not set");
  const best=src.find(s=>s.sent>=3&&s.iv>0);
  const worst=[...src].reverse().find(s=>s.sent>=5&&s!==best);
  if(best&&worst&&best.rate>=worst.rate*2)
    out.push(["up","<b>"+esc(best.label)+" works best.</b> "+best.iv+" of "+best.sent+
      " became interviews; "+esc(worst.label)+", "+worst.iv+" of "+worst.sent+"."]);
  const st=fnStages();
  let w=-1, low=101;
  st.forEach(([,n],i)=>{ if(!i) return; const d=st[i-1][1];
    if(d>=10){ const r=n/d*100; if(r<low){ low=r; w=i } } });
  if(w>0){
    const [,n]=st[w], d=st[w-1][1];
    const say=[null,
      ["Most applications go unanswered.",n+" of "+d+" sent have heard back."],
      ["Replies are mostly no.",n+" of "+d+" answers were an interview."],
      ["The offer stage leaks most.",n+" of "+d+" interviews became offers"+
        ((c.rejected_interviewing||0)?"; "+c.rejected_interviewing+" were turned down after interviewing.":".")],
      ["Offers are not turning into a yes.",n+" of "+d+" accepted."]][w];
    out.push(["leak","<b>"+say[0]+"</b> "+say[1]]);
  }
  const early=c.rejected||0, late=c.rejected_interviewing||0;
  if(early) out.push(["lost","<b>"+early+" rejection"+(early===1?"":"s")+" came before any interview"+
    (late?", "+late+" after":"")+".</b> Only the first kind points at the CV."]);
  const ghost=(c.ghosted||0)+(c.ghosted_interviewing||0);
  if(ghost) out.push(["wait","<b>"+ghost+" never answered,</b> "+Math.round(ghost/Math.max(1,t.applied)*100)+
    "% of everything sent."]);
  if(!out.length) out.push(["wait","<b>Not enough has happened yet</b> to read anything into."]);
  return out.slice(0,3);
}
function drawRates(){
  $("#fn-rates").innerHTML='<div class="fn-ch"><h2>What it says</h2></div>'+readings().map(([k,txt])=>
    '<div class="fn-ins"><span class="ic '+k+'"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" '+
    'stroke="currentColor" stroke-width="2.2" aria-hidden="true">'+FN_ICON[k]+'</svg></span><p>'+txt+
    '</p></div>').join("");
}
$("#ex-csv").onclick=()=>download("/api/jobs/export?format=csv"+tok(),"applications.csv");

/* Re-lay on resize. Debounced, because a sankey layout on every pixel of a
   window drag is wasted work. */
let sizeTimer=null;
window.addEventListener("resize",()=>{
  if(S.view==="funnel"&&S.funnel){
    clearTimeout(sizeTimer); sizeTimer=setTimeout(()=>drawSankey("still",false),140);
  }
});

