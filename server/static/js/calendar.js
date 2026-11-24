/* Next actions, calendar overview, month view, and week view. */
/* =========================================================================
   Calendar

   Interviews, follow-ups and what happened, by date. Nothing here is new data:
   the interview time and its zone, the follow-up date, and the status history
   the funnel is drawn from. Days are days where you are (userTz).
   ========================================================================= */
S.calView="overview"; S.calAnchor=null; S.calTick=null; S.calPop=null;
/* Over, and still in play. Not written out here: the lifecycle is one table
   on the server (jobs.py, LIFECYCLE) and loadJobs fills these from it, so
   they are empty only while there are no applications to ask about. */
const DEAD_ST=new Set(), LIVE_ST=new Set();
const refill=(set,list)=>{ set.clear(); (list||[]).forEach(x=>set.add(x)) };
function adoptLifecycle(d){
  const lc=d.lifecycle||{};
  S.next=lc.next||{};
  refill(DEAD_ST,lc.terminal); refill(LIVE_ST,lc.live); refill(IV_STATUSES,(d.nodes||{}).interview_s);
}
/* The status menu of one application: where it is, then the usual moves
   from there, then everything else, because a record can always be put
   right. The same moves the card in the chat offers. */
function statusOptions(cur){
  const opt=s=>'<option value="'+s+'"'+(s===cur?" selected":"")+'>'+esc(prettyStatus(s))+'</option>';
  const next=((S.next||{})[cur]||[]).filter(s=>S.statuses.includes(s));
  if(!next.length) return S.statuses.map(opt).join("");
  const rest=S.statuses.filter(s=>s!==cur&&!next.includes(s));
  return (S.statuses.includes(cur)?opt(cur):"")+
    '<optgroup label="'+esc(t("Next step"))+'">'+next.map(opt).join("")+'</optgroup>'+
    '<optgroup label="'+esc(t("Other statuses"))+'">'+rest.map(opt).join("")+'</optgroup>';
}
/* A moment as its day where you are, "2026-09-25". */
function dayIn(date,tz){
  const p=DTF("en-CA",{timeZone:tz||userTz(),year:"numeric",month:"2-digit",day:"2-digit"})
    .formatToParts(date);
  const g=k=>p.find(x=>x.type===k).value;
  return g("year")+"-"+g("month")+"-"+g("day");
}
/* The wall-clock time a moment shows in a zone, "2026-09-25T10:00". */
function wallIn(date,tz){
  const p=DTF("en-CA",{timeZone:tz,hourCycle:"h23",year:"numeric",month:"2-digit",day:"2-digit",
    hour:"2-digit",minute:"2-digit"}).formatToParts(date);
  const g=k=>p.find(x=>x.type===k).value;
  return g("year")+"-"+g("month")+"-"+g("day")+"T"+g("hour")+":"+g("minute");
}
const hmIn=(date,tz)=>wallIn(date,tz).slice(11);
/* The same, as it is read: in the clock you chose (Settings › Language & region). */
const hmShow=(date,tz)=>fmtTime(hmIn(date,tz));
const todayKey=()=>dayIn(new Date());
const keyDate=k=>{ const [y,m,d]=k.split("-").map(Number); return new Date(Date.UTC(y,m-1,d,12)) };
const addDays=(k,n)=>{ const d=keyDate(k); d.setUTCDate(d.getUTCDate()+n); return d.toISOString().slice(0,10) };
const dayDiff=(a,b)=>Math.round((keyDate(b)-keyDate(a))/DAY);
const monday=k=>{ const w=(keyDate(k).getUTCDay()+6)%7; return addDays(k,-w) };
const fmtKey=(k,o)=>{ try{ return DTF(uiLocale(),Object.assign({timeZone:"UTC"},o)).format(keyDate(k)) }
  catch(e){ return k } };
const otherTz=j=>{ const at=interviewMoment(j); return j.interview_tz&&at&&tzOffset(j.interview_tz,at)!==tzOffset(userTz(),at) };
const GLOBE='<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '+
  'aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z"/></svg>';

/* ---- Next actions, above the applications list -----------------------------
   What to do now, one row per thing, most urgent first, each with the button
   that does it: an interview coming, a follow-up late, an offer to answer, a
   thank-you after yesterday's interview, a draft left sitting. Beside it,
   this week in seven days and three counts. It shows on the whole list only. */
/* An interview today or in the next seven days, counted by day: the rule
   the server's "Interview in 7 days" uses, so the two numbers agree. */
function ivSoon(j){
  const at=interviewMoment(j); if(!at) return false;
  const d=dayDiff(todayKey(),dayIn(at)); return d>=0&&d<=7;
}
/* A status set in one click says so, and can be taken back: the row it came
   from disappears, and nothing else told you what just happened. */
