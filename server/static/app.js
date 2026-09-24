/* The interface. Served as /static/app.js at the end of the page (INDEX_HTML
   in studio.py), after i18n.js and worldmap.js; API_TOKEN and the
   preferences are set by the page's first, inline script. */
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* ---- the interface's language ---------------------------------------------
   English, French, Spanish or Brazilian Portuguese: the computer's language
   unless Settings says otherwise. Two ways in. t("...") for text built in
   code, with {name} holes for what changes. And a watcher that translates
   the interface's text as it lands in the page, so a label written once in
   markup needs no call at all. It never touches what is yours: CVs, letters,
   postings, notes and anything you are typing (I18N_SKIP). */
const UI_LANGS={en:"en-GB",fr:"fr-FR",es:"es-ES",pt:"pt-BR"};
function uiLang(){
  let p=new URLSearchParams(location.search).get("lang");
  if(!p||!UI_LANGS[p]) p=(window.CVS_PREFS||{}).ui_lang;
  if(p&&UI_LANGS[p]) return p;
  const n=String(navigator.language||"en").slice(0,2).toLowerCase();
  return UI_LANGS[n]?n:"en";
}
const UI_LANG=uiLang();
const uiLocale=()=>UI_LANGS[UI_LANG];
const I18N_D=(window.I18N||{})[UI_LANG]||null, I18N_P=(window.I18N_RX||{})[UI_LANG]||[];
/* The catalogue writes a plural as "envoyée(s)"; with the count in front of
   it, it becomes the right form. French counts 0 and 1 as singular. */
const plur=r=>{
  if(r.indexOf("(s)")<0) return r;
  /* Every "(s)" follows the nearest number before it: "2 choses prévues". */
  let n=null;
  return r.replace(/(\d+)|\(s\)/g,(m,d)=>d!=null?(n=+d,m):n==null?m:(UI_LANG==="fr"?n<2:n===1)?"":"s");
};
function t(s,v){
  let r=(I18N_D&&I18N_D[s])||s;
  if(v) r=r.replace(/\{(\w+)\}/g,(m,k)=>v[k]??m);
  return plur(r);
}
function trString(str){
  if(!I18N_D) return null;
  const key=str.replace(/\s+/g," ").trim();
  if(!key||key.length>600) return null;
  let out=I18N_D[key];
  if(out==null) for(const [re,rep] of I18N_P){ if(re.test(key)){ out=key.replace(re,rep); break } }
  if(out==null||out===key) return null;
  /* A pattern can turn "1 page" into "1 page(s)", which plur turns back into
     "1 page": writing that would be a change of nothing, which the watcher
     sees as a change, forever. */
  out=plur(out);
  if(out===key) return null;
  const lead=str.match(/^\s*/)[0], trail=str.match(/\s*$/)[0];
  return lead+out+trail;
}
/* Text the server wrote (an error, a hint), put into a sentence of ours:
   the watcher only knows a text node whole, so the part is translated here. */
const tx=s=>{ s=String(s??""); const r=trString(s); return r==null?s:r };
const I18N_SKIP="[data-noi18n],[contenteditable],textarea,input,script,style,code,pre,.ap-post,"+
  ".ap-notes,.lt-stage,.fj-who,.fn-src .sn,.co,.con,.sk-tip .who,#yaml,.yamlerr,.drift-list .then";
