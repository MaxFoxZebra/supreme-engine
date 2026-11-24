/* Time zone helpers and the interactive world map picker. */
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
/* The cities people write differently in the interface's languages. */
const TZ_LOCAL={
  fr:{London:"Londres",Lisbon:"Lisbonne",Brussels:"Bruxelles",Vienna:"Vienne",Warsaw:"Varsovie",Copenhagen:"Copenhague",
    Athens:"Athènes",Moscow:"Moscou",Singapore:"Singapour",Mexico_City:"Mexico",Montreal:"Montréal",Rome:"Rome",
    Prague:"Prague",Bucharest:"Bucarest",Cairo:"Le Caire",Algiers:"Alger",Tunis:"Tunis",Casablanca:"Casablanca"},
  es:{London:"Londres",Lisbon:"Lisboa",Brussels:"Bruselas",Vienna:"Viena",Warsaw:"Varsovia",Copenhagen:"Copenhague",
    Athens:"Atenas",Moscow:"Moscú",Singapore:"Singapur",Mexico_City:"Ciudad de México",New_York:"Nueva York",
    Stockholm:"Estocolmo",Berlin:"Berlín",Paris:"París",Munich:"Múnich",Zurich:"Zúrich",Rome:"Roma",Prague:"Praga",
    Tokyo:"Tokio",Cairo:"El Cairo",Amsterdam:"Ámsterdam",Dublin:"Dublín",Helsinki:"Helsinki"},
  pt:{London:"Londres",Lisbon:"Lisboa",Brussels:"Bruxelas",Vienna:"Viena",Warsaw:"Varsóvia",Copenhagen:"Copenhague",
    Athens:"Atenas",Moscow:"Moscou",Singapore:"Singapura",Mexico_City:"Cidade do México",New_York:"Nova York",
    Stockholm:"Estocolmo",Berlin:"Berlim",Munich:"Munique",Zurich:"Zurique",Rome:"Roma",Prague:"Praga",
    Tokyo:"Tóquio",Cairo:"Cairo",Amsterdam:"Amsterdã",Dublin:"Dublin"}};
const tzCity=tz=>{ const k=String(tz||"").split("/").pop();
  return ((TZ_LOCAL[UI_LANG]||{})[k])||TZ_NAMES[k]||k.replace(/_/g," ") };
function fmtWhen(date,tz,withDay=true){
  const o={timeZone:tz,hour:"2-digit",minute:"2-digit",hourCycle:clock12()?"h12":"h23"};
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
  const hm=z=>DTF(uiLocale(),{timeZone:z,hour:"2-digit",minute:"2-digit",hourCycle:clock12()?"h12":"h23"}).format(now);
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
    offer:"Offer", accepted:"Accepted", refused:"Declined by me", rejected:"Rejected",
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
/* How well the base CV matches the posting, as the AI client scored it, out
   of 10. Four bands with their own colour, so a strong one is seen from
   across the room; the bands start where the server's do (jobs.py). */
const MATCH_BANDS=[["top",9,"Top","Top match"],["strong",7,"Strong","Strong match"],
  ["good",5,"Good","Good match"],["fair",3,"Fair","Fair match"],["weak",0,"Weak","Weak match"]];
const MATCH_CRIT={requirements:"Requirements",experience:"Experience",skills:"Skills",domain:"Domain",practical:"Practicalities"};
const hasMatch=j=>!!(j&&j.match&&typeof j.match.score==="number");
const matchBand=sc=>MATCH_BANDS.find(b=>sc>=b[1]);
const matchKey=j=>hasMatch(j)?matchBand(j.match.score)[0]:"none";
/* m-strong, m-good, m-fair, m-weak, m-none: the band's colours. */
const bandCls=k=>"m-"+k;
const matchNum=sc=>Number(sc).toLocaleString(uiLocale(),{minimumFractionDigits:sc>=10?0:1,maximumFractionDigits:sc>=10?0:1});
function matchStaleWords(m){
  const w=(m&&m.stale)||[];
  return w.includes("cv")&&w.includes("posting")?t("Out of date: your base CV and the posting changed since.")
    :w.includes("cv")?t("Out of date: your base CV changed since.")
    :w.includes("posting")?t("Out of date: the posting changed since."):"";
}
function matchRing(m,cls){
  const b=matchBand(m.score), stale=((m.stale||[]).length>0);
  return '<span class="mring '+bandCls(b[0])+(stale?' stale':'')+(cls?' '+cls:'')+'" style="--p:'+
    Math.max(0,Math.min(100,Math.round(m.score*10)))+'"><b>'+esc(matchNum(m.score))+'</b></span>';
}
/* The prompt that gets a score, for when there is none yet. */
const matchAsk=j=>j?t("score my {co} application against my base CV",{co:j.company}):t("score my applications against my base CV");
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
  {id:"welcometothejungle",label:"Welcome to the Jungle",short:"WTTJ",bg:"#FFCD00",fg:"#000",
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
  const units=d.units||[], n=units.length;
  const secCounts=new Map();
  for(const u of units){
    const sec=u.kind==="field"?t("Header"):u.kind==="block"?t(u.label):String(u.where||"").split(" › ")[0]||t("CV");
    secCounts.set(sec,(secCounts.get(sec)||0)+1);
  }
  const pills=[...secCounts.entries()].map(([s,c])=>'<span class="bd-pill" data-noi18n>'+esc(s)+(c>1?' <i>'+c+'</i>':'')+'</span>').join("");
  el.innerHTML=
    '<div class="bd-bar"><p class="bd-head">'+(n
      ?'<b>'+esc(t("{n} change(s)",{n}))+'</b> from <b>'+esc(base)+'</b>'
      :'Same as <b>'+esc(base)+'</b> so far. Nothing has been tailored yet.')+
    '</p>'+(n?'<button class="obtn bd-open" type="button">'+esc(t("See every change beside the page"))+'</button>':'')+'</div>'+
    (pills?'<div class="bd-pills">'+pills+'</div>':'');
  const go=el.querySelector(".bd-open");
  if(go) go.onclick=async()=>{
    if(!await leaveEdits()) return;
    await openDoc(path);
    if(S.path===path) showTab("diff");
  };
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
/* Files out of the app: a PDF, an export, a calendar file. A browser tab
   downloads them itself. The desktop webview drops window.open on the floor
   as it does target=_blank, so Export PDF did nothing there: the page fetches
   the file and the server saves it in Downloads and shows it. `name` is for
   a file the server does not name itself. */
async function download(url,name){
  if(!window.__TAURI__){ window.open(url); return }
  try{
    const r=await fetch(url);
    if(!r.ok){ let m=""; try{ m=(await r.json()).error||"" }catch(e){} throw new Error(m||"HTTP "+r.status) }
    const said=(r.headers.get("Content-Disposition")||"").match(/filename="([^"]+)"/);
    const bytes=new Uint8Array(await r.arrayBuffer());
    let bin="";
    for(let i=0;i<bytes.length;i+=0x8000) bin+=String.fromCharCode.apply(null,bytes.subarray(i,i+0x8000));
    const s=await post("/api/download",{name:(said&&said[1])||name,data:btoa(bin)});
    toast(t("Saved to Downloads: {name}",{name:s.name}));
  }catch(e){ toast(e.message,true) }
}

const appliedAt=j=>{
  const h=j.status_history||[];
  for(const e of h) if(e.status==="applied") return e.at;
  return j.status==="pending"?null:j.created_at;
};
const isoToday=()=>new Date().toISOString().slice(0,10);