async function statusWithUndo(j,status){
  const was=j.status;
  await saveJob(j.id,{status});
  toast(t("{co} is now {s}",{co:j.company,s:prettyStatus(status)}),false,
    {label:t("Undo"),fn:()=>saveJob(j.id,{status:was})});
}
function nextActions(){
  const now=new Date(), today=todayKey(), out=[];
  const dshort=k=>fmtKey(k,{day:"numeric",month:"short"});
  /* Who an email goes to: the hiring manager or the recruiter first, the
     person who referred you last. */
  const writer=j=>{ const ps=(j.people||[]).filter(p=>p.email);
    return ps.find(p=>p.role==="Hiring manager")||ps.find(p=>p.role==="Recruiter")||ps.find(p=>p.role!=="Referral")||ps[0]||null };
  const lastWrote=j=>(j.people||[]).filter(p=>p.last).sort((a,b)=>b.last.localeCompare(a.last))[0];
  const when=at=>{
    const mins=Math.round((at-now)/6e4), k=dayIn(at), days=dayDiff(today,k), hm=hmShow(at,userTz());
    if(mins<180) return t("in {h} h {m} min",{h:Math.floor(mins/60),m:mins%60});
    if(days===0) return t("today {t}",{t:hm});
    if(days===1) return t("tomorrow {t}",{t:hm});
    return fmtKey(k,{weekday:"short"})+" "+hm+" · "+t("in {n} days",{n:days});
  };
  (S.jobs||[]).forEach(j=>{
    /* A CV or letter an AI client changed and you have not looked at yet:
       most likely the one you are about to send. */
    for(const [path,kind] of [[j.cv_path,"cv"],[j.letter_path,"letter"]]){
      const d=path&&docByPath(path), r=d&&d.review; if(!r) continue;
      out.push({j,rank:1,at:-(r.at||0),dot:"live",what:t("Review what {who} changed",{who:whoLabel(r)}),
        why:kind==="cv"?t("The CV"):t("The cover letter"),when:t("{t} ago",{t:ago(r.at*1000)}),
        act:t("Review"),run:()=>openForReview(path)});
    }
    if(DEAD_ST.has(j.status)) return;
    const rs=j.rounds||[], at=interviewMoment(j);
    /* An interview this week. */
    if(at&&at>now&&ivSoon(j)){
      const n=rs.findIndex(r=>!r.outcome&&r.at), r=rs[n]||{};
      const why=[rs.length>1&&n>=0?t("Round {n} of {m}",{n:n+1,m:rs.length}):"",r.kind?t(r.kind):"",
        r.with?t("with {n}",{n:r.with}):""].filter(Boolean).join(" · ");
      out.push({j,rank:at-now<3*864e5?0:5,at:+at,dot:"live",what:t("Interview"),why,when:when(at),
        hot:at-now<3*36e5,act:t("Prepare"),run:()=>selectJob(j.id)});
    }
    /* An interview that has happened and has no outcome yet. */
    const past=rs.filter(r=>r.at&&!r.outcome).map(r=>({r,at:interviewMoment({interview_at:r.at+":00",interview_tz:r.tz||null})}))
      .filter(x=>x.at&&x.at<now).pop();
    if(past&&j.status==="interviewing"){
      const ago=dayDiff(dayIn(past.at),today), p=past.r.with&&(j.people||[]).find(x=>x.name===past.r.with&&x.email)||writer(j);
      const thanked=p&&p.last&&p.last>=dayIn(past.at);
      if(ago<=2&&p&&!thanked)
        out.push({j,rank:2,at:+past.at,dot:"live",what:t("Say thank you"),
          why:[past.r.kind?t(past.r.kind):t("Interview"),past.r.with?t("with {n}",{n:past.r.with}):"",
            ago===0?t("today"):ago===1?t("yesterday"):t("{n} days ago",{n:ago})].filter(Boolean).join(" · "),
          when:t("today"),act:t("Write thank-you"),run:()=>draftSheet(j,p,"thanks")});
      else if(ago<=21)
        out.push({j,rank:4,at:+past.at,dot:"live",what:t("How did it go?"),
          why:[past.r.kind?t(past.r.kind):t("Interview"),dshort(dayIn(past.at))].join(" · "),
          when:ago===0?t("today"):t("{n} days ago",{n:ago}),act:t("Open"),run:()=>selectJob(j.id)});
    }
    /* A follow-up late or due today. */
    if(j.followup_date&&j.followup_date<=today){
      const late=dayDiff(j.followup_date,today), lw=lastWrote(j), p=writer(j), ap=appliedAt(j);
      const quiet=dayDiff(String(lw?lw.last:ap||today).slice(0,10),today);
      /* Six weeks of silence on an application: chasing it again is rarely
         worth it, and calling it ghosted keeps the list honest. */
      if(j.status==="applied"&&quiet>42) out.push({j,rank:late?1:3,at:-late,dot:"late",what:t("Follow up or let it go"),
        why:lw?t("Last wrote to {n} on {d}",{n:lw.name||lw.email,d:dshort(lw.last)}):t("Sent {d}, no reply since",{d:dshort(String(ap).slice(0,10))}),
        when:t("{n} weeks quiet",{n:Math.floor(quiet/7)}),
        acts:[[t("Mark ghosted"),()=>statusWithUndo(j,"ghosted")],[t("Write follow-up"),()=>draftSheet(j,p,"followup")]]});
      else out.push({j,rank:late?1:3,at:-late,dot:late?"late":"waiting",what:t("Follow up"),
        why:lw?t("Last wrote to {n} on {d}",{n:lw.name||lw.email,d:dshort(lw.last)})
          :ap?t("Sent {d}, no reply since",{d:dshort(String(ap).slice(0,10))}):"",
        when:late?t("{n} day(s) overdue",{n:late}):t("today"),hot:!!late,
        act:t("Write follow-up"),run:()=>draftSheet(j,p,"followup")});
    }
    /* An offer to answer. */
    if(j.status==="offer"){
      const h=(j.status_history||[]).filter(x=>x.status==="offer").pop(), k=h?dayIn(new Date(String(h.at).slice(0,19))):today;
      const p=writer(j), days=dayDiff(k,today);
      /* A week is a long time to leave an offer unanswered; after a month it
         was answered and the list was not told, so it asks which way. */
      if(days>30) out.push({j,rank:2,at:-days,dot:"offer",what:t("Answer the offer"),
        why:t("Offer since {d}. Did you take it?",{d:dshort(k)}),when:t("{n} day(s) ago",{n:days}),
        acts:[[t("Accepted"),()=>statusWithUndo(j,"accepted")],[t("Declined by me"),()=>statusWithUndo(j,"refused")]]});
      else out.push({j,rank:2,at:-days,dot:"offer",what:t("Answer the offer"),why:t("Offer since {d}",{d:dshort(k)}),
        when:days?t("{n} day(s) ago",{n:days}):t("today"),hot:days>7,act:t("Reply to the offer"),run:()=>draftSheet(j,p,"offer")});
    }
    /* A draft left for more than three days; after a month it is not a
       plan any more, and it stays in the list below. */
    if(j.status==="pending"){
      const days=dayDiff(String(j.created_at||"").slice(0,10)||today,today);
      if(days>3&&days<=30) out.push({j,rank:6,at:-days,dot:"draft",what:t("Send it"),
        why:j.cv_path?t("CV ready"):t("No tailored CV yet"),when:t("draft for {n} days",{n:days}),
        act:t("Open"),run:()=>selectJob(j.id)});
    }
  });
  return out.sort((a,b)=>a.rank-b.rank||a.at-b.at);
}
function drawNextUp(){
  drawScanBar();
  const el=$("#nextup"); if(!el) return;
  const f=S.jfilter||{kind:"all"}, q=($("#jobq").value||"").trim();
  if(!S.jready||f.kind!=="all"||q||S.jsel||!(S.jobs||[]).length){ el.hidden=true; return }
  const acts=nextActions(), today=todayKey(), ev=calEvents(), mon=monday(today);
  const shown=S.naAll?acts:acts.slice(0,5), more=acts.length-shown.length;
  const rows=shown.map((a,i)=>'<div class="na-row">'+companyMark(a.j)+
      '<span class="dot '+a.dot+'" aria-hidden="true"></span>'+
      '<span class="na-tx"><b>'+esc(a.what)+' <span>· '+esc(a.j.company)+', '+esc(a.j.title)+'</span></b>'+
        (a.why?'<small>'+esc(a.why)+'</small>':'')+'</span>'+
      '<span class="na-when'+(a.hot?" hot":"")+'">'+esc(a.when)+'</span>'+
      (a.acts?'<span class="na-acts">'+a.acts.map((x,k)=>'<button type="button" class="obtn" data-na="'+i+'" data-k="'+k+'">'+esc(x[0])+'</button>').join("")+'</span>'
        :'<button type="button" class="obtn" data-na="'+i+'">'+esc(a.act)+'</button>')+'</div>').join("");
  const days=Array.from({length:7},(_,i)=>{
    const k=addDays(mon,i), iv=ev.filter(e=>e.kind==="iv"&&e.day===k), fu=ev.filter(e=>e.kind==="fu"&&e.day===k), lt=fu.some(e=>e.late);
    const tip=[iv.length?t("Interview")+" · "+iv.map(e=>e.job.company).join(", "):"",
      fu.length?t("Follow up")+" · "+fu.map(e=>e.job.company).join(", "):""].filter(Boolean).join("\n");
    return '<button data-nu-day="'+k+'"'+(k===today?' class="today"':'')+' title="'+esc(tip||fmtKey(k,{weekday:"long",day:"numeric",month:"long"}))+'">'+
      '<small>'+esc(fmtKey(k,{weekday:"short"}))+'</small>'+Number(k.slice(8))+
      '<span class="mk">'+(iv.length?'<span class="d"></span>':'')+(fu.length?'<span class="r'+(lt?" late":"")+'"></span>':'')+'</span></button>';
  }).join("");
  const end=addDays(mon,6), inWeek=k=>k>=mon&&k<=end;
  const ivWeek=(S.jobs||[]).filter(j=>!DEAD_ST.has(j.status)&&ivSoon(j)).length;
  const sentWeek=(S.jobs||[]).filter(j=>(j.status_history||[]).some(h=>h.status==="applied"&&inWeek(String(h.at).slice(0,10)))).length;
  const dueWeek=(S.jobs||[]).filter(j=>j.followup_date&&!DEAD_ST.has(j.status)&&inWeek(j.followup_date)).length;
  const stat=(label,n)=>'<span class="na-stat"><span>'+esc(label)+'</span><b>'+n+'</b></span>';
  el.className="na";
  el.innerHTML='<div class="na-main"><div class="na-hd"><h2>'+esc(t("Next actions"))+'</h2><span>'+
      esc(acts.length?t("{n} thing(s) to do, most urgent first",{n:acts.length}):t("Nothing to do right now"))+'</span></div>'+
      (rows||'<p class="na-none">'+esc(t("Nothing is late or coming up."))+'</p>')+
      (more>0||S.naAll&&acts.length>5?'<div class="na-more"><button type="button" class="linkbtn" data-na-more>'+
        esc(more>0?t("Show {n} more",{n:more}):t("Show fewer"))+'</button></div>':'')+'</div>'+
    '<div class="na-side"><div class="hd">'+esc(t("This week"))+'<button data-nu-cal>'+esc(t("Open calendar"))+' →</button></div>'+
      '<div class="days">'+days+'</div>'+
      '<div class="na-stats">'+stat(t("Interviews in the next 7 days"),ivWeek)+stat(t("Sent this week"),sentWeek)+stat(t("Follow-ups this week"),dueWeek)+'</div></div>';
  el.hidden=false;
  $$("#nextup [data-na]").forEach(b=>b.onclick=()=>{ const a=shown[+b.dataset.na];
    b.dataset.k!=null&&a.acts?a.acts[+b.dataset.k][1]():a.run() });
  const mb=$("#nextup [data-na-more]"); if(mb) mb.onclick=()=>{ S.naAll=!S.naAll; drawNextUp() };
  const cal=$("#nextup [data-nu-cal]"); if(cal) cal.onclick=()=>{ S.calView="overview"; setView("cal") };
  $$("#nextup [data-nu-day]").forEach(b=>b.onclick=()=>{ S.calView="week"; S.calAnchor=b.dataset.nuDay; setView("cal") });
}