const I18N_ATTRS=["placeholder","title","aria-label","data-ph"];
const I18N_SKIP_FIELD="[data-noi18n],[contenteditable],#yaml,.ap-post,.ap-notes";
function trNode(n){
  if(n.nodeType===3){
    const p=n.parentElement; if(!p||p.closest(I18N_SKIP)) return;
    const r=trString(n.nodeValue); if(r!=null) n.nodeValue=r;
  }else if(n.nodeType===1){
    /* A field's own words (its placeholder, its label) are the app's, even
       though what is typed in it never is. */
    (n.matches("input,textarea")?[n]:[...n.querySelectorAll("input,textarea")]).forEach(f=>{
      if(f.closest(I18N_SKIP_FIELD)) return;
      for(const a of I18N_ATTRS){ const v=f.getAttribute(a); if(v){ const r=trString(v); if(r!=null) f.setAttribute(a,r) } }
    });
    if(n.closest(I18N_SKIP)) return;
    for(const a of I18N_ATTRS){ const v=n.getAttribute(a); if(v){ const r=trString(v); if(r!=null) n.setAttribute(a,r) } }
    const w=document.createTreeWalker(n,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode:x=>
      (x.nodeType===1?x:x.parentElement).closest(I18N_SKIP)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
    let x; while((x=w.nextNode())){
      if(x.nodeType===3){ const r=trString(x.nodeValue); if(r!=null) x.nodeValue=r }
      else for(const a of I18N_ATTRS){ const v=x.getAttribute(a); if(v){ const r=trString(v); if(r!=null) x.setAttribute(a,r) } }
    }
  }
}
if(I18N_D){
  /* Native dialogs are outside the page, where the watcher cannot see. */
  for(const k of ["confirm","prompt","alert"]){
    const f=window[k].bind(window);
    window[k]=(m,...a)=>f(m==null?m:(trString(String(m))??String(m)),...a);
  }
  document.documentElement.lang=UI_LANG;
  trNode(document.body);
  new MutationObserver(ms=>{ for(const m of ms){
    if(m.type==="childList") m.addedNodes.forEach(trNode);
    else if(m.type==="characterData") trNode(m.target);
    else if(m.type==="attributes") trNode(m.target);
  } }).observe(document.body,{childList:true,subtree:true,characterData:true,
    attributes:true,attributeFilter:I18N_ATTRS});
}
/* Date fields in the app's language. A native date field formats itself in
   the system's locale (mm/dd/yyyy on an English Windows, whatever language
   the app is in), and no attribute changes that. So each one gets a face: the
   date as the app would write it, over the field while it is not being
   typed into. The field underneath is untouched, picker and all. */
const DFX_FMT={date:{day:"numeric",month:"short",year:"numeric"},
  "datetime-local":{day:"numeric",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"}};
const DFX_VALUE=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value");
function dfxPaint(inp){
  const sp=inp.nextElementSibling; if(!sp||!sp.classList.contains("dfx-v")) return;
  const v=DFX_VALUE.get.call(inp); let txt="";
  if(v){ const d=inp.type==="date"?new Date(v+"T12:00:00"):new Date(v);
    if(!isNaN(d)) try{ txt=DTF(uiLocale(),DFX_FMT[inp.type]).format(d) }catch(e){} }
  sp.textContent=txt||t("No date"); sp.classList.toggle("dfx-none",!txt);
}
function dfxWrap(inp){
  if(inp.dataset.dfx||!DFX_FMT[inp.type]) return;
  inp.dataset.dfx="1";
  const cs=getComputedStyle(inp), w=document.createElement("span"), sp=document.createElement("span");
  w.className="dfx"; sp.className="dfx-v"; sp.setAttribute("aria-hidden","true");
  sp.style.paddingLeft=(parseFloat(cs.paddingLeft)+parseFloat(cs.borderLeftWidth)||0)+"px";
  sp.style.fontSize=cs.fontSize; sp.style.fontWeight=cs.fontWeight;
  inp.replaceWith(w); w.append(inp,sp);
  /* Code that sets the value directly fires no event; repaint on that too. */
  Object.defineProperty(inp,"value",{configurable:true,get(){ return DFX_VALUE.get.call(this) },
    set(v){ DFX_VALUE.set.call(this,v); dfxPaint(this) }});
  inp.addEventListener("input",()=>dfxPaint(inp)); inp.addEventListener("change",()=>dfxPaint(inp));
  dfxPaint(inp);
}
const DFX_SEL='input[type="date"],input[type="datetime-local"]';
new MutationObserver(ms=>{ for(const m of ms) for(const n of m.addedNodes){ if(n.nodeType!==1) continue;
  if(n.matches(DFX_SEL)) dfxWrap(n); else n.querySelectorAll&&n.querySelectorAll(DFX_SEL).forEach(dfxWrap) } })
  .observe(document.body,{childList:true,subtree:true});
document.querySelectorAll(DFX_SEL).forEach(dfxWrap);

const tok=()=>API_TOKEN?"&token="+encodeURIComponent(API_TOKEN):"";

/* One object holds everything the three screens share. Selection, filters and
   the last good render all live here so that switching views never throws work
   away: the funnel can hand a status filter to Jobs, and Jobs can hand a
   document to the editor, without either reloading. */
const S={
  view:"jobs", state:null,
  path:null, doc:null, data:null, tab:"page",
  dirty:false, savedAt:null, busy:false,
  pdf:null, render:null, renderMs:null, live:"idle", liveMsg:"",
  page:0, zoom:1, zoomAuto:true, fill:null,
  sel:null, openSection:null,
  docThumbs:{},             /* each document's first page, for Documents */
  baseLang:null,            /* which language of the base CV Documents shows */
  docLang:null,             /* the Documents language filter, null for all */
  driftBy:null,             /* how far behind each translation of the base is */
  ai:null,                  /* which AI clients are wired up to us */
  prov:null,                /* who wrote each field, and what differs from the base */
  pulse:null,               /* last workspace poll: file stamps and AI activity */
  skills:null,              /* the cv-studio skills on this machine */
  keyShown:false,           /* the API key is masked until asked for */
  docMtime:null,            /* the open file as we last read or wrote it */
  extMtime:null,            /* a newer version on disk we have not taken */
  extTheirs:null,           /* their version, so the bar can name the fields */
  resolved:null,            /* how the last conflict was settled, and when */
  pages:{},                 /* path -> page count, learned as things render */
  themePages:{},            /* theme -> page count for the open document */
  jobs:[], statuses:[], nodes:{}, labels:{}, jready:false,
  tailoring:new Set(),      /* applications whose CV is being copied right now */
  jfilter:{kind:"all", value:""}, jsel:null,
  funnel:null, since:"", fnode:null,
  baseThumb:null,           /* {path, png, failed}: the base's first page */
  schema:null, schemaTheme:null,
};
const DZ={theme:null, section:"theme"};

/* act = {label, fn}: one button in the toast, and a longer life to reach it. */
function toast(msg,bad,act){
  const t=document.createElement("div");
  t.className="toast"+(bad?" bad":"")+(act?" has-act":""); t.textContent=msg;
  if(act){ const b=document.createElement("button"); b.className="act"; b.textContent=act.label;
    b.onclick=()=>{ t.remove(); act.fn() }; t.append(b) }
  $("#toasts").append(t);
  setTimeout(()=>{t.style.transition="opacity .3s";t.style.opacity="0";
    setTimeout(()=>t.remove(),320)}, act?9000:bad?5200:2200);
}
window.studioError=m=>{$("#pane-page").innerHTML=
  '<div class="err"><h4>Could not start</h4><p>'+esc(m)+'</p></div>'};

const api=async(u,o)=>{
  o=o||{};
  if(API_TOKEN){ o.headers=Object.assign({},o.headers,{"X-API-Key":API_TOKEN}) }
  const r=await fetch(u,o);
  const j=await r.json().catch(()=>({error:"The renderer sent an unreadable response."}));
  if(j&&j.error&&!("ok"in j)) throw new Error(j.error);
  return j;
};
const post=(u,body)=>api(u,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(body)});

/* ---- small shared formatters ---- */
const MONTHS=Array.from({length:12},(_,i)=>{ try{ return new Intl.DateTimeFormat(uiLocale(),{month:"short"})
  .format(new Date(2026,i,15)).replace(".","") }catch(e){ return ["Jan","Feb","Mar","Apr","May","Jun","Jul",
  "Aug","Sep","Oct","Nov","Dec"][i] } });
function shortDate(iso){
  if(!iso) return "";
  /* The tracker stores ISO strings; a document's age arrives as an mtime that
     has already been turned into a Date. Both want the same "14 Sep". */
  const d=iso instanceof Date?iso:new Date(String(iso).slice(0,19));
  if(isNaN(d)) return String(iso).slice(0,10);
  return d.getDate()+" "+MONTHS[d.getMonth()]+
    (d.getFullYear()!==new Date().getFullYear()?" "+String(d.getFullYear()).slice(2):"");
}
/* ---- time zones -----------------------------------------------------------
   An interview time is kept as the wall-clock time the invitation gave, in the
   zone it gave it in (interview_tz), so the file says what the email said.
   Showing it, or comparing it with now, goes through these. The zone data is
   the browser's own, so nothing ships for it. */
/* A formatter is slow to make and quick to use, and the calendar formats
   thousands of dates a draw: each kind is made once. */
function DTF(loc,o){
  const c=DTF.c||(DTF.c=new Map()), k=loc+"|"+JSON.stringify(o);
  let f=c.get(k); if(!f){ f=new Intl.DateTimeFormat(loc,o); c.set(k,f) }
  return f;
}
/* Asked often; looked up once a minute, which still follows a laptop that
   has crossed a border. */
const machineTz=()=>{
  const now=Date.now();
  if(!machineTz.at||now-machineTz.at>60000){
    try{ machineTz.v=Intl.DateTimeFormat().resolvedOptions().timeZone||"UTC" }catch(e){ machineTz.v="UTC" }
    machineTz.at=now;
  }
  return machineTz.v;
};
const userTz=()=>prefs().tz||machineTz();
function tzOffset(tz,date){
  const p=DTF("en-US",{timeZone:tz,hourCycle:"h23",year:"numeric",month:"2-digit",
    day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit"}).formatToParts(date);
  const g=t=>+p.find(x=>x.type===t).value;
  return (Date.UTC(g("year"),g("month")-1,g("day"),g("hour")%24,g("minute"),g("second"))-date.getTime())/60000;
}
/* "2026-09-25T10:00" in a zone, as the moment it names. */
function wallToInstant(wall,tz){
  const [d,t="00:00"]=String(wall).split("T"), [Y,M,D]=d.split("-").map(Number);
  const [h,m]=t.split(":").map(Number), guess=Date.UTC(Y,M-1,D,h||0,m||0);
  const off=tzOffset(tz,new Date(guess)); let at=guess-off*60000;
  const off2=tzOffset(tz,new Date(at)); if(off2!==off) at=guess-off2*60000;
  return new Date(at);
}
function interviewMoment(j){
  if(!j||!j.interview_at) return null;
  return wallToInstant(String(j.interview_at).slice(0,16),j.interview_tz||machineTz());
}
const TZ_NAMES={Sao_Paulo:"São Paulo",Bogota:"Bogotá",Mexico_City:"Mexico City",Zurich:"Zürich"};
const tzCity=tz=>{ const k=String(tz||"").split("/").pop(); return TZ_NAMES[k]||k.replace(/_/g," ") };
function fmtWhen(date,tz,withDay=true){
  const o={timeZone:tz,hour:"2-digit",minute:"2-digit"};
  if(withDay) Object.assign(o,{weekday:"short",day:"numeric",month:"short"});
  try{ return DTF(uiLocale(),o).format(date) }catch(e){ return date.toISOString().slice(0,16) }
}
/* "Thu 25 Sep, 11:00 your time · 10:00 in London", or just the one when the
   two zones agree at that moment. */
function interviewLine(j){
  const at=interviewMoment(j); if(!at) return "";
  const mine=userTz(), theirs=j.interview_tz;
  const here=fmtWhen(at,mine);
  if(!theirs||tzOffset(theirs,at)===tzOffset(mine,at)) return here;
  return here+" "+t("your time")+" · "+fmtWhen(at,theirs,false)+" "+t("in")+" "+tzCity(theirs);
}
/* The zone an interview is probably in, from where the job is. Remote, or
   anywhere not listed, is your own. */
const TZ_GUESS=[
  [/london|manchester|edinburgh|\buk\b|united kingdom|england|scotland/i,"Europe/London"],
  [/dublin|ireland/i,"Europe/Dublin"],
  [/paris|lyon|marseille|toulouse|bordeaux|lille|nantes|montpellier|france/i,"Europe/Paris"],
  [/madrid|barcelona|valencia|sevilla|seville|spain|españa/i,"Europe/Madrid"],
  [/lisbon|lisboa|porto|portugal/i,"Europe/Lisbon"],
  [/berlin|munich|münchen|hamburg|frankfurt|germany|deutschland/i,"Europe/Berlin"],
  [/amsterdam|rotterdam|netherlands/i,"Europe/Amsterdam"],
  [/brussels|bruxelles|belgium/i,"Europe/Brussels"],
  [/zurich|zürich|geneva|genève|switzerland/i,"Europe/Zurich"],
  [/milan|milano|rome|roma|italy/i,"Europe/Rome"],
  [/stockholm|sweden/i,"Europe/Stockholm"],[/copenhagen|denmark/i,"Europe/Copenhagen"],
  [/oslo|norway/i,"Europe/Oslo"],[/helsinki|finland/i,"Europe/Helsinki"],
  [/warsaw|poland/i,"Europe/Warsaw"],
  [/são paulo|sao paulo|rio de janeiro|belo horizonte|brazil|brasil/i,"America/Sao_Paulo"],
  [/new york|nyc|boston|miami|atlanta|washington|toronto|montreal|montréal/i,"America/New_York"],
  [/chicago|austin|dallas|houston/i,"America/Chicago"],[/denver|boulder/i,"America/Denver"],
  [/san francisco|\bsf\b|bay area|seattle|los angeles|palo alto|mountain view|vancouver|california/i,"America/Los_Angeles"],
  [/mexico/i,"America/Mexico_City"],[/buenos aires|argentina/i,"America/Argentina/Buenos_Aires"],
  [/singapore/i,"Asia/Singapore"],[/tokyo|japan/i,"Asia/Tokyo"],[/sydney|melbourne/i,"Australia/Sydney"],
  [/dubai/i,"Asia/Dubai"],[/bangalore|bengaluru|india/i,"Asia/Kolkata"],
];
function guessTz(j){
  const where=[j.location,j.country].filter(Boolean).join(" ");
  const hit=TZ_GUESS.find(([re])=>re.test(where));
  return hit?hit[1]:null;
}
const COMMON_TZ=["Europe/London","Europe/Dublin","Europe/Lisbon","Europe/Paris","Europe/Madrid",
  "Europe/Berlin","Europe/Amsterdam","Europe/Zurich","Europe/Stockholm","America/New_York",
  "America/Chicago","America/Denver","America/Los_Angeles","America/Sao_Paulo","America/Mexico_City",
  "Asia/Dubai","Asia/Kolkata","Asia/Singapore","Asia/Tokyo","Australia/Sydney","UTC"];
function allTz(){ try{ return Intl.supportedValuesOf("timeZone") }catch(e){ return COMMON_TZ } }
/* ---- the little world on the time zone setting ------------------------ */
const WX=lon=>(lon+180)*2, WY=lat=>(82.5-lat)*2, WW=720, WH=280;
function tzCoords(z){ const W=window.WORLD; return W&&W.zones[z]||null }
/* Where it is night now: the terminator from the sun's declination and the
   meridian it stands over, closed toward the pole that is in the dark. */
function nightPath(now){
  const doy=(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate())-
    Date.UTC(now.getUTCFullYear(),0,0))/864e5;
  let dec=-23.44*Math.cos(2*Math.PI/365*(doy+10));
  if(Math.abs(dec)<.3) dec=dec<0?-.3:.3;
  const sunLon=-((now.getUTCHours()+now.getUTCMinutes()/60)-12)*15, r=Math.PI/180;
  const pts=[];
  for(let lon=-180;lon<=180;lon+=3){
    const lat=Math.atan(-Math.cos((lon-sunLon)*r)/Math.tan(dec*r))/r;
    pts.push(WX(lon).toFixed(1)+","+Math.max(-4,Math.min(WH+4,WY(lat))).toFixed(1));
  }
  const edge=dec>0?WH+4:-4;
  return "M"+pts.join("L")+"L"+WX(180)+","+edge+"L"+WX(-180)+","+edge+"Z";
}
function drawTzMap(sel){
  const host=$("#tzmap"), W=window.WORLD;
  if(!host) return;
  if(!W){ host.hidden=true; return }
  const now=new Date(), zone=userTz(), here=tzCoords(zone);
  const off=tzOffset(zone,now)/60;
  let land="";
  W.rows.forEach((hex,ri)=>{
    const y=WY(W.lat0-ri*W.step);
    for(let c=0;c<hex.length;c++){ const v=parseInt(hex[c],16);
      for(let b=0;b<4;b++) if(v&(8>>b)) land+="M"+WX(W.lon0+(c*4+b)*W.step).toFixed(1)+" "+y.toFixed(1)+"h0" }
  });
  /* The band is the zone's offset as a slice of the globe: fifteen degrees an hour. */
  const bx=WX(Math.max(-180,off*15-7.5)), bw=WX(Math.min(180,off*15+7.5))-bx;
  const ivs=[...new Set((S.jobs||[]).filter(j=>j.interview_tz&&j.interview_at&&
    interviewMoment(j)>=Date.now()-864e5&&j.interview_tz!==zone).map(j=>j.interview_tz))]
    .map(z=>({z,c:tzCoords(z)})).filter(x=>x.c);
  const arc=(a,b)=>{ const x1=WX(a[0]),y1=WY(a[1]),x2=WX(b[0]),y2=WY(b[1]);
    const mx=(x1+x2)/2, my=(y1+y2)/2-Math.hypot(x2-x1,y2-y1)*.28;
    return '<path class="arc" d="M'+x1+' '+y1+'Q'+mx+' '+my+' '+x2+' '+y2+'"/>' };
  const hm=z=>DTF(uiLocale(),{timeZone:z,hour:"2-digit",minute:"2-digit"}).format(now);
  host.innerHTML='<svg viewBox="0 0 '+WW+' '+WH+'" role="img">'+
    '<rect class="band" x="'+bx+'" y="0" width="'+bw+'" height="'+WH+'"/>'+
    '<line class="band-edge" x1="'+bx+'" x2="'+bx+'" y1="0" y2="'+WH+'"/>'+
    '<line class="band-edge" x1="'+(bx+bw)+'" x2="'+(bx+bw)+'" y1="0" y2="'+WH+'"/>'+
    '<path class="land" d="'+land+'"/>'+
    '<path class="night" d="'+nightPath(now)+'"/>'+
    (here?ivs.map(x=>arc(here,x.c)).join(""):"")+
    ivs.map(x=>'<circle class="iv" cx="'+WX(x.c[0])+'" cy="'+WY(x.c[1])+'" r="4.5"><title>'+
      esc(tzCity(x.z)+" · "+hm(x.z))+'</title></circle>').join("")+
    (here?'<circle class="ring" cx="'+WX(here[0])+'" cy="'+WY(here[1])+'" r="7"/>'+
      '<circle class="pin" cx="'+WX(here[0])+'" cy="'+WY(here[1])+'" r="5.5"/>':"")+
    '<circle class="ghost" r="6" cx="-20" cy="-20"/></svg>'+
    (here?'<div class="card" id="tz-card"><div class="c1">'+esc(tzCity(zone))+'</div><div class="c2">'+esc(hm(zone))+
      ' · '+esc(utcLabel(zone))+'</div></div>':"")+
    '<div class="tip" hidden></div>'+
    '<div class="cap"><span>'+esc(t("Click the map to pick the nearest city."))+'</span><span class="grow"></span>'+
      (ivs.length?'<span class="sw"><i style="border:2px solid var(--acc);border-radius:50%;width:8px;height:8px"></i>'+
        esc(t("Interviews"))+'</span>':"")+
      '<span class="sw"><i style="background:var(--acc);opacity:.35"></i>'+esc(t("Your zone"))+'</span>'+
      '<span class="sw"><i style="background:var(--t900);opacity:.2"></i>'+esc(t("Night now"))+'</span></div>';
  /* The card sits beside the pin, on whichever side has room. */
  const card=$("#tz-card"), svg=host.querySelector("svg");
  const place=()=>{ if(!card||!here) return;
    const k=svg.clientWidth/WW, x=14+WX(here[0])*k, y=14+WY(here[1])*k;
    const left=x>svg.clientWidth*.6;
    card.style.left=(left?x-card.offsetWidth-14:x+14)+"px"; card.style.top=(y-card.offsetHeight/2)+"px" };
  requestAnimationFrame(place);
  const zones=allTz().map(z=>[z,tzCoords(z)]).filter(x=>x[1]);
  const nearest=(lon,lat)=>{ let best=null,bd=1e9;
    for(const [z,c] of zones){ const dl=Math.abs(c[0]-lon), dx=Math.min(dl,360-dl)*Math.cos(lat*Math.PI/180),
      d=dx*dx+(c[1]-lat)**2; if(d<bd){bd=d;best=[z,c]} } return best };
  const at=e=>{ const r=svg.getBoundingClientRect(), k=WW/r.width;
    return [(e.clientX-r.left)*k/2-180, 82.5-(e.clientY-r.top)*k/2] };
  const tip=host.querySelector(".tip"), ghost=host.querySelector(".ghost");
  svg.onmousemove=e=>{ const n=nearest(...at(e)); if(!n) return;
    const k=svg.clientWidth/WW;
    ghost.setAttribute("cx",WX(n[1][0])); ghost.setAttribute("cy",WY(n[1][1]));
    tip.hidden=false; tip.textContent=tzCity(n[0])+" · "+utcLabel(n[0]);
    tip.style.left=(14+WX(n[1][0])*k)+"px"; tip.style.top=(14+WY(n[1][1])*k)+"px" };
  svg.onmouseleave=()=>{ tip.hidden=true; ghost.setAttribute("cx",-20) };
  svg.onclick=e=>{ const n=nearest(...at(e)); if(!n||n[0]===zone) return;
    if(![...sel.options].some(o=>o.value===n[0])) sel.insertAdjacentHTML("beforeend",
      '<option value="'+esc(n[0])+'">'+esc(tzCity(n[0]))+'</option>');
    sel.value=n[0]; sel.onchange() };
}

/* A zone as people read it: "UTC−3", "UTC+5:30". Worked out once per zone. */
const TZ_UTC=new Map();
function utcLabel(z){
  if(TZ_UTC.has(z)) return TZ_UTC.get(z);
  let l="";
  try{ const m=Math.round(tzOffset(z,new Date())), a=Math.abs(m);
    l="UTC"+(m?(m<0?"\u2212":"+")+Math.floor(a/60)+(a%60?":"+String(a%60).padStart(2,"0"):""):"");
  }catch(e){}
  TZ_UTC.set(z,l); return l;
}
function tzOptions(cur,first){
  const top=[...new Set([first,...COMMON_TZ].filter(Boolean))];
  /* Four hundred zones, each with its offset: made once an hour (offsets
     move only at a clock change), not every time an application opens. */
  const hour=Math.floor(Date.now()/36e5);
  if(!tzOptions.c||tzOptions.c.hour!==hour) tzOptions.c={hour,m:new Map()};
  const m=tzOptions.c.m;
  const opt=z=>{
    let h=m.get(z);
    if(h==null){ h='<option value="'+esc(z)+'">'+esc(tzCity(z))+(utcLabel(z)?' · '+utcLabel(z):"")+'</option>'; m.set(z,h) }
    return z===cur?h.replace('">','" selected>'):h;
  };
  return top.map(opt).join("")+'<option disabled>──────────</option>'+
    allTz().filter(z=>!top.includes(z)).map(opt).join("");
}

const prettyStatus=s=>{
  const map={pending:"Draft", applied:"Awaiting reply", interviewing:"Interviewing",
    offer:"Offer", accepted:"Accepted", refused:"Declined", rejected:"Rejected",
    ghosted:"Ghosted", rejected_interviewing:"Rejected after interview",
    ghosted_interviewing:"Ghosted after interview"};
  return map[s]||String(s).replace(/_/g," ");
};
/* Live means an application can still turn into a job; dead means it cannot. */
/* One status vocabulary, read by the Jobs table and the funnel alike. Two
   views of the same data disagreeing about what a colour means is worse than
   having no colour at all: it was telling you an offer you declined and an
   offer you accepted were the same thing.

   draft   nothing sent yet            grey
   waiting sent, their move            blue
   live    a conversation is happening amber
   won     you got it                  teal
   closed  you ended it                violet
   lost    they ended it               red   */
const STATUS_TONE={
  pending:"draft", applied:"waiting", interviewing:"live", offer:"offer",
  accepted:"won", refused:"closed", rejected:"lost", ghosted:"lost",
  rejected_interviewing:"lost", ghosted_interviewing:"lost",
};
const statusTone=st=>STATUS_TONE[st]||"draft";
const DEAD_STATUS=new Set(Object.keys(STATUS_TONE).filter(
  k=>["lost","closed","draft"].includes(STATUS_TONE[k])));

/* A company's mark: its logo if one has been stored, otherwise its initials.
   Most companies will never have a logo, so the fallback is the common case and
   has to look chosen rather than missing. The tint is derived from the name, so
   a company keeps the same colour everywhere without anyone assigning one.

   These used to be the funnel's own hues -- #3a6ea5 was --fn-wait, #007a5e was
   --fn-won, #a83519 was --fn-lost -- so a 20x20 saturated square carrying a
   hash of the company name sat in the same row as a 6px dot carrying the
   status, in the same colours, meaning nothing. Contoso wore the Rejected red
   while its dot said Awaiting reply. The status palette is signal and a hash is
   not, so the marks are neutral now: they separate one row from the next
   without competing for the colour that means something. They were also
   hardcoded hexes, which left them running light-mode values in dark. */
const CO_TINTS=["var(--co-1)","var(--co-2)","var(--co-3)",
                "var(--co-4)","var(--co-5)","var(--co-6)"];
function companyTint(name){
  let h=0;
  for(const ch of String(name||"")) h=(h*31+ch.charCodeAt(0))>>>0;
  return CO_TINTS[h%CO_TINTS.length];
}
function initials(name){
  const words=String(name||"").split(/[^A-Za-z0-9]+/).filter(Boolean);
  if(!words.length) return "?";
  return (words.length>1?words[0][0]+words[1][0]:words[0].slice(0,2)).toUpperCase();
}
function companyMark(j){
  if(j.logo_url) return '<img class="colog" src="'+esc(j.logo_url)+tok()+
    '" alt="" loading="lazy">';
  return '<span class="colog mono" style="background:'+companyTint(j.company)+
    '">'+esc(initials(j.company))+'</span>';
}

/* Where a posting was found. The source is what someone said -- "LinkedIn",
   "via a recruiter on Indeed" -- so it wins over the link, which for a board
   that forwards to the company's own careers page names the wrong place.
   Boards that publish no mark anyone may use get their initials on a neutral
   tile, like a company with no logo, rather than a drawing of their logo. */
const BOARDS=[
  {id:"linkedin",label:"LinkedIn",bg:"#0A66C2",fg:"#fff",
   host:/(^|\.)linkedin\.com$/,word:/linked\s?in/i},
  {id:"indeed",label:"Indeed",bg:"#003A9B",fg:"#fff",host:/(^|\.)indeed\./,word:/indeed/i},
  {id:"glassdoor",label:"Glassdoor",bg:"#00A162",fg:"#fff",
   host:/(^|\.)glassdoor\./,word:/glassdoor/i},
  {id:"greenhouse",label:"Greenhouse",bg:"#24A47F",fg:"#fff",
   host:/(^|\.)greenhouse\.io$/,word:/greenhouse/i},
  {id:"wellfound",label:"Wellfound",bg:"#000",fg:"#fff",
   host:/(^|\.)(wellfound\.com|angel\.co)$/,word:/wellfound|angel\.?list/i},
  {id:"welcometothejungle",label:"Welcome to the Jungle",bg:"#FFCD00",fg:"#000",
   host:/(^|\.)welcometothejungle\.com$/,word:/welcome to the jungle|\bwttj\b/i},
  {id:"xing",label:"XING",bg:"#006567",fg:"#fff",host:/(^|\.)xing\.com$/,word:/\bxing\b/i},
  {id:"monster",label:"Monster",bg:"#6D4C9F",fg:"#fff",host:/(^|\.)monster\./,
   word:/\bmonster\b/i},
  {id:"ycombinator",label:"Work at a Startup",bg:"#F0652F",fg:"#fff",
   host:/(^|\.)(workatastartup|ycombinator)\.com$/,word:/y\s?combinator|work at a startup/i},
  {id:"lever",label:"Lever",letters:"Lv",host:/(^|\.)lever\.co$/,word:/\blever\b/i},
  {id:"workday",label:"Workday",letters:"Wd",
   host:/(^|\.)(myworkdayjobs|workday)\.com$/,word:/workday/i},
  {id:"ashby",label:"Ashby",letters:"As",host:/(^|\.)ashbyhq\.com$/,word:/\bashby/i},
  {id:"smartrecruiters",label:"SmartRecruiters",letters:"SR",
   host:/(^|\.)smartrecruiters\.com$/,word:/smart\s?recruiters/i},
  {id:"francetravail",label:"France Travail",letters:"FT",
   host:/(^|\.)(francetravail|pole-emploi)\.fr$/,word:/france travail|p[o\u00f4]le.emploi/i},
  {id:"apec",label:"Apec",letters:"Ap",host:/(^|\.)apec\.fr$/,word:/\bapec\b/i},
  {id:"hellowork",label:"HelloWork",letters:"HW",host:/(^|\.)hellowork\.com$/,
   word:/hello\s?work/i},
  {id:"jobteaser",label:"JobTeaser",letters:"JT",host:/(^|\.)jobteaser\.com$/,
   word:/job\s?teaser/i},
];
function jobBoard(j){
  const said=String(j.source||"");
  if(said){ const b=BOARDS.find(b=>b.word.test(said)); if(b) return b }
  let host="";
  try{ host=new URL(j.url).hostname.toLowerCase() }catch(e){}
  return host?BOARDS.find(b=>b.host.test(host))||null:null;
}
function boardMark(b){
  if(b.letters) return '<span class="board lettered mono" aria-hidden="true">'+
    esc(b.letters)+'</span>';
  return '<span class="board" aria-hidden="true" style="background:'+b.bg+';color:'+b.fg+
    '"><svg viewBox="0 0 24 24"><use href="#board-'+b.id+'"/></svg></span>';
}

/* What the tailored CV changed from the one it was copied from. The editor
   has been marking these field by field since lineage was recorded; this is
   the same list said once, on the application, where the question "what did
   I send them" gets asked. */
const DIFF_SHOWN=4;
async function fillBaseDiff(el,path){
  let d;
  try{ d=await api("/api/basediff?path="+encodeURIComponent(path)) }
  catch(e){ return }
  if(!el.isConnected||el.dataset.path!==path) return;
  const box=el.closest(".block")||el;
  const base=d.base?d.base.split("/").pop().replace(/\.ya?ml$/,""):null;
  if(!base) return;
  box.hidden=false;
  if(d.missing){
    el.innerHTML='<p class="bd-head">Copied from <b>'+esc(base)+'</b>, which is no longer '+
      'in the workspace, so there is nothing to compare it with.</p>';
    return;
  }
  const n=d.changes.length;
  const item=c=>'<li><span class="bd-where">'+esc(c.where)+
      (c.kind==="changed"?"":' <em class="bd-kind '+c.kind+'">'+c.kind+'</em>')+'</span>'+
    (c.before&&c.kind!=="added"?'<del data-noi18n>'+esc(c.before)+'</del>':'')+
    (c.after&&c.kind!=="removed"?'<ins data-noi18n>'+esc(c.after)+'</ins>':'')+'</li>';
  el.innerHTML=
    '<p class="bd-head">'+(n
      ?'<b>'+esc(t("{n} change(s)",{n}))+'</b> from <b>'+esc(base)+'</b>'
      :'Same as <b>'+esc(base)+'</b> so far. Nothing has been tailored yet.')+
    (d.design.length?'<span class="bd-design">Design: '+esc(d.design.join(", "))+'</span>':'')+
    '</p>'+
    (n?'<ul class="bd-list">'+d.changes.slice(0,DIFF_SHOWN).map(item).join("")+'</ul>':'')+
    (n>DIFF_SHOWN?'<details class="bd-more"><summary>'+(n-DIFF_SHOWN)+' more</summary>'+
      '<ul class="bd-list">'+d.changes.slice(DIFF_SHOWN).map(item).join("")+'</ul></details>':'');
}

/* Links out of the app. The desktop webview ignores target=_blank -- the
   click simply went nowhere -- so outside a browser tab the server opens the
   link in the system browser instead. In a browser tab the default works and
   is left alone. */
document.addEventListener("click",e=>{
  const a=e.target.closest&&e.target.closest('a[target="_blank"]');
  if(!a||!window.__TAURI__) return;
  const href=a.href||"";
  if(!/^https?:/i.test(href)) return;
  e.preventDefault();
  post("/api/open",{url:href}).catch(err=>toast(err.message,true));
});

const appliedAt=j=>{
  const h=j.status_history||[];
  for(const e of h) if(e.status==="applied") return e.at;
  return j.status==="pending"?null:j.created_at;
};
const isoToday=()=>new Date().toISOString().slice(0,10);

/* ---- view switching ---------------------------------------------------- */
/* The app has two kinds of screen -- a list you navigate, and one document you
   work on -- so the chrome has two shapes rather than six elements hidden
   independently of each other. The gap between them is what pins the cluster
   to the right edge, and that has to hold in both or the gear moves when you
   switch. */
function setView(v){
  S.view=v;
  const doc=v==="cvs";
  ["cvs","jobs","docs","funnel","cal","letter"].forEach(k=>{ $("#v-"+k).hidden = k!==v });
  if(v!=="cal") clearInterval(S.calTick);
  if(doc) paintBackLabel();
  else if(v==="letter") ltBackLabel();
  else $$("#nav button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.view===v)));
  if(v==="jobs"){ loadJobs(); loadAlerts() }
  /* Which application a document was written for is a fact about the jobs, so
     this screen needs them too -- and you can land on it without ever having
     opened the list. Draw what is known now, fill in the rest when it lands. */
  if(v==="docs"){ S.driftBy=null; drawDocuments(); if(!S.jready) loadJobs(true) }
  if(v==="funnel") loadFunnel();
  if(v==="cal") openCalendar();
  paintStatus();
}
/* Out of the editor, to the application the open document was written for.
   Derived from the link rather than remembered as history: a stack can go
   stale and this cannot, and "this document belongs to Acme" is the relation
   that is actually true. With nothing linked it is the list you left, which
   A document written for no application lives in Documents, however it was
   opened. */
function goBack(){
  const j=linkedJob();
  if(j){ setView("jobs"); selectJob(j.id); return }
  setView("docs");
}
/* The crumb says where it goes -- the application the document was written
   for, or Documents -- and that tab stays lit while you are in the editor. */
function paintBackLabel(){
  const to=linkedJob()?"jobs":"docs";
  $("#back").textContent = to==="docs" ? "Documents" : "Applications";
  $$("#nav button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.view===to)));
}
$("#back").onclick=goBack;
$$("#nav button").forEach(b=>b.onclick=()=>{ closeOverlays(); setView(b.dataset.view) });

/* ---- status bar --------------------------------------------------------- */
function paintStatus(){
  const L=$("#st-left"), R=$("#st-right");
  if(S.view==="cvs"){
    const bits=[];
    if(S.renderMs!=null) bits.push("rendered "+(S.renderMs/1000).toFixed(2)+"s");
    if(S.live==="working") bits.push("rendering…");
    else if(S.live==="bad") bits.push(S.liveMsg||"not valid yet");
    if(S.dirty) bits.push("unsaved changes");
    else if(S.savedAt) bits.push("saved "+ago(S.savedAt)+" ago");
    L.className=S.live==="bad"||S.dirty?"warn":"";
    L.textContent=bits.join(" · ")||"ready";
    L.classList.add("mono");
    /* The right of the status bar is where the workspace lives, and what Claude
       last did in it belongs in the same place -- it is the other thing acting
       on these files. It gives way to the path once it goes stale. */
    /* A resolved conflict outranks the raw activity line: after you choose,
       the footer has to report your decision, not keep reporting their edit. */
    const done=S.resolved&&(Date.now()-S.resolved.at)<900000?S.resolved:null;
    if(done){
      R.classList.add("said");
      R.textContent=done.kept==="mine"
        ? "You kept your version over "+done.who+"'s change"
        : "You took "+done.who+"'s version";
      R.title="";
      return;
    }
    R.classList.remove("said");
    R.classList.toggle("blocked",!!(S.pulse&&S.pulse.mcp&&
      S.pulse.mcp.last&&S.pulse.mcp.last.ok===false));
    const last=S.pulse&&S.pulse.mcp&&S.pulse.mcp.last;
    R.textContent=last&&(Date.now()/1000-last.at)<900
      ? clientName(last)+" · "+(last.ok===false?"refused ":"")+last.tool+(last.path?" · "+last.path:"")+
        " · "+ago(last.at*1000)+" ago"
      : shortPath((S.state&&S.state.workspace)||"");
    R.title=(S.state&&S.state.workspace)||"";
  }else if(S.view==="jobs"){
    L.className="mono";
    L.textContent=S.jobs.length+" application"+(S.jobs.length===1?"":"s")+
      (S.jsel?" · 1 selected":"");
    R.textContent="applications.db";
  }else if(S.view==="docs"){
    /* The applications view names the store its rows live in; these rows live
       in the workspace folder, so that is what belongs in the same slot. */
    const n=((S.state&&S.state.documents)||[]).length;
    L.className="mono";
    L.textContent=n+" document"+(n===1?"":"s");
    R.textContent=(S.state&&S.state.workspace)||"";
  }else{
    /* The chart says how to use it, in its own header; the footer only says
       which applications it is drawn from. */
    L.className="mono";
    L.textContent=S.funnel?S.funnel.totals.total+" applications":"";
    R.textContent="applications.db";
  }
}
function ago(t){
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<60) return s+"s";
  if(s<3600) return Math.round(s/60)+"m";
  return Math.round(s/3600)+"h";
}
setInterval(()=>{ if(S.view==="cvs"&&!S.dirty&&S.savedAt) paintStatus() },10000);

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
  unknown:"Checking…",
};
const aiClient=id=>(S.ai||[]).find(c=>c.id===id)||null;
/* Every tool call now says which client made it, so naming one is a lookup
   rather than a deduction. loneClient stays as the answer for a call recorded
   before any of this existed, and for a client that names itself something
   nobody here recognises. */
function loneClient(){
  const on=(S.ai||[]).filter(c=>c.state==="connected");
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
      (c&&st==="connected"&&c.last_seen
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
  unknown:"Checking",
};
/* "Configured" and "Connected" are different claims and the card should not
   make the second on the strength of the first. */
const aiPill=c=>c.state==="connected"&&!c.last_seen?"Configured":AI_PILL[c.state];
function aiSay(c){
  if(c.state==="connected"){
    /* A config file says a client has been *told* where the server is, not
       that it ever started it. A tool call is the only evidence the handshake
       actually happened, so the card reports that instead of implying it. */
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
  if(c.state==="absent") return "Not set up yet. The steps are below.";
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
  if(c.state==="unreadable") return "";
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
    const wrong=st==="elsewhere"||st==="other-workspace"||st==="unreadable";
    return '<div class="client'+(wrong?" wrong":"")+'" data-client="'+c.id+'">'+
      '<span class="badge"><svg width="19" height="19" viewBox="0 0 24 24"'+
        ' aria-hidden="true"><use href="#'+c.id+'-mark"/></svg></span>'+
      '<div class="who"><b>'+esc(c.label)+'</b>'+
        '<span class="pill" data-state="'+(st==="connected"&&!c.last_seen?"unknown":st)+
          '"><i></i>'+aiPill(c)+'</span></div>'+
      '<div class="say">'+esc(aiSay(c))+'</div>'+
      '<div class="go"><button class="obtn" data-connect="'+c.id+'">'+
        (st==="connected"?"Set up again"
          :st==="absent"?"Add to its config":"Point it at this workspace")+
        '</button></div>'+
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
      toast(r.action==="unchanged" ? t("Already set up.")
        : t("{c} is connected.",{c:c?c.label:"CV Studio"})+" "+tx(r.restart||""));
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
/* Claude Code already reads these off disk; the desktop app cannot, so the
   button packages them for upload rather than pretending to install them. */
async function loadSkills(){
  try{ S.skills=await api("/api/skills") }catch(e){ S.skills=null }
  paintSkills();
}
function paintSkills(){
  const d=S.skills, list=(d&&d.skills)||[];
  $("#s-skills").innerHTML=list.length
    ? list.map(k=>'<div><span class="nm">'+esc(k.name)+'</span>'+
        '<span class="ds">'+esc(k.description)+'</span>'+
        '<span class="tag'+(k.needs_mcp?" mcp":"")+'">'+
        (k.needs_mcp?"needs the tools":"travels as is")+'</span></div>').join("")
    : '<div><span class="ds">None found'+(d?" in "+esc(d.source):"")+
      '. They come with the Claude Code setup.</span></div>';
  const packed=list.some(k=>k.packaged);
  $("#s-skill-show").hidden=!packed;
  $("#s-skill-steps").hidden=!packed;
  const pack=$("#s-skill-pack");
  pack.disabled=!list.length;
  pack.textContent=packed?"Package again":"Package for Claude Desktop";
  pack.onclick=async()=>{
    pack.disabled=true; pack.textContent="Packaging…";
    try{
      const r=await post("/api/skills/package",{});
      await loadSkills();
      toast(t("{n} skill(s) ready to upload in {dir}",{n:r.skills.length,dir:r.dir}));
    }catch(e){ toast(e.message,true); await loadSkills() }
  };
  $("#s-skill-show").onclick=async()=>{
    try{ await post("/api/reveal",{path:(S.skills&&S.skills.out_dir)||""}) }
    catch(e){ toast(e.message,true) }
  };
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
  hermes:"Hermes",ai:"An AI client",you:"You"};
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
}

/* The base says so. A tailored copy has always said what it came from; the
   document it came from said nothing, so the one CV every other one is copied
   from looked like any other file while you edited it. */
const isBase=p=>!!(p&&S.state&&S.state.base&&S.state.base.path===p);
function paintBaseChip(){
  const chip=$("#basechip");
  if(!chip) return;
  if(!isBase(S.path)){ chip.hidden=true; return }
  const n=((S.state&&S.state.documents)||[]).filter(d=>d.base===S.path).length;
  chip.innerHTML='<span class="btag">Base CV</span>'+
    '<span>'+(n?'<b>'+n+'</b> tailored from it':'every tailored CV starts as a copy of it')+
    '</span>';
  chip.title="Changes here reach the next CV you tailor, not the ones already "+
    "copied. Click to see every document.";
  chip.hidden=false;
  chip.onclick=()=>{
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
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
  if(!j){
    chip.innerHTML='<span>Link to an application</span>';
    chip.title="Attach this document to the application it was written for.";
    chip.hidden=false;
    chip.onclick=()=>linkJobSheet();
    return;
  }
  chip.innerHTML='<span class="dot '+statusTone(j.status)+'"></span>'+
    '<span>for <b>'+esc(j.company)+'</b></span>'+
    '<span class="dot"></span><span>'+esc(prettyStatus(j.status))+'</span>';
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
      '<span><i class="fromb"></i> <b>'+pv.from_base.length+
        '</b> differ from base</span>');
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
    '<div class="foot"><button class="sbtn primary" data-cancel>Close</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
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
  if(S.view==="cvs") paintStatus();
  if(!$("#ovl-settings").hidden&&!$("#sp-ai").hidden) paintAILog();
  if(!before) return;

  /* An application changed in another process, which means an AI client moved
     a status while the table was open. Reload rather than leaving it stale:
     without this the row still reads "applied" until the user navigates. */
  if(before.jobs!==p.jobs){
    loadJobs(true);
    loadAlerts();
  }

  /* A new mark without a new file: an AI client can change the base CV this
     one is compared against, which moves what "differs from the base" means
     here without touching this file at all. */
  if(before.edits!==p.edits&&S.path&&!S.dirty){
    try{ setProv((await api("/api/doc?path="+encodeURIComponent(S.path))).prov);
         buildOutline(); buildInspector(); }catch(e){}
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
  await openDoc(S.path);
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
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){
    if(!$("#sheet").hidden) return closeSheet();
    if(!$("#ovl-design").hidden||!$("#ovl-settings").hidden) return closeOverlays();
    if(S.view==="cvs"&&!$("#ed").hidden){ e.preventDefault(); return closeEditor() }
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
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="s"){ e.preventDefault(); save() }
});
window.addEventListener("beforeunload",e=>{if(S.dirty){e.preventDefault();e.returnValue=""}});

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
const atsReadable=t=>esc(t).replace(/[-]/g,'<i class="pua" title="An icon, read as an unreadable character">□</i>');
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
    '<p class="onb-lede">A CV editor that shows you the page, and an application tracker '+
      'beside it. An AI client can read and write both, if you connect one.</p>'+
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
      ["pdf","PDF","Import a PDF of your CV","Contact details come out exactly. Jobs, "+
        "education and skills are sorted into sections, and a connected AI client can tidy "+
        "what the rules miss."],
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
        "saved as PDF.":"A PDF of your CV, your LinkedIn profile saved as PDF, or your "+
        "LinkedIn data archive (.zip).")+'</span>'+
      (OB.impErr?'<span class="err" role="alert">'+esc(OB.impErr)+'</span>':'')+
      '<button class="onb-btn" id="onb-pick"'+(OB.importing?" disabled":"")+'>Choose a file…</button>'+
      '<small>Read on this machine. Nothing is uploaded.</small>'+
      '<input type="file" id="onb-file" accept=".pdf,.zip,application/pdf,application/zip" hidden>'+
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


/* =========================================================================
   Cover letters
   ========================================================================= */
/* A letter is a Markdown file laid out by the app itself, in the look of the
   CV it names. It is written on the page: the body is editable where it
   prints, the letterhead comes from the CV, and place, date, language and the
   CV it looks like are set beside it. The PDF tab is the exact render. */
const LT={path:null, doc:null, meta:{}, body:"", dirty:false, tab:"write", pages:null,
  png:null, rendering:false, mdText:null, t:null};
const isLetterPath=p=>/\.md$/i.test(p||"");

async function openLetter(path){
  if(path) palRemember({doc:path});
  closeOverlays();
  try{
    const d=await api("/api/doc?path="+encodeURIComponent(path));
    Object.assign(LT,{path, doc:d, meta:Object.assign({},d.meta), body:d.body, dirty:false,
      tab:"write", pages:null, png:null, mdText:null});
  }catch(e){ return toast(e.message,true) }
  setView("letter");
  ltPaint();
  ltRender();
}
function ltJob(){
  const id=LT.meta.application;
  return (S.jobs||[]).find(j=>j.id===id||j.letter_path===LT.path)||null;
}
function ltBackLabel(){
  const to=ltJob()?"jobs":"docs";
  $("#lt-back").textContent=to==="docs"?"Documents":"Applications";
  $$("#nav button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.view===to)));
}
$("#lt-back").onclick=()=>{
  if(LT.dirty&&!confirm("This letter has unsaved changes. Leave without saving?")) return;
  LT.path=null;
  const j=ltJob();
  if(j){ setView("jobs"); selectJob(j.id); return }
  setView("docs");
};

/* The Markdown a letter uses, as HTML for the page and back. Paragraphs,
   '- ' lists, **bold**, *italic*, [links](url): what letters.py prints. */
function ltInline(t){
  let out="", pos=0;
  const re=/\*\*(.+?)\*\*|__(.+?)__|\*(.+?)\*|_(.+?)_|\[([^\]]+)\]\(([^)\s]+)\)/g; let m;
  while((m=re.exec(t))){
    out+=esc(t.slice(pos,m.index));
    if(m[1]||m[2]) out+="<b>"+ltInline(m[1]||m[2])+"</b>";
    else if(m[3]||m[4]) out+="<i>"+ltInline(m[3]||m[4])+"</i>";
    else out+='<a href="'+esc(m[6])+'">'+ltInline(m[5])+"</a>";
    pos=re.lastIndex;
  }
  return out+esc(t.slice(pos));
}
function ltToHTML(body){
  return String(body||"").trim().split(/\n\s*\n/).filter(c=>c.trim()).map(c=>{
    const lines=c.split("\n").filter(l=>l.trim());
    if(lines.every(l=>/^\s*[-*•]\s+/.test(l)))
      return "<ul>"+lines.map(l=>"<li>"+ltInline(l.replace(/^\s*[-*•]\s+/,"").trim())+"</li>").join("")+"</ul>";
    return "<p>"+ltInline(lines.map(l=>l.trim()).join(" "))+"</p>";
  }).join("")||"<p><br></p>";
}
function ltInlineMD(node){
  let out="";
  node.childNodes.forEach(n=>{
    if(n.nodeType===3){ out+=n.nodeValue.replace(/ /g," "); return }
    if(n.nodeType!==1) return;
    const t=n.tagName, inner=ltInlineMD(n);
    const fw=n.style&&(n.style.fontWeight==="bold"||Number(n.style.fontWeight)>=600);
    const it=n.style&&n.style.fontStyle==="italic";
    if(t==="BR") out+=" ";
    else if(!inner.trim()) out+=inner;
    else if(t==="B"||t==="STRONG"||fw) out+="**"+inner.trim()+"**"+(/\s$/.test(inner)?" ":"");
    else if(t==="I"||t==="EM"||it) out+="*"+inner.trim()+"*"+(/\s$/.test(inner)?" ":"");
    else if(t==="A") out+="["+inner+"]("+(n.getAttribute("href")||"")+")";
    else out+=inner;
  });
  return out;
}
function ltToMD(root){
  const out=[];
  const blockOf=n=>{
    if(n.nodeType===3){ const t=n.nodeValue.trim(); if(t) out.push(t); return }
    if(n.nodeType!==1) return;
    if(n.tagName==="UL"||n.tagName==="OL"){
      const items=[...n.querySelectorAll(":scope>li")].map(li=>"- "+ltInlineMD(li).replace(/\s+/g," ").trim())
        .filter(x=>x!=="- ");
      if(items.length) out.push(items.join("\n"));
      return;
    }
    if(/^(P|DIV|H\d)$/.test(n.tagName)&&n.querySelector("ul,ol,p,div")){ n.childNodes.forEach(blockOf); return }
    const t=ltInlineMD(n).replace(/\s+/g," ").trim();
    if(t) out.push(t);
  };
  root.childNodes.forEach(blockOf);
  return out.join("\n\n");
}

/* Lengths in the letter's own units, drawn at the width the page is shown. */
const LT_PAPER={"a4":[210,297],"a5":[148,210],"us-letter":[215.9,279.4],"us-executive":[184.15,266.7]};
function ltMM(v){
  const m=String(v||"").trim().match(/^([\d.]+)\s*(cm|mm|in|pt)?$/); if(!m) return 20;
  const n=parseFloat(m[1]);
  return {cm:n*10,mm:n,in:n*25.4,pt:n*0.3528}[m[2]||"cm"];
}
function ltFont(fam){
  if(!fam) return;
  const id="cvfont-"+fam.replace(/\W+/g,"-");
  if(document.getElementById(id)) return;
  const l=document.createElement("link");
  l.id=id; l.rel="stylesheet"; l.href="/cvfont/"+encodeURIComponent(fam)+".css";
  document.head.append(l);
}

function ltPaint(){
  const d=LT.doc, h=(d&&d.head)||{}, j=ltJob();
  const bar=$("#lt-bar"); if(bar) bar.remove();
  $("#lt-title").textContent=t("Cover letter")+(j?" · "+j.company:LT.meta.company?" · "+LT.meta.company:"");
  $("#lt-file").textContent=LT.path.split("/").pop();
  ltBackLabel();
  $$("#lt-tabs [data-lt]").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.lt===LT.tab)));
  $("#lt-save").disabled=!LT.dirty;
  const st=$("#lt-stage");
  if(LT.tab==="pdf"){
    st.innerHTML=LT.png&&LT.png.length?'<div class="lt-pdf">'+LT.png.map((u,i)=>
      '<img alt="Page '+(i+1)+' of the letter" style="width:min(720px,100%)" src="'+esc(u+tok())+'">').join("")+'</div>'
      :'<p class="sp-note">'+(LT.rendering?"Laying it out…":"Not rendered yet.")+'</p>';
  }else if(LT.tab==="md"){
    const text=LT.mdText!=null?LT.mdText:(LT.dirty?null:d.text);
    st.innerHTML='<textarea class="lt-md" id="lt-md" spellcheck="true" aria-label="The letter as Markdown"></textarea>';
    const ta=$("#lt-md");
    ta.value=text!=null?text:"Saving…";
    if(text==null) ltSave().then(()=>{ ta.value=LT.doc.text });
    ta.oninput=()=>{ LT.mdText=ta.value; LT.dirty=true; $("#lt-save").disabled=false };
  }else{
    ltFont(h.font); ltFont(h.name_font);
    const [wmm,hmm]=LT_PAPER[h.paper]||LT_PAPER.a4;
    const W=Math.min(700,Math.max(480,st.clientWidth-60)), u=W/wmm, pt=u*0.3528;
    const mg=h.margins||{}, to=LT.meta.to;
    const toLines=Array.isArray(to)?to:String(to||"").split("\n").filter(x=>x.trim());
    st.innerHTML='<div class="lt-paper" id="lt-paper" style="width:'+W+'px;min-height:'+(hmm*u)+'px;'+
      'padding:'+(ltMM(mg.top)*u)+'px '+(ltMM(mg.right)*u)+'px '+(ltMM(mg.bottom)*u)+'px '+(ltMM(mg.left)*u)+'px;'+
      'box-sizing:border-box;font-family:\''+esc(h.font||"Source Sans 3")+'\',sans-serif;font-size:'+(10.5*pt)+'px;'+
      'line-height:1.52;color:'+esc(h.body_color||"#000")+'">'+
      '<div class="lh" tabindex="0" role="link" aria-label="'+esc(t("Letterhead, from {cv}. Opens that CV.",{cv:h.cv||t("the CV")}))+'" id="lt-lh">'+
        '<span class="tag">From '+esc((h.cv||"the CV").split("/").pop().replace(/\.ya?ml$/,""))+' · <u>change it there</u></span>'+
        '<div style="font-family:\''+esc(h.name_font||h.font)+'\',sans-serif;font-size:'+(24*pt)+'px;line-height:1.1;'+
          'font-weight:'+(h.name_bold?700:400)+';color:'+esc(h.name_color)+'">'+esc(h.name)+'</div>'+
        (h.headline?'<div style="font-size:'+(11*pt)+'px;color:'+esc(h.headline_color)+'">'+esc(h.headline)+'</div>':'')+
        '<div style="margin-top:'+(4*pt)+'px;font-size:'+(9.5*pt)+'px;color:'+esc(h.contact_color)+'">'+
          (h.contact||[]).map(esc).join(' &nbsp;•&nbsp; ')+'</div>'+
        '<div style="margin-top:'+(6*pt)+'px;border-top:'+Math.max(1,0.6*pt)+'px solid '+esc(h.rule_color)+'"></div>'+
      '</div>'+
      (toLines.length?'<div style="margin-top:'+(16*pt)+'px">'+toLines.map(esc).join("<br>")+'</div>':'')+
      '<div style="margin-top:'+(16*pt)+'px;text-align:right;color:'+esc(h.contact_color)+'" title="Set the place and date beside the letter">'+
        esc(LT.doc.date_line||"")+'</div>'+
      '<div class="subj" id="lt-subj" contenteditable="true" spellcheck="true" data-ph="Subject" '+
        'aria-label="Subject" style="margin-top:'+(18*pt)+'px;font-weight:700">'+esc(LT.meta.subject||"")+'</div>'+
      '<div class="ltbody" id="lt-edit" contenteditable="true" spellcheck="true" aria-label="The letter" '+
        'style="margin-top:'+(10*pt)+'px;display:flex;flex-direction:column;gap:'+(8*pt)+'px;text-align:justify">'+
        ltToHTML(LT.body)+'</div>'+
      '<div style="margin-top:'+(24*pt)+'px;font-weight:700">'+esc(h.name)+'</div>'+
    '</div>';
    try{ document.execCommand("styleWithCSS",false,false); document.execCommand("defaultParagraphSeparator",false,"p") }catch(e){}
    const ed=$("#lt-edit"), sj=$("#lt-subj");
    ed.oninput=()=>ltChanged();
    sj.oninput=()=>{ LT.meta.subject=sj.textContent.replace(/\s+/g," ").trim(); ltDirty() };
    sj.onkeydown=e=>{ if(e.key==="Enter"){ e.preventDefault(); ed.focus() } };
    [ed,sj].forEach(el=>el.onpaste=e=>{ e.preventDefault();
      document.execCommand("insertText",false,(e.clipboardData||window.clipboardData).getData("text/plain")) });
    ed.onkeydown=e=>{
      if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="k"){ e.preventDefault(); ltLink() }
    };
    const lh=$("#lt-lh"), open=()=>{ if(h.cv){
      if(LT.dirty&&!confirm("This letter has unsaved changes. Leave without saving?")) return;
      LT.path=null; openDoc(h.cv) } };
    lh.onclick=open; lh.onkeydown=e=>{ if(e.key==="Enter") open() };
    ltPageMark();
  }
  ltPanel();
}
/* Where the first page ends, drawn on the sheet, so a letter that runs over
   says so while it is being written. */