/* Every dated thing, in one list: {kind, day, job, at?, late?}. */
function calEvents(){
  const out=[], today=todayKey();
  (S.jobs||[]).forEach(j=>{
    const at=interviewMoment(j);
    if(at) out.push({kind:"iv",day:dayIn(at),at,job:j});
    /* The rounds already held stay on the calendar, where they happened. */
    (j.rounds||[]).forEach(r=>{ if(!r.at) return;
      const m=interviewMoment({interview_at:r.at+":00",interview_tz:r.tz||null});
      if(m&&m<new Date()&&!(at&&+m===+at)) out.push({kind:"iv",day:dayIn(m),at:m,job:j,past:true,round:r});
    });
    if(j.followup_date&&!DEAD_ST.has(j.status)){
      const d=String(j.followup_date).slice(0,10);
      out.push({kind:"fu",day:d,job:j,late:d<today});
    }
    let seenIv=false;
    (j.status_history||[]).forEach((h,i)=>{
      if(!h.at) return;
      const d=dayIn(new Date(String(h.at).slice(0,19)));
      const st=h.status;
      if(st==="applied") out.push({kind:"sent",day:d,job:j});
      else if(st==="interviewing"&&!seenIv){ seenIv=true; out.push({kind:"rp",day:d,job:j}) }
      else if(st==="offer") out.push({kind:"of",day:d,job:j});
      else if(DEAD_ST.has(st)&&st!=="accepted") out.push({kind:"closed",day:d,job:j});
      else if(st==="accepted") out.push({kind:"of",day:d,job:j});
    });
  });
  return out;
}

function openCalendar(){
  if(!S.calAnchor) S.calAnchor=todayKey();
  const go=()=>{ drawCalendar(true) };
  if(!S.jready) loadJobs(true).then(go); else go();
}
function drawCalendar(animate){
  const page=$("#cal-page"), v=S.calView;
  $$("#cal-views button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.cv===v)));
  $("#cal-nav").hidden=v==="overview";
  const ev=calEvents(), today=todayKey();
  /* The same rule, and the same words, as Applications: interviews today
     and in the next seven days. This one counted to Sunday, so the two
     screens gave two different numbers for "this week". */
  const ivs=(S.jobs||[]).filter(j=>!DEAD_ST.has(j.status)&&ivSoon(j)).length;
  const late=ev.filter(e=>e.kind==="fu"&&e.late).length;
  /* The overdue count is a way in: it opens those applications. */
  $("#cal-sub").innerHTML=[ivs?esc(t(ivs===1?"1 interview in the next 7 days":"{n} interviews in the next 7 days",{n:ivs})):"",
    late?'<button type="button" class="linkbtn" id="cal-late">'+esc(t(late===1?"1 follow-up overdue":"{n} follow-ups overdue",{n:late}))+'</button>':""]
    .filter(Boolean).join(" · ");
  const cl=$("#cal-late"); if(cl) cl.onclick=()=>{ S.jfilter={kind:"alert",value:"followup_due"}; S.fnode=null; setView("jobs"); drawJobs() };
  clearInterval(S.calTick);
  if(animate&&!reduceMotion()){ page.classList.remove("cal-in"); void page.offsetWidth; page.classList.add("cal-in");
    setTimeout(()=>page.classList.remove("cal-in"),3200) }
  if(v==="overview") drawCalOverview(ev);
  else if(v==="month") drawCalMonth(ev);
  else drawCalWeek(ev);
}
$$("#cal-views button").forEach(b=>b.onclick=()=>{ S.calView=b.dataset.cv; S.calPop=null; drawCalendar(true) });
$$("#cal-nav [data-step]").forEach(b=>b.onclick=()=>{
  const n=+b.dataset.step;
  if(S.calView==="week") S.calAnchor=addDays(S.calAnchor,7*n);
  else{ const d=keyDate(S.calAnchor); d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth()+n); S.calAnchor=d.toISOString().slice(0,10) }
  S.calPop=null; drawCalendar(false);
});
$("#cal-today").onclick=()=>{ S.calAnchor=todayKey(); S.calPop=null; drawCalendar(false) };
$("#cal-ics").onclick=()=>download("/api/calendar.ics"+tok(),"calendar.ics");
const openJob=id=>{ S.jfilter={kind:"all",value:""}; setView("jobs"); selectJob(id) };