function ltPageMark(){
  const paper=$("#lt-paper"); if(!paper) return;
  paper.querySelectorAll(".pgend").forEach(x=>x.remove());
  const h=LT.doc.head||{}, [wmm,hmm]=LT_PAPER[h.paper]||LT_PAPER.a4;
  const u=paper.offsetWidth/wmm, pageH=hmm*u-ltMM((h.margins||{}).bottom)*u;
  if(paper.scrollHeight>hmm*u+2){
    const m=document.createElement("div"); m.className="pgend"; m.style.top=pageH+"px";
    m.innerHTML="<span>End of page 1</span>"; paper.append(m);
  }
}
function ltChanged(){
  LT.body=ltToMD($("#lt-edit"));
  ltDirty();
  clearTimeout(LT.t); LT.t=setTimeout(ltPageMark,120);
}
function ltDirty(){
  LT.dirty=true; $("#lt-save").disabled=false;
  const w=$("#lt-words"); if(w) w.textContent=ltWords(LT.body);
  const m=$("#lt-meterfill"); if(m) m.style.width=Math.min(100,ltWords(LT.body)/350*100)+"%";
}
const ltWords=b=>(String(b||"").replace(/\[([^\]]+)\]\([^)]+\)/g,"$1").match(/[\p{L}\p{N}'’-]+/gu)||[]).length;

function ltPanel(){
  const j=ltJob(), n=ltWords(LT.body), m=LT.meta, docs=(S.state&&S.state.documents)||[];
  const cvs=docs.filter(d=>d.group!=="Cover letters"&&!d.letter);
  const today=!m.date||m.date==="today";
  const lang=m.language||"en";
  const fits=LT.pages==null?(LT.rendering?"Checking the page…":"Not laid out yet.")
    :LT.pages===1?"Fits on one page.":"Runs to "+LT.pages+" pages. Letters read best on one.";
  const scaffold=/Open with something only you could write|Commencez par ce que vous seul/.test(LT.body);
  const say="In CV Studio, write the cover letter "+LT.path+(j?" for my "+j.company+" application, against its posting.":".");
  $("#lt-panel").innerHTML=
    '<div style="display:flex;flex-direction:column;gap:8px"><h4>Length</h4>'+
      '<span><span class="big" id="lt-words">'+n+'</span> <span class="muted2">of about 350 words</span></span>'+
      '<div class="lt-meter'+(n>420?" over":"")+'"><i id="lt-meterfill" style="width:'+Math.min(100,n/350*100)+'%"></i></div>'+
      '<span class="muted2">'+fits+'</span></div>'+
    '<hr>'+
    '<div style="display:flex;flex-direction:column;gap:10px"><h4>This letter</h4><div class="fg">'+
      '<label>For</label><span>'+(j?'<b style="font-weight:600">'+esc(j.company)+'</b> · '+esc(j.title)
        :'<span class="muted2">No application</span>')+'</span>'+
      '<label for="lt-cv">Letterhead from</label><select id="lt-cv">'+cvs.map(d=>'<option value="'+esc(d.path)+'"'+
        (d.path===(m.looks_like||(LT.doc.head||{}).cv)?" selected":"")+'>'+esc(d.label)+'</option>').join("")+'</select>'+
      '<label for="lt-lang">Language</label><select id="lt-lang">'+((S.state&&S.state.languages)||[]).map(l=>
        '<option value="'+l.code+'"'+(l.code===lang?" selected":"")+'>'+esc(l.native)+'</option>').join("")+'</select>'+
      '<label for="lt-place">Written from</label><input id="lt-place" value="'+esc(m.place||"")+'" placeholder="City">'+
      '<label for="lt-date">Date</label><span class="today"><label style="display:flex;gap:5px;align-items:center;color:var(--t800)">'+
        '<input type="checkbox" id="lt-today"'+(today?" checked":"")+'>Today</label>'+
        '<input type="date" id="lt-date" value="'+(today?"":esc(String(m.date)))+'"'+(today?" hidden":"")+'></span>'+
      '<label for="lt-to">Addressed to</label><textarea id="lt-to" placeholder="Optional. Printed above the date.">'+
        esc(Array.isArray(m.to)?m.to.join("\n"):(m.to||""))+'</textarea>'+
    '</div></div>'+
    '<hr>'+
    (scaffold?sayBox("To have your AI client write it, ask it:",say)+'<hr>':'')+
    '<div class="muted2" style="font-size:12.5px">Click anywhere in the letter and type. Select text for bold, '+
      'italic, a link or a list. The letterhead is the CV’s: change it there, and every letter that '+
      'looks like it follows.</div>';
  if(scaffold) wireSay(say);
  const set=(k,v)=>{ LT.meta[k]=v; ltDirty(); ltSave().then(()=>{ if(LT.tab==="write") ltPaint() }) };
  $("#lt-cv").onchange=e=>set("looks_like",e.target.value);
  $("#lt-lang").onchange=e=>set("language",e.target.value);
  $("#lt-place").onchange=e=>set("place",e.target.value.trim()||null);
  $("#lt-today").onchange=e=>{ const d=$("#lt-date"); d.hidden=e.target.checked;
    if(!d.hidden&&!d.value) d.value=new Date().toISOString().slice(0,10);
    set("date",e.target.checked?"today":($("#lt-date").value||new Date().toISOString().slice(0,10))) };
  $("#lt-date").onchange=e=>set("date",e.target.value||"today");
  $("#lt-to").onchange=e=>{ const v=e.target.value.split("\n").map(x=>x.trim()).filter(Boolean);
    set("to",v.length?v:null) };
}

async function ltSave(){
  if(!LT.path) return;
  const body=LT.tab==="md"&&LT.mdText!=null?{path:LT.path,text:LT.mdText}
    :{path:LT.path,meta:LT.meta,body:LT.body};
  try{
    const r=await post("/api/save",body);
    LT.doc=r; LT.meta=Object.assign({},r.meta); LT.body=r.body; LT.mdText=null; LT.dirty=false;
    $("#lt-save").disabled=true;
    ltRender();
  }catch(e){ toast(e.message,true) }
}
/* Laid out after every save, so the page count and the PDF tab are always
   the letter as it stands. */
async function ltRender(){
  if(!LT.path) return;
  LT.rendering=true;
  try{
    const r=await post("/api/render",{path:LT.path});
    if(r.ok){ LT.pages=r.pages; LT.png=r.pngs }
    else toast(r.hint||"The letter did not lay out.",true);
  }catch(e){}
  LT.rendering=false;
  if(S.view==="letter"){ if(LT.tab==="pdf") ltPaint(); else ltPanel() }
  S.docThumbs[LT.path]=null;
}
$("#lt-save").onclick=()=>ltSave().then(()=>toast("Saved"));
$$("#lt-tabs [data-lt]").forEach(b=>b.onclick=async()=>{
  if(b.dataset.lt===LT.tab) return;
  if(LT.tab==="md"&&LT.mdText!=null) await ltSave();
  LT.tab=b.dataset.lt; ltPaint();
});
document.addEventListener("keydown",e=>{
  if(S.view!=="letter") return;
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="s"){ e.preventDefault(); ltSave().then(()=>toast("Saved")) }
});

/* The selection's own bar: the four things a letter prints. */
function ltLink(){
  const url=prompt("Link to","https://");
  if(url&&url!=="https://") document.execCommand("createLink",false,url.trim());
  ltChanged();
}
document.addEventListener("selectionchange",()=>{
  let bar=$("#lt-bar");
  const sel=document.getSelection(), ed=$("#lt-edit");
  const inside=S.view==="letter"&&LT.tab==="write"&&ed&&sel.rangeCount&&!sel.isCollapsed&&
    ed.contains(sel.anchorNode)&&ed.contains(sel.focusNode);
  if(!inside){ if(bar) bar.remove(); return }
  const r=sel.getRangeAt(0).getBoundingClientRect();
  if(!bar){
    bar=document.createElement("div"); bar.id="lt-bar"; bar.className="lt-bar";
    bar.setAttribute("role","toolbar"); bar.setAttribute("aria-label","Format the selection");
    bar.innerHTML='<button data-c="bold" title="Bold (⌘B)"><b>B</b></button>'+
      '<button data-c="italic" title="Italic (⌘I)"><i style="font-family:Georgia,serif">I</i></button>'+
      '<button data-c="link" title="Link (⌘K)">Link</button>'+
      '<button data-c="insertUnorderedList" title="Bullet list">• List</button>';
    bar.onmousedown=e=>e.preventDefault();
    bar.onclick=e=>{ const b=e.target.closest("[data-c]"); if(!b) return;
      if(b.dataset.c==="link") return ltLink();
      document.execCommand(b.dataset.c); ltChanged() };
    document.body.append(bar);
  }
  bar.style.left=Math.max(8,r.left+r.width/2-90)+"px";
  bar.style.top=Math.max(60,r.top-42)+"px";
  bar.querySelector('[data-c=bold]').setAttribute("aria-pressed",String(document.queryCommandState("bold")));
  bar.querySelector('[data-c=italic]').setAttribute("aria-pressed",String(document.queryCommandState("italic")));
});

/* Export: the PDF to attach, Word for recruiters who ask for it, plain text to
   paste into a form. */
$("#lt-export").onclick=e=>{
  e.stopPropagation();
  let m=$("#lt-menu"); if(m){ m.remove(); return }
  m=document.createElement("div"); m.className="lt-menu"; m.id="lt-menu"; m.setAttribute("role","menu");
  m.innerHTML=[["pdf","PDF","To attach to the application"],["docx","Word (.docx)","For recruiters who ask for one"],
    ["txt","Plain text","To paste into a form's cover letter box"]].map(([f,t,x])=>
    '<button role="menuitem" data-f="'+f+'">'+t+'<small>'+x+'</small></button>').join("");
  $("#lt-export").parentElement.append(m);
  m.onclick=async ev=>{ const b=ev.target.closest("[data-f]"); if(!b) return; m.remove();
    if(LT.dirty) await ltSave();
    window.open("/api/letter/export?path="+encodeURIComponent(LT.path)+"&format="+b.dataset.f+tok()) };
  setTimeout(()=>document.addEventListener("click",function off(){ m.remove();
    document.removeEventListener("click",off) }),0);
};
/* A letter an AI client is writing lands here as it writes, unless there are
   edits here it would overwrite. */
async function ltExternal(stamp){
  if(S.view!=="letter"||!LT.path||LT.dirty||!stamp||!LT.doc||stamp<=LT.doc.mtime+0.001) return;
  try{
    const d=await api("/api/doc?path="+encodeURIComponent(LT.path));
    Object.assign(LT,{doc:d, meta:Object.assign({},d.meta), body:d.body});
    ltPaint(); ltRender();
  }catch(e){}
}
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
    return String(it||"").split(/\s+/).slice(0,4).join(" ")||("item "+(i+1));
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
        const label=tr?langOf(d.lang).native:other?String(d.label).replace(/\.[a-z]{2}(-[a-z]{2})?$/i,""):d.label;
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
          markHTML(d.ai,null,true)+
          (job?'<span class="tie" title="'+esc(t("Linked to"))+' '+
            esc(job.title+" · "+job.company)+'"></span>':"")+
          '<span class="ct mono">'+(pp?pp+"pp":"")+'</span></button>';
      }).join("");
      /* Only worth naming the groups once there is more than one of them. */
      return (groups.length>1
        ? '<div class="rail-sub">'+esc(g==="My CVs"?"CVs":g)+'</div>' : "")+rows;
    }).join("")+newRow;
  }
  $$("#doclist [data-path]").forEach(b=>b.onclick=()=>{
    if(b.dataset.path===S.path) return;
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
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
  S.prov=null; $("#provchip").hidden=true;
  S.render=null; S.renderMs=null; S.fill=null; S.themePages={}; S.zoomAuto=true;
  hideExternalChange();
  /* The Design panel still holds the last document's controls, and its inputs
     are read straight into the patch list. Empty it until it is rebuilt. */
  $("#dz-advanced").innerHTML=""; S.thumbs=null;
  $("#pane-page").innerHTML='<div class="skel" style="width:472px;height:668px"></div>';
  $("#btn-render").disabled=false;
  renderDocs(S.state.documents);
  try{
    const doc=await api("/api/doc?path="+encodeURIComponent(path));
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
  }catch(e){ toast(e.message,true) }
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
const touch=()=>{ S.dirty=true; paintStatus(); scheduleLive() };

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
  return d?d.label:String(p||"").split("/").pop();
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
  if(!confirm('Remove "'+entryTitle(list[at],at)+'" from '+sectionLabel(name)+"?"+
    (last?"\n\nIt is the only entry, so the section goes with it: RenderCV "+
          "reads a section's type from its entries and cannot render an empty "+
          "one.":"")+
    "\n\nThis is written to the file straight away."))
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
  $("#split").hidden=tab==="page";
  if(tab==="yaml"){ paint(); markYamlSelection() }
  if(tab==="form") buildForm();   /* re-read the model, in case the page moved on */
  trackSelectionOnPage();         /* the selection may have moved while away */
  refit();                        /* the page just got a different amount of room */
  setPref("tab",tab);
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
async function save(ops){
  if(!S.path||S.busy) return;
  S.busy=true; $("#btn-render").disabled=true;
  try{
    const body=S.tab==="yaml" ? {path:S.path,yaml:$("#yaml").value}
                              : {path:S.path,patches:collectPatches()};
    if(ops&&ops.length) body.ops=ops;
    const r=await post("/api/save",body);
    S.doc=r; S.data=r.data?JSON.parse(JSON.stringify(r.data)):null; paintLang(); paintPhotoChip();
    /* Our own write, so take its timestamp: the poll must not read it back as
       somebody else having changed the file. */
    S.docMtime=r.mtime; hideExternalChange();
    S.dirty=false; S.savedAt=Date.now();
    setProv(r.prov);
    $("#yaml").value=r.yaml; paint(); setYamlError(r.parse_error);
    buildOutline(); buildInspector(); if(S.tab==="form") buildForm();
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
  $("#pane-page").innerHTML='<div class="skel" style="width:472px;height:668px"></div>';
  try{
    const r=await post("/api/render",{path:S.path});
    S.renderMs=Math.round(performance.now()-t0);
    if(!r.ok){
      S.pdf=null; $("#btn-pdf").disabled=true;
      $("#pane-page").innerHTML='<div class="err"><h4>This CV didn\'t render</h4>'+
        (r.hint?'<div class="hint">'+esc(r.hint)+'</div>':"")+
        '<pre>'+esc(r.error||"")+'</pre></div>';
      S.render=null; paintBudget(); paintStatus();
      return;
    }
    await adoptRender(r);
  }catch(e){
    S.render=null;
    $("#pane-page").innerHTML='<div class="err"><h4>Render failed</h4><pre>'+
      esc(e.message)+'</pre></div>';
  }
  paintStatus();
}

/* Every good render updates the same three things: the pages on screen, the
   page budget, and what we know about this document and this theme. */
async function adoptRender(r){
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
  });
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
  wrap.querySelectorAll(".pgmark").forEach(el=>el.remove());
  if(!S.prov) return;
  const pageH=img.naturalHeight/2;
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
      S.live="ok"; await adoptRender(r);
    }else{ S.live="bad"; S.liveMsg=r.hint||"not valid yet" }
  }catch(e){ if(token===liveToken){ S.live="bad"; S.liveMsg=e.message } }
  paintStatus();
}

$("#btn-pdf").onclick=()=>{ if(S.pdf)
  window.open("/api/asset?path="+encodeURIComponent(S.pdf)+tok()) };

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

/* =========================================================================
   Jobs
   ========================================================================= */
async function loadJobs(quiet){
  try{
    const d=await api("/api/jobs");
    S.jobs=d.jobs; S.statuses=d.statuses; S.nodes=d.nodes||{}; S.labels=d.labels||{};
    S.jready=true;
  }catch(e){
    S.jready=false;
    if(!quiet) $("#jobrows").innerHTML='<div class="empty"><h3>Could not load</h3><p>'+
      esc(e.message)+'</p></div>';
    return;
  }
  if(S.view==="jobs") drawJobs();
  if(S.view==="docs") drawDocuments();
  if(S.view==="cvs"&&S.path){
    paintTitle(); paintLink();
    if(!$("#ed").hidden) buildInspector();
  }
  /* The rail marks which documents belong to an application, so it has to
     be redrawn once we know what the applications are. */
  if(S.state) renderDocs(S.state.documents);
  paintStatus();
}

/* The sidebar is the filter. Statuses come from the store rather than a list
   written here, so a status added to jobs.py shows up without a UI change. */
function statusCounts(){
  const c={};
  S.jobs.forEach(j=>{ c[j.status]=(c[j.status]||0)+1 });
  return c;
}
const NO_LETTER=j=>!j.letter_path;
const SAVED={"No cover letter":NO_LETTER};

/* Attention is computed by the server, not here. The same four rules answer
   the desktop notification and the digest an AI client reads out, and three
   copies of "what counts as overdue" would have drifted apart within a month.
   Needs-follow-up used to live in SAVED above and is now one of them. */
const ATTENTION=[
  ["interview_soon",   "Interview soon"],
  ["followup_due",     "Follow-up due"],
  ["interview_passed", "Interview, no outcome"],
  ["silent",           "No reply"],
];

async function loadAlerts(){
  try{ S.alerts=await api("/api/alerts") }catch(e){ S.alerts=null; return }
  if(S.view==="jobs") drawRail();
  notifyAlerts();
}

/* ---- Notifications ---------------------------------------------------- */
/* Nothing outside this app knows when an interview is, so a reminder has to
   come from here. Opt-in, from Settings > Notifications: before each
   interview at the leads chosen there, and once a day for follow-ups, in one
   notification rather than one per application. Each is sent once: what was
   sent is remembered on this machine. They come while the app is open. */
const notifyOn=()=>!!prefs().notify;
const notifyIv=()=>{ const v=prefs().notify_iv??"60,10"; return v==="off"?[]:String(v).split(",").map(Number).sort((a,b)=>b-a) };
const notifyFu=()=>{ const v=prefs().notify_fu??"9"; return v==="off"?null:+v };
function notifyApi(){
  const N=window.__TAURI__&&window.__TAURI__.notification;
  if(N) return {
    granted:()=>N.isPermissionGranted(),
    ask:async()=>(await N.requestPermission())==="granted",
    send:(title,body)=>N.sendNotification({title,body})};
  if(!("Notification" in window)) return null;
  return {
    granted:async()=>Notification.permission==="granted",
    ask:async()=>(await Notification.requestPermission())==="granted",
    send:(title,body,onclick)=>{ const n=new Notification(title,{body,icon:"/static/brand-mark-256.png"});
      n.onclick=()=>{ window.focus(); if(onclick) onclick(); n.close() } }};
}
function notifySeen(){ return Object.assign({},prefs().notified||{}) }
function notifyMark(k){
  const m=notifySeen(), cut=Date.now()-14*864e5;
  for(const x in m) if(m[x]<cut) delete m[x];
  m[k]=Date.now(); setPref("notified",m);
}
async function notifySend(key,title,body,onclick){
  const A=notifyApi(); if(!A) return false;
  try{ if(!(await A.granted())) return false; A.send(title,body,onclick); if(key) notifyMark(key); return true }
  catch(e){ return false }
}
const inWords=ms=>{ const m=Math.max(1,Math.round(ms/6e4));
  if(m<60) return t(m===1?"in 1 minute":"in {n} minutes",{n:m});
  const h=Math.round(m/60);
  if(h<24) return t(h===1?"in 1 hour":"in {n} hours",{n:h});
  return t("tomorrow") };