/* ---- overview ------------------------------------------------------------ */
function clockSVG(date,tz,label,delay){
  const [h,m]=hmIn(date,tz).split(":").map(Number);
  const ha=((h%12)+m/60)*30, ma=m*6;
  const ticks=Array.from({length:12},(_,k)=>'<line class="tk'+(k%3?"":" big")+'" x1="50" y1="8" x2="50" y2="'+
    (k%3?11:14)+'" stroke-width="'+(k%3?1.2:2)+'" transform="rotate('+k*30+' 50 50)"/>').join("");
  return '<div class="cal-clock"><svg width="92" height="92" viewBox="0 0 100 100" aria-hidden="true">'+
    '<circle class="face" cx="50" cy="50" r="46" stroke-width="2"/>'+ticks+
    '<line class="cal-hand hh" x1="50" y1="50" x2="50" y2="27" stroke-width="4" stroke-linecap="round" style="--a:'+ha+
      'deg;animation-delay:'+delay+'s"/>'+
    '<line class="cal-hand mh" x1="50" y1="50" x2="50" y2="15" stroke-width="2.5" stroke-linecap="round" style="--a:'+ma+
      'deg;animation-delay:'+(delay+.1)+'s"/><circle class="pin" cx="50" cy="50" r="3.5"/></svg>'+
    '<b>'+hmShow(date,tz)+'</b><span>'+esc(label)+'</span></div>';
}
function drawCalOverview(ev){
  const now=new Date(), today=todayKey();
  const next=ev.filter(e=>e.kind==="iv"&&e.at>now).sort((a,b)=>a.at-b.at)[0];
  let hero;
  if(next){
    const j=next.job, mine=userTz(), theirs=j.interview_tz, two=otherTz(j);
    const words=(String(j.description||"").match(/\S+/g)||[]).length;
    const diffH=two?Math.round((tzOffset(mine,next.at)-tzOffset(theirs,next.at))/60):0;
    const cvName=j.cv_path?j.cv_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
    hero='<section class="cal-hero cal-r"><div>'+
      '<div class="cal-next"><span class="tag"><i></i>'+t("NEXT UP")+'</span><span>'+t("Interview")+'</span></div>'+
      '<div class="cal-who">'+companyMark(j)+'<div><b>'+esc(j.company)+'</b><span>'+esc(j.title)+
        (j.location?' · '+esc(j.location):'')+'</span></div></div>'+
      '<div class="cal-cd" id="cal-cd"></div>'+
      '<div class="cal-checks">'+
        /* They will ask about what is on the CV they have: re-read it. */
        '<span class="cal-check"><i class="'+(cvName?"ok":"no")+'">'+(cvName?"✓":"")+'</i>'+
          (cvName?t("Re-read the CV you sent")+' · <button type="button" class="linkbtn" data-open-doc="'+esc(j.cv_path)+'">'+esc(cvName)+'</button>'
            :t("Which CV did you send?")+' · <button type="button" class="linkbtn" data-open-job="'+esc(j.id)+'">'+t("Link it")+'</button>')+'</span>'+
        '<span class="cal-check"><i class="'+(words?"ok":"no")+'">'+(words?"✓":"")+'</i>'+
          (words?t("Posting saved"):t("Posting not saved"))+'</span>'+
        '<span class="cal-check"><i class="'+(j.notes?"ok":"no")+'">'+(j.notes?"✓":"")+'</i>'+
          (j.notes?t("Notes written"):t("Notes for this round"))+'</span></div>'+
      '<div class="cal-acts"><button class="pbtn" data-open-job="'+esc(j.id)+'">'+t("Prepare")+'</button>'+
        '<button class="obtn" data-ics="'+esc(j.id)+'">'+t("Add to my calendar")+'</button></div></div>'+
      '<div class="cal-clocks"><small class="cap">'+esc(t("At the interview"))+'</small><div class="row">'+clockSVG(next.at,mine,t("Your time")+" · "+tzCity(mine),.5)+
        (two?clockSVG(next.at,theirs,t("In {city}",{city:tzCity(theirs)}),.7):'')+'</div>'+
        (two?'<small>'+esc(diffH>0?t("{city} is {n} h behind you",{city:tzCity(theirs),n:diffH})
          :t("{city} is {n} h ahead of you",{city:tzCity(theirs),n:-diffH}))+'</small>':'')+'</div></section>';
  }else{
    const fu=ev.filter(e=>e.kind==="fu"&&!e.late).sort((a,b)=>a.day.localeCompare(b.day))[0];
    hero='<section class="cal-hero cal-r"><div class="cal-empty"><div class="cal-next"><span class="tag"><i></i>'+
      t("NEXT UP")+'</span></div><b>'+t("No interview scheduled")+'</b><p>'+
      (fu?esc(t("Next: follow up with {co}, {day}.",{co:fu.job.company,day:fmtKey(fu.day,{weekday:"long",day:"numeric",month:"long"})}))
        :t("When an application gets an interview time, it counts down here, in your time and theirs."))+
      '</p></div><div></div></section>';
  }
  $("#cal-body").innerHTML='<div class="cal-top">'+hero+calMini(ev)+'</div>'+calJourneys(ev);
  wireCal();
  if(next){
    const tick=()=>{
      const el=$("#cal-cd"); if(!el){ clearInterval(S.calTick); return }
      const left=Math.max(0,next.at-new Date());
      const p=n=>String(n).padStart(2,"0");
      const d=Math.floor(left/864e5), h=Math.floor(left/36e5)%24, m=Math.floor(left/6e4)%60, s=Math.floor(left/1e3)%60;
      el.innerHTML=(d?'<div><div class="n">'+d+'</div><div class="u">'+t(d===1?"day":"days")+'</div></div>':'')+
        '<div><div class="n">'+p(h)+'</div><div class="u">'+t("hours")+'</div></div>'+
        '<div><div class="n">'+p(m)+'</div><div class="u">'+t("min")+'</div></div>'+
        '<div class="when"><b>'+esc(fmtKey(next.day,{weekday:"long",day:"numeric",month:"long"}))+'</b><br>'+
        esc(hmShow(next.at,userTz()))+' '+t("your time")+'</div>';
    };
    tick(); S.calTick=setInterval(tick,15000);
  }
}
function calMini(ev){
  const today=todayKey(), first=today.slice(0,8)+"01", start=monday(first);
  const month=first.slice(0,7);
  const busy={}, iv=new Set();
  /* What you had to do that day: an interview counts double, a rejection
     or a status someone else changed not at all. */
  const W={iv:2,fu:1,sent:1,of:1};
  const cnt={};
  ev.forEach(e=>{ if(W[e.kind]){ busy[e.day]=(busy[e.day]||0)+W[e.kind]; cnt[e.day]=(cnt[e.day]||0)+1 } if(e.kind==="iv") iv.add(e.day) });
  let cells="";
  for(let i=0;i<42;i++){
    const k=addDays(start,i); if(i>=35&&k.slice(0,7)!==month) break;
    const inm=k.slice(0,7)===month, n=busy[k]||0, lv=!inm?"":n>=4?" l3":n>=2?" l2":n?" l1":"";
    cells+='<button class="cal-md'+(inm?"":" out")+lv+(k===today?" today":"")+'" data-day="'+k+'" style="animation-delay:'+
      (.4+i*.018).toFixed(3)+'s"'+(inm?'':' tabindex="-1"')+' aria-label="'+esc(fmtKey(k,{day:"numeric",month:"long"}))+
      (iv.has(k)?', '+t("Interview"):'')+(inm&&n?', '+t("{n} thing(s) planned",{n:cnt[k]}):'')+'">'+Number(k.slice(8))+(iv.has(k)?'<i></i>':'')+'</button>';
  }
  const dows=Array.from({length:7},(_,i)=>'<span class="dw">'+esc(fmtKey(addDays("2026-09-21",i),{weekday:"narrow"}))+'</span>').join("");
  const acc=["var(--bd-inner)","color-mix(in srgb,var(--acc) 22%,var(--field))","color-mix(in srgb,var(--acc) 45%,var(--field))",
    "color-mix(in srgb,var(--acc) 75%,var(--field))"];
  return '<section class="cal-card cal-mini cal-r" style="animation-delay:.1s"><div class="cal-ch"><h2>'+
    esc(fmtKey(first,{month:"long"}))+'</h2><span>'+t("how full each day is, past and planned")+'</span></div>'+
    '<div class="cal-mgrid">'+dows+cells+'</div><div class="cal-legend"><span style="display:flex;align-items:center;gap:4px">'+
    t("Quiet")+'<span class="ramp">'+acc.map(c=>'<i style="background:'+c+'"></i>').join("")+'</span>'+t("Busy")+
    '</span><span style="display:flex;align-items:center;gap:6px"><i class="dia"></i>'+t("Interview")+'</span></div></section>';
}
/* Every live application as a lane across eight weeks, three of them ahead: how long it has been
   waiting, when it moved to interviews or an offer, and what is ahead. */
function calJourneys(ev){
  /* Four weeks back and four ahead: today sits in the middle, so what is
     coming has room for its label on the right of its mark. */
  const today=todayKey(), start=addDays(monday(today),-28), days=56, end=addDays(start,days-1);
  const pct=k=>Math.max(0,Math.min(100,(dayDiff(start,k)+.5)/days*100));
  /* Labels only turn to the left of their mark at the very edge, where they
     would otherwise run off the card. */
  const labAt=(p,gap)=>p>86?"right:calc("+(100-p)+"% + "+gap+"px)":"left:calc("+p+"% + "+gap+"px)";
  const recent=j=>{ const h=(j.status_history||[]).slice(-1)[0]; return h&&dayIn(new Date(String(h.at).slice(0,19)))>=start };
  const rank=j=>{ const at=interviewMoment(j); if(at&&at>new Date()) return 0;
    return {offer:1,interviewing:2,applied:3}[j.status]||4 };
  const jobs=(S.jobs||[]).filter(j=>LIVE_ST.has(j.status)||(DEAD_ST.has(j.status)&&recent(j)))
    .sort((a,b)=>rank(a)-rank(b)||(interviewMoment(a)||0)-(interviewMoment(b)||0)||
      String(b.updated_at).localeCompare(String(a.updated_at)));
  const shown=jobs.slice(0,14);
  let ticks="";
  for(let w=0;w<=8;w++){ const k=addDays(start,7*w); if(Math.abs(dayDiff(k,today))<6||dayDiff(start,k)>=days) continue;
    ticks+='<span class="jr-tick" style="left:'+pct(k)+'%">'+esc(fmtKey(k,{day:"numeric",month:"short"}))+'</span>' }
  let wk=""; for(let i=5;i<days;i+=7) wk+='<span class="jr-wkend" style="left:'+(i/days*100)+'%;width:'+(2/days*100)+'%"></span>';
  const lanes=shown.map((j,i)=>{
    const h=(j.status_history||[]).filter(x=>x.at).map(x=>({st:x.status,day:dayIn(new Date(String(x.at).slice(0,19)))}));
    const stage=st=>st==="applied"?"wait":st==="interviewing"?"live":(st==="offer"||st==="accepted")?"offer":null;
    let segs="", marks="";
    h.forEach((x,n)=>{
      const sg=stage(x.st); if(!sg) return;
      const to=n+1<h.length?h[n+1].day:(LIVE_ST.has(j.status)?today:x.day);
      if(to<start||x.day>end) return;
      const a=pct(x.day<start?start:x.day), b=pct(to>end?end:to)+(to===today?.5/days*100:0);
      segs+='<span class="jr-seg '+sg+'" style="left:'+a+'%;width:'+Math.max(.6,b-a)+'%;animation-delay:'+
        (.5+i*.06+n*.1).toFixed(2)+'s"></span>';
    });
    const first=h.find(x=>x.st==="interviewing"); if(first&&first.day>=start) marks+='<span class="jr-dot rp" style="left:'+pct(first.day)+'%"></span>';
    const of=h.find(x=>x.st==="offer"); if(of&&of.day>=start) marks+='<span class="jr-dot of" style="left:'+pct(of.day)+'%"></span>';
    const aheadKeys=[];
    const at=interviewMoment(j);
    if(at&&at>new Date()&&dayIn(at)<=end){
      const k=dayIn(at); aheadKeys.push(k);
      const lab=fmtKey(k,{weekday:"short"})+" "+hmShow(at,userTz())+(otherTz(j)?" · "+hmShow(at,j.interview_tz)+" "+tzCity(j.interview_tz):"");
      marks+='<span class="jr-iv" style="left:'+pct(k)+'%;animation-delay:'+(1.5+i*.07).toFixed(2)+'s"></span>'+
        '<span class="jr-lab iv" style="'+labAt(pct(k),14)+';animation-delay:'+(1.7+i*.07).toFixed(2)+'s">'+esc(lab)+'</span>';
    }
    if(j.followup_date&&!DEAD_ST.has(j.status)){
      const k=String(j.followup_date).slice(0,10), late=k<today;
      if(k>=start&&k<=end&&!aheadKeys.length){
        if(!late) aheadKeys.push(k);
        marks+='<span class="jr-ring'+(late?" late":"")+'" style="left:'+pct(k)+'%;animation-delay:'+(1.5+i*.07).toFixed(2)+'s"></span>'+
          '<span class="jr-lab'+(late?" late":"")+'" style="'+labAt(pct(k),12)+';animation-delay:'+(1.7+i*.07).toFixed(2)+'s">'+
          esc(late?t("Follow-up overdue"):k===addDays(today,1)?t("Follow up tomorrow"):t("Follow up")+" · "+fmtKey(k,{day:"numeric",month:"short"}))+'</span>';
      }
    }
    const closed=DEAD_ST.has(j.status)&&j.status!=="accepted";
    if(closed){ const k=h.slice(-1)[0].day; marks+='<span class="jr-x" style="left:'+pct(k)+'%">×</span>'+
      '<span class="jr-lab x" style="left:calc('+pct(k)+'% + 10px)">'+esc(prettyStatus(j.status))+'</span>' }
    if(aheadKeys.length&&!closed){ const far=aheadKeys.sort().slice(-1)[0];
      segs+='<span class="jr-dash" style="left:'+(pct(today)+.5/days*100)+'%;width:'+Math.max(0,pct(far)-pct(today)-.5/days*100)+'%"></span>' }
    return '<div class="jr-lane'+(closed?" closed":"")+' cal-r" role="button" tabindex="0" data-open-job="'+esc(j.id)+'" style="animation-delay:'+(.3+i*.05).toFixed(2)+
      's"><div class="jr-who">'+companyMark(j)+'<span style="min-width:0"><b>'+esc(j.company)+'</b><small>'+esc(j.title)+
      (j.location?' · '+esc(j.location):'')+'</small></span></div><div class="jr-track">'+segs+marks+'</div></div>';
  }).join("");
  return '<section class="cal-card cal-jr cal-r" style="--jr-lx:236px;animation-delay:.2s"><div class="cal-ch" style="align-items:center">'+
    '<h2 style="font-size:15px">'+t("Journeys")+'</h2><span>'+t("each application from sent to where it is now, and what is ahead")+'</span>'+
    '<div class="jr-legend"><span><i class="bar" style="background:color-mix(in srgb,var(--fn-wait) 40%,transparent)"></i>'+t("Awaiting reply")+'</span>'+
    '<span><i class="bar" style="background:color-mix(in srgb,var(--fn-positive) 55%,transparent)"></i>'+t("Interviewing")+'</span>'+
    '<span><i class="bar" style="background:color-mix(in srgb,var(--fn-offer) 55%,transparent)"></i>'+t("Offer")+'</span>'+
    '<span><i style="width:9px;height:9px;border-radius:2px;background:var(--fn-positive);transform:rotate(45deg)"></i>'+t("Interview")+'</span>'+
    '<span><i style="width:10px;height:10px;border-radius:50%;box-shadow:inset 0 0 0 2px var(--fn-wait)"></i>'+t("Follow-up")+'</span></div></div>'+
    (shown.length?'<div class="jr-axis">'+ticks+'</div><div class="jr-body"><div class="jr-bg">'+wk+
      '<span class="jr-now" data-label="'+esc(t("Today"))+'" style="left:'+pct(today)+'%"></span></div>'+lanes+'</div>'+
      (jobs.length>shown.length?'<div class="jr-more">'+esc(t("{n} more in Applications",{n:jobs.length-shown.length}))+'</div>':'')
      :'<p class="cal-none">'+t("Nothing in play in these seven weeks.")+'</p>')+'</section>';
}