async function notifyTick(){
  if(!notifyOn()||!S.jready) return;
  const now=Date.now(), seen=notifySeen();
  /* Interviews: the largest lead whose moment has come and not been sent.
     Opening the app half an hour before still gets one, saying so. */
  const leads=notifyIv();
  for(const j of S.jobs||[]){
    const at=interviewMoment(j); if(!at||at<=now||!leads.length) continue;
    const base="iv:"+j.id+":"+at.getTime()+":";
    const due=leads.filter(L=>now>=at-L*6e4);
    if(!due.length) continue;
    const L=due[due.length-1];
    if(seen[base+L]) continue;
    const ok=await notifySend(base+L,t("Interview {when} · {co}",{when:inWords(at-now),co:j.company}),
      j.title+" · "+interviewLine(j),()=>{ setView("jobs"); selectJob(j.id) });
    if(ok) due.forEach(x=>notifyMark(base+x));
  }
  /* Follow-ups: once a day, from the hour chosen. */
  const hr=notifyFu();
  if(hr!=null&&new Date().getHours()>=hr){
    const today=isoToday(), key="fu:"+today;
    if(!seen[key]){
      const list=(S.jobs||[]).filter(j=>j.followup_date&&!DEAD_ST.has(j.status)&&String(j.followup_date).slice(0,10)<=today);
      if(list.length){
        const late=list.filter(j=>String(j.followup_date).slice(0,10)<today).length;
        const names=list.map(j=>j.company); const shown=names.length>4?names.slice(0,4).join(", ")+" +"+(names.length-4):names.join(", ");
        await notifySend(key,list.length===1?t("Follow up with {co} today",{co:list[0].company})
            :t("{n} follow-ups to send",{n:list.length}),
          shown+(late?" · "+t(late===1?"1 is late":"{n} are late",{n:late}):""),
          ()=>{ S.jfilter={kind:"alert",value:"followup_due"}; setView("jobs") });
      }else notifyMark(key);
    }
  }
}
setInterval(notifyTick,30000);
async function notifyAlerts(){ notifyTick() }
/* Backups: when the last one was, one now, and putting one back. */
async function fillBackups(){
  const say=$("#s-bk-say"), now=$("#s-bk-now"), list=$("#s-bk-list");
  let r; try{ r=await api("/api/backups") }catch(e){ return }
  const last=r.backups[0];
  if(r.sample){ say.textContent=t("Sample data is not backed up; it is made fresh each time."); now.disabled=list.disabled=true; return }
  now.disabled=list.disabled=false;
  const relT=at=>{ const sec=(at*1000-Date.now())/1000, f=new Intl.RelativeTimeFormat(uiLocale(),{numeric:"auto"});
    const a=Math.abs(sec); return a<3600?f.format(Math.round(sec/60),"minute"):a<86400?f.format(Math.round(sec/3600),"hour")
      :f.format(Math.round(sec/86400),"day") };
  say.textContent=last?t("Last one {when}. {n} kept, beside the app.",{when:relT(last.at),n:r.backups.length})
    :t("A copy of this workspace every day, beside the app, the last fourteen kept.");
  list.disabled=!r.backups.length;
  now.onclick=async()=>{ now.disabled=true;
    try{ await post("/api/backups/new",{}); toast(t("Backed up")); fillBackups() }
    catch(e){ toast(e.message,true); now.disabled=false } };
  list.onclick=()=>{
    const kb=n=>n>1048576?(n/1048576).toFixed(1)+" MB":Math.max(1,Math.round(n/1024))+" KB";
    const when=a=>DTF(uiLocale(),{weekday:"short",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"}).format(new Date(a*1000));
    openSheet('<div><h3>'+t("Restore a backup")+'</h3><p>'+t("Its files are put back into the workspace. What is there now is backed up first, so this can be undone the same way.")+'</p></div>'+
      '<div class="bk-list">'+r.backups.map(b=>'<div class="bk-row"><span><b>'+esc(when(b.at))+'</b>'+
        '<small>'+esc(kb(b.size))+(/before-restore/.test(b.name)?' · '+t("before a restore"):/manual/.test(b.name)?' · '+t("made by hand"):'')+'</small></span>'+
        '<button class="sbtn" data-bk="'+esc(b.name)+'">'+t("Restore")+'</button></div>').join("")+'</div>'+
      '<p class="note muted mono" style="font-size:11.5px">'+esc(r.folder)+'</p>'+
      '<div class="foot"><div class="grow"></div><button class="sbtn" data-cancel>'+t("Close")+'</button></div>');
    $("#sheet [data-cancel]").onclick=closeSheet;
    $$("#sheet [data-bk]").forEach(b=>b.onclick=async()=>{
      if(!b.dataset.sure){ b.dataset.sure="1"; b.textContent=t("Restore it"); b.classList.add("danger"); return }
      try{ await post("/api/backups/restore",{name:b.dataset.bk}); closeSheet();
        toast(t("Restored. Reloading…")); setTimeout(()=>location.reload(),900) }
      catch(e){ toast(e.message,true) }
    });
  };
}
function fillNotify(){
  const on=$("#s-notify"), iv=$("#s-notify-iv"), fu=$("#s-notify-fu"), st=$("#s-notify-state"), A=notifyApi();
  const pr=prefs();
  on.checked=notifyOn(); iv.value=pr.notify_iv??"60,10"; fu.value=String(pr.notify_fu??"9");
  /* Off means off for the keyboard too, not only for the mouse. */
  $$("#sp-notify .nf-sub").forEach(r=>{ r.classList.toggle("off",!on.checked);
    r.setAttribute("aria-disabled",String(!on.checked));
    r.querySelectorAll("select,input,button").forEach(c=>c.disabled=!on.checked) });
  const say=async()=>{
    if(!A){ st.textContent=t("This window cannot show notifications."); on.disabled=true; return }
    const g=await A.granted().catch(()=>false);
    st.textContent=!on.checked?t("Off. Turn them on to be reminded of interviews and follow-ups.")
      :g?t("On, while CV Studio is open."):t("Blocked by your system. Allow CV Studio in your notification settings.");
  };
  say();
  on.onchange=async()=>{
    if(on.checked&&A){
      let g=await A.granted().catch(()=>false);
      if(!g) g=await A.ask().catch(()=>false);
      if(!g){ on.checked=false; toast(t("Notifications are blocked for CV Studio in your system settings."),true) }
    }
    setPref("notify",on.checked);
    fillNotify(); notifyTick();
  };
  iv.onchange=()=>{ setPref("notify_iv",iv.value); notifyTick() };
  fu.onchange=()=>{ setPref("notify_fu",fu.value); notifyTick() };
  /* The desktop app only: the window can close to the tray, and the app can
     open at login. The shell reads keep_running from the same file when the
     window closes. */
  const T=window.__TAURI__, AS=T&&T.autostart;
  $$("#sp-notify [data-desk]").forEach(r=>r.hidden=!T);
  const keep=$("#s-keep"), auto=$("#s-autostart");
  keep.checked=pr.keep_running!==false;
  keep.onchange=()=>setPref("keep_running",keep.checked);
  if(AS){
    AS.isEnabled().then(v=>{ auto.checked=!!v }).catch(()=>{ auto.closest(".srow").hidden=true });
    auto.onchange=async()=>{
      try{ await (auto.checked?AS.enable():AS.disable()) }
      catch(e){ auto.checked=!auto.checked; toast(t("Could not change that: {e}",{e:String(e&&e.message||e)}),true) }
    };
  }else if(auto) auto.closest(".srow").hidden=true;
  $("#s-notify-test").onclick=async()=>{
    const ok=await notifySend(null,t("CV Studio"),t("This is how a reminder will look."));
    if(!ok) toast(t("Notifications are blocked for CV Studio in your system settings."),true);
  };
}

function drawRail(){
  const c=statusCounts(), f=S.jfilter;
  /* The .mark span is what .row.sel paints ochre. Without it this rail marked
     its selection with a background lift alone, at 1.28:1 -- the documents rail
     next door emits the span and gets the bar, so the two rails disagreed about
     what "selected" looks like. */
  const row=(label,count,kind,value)=>
    '<button class="row'+(f.kind===kind&&f.value===value?" sel":"")+'" data-k="'+kind+
    '" data-v="'+esc(value)+'"><span class="mark"></span>'+
    '<span class="lbl">'+esc(label)+'</span>'+
    (count==null?"":'<span class="ct mono">'+count+'</span>')+'</button>';
  let h=row("All",S.jobs.length,"all","");
  S.statuses.forEach(s=>{ if(c[s]) h+=row(prettyStatus(s),c[s],"status",s) });
  /* The funnel hands over a node to filter by, and its label often matches a
     status already listed above -- "Draft" under "Draft", the second one with
     no count. Only add it when it is actually saying something new. */
  const shown=new Set(S.statuses.filter(x=>c[x]).map(prettyStatus));
  if(S.fnode&&S.labels[S.fnode]&&!shown.has(S.labels[S.fnode]))
    h+=row(S.labels[S.fnode],null,"node",S.fnode);
  $("#statuslist").innerHTML=h;

  /* Only buckets with something in them. An Attention list showing four zeroes
     is worse than no list: it trains you to stop looking at it. */
  const a=S.alerts;
  const live=a?ATTENTION.filter(([k])=>a.counts[k]>0):[];
  $("#attentionwrap").hidden=!live.length;
  $("#attentionlist").innerHTML=live.map(([k,label])=>
    row(label,a.counts[k],"alert",k)).join("");

  $("#savedlist").innerHTML=Object.keys(SAVED).map(k=>
    row(k,S.jobs.filter(SAVED[k]).length,"saved",k)).join("");
  $$("#statuslist [data-k],#attentionlist [data-k],#savedlist [data-k]").forEach(b=>b.onclick=()=>{
    S.jfilter={kind:b.dataset.k,value:b.dataset.v};
    if(b.dataset.k!=="node") S.fnode=null;
    drawJobs();
  });
}

function visibleJobs(){
  const f=S.jfilter, q=($("#jobq").value||"").trim().toLowerCase();
  let rows=S.jobs;
  if(f.kind==="status") rows=rows.filter(j=>j.status===f.value);
  else if(f.kind==="node"){
    const want=new Set(S.nodes[f.value]||[]);
    rows=rows.filter(j=>want.has(j.status));
  }else if(f.kind==="saved"&&SAVED[f.value]) rows=rows.filter(SAVED[f.value]);
  else if(f.kind==="alert"){
    /* Filter against the server's answer rather than re-deriving the rule
       here, which is the whole point of computing it in one place. */
    const ids=new Set(((S.alerts&&S.alerts[f.value])||[]).map(x=>x.id));
    rows=rows.filter(j=>ids.has(j.id));
  }
  if(q) rows=rows.filter(j=>(j.company+" "+j.title+" "+(j.notes||"")+" "+(j.source||""))
    .toLowerCase().includes(q));
  return rows;
}

/* ---- the base CV --------------------------------------------------------
   One document every tailored CV is copied from. It is pinned above the
   applications rather than listed among the documents because it is what they
   are all made of: the question "which CV is this one a version of" now has
   one answer instead of one per file. */
/* Not baseName(): that one already means "the file this document was copied
   from" a few hundred lines up, and two answers to one name is how the two
   ideas get confused in the first place. */
function baseLabel(){ const b=S.state&&S.state.base;
  return b?b.path.split("/").pop().replace(/\.ya?ml$/,""):null }
/* The band above the applications and the card on the Documents screen are the
   same statement about the same document, so it is written once and mounted
   twice. The handlers hang off data attributes rather than ids: both elements
   are in the DOM whichever screen is showing, and one id in two places is one
   id too many. */
function baseHTML(b){
  if(!b)
    return '<span class="bl">Base CV</span>'+
      '<span class="bsub">Not chosen yet. Every tailored CV starts as a copy '+
      'of one.</span>'+
      '<button class="obtn" data-base-pick>Choose\u2026</button>'+
      '<div class="grow"></div>';
  if(b.missing)
    return '<span class="bl">Base CV</span>'+
      '<span class="bn">'+esc(baseLabel())+'</span>'+
      '<span class="bsub">is no longer in the workspace</span>'+
      '<button class="obtn" data-base-pick>Choose another\u2026</button>'+
      '<div class="grow"></div>';
  const pages=S.pages[b.path];
  const th=S.baseThumb&&S.baseThumb.path===b.path?S.baseThumb:null;
  const fam=baseFamily();
  const tailored=((S.state&&S.state.documents)||[]).filter(d=>d.base===b.path).length;
  const ico=d=>'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" '+
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+d+'</svg>';
  /* The page on its own little stage, the way Documents shows it, with what
     it is, the languages it comes in and how much has been made from it. */
  return '<div class="bstage">'+
      '<span class="btag">'+t("Base CV")+'</span>'+
      '<button class="bthumb'+(th&&th.png?"":" empty")+'" data-base-open aria-label="'+
        t("Open the base CV")+'">'+
      (th&&th.png?'<img alt="" src="'+esc(th.png+tok())+'">'
        :'<span>'+(th&&th.failed?"Doesn\u2019t render":"Rendering\u2026")+'</span>')+
      '</button></div>'+
    '<div class="bmeta"><span class="bn">'+esc(baseLabel())+'</span>'+
      (fam.length>1?'<span class="bflags">'+fam.map(m=>flag(m.lang)||lchip(m.lang)).join("")+'</span>':'')+
    '</div>'+
    '<span class="bsub">'+[pages?pages+" page"+(pages===1?"":"s"):"",
      tailored?tailored+" tailored from it":"Every tailored CV starts here"].filter(Boolean).join(" · ")+'</span>'+
    '<div class="bacts"><button class="obtn bopen" data-base-open>Open</button>'+
      '<button class="obtn bico" data-base-design title="Design" aria-label="Design">'+
        ico('<circle cx="13.5" cy="6.5" r="1"/><circle cx="17.5" cy="10.5" r="1"/><circle cx="8.5" cy="7.5" r="1"/>'+
          '<circle cx="6.5" cy="12.5" r="1"/><path d="M12 2a10 10 0 0 0 0 20c1.1 0 2-.9 2-2 0-.5-.2-1-.5-1.3-.3-.4-.5-.8-.5-1.3 0-1.1.9-2 2-2h2.4A5.6 5.6 0 0 0 22 9.8C22 5.5 17.5 2 12 2z"/>')+'</button>'+
      '<button class="obtn bico" data-base-pick title="Change base…" aria-label="Change base…">'+
        ico('<path d="M17 3l4 4-4 4"/><path d="M3 7h18"/><path d="M7 21l-4-4 4-4"/><path d="M21 17H3"/>')+'</button>'+
    '</div>';
}
/* The same base, on Documents, at the size of a page you can read. */
function baseHeroHTML(b){
  if(!b||b.missing) return '<div class="bmain"><div class="bbody"><span class="bl">Base CV</span>'+
    (b?'<h2 class="bn">'+esc(baseLabel())+'</h2><span class="bmeta">is no longer in the '+
      'workspace</span>':'<h2 class="bn">Not chosen yet</h2>')+
    '<p class="bwhy">Every CV you tailor for an application starts as a copy of the base.</p>'+
    '<div class="bacts"><button class="pbtn" data-base-pick>'+(b?"Choose another":"Choose")+
      '&#8230;</button></div></div></div>';
  const fam=multiLang()?baseFamily():baseFamily().slice(0,1);
  let m=fam.find(x=>x.lang===S.baseLang)||fam[0];
  const docs=(S.state&&S.state.documents)||[];
  const me=docs.find(d=>d.path===m.path);
  const pages=S.pages[m.path];
  const th=m.source?(S.baseThumb&&S.baseThumb.path===b.path?S.baseThumb:null):S.docThumbs[m.path];
  const kids=docs.filter(d=>d.base===m.path);
  const used=new Set((S.jobs||[]).map(j=>j.cv_path).filter(Boolean));
  const sent=kids.filter(d=>used.has(d.path)).length;
  const L=langOf(m.lang), src=langOf(fam[0].lang);
  const behind=p=>{ const d=S.driftBy&&S.driftBy[p]; return d&&d.changes&&d.changes.length };
  const tabs='<div class="btabs" role="tablist" aria-label="Base CV language">'+fam.map(x=>
      '<button role="tab" data-blang="'+esc(x.lang)+'" aria-selected="'+String(x===m)+'">'+
        lchip(x.lang,x===m)+esc(langOf(x.lang).native)+
        (x.source&&fam.length>1?'<span class="src">source</span>':'')+
        (behind(x.path)?'<span class="behind"><i></i>'+behind(x.path)+' behind</span>':'')+
      '</button>').join("")+
    '<button class="add" data-blang-add>+ Add a language</button></div>';
  const why=m.source
    ? (fam.length>1?'Every CV you tailor in '+esc(L.english)+' starts as a copy of this one. '+
        'Its translations say what they are missing when it changes.'
       :'Every CV you tailor for an application starts as a copy of this one, and shows '+
        'what it changed from it. Improving the base improves every CV you tailor from now on.')
    : 'The '+esc(L.english)+' translation of <b>'+esc(docLabel(fam[0].path))+'</b>. CVs for '+
      'postings in '+esc(L.english)+' start from it. Its design follows the '+esc(src.english)+' one.';
  return tabs+'<div class="bmain"><button class="bthumb'+(th&&th.png?"":" empty")+'" data-base-open'+
      ' aria-label="Open the base CV">'+
      (th&&th.png?'<img alt="Page one of the base CV" src="'+esc(th.png+tok())+'">'
        :'<span>'+(th&&th.failed?"Doesn’t render":"Rendering…")+'</span>')+
    '</button>'+
    '<div class="bbody"><span class="bl">Base CV'+(fam.length>1?' · '+esc(L.native):'')+'</span>'+
      '<h2 class="bn">'+esc(m.source?baseLabel():docLabel(m.path))+'</h2>'+
      '<span class="bmeta">'+[pages?pages+" page"+(pages===1?"":"s"):"",
        me&&me.mtime?"updated "+mtimeLabel(me.mtime):""].filter(Boolean).join(" · ")+
      '</span>'+
      '<p class="bwhy">'+why+'</p>'+
      '<div class="bstats"><div><b>'+kids.length+'</b><span>tailored from it</span></div>'+
        '<div><b>'+sent+'</b><span>attached to applications</span></div>'+
        (!m.source&&behind(m.path)?'<div><b>'+behind(m.path)+'</b><span>changes in '+
          esc(src.english)+' to carry over</span></div>':'')+'</div>'+
      '<div class="bacts"><button class="pbtn" data-base-open>Open</button>'+
        (!m.source&&behind(m.path)?'<button class="obtn" data-base-drift>What changed</button>':'')+
        '<button class="obtn" data-base-design>Design</button>'+
        (m.source?'<button class="obtn" data-base-pick>Change base&#8230;</button>':'')+'</div>'+
    '</div></div>';
}

function mountBase(el,cls){
  if(!el) return;
  const b=S.state&&S.state.base;
  el.className=cls+(b&&b.missing?" gone":"")+(cls==="bhero"&&!b?" none":"");
  el.innerHTML=cls==="bhero"?baseHeroHTML(b):baseHTML(b);
  /* On Documents the card shows whichever language tab is picked. */
  const shown=cls==="bhero"&&b&&!b.missing
    ?((baseFamily().find(x=>x.lang===S.baseLang)||{}).path||b.path):b&&b.path;
  el.querySelectorAll("[data-base-open]").forEach(open=>open.onclick=()=>{
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    openDoc(shown);
  });
  const design=el.querySelector("[data-base-design]");
  if(design) design.onclick=async()=>{
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    await openDoc(b.path); openDesign();
  };
  const pick=el.querySelector("[data-base-pick]"); if(pick) pick.onclick=baseSheet;
  el.querySelectorAll("[data-blang]").forEach(t=>t.onclick=()=>{
    S.baseLang=t.dataset.blang; mountBase(el,cls);
  });
  const add=el.querySelector("[data-blang-add]");
  if(add) add.onclick=()=>addLanguageSheet(b.path);
  const dr=el.querySelector("[data-base-drift]");
  if(dr) dr.onclick=()=>driftSheet(shown,S.driftBy[shown]);
}
function paintBase(){
  applyMultiLang();
  mountBase($("#baserow"),"baserow");
  mountBase($("#docbase"),"bhero");
  paintBaseChip();
  baseThumb(false);
}

/* The base as it actually prints, not a sketch of its theme: the sketches in
   Design say what a theme looks like, and this card is about one document.
   Whatever is on disk is shown at once; a page older than the YAML is shown
   while a fresh one renders, then swapped, so the card never waits on Typst
   and never settles on a page that is out of date. */
let thumbBusy=null;
async function baseThumb(force){
  const b=S.state&&S.state.base;
  if(!b||b.missing) return;
  if(!force&&S.baseThumb&&S.baseThumb.path===b.path) return;
  if(thumbBusy===b.path) return;
  thumbBusy=b.path;
  const put=(png,failed)=>{
    S.baseThumb={path:b.path,png:png||(S.baseThumb&&S.baseThumb.path===b.path
      ?S.baseThumb.png:null),failed:!!failed};
    mountBase($("#baserow"),"baserow"); mountBase($("#docbase"),"bhero");
  };
  try{
    const t=await api("/api/thumb?path="+encodeURIComponent(b.path));
    put(t.png);
    if(!t.fresh){
      const r=await post("/api/render",{path:b.path});
      if(r.ok){ S.pages[b.path]=r.pages; put(r.pngs[0]) }
      else put(null,true);
    }
  }catch(e){ put(null,true) }
  finally{ thumbBusy=null }
}

/* A document's age. "720h" is what ago() would say about a CV last touched in
   the spring, which is true and useless, so anything older than yesterday gets
   the same date the applications table uses. */
function mtimeLabel(t){
  if(!t) return "";
  const d=new Date(t*1000), now=new Date();
  const same=(a,b)=>a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()
    &&a.getDate()===b.getDate();
  if(same(d,now)) return "today";
  const y=new Date(now); y.setDate(y.getDate()-1);
  return same(d,y)?"yesterday":shortDate(d);
}

/* ---- languages ------------------------------------------------------------
   A CV's language is RenderCV's locale; a translation is its own document,
   linked to the one it was translated from. CV Studio writes the part with
   one right answer -- dates, month names, section titles -- and the prose is
   translated by you or by your AI client. Nothing here asks a client to do
   anything: a client acts when you ask it to, so the app says what to ask. */
const langOf=c=>((S.state&&S.state.languages)||[]).find(l=>l.code===c)||
  {code:c||"en",native:String(c||"en").toUpperCase(),english:String(c||"en").toUpperCase()};
/* Flags for the languages people most often write a CV in here, drawn rather
   than emoji: Windows prints a flag emoji as two letters. English is the US
   flag and Portuguese the Brazilian one, as asked; the code is printed beside
   the flag either way, so a flag is never the only thing saying which. */
const FLAGS={
  fr:'<rect width="10" height="20" fill="#0055A4"/><rect x="10" width="10" height="20" fill="#fff"/>'+
    '<rect x="20" width="10" height="20" fill="#EF4135"/>',
  es:'<rect width="30" height="20" fill="#AA151B"/><rect y="5" width="30" height="10" fill="#F1BF00"/>',
  en:'<rect width="30" height="20" fill="#fff"/>'+[0,2,4,6,8,10,12].map(i=>'<rect y="'+(i*20/13).toFixed(2)+
    '" width="30" height="'+(20/13).toFixed(2)+'" fill="#B22234"/>').join("")+
    '<rect width="13" height="10.77" fill="#3C3B6E"/>'+[[2.5,2.2],[6.5,2.2],[10.5,2.2],[4.5,5.4],[8.5,5.4],
    [2.5,8.6],[6.5,8.6],[10.5,8.6]].map(([x,y])=>'<circle cx="'+x+'" cy="'+y+'" r=".75" fill="#fff"/>').join(""),
  pt:'<rect width="30" height="20" fill="#009B3A"/><path d="M15 2.2 27.6 10 15 17.8 2.4 10z" fill="#FEDF00"/>'+
    '<circle cx="15" cy="10" r="4.6" fill="#002776"/>',
};
const flag=c=>FLAGS[c]?'<svg class="flg" viewBox="0 0 30 20" aria-hidden="true">'+FLAGS[c]+'</svg>':"";
/* The flag beside an application's language follows the choice at once. */
document.addEventListener("change",e=>{
  const sel=e.target.closest&&e.target.closest(".ap-lang select"); if(!sel) return;
  sel.parentNode.querySelector(".flg")?.remove();
  sel.insertAdjacentHTML("beforebegin",flag(sel.value));
});
const lchip=(c,on)=>{ const k=String(c||"en").toLowerCase();
  return '<span class="lchip'+(on?' on':'')+(FLAGS[k]?' fl':'')+'">'+flag(k)+esc(k.toUpperCase())+'</span>' };
/* The base CV and its translations: the set the Documents tabs move between. */
function baseFamily(){
  const b=S.state&&S.state.base; if(!b||b.missing) return [];
  const docs=(S.state&&S.state.documents)||[], me=docs.find(d=>d.path===b.path);
  return [{path:b.path,lang:(me&&me.lang)||"en",source:true}].concat(docs
    .filter(d=>d.translation_of===b.path)
    .map(d=>({path:d.path,lang:d.lang,source:false})));
}
const baseIn=lang=>baseFamily().find(m=>m.lang===lang)||null;
/* Whether CVs come in more than one language here. Most people apply in one
   country, and for them every tab, flag and language question is noise, so it
   is off until a translation exists or Settings turns it on. Off hides the
   language machinery; nothing is deleted, and translations come back as they
   were when it is turned on again. */
function multiLang(){
  const p=prefs().multilang;
  return p==null?baseFamily().length>1:!!p;
}
function applyMultiLang(){ document.documentElement.classList.toggle("mono",!multiLang()) }
/* Which AI clients could do the translating, in words, for the prompts. */
function clientsSay(){
  const on=(S.ai||[]).filter(c=>c.state==="connected").map(c=>c.label);
  return on.length?on.join(" or "):null;
}
function copyText(text,btn){
  const done=()=>{ if(btn){ const t=btn.textContent; btn.textContent="Copied";
    setTimeout(()=>btn.textContent=t,1400) } };
  try{ navigator.clipboard.writeText(text).then(done,()=>toast("Select the text and copy it",true)) }
  catch(e){ toast("Select the text and copy it",true) }
}
/* What to say to a client, ready to copy, with who could hear it. */
function sayBox(title,text){
  const who=clientsSay();
  return '<div class="say-box"><b>'+title+'</b><div class="say" id="say-text">'+esc(text)+'</div>'+
    '<div class="row"><span class="grow">'+(who?"Paste it into "+esc(who)+
      ". It works through CV Studio, so you can watch it land here."
      :"No AI client is connected yet. Settings → AI clients sets one up.")+'</span>'+
    (who?'':'<button class="obtn" id="say-setup">AI clients</button>')+
    '<button class="obtn" id="say-copy">Copy</button></div></div>';
}
function wireSay(text){
  const c=$("#say-copy"); if(c) c.onclick=()=>copyText(text,c);
  const st=$("#say-setup"); if(st) st.onclick=()=>{ closeSheet(); openSettings("ai") };
}

function addLanguageSheet(src){
  const have=new Set(((S.state&&S.state.documents)||[])
    .filter(d=>d.path===src||d.translation_of===src).map(d=>d.lang));
  const from=langOf((((S.state&&S.state.documents)||[]).find(d=>d.path===src)||{}).lang);
  const all=((S.state&&S.state.languages)||[]).filter(l=>!have.has(l.code));
  let pick=(all.find(l=>l.code==="fr")||all[0]||{}).code;
  openSheet(
    '<div><h3 id="sheet-title">Add a language to '+esc(docLabel(src))+'</h3>'+
      '<p>A copy in another language, saved beside it and linked to it. CVs you tailor '+
      'for a posting in that language start from it.</p></div>'+
    '<div class="fg w88"><label for="al-q">Language</label>'+
      '<input id="al-q" autocomplete="off" placeholder="Search '+all.length+' languages"></div>'+
    '<div class="al-list" id="al-list" role="radiogroup" aria-label="Language"></div>'+
    '<ul class="al-does">'+
      '<li><span>Dates, month names and “present” print in the new language.</span></li>'+
      '<li><span>Common section titles are translated: Experience, Education, Skills and the like.</span></li>'+
      '<li><span>Your name, contact details, links, company names and dates stay exactly as they are, '+
        'and the design follows '+esc(from.english)+'.</span></li>'+
    '</ul>'+
    '<p>Your text stays in '+esc(from.english)+' until you, or your AI client, translate it.</p>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
      '<button class="sbtn primary" id="al-go">Add</button></div>');
  const paint=()=>{
    const q=$("#al-q").value.trim().toLowerCase();
    const list=all.filter(l=>!q||(l.native+" "+l.english+" "+l.code).toLowerCase().includes(q));
    $("#al-list").innerHTML=list.map(l=>'<button role="radio" data-l="'+l.code+'" aria-checked="'+
      String(l.code===pick)+'">'+lchip(l.code,l.code===pick)+'<span><b>'+esc(l.native)+'</b> '+
      (l.english!==l.native?'<em>'+esc(l.english)+'</em>':'')+'</span></button>').join("")||
      '<p class="note muted">No language by that name.</p>';
    $$("#al-list [data-l]").forEach(b=>b.onclick=()=>{ pick=b.dataset.l; paint() });
    $("#al-go").textContent="Add "+(pick?langOf(pick).english:"");
    $("#al-go").disabled=!pick;
  };
  $("#al-q").oninput=paint; paint();
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#al-go").onclick=async()=>{
    $("#al-go").disabled=true;
    try{
      const r=await post("/api/language/add",{path:src,language:pick});
      if(!r.ok){ $("#al-go").disabled=false; return toast(r.error,true) }
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      S.baseLang=r.lang;
      if(S.view==="docs") drawDocuments();
      translateNextSheet(r);
    }catch(e){ $("#al-go").disabled=false; toast(e.message,true) }
  };
}
/* After the copy is written: what is done, and what to ask a client for. */
function translateNextSheet(r){
  const to=langOf(r.lang), from=langOf(r.from_lang);
  const titled=Object.keys(r.sections_titled||{}).length, left=r.sections_to_title||[];
  const text="In CV Studio, translate "+r.path+" into "+to.english+". It is the "+
    to.english+" copy of "+r.of+".";
  openSheet(
    '<div><h3 id="sheet-title">'+esc(to.english)+' copy made</h3><p>'+
      'Dates and month names are in '+esc(to.english)+(titled?', and '+titled+' section title'+
      (titled===1?' is':'s are')+' translated':'')+'. The rest of the text is still in '+
      esc(from.english)+'.'+(left.length?' Still to title: '+left.map(esc).join(", ")+'.':'')+
    '</p>'+(r.font?'<p class="muted">Its font, '+esc(r.font.language)+' Noto Sans, is '+
      'downloading now ('+r.font.mb+' MB, once). Until it arrives the page prints in a font '+
      'your computer has.</p>':'')+'</div>'+
    sayBox("To have your AI client translate it, ask it:",text)+
    '<p>Or open it and translate it yourself, field by field.</p>'+
    '<div class="foot"><button class="sbtn" data-cancel>Close</button>'+
      '<button class="sbtn primary" id="tn-open">Open the '+esc(to.english)+' CV</button></div>');
  wireSay(text);
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#tn-open").onclick=()=>{ closeSheet(); openDoc(r.path) };
}
/* What the source changed since this translation last caught up. */
function driftSheet(path,drift){
  const to=langOf(drift.lang), from=langOf(drift.from_lang||"en");
  const text="In CV Studio, bring "+path+" up to date with "+drift.of+
    ": translate what changed into "+to.english+".";
  openSheet(
    '<div><h3 id="sheet-title">What '+esc(docLabel(drift.of))+' changed</h3><p>'+
      'Since this '+esc(to.english)+' version was last brought up to date. Nothing '+
      'here has been changed for you.</p></div>'+
    '<ul class="drift-list">'+drift.changes.map(c=>'<li><span class="w">'+esc(c.where)+'</span>'+
      (c.kind!=="added"&&c.before?'<span class="l">'+lchip(from.code)+'<span class="then">'+
        esc(c.before)+'</span></span>':'')+
      (c.kind!=="removed"?'<span class="l">'+lchip(from.code,true)+'<span>'+esc(c.after||"")+
        '</span></span>':'<span class="l">'+lchip(from.code,true)+'<span class="muted">removed</span></span>')+
      (c.translation?'<span class="l">'+lchip(to.code)+'<span>'+esc(c.translation)+
        '</span></span>':'')+'</li>').join("")+'</ul>'+
    sayBox("To have your AI client carry these over, ask it:",text)+
    '<div class="foot"><button class="sbtn left" data-cancel>Close</button>'+
      '<button class="sbtn primary" id="ds-done">Mark as done</button></div>');
  wireSay(text);
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#ds-done").onclick=()=>{ closeSheet(); markTranslationDone(path) };
}
async function markTranslationDone(path){
  try{
    await post("/api/language/done",{path});
    if(S.path===path){ const d=await api("/api/doc?path="+encodeURIComponent(path));
      S.doc.drift=d.drift; paintLang() }
    S.driftBy=null;
    if(S.view==="docs") drawDocuments();
    toast("Marked as up to date");
  }catch(e){ toast(e.message,true) }
}
/* The editor's bar: which language this is, the others to switch to, and
   what it is missing from its source. */
function paintLang(){
  const sw=$("#langsw"), bar=$("#driftbar");
  const fam=S.doc&&S.doc.family, members=(fam&&fam.members)||[];
  if(members.length<2){ sw.hidden=true }
  else{
    sw.hidden=false;
    sw.innerHTML=members.map(m=>'<button data-lp="'+esc(m.path)+'"'+
      (m.path===S.path?' aria-current="true"':'')+' title="'+esc(m.path)+
      (m.source?" · the source":" · translated from "+docLabel(fam.source))+'">'+
      lchip(m.lang,m.path===S.path)+'<span class="ln">'+esc(langOf(m.lang).native)+'</span></button>').join("");
    $$("#langsw [data-lp]").forEach(b=>b.onclick=()=>{
      if(b.dataset.lp===S.path) return;
      if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
      openDoc(b.dataset.lp);
    });
  }
  const dr=S.doc&&S.doc.drift;
  if(!dr||(!dr.missing&&!dr.changes.length)){ bar.hidden=true; return }
  bar.hidden=false;
  const from=langOf(dr.from_lang||"en"), n=dr.changes.length;
  $("#driftbar-msg").innerHTML=dr.missing
    ? 'The '+esc(from.english)+' CV this was translated from, <b>'+esc(dr.of)+'</b>, is missing.'
    : '<b>'+esc(docLabel(dr.of))+' changed since this was translated.</b> '+n+' thing'+
      (n===1?'':'s')+' in it '+(n===1?'is':'are')+' not in this '+esc(langOf(dr.lang).english)+
      ' version yet.';
  $("#drift-show").hidden=!!dr.missing; $("#drift-done").hidden=!!dr.missing;
  $("#drift-show").onclick=()=>driftSheet(S.path,dr);
  $("#drift-done").onclick=()=>markTranslationDone(S.path);
}

/* Two lanes, and the second is the reason this screen exists. A CV written for
   an application is reachable from that application's row; one written for
   nothing was reachable from nowhere, because the only list of documents lived
   inside the editor and you needed a document open to see it. Each is shown as
   its page, since two CVs are told apart by looking at them. */
function drawDocuments(){
  mountBase($("#docbase"),"bhero");
  const docs=(S.state&&S.state.documents)||[];
  $("#dcount").textContent=String(docs.length);
  const basePath=(S.state&&S.state.base&&S.state.base.path)||null;
  const owner={};
  for(const j of (S.jobs||[])){
    if(j.cv_path) owner[j.cv_path]=j;
    if(j.letter_path) owner[j.letter_path]=j;
  }
  /* The base's translations live in its tabs, not in the lanes. */
  const family=new Set(baseFamily().map(m=>m.path));
  /* The base CV's language first, then the rest by how many documents use it. */
  const srcLang=(baseFamily()[0]||{}).lang||"en", nOf={};
  docs.forEach(d=>{ const c=d.lang||"en"; nOf[c]=(nOf[c]||0)+1 });
  const langs=Object.keys(nOf).sort((a,b)=>(b===srcLang)-(a===srcLang)||nOf[b]-nOf[a]);
  const filt=$("#dfilter");
  if(langs.length<2||!multiLang()){ filt.hidden=true; S.docLang=null }
  else{
    filt.hidden=false;
    filt.innerHTML=[null,...langs].map(c=>'<button data-dl="'+(c||"")+'" aria-pressed="'+
      String((S.docLang||null)===c)+'">'+(c?flag(c)+esc(langOf(c).native):"All languages")+
      '</button>').join("");
    $$("#dfilter [data-dl]").forEach(b=>b.onclick=()=>{ S.docLang=b.dataset.dl||null;
      drawDocuments() });
  }
  const attached=[], loose=[];
  for(const d of docs){
    if(family.has(d.path)) continue;
    if(S.docLang&&(d.lang||"en")!==S.docLang) continue;
    (owner[d.path]?attached:loose).push(d);
  }
  baseDrift();
  const recent=(a,b)=>(b.mtime||0)-(a.mtime||0);
  attached.sort(recent); loose.sort(recent);

  const card=d=>{
    const j=owner[d.path], letter=d.group==="Cover letters";
    const copied=d.base?d.base.split("/").pop().replace(/\.ya?ml$/,""):null;
    const about=j
      ? '<span class="dot '+statusTone(j.status)+'"></span>'+esc(j.company)+' \u00b7 '+esc(j.title)
      : copied?'Copied from '+esc(copied):letter?'Cover letter':'Not attached';
    const th=S.docThumbs[d.path];
    return '<div class="dcw"><button class="dcard" data-open="'+esc(d.path)+'" title="'+esc(d.path)+'">'+
      '<span class="pg">'+(th&&th.png?'<img alt="" loading="lazy" src="'+esc(th.png+tok())+'">'
        :'<span>'+(th&&th.failed?"Doesn\u2019t render":"Rendering\u2026")+'</span>')+
        ((d.lang||"en")!==srcLang?'<span class="langs">'+lchip(d.lang,true)+'</span>':'')+
        (letter?'<span class="tag">Letter</span>':'')+'</span>'+
      '<span class="meta"><b>'+esc(d.label)+'</b><span>'+about+'</span>'+
        '<em>'+esc(mtimeLabel(d.mtime))+(S.pages[d.path]?" \u00b7 "+S.pages[d.path]+
          " page"+(S.pages[d.path]===1?"":"s"):"")+'</em></span></button>'+
      '<button class="dmore" data-more="'+esc(d.path)+'" aria-label="'+esc(t("Rename or delete {name}",{name:d.label}))+
        '" title="'+esc(t("Rename or delete"))+'">&#8943;</button></div>';
  };
  const lane=(title,list,why,empty)=>
    '<section class="dsec"><h2>'+title+'<span class="n">'+list.length+'</span></h2>'+
    '<p class="why">'+why+'</p>'+
    (list.length?'<div class="dgrid">'+list.map(card).join("")+'</div>'
      :'<div class="dempty">'+empty+'</div>')+'</section>';

  $("#doclanes").innerHTML=
    lane("Written for an application",attached,
      "Each is attached to the application it was tailored for. Opening one here is the "+
      "same as opening it from that row.",
      "None yet. On Applications, <b>Tailor a CV</b> on a row copies the base CV for it.")+
    lane("Everything else",loose,
      "CVs and letters no application points at: a master copy, an old version, a draft "+
      "you have not attached yet.",
      "Nothing here. <b>New document</b> or <b>Import</b> puts a CV here.");
  paintStatus();
  $$("#doclanes .dcard").forEach(b=>{
    b.onclick=()=>{
      if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
      openDoc(b.dataset.open);
    };
  });
  $$("#doclanes [data-more]").forEach(b=>b.onclick=e=>{ e.stopPropagation(); docMenuSheet(b.dataset.more) });
  docThumbs();
}
/* Every card's page. What is on disk is shown at once; anything missing or
   older than its YAML is rendered one at a time behind it, and only while
   Documents is on screen, so a big workspace never queues up Typst for a
   screen nobody is looking at. */
let docThumbRun=0;
async function docThumbs(){
  const run=++docThumbRun;
  const basePath=(S.state&&S.state.base&&S.state.base.path)||null;
  const docs=((S.state&&S.state.documents)||[]).filter(d=>d.path!==basePath);
  const stale=[];
  for(const d of docs){
    const have=S.docThumbs[d.path];
    if(have&&have.mtime===d.mtime) continue;
    try{
      const t=await api("/api/thumb?path="+encodeURIComponent(d.path));
      if(run!==docThumbRun) return;
      S.docThumbs[d.path]={png:t.png,mtime:t.fresh?d.mtime:null};
      if(t.png) docPaintThumb(d.path);
      if(!t.fresh) stale.push(d);
    }catch(e){ return }
  }
  for(const d of stale){
    if(run!==docThumbRun||S.view!=="docs") return;
    try{
      const r=await post("/api/render",{path:d.path});
      if(r.ok){ S.pages[d.path]=r.pages; S.docThumbs[d.path]={png:r.pngs[0],mtime:d.mtime} }
      else S.docThumbs[d.path]={png:(S.docThumbs[d.path]||{}).png,failed:true,mtime:d.mtime};
      docPaintThumb(d.path);
    }catch(e){ return }
  }
}
/* How far behind each translation of the base is, for its tab. Fetched once
   per visit to Documents: a translation only falls behind when its source is
   saved, and the tabs are not worth a request per save. */
async function baseDrift(){
  if(S.driftBy) return;
  S.driftBy={};
  for(const m of baseFamily().filter(x=>!x.source)){
    try{ const d=await api("/api/doc?path="+encodeURIComponent(m.path)); S.driftBy[m.path]=d.drift }
    catch(e){}
  }
  if(S.view==="docs") mountBase($("#docbase"),"bhero");
}
function docPaintThumb(path){
  if(baseFamily().some(m=>m.path===path&&!m.source)) mountBase($("#docbase"),"bhero");
  const b=$('#doclanes .dcard[data-open="'+CSS.escape(path)+'"] .pg'); if(!b) return;
  const th=S.docThumbs[path], tag=b.querySelector(".tag"), lg=b.querySelector(".langs");
  b.innerHTML=th&&th.png?'<img alt="" src="'+esc(th.png+tok())+'">'
    :'<span>'+(th&&th.failed?"Doesn\u2019t render":"Rendering\u2026")+'</span>';
  if(lg) b.append(lg);
  if(tag) b.append(tag);
  const em=b.parentElement.querySelector(".meta em"), d=((S.state&&S.state.documents)||[])
    .find(x=>x.path===path);
  if(em&&d) em.textContent=mtimeLabel(d.mtime)+(S.pages[path]?" \u00b7 "+S.pages[path]+
    " page"+(S.pages[path]===1?"":"s"):"");
}
function baseSheet(){
  const b=S.state&&S.state.base;
  /* Letters are excluded for the same reason the New document sheet filters
     its Base on list: a cover letter as the thing every CV is copied from
     produces nonsense. */
  const docs=(S.state.documents||[]).filter(d=>d.group!=="Cover letters");
  if(!docs.length) return toast("There are no CVs in the workspace yet.",true);
  openSheet(
    '<div><h3 id="sheet-title">Base CV</h3><p>Every new CV starts as a copy of '+
    'this one, and a tailored CV is measured against whatever it was copied '+
    'from. Changing it touches no document: CVs already tailored keep the base '+
    'they were made from.</p></div>'+
    '<div class="fg w88"><label>Use</label><select id="bs-doc">'+
      docs.map(d=>'<option value="'+esc(d.path)+'"'+
        (b&&b.path===d.path?" selected":"")+'>'+esc(d.label)+'</option>').join("")+
    '</select></div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="bs-go">Set as base</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#bs-go").onclick=async()=>{
    const path=$("#bs-doc").value;
    try{
      const r=await post("/api/base",{path:path});
      S.state.base=r.base;
      closeSheet(); paintBase();
      toast(t("{name} is the base CV",{name:baseLabel()}));
    }catch(e){ toast(e.message,true) }
  };
}
/* ---- tailoring a CV for an application -----------------------------------
   The one action the home screen exists to offer. Not a dialog: a dialog is
   for choices, and every choice here has already been made -- the base is
   pinned, the name comes from the application, and the link is the whole
   point. The New document sheet stays for when you do want the choices. */
function uniqueDocName(stem){
  /* Two applications to one company for one role is ordinary -- re-applying, or
     two openings -- so the second one gets a suffix rather than a dead end. */
  const taken=p=>(S.state.documents||[]).some(d=>d.path==="profile/"+p+".yaml");
  if(!taken(stem)) return stem;
  for(let n=2;n<50;n++) if(!taken(stem+"-"+n)) return stem+"-"+n;
  return stem+"-"+Date.now();
}
async function tailorFor(id,force){
  const j=(S.jobs||[]).find(x=>x.id===id);
  if(!j||S.tailoring.has(id)) return;
  const b=S.state&&S.state.base;
  if(!b||b.missing){
    toast(b?"The base CV is missing, so there is nothing to copy."
           :"Choose a base CV first \u2014 the tailored copy starts from it.",true);
    return baseSheet();
  }
  /* In the posting's language, from the base CV in that language. Without
     one, ask: translating the base now makes it for every later posting. */
  const lang=j.language||j.language_guess;
  const fam=baseFamily(), from=(lang&&baseIn(lang))||fam[0];
  if(multiLang()&&lang&&!baseIn(lang)&&!force) return tailorLangSheet(j,lang);
  const name=uniqueDocName(derivedName(j.title,j.company));
  S.tailoring.add(id); drawJobs();
  try{
    /* /api/new already records what it was copied from, so the new document's
       vs-base marks work with nothing extra done here. */
    const r=await post("/api/new",{name:name,kind:"cv",from:from.path});
    const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
    try{
      await post("/api/jobs/update",{id:j.id,cv_path:r.path});
      await loadJobs();
    }catch(e){
      /* The document exists either way, and an unlinked document is the
         recoverable half: the link chip in the editor attaches it. A job
         pointing at a file that was never written would not be. */
      toast(t("Created {name}, but linking it to {co} failed: {err} Use the link chip in the editor.",
            {name,co:j.company,err:tx(e.message)}),true);
    }
    openDoc(r.path);
    toast(t("Tailored from {p}",{p:docLabel(from.path)}));
  }catch(e){
    toast(e.message,true);
  }finally{
    S.tailoring.delete(id);
    if(S.view==="jobs") drawJobs();
  }
}

function tailorLangSheet(j,lang){
  const to=langOf(lang), src=langOf((baseFamily()[0]||{}).lang);
  openSheet(
    '<div><h3 id="sheet-title">This posting is in '+esc(to.english)+'</h3><p>Your base CV has no '+
      esc(to.english)+' version, so a CV tailored now would start in '+esc(src.english)+'.</p></div>'+
    '<p>Adding '+esc(to.english)+' to the base CV makes a copy with the dates and section '+
      'titles already in '+esc(to.english)+', for this application and every later one. '+
      'Your AI client, or you, then translate the text.</p>'+
    '<div class="foot"><button class="sbtn left" data-cancel>Cancel</button>'+
      '<button class="sbtn" id="tl-anyway">Tailor in '+esc(src.english)+'</button>'+
      '<button class="sbtn primary" id="tl-add">Add '+esc(to.english)+' to the base CV</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#tl-anyway").onclick=()=>{ closeSheet(); tailorFor(j.id,true) };
  $("#tl-add").onclick=async()=>{
    $("#tl-add").disabled=true;
    try{
      const r=await post("/api/language/add",{path:baseFamily()[0].path,language:lang});
      if(!r.ok){ $("#tl-add").disabled=false; return toast(r.error,true) }
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      translateNextSheet(r);
    }catch(e){ $("#tl-add").disabled=false; toast(e.message,true) }
  };
}

/* What the list is showing, in words, for the header above it. */
function filterTitle(){
  const f=S.jfilter;
  if(f.kind==="status") return prettyStatus(f.value);
  if(f.kind==="node") return (S.labels&&S.labels[f.value])||"Applications";
  if(f.kind==="saved") return f.value;
  if(f.kind==="alert"){
    const a=ATTENTION.find(([k])=>k===f.value);
    return a?a[1]:"Applications";
  }
  return "All applications";
}
function drawJobs(){
  drawRail();
  drawNextUp();
  const rows=visibleJobs();
  $("#jtitle").textContent=filterTitle();
  $("#jcount").textContent=S.jready?String(rows.length):"";
  const docName=p=>p?p.split("/").pop().replace(/\.ya?ml$/,""):null;
  $("#jobrows").innerHTML=rows.length?rows.map(j=>{
    const cv=docName(j.cv_path), letter=docName(j.letter_path);
    /* A row with a letter and no CV used to read "no CV yet" and drop the
       letter on the floor, which was wrong before and would now be worse: the
       offer to make one would be standing on top of a document that exists. */
    const docs=cv?esc(cv)+(letter?" + "+esc(t("letter")):""):(letter?esc(letter):null);
    const openable=cv?j.cv_path:j.letter_path;
    const ap=appliedAt(j);
    const due=j.followup_date&&j.followup_date<=isoToday();
    const jb=jobBoard(j);
    return '<button class="trow'+(DEAD_STATUS.has(j.status)?" dead":"")+
      (S.jsel===j.id?" sel":"")+'" data-id="'+esc(j.id)+'">'+
      '<span class="co">'+companyMark(j)+'<span class="con">'+
        esc(j.company)+'</span></span>'+
      '<span class="role"><b>'+esc(j.title)+'</b>'+
        (jb?'<span class="via" title="Found on '+esc(jb.label)+'">'+boardMark(jb)+'</span>':'')+
      '</span>'+
      '<span>'+(docs?'<span class="docs" data-open="'+esc(openable)+'">'+docs+'</span>'
               :S.tailoring.has(j.id)
                 ?'<span class="docs busy">Tailoring\u2026</span>'
                 /* The busy label is rendered from state rather than written
                    onto the node, because the workspace poll can redraw this
                    whole table underneath a copy that is still running. */
                 /* Quiet until you are on the row. Six ochre offers down one
                    column was the loudest thing on the screen, and the least
                    urgent. */
                 :'<span class="docs make" data-tailor="'+esc(j.id)+'" title="'+
                  'Copy the base CV, name it after this application, and open it'+
                  '"><i class="nt">Not tailored</i><u class="tl">Tailor a CV</u></span>')+'</span>'+
      '<span class="st"><span class="dot '+statusTone(j.status)+'"></span>'+
        esc(prettyStatus(j.status))+'</span>'+
      '<span class="when'+(ap?"":" none")+'">'+(ap?esc(shortDate(ap)):"–")+'</span>'+
      '<span class="when'+(j.followup_date?(due?" due":""):" none")+'">'+
        (j.followup_date?esc(shortDate(j.followup_date)):"–")+'</span>'+
      '</button>';
  }).join(""):(S.jobs.length
    ? '<div class="empty"><h3>Nothing matches</h3>'+
      '<p>Try another filter, or clear the search.</p></div>'
    /* The home screen of an empty workspace. This used to be the only place
       the app explained itself, on a CVs screen nobody lands on any more. */
    : '<div class="empty"><h3>No applications yet</h3>'+
      '<p>Add the roles you are applying for. Each one gets a CV tailored from '+
      'your base, in a click, and the funnel shows where they actually go.</p>'+
      '<p>'+(S.state&&S.state.base&&!S.state.base.missing
        ? "Your base CV is <b>"+esc(baseLabel())+"</b>. Every application starts "+
          "as a copy of it."
        : "Pick a base CV above and every application can start from it.")+'</p>'+
      '<div class="cta"><button class="sbtn primary" id="jb-first">Add an application'+
      '</button><button class="sbtn" id="jb-ai">Connect an AI client</button>'+
      '</div></div>');

  $$("#jobrows [data-id]").forEach(b=>b.onclick=e=>{
    if(e.target.closest("[data-open],[data-tailor]")) return;
    selectJob(b.dataset.id);
  });
  $$("#jobrows [data-tailor]").forEach(el=>el.onclick=e=>{
    e.stopPropagation();
    tailorFor(el.dataset.tailor);
  });
  const first=$("#jb-first"); if(first) first.onclick=()=>newJobSheet();
  const jai=$("#jb-ai"); if(jai) jai.onclick=()=>$("#btn-ai").click();
  $$("#jobrows [data-open]").forEach(a=>a.onclick=e=>{
    e.stopPropagation();
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    openDoc(a.dataset.open);
  });
  if(S.jsel&&!rows.some(j=>j.id===S.jsel)) S.jsel=null;
  drawJobInspector();
  paintStatus();
}
$("#jobq").addEventListener("input",()=>drawJobs());

function selectJob(id){
  S.jsel=id;
  if(id) palRemember({job:id});
  const j=S.jobs.find(x=>x.id===id);
  /* A job reached from the editor or the funnel may be filtered out of the
     current view; widen the filter rather than selecting something invisible. */
  if(j&&!visibleJobs().some(x=>x.id===id)){
    S.jfilter={kind:"all",value:""}; $("#jobq").value="";
  }
  /* The row is already there: move the highlight, not the whole table. */
  if(!moveSel()) drawJobs();
  const row=$("#jobrows .trow.sel");
  if(row) row.scrollIntoView({block:"nearest"});
}
/* Selection touches one class on two rows and the panels around the table;
   rebuilding five hundred rows for it is what made opening one slow. False
   when the row is not drawn, and the table has to be. */
function moveSel(){
  const host=$("#jobrows");
  const want=S.jsel?host.querySelector('.trow[data-id="'+CSS.escape(S.jsel)+'"]'):null;
  if(S.jsel&&!want) return false;
  host.querySelectorAll(".trow.sel").forEach(r=>{ if(r!==want) r.classList.remove("sel") });
  if(want) want.classList.add("sel");
  drawNextUp(); drawJobInspector(); paintStatus();
  return true;
}

const JOB_GRID=[
  ["status","Status","status"],["followup_date","Follow-up","date"],
  ["score","Fit","fit"],["source","Found on","text"],
  ["language","Language","lang"],
];
/* Everything you set once when the application is created and rarely touch
   again. Company and Role are here rather than at the top because the peek's
   own header already prints them, and they used to be the first two fields
   you read -- restating the title one line below itself. */
const JOB_MORE=[
  ["company","Company","text"],["title","Role","text"],
  ["location","Location","text"],["url","Link","text"],
  ["salary_expected","Salary","number"],
];
/* The rows the arrows walk: what the table is currently showing, in the order
   it is showing it, so Down always means "the row under this one". */
const peekRows=()=>visibleJobs();
function peekStep(delta){
  const rows=peekRows();
  const i=rows.findIndex(x=>x.id===S.jsel);
  if(i<0) return;
  const next=rows[i+delta];
  if(!next) return;
  S.jsel=next.id;
  drawJobs();
  drawJobInspector();
  const row=$('#jobrows [data-id="'+next.id+'"]');
  if(row) row.scrollIntoView({block:"nearest"});
}
function closePeek(){
  S.jsel=null;
  $("#jpeek").hidden=true;
  $("#v-jobs").classList.remove("peeking");
  moveSel();
}


/* ---- an application's posting ---------------------------------------------
   Stored as Markdown when an AI client saved it, and as pasted text when a
   person did. Both read the same: headings from "#" lines, or from a short line
   ending in a colon; lists from "-", "*" or "•"; bold and links inline. */
function postInline(t){
  let out=esc(t);
  out=out.replace(/\*\*(.+?)\*\*/g,"<b>$1</b>").replace(/(^|\W)\*(\S.*?\S|\S)\*(?=\W|$)/g,"$1<i>$2</i>");
  out=out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  out=out.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,'$1<a href="$2" target="_blank" rel="noreferrer">$2</a>');
  return out;
}
function postingHTML(text){
  const lines=String(text||"").replace(/\r/g,"").split("\n");
  let html="", list=null, para=[];
  /* A line break inside a pasted paragraph is usually meant: "Location: ..."
     under the title is its own line, not the rest of the title's sentence. */
  const flushP=()=>{ if(para.length){ html+="<p>"+para.map(postInline).join("<br>")+"</p>"; para=[] } };
  const flushL=()=>{ if(list){ html+="<ul>"+list.map(i=>"<li>"+postInline(i)+"</li>").join("")+"</ul>"; list=null } };
  for(const raw of lines){
    const l=raw.trim();
    if(!l){ flushP(); flushL(); continue }
    let m;
    if((m=l.match(/^#{1,6}\s+(.+)$/))||(l.length<60&&/:$/.test(l)&&!/^[-*•]/.test(l)&&!/https?:/.test(l))){
      flushP(); flushL();
      html+="<h4>"+postInline((m?m[1]:l.replace(/:$/,"")).replace(/\*\*/g,""))+"</h4>"; continue;
    }
    if((m=l.match(/^[-*•]\s+(.+)$/))||(m=l.match(/^\d+[.)]\s+(.+)$/))){ flushP(); (list=list||[]).push(m[1]); continue }
    flushL(); para.push(l);
  }
  flushP(); flushL();
  return html;
}
/* The facts a posting buries, pulled up as chips. Only what reads the same
   in every posting: a place, a salary range, travel, sponsorship. */
function postingChips(j){
  const txt=String(j.description||""), out=[];
  const where=j.location||((txt.match(/location\s*:\s*([^\n.]+)/i)||[])[1]||"").trim();
  const mode=(txt.match(/\b(remote|hybrid|on-?site)\b/i)||[])[1];
  /* The place is the posting's; how you work there is the app's word for it. */
  const how=mode&&t(mode[0].toUpperCase()+mode.slice(1).toLowerCase().replace(/^on-?site$/,"On-site"));
  if(where) out.push(where+(mode&&!new RegExp(mode,"i").test(where)?" · "+how:""));
  else if(mode) out.push(how);
  const pay=txt.match(/[$€£]\s?\d[\d,.]*\s?[kK]?\s?(?:[-–]|to)\s?[$€£]?\s?\d[\d,.]*\s?[kK]?(?:\s?(?:USD|EUR|GBP))?|\d[\d\s.,]*\s?(?:[kK]\s?)?€\s?(?:[-–]|à|to)\s?\d[\d\s.,]*\s?(?:[kK]\s?)?€/);
  if(pay) out.push(pay[0].replace(/\s+/g," ").trim());
  const tr=txt.match(/travel[^.\n]{0,40}?(\d{1,2}\s?(?:[-–]\s?\d{1,2}\s?)?%)/i);
  if(tr) out.push("Travel "+tr[1].replace(/\s/g,""));
  if(/visa sponsorship|sponsor(?:ship)? (?:a |your )?visa/i.test(txt)) out.push("Visa sponsorship");
  return out;
}
/* Where it was found, chosen rather than typed: the boards the app knows,
   with their marks, then the ways that are not a board. */
const SRC_FIRST=["linkedin","indeed","welcometothejungle","glassdoor","greenhouse","lever","wellfound"];
const SRC_OTHER=[["careers","Company’s careers page","↗"],["referral","Referral","♥"],
  ["recruiter","Recruiter","☎"]];
function sourceMark(j){
  const b=jobBoard(j);
  if(b) return boardMark(b)+'<span class="nm">'+esc(b.label)+'</span>';
  const o=SRC_OTHER.find(x=>x[1]===j.source);
  if(j.source) return (o?'<span class="gl">'+o[2]+'</span>':'')+'<span class="nm">'+esc(j.source)+'</span>';
  return '<span class="nm" style="color:var(--t500)">Not set</span>';
}
function sourceMenu(j,btn){
  let m=$("#ap-menu"); if(m){ m.remove(); return }
  const cur=(jobBoard(j)||{}).id, said=String(j.source||"");
  const boards=SRC_FIRST.map(id=>BOARDS.find(b=>b.id===id)).filter(Boolean)
    .concat(BOARDS.filter(b=>!SRC_FIRST.includes(b.id)));
  m=document.createElement("div"); m.id="ap-menu"; m.className="ap-menu"; m.setAttribute("role","listbox");
  m.setAttribute("aria-label","Where you found it");
  m.innerHTML=boards.map(b=>'<button role="option" data-src="'+esc(b.label)+'" aria-selected="'+
      String(b.id===cur)+'">'+boardMark(b)+esc(b.label)+'</button>').join("")+'<hr>'+
    SRC_OTHER.map(([k,l,g])=>'<button role="option" data-src="'+esc(l)+'" aria-selected="'+
      String(!cur&&said===l)+'"><span class="gl">'+g+'</span>'+esc(l)+'</button>').join("")+
    '<button role="option" data-src=""><span class="gl">…</span>Other…</button>';
  document.body.append(m);
  const r=btn.getBoundingClientRect();
  m.style.left=Math.min(r.left,innerWidth-260)+"px";
  m.style.top=Math.min(r.bottom+6,innerHeight-370)+"px";
  m.onclick=e=>{ const o=e.target.closest("[data-src]"); if(!o) return; m.remove();
    let v=o.dataset.src;
    if(!v){ v=(prompt("Where did you find it?",jobBoard(j)?"":said)||"").trim(); if(!v) return }
    saveJob(j.id,{source:v}) };
  setTimeout(()=>document.addEventListener("pointerdown",function off(ev){
    if(!m.contains(ev.target)){ m.remove(); document.removeEventListener("pointerdown",off,true) } },true),0);
}
/* A document on the application, as its first page. */
async function apThumb(el,path){
  let th=S.docThumbs[path];
  if(!th){
    try{ const t=await api("/api/thumb?path="+encodeURIComponent(path));
      th={png:t.png}; if(!t.fresh){ const r=await post("/api/render",{path});
        if(r.ok) th={png:r.pngs[0]} } }catch(e){ th={png:null,failed:true} }
    S.docThumbs[path]=th;
  }
  if(el.isConnected) el.innerHTML=th&&th.png?'<img alt="" src="'+esc(th.png+tok())+'">'
    :'<span>'+(th&&th.failed?"Doesn’t render":"Rendering…")+'</span>';
}
function drawJobInspector(){
  const j=S.jobs.find(x=>x.id===S.jsel);
  const peek=$("#jpeek"), head=$("#jinsp-title"), body=$("#jinsp-body");
  $("#v-jobs").classList.toggle("peeking",!!j);
  if(!j){ peek.hidden=true; return }
  peek.hidden=false;
  head.textContent=j.title||j.company;
  head.title=j.title||j.company;
  const sub=[j.company,j.location].filter(Boolean).join(" \u00b7 ");
  $("#jinsp-sub").textContent=sub; $("#jinsp-sub").title=sub;
  $("#jinsp-logo").innerHTML=companyMark(j);
  $("#jinsp-status").innerHTML='<span class="dot '+statusTone(j.status)+'"></span>'+esc(prettyStatus(j.status));

  const rows=peekRows(), at=rows.findIndex(x=>x.id===j.id);
  $("#jpk-idx").textContent=at<0?"":(at+1)+" of "+rows.length;
  $("#jpk-prev").disabled=at<=0;
  $("#jpk-next").disabled=at<0||at>=rows.length-1;

  const field=([k,label,kind])=>{
    let ctl;
    if(kind==="status") ctl='<span class="statusctl"><span class="dot '+statusTone(j.status)+'"></span><select data-j="status">'+S.statuses.map(s=>
      '<option value="'+s+'"'+(s===j.status?" selected":"")+'>'+esc(prettyStatus(s))+
      '</option>').join("")+'</select></span>';
    else if(kind==="lang"){
      /* Stored once said; until then a guess from the posting, offered. */
      const guess=j.language_guess, cur=j.language||"";
      ctl='<select data-j="language">'+
        '<option value=""'+(cur?"":" selected")+'>'+(guess?"Looks like "+
          esc(langOf(guess).native):"Not set")+'</option>'+
        ((S.state&&S.state.languages)||[]).map(l=>'<option value="'+l.code+'"'+
          (l.code===cur?" selected":"")+'>'+esc(l.native)+'</option>').join("")+'</select>';
    }
    else if(kind==="fit") ctl='<div class="fit" role="group" aria-label="Fit">'+
      [1,2,3,4,5].map(n=>'<button data-fit="'+n+'"'+((j.score||0)>=n?' class="on"':"")+
      ' title="'+n+' of 5" aria-label="'+n+' of 5"></button>').join("")+
      '<span class="fitv">'+(j.score?j.score+" / 5":"not rated")+'</span></div>';
    else ctl='<input data-j="'+k+'" aria-label="'+esc(label)+'"'+(kind==="date"?' type="date"':"")+
      (kind==="number"?' type="number" class="mono"':"")+' value="'+
      esc(j[k]==null?"":j[k])+'">';
    return '<label>'+esc(label)+'</label>'+ctl;
  };
  const more=JOB_MORE.map(field).join("");

  const docRow=(label,key,group)=>{
    const linked=j[key];
    return '<div class="drow"><select data-j="'+key+'" class="'+(linked?"":"empty")+'">'+
      '<option value="">'+(key==="cv_path"?"no CV yet":"no cover letter")+'</option>'+
      ((S.state&&S.state.documents||[]).filter(d=>d.group===group).map(d=>
        '<option value="'+esc(d.path)+'"'+(d.path===linked?" selected":"")+'>'+
        esc(d.label)+'</option>').join(""))+'</select>'+
      (linked?'<button class="alink" data-open-doc="'+esc(linked)+'">Open</button>':"")+
      '</div>';
  };

  /* One row of facts, the documents as pages, and the posting beside them,
     read as a posting. The fields set once when the application was made are
     folded away, with the delete under them. */
  const fact=(label,ctl)=>'<div class="ap-fact'+(label==="Interview"?" wide":label==="Language"?" lang":"")+
    '"><span>'+t(label)+'</span>'+ctl+'</div>';
  const statusCtl='<span class="statusctl"><span class="dot '+statusTone(j.status)+'"></span>'+
    '<select data-j="status" aria-label="Status">'+S.statuses.map(s=>'<option value="'+s+'"'+
    (s===j.status?" selected":"")+'>'+esc(prettyStatus(s))+'</option>').join("")+'</select></span>';
  const fitCtl='<div class="fit" role="group" aria-label="Fit">'+[1,2,3,4,5].map(n=>
    '<button data-fit="'+n+'"'+((j.score||0)>=n?' class="on"':"")+' title="'+n+' of 5" aria-label="'+
    n+' of 5"></button>').join("")+'</div>';
  const guess=j.language_guess, curL=j.language||"";
  const langCtl='<span class="ap-lang">'+flag(curL||guess)+'<select data-j="language" aria-label="Language">'+
    '<option value=""'+(curL?"":" selected")+'>'+(guess?"Looks like "+esc(langOf(guess).native):"Not set")+'</option>'+
    ((S.state&&S.state.languages)||[]).map(l=>'<option value="'+l.code+'"'+(l.code===curL?" selected":"")+'>'+
      esc(l.native)+'</option>').join("")+'</select></span>';
  const facts='<div class="ap-facts">'+
    fact("Status",statusCtl)+
    fact("Follow-up",'<input data-j="followup_date" type="date" aria-label="Follow-up" value="'+esc(j.followup_date||"")+'">')+
    fact("Fit",fitCtl)+
    fact("Found on",'<button class="ap-src" id="ap-src" aria-haspopup="listbox">'+sourceMark(j)+
      '<span class="caret"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></span></button>')+
    fact("Language",langCtl)+
    fact("Interview",interviewCtl(j))+'</div>';

  const cvName=j.cv_path?j.cv_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
  const ltName=j.letter_path?j.letter_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
  const tailoring=S.tailoring&&S.tailoring.has(j.id);
  const cvCard=j.cv_path
    ? '<div class="ap-doc"><div class="pg" data-open-doc="'+esc(j.cv_path)+'" data-thumb="'+esc(j.cv_path)+
        '" role="button" tabindex="0" aria-label="Open the CV"><span>Rendering…</span></div>'+
      '<div class="t"><b>CV</b><span>'+esc(cvName)+'</span></div>'+
      '<div class="a"><button class="obtn" data-open-doc="'+esc(j.cv_path)+'">Open</button>'+
        '<button class="obtn" id="job-ats" title="ATS check: what an applicant tracking system reads">ATS</button></div></div>'
    : '<div class="ap-doc"><div class="pg none"><span>No CV for this one yet</span>'+
        '<button class="obtn" data-tailor-here'+(tailoring?" disabled":"")+'>'+(tailoring?"Tailoring…":"Tailor a CV")+'</button></div>'+
      '<div class="t"><b>CV</b><span>Copied from your base CV</span></div></div>';
  const ltCard=j.letter_path
    ? '<div class="ap-doc"><div class="pg" data-open-doc="'+esc(j.letter_path)+'" data-thumb="'+esc(j.letter_path)+
        '" role="button" tabindex="0" aria-label="Open the cover letter"><span>Rendering…</span></div>'+
      '<div class="t"><b>Cover letter</b><span>'+esc(ltName)+'</span></div>'+
      '<div class="a"><button class="obtn" data-open-doc="'+esc(j.letter_path)+'">Open</button></div></div>'
    : '<div class="ap-doc"><div class="pg none"><span>No cover letter</span>'+
        '<button class="obtn" id="ap-write">Write one</button></div>'+
      '<div class="t"><b>Cover letter</b><span>In the look of the CV</span></div></div>';
  const jb=jobBoard(j), words=(String(j.description||"").match(/\S+/g)||[]).length;
  /* The posting is not a document you send, so it is not a tile beside
     them: it is one card on the right, its link and its text together. */
  /* A board with a website is named; a referral or a recruiter is not a
     place, so the link says where it goes instead. */
  let site=""; try{ site=new URL(j.url).hostname.replace(/^www\./,"") }catch(e){}
  const web=jb&&jb.host?jb:null, who=web?web.label:site;

  const hist=(j.status_history||[]);
  const timeline=hist.length?'<div class="tl">'+hist.map(h=>
    '<div class="tli"><div class="spine"><i></i><u></u></div>'+
    '<div class="ev"><span>'+esc(prettyStatus(h.status))+'</span>'+
    '<span class="when mono">'+esc(shortDate(h.at))+'</span></div></div>').join("")+'</div>'
    :'<p class="note muted">No history yet.</p>';
  const chips=postingChips(j);
  const openLink=j.url
    ? '<a class="obtn" href="'+esc(j.url)+'" target="_blank" rel="noreferrer" title="'+esc(j.url)+'">'+
        (web?boardMark(web):'')+esc(who?t("Open on {site}",{site:who}):t("Open the posting"))+'<span class="ext">↗</span></a>'
    : '<button class="obtn" id="ap-addlink">'+t("Add the link")+'</button>';
  const pasteBox=(val,first)=>'<label class="ap-plabel" for="ap-paste">'+
      t(first?"Paste it here while it is up":"The posting, as text")+'</label>'+
    '<textarea class="posting-edit" id="ap-paste" placeholder="'+
      esc(t("Paste the posting. Headings and lists come through: a short line ending in a colon, or lines starting with -."))+'">'+
      esc(val||"")+'</textarea>'+
    '<div class="ap-pfoot">'+(first?'<span>'+t("Or ask your AI client to save it:")+' <code>'+
      esc(t("save the posting for {co}",{co:j.company}))+'</code></span>':'<span></span>')+
      '<span class="grow"></span>'+(first?'':'<button class="obtn" id="ap-post-cancel">'+t("Cancel")+'</button>')+
      '<button class="pbtn" id="ap-post-save">'+t(first?"Save the posting":"Save")+'</button></div>';
  const posting='<article class="ap-pcard" aria-label="'+esc(t("The posting"))+'">'+
    '<div class="ap-phead"><div class="tx"><b>'+t("The posting")+'</b><span>'+
      esc(words?t("{n} words · kept here in case the advert comes down",{n:words})
        :t("Not saved yet · keep a copy before the advert comes down"))+
      '</span></div>'+openLink+
      (words?'<button class="obtn" id="ap-post-edit">'+t("Edit")+'</button>':'')+'</div>'+
    '<div class="ap-pbody" id="ap-pbody">'+(words
      ? (chips.length?'<div class="ap-chips">'+chips.map(c=>'<span>'+esc(c)+'</span>').join("")+'</div>':'')+
        '<div class="ap-post" id="ap-post">'+postingHTML(j.description)+'</div>'
      : pasteBox("",true))+'</div></article>';

  body.innerHTML=
    '<div class="peek-grid">'+
      '<div class="col">'+
        facts+
        '<div class="block"><div class="bhead"><span class="blabel">Documents</span>'+
          (j.cv_path||j.letter_path?'<button class="obtn" id="ap-pack" data-job="'+esc(j.id)+'">'+
            (j.cv_path&&j.letter_path?"Export both…":"Export…")+'</button>':'')+
          '</div><div class="ap-docs">'+cvCard+ltCard+'</div></div>'+
        (j.cv_path?'<div class="block" id="jdiff-block" hidden><span class="blabel">'+
          'Changed from the base</span><div class="bdiff" id="jdiff" data-path="'+
          esc(j.cv_path)+'"></div></div>':'')+
        '<div class="block"><span class="blabel">History</span>'+timeline+'</div>'+
        '<details class="fold"><summary>Company, role and the rest</summary>'+
          '<div class="fg2" style="margin-top:11px">'+more+'</div>'+
          '<div class="card" style="margin-top:12px">'+docRow("CV","cv_path","My CVs")+
            docRow("Cover letter","letter_path","Cover letters")+'</div></details>'+
        '<div class="block ruled foot-del"><button class="sbtn danger" id="job-del">'+
          'Delete this application</button></div>'+
      '</div>'+
      '<div class="col">'+
        '<div class="block"><span class="blabel">Notes</span>'+
          '<textarea data-j="notes" class="notes" aria-label="Notes">'+esc(j.notes||"")+'</textarea></div>'+
        peopleHTML(j)+
        '<div class="block grow">'+posting+'</div>'+
      '</div>'+
    '</div>';
  body.querySelectorAll("[data-thumb]").forEach(el=>apThumb(el,el.dataset.thumb));
  $("#ap-src").onclick=e=>{ e.stopPropagation(); sourceMenu(j,$("#ap-src")) };
  const wr=$("#ap-write");
  if(wr) wr.onclick=async()=>{
    wr.disabled=true;
    try{ const r=await post("/api/letter/new",{job_id:j.id});
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      await loadJobs(true); openDoc(r.path) }
    catch(e){ wr.disabled=false; toast(e.message,true) }
  };
  const th=body.querySelector("[data-tailor-here]");
  if(th) th.onclick=()=>tailorFor(j.id);
  const wirePaste=()=>{
    const sv=$("#ap-post-save"), ta=$("#ap-paste");
    if(sv) sv.onclick=()=>{ const v=ta.value.trim(); if(!v&&!j.description) return ta.focus();
      saveJob(j.id,{description:v||null}) };
    const cn=$("#ap-post-cancel"); if(cn) cn.onclick=()=>selectJob(j.id);
  };
  wirePaste();
  const ed=$("#ap-post-edit");
  if(ed) ed.onclick=()=>{ ed.hidden=true; $("#ap-pbody").innerHTML=pasteBox(j.description,false);
    wirePaste(); $("#ap-paste").focus() };
  const al=$("#ap-addlink");
  if(al) al.onclick=()=>{ const u=prompt(t("The link to the posting"),"https://");
    if(u&&/^https?:\/\/\S+\.\S+/.test(u.trim())) saveJob(j.id,{url:u.trim()}) };
  const diff=$("#jdiff");
  if(diff) fillBaseDiff(diff,j.cv_path);
  const ab=$("#job-ats");
  if(ab) ab.onclick=()=>atsSheet(j.cv_path,j.description?j.id:"");
  body.querySelectorAll("[data-j]").forEach(el=>{
    el.onchange=()=>{
      let v=el.value;
      if(el.type==="number") v=v===""?null:Number(v);
      saveJob(j.id,{[el.dataset.j]:v===""?null:v});
    };
  });
  wireInterview(j);
  body.querySelectorAll("[data-fit]").forEach(b=>b.onclick=()=>{
    const n=+b.dataset.fit;
    saveJob(j.id,{score:j.score===n?null:n});
  });
  body.querySelectorAll("[data-open-doc]").forEach(b=>b.onclick=()=>{
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
    openDoc(b.dataset.openDoc);
  });
  /* No "are you sure": it goes to the trash, and the toast can bring it back. */
  $("#job-del").onclick=async()=>{
    try{
      await post("/api/jobs/delete",{id:j.id});
      S.jsel=null; await loadJobs();
      toast(t("Deleted {what}",{what:j.title+" · "+j.company}),false,{label:t("Undo"),fn:async()=>{
        try{ await post("/api/jobs/restore",{id:j.id}); await loadJobs(); selectJob(j.id); toast(t("Restored")) }
        catch(e){ toast(e.message,true) } }});
    }catch(e){ toast(e.message,true) }
  };
}
$("#jpk-prev").onclick=()=>peekStep(-1);
$("#jpk-next").onclick=()=>peekStep(1);
$("#jpk-close").onclick=closePeek;

/* Arrow keys walk the list with the peek open, which is the whole point of it
   being a peek rather than a page: you can read every application in the
   funnel without ever closing anything. They stay out of the way of a field
   being typed into, and of the select and date inputs, which use the arrows
   themselves. */
document.addEventListener("keydown",e=>{
  if(S.view!=="jobs"||$("#jpeek").hidden) return;
  if(!$("#sheet").hidden||!$("#ovl-settings").hidden||!$("#ovl-design").hidden) return;
  if(e.key==="Escape"){ e.preventDefault(); return closePeek() }
  if(e.key!=="ArrowUp"&&e.key!=="ArrowDown") return;
  const el=document.activeElement;
  if(el&&el.closest("#jinsp-body")&&
     /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
  e.preventDefault();
  peekStep(e.key==="ArrowDown"?1:-1);
});

/* When and where the interview is: the time as the invitation gave it, the
   zone it gave it in (guessed from where the job is), and what that is for
   you. */
function interviewCtl(j){
  const zone=j.interview_tz||"", guess=guessTz(j);
  const mine=userTz();
  return '<div class="ap-iv"><input type="datetime-local" id="ap-iv-at" aria-label="'+t("Interview time")+
    '" value="'+esc(String(j.interview_at||"").slice(0,16))+'">'+
    '<select id="ap-iv-tz" aria-label="'+t("Time zone of the interview")+'">'+
    '<option value=""'+(zone?"":" selected")+'>'+t("Your time")+' · '+esc(tzCity(mine))+'</option>'+
    tzOptions(zone,guess&&guess!==mine?guess:null)+'</select></div>'+
    (j.interview_at?'<small class="ap-iv-say">'+esc(interviewLine(j))+'</small>':'');
}
function wireInterview(j){
  const at=$("#ap-iv-at"), tz=$("#ap-iv-tz");
  if(!at) return;
  at.onchange=()=>{
    const patch={interview_at:at.value?at.value+":00":null};
    /* A first time for a job somewhere else starts in that place's zone. */
    if(at.value&&!j.interview_at&&!tz.value){ const g=guessTz(j); if(g&&g!==userTz()) patch.interview_tz=g }
    saveJob(j.id,patch);
  };
  tz.onchange=()=>saveJob(j.id,{interview_tz:tz.value||null});
}

async function saveJob(id,patch){
  try{
    const updated=await post("/api/jobs/update",Object.assign({id},patch));
    const i=S.jobs.findIndex(x=>x.id===id);
    if(i>=0) S.jobs[i]=updated;
    /* Sorting is by updated_at, so an edit moves the row; redraw the whole
       table rather than leaving a stale order behind. */
    S.jobs.sort((a,b)=>String(b.updated_at).localeCompare(String(a.updated_at)));
    drawJobs();
    if(S.view==="cvs") buildInspector();
    S.funnel=null;
  }catch(e){ toast(e.message,true) }
}

/* ---- new job sheet -------------------------------------------------------- */
function newJobSheet(seed){
  seed=seed||{};
  const docs=g=>(S.state&&S.state.documents||[]).filter(d=>d.group===g);
  openSheet(
    '<div><h3 id="sheet-title">New application</h3><p>Only the company and the role are '+
    'required. Everything else can come later.</p></div>'+
    '<div class="fg w88">'+
      '<label>Company</label><input id="nj-company" autocomplete="off">'+
      '<label>Role</label><input id="nj-title" autocomplete="off">'+
      '<label>Status</label><select id="nj-status">'+S.statuses.map(s=>
        '<option value="'+s+'">'+esc(prettyStatus(s))+'</option>').join("")+'</select>'+
      '<label>Found on</label><input id="nj-source" autocomplete="off" '+
        'placeholder="LinkedIn, referral, careers page…">'+
      '<label>Salary</label><input id="nj-salary" type="number" class="mono">'+
      '<label>Follow-up</label><input id="nj-followup" type="date">'+
      '<label>Link</label><input id="nj-url" autocomplete="off" placeholder="https://">'+
      '<label>CV</label><select id="nj-cv"><option value="">Not linked</option>'+
        docs("My CVs").map(d=>'<option value="'+esc(d.path)+'"'+
          (d.path===seed.cv_path?" selected":"")+'>'+esc(d.label)+'</option>').join("")+
        '</select>'+
      '<label>Cover letter</label><select id="nj-letter"><option value="">Not linked</option>'+
        docs("Cover letters").map(d=>'<option value="'+esc(d.path)+'">'+esc(d.label)+
          '</option>').join("")+'</select>'+
      '<label>Notes</label><textarea id="nj-notes" rows="3"></textarea>'+
    '</div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="nj-go">Add</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#nj-go").onclick=async()=>{
    const v=id=>$("#"+id).value.trim();
    if(!v("nj-company")||!v("nj-title")) return toast("Company and role are required",true);
    try{
      const j=await post("/api/jobs",{
        company:v("nj-company"), title:v("nj-title"), status:$("#nj-status").value,
        source:v("nj-source")||null, url:v("nj-url")||null,
        salary_expected:v("nj-salary")?Number(v("nj-salary")):null,
        followup_date:v("nj-followup")||null, notes:v("nj-notes")||null,
        cv_path:$("#nj-cv").value||null, letter_path:$("#nj-letter").value||null});
      closeSheet(); await loadJobs(); S.funnel=null;
      setView("jobs"); selectJob(j.id); toast(t("Added {co}",{co:j.company}));
    }catch(e){ toast(e.message,true) }
  };
  $("#nj-company").focus();
}
$("#btn-newjob").onclick=()=>newJobSheet();
$("#btn-newdoc").onclick=()=>newDocumentSheet();
/* Import makes a new CV from a file rather than overwriting one: the design is
   the base CV's, so it prints like everything else, and the base is left
   alone. What was read, and anything to check, is shown before it is made. */
$("#btn-importdoc").onclick=()=>{ $("#importdoc-file").value=""; $("#importdoc-file").click() };
$("#importdoc-file").onchange=async()=>{
  const file=$("#importdoc-file").files[0]; if(!file) return;
  const btn=$("#btn-importdoc"); btn.disabled=true; btn.textContent="Reading…";
  let imp;
  try{ imp=await importFile(file) }
  catch(e){ return toast(e.message,true) }
  finally{ btn.disabled=false; btn.innerHTML="Import&#8230;" }
  importSheet(imp);
};
function importSheet(imp){
  const b=S.state&&S.state.base, base=b&&!b.missing?b.path:null;
  const stem=slug(imp.cv.name&&imp.cv.name!=="Your Name"?imp.cv.name:
    file_stem(imp.name))||"imported";
  openSheet(
    '<div><h3 id="sheet-title">Import '+esc(imp.name)+'</h3><p>'+
      (base?'A new CV, in the base CV&#8217;s design. The base itself is not changed.'
           :'A new CV from what was read.')+'</p></div>'+
    '<ul class="imp-found">'+imp.found.map(f=>'<li><b>'+esc(f.what)+'</b><span>'+
      esc(String(f.count))+'</span></li>').join("")+'</ul>'+
    (imp.notes.length?'<div class="imp-notes"><b>To check</b><ul>'+imp.notes.map(n=>
      '<li>'+esc(n)+'</li>').join("")+'</ul></div>':'')+
    '<div class="fg w88"><label for="imp-name">Save as</label>'+
      '<input id="imp-name" class="mono" autocomplete="off" value="'+
        esc(uniqueDocName(stem+"-cv"))+'"></div>'+
    '<div class="foot"><button class="sbtn" data-cancel>Cancel</button>'+
    '<button class="sbtn primary" id="imp-go">Create and open</button></div>');
  $("#sheet [data-cancel]").onclick=closeSheet;
  $("#imp-go").onclick=async()=>{
    const name=$("#imp-name").value.trim().replace(/\.ya?ml$/i,"");
    if(!name) return toast("Give it a name",true);
    $("#imp-go").disabled=true;
    try{
      const r=await post("/api/new",{name,kind:"cv",from:base});
      await post("/api/save",{path:r.path,patches:[{path:["cv"],value:imp.cv}]});
      closeSheet();
      const st=await api("/api/state"); S.state=st; renderDocs(st.documents);
      openDoc(r.path); toast(t("Imported {name}",{name:imp.name}));
    }catch(e){ $("#imp-go").disabled=false; toast(e.message,true) }
  };
  $("#imp-name").select();
}
const file_stem=n=>String(n||"").replace(/\.[^.]+$/,"");

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
const fnTone=id=>FN_TOTAL.has(id)?"t-total":"t-"+(FN_TONE[id]||"draft");
/* A band takes the colour of where it lands: that is the outcome it reports. */
const fnBand=id=>"b-"+(FN_TONE[id]||"draft");

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
const IV_STATUSES=new Set(["interviewing","offer","accepted","refused",
  "rejected_interviewing","ghosted_interviewing"]);
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
    (first&&!f.since?" since "+shortDate(first):"")+" · "+live+" still in play":"";
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
  let h="";
  st.forEach(([label,n],i)=>{
    if(i){
      const d=st[i-1][1], r=d?Math.round(n/d*100):null;
      h+='<div class="fj-conv'+(i===worst?" acc":"")+'" style="animation-delay:'+(.55+i*.18)+'s">'+
        '<svg width="26" height="12" viewBox="0 0 26 12" aria-hidden="true"><path d="M0 6h22M17 1l5 5-5 5" '+
        'fill="none" stroke="currentColor" stroke-width="1.6"/></svg><b>'+(r==null?"–":r+"%")+'</b>'+
        '<span>'+conv[i-1][0]+(i===worst?'<br>lowest step':'')+'</span></div>';
    }
    const sub=i?(sent?Math.round(n/sent*100)+"% of sent":"–"):
      (t.total?Math.round(n/t.total*100)+"% of "+t.total+" tracked":"");
    const w=sent?Math.max(n?1.5:0,n/sent*100):0;
    h+='<div class="fj-stg fx-r" style="animation-delay:'+(.35+i*.18)+'s"><span class="sl">'+label+'</span>'+
      '<b data-to="'+n+'" data-from="'+(mode==="morph"&&prev[i]!=null?prev[i]:0)+'" data-delay="'+
      (mode==="morph"?0:450+i*180)+'">'+n+'</b><small>'+sub+'</small>'+
      '<span class="fj-bar"><span style="width:'+w+'%;animation-delay:'+(.6+i*.18)+'s"></span></span></div>';
  });
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
  const H=Math.max(380,Math.min(640,side>200&&innerWidth>1180?side-86:nodes.length*42));
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
    return '<g class="sk-hit'+(off?" sk-dim":"")+'" data-node="'+esc(n.id)+'" role="button" tabindex="0" '+
      'aria-label="'+esc(t(n.label))+': '+n.count+'. '+esc(t("List them"))+'" style="animation-delay:'+
      (.7+n.depth*.28).toFixed(2)+'s">'+
      '<rect x="'+(n.x0-6)+'" y="'+(n.y0-10)+'" width="'+(n.x1-n.x0+12)+'" height="'+(h+20)+
      '" fill="transparent"/>'+
      '<rect class="sk-node'+(S.fnode===n.id?" sk-on":"")+'" data-n="'+esc(n.id)+'" x="'+n.x0+
      '" y="'+n.y0+'" width="'+(n.x1-n.x0)+'" height="'+h+'" rx="2.5" style="fill:'+skCol(n.id)+'"/>'+
      (FN_LIVE.includes(n.id)?'<rect class="sk-ring" x="'+(n.x0-3)+'" y="'+(n.y0-3)+'" width="'+
        (n.x1-n.x0+6)+'" height="'+(h+6)+'" rx="4" style="stroke:'+skCol(n.id)+'"/>':"")+'</g>';
  }).join("");

  const chips=g.nodes.map(n=>{
    const cy=(n.y0+n.y1)/2, off=S.fnode&&S.fnode!==n.id&&
      ![...lineage.get(S.fnode)].some(l=>l.sid===n.id||l.tid===n.id);
    return '<div class="sk-chip'+(n.id==="accepted"?" won":"")+(off?" sk-dim":"")+'" data-chip="'+
      esc(n.id)+'" style="left:'+(n.x1+8)+'px;top:'+(cy-12)+'px;animation-delay:'+
      (1.2+n.depth*.28).toFixed(2)+'s"><i style="background:'+skCol(n.id)+'"></i>'+esc(n.label)+
      ' <b>'+n.count+'</b><span>'+Math.round(n.count/total*100)+'%</span>'+
      (FN_LIVE.includes(n.id)?'<em>live</em>':'')+'</div>';
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
    tip.innerHTML='<b>'+esc(n.label)+' · '+n.count+'</b><span>'+Math.round(n.count/total*100)+
      '% of everything tracked</span>'+(who?'<span class="who">'+esc(who)+'</span>':'')+
      '<span class="go">'+(S.fnode===id?"Click to clear":"Click to list them")+'</span>';
    host.append(tip);
    const x=Math.min(n.x1+30,W-tip.offsetWidth-4), y=Math.max(-8,n.y0-tip.offsetHeight-8);
    tip.style.left=x+"px"; tip.style.top=(y<0?n.y1+10:y)+"px";
  };
  host.querySelectorAll("[data-node]").forEach(el=>{
    const id=el.dataset.node, pick=()=>fnPick(id);
    el.onclick=pick;
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
    '<div class="sub">'+avg+' a week on average<br><b>● '+ivs+' interview'+(ivs===1?"":"s")+'</b> in '+
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
    statusTone(j.status)+'"></span>'+esc(prettyStatus(j.status))+'</span><small>'+
    esc(fnNext(j))+'</small></span></button>';
  if(!S.fnode){
    hint.textContent="Hover a stage to trace its path · click to list them";
    const live=fnJobs().filter(j=>["applied","interviewing","offer"].includes(j.status))
      .sort((a,b)=>{ const r={offer:0,interviewing:1,applied:2};
        return r[a.status]-r[b.status]||String(b.updated_at).localeCompare(String(a.updated_at)) });
    host.innerHTML='<div class="fn-ch"><h2>Still in play</h2><span>'+live.length+'</span></div>'+
      (live.length?live.slice(0,5).map(row).join("")
        :'<p class="note">Nothing is waiting on an answer. Everything here has an outcome.</p>')+
      (live.length>5?'<div class="fn-jfoot"><button class="obtn" id="fn-open">All '+live.length+
        ' in Applications</button></div>':'');
  }else{
    const want=new Set((S.nodes&&S.nodes[S.fnode])||[]);
    const rows=fnJobs().filter(j=>want.has(j.status));
    const label=(S.labels&&S.labels[S.fnode])||S.fnode;
    hint.innerHTML="Showing <b>"+esc(label)+"</b> · click it again to clear";
    host.innerHTML='<div class="fn-ch"><span class="sw" style="background:'+skCol(S.fnode)+
      '"></span><h2>'+esc(label)+'</h2><span>'+rows.length+'</span><span class="grow"></span>'+
      '<button class="x" id="fn-clear" title="Back to still in play" aria-label="Clear">&#10005;</button></div>'+
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
  host.querySelectorAll("[data-id]").forEach(b=>b.onclick=()=>{
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
$("#ex-csv").onclick=()=>window.open("/api/jobs/export?format=csv"+tok());
$("#ex-json").onclick=()=>window.open("/api/jobs/export?format=json"+tok());

/* Re-lay on resize. Debounced, because a sankey layout on every pixel of a
   window drag is wasted work. */
let sizeTimer=null;
window.addEventListener("resize",()=>{
  if(S.view==="funnel"&&S.funnel){
    clearTimeout(sizeTimer); sizeTimer=setTimeout(()=>drawSankey("still",false),140);
  }
});

/* =========================================================================
   Calendar

   Interviews, follow-ups and what happened, by date. Nothing here is new data:
   the interview time and its zone, the follow-up date, and the status history
   the funnel is drawn from. Days are days where you are (userTz).
   ========================================================================= */
S.calView="overview"; S.calAnchor=null; S.calTick=null; S.calPop=null;
const DEAD_ST=new Set(["accepted","refused","rejected","ghosted","rejected_interviewing","ghosted_interviewing"]);
const LIVE_ST=new Set(["applied","interviewing","offer"]);
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

/* Every dated thing, in one list: {kind, day, job, at?, late?}. */
/* ---- Next up, above the applications list ------------------------------ */
/* The home screen's one look ahead: the next interview with a countdown, the
   follow-ups that are late, and this week in seven days. It shows on the
   whole list only, and only when one of those has something in it. */
function drawNextUp(){
  const el=$("#nextup"); if(!el) return;
  clearInterval(S.nuTick);
  const f=S.jfilter||{kind:"all"}, q=($("#jobq").value||"").trim();
  if(!S.jready||f.kind!=="all"||q||S.jsel){ el.hidden=true; return }
  const now=new Date(), today=todayKey(), ev=calEvents();
  const next=ev.filter(e=>e.kind==="iv"&&e.at>now).sort((a,b)=>a.at-b.at)[0];
  const fus=ev.filter(e=>e.kind==="fu"&&e.day<=today).sort((a,b)=>a.day.localeCompare(b.day));
  const late=fus.filter(e=>e.late), dueToday=fus.filter(e=>!e.late);
  if(!next&&!fus.length){ el.hidden=true; return }
  const soon=next&&next.at-now<3*36e5;
  const names=list=>{ const n=list.map(e=>e.job.company);
    if(n.length>3) return n.slice(0,3).join(", ")+" +"+(n.length-3);
    try{ return new Intl.ListFormat(uiLocale(),{type:"conjunction"}).format(n) }catch(e){ return n.join(", ") } };
  const ivPart=j=>'<div class="nu-iv">'+companyMark(j)+'<div class="nu-tx">'+
      '<span class="ey"><i></i>'+t(soon?"Interview today":"Next interview")+'</span>'+
      '<span class="who">'+esc(j.company)+' <span>· '+esc(j.title)+'</span></span>'+
      '<span class="sub">'+esc(interviewLine(j))+'</span></div>';
  let html="", cols="";
  if(soon){
    const j=next.job, cvName=j.cv_path?j.cv_path.split("/").pop().replace(/\.(ya?ml|md)$/,""):null;
    const words=(String(j.description||"").match(/\S+/g)||[]).length;
    const li=(ok,yes,no)=>'<li><i class="'+(ok?"ok":"no")+'">'+(ok?"✓":"")+'</i>'+esc(ok?yes:no)+'</li>';
    html=ivPart(j)+'<div class="left"><small>'+t("Starts in")+'</small><b id="nu-left"></b></div>'+
      '<ul class="ready" aria-label="'+esc(t("Ready for it"))+'">'+
        li(cvName,t("CV tailored")+(cvName?" · "+cvName:""),t("No tailored CV yet"))+
        li(words,t("Posting saved"),t("Posting not saved"))+
        li(j.notes,t("Notes written"),t("Notes for this round"))+'</ul>'+
      '<button class="pbtn" data-nu-open="'+esc(j.id)+'">'+t("Prepare")+'</button></div>';
    el.className="nu soon"; el.style.gridTemplateColumns="minmax(0,1fr)";
  }else{
    if(next){
      const j=next.job;
      html+=ivPart(j)+'<div class="end"><div class="cd" id="nu-cd" role="timer"></div><div class="acts">'+
        '<button class="obtn" data-nu-ics="'+esc(j.id)+'">'+t("Add to calendar")+'</button>'+
        '<button class="obtn dark" data-nu-open="'+esc(j.id)+'">'+t("Prepare")+'</button></div></div></div>';
      cols+="minmax(0,1.35fr) ";
    }
    if(fus.length){
      const lead=late.length?late:dueToday, oldest=late.length?dayDiff(late[0].day,today):0;
      const head=late.length?t(late.length===1?"1 follow-up overdue":"{n} follow-ups overdue",{n:late.length})
        :t(dueToday.length===1?"1 follow-up due today":"{n} follow-ups due today",{n:dueToday.length});
      const sub=late.length?(oldest===1?t("The oldest is 1 day late"):t("The oldest is {n} days late",{n:oldest}))
        +(dueToday.length?" · "+t("{n} more due today",{n:dueToday.length}):"")
        :(next?"":t("No interview booked"));
      html+='<div class="nu-fu"><span class="stack">'+lead.slice(0,3).map(e=>companyMark(e.job)).join("")+'</span>'+
        '<div class="nu-tx"><span class="ey '+(late.length?"late":"due")+'"><i></i>'+esc(head)+'</span>'+
        '<span class="who" title="'+esc(lead.map(e=>e.job.company).join(", "))+'">'+esc(names(lead))+'</span>'+
        (sub?'<span class="sub">'+esc(sub)+'</span>':'')+'<button class="lnk" data-nu-show>'+t("Show them")+' →</button></div></div>';
      cols+="minmax(0,1fr) ";
    }
    if(next){
      const mon=monday(today);
      const days=Array.from({length:7},(_,i)=>{
        const k=addDays(mon,i), iv=ev.some(e=>e.kind==="iv"&&e.day===k),
          fu=ev.filter(e=>e.kind==="fu"&&e.day===k), lt=fu.some(e=>e.late);
        const tip=[iv?t("Interview")+" · "+ev.filter(e=>e.kind==="iv"&&e.day===k).map(e=>e.job.company).join(", "):"",
          fu.length?t("Follow up")+" · "+fu.map(e=>e.job.company).join(", "):""].filter(Boolean).join("\n");
        return '<button data-nu-day="'+k+'"'+(k===today?' class="today"':'')+' title="'+esc(tip||fmtKey(k,{weekday:"long",day:"numeric",month:"long"}))+'">'+
          '<small>'+esc(fmtKey(k,{weekday:"short"}))+'</small>'+Number(k.slice(8))+
          '<span class="mk">'+(iv?'<span class="d"></span>':'')+(fu.length?'<span class="r'+(lt?" late":"")+'"></span>':'')+'</span></button>';
      }).join("");
      html+='<div class="nu-wk"><div class="hd">'+t("This week")+'<button data-nu-cal>'+t("Open calendar")+' →</button></div>'+
        '<div class="days">'+days+'</div></div>';
      cols+="300px";
    }
    el.className="nu"+(next?"":" slim");
    el.style.gridTemplateColumns=cols.trim();
  }
  const wasHidden=el.hidden;
  el.innerHTML=html; el.hidden=false;
  el.style.animation=wasHidden?"":"none";
  $$("#nextup [data-nu-open]").forEach(b=>b.onclick=()=>selectJob(b.dataset.nuOpen));
  $$("#nextup [data-nu-ics]").forEach(b=>b.onclick=()=>window.open("/api/calendar.ics?id="+encodeURIComponent(b.dataset.nuIcs)+tok()));
  const sh=$("#nextup [data-nu-show]"); if(sh) sh.onclick=()=>{ S.jfilter={kind:"alert",value:"followup_due"}; S.fnode=null; drawJobs() };
  const cal=$("#nextup [data-nu-cal]"); if(cal) cal.onclick=()=>{ S.calView="overview"; setView("cal") };
  $$("#nextup [data-nu-day]").forEach(b=>b.onclick=()=>{ S.calView="week"; S.calAnchor=b.dataset.nuDay; setView("cal") });
  if(next){
    const p=n=>String(n).padStart(2,"0");
    /* Units short enough to sit beside the digits; French counts days in "j". */
    const U={d:UI_LANG==="fr"?"j":"d", m:UI_LANG==="en"?"m":"min"};
    const tick=()=>{
      const cd=$("#nu-cd"), lf=$("#nu-left");
      if(!cd&&!lf){ clearInterval(S.nuTick); return }
      const left=Math.max(0,next.at-new Date());
      const d=Math.floor(left/864e5), h=Math.floor(left/36e5)%24, m=Math.floor(left/6e4)%60, s=Math.floor(left/1e3)%60;
      /* Days and hours while it is days away, minutes once it is today: no seconds ticking. */
      if(cd){ cd.innerHTML=d?'<b>'+d+'</b><u>'+U.d+'</u><b>'+p(h)+'</b><u>h</u>'
        :'<b>'+p(h)+'</b><u>h</u><b>'+p(m)+'</b><u>'+U.m+'</u>';
        /* Read as one time, not digit by digit; a timer is never announced unasked. */
        cd.setAttribute("aria-label",(d?d+" "+U.d+" ":"")+h+" h "+m+" min") }
      if(lf) lf.textContent=(h?h+" h ":"")+m+" min";
      if(left<=0) drawNextUp();
    };
    tick(); S.nuTick=setInterval(tick,15000);
  }
}

function calEvents(){
  const out=[], today=todayKey();
  (S.jobs||[]).forEach(j=>{
    const at=interviewMoment(j);
    if(at) out.push({kind:"iv",day:dayIn(at),at,job:j});
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
  const wkEnd=addDays(monday(today),6);
  const ivs=ev.filter(e=>e.kind==="iv"&&e.day>=today&&e.day<=wkEnd).length;
  const late=ev.filter(e=>e.kind==="fu"&&e.late).length;
  $("#cal-sub").textContent=[ivs?t(ivs===1?"1 interview this week":"{n} interviews this week",{n:ivs}):"",
    late?t(late===1?"1 follow-up overdue":"{n} follow-ups overdue",{n:late}):""].filter(Boolean).join(" · ");
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
$("#cal-ics").onclick=()=>window.open("/api/calendar.ics"+tok());
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
    '<b>'+hmIn(date,tz)+'</b><span>'+esc(label)+'</span></div>';
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
        '<span class="cal-check"><i class="'+(cvName?"ok":"no")+'">'+(cvName?"✓":"")+'</i>'+
          (cvName?t("CV tailored")+' · '+esc(cvName):t("No tailored CV yet"))+'</span>'+
        '<span class="cal-check"><i class="'+(words?"ok":"no")+'">'+(words?"✓":"")+'</i>'+
          (words?t("Posting saved"):t("Posting not saved"))+'</span>'+
        '<span class="cal-check"><i class="'+(j.notes?"ok":"no")+'">'+(j.notes?"✓":"")+'</i>'+
          (j.notes?t("Notes written"):t("Notes for this round"))+'</span></div>'+
      '<div class="cal-acts"><button class="obtn dark" data-open-job="'+esc(j.id)+'">'+t("Prepare")+'</button>'+
        '<button class="obtn" data-ics="'+esc(j.id)+'">'+t("Add to my calendar")+'</button></div></div>'+
      '<div class="cal-clocks"><div class="row">'+clockSVG(next.at,mine,t("Your time")+" · "+tzCity(mine),.5)+
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
        '<div class="s"><div class="n">'+p(s)+'</div><div class="u">'+t("sec")+'</div></div>'+
        '<div class="when"><b>'+esc(fmtKey(next.day,{weekday:"long",day:"numeric",month:"long"}))+'</b><br>'+
        esc(hmIn(next.at,userTz()))+' '+t("your time")+'</div>';
    };
    tick(); S.calTick=setInterval(tick,1000);
  }
}
function calMini(ev){
  const today=todayKey(), first=today.slice(0,8)+"01", start=monday(first);
  const month=first.slice(0,7);
  const busy={}, iv=new Set();
  ev.forEach(e=>{ busy[e.day]=(busy[e.day]||0)+1; if(e.kind==="iv") iv.add(e.day) });
  let cells="";
  for(let i=0;i<42;i++){
    const k=addDays(start,i); if(i>=35&&k.slice(0,7)!==month) break;
    const inm=k.slice(0,7)===month, n=busy[k]||0, lv=!inm?"":n>=4?" l3":n>=2?" l2":n?" l1":"";
    cells+='<button class="cal-md'+(inm?"":" out")+lv+(k===today?" today":"")+'" data-day="'+k+'" style="animation-delay:'+
      (.4+i*.018).toFixed(3)+'s"'+(inm?'':' tabindex="-1"')+' aria-label="'+esc(fmtKey(k,{day:"numeric",month:"long"}))+
      (iv.has(k)?', '+t("Interview"):'')+(inm&&n?', '+t("{n} thing(s) planned",{n}):'')+'">'+Number(k.slice(8))+(inm&&iv.has(k)?'<i></i>':'')+'</button>';
  }
  const dows=Array.from({length:7},(_,i)=>'<span class="dw">'+esc(fmtKey(addDays("2026-09-21",i),{weekday:"narrow"}))+'</span>').join("");
  const acc=["var(--bd-inner)","color-mix(in srgb,var(--acc) 22%,var(--field))","color-mix(in srgb,var(--acc) 45%,var(--field))",
    "color-mix(in srgb,var(--acc) 75%,var(--field))"];
  return '<section class="cal-card cal-mini cal-r" style="animation-delay:.1s"><div class="cal-ch"><h2>'+
    esc(fmtKey(first,{month:"long"}))+'</h2><span>'+t("how busy each day was")+'</span></div>'+
    '<div class="cal-mgrid">'+dows+cells+'</div><div class="cal-legend"><span style="display:flex;align-items:center;gap:4px">'+
    t("Quiet")+'<span class="ramp">'+acc.map(c=>'<i style="background:'+c+'"></i>').join("")+'</span>'+t("Busy")+
    '</span><span style="display:flex;align-items:center;gap:6px"><i class="dia"></i>'+t("Interview")+'</span></div></section>';
}
/* Every live application as a lane across eight weeks, three of them ahead: how long it has been
   waiting, when it moved to interviews or an offer, and what is ahead. */
function calJourneys(ev){
  const today=todayKey(), start=addDays(monday(today),-35), days=56, end=addDays(start,days-1);
  const pct=k=>Math.max(0,Math.min(100,(dayDiff(start,k)+.5)/days*100));
  /* Labels near the right edge go to the left of their mark, so none runs
     off the card. */
  const labAt=(p,gap)=>p>80?"right:calc("+(100-p)+"% + "+gap+"px)":"left:calc("+p+"% + "+gap+"px)";
  const recent=j=>{ const h=(j.status_history||[]).slice(-1)[0]; return h&&dayIn(new Date(String(h.at).slice(0,19)))>=start };
  const rank=j=>{ const at=interviewMoment(j); if(at&&at>new Date()) return 0;
    return {offer:1,interviewing:2,applied:3}[j.status]||4 };
  const jobs=(S.jobs||[]).filter(j=>LIVE_ST.has(j.status)||(DEAD_ST.has(j.status)&&recent(j)))
    .sort((a,b)=>rank(a)-rank(b)||(interviewMoment(a)||0)-(interviewMoment(b)||0)||
      String(b.updated_at).localeCompare(String(a.updated_at)));
  const shown=jobs.slice(0,14);
  let ticks="";
  for(let w=0;w<=8;w++){ const k=addDays(start,7*w); if(Math.abs(dayDiff(k,today))<4||dayDiff(start,k)>=days) continue;
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
      const lab=fmtKey(k,{weekday:"short"})+" "+hmIn(at,userTz())+(otherTz(j)?" · "+hmIn(at,j.interview_tz)+" "+tzCity(j.interview_tz):"");
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
    '<div class="jr-legend"><span><i class="bar" style="background:color-mix(in srgb,var(--fn-wait) 40%,transparent)"></i>'+t("Waiting")+'</span>'+
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
function calChip(e){
  const j=e.job, co=esc(j.company);
  const drag=(e.kind==="iv"||e.kind==="fu")?' draggable="true" data-drag="'+e.kind+':'+esc(j.id)+'"':'';
  if(e.kind==="iv") return '<button class="cal-chip iv" data-open-job="'+esc(j.id)+'"'+drag+' title="'+esc(interviewLine(j))+
    '"><i class="pt"></i><span>'+hmIn(e.at,userTz())+' '+co+'</span>'+(otherTz(j)?'<i class="gl">'+GLOBE+'</i>':'')+'</button>';
  if(e.kind==="fu") return '<button class="cal-chip fu'+(e.late?" late":"")+'" data-open-job="'+esc(j.id)+'"'+drag+'><span>'+
    esc(e.late?t("Overdue"):t("Follow up"))+' · '+co+'</span></button>';
  if(e.kind==="of") return '<button class="cal-chip of" data-open-job="'+esc(j.id)+'"><span>'+t("Offer")+' · '+co+'</span></button>';
  if(e.kind==="rp") return '<button class="cal-chip rp" data-open-job="'+esc(j.id)+'"><span>↗ '+co+' · '+t("interviews")+'</span></button>';
  return "";
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
    const sent=list.filter(e=>e.kind==="sent").length, closed=list.filter(e=>e.kind==="closed").length;
    const dnum=Number(k.slice(8));
    cells+='<div class="cal-cell'+(inm?"":" out")+(((i%7)>=5)?" wkend":"")+(k===today?" today":"")+'" data-drop="'+k+'">'+
      '<div class="d"><span>'+dnum+(dnum===1?" "+esc(fmtKey(k,{month:"short"})):"")+'</span></div>'+
      chips.slice(0,3).map(calChip).join("")+(chips.length>3?'<small style="font-size:11px;color:var(--t500)">+'+(chips.length-3)+'</small>':'')+
      ((sent||closed)?'<div class="act">'+(sent?'<span><i style="background:var(--fn-wait)"></i>'+esc(t("{n} sent",{n:sent}))+'</span>':'')+
        (closed?'<span><i style="background:var(--bd-field)"></i>'+esc(t("{n} closed",{n:closed}))+'</span>':'')+'</div>':'')+'</div>';
  }
  const dows=Array.from({length:7},(_,i)=>'<div class="dw">'+esc(fmtKey(addDays("2026-09-21",i),{weekday:"short"}))+'</div>').join("");
  $("#cal-body").innerHTML='<div class="cal-month"><section class="cal-card cal-grid cal-r" style="grid-template-rows:auto repeat('+
    weeks+',minmax(112px,1fr))">'+dows+cells+'</section>'+calComing(ev)+'</div>';
  wireCal(); wireDrag();
}
function calComing(ev){
  const today=todayKey(), end=addDays(today,7), now=new Date();
  const late=ev.filter(e=>e.kind==="fu"&&e.late).sort((a,b)=>a.day.localeCompare(b.day));
  const up=ev.filter(e=>(e.kind==="iv"&&e.at>=now&&e.day<=end)||(e.kind==="fu"&&!e.late&&e.day<=end)||
      (e.kind==="rp"&&e.day===today)||(e.kind==="of"&&e.day===today))
    .sort((a,b)=>a.day.localeCompare(b.day)||(a.at||0)-(b.at||0));
  const col={iv:"var(--fn-positive)",fu:"var(--fn-wait)",rp:"var(--fn-offer)",of:"var(--fn-offer)"};
  const when=e=>(e.day===today?t("Today")+" · ":"")+fmtKey(e.day,{weekday:"short",day:"numeric"})+
    (e.kind==="iv"?" · "+hmIn(e.at,userTz())+(otherTz(e.job)?" "+t("your time"):""):"");
  const what=e=>e.kind==="iv"?t("Interview")+" · "+e.job.company:e.kind==="fu"?t("Follow up with {co}",{co:e.job.company})
    :e.kind==="rp"?t("{co} moved to interviews",{co:e.job.company}):t("Offer")+" · "+e.job.company;
  return '<section class="cal-card cal-side cal-r" style="animation-delay:.1s"><div class="cal-ch"><h2>'+t("Coming up")+
    '</h2><span>'+t("next 7 days")+'</span></div>'+
    (late.length?'<div class="cal-late"><b>'+esc(t(late.length===1?"1 follow-up overdue":"{n} follow-ups overdue",{n:late.length}))+'</b> · '+
      late.slice(0,4).map(e=>esc(e.job.company)+" ("+esc(fmtKey(e.day,{day:"numeric",month:"short"}))+")").join(", ")+'</div>':'')+
    (up.length?up.map(e=>'<button class="cal-item" data-open-job="'+esc(e.job.id)+'"><span class="bar" style="background:'+col[e.kind]+'"></span>'+
      '<span class="tx"><small>'+esc(when(e))+'</small><b>'+esc(what(e))+'</b><span>'+esc(e.job.title)+'</span>'+
      (e.kind==="iv"&&otherTz(e.job)?'<em>'+GLOBE+esc(hmIn(e.at,e.job.interview_tz)+" "+t("in")+" "+tzCity(e.job.interview_tz))+'</em>':'')+
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
  const fus=ev.filter(e=>e.kind==="fu"&&e.day>=start&&e.day<=end);
  const hrs=ivs.map(e=>+hmIn(e.at,userTz()).slice(0,2));
  const h0=Math.min(9,...hrs), h1=Math.max(19,...hrs.map(h=>h+1)), HH=56;
  S.calH0=h0;
  const head='<div class="cal-wh"><div></div>'+days.map(k=>'<div'+(k===today?' class="today"':'')+'><small>'+
    esc(fmtKey(k,{weekday:"short"}))+'</small><b>'+Number(k.slice(8))+'</b></div>').join("")+'</div>';
  const allday='<div class="cal-wa"><div>'+t("Follow-ups")+'</div>'+days.map(k=>'<div data-drop="'+k+'">'+
    fus.filter(e=>e.day===k).map(calChip).join("")+'</div>').join("")+'</div>';
  const hours=Array.from({length:h1-h0},(_,i)=>h0+i);
  const blocks=ivs.map(e=>{
    const [h,m]=hmIn(e.at,userTz()).split(":").map(Number), col=days.indexOf(e.day);
    const top=(h-h0+m/60)*HH+2;
    return '<button class="cal-blk'+(S.calPop===e.job.id?" on":"")+'" data-pop="'+esc(e.job.id)+'" style="left:calc('+col+
      ' * (100% / 7) + 4px);width:calc(100% / 7 - 8px);top:'+top+'px;height:'+(HH-4)+'px"><b>'+hmIn(e.at,userTz())+' '+
      esc(e.job.company)+'</b><span>'+esc(e.job.title)+'</span>'+(otherTz(e.job)?'<em>'+GLOBE+esc(hmIn(e.at,e.job.interview_tz)+
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
  const e=ivs.find(x=>x.job.id===S.calPop); if(!e) return "";
  const j=e.job, two=otherTz(j);
  const col=((dayDiff(monday(e.day),e.day))+7)%7, [h,m]=hmIn(e.at,userTz()).split(":").map(Number);
  const top=Math.max(8,Math.min(360,110+(h-S.calH0+m/60)*56-40));
  const side=col>=4?"right:calc("+(7-col)+" * ((100% - 64px) / 7) + 12px)":"left:calc(64px + "+(col+1)+" * ((100% - 64px) / 7) + 12px)";
  return '<div class="cal-pop" style="'+side+';top:'+top+'px" role="dialog" aria-label="'+esc(t("Interview")+" · "+j.company)+'">'+
    '<button class="x" id="cal-pop-x" aria-label="'+esc(t("Close"))+'">✕</button>'+
    '<div class="hd">'+companyMark(j)+'<div><b>'+esc(t("Interview")+" · "+j.company)+'</b><span>'+esc(j.title)+
      (j.location?" · "+esc(j.location):"")+'</span></div></div>'+
    '<div class="tz"><div><small>'+t("Your time")+'</small><b>'+esc(fmtKey(e.day,{weekday:"short",day:"numeric"})+" · "+hmIn(e.at,userTz()))+'</b></div>'+
      (two?'<div><small>'+esc(t("In {city}",{city:tzCity(j.interview_tz)}))+'</small><b>'+esc(hmIn(e.at,j.interview_tz))+'</b></div>'
        :'<div><small>'+t("Status")+'</small><b>'+esc(prettyStatus(j.status))+'</b></div>')+'</div>'+
    (j.notes?'<p>'+esc(String(j.notes).slice(0,220))+'</p>':'')+
    '<div class="a"><button class="pbtn" data-open-job="'+esc(j.id)+'">'+t("Open application")+'</button>'+
      '<button class="obtn" data-ics="'+esc(j.id)+'">'+t("Add to my calendar")+'</button></div></div>';
}
function wireCal(){
  $$("#cal-body [data-open-job]").forEach(el=>el.onclick=ev=>{ if(ev.target.closest("[data-pop]")) return; openJob(el.dataset.openJob) });
  $$("#cal-body [data-ics]").forEach(el=>el.onclick=ev=>{ ev.stopPropagation(); window.open("/api/calendar.ics?id="+encodeURIComponent(el.dataset.ics)+tok()) });
  $$("#cal-body .cal-md[data-day]").forEach(el=>el.onclick=()=>{ if(el.classList.contains("out")) return;
    S.calView="month"; S.calAnchor=el.dataset.day; drawCalendar(false) });
}

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
    $("#nd-base").innerHTML='<option value="">A blank starter</option>'+
      forKind(kind).map(d=>'<option value="'+esc(d.path)+'"'+
        (d.path===S.path?" selected":"")+'>'+esc(d.label)+
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
  .replace(/^engineeringresumes$/,"Engineering résumés")
  .replace(/^(.)/,c=>c.toUpperCase());

/* One design for every language of a CV, kept on the source: a design edited
   on a translation would be overwritten the next time the source changed. */
$("#btn-design").onclick=async()=>{
  const fam=S.doc&&S.doc.family;
  if(fam&&fam.source&&fam.source!==S.path){
    if(S.dirty&&!confirm("You have unsaved changes. Discard them?")) return;
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
  header:"Your name, headline and contact line at the top of page one.",
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
function paintThemes(){
  const themes=(S.state&&S.state.themes)||[];
  const cur=DZ.theme||themes[0];
  const by=(S.thumbs&&S.thumbs.by)||{};
  const live=S.render&&S.render.pngs&&S.render.pngs[0];
  $("#themegrid").innerHTML=themes.map(t=>{
    const img=t===cur&&live?live+tok():(by[t]?by[t].png+tok():null);
    const pp=S.themePages[t];
    return '<button class="thumbwrap'+(t===cur?" sel":"")+'" role="radio" aria-checked="'+
      String(t===cur)+'" data-theme="'+esc(t)+'">'+
      (img?'<img src="'+esc(img)+'" alt="">':thumbHTML(t))+
      '<span class="thumbcap"><span>'+esc(themeLabel(t))+'</span>'+
      '<em>'+(pp?pp+" page"+(pp===1?"":"s"):"")+'</em></span></button>';
  }).join("");
  $$("#themegrid [data-theme]").forEach(b=>b.onclick=()=>{
    if(b.dataset.theme===cur) return;
    DZ.theme=b.dataset.theme; S.schema=null;
    paintThemes(); touch();
    ensureSchema().then(()=>{ paintAdvanced(); paintDesignNav() });
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
  const at=' data-d=\''+dp+'\' aria-label="'+lbl+'"';
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
  if(!confirm("Remove the photo from your CV Studio folder? It comes off every CV that shows it."))
    return;
  try{
    const r=await post("/api/photo/remove",{});
    S.state.photo=null;
    if(S.path){
      const d=await api("/api/doc?path="+encodeURIComponent(S.path));
      S.doc=d; S.data=d.data?JSON.parse(JSON.stringify(d.data)):null; S.docMtime=d.mtime;
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
      rows+='<div class="dz-row" data-def=\''+esc(JSON.stringify(f.default==null?null:f.default))+
        '\' data-was=\''+esc(JSON.stringify(v==null?null:v))+'\'><label><i class="dz-dot" title="Changed from the theme default"></i><span>'+
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
      ?(!(S.state&&S.state.photo)?"None":photoOn()?"On":"Off"):String(g.fields.length),
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
    ? "The starting point for every other setting. Each tile is this document in that theme."
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
    : '<p class="note muted">Rendering…</p>';
  if(!r){ $("#dz-effect").innerHTML='<span>Nothing rendered yet</span>'; return }
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

/* =========================================================================
   Settings and updates
   ========================================================================= */
/* Preferences are per-machine conveniences, so they live beside the app
   rather than in the workspace: a workspace copied to another machine should
   carry documents, not window preferences. The server keeps them in a file
   and hands them over with the page (see load_prefs); this browser's storage
   keeps a copy for a page served without them. */
const PREFS_KEY="cvstudio.prefs";
function prefs(){ return window.CVS_PREFS||(window.CVS_PREFS={}) }
function setPref(k,v){
  const p=prefs(); p[k]=v;
  try{ localStorage.setItem(PREFS_KEY,JSON.stringify(p)) }catch(e){}
  post("/api/prefs",{set:{[k]:v}}).catch(()=>{});
}
if(window.CVS_PREFS_MOVE&&Object.keys(prefs()).length)
  setTimeout(()=>post("/api/prefs",{replace:prefs()}).catch(()=>{}),0);
/* "system" means take the attribute off and let prefers-color-scheme decide;
   anything else pins it. Everything downstream is a CSS variable, so nothing
   needs redrawing -- including the funnel, which is styled rather than filled. */
const ACCENTS=[["ochre","Ochre"],["indigo","Indigo"],["teal","Teal"],
  ["rose","Rose"],["moss","Moss"]];
function applyAppearance(){
  const p=prefs(), a=p.appearance||"system";
  if(a==="system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme=a;
  /* Ochre is what :root already defines, so it is the absence of an override
     rather than one more rule to keep in step with the others. */
  const acc=p.accent||"ochre";
  if(acc==="ochre") delete document.documentElement.dataset.accent;
  else document.documentElement.dataset.accent=acc;
}
applyAppearance();

/* A settings row says what its control is in the words beside it: tie them
   together, so a screen reader reads the title, then the explanation. */
function nameRows(){
  document.querySelectorAll(".srow").forEach((row,i)=>{
    const b=row.querySelector(":scope>div>b"), d=row.querySelector(":scope>div>span");
    if(!b) return;
    b.id=b.id||"srow-t"+i; if(d) d.id=d.id||"srow-d"+i;
    row.querySelectorAll("select,input,textarea").forEach(c=>{
      if(c.getAttribute("aria-label")||c.getAttribute("aria-labelledby")) return;
      c.setAttribute("aria-labelledby",b.id); if(d) c.setAttribute("aria-describedby",d.id);
    });
  });
}
function openSettings(pane){
  if(!openSettings.named){ nameRows(); openSettings.named=true }
  $("#ovl-design").hidden=true;
  $("#ovl-settings").hidden=false;
  fillSettings();
  showSettingsPane(pane||"workspace");
}
$("#btn-settings").onclick=()=>{
  if($("#ovl-settings").hidden) openSettings("workspace");
  else closeOverlays();
};
/* Something drawn as a button that is not a <button> answers Enter and
   Space the way one would, unless it already handles them itself. */
document.addEventListener("keydown",e=>{
  if(e.defaultPrevented||(e.key!=="Enter"&&e.key!==" ")) return;
  const el=e.target;
  if(!el.matches||!el.matches('[role=button]:not(button)')) return;
  e.preventDefault(); el.dispatchEvent(new MouseEvent("click",{bubbles:true}));
});
/* The one shortcut every desktop user tries. */
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&e.key===","){
    e.preventDefault();
    if($("#ovl-settings").hidden) openSettings("workspace"); else closeOverlays();
  }
});
function showSettingsPane(which){
  $$("#set-rail button").forEach(x=>
    x.setAttribute("aria-selected",String(x.dataset.s===which)));
  ["workspace","editor","region","notify","browser","ai","api","updates","about"].forEach(k=>
    $("#sp-"+k).hidden = k!==which);
  if(which==="updates") checkUpdates(true);
  if(which==="ai") loadAI();
  if(which==="browser") fillClip();
  if(which==="about") $("#s-logs").onclick=()=>post("/api/reveal",{logs:true}).catch(e=>toast(e.message,true));
}
/* Save to CV Studio: the bookmark is made by the server, for the port it
   listens on for it. Clicking it here, rather than dragging, would run it on
   this page: say what to do instead. */
async function fillClip(){
  const a=$("#s-clip-bm"), st=$("#s-clip-state");
  try{
    const r=await api("/api/clip");
    a.href=r.bookmarklet;
    st.textContent=r.ok?t("Ready"):t("Not available");
    st.className="clip-state "+(r.ok?"ok":"bad");
    st.title=r.ok?"":r.why;
    if(!r.ok) $("#s-clip-say").textContent=t("Another program is using the address the button needs ({why}). Quit it, or the other copy of CV Studio, then restart this one.",{why:r.why});
  }catch(e){ st.textContent=t("Not available"); st.className="clip-state bad" }
  a.onclick=e=>{ e.preventDefault(); toast(t("Drag it to your bookmarks bar, then click it there on a job page.")) };
  $("#s-clip-copy").onclick=async()=>{
    try{ await navigator.clipboard.writeText(a.href); toast(t("Copied")) }
    catch(e){ toast(t("Could not copy: select the text instead."),true) }
  };
}
$$("#set-rail button").forEach(b=>b.onclick=()=>showSettingsPane(b.dataset.s));
$$("[data-copy]").forEach(b=>b.onclick=async()=>{
  try{ await navigator.clipboard.writeText($("#"+b.dataset.copy).textContent);
       toast("Copied") }
  catch(e){ toast("Select the text and copy manually",true) }
});

function fillSettings(){
  fillNotify();
  fillBackups();
  const st=S.state||{}, base=location.origin, pr=prefs();
  $("#s-ws").textContent=st.workspace||"";
  $("#s-count").textContent=(st.documents||[]).length+" documents";
  $("#s-base").textContent=base;
  $("#s-spec").href=base+"/api/docs"+(st.api_token?"?token="+
    encodeURIComponent(st.api_token):"");
  $("#s-ver").textContent="CV Studio "+(st.version||"");
  $("#s-open").onclick=async()=>{
    try{ await post("/api/reveal",{}) }catch(e){ toast(e.message,true) }
  };
  $("#s-setup").onclick=()=>{ closeOverlays(); onboardingSheet() };
  const ul=$("#s-uilang");
  ul.value=pr.ui_lang||"";
  ul.onchange=()=>{ setPref("ui_lang",ul.value||null); location.reload() };
  const ml=$("#s-multilang"), cvl=$("#s-cvlang"), bcv=st.base;
  ml.checked=multiLang();
  ml.onchange=()=>{ setPref("multilang",ml.checked); applyMultiLang(); paintBase();
    if(S.view==="docs") drawDocuments(); if(S.jsel&&S.view==="jobs") drawJobs(); fillSettings() };
  /* With one language, which one it is, written onto the base CV. With
     several, each base CV already says its own on Documents. */
  $("#s-cvlang-row").hidden=ml.checked||!bcv||bcv.missing;
  const cur=(((st.documents||[]).find(d=>bcv&&d.path===bcv.path))||{}).lang||"en";
  cvl.innerHTML=(st.languages||[]).map(l=>'<option value="'+l.code+'"'+(l.code===cur?" selected":"")+'>'+
    esc(l.native)+(l.native!==l.english?' · '+esc(l.english):'')+'</option>').join("");
  cvl.onchange=async()=>{
    const l=(st.languages||[]).find(x=>x.code===cvl.value); if(!l||!bcv) return;
    try{
      const r=await post("/api/language/set",{path:bcv.path,language:l.code});
      if(r&&r.ok===false) throw new Error(r.error||"Could not save");
      const s2=await api("/api/state"); S.state=s2; renderDocs(s2.documents);
      S.baseThumb=null; paintBase(); toast(t("Saved"));
    }catch(e){ toast(e.message,true) }
  };
  const tzs=$("#s-tz");
  tzs.innerHTML='<option value="">'+esc(t("Match system"))+' · '+esc(tzCity(machineTz()))+'</option>'+
    tzOptions(pr.tz||"",null);
  tzs.value=pr.tz||"";
  tzs.onchange=()=>{ setPref("tz",tzs.value||null);
    try{ sessionStorage.setItem("cvs.reopen","region") }catch(e){}
    location.reload() };
  drawTzMap(tzs);
  const sb=$("#s-sample");
  sb.textContent=st.sample?"Back to my workspace":"Open sample data";
  sb.onclick=()=>setSample(!st.sample,sb);
  $("#s-exp").onclick=()=>window.open("/api/jobs/export?format=json"+tok());
  /* Revealing is deliberate and one click; copying never needs it. */
  const key=$("#s-key");
  key.hidden=!(S.state&&S.state.api_token);
  key.textContent=S.keyShown?"Hide the key":"Show the key";
  key.onclick=()=>{ S.keyShown=!S.keyShown; fillSettings() };
  $("#s-check").onclick=()=>checkUpdates(true);

  const live=$("#s-live");
  live.checked=pr.live!==false;
  live.onchange=()=>setPref("live",live.checked);
  const delay=$("#s-delay");
  delay.value=String(pr.delay||700);
  delay.onchange=()=>setPref("delay",Number(delay.value));
  const acc=prefs().accent||"ochre";
  $("#s-accent").innerHTML=ACCENTS.map(([id,label])=>
    '<button data-accent="'+id+'" title="'+label+'" aria-label="'+label+
    '" aria-pressed="'+String(id===acc)+'"></button>').join("");
  $$("#s-accent button").forEach(b=>{
    /* Painted from the theme's own token rather than a colour repeated here,
       so a swatch can never drift from what it selects. */
    b.style.background=getComputedStyle(document.documentElement)
      .getPropertyValue("--sw-"+b.dataset.accent).trim();
    b.onclick=()=>{ setPref("accent",b.dataset.accent); applyAppearance();
      $$("#s-accent button").forEach(x=>
        x.setAttribute("aria-pressed",String(x===b))); };
  });
  const ap=$("#s-appearance");
  ap.value=pr.appearance||"system";
  ap.onchange=()=>{ setPref("appearance",ap.value); applyAppearance() };
  const dt=$("#s-deftheme");
  if(dt&&!dt.dataset.filled){
    dt.innerHTML=(st.themes||[]).map(t=>"<option value=\""+esc(t)+"\">"+esc(themeLabel(t))+"</option>").join("");
    dt.dataset.filled="1";
  }
  if(dt){ dt.value=pr.theme||(st.themes||[])[0]||"";
          dt.onchange=()=>setPref("theme",dt.value) }

  /* The hand-setup snippets come from the server: each client has its own
     config format, and there is no reason for two places to know both. */
  fillAIPanel();

  /* Masked by default: this pane ends up in screenshots and screen shares,
     and the key in it is live. Copy still copies the real thing. */
  const shown=st.api_token&&S.keyShown?st.api_token
    :st.api_token?"•".repeat(16):"";
  const auth=st.api_token?' \
  -H "X-API-Key: '+shown+'"':"";
  $("#s-curl").textContent=
    "curl "+base+"/api/state"+auth+"\n\n"+
    "curl -X POST "+base+"/api/render"+auth+" \\\n"+
    '  -H "Content-Type: application/json" \\\n'+
    "  -d '{\"path\":\"profile/my-cv.yaml\"}'";
  $("#s-auth").textContent=st.api_token
    ? "An X-API-Key header is required; the key is shown in the example below."
    : "None needed. The server accepts local connections only. Start it with "+
      "--token to require a key, or --host to expose it, which forces one.";
}

/* ---- what a new version says about itself ------------------------------
   The release notes are assembled from CHANGELOG.md and land in the updater
   manifest, so `body` is a real account of what changed rather than the same
   install note every time. The install note is still on the end of it, inside
   a <details>, which is for someone downloading the file by hand -- this app
   is already installed. Cut it off.

   Rendered rather than printed: the notes are markdown, and a wall of "- **"
   reads worse than nothing. Escaped first, then the three marks the notes
   actually use are put back, so nothing in a release body can inject markup. */
function updateNotes(body){
  const head=String(body||"").split(/\n---\s*\n|<details/)[0].trim();
  if(!head) return "";
  const inline=t=>esc(t)
    .replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>")
    .replace(/`([^`]+)`/g,'<code>$1</code>');
  /* Blocks, not lines. The notes are hard-wrapped, so a bullet is its "- "
     line plus every continuation under it; taking each line as its own block
     turned one bullet into a list item followed by a stray paragraph. */
  const blocks=[];
  for(const raw of head.split("\n")){
    const item=raw.match(/^\s*[-*]\s+(.*)$/);
    const last=blocks.length?blocks[blocks.length-1]:null;
    if(item) blocks.push({list:true,text:item[1]});
    else if(!raw.trim()){ if(last) blocks.push(null) }
    else if(last) last.text+=" "+raw.trim();
    else blocks.push({list:false,text:raw.trim()});
  }
  let html="", list=false;
  for(const b of blocks){
    if(!b){ if(list){ html+="</ul>"; list=false } continue }
    if(b.list&&!list){ html+="<ul>"; list=true }
    if(!b.list&&list){ html+="</ul>"; list=false }
    html+=b.list?"<li>"+inline(b.text)+"</li>":"<p>"+inline(b.text)+"</p>";
  }
  return html+(list?"</ul>":"");
}

/* One download, wherever it was started from: the panel in Settings and the
   panel that comes to you both report into `say`. */
async function installUpdate(up,say,done){
  const T=window.__TAURI__;
  let total=0, got=0;
  try{
    await up.downloadAndInstall(e=>{
      if(e.event==="Started") total=e.data.contentLength||0;
      if(e.event==="Progress"){
        got+=e.data.chunkLength||0;
        say(total?"Downloading "+Math.round(got/total*100)+"%":"Downloading\u2026");
      }
      if(e.event==="Finished") say("Installing\u2026");
    });
    say("Restarting\u2026");
    if(T.process&&T.process.relaunch) await T.process.relaunch();
  }catch(err){ say("Update failed: "+err); if(done) done(err) }
}

/* An update used to arrive as a toast saying to go and look in Settings, which
   is a notification about a notification. It comes to you now. Once per
   version: saying Later means later, not at every launch until you give in --
   Settings still has it whenever you want it. */
function updateSheet(up){
  if(!$("#sheet").hidden) return;      /* never over something being filled in */
  const have=(S.state&&S.state.version)||"";
  const notes=updateNotes(up.body);
  openSheet(
    '<div><h3 id="sheet-title">CV Studio '+esc(up.version)+' is ready</h3>'+
    (have?'<p>You have '+esc(have)+'. It installs and restarts in one step; '+
          'nothing in your workspace is touched.</p>':"")+'</div>'+
    (notes?'<div class="relnotes" id="u-notes">'+notes+'</div>':"")+
    '<div class="foot"><span class="mono" id="u-say"></span>'+
    '<button class="sbtn" id="u-later">Later</button>'+
    '<button class="sbtn primary" id="u-now">Install and restart</button></div>');
  $("#u-later").onclick=()=>{ setPref("skipUpdate",up.version); closeSheet() };
  $("#u-now").onclick=()=>{
    $("#u-now").disabled=true; $("#u-later").disabled=true;
    installUpdate(up,t=>{ $("#u-say").textContent=t },
      ()=>{ $("#u-now").disabled=false; $("#u-later").disabled=false });
  };
}

/* Tauri's updater verifies a signature against the public key baked into the
   build, so a compromised release host still cannot push a package this app
   will install. */
async function checkUpdates(loud){
  const T=window.__TAURI__;
  const st=$("#u-state"), act=$("#u-actions");
  if(!T||!T.updater){
    if(st) st.textContent="Updates are available in the desktop app only.";
    return;
  }
  if(st) st.innerHTML='<span class="spin"></span> Checking for updates…';
  if(act) act.innerHTML="";
  try{
    const up=await T.updater.check();
    if(!up){ if(st) st.textContent="You are on the latest version."; return }
    if(st) st.innerHTML="Version <b>"+esc(up.version)+"</b> is available."+
      (up.body?'<br>'+esc(up.body).slice(0,300):"");
    if(act){
      act.innerHTML='<button class="sbtn primary" id="u-go">Download and install</button>';
      $("#u-go").onclick=()=>{
        $("#u-go").disabled=true;
        installUpdate(up,t=>{ st.textContent=t },
          ()=>{ $("#u-go").disabled=false });
      };
    }
    if(!loud&&prefs().skipUpdate!==up.version) updateSheet(up);
  }catch(e){
    /* A 404 here almost always means no release has been published yet, or the
       repository is private so the asset cannot be fetched without credentials.
       Saying that is more useful than relaying the transport error. */
    const raw=String(e);
    if(st) st.textContent=/release JSON|404|not found/i.test(raw)
      ? "No published release to update to yet. Updates begin working once a version "+
        "is tagged and the release is publicly downloadable."
      : "Could not check for updates: "+raw;
  }
}
setTimeout(()=>{ if(window.__TAURI__&&window.__TAURI__.updater) checkUpdates(false) },4000);

/* The YAML tab is where the model actually writes, so it is the one view where
   losing the selection hurts most. The source lines come from ruamel, which
   knows exactly where it parsed each node -- no guessing, no string search.
   Drawn as a band behind the text rather than by re-marking the highlighted
   HTML, so syntax colouring and the invisible textarea both stay untouched. */
function selectedLines(){
  const m=(S.doc&&S.doc.lines)||{}, sel=S.sel;
  if(!sel) return null;
  if(sel.kind==="header") return m.header||null;
  if(sel.kind==="entry"){
    return m[sel.name+"/"+sel.i]||m[sel.name]||null;
  }
  return m[sel.name]||null;
}
/* The other direction of the same thread. S.doc.lines maps every block to the
   span of source that holds it; this reads it backwards, so the line the caret
   is on says which entry you are in and the page can follow the source the way
   it follows the form. Narrowest span wins, or an entry would always lose to
   the section around it. */
function blockAtLine(line){
  const m=(S.doc&&S.doc.lines)||{};
  let best=null, width=Infinity;
  for(const key of Object.keys(m)){
    const span=m[key];
    if(!span) continue;
    const end=Math.max(span[0]+1,span[1]);
    if(line<span[0]||line>=end) continue;
    const w=end-span[0];
    if(w>=width) continue;
    width=w;
    const cut=key.lastIndexOf("/");
    best=key==="header" ? {kind:"header"}
       : cut<0 ? {kind:"section",name:key}
       : {kind:"entry",name:key.slice(0,cut),i:+key.slice(cut+1)};
  }
  return best;
}
/* selectionchange rather than click: the caret arrives by arrow key and by
   typing at least as often as by mouse. Nothing happens unless it has crossed
   into a different block, so walking within one entry costs nothing. */
document.addEventListener("selectionchange",()=>{
  if(S.view!=="cvs"||S.tab!=="yaml") return;
  const ta=$("#yaml");
  if(document.activeElement!==ta) return;
  const line=ta.value.slice(0,ta.selectionStart).split("\n").length-1;
  const at=blockAtLine(line);
  if(at&&!sameSel(at,S.sel)) select(at,"yaml");
});
function markYamlSelection(){
  const wrap=$(".edwrap"), ta=$("#yaml");
  if(!wrap||!ta) return;
  let band=wrap.querySelector(".yband");
  const span=selectedLines();
  if(!span){ if(band) band.remove(); return }
  if(!band){
    band=document.createElement("div");
    band.className="yband";
    wrap.insertBefore(band,wrap.firstChild);
  }
  /* Line height and padding come from the computed style rather than repeating
     the numbers here, so the band cannot drift if the type changes. */
  const cs=getComputedStyle(ta);
  const lh=parseFloat(cs.lineHeight), top=parseFloat(cs.paddingTop);
  band.style.top=(top+span[0]*lh)+"px";
  band.style.height=(Math.max(1,span[1]-span[0])*lh)+"px";
  band.style.transform="translateY("+(-ta.scrollTop)+"px)";
}
/* Keep it pinned while the source scrolls under it. */
$("#yaml").addEventListener("scroll",()=>{
  const band=$(".edwrap .yband");
  if(band) band.style.transform="translateY("+(-$("#yaml").scrollTop)+"px)";
});

/* ---- Search: everything, from anywhere ------------------------------------
   Ctrl K (⌘K on a Mac), or the magnifier in the top bar. Before you type: what
   you opened last, and the places people go. As you type: applications by
   company and role, documents by name and by what is in them (the server reads
   those), the words inside postings and notes, and the places again. */
const PAL={sel:0,items:[],q:"",docs:null,seq:0};
const PAL_ICONS={
  doc:'<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/>',
  letter:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  list:'<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  cal:'<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
  funnel:'<path d="M4 5h16l-6 8v6l-4-2v-4z"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
};
const palIcon=k=>'<span class="ic"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" '+
  'stroke-width="2" aria-hidden="true">'+PAL_ICONS[k]+'</svg></span>';
const PAL_MAC=/Mac|iPhone|iPad/.test(navigator.platform||"");
/* The places, in the order people want them. */
const palPlaces=()=>[
  {label:t("New application"),icon:"plus",run:()=>{ setView("jobs"); newJobSheet() }},
  {label:t("New document"),sub:t("CV or cover letter"),icon:"plus",run:()=>newDocumentSheet()},
  {label:t("Applications"),icon:"list",run:()=>{ S.jfilter={kind:"all",value:""}; setView("jobs"); drawJobs() }},
  {label:t("Documents"),icon:"doc",run:()=>setView("docs")},
  {label:t("Funnel"),icon:"funnel",run:()=>setView("funnel")},
  {label:t("Calendar"),icon:"cal",run:()=>setView("cal")},
  ...[["workspace","Workspace"],["editor","Editor"],["region","Language & region"],["notify","Notifications"],["browser","Browser"],
      ["ai","AI clients"],["api","API"],["updates","Updates"],["about","About"]]
    .map(([k,l])=>({label:t("Settings")+" › "+t(l),icon:"gear",stay:true,run:()=>openSettings(k)})),
];
/* Lower case without accents, one character for one, so a position in the
   folded text is a position in the original. */
const palFold=s=>Array.from(String(s||""),c=>(c.normalize("NFD")[0]||c).toLowerCase()).join("");
const palWords=q=>palFold(q).split(/\s+/).filter(Boolean);
const palHas=(text,words)=>{ const f=palFold(text); return words.every(w=>f.includes(w)) };
/* The text with every word found in it marked. */
function palMark(text,words){
  text=String(text||""); const f=palFold(text), spans=[];
  words.forEach(w=>{ let i=f.indexOf(w); while(i>=0){ spans.push([i,i+w.length]); i=f.indexOf(w,i+w.length) } });
  spans.sort((a,b)=>a[0]-b[0]);
  let out="", at=0;
  spans.forEach(([a,b])=>{ if(a<at) a=at; if(b<=a) return;
    out+=esc(text.slice(at,a))+"<mark>"+esc(text.slice(a,b))+"</mark>"; at=b });
  return out+esc(text.slice(at));
}
/* A few words either side of the first match, on one line. */
function palSnip(text,words){
  const flat=String(text||"").replace(/[#*_>`\[\]]/g,"").replace(/\s+/g," ").trim();
  const at=palFold(flat).indexOf(words[0]); if(at<0) return flat.slice(0,110);
  const a=Math.max(0,at-48), b=Math.min(flat.length,at+80);
  return (a?"…":"")+flat.slice(a,b).trim()+(b<flat.length?"…":"");
}
const palJobLine=j=>[prettyStatus(j.status)?t(prettyStatus(j.status)):"",j.location].filter(Boolean).join(" · ");

function palRemember(item){
  const key=item.job?"job:"+item.job:"doc:"+item.doc;
  const list=(prefs().recent||[]).filter(x=>(x.job?"job:"+x.job:"doc:"+x.doc)!==key);
  list.unshift(item);
  prefs().recent=list.slice(0,6);
  clearTimeout(PAL.saveT);
  /* Walking the list with the arrows opens one application after another:
     remember where it settled, not every stop. */
  PAL.saveT=setTimeout(()=>setPref("recent",prefs().recent),900);
}

function palBuild(){
  const q=PAL.q.trim(), words=palWords(q), groups=[];
  const docs=(S.state&&S.state.documents)||[];
  const docItem=(d,sub,kind)=>({html:palIcon(isLetterPath(d.path)?"letter":"doc"),
    title:esc(d.label||d.path), sub, kind:kind||t(isLetterPath(d.path)?"Letter":"CV"), run:()=>openDoc(d.path)});
  const jobItem=(j,sub,kind,title)=>({html:'<span class="ic">'+companyMark(j)+'</span>',
    title:title||esc(j.company)+" · "+esc(j.title), sub:sub==null?esc(palJobLine(j)):sub,
    kind:kind||t("Application"), run:()=>openJob(j.id)});
  if(!words.length){
    const recent=(prefs().recent||[]).map(r=>{
      if(r.job){ const j=(S.jobs||[]).find(x=>x.id===r.job); return j&&jobItem(j) }
      const d=docs.find(x=>x.path===r.doc); return d&&docItem(d,esc(d.path))
    }).filter(Boolean).slice(0,4);
    if(recent.length) groups.push([t("Recent"),recent]);
    /* New application, New document, Calendar, Funnel, Settings › Notifications. */
    const pl=palPlaces();
    groups.push([t("Go to"),[0,1,5,4,9].map(i=>pl[i])
      .map(p=>({html:palIcon(p.icon),title:esc(p.label),sub:p.sub?esc(p.sub):"",kind:"",stay:p.stay,run:p.run}))]);
  } else {
    const jobs=S.jobs||[], apps=[], inside=[];
    jobs.forEach(j=>{
      const head=[j.company,j.title,j.location].join(" ");
      if(palHas(head,words)) apps.push(jobItem(j,null,null,palMark(j.company,words)+" · "+palMark(j.title,words)));
      else for(const [field,label] of [["notes","Notes"],["description","Posting"]]){
        if(j[field]&&palHas(head+" "+j[field],words)&&palHas(j[field],words.slice(0,1))){
          inside.push(jobItem(j,esc(t(label))+": “"+palMark(palSnip(j[field],words),words)+"”",t(label)));
          break;
        }
      }
    });
    if(apps.length) groups.push([t("Applications"),apps.slice(0,6),apps.length]);
    const seen=new Set(), dl=[];
    docs.forEach(d=>{ if(palHas(d.label+" "+d.path,words)){ seen.add(d.path); dl.push(docItem(d,esc(d.path))) } });
    (PAL.docs||[]).forEach(d=>{ if(seen.has(d.path)) return;
      dl.push(docItem(d,palMark(d.line,words))) });
    if(dl.length) groups.push([t("Documents"),dl.slice(0,6),dl.length]);
    if(inside.length) groups.push([t("In postings and notes"),inside.slice(0,5),inside.length]);
    const places=palPlaces().filter(p=>palHas(p.label,words));
    if(places.length) groups.push([t("Go to"),places.slice(0,4).map(p=>({html:palIcon(p.icon),
      title:palMark(p.label,words),sub:p.sub?esc(p.sub):"",kind:"",stay:p.stay,run:p.run}))]);
  }
  return groups;
}

function palDraw(){
  const groups=palBuild(), list=$("#pal-list");
  PAL.items=[]; let html="";
  groups.forEach(([head,items,n])=>{
    html+='<div class="pal-h" role="presentation">'+esc(head)+(n>1?'<span>'+n+'</span>':"")+'</div>';
    items.forEach(it=>{
      const i=PAL.items.push(it)-1;
      html+='<button type="button" class="pal-it" role="option" id="pal-o'+i+'" data-i="'+i+'" tabindex="-1" '+
        'aria-selected="'+(i===PAL.sel)+'">'+it.html+
        '<span class="tx"><b data-noi18n>'+it.title+'</b>'+(it.sub?'<small data-noi18n>'+it.sub+'</small>':"")+'</span>'+
        (it.kind?'<span class="kd">'+esc(it.kind)+'</span>':"")+'</button>';
    });
  });
  if(!PAL.items.length) html='<div class="pal-none">'+esc(t("Nothing matches “{q}”.",{q:PAL.q.trim()}))+'</div>';
  list.innerHTML=html;
  PAL.sel=Math.min(PAL.sel,Math.max(0,PAL.items.length-1));
  /* The footer agrees with the group counts: each group lists its first few. */
  const n=PAL.items.length, total=groups.reduce((a,[,items,c])=>a+(c||items.length),0);
  $("#pal-count").textContent=PAL.q.trim()?(total>n?t("{n} of {total} shown",{n,total}):t("{n} result(s)",{n})):(PAL_MAC?t("⌘K, from anywhere"):t("Ctrl K, from anywhere"));
  $("#pal-in").setAttribute("aria-activedescendant",n?"pal-o"+PAL.sel:"");
  $$("#pal-list .pal-it").forEach(b=>{
    b.onclick=()=>palRun(+b.dataset.i);
    b.onmousemove=()=>{ if(PAL.sel!==+b.dataset.i){ PAL.sel=+b.dataset.i; palMarkSel() } };
  });
}
function palMarkSel(){
  $$("#pal-list .pal-it").forEach(b=>b.setAttribute("aria-selected",String(+b.dataset.i===PAL.sel)));
  $("#pal-in").setAttribute("aria-activedescendant",PAL.items.length?"pal-o"+PAL.sel:"");
  const el=$("#pal-o"+PAL.sel); if(el) el.scrollIntoView({block:"nearest"});
}
function palRun(i){
  const it=PAL.items[i]; if(!it) return;
  /* Settings opens over the editor; everything else leaves it. */
  if(S.dirty&&!it.stay&&!confirm(t("You have unsaved changes. Discard them?"))) return;
  closePal();
  it.run();
}
async function palFetch(){
  const q=PAL.q.trim(), seq=++PAL.seq;
  if(!q){ PAL.docs=null; return }
  try{
    const r=await api("/api/search?q="+encodeURIComponent(q));
    if(seq!==PAL.seq) return;       /* a newer query already answered */
    PAL.docs=r.documents||[]; palDraw();
  }catch(e){}
}
function openPal(){
  if(onbOpen()) return;
  if(!S.jready) loadJobs(true);
  PAL.q=""; PAL.sel=0; PAL.docs=null;
  $("#pal").hidden=false; $("#pal-scrim").hidden=false;
  const inp=$("#pal-in"); inp.value=""; palDraw(); inp.focus();
}
function closePal(){ $("#pal").hidden=true; $("#pal-scrim").hidden=true }
$("#pal-scrim").onclick=closePal;
$("#btn-search").onclick=()=>$("#pal").hidden?openPal():closePal();
if(PAL_MAC){ $("#tsearch-kbd").textContent="⌘K"; $("#btn-search").title=t("Search everything (⌘K)") }
$("#pal-in").addEventListener("input",e=>{
  PAL.q=e.target.value; PAL.sel=0; palDraw();
  clearTimeout(PAL.fetchT); PAL.fetchT=setTimeout(palFetch,140);
});
$("#pal-in").addEventListener("keydown",e=>{
  const n=PAL.items.length;
  if(e.key==="ArrowDown"||e.key==="ArrowUp"){
    e.preventDefault(); if(!n) return;
    PAL.sel=(PAL.sel+(e.key==="ArrowDown"?1:-1)+n)%n; palMarkSel();
  }else if(e.key==="Enter"){ e.preventDefault(); palRun(PAL.sel) }
  else if(e.key==="Escape"){ e.preventDefault(); e.stopPropagation(); closePal() }
});
document.addEventListener("keydown",e=>{
  if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&!e.altKey&&e.key.toLowerCase()==="k"){
    e.preventDefault(); $("#pal").hidden?openPal():closePal();
  }
});


/* ---- Export both: the CV and the letter for one application ------------- */
document.addEventListener("click",e=>{ const b=e.target.closest("#ap-pack"); if(b) packSheet(b.dataset.job) });
async function packSheet(id){
  const j=(S.jobs||[]).find(x=>x.id===id); if(!j) return;
  let info;
  try{ info=await api("/api/pack/info?job="+encodeURIComponent(id)) }catch(e){ return toast(e.message,true) }
  const both=info.cv&&info.letter, draft=j.status==="pending";
  openSheet('<h3>'+esc(t("Export for {co}",{co:j.company}))+'</h3>'+
    '<p>'+esc(both?t("The CV and the letter for this application, ready to upload or attach."):
      t("This application's document, ready to upload or attach."))+'</p>'+
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
    (draft?'<label class="pk-check"><input type="checkbox" id="pk-applied" checked>'+
      esc(t("Mark the application as applied today"))+'</label>':'')+
    '<p class="pk-note">'+esc(t("Each is rendered from what is saved now."))+'</p>'+
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
    window.open("/api/pack?"+q+tok());
    closeSheet();
    if(mark) saveJob(id,{status:"applied"});
  };
}


/* ---- People on an application, and what to write them --------------------
   A recruiter, a manager, whoever referred you: kept with the application,
   with when you last wrote. The emails are drafted here from what the
   application knows, in its language, and sent from your own mail app. */
const PP_ROLES=["Recruiter","Hiring manager","Interviewer","Referral"];
const ppInitials=n=>String(n||"").split(/\s+/).filter(Boolean).slice(0,2).map(w=>w[0].toUpperCase()).join("")||"@";
const ppTint=n=>["#f3dfb8","#dbe6f2","#dcecd9","#f2dcdc","#e6dff2"][[...String(n||"")].reduce((a,c)=>a+c.charCodeAt(0),0)%5];
/* After an interview in the last few days, the email to write is a thank-you. */
function ppKind(j){
  const h=(j.status_history||[]).filter(x=>x.status==="interviewing").pop();
  const iv=j.interview_at&&new Date(j.interview_at)<new Date()?j.interview_at:(h&&h.at);
  return iv&&(Date.now()-new Date(iv))<5*DAY?"thanks":"followup";
}
const PP_KIND_LABEL={followup:"Follow-up email",thanks:"Thank-you note",next:"Ask about next steps"};
function ppLast(p){
  if(!p.last) return t("not written to from here yet");
  const d=dayDiff(p.last,todayKey());
  return d<=0?t("last written to today"):t("last written to {d}, {n} day(s) ago",{d:fmtKey(p.last,{day:"numeric",month:"short"}),n:d});
}
function peopleHTML(j){
  const ps=j.people||[];
  return '<div class="block" id="ap-people"><div class="bhead"><span class="blabel">'+esc(t("People"))+'</span>'+
    '<button class="obtn" data-pp-add="'+esc(j.id)+'">'+esc(t("+ Add someone"))+'</button></div>'+
    (ps.length?'<div class="pp-list">'+ps.map(p=>{
      const kind=ppKind(j);
      return '<div class="pp-row"><span class="pp-av" aria-hidden="true" style="background:'+ppTint(p.name||p.email)+'">'+
        esc(ppInitials(p.name||p.email))+'</span>'+
        '<span class="pp-tx"><b><span data-noi18n>'+esc(p.name||p.email)+'</span>'+
          (p.role?' <span class="pp-role">· '+(PP_ROLES.includes(p.role)?esc(t(p.role)):'<span data-noi18n>'+esc(p.role)+'</span>')+'</span>':'')+'</b>'+
        '<small>'+(p.email&&p.name?'<span data-noi18n>'+esc(p.email)+'</span> · ':'')+esc(ppLast(p))+'</small></span>'+
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
    followup:{s:"{role}: following up on my application",
      b:"I applied for the {role} role{applied} and wanted to check in on where things stand.\n\nI am still very interested{duty}.\n\nIs there anything else I can send you?"},
    thanks:{s:"Thank you: {role} interview",
      b:"Thank you for your time{met}. I enjoyed hearing about the team and the work, and it made me more keen on the {role} role.\n\n[One thing you talked about that stayed with you.]\n\nI look forward to hearing about the next steps."},
    next:{s:"{role}: next steps",
      b:"Thank you again for the conversation{met}. Could you tell me what the next steps are, and roughly when I might hear back?\n\nI remain very interested in the {role} role at {co}."},
    applied:d=>" on "+d, met:d=>" on "+d, duty:d=>", not least in this part of the role: “"+d+"”"},
  fr:{greet:n=>n?"Bonjour "+n+",":"Bonjour,", sign:"Bien cordialement,",
    followup:{s:"{role} : suivi de ma candidature",
      b:"J'ai postulé au poste de {role}{applied} et je me permets de revenir vers vous pour savoir où en est le processus.\n\nLe poste m'intéresse toujours beaucoup{duty}.\n\nPuis-je vous transmettre autre chose ?"},
    thanks:{s:"Merci pour l'entretien : {role}",
      b:"Merci pour le temps que vous m'avez accordé{met}. J'ai beaucoup apprécié d'en apprendre plus sur l'équipe et le travail, et le poste de {role} m'intéresse d'autant plus.\n\n[Un point de l'échange qui vous a marqué.]\n\nJe reste à votre disposition pour la suite."},
    next:{s:"{role} : prochaines étapes",
      b:"Merci encore pour notre échange{met}. Pourriez-vous m'indiquer les prochaines étapes, et à peu près quand je pourrai avoir un retour ?\n\nLe poste de {role} chez {co} m'intéresse toujours beaucoup."},
    applied:d=>" le "+d, met:d=>" le "+d, duty:d=>", en particulier pour cette partie du poste : « "+d+" »"},
  es:{greet:n=>n?"Hola, "+n+":":"Hola:", sign:"Un saludo,",
    followup:{s:"{role}: seguimiento de mi candidatura",
      b:"Presenté mi candidatura al puesto de {role}{applied} y quería saber en qué punto está el proceso.\n\nEl puesto me sigue interesando mucho{duty}.\n\n¿Puedo enviarte algo más?"},
    thanks:{s:"Gracias por la entrevista: {role}",
      b:"Gracias por tu tiempo{met}. Me gustó mucho conocer mejor el equipo y el trabajo, y el puesto de {role} me interesa todavía más.\n\n[Algo de la conversación que te quedó.]\n\nQuedo a la espera de los próximos pasos."},
    next:{s:"{role}: próximos pasos",
      b:"Gracias de nuevo por la conversación{met}. ¿Podrías decirme cuáles son los próximos pasos y cuándo podría tener noticias?\n\nEl puesto de {role} en {co} me sigue interesando mucho."},
    applied:d=>" el "+d, met:d=>" el "+d, duty:d=>", sobre todo esta parte del puesto: «"+d+"»"},
  pt:{greet:n=>n?"Olá, "+n+",":"Olá,", sign:"Atenciosamente,",
    followup:{s:"{role}: acompanhamento da minha candidatura",
      b:"Candidatei-me à vaga de {role}{applied} e gostaria de saber em que ponto está o processo.\n\nA vaga continua me interessando muito{duty}.\n\nPosso enviar mais alguma coisa?"},
    thanks:{s:"Agradecimento pela entrevista: {role}",
      b:"Agradeço pelo seu tempo{met}. Gostei muito de conhecer melhor o time e o trabalho, e a vaga de {role} me interessa ainda mais.\n\n[Algo da conversa que ficou com você.]\n\nFico no aguardo dos próximos passos."},
    next:{s:"{role}: próximos passos",
      b:"Agradeço novamente pela conversa{met}. Poderia me dizer quais são os próximos passos e quando devo ter um retorno?\n\nA vaga de {role} na {co} continua me interessando muito."},
    applied:d=>" em "+d, met:d=>" em "+d, duty:d=>", principalmente esta parte da vaga: “"+d+"”"},
};
const PP_LOCALE={en:"en-GB",fr:"fr-FR",es:"es-ES",pt:"pt-BR"};
function ppDraft(kind,lang,j,p,ctx){
  const L=PP_TPL[lang]||PP_TPL.en, T=L[kind];
  const day=d=>{ try{ return DTF(PP_LOCALE[lang]||"en-GB",{day:"numeric",month:"long",timeZone:"UTC"}).format(keyDate(d.slice(0,10))) }catch(e){ return d } };
  const fill=s=>s.replace(/\{role\}/g,j.title).replace(/\{co\}/g,j.company)
    .replace(/\{applied\}/g,ctx.applied?L.applied(day(ctx.applied)):"")
    .replace(/\{met\}/g,ctx.interviewed?L.met(day(ctx.interviewed)):"")
    .replace(/\{duty\}/g,ctx.duties&&ctx.duties[0]?L.duty(ctx.duties[0]):"");
  const first=String(p.name||"").trim().split(/\s+/)[0]||"";
  return {subject:fill(T.s), body:L.greet(first)+"\n\n"+fill(T.b)+"\n\n"+L.sign+(ctx.you?"\n"+ctx.you:"")};
}
/* A language named in the interface's own language: "anglais", not "English". */
function langName(code){
  try{ return new Intl.DisplayNames([uiLocale()],{type:"language"}).of(code) }catch(e){ return code }
}
async function draftSheet(j,p,kind){
  let ctx={};
  try{ ctx=await api("/api/jobs/draft?id="+encodeURIComponent(j.id)) }catch(e){}
  const lang=PP_TPL[j.language]?j.language:(PP_TPL[UI_LANG]?UI_LANG:"en");
  openSheet('<h3>'+esc(t(PP_KIND_LABEL[kind]))+'</h3>'+
    '<p>'+esc(t("To {n}, at {co}. Written from this application: edit anything.",{n:p.name||p.email,co:j.company}))+
      (lang!==UI_LANG?' '+esc(t("In {lang}, the application's language.",{lang:langName(lang)})):'')+'</p>'+
    '<div class="pp-kinds" role="radiogroup" aria-label="'+esc(t("Kind of email"))+'">'+
      Object.keys(PP_KIND_LABEL).map(k=>'<button class="pp-kind" role="radio" aria-checked="'+(k===kind)+'" data-k="'+k+'">'+
        esc(t(PP_KIND_LABEL[k]))+'</button>').join("")+'</div>'+
    '<label class="pk-field">'+esc(t("Subject"))+'<input id="dr-sub" spellcheck="true"></label>'+
    '<label class="pk-field">'+esc(t("Message"))+'<textarea id="dr-body" rows="12" spellcheck="true"></textarea></label>'+
    '<label class="pk-check" id="dr-move-row"><input type="checkbox" id="dr-move" checked>'+
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
    await saveJob(j.id,Object.assign({people},move?{followup_date:addDays(todayKey(),7)}:{}));
  };
}

boot();