/* ---- month --------------------------------------------------------------- */
/* A word that tells one application apart from the others open at the same
   company ("Backend", "Platform"), or nothing when the company is there once. */
const SENIORITY=new Set(["senior","staff","lead","junior","principal","sr","jr","head","of","and","&","-"]);
function roleTag(j){
  const same=(S.jobs||[]).filter(x=>x.id!==j.id&&x.company===j.company&&!DEAD_ST.has(x.status));
  if(!same.length) return "";
  const other=new Set(same.flatMap(x=>String(x.title||"").toLowerCase().split(/\s+/)));
  const ws=String(j.title||"").split(/\s+/).filter(w=>!other.has(w.toLowerCase()));
  return ws.find(w=>!SENIORITY.has(w.toLowerCase()))||ws[0]||"";
}
function calChip(e){
  /* The company on the chip, the role in its tooltip; when the company is
     there more than once, a word of the role tells them apart. */
  const j=e.job, tag=e.kind==="iv"||e.kind==="fu"?roleTag(j):"", co=esc(j.company)+(tag?' <em class="rt">'+esc(tag)+'</em>':'');
  if(e.past) return '<button class="cal-chip iv past" data-open-job="'+esc(j.id)+'" title="'+esc(j.company+" · "+j.title+(e.round.kind?" · "+t(e.round.kind):"")+
    (e.round.outcome?" · "+t(e.round.outcome==="passed"?"Passed":"Didn't pass"):""))+'"><i class="pt"></i><span>'+hmShow(e.at,userTz())+' '+co+'</span></button>';
  const drag=(e.kind==="iv"||e.kind==="fu")?' draggable="true" data-drag="'+e.kind+':'+esc(j.id)+'"':'';
  if(e.kind==="iv") return '<button class="cal-chip iv" data-open-job="'+esc(j.id)+'"'+drag+' title="'+esc(interviewLine(j))+
    '"><i class="pt"></i><span>'+hmShow(e.at,userTz())+' '+co+'</span>'+(otherTz(j)?'<i class="gl">'+GLOBE+'</i>':'')+'</button>';
  /* The chip's colour says follow-up; the words left for the company. */
  if(e.kind==="fu") return '<button class="cal-chip fu'+(e.late?" late":"")+'" data-open-job="'+esc(j.id)+'" title="'+
    esc((e.late?t("Overdue"):t("Follow up"))+" · "+j.company+" · "+j.title)+'"'+drag+'><span>'+
    (e.late?esc(t("Overdue"))+' · ':'↩ ')+co+'</span></button>';
  if(e.kind==="of") return '<button class="cal-chip of" data-open-job="'+esc(j.id)+'" title="'+esc(j.company+" · "+j.title)+'"><span>'+t("Offer")+' · '+co+'</span></button>';
  if(e.kind==="rp") return '<button class="cal-chip rp" data-open-job="'+esc(j.id)+'" title="'+esc(t("{co} moved to interviews",{co:j.company})+" · "+j.title)+'"><span>'+co+' · '+esc(t("invited"))+'</span></button>';
  return "";
}
/* What was sent and what ended on a day, said as what happened. */
function dayActs(list){
  const sent=list.filter(e=>e.kind==="sent").length, closed=list.filter(e=>e.kind==="closed");
  if(!sent&&!closed.length) return "";
  const ends=closed.reduce((m,e)=>{ const st=e.job.status;
    const k=/^rejected/.test(st)?"{n} rejected":/^ghosted/.test(st)?"{n} ghosted":"{n} declined"; m[k]=(m[k]||0)+1; return m },{});
  return '<div class="act">'+(sent?'<span title="'+esc(list.filter(e=>e.kind==="sent").map(e=>e.job.company+" · "+e.job.title).join("\n"))+'"><i style="background:var(--fn-wait)"></i>'+esc(t("{n} sent",{n:sent}))+'</span>':'')+
    (closed.length?'<span title="'+esc(closed.map(e=>e.job.company+" · "+t(prettyStatus(e.job.status))).join("\n"))+'"><i style="background:var(--bd-field)"></i>'+
      esc(Object.entries(ends).map(([k,n])=>t(k,{n})).join(", "))+'</span>':'')+'</div>';
}
function drawCalMonth(ev){
  const today=todayKey(), first=S.calAnchor.slice(0,8)+"01", month=first.slice(0,7), start=monday(first);
  $("#cal-title").textContent=fmtKey(first,{month:"long",year:"numeric"});
  const by={}; ev.forEach(e=>(by[e.day]=by[e.day]||[]).push(e));
  const order={iv:0,fu:1,of:2,rp:3};
  let cells="";
  const weeks=dayDiff(start,addDays(first.slice(0,7)+"-28",4))>=35?6:5;
  for(let i=0;i<weeks*7;i++){
    const k=addDays(start,i), list=(by[k]||[]), inm=k.slice(0,7)===month;
    const chips=list.filter(e=>order[e.kind]!=null).sort((a,b)=>order[a.kind]-order[b.kind]||(a.at||0)-(b.at||0));
    const dnum=Number(k.slice(8));
    cells+='<div class="cal-cell'+(inm?"":" out")+(((i%7)>=5)?" wkend":"")+(k===today?" today":"")+'" data-drop="'+k+'">'+
      '<div class="d"><span>'+dnum+(dnum===1?" "+esc(fmtKey(k,{month:"short"})):"")+'</span></div>'+
      /* Room for three: past three, two and a count of the rest. */
      chips.slice(0,chips.length>3?2:3).map(calChip).join("")+(chips.length>3?'<small style="font-size:11px;color:var(--t500)">+'+(chips.length-2)+'</small>':'')+
      dayActs(list)+'</div>';
  }
  const dows=Array.from({length:7},(_,i)=>'<div class="dw">'+esc(fmtKey(addDays("2026-09-21",i),{weekday:"short"}))+'</div>').join("");
  $("#cal-body").innerHTML='<div class="cal-month"><section class="cal-card cal-grid cal-r" style="grid-template-rows:auto repeat('+
    weeks+',minmax(112px,1fr))">'+dows+cells+'</section>'+calComing(ev)+'</div>';
  wireCal(); wireDrag();
}
function calComing(ev){
  const today=todayKey(), end=addDays(today,7), now=new Date();
  const late=ev.filter(e=>e.kind==="fu"&&e.late).sort((a,b)=>a.day.localeCompare(b.day));
  /* What is ahead only: something that already happened today is in the
     month, not in Coming up. */
  const up=ev.filter(e=>(e.kind==="iv"&&e.at>=now&&e.day<=end)||(e.kind==="fu"&&!e.late&&e.day<=end))
    .sort((a,b)=>a.day.localeCompare(b.day)||(a.at||0)-(b.at||0));
  const col={iv:"var(--fn-positive)",fu:"var(--fn-wait)",rp:"var(--fn-offer)",of:"var(--fn-offer)"};
  const when=e=>(e.day===today?t("Today")+" · ":"")+fmtKey(e.day,{weekday:"short",day:"numeric"})+
    (e.kind==="iv"?" · "+hmShow(e.at,userTz())+(otherTz(e.job)?" "+t("your time"):""):"");
  const what=e=>e.kind==="iv"?t("Interview")+" · "+e.job.company:e.kind==="fu"?t("Follow up with {co}",{co:e.job.company})
    :e.kind==="rp"?t("{co} moved to interviews",{co:e.job.company}):t("Offer")+" · "+e.job.company;
  return '<section class="cal-card cal-side cal-r" style="animation-delay:.1s"><div class="cal-ch"><h2>'+t("Coming up")+
    '</h2><span>'+t("next 7 days")+'</span></div>'+
    (late.length?'<div class="cal-late"><b>'+esc(t(late.length===1?"1 follow-up overdue":"{n} follow-ups overdue",{n:late.length}))+'</b> · '+
      late.slice(0,4).map(e=>esc(e.job.company)+" ("+esc(fmtKey(e.day,{day:"numeric",month:"short"}))+")").join(", ")+'</div>':'')+
    (up.length?up.map(e=>'<button class="cal-item" data-open-job="'+esc(e.job.id)+'"><span class="bar" style="background:'+col[e.kind]+'"></span>'+
      '<span class="tx"><small>'+esc(when(e))+'</small><b>'+esc(what(e))+'</b><span>'+esc(e.job.title)+'</span>'+
      (e.kind==="iv"&&otherTz(e.job)?'<em>'+GLOBE+esc(hmShow(e.at,e.job.interview_tz)+" "+t("in")+" "+tzCity(e.job.interview_tz))+'</em>':'')+
      '</span></button>').join(""):'<p class="cal-none">'+t("Nothing in the next seven days.")+'</p>')+'</section>';
}
/* Drag an interview or a follow-up to another day. An interview keeps its
   hour where you are, and is written back in the zone it was given in. */
function wireDrag(){
  let what=null;
  $$("#cal-body [data-drag]").forEach(el=>el.ondragstart=e=>{ what=el.dataset.drag; e.dataTransfer.setData("text/plain",what);
    e.dataTransfer.effectAllowed="move" });
  $$("#cal-body [data-drop]").forEach(c=>{
    c.ondragover=e=>{ if(what){ e.preventDefault(); c.classList.add("drop") } };
    c.ondragleave=()=>c.classList.remove("drop");
    c.ondrop=async e=>{
      e.preventDefault(); c.classList.remove("drop");
      const [kind,id]=(what||"").split(":"); what=null;
      const j=(S.jobs||[]).find(x=>x.id===id), day=c.dataset.drop; if(!j) return;
      if(kind==="fu") await saveJob(id,{followup_date:day});
      else{
        const at=interviewMoment(j), hm=hmIn(at,userTz());
        const moved=wallToInstant(day+"T"+hm,userTz());
        await saveJob(id,{interview_at:wallIn(moved,j.interview_tz||machineTz())+":00"});
      }
      toast(t("Moved to {day}",{day:fmtKey(day,{weekday:"long",day:"numeric",month:"long"})}));
      drawCalendar(false);
    };
  });
}

/* ---- week ---------------------------------------------------------------- */
function drawCalWeek(ev){
  const today=todayKey(), start=monday(S.calAnchor), end=addDays(start,6);
  $("#cal-title").textContent=fmtKey(start,{day:"numeric",month:"short"})+" – "+fmtKey(end,{day:"numeric",month:"short",year:"numeric"});
  const days=Array.from({length:7},(_,i)=>addDays(start,i));
  const ivs=ev.filter(e=>e.kind==="iv"&&e.day>=start&&e.day<=end);
  const fus=ev.filter(e=>(e.kind==="fu"||e.kind==="of"||e.kind==="rp")&&e.day>=start&&e.day<=end);
  const inWk=ev.filter(e=>e.day>=start&&e.day<=end);
  const hrs=ivs.map(e=>+hmIn(e.at,userTz()).slice(0,2));
  const h0=Math.min(9,...hrs), h1=Math.max(19,...hrs.map(h=>h+1)), HH=56;
  S.calH0=h0;
  const head='<div class="cal-wh"><div></div>'+days.map(k=>'<div'+(k===today?' class="today"':'')+'><small>'+
    esc(fmtKey(k,{weekday:"short"}))+'</small><b>'+Number(k.slice(8))+'</b></div>').join("")+'</div>';
  const allday='<div class="cal-wa"><div>'+t("All day")+'</div>'+days.map(k=>'<div data-drop="'+k+'">'+
    fus.filter(e=>e.day===k).map(calChip).join("")+dayActs(inWk.filter(e=>e.day===k))+'</div>').join("")+'</div>';
  const hours=Array.from({length:h1-h0},(_,i)=>h0+i);
  /* Two interviews in the same hour share the column side by side. */
  const slot=e=>e.day+"|"+hmIn(e.at,userTz()).slice(0,2), bySlot={};
  ivs.forEach(e=>(bySlot[slot(e)]=bySlot[slot(e)]||[]).push(e));
  const blocks=ivs.map(e=>{
    const [h,m]=hmIn(e.at,userTz()).split(":").map(Number), col=days.indexOf(e.day);
    const top=(h-h0+m/60)*HH+2, grp=bySlot[slot(e)], n=grp.length, k=grp.indexOf(e);
    const tz=!e.past&&otherTz(e.job);
    return '<button class="cal-blk'+(e.past?" past":"")+(S.calPop===e.job.id&&!e.past?" on":"")+'" '+(e.past?'data-open-job':'data-pop')+'="'+esc(e.job.id)+'" style="left:calc('+col+
      ' * (100% / 7) + '+k+' * (100% / 7 / '+n+') + 4px);width:calc(100% / 7 / '+n+' - 8px);top:'+top+'px;height:'+(HH-4)+'px"><b>'+hmShow(e.at,userTz())+' '+
      esc(e.job.company)+'</b><span>'+esc(e.past&&e.round&&e.round.kind?t(e.round.kind):e.job.title)+'</span>'+(tz?'<em>'+GLOBE+esc(hmShow(e.at,e.job.interview_tz)+
      " "+t("in")+" "+tzCity(e.job.interview_tz))+'</em>':'')+'</button>';
  }).join("");
  let nowl="";
  const ni=days.indexOf(today);
  if(ni>=0){ const [h,m]=hmIn(new Date(),userTz()).split(":").map(Number);
    if(h>=h0&&h<h1) nowl='<span class="cal-nowline" style="left:calc('+ni+' * (100% / 7));width:calc(100% / 7);top:'+((h-h0+m/60)*HH)+'px"></span>' }
  const grid='<div class="cal-wg"><div>'+hours.map(h=>'<div class="cal-hr">'+String(h).padStart(2,"0")+':00</div>').join("")+
    '</div><div class="cal-wcols">'+hours.map(()=>'<div class="ln"></div>').join("")+
    days.map((_,i)=>'<span class="col" style="left:calc('+i+' * (100% / 7))"></span>').join("")+blocks+nowl+'</div></div>';
  $("#cal-body").innerHTML='<section class="cal-card cal-week cal-r">'+head+allday+grid+calPopHTML(ivs)+'</section>';
  wireCal(); wireDrag();
  $$("#cal-body [data-pop]").forEach(b=>b.onclick=()=>{ S.calPop=S.calPop===b.dataset.pop?null:b.dataset.pop; drawCalWeek(calEvents()) });
  const x=$("#cal-pop-x"); if(x) x.onclick=()=>{ S.calPop=null; drawCalWeek(calEvents()) };
}
function calPopHTML(ivs){
  const e=ivs.find(x=>x.job.id===S.calPop&&!x.past)||ivs.find(x=>x.job.id===S.calPop); if(!e) return "";
  const j=e.job, two=otherTz(j);
  const col=((dayDiff(monday(e.day),e.day))+7)%7, [h,m]=hmIn(e.at,userTz()).split(":").map(Number);
  const top=Math.max(8,Math.min(360,110+(h-S.calH0+m/60)*56-40));
  const side=col>=4?"right:calc("+(7-col)+" * ((100% - 64px) / 7) + 12px)":"left:calc(64px + "+(col+1)+" * ((100% - 64px) / 7) + 12px)";
  return '<div class="cal-pop" style="'+side+';top:'+top+'px" role="dialog" aria-label="'+esc(t("Interview")+" · "+j.company)+'">'+
    '<button class="x" id="cal-pop-x" aria-label="'+esc(t("Close"))+'">✕</button>'+
    '<div class="hd">'+companyMark(j)+'<div><b>'+esc(t("Interview")+" · "+j.company)+'</b><span>'+esc(j.title)+
      (j.location?" · "+esc(j.location):"")+'</span></div></div>'+
    '<div class="tz"><div><small>'+t("Your time")+'</small><b>'+esc(fmtKey(e.day,{weekday:"short",day:"numeric"})+" · "+hmShow(e.at,userTz()))+'</b></div>'+
      (two?'<div><small>'+esc(t("In {city}",{city:tzCity(j.interview_tz)}))+'</small><b>'+esc(hmShow(e.at,j.interview_tz))+'</b></div>'
        :'<div><small>'+t("Status")+'</small><b>'+esc(prettyStatus(j.status))+'</b></div>')+'</div>'+
    (j.notes?'<p>'+esc(String(j.notes).slice(0,220))+'</p>':'')+
    '<div class="a"><button class="pbtn" data-open-job="'+esc(j.id)+'">'+t("Open application")+'</button>'+
      (j.cv_path?'<button class="obtn" data-open-doc="'+esc(j.cv_path)+'">'+t("Open CV")+'</button>':'')+
      '<button class="obtn" data-ics="'+esc(j.id)+'">'+t("Add to my calendar")+'</button></div></div>';
}
function wireCal(){

  $$("#cal-body [data-open-job]").forEach(el=>el.onclick=ev=>{ if(ev.target.closest("[data-pop]")) return; openJob(el.dataset.openJob) });
  $$("#cal-body [data-open-doc]").forEach(el=>el.onclick=ev=>{ ev.stopPropagation(); openDoc(el.dataset.openDoc) });
  $$("#cal-body [data-ics]").forEach(el=>el.onclick=ev=>{ ev.stopPropagation(); download("/api/calendar.ics?id="+encodeURIComponent(el.dataset.ics)+tok(),"interview.ics") });
  $$("#cal-body .cal-md[data-day]").forEach(el=>el.onclick=()=>{ if(el.classList.contains("out")) return;
    S.calView="month"; S.calAnchor=el.dataset.day; drawCalendar(false) });
}

