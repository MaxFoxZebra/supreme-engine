/* On-page Markdown cover letter editor, toolbar, and find/replace. */
/* =========================================================================
   Cover letters
   ========================================================================= */
/* A letter is a Markdown file laid out by the app itself, in the look of the
   CV it names. It is written on the page: the body is editable where it
   prints, the letterhead comes from the CV, and place, date, language and the
   CV it looks like are set beside it. The PDF tab is the exact render. */
const LT={path:null, doc:null, meta:{}, body:"", dirty:false, tab:"write", pages:null,
  png:null, rendering:false, mdText:null, t:null, rv:false};
const isLetterPath=p=>/\.md$/i.test(p||"");

async function openLetter(path){
  if(LT.dirty&&LT.path&&LT.path!==path) await ltSave();
  if(path) palRemember({doc:path});
  closeOverlays();
  const seq=S.openSeq=(S.openSeq||0)+1;
  try{
    const d=await api("/api/doc?path="+encodeURIComponent(path));
    if(seq!==S.openSeq) return;
    Object.assign(LT,{path, doc:d, meta:Object.assign({},d.meta), body:d.body, dirty:false,
      tab:"write", pages:null, png:null, mdText:null, rv:false});
  }catch(e){ return seq===S.openSeq?toast(e.message,true):undefined }
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
$("#lt-back").onclick=async()=>{
  if(LT.dirty) await ltSave();
  const j=ltJob();
  LT.path=null;
  if(j){ setView("jobs"); selectJob(j.id); return }
  setView("docs");
};

/* The Markdown a letter uses, as HTML for the page and back: the same
   rules letters.py prints by (parse_md, spans), so the page is the PDF.
   Headings (three levels), paragraphs, hard breaks, bullet and numbered
   lists at any depth, quotes, rules, code blocks, tables; bold, italic,
   strikethrough, code, links and backslash escapes. */
const LT_TOK=/\\([\\`*_{}\[\]()#+\-.!~|>])|`([^`]+)`|\*\*\*(?!\s)(.+?)(?<!\s)\*\*\*|\*\*(?!\s)(.+?)(?<!\s)\*\*|(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)|~~(?!\s)(.+?)(?<!\s)~~|\*(?!\s)(.+?)(?<!\s)\*|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)|\[([^\]]+)\]\(([^)\s]+)\)|(\n)/g;
function ltInline(t){
  t=String(t??"");
  let out="", pos=0; const re=new RegExp(LT_TOK.source,"g"); let m;
  while((m=re.exec(t))){
    out+=esc(t.slice(pos,m.index));
    if(m[1]!=null) out+=esc(m[1]);
    else if(m[2]!=null) out+="<code>"+esc(m[2])+"</code>";
    else if(m[3]!=null) out+="<b><i>"+ltInline(m[3])+"</i></b>";
    else if(m[4]!=null||m[5]!=null) out+="<b>"+ltInline(m[4]??m[5])+"</b>";
    else if(m[6]!=null) out+="<s>"+ltInline(m[6])+"</s>";
    else if(m[7]!=null||m[8]!=null) out+="<i>"+ltInline(m[7]??m[8])+"</i>";
    else if(m[9]!=null) out+='<a href="'+esc(m[10])+'">'+ltInline(m[9])+"</a>";
    else out+="<br>";
    pos=re.lastIndex;
  }
  return out+esc(t.slice(pos));
}
const MD_ITEM=/^(\s*)([-*+•]|\d{1,9}[.)])\s+(.*)$/, MD_HEAD=/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/,
  MD_RULE=/^\s{0,3}([-*_])(\s*\1){2,}\s*$/, MD_FENCE=/^\s{0,3}(```|~~~)/, MD_QUOTE=/^\s{0,3}>\s?(.*)$/,
  MD_SEP=/^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;
const mdCells=l=>{ let s=l.trim(); if(s.startsWith("|")) s=s.slice(1); if(s.endsWith("|")&&!s.endsWith("\\|")) s=s.slice(0,-1);
  return s.split(/(?<!\\)\|/).map(c=>c.trim().replace(/\\\|/g,"|")) };
const mdIsTable=(l,n)=>l.includes("|")&&n!=null&&MD_SEP.test(n)&&n.includes("-");
const mdStarts=(l,n)=>MD_HEAD.test(l)||MD_RULE.test(l)||MD_FENCE.test(l)||MD_QUOTE.test(l)||MD_ITEM.test(l)||mdIsTable(l,n);
function mdJoin(lines){
  return lines.map((l,i)=>{ const hard=/  $/.test(l)||/\\\s*$/.test(l); let t=l.trim();
    if(/[^\\]\\$|^\\$/.test(t)) t=t.slice(0,-1).trimEnd();
    return t+(i<lines.length-1?(hard?"\n":" "):"") }).join("");
}
function mdList(lines){
  const items=[];
  for(const l of lines){ const m=MD_ITEM.exec(l);
    if(m){ const mk=m[2]; items.push([m[1].replace(/\t/g,"    ").length,/\d/.test(mk[0]),/\d/.test(mk[0])?parseInt(mk,10):1,[m[3]]]) }
    else if(items.length) items[items.length-1][3].push(l) }
  const build=(i,base)=>{
    const first=items[i], node={t:first[1]?"ol":"ul",start:first[2],items:[]};
    while(i<items.length&&items[i][0]>=base){
      if(items[i][0]===base&&items[i][1]!==first[1]) break;
      if(items[i][0]>base&&node.items.length){ const [c,j]=build(i,items[i][0]); node.items[node.items.length-1].children.push(c); i=j; continue }
      node.items.push({text:mdJoin(items[i][3]),children:[]}); i++;
    }
    return [node,i];
  };
  const out=[]; let i=0; while(i<items.length){ const [n,j]=build(i,items[i][0]); out.push(n); i=j }
  return out;
}
function mdParse(body){
  const L=String(body||"").replace(/\r\n/g,"\n").replace(/\t/g,"    ").split("\n"), out=[]; let i=0;
  while(i<L.length){
    const l=L[i], n=L[i+1];
    if(!l.trim()){ i++; continue }
    let m=MD_FENCE.exec(l);
    if(m){ const f=m[1], code=[]; i++; while(i<L.length&&!L[i].trim().startsWith(f)) code.push(L[i++]); i++;
      out.push({t:"code",text:code.join("\n")}); continue }
    if((m=MD_HEAD.exec(l))){ out.push({t:"h",level:Math.min(3,m[1].length),text:m[2]}); i++; continue }
    if(MD_RULE.test(l)){ out.push({t:"hr"}); i++; continue }
    if(mdIsTable(l,n)){
      const head=mdCells(l), al=mdCells(n).map(c=>c.startsWith(":")&&c.endsWith(":")?"center":c.endsWith(":")?"right":"left"), rows=[];
      i+=2; while(i<L.length&&L[i].trim()&&L[i].includes("|")) rows.push(mdCells(L[i++]));
      const w=head.length; out.push({t:"table",head,rows:rows.map(r=>r.concat(Array(w).fill("")).slice(0,w)),align:al.concat(Array(w).fill("left")).slice(0,w)});
      continue;
    }
    if(MD_QUOTE.test(l)){ const q=[];
      while(i<L.length&&L[i].trim()&&(MD_QUOTE.test(L[i])||!mdStarts(L[i],null))){ const mm=MD_QUOTE.exec(L[i]); q.push(mm?mm[1]:L[i]); i++ }
      out.push({t:"quote",blocks:mdParse(q.join("\n"))}); continue }
    if(MD_ITEM.test(l)){ const blk=[];
      while(i<L.length){ const x=L[i];
        if(!x.trim()){ let j=i+1; while(j<L.length&&!L[j].trim()) j++;
          if(j<L.length&&(MD_ITEM.test(L[j])||L[j].startsWith("  "))){ i=j; continue } break }
        if(blk.length&&!MD_ITEM.test(x)&&!x.startsWith(" ")&&mdStarts(x,L[i+1])) break;
        blk.push(x); i++ }
      out.push(...mdList(blk)); continue }
    const para=[];
    while(i<L.length&&L[i].trim()){ if(para.length&&mdStarts(L[i],L[i+1])) break; para.push(L[i++]) }
    out.push({t:"p",text:mdJoin(para)});
  }
  return out;
}
/* A new letter's writing prompts are notes to you, not the letter: drawn as
   placeholders, never printed, and one click selects one so what you type
   replaces it. Which paragraphs they are, the server says (letters.py). */
const ltFlat=s=>String(s||"").replace(/\s+/g," ").trim();
const ltPrompts=()=>new Set(((LT.doc&&LT.doc.prompts)||[]).map(ltFlat));
function mdHTML(bl,ps){
  const list=nd=>"<"+(nd.t==="ol"?'ol'+(nd.start!==1?' start="'+nd.start+'"':''):'ul')+">"+
    nd.items.map(it=>"<li>"+ltInline(it.text)+it.children.map(list).join("")+"</li>").join("")+"</"+nd.t+">";
  return bl.map(b=>{
    if(b.t==="p"){ const flat=ltFlat(b.text);
      if(ps&&ps.has(flat)) return '<p class="lt-prompt" data-tip="'+esc(t("Writing prompt · click to replace it"))+'" data-orig="'+esc(flat)+'">'+ltInline(flat)+"</p>";
      return "<p>"+ltInline(b.text)+"</p>" }
    if(b.t==="h") return "<h"+b.level+">"+ltInline(b.text)+"</h"+b.level+">";
    if(b.t==="ul"||b.t==="ol") return list(b);
    if(b.t==="quote") return "<blockquote>"+mdHTML(b.blocks)+"</blockquote>";
    if(b.t==="hr") return "<hr>";
    if(b.t==="code") return "<pre>"+esc(b.text)+"</pre>";
    if(b.t==="table") return "<table><thead><tr>"+b.head.map((c,k)=>'<th style="text-align:'+b.align[k]+'">'+ltInline(c)+"</th>").join("")+
      "</tr></thead><tbody>"+b.rows.map(r=>"<tr>"+r.map((c,k)=>'<td style="text-align:'+b.align[k]+'">'+ltInline(c)+"</td>").join("")+"</tr>").join("")+"</tbody></table>";
    return "";
  }).join("");
}
function ltToHTML(body){ return mdHTML(mdParse(body),ltPrompts())||"<p><br></p>" }

/* And back: what is on the page, as Markdown. Characters that would start
   formatting of their own are escaped, so "C# at 5 * 3" stays as typed. */
function mdEscText(s){
  /* Only what could start formatting: a lone "*" between spaces, a single
     "~", or brackets with no link after them are left as typed. */
  s=s.replace(/[\\`]/g,"\\$&")
    .replace(/\*/g,(m,i,str)=>/\s/.test(str[i-1]||" ")&&/\s/.test(str[i+1]||" ")?"*":"\\*")
    .replace(/~~/g,"\\~\\~");
  if(/\]\(/.test(s)) s=s.replace(/[\[\]]/g,"\\$&");
  return s.replace(/_/g,(m,i,str)=>{ const a=str[i-1], b=str[i+1]; return (!a||/[\W]/.test(a)||!b||/[\W]/.test(b))?"\\_":"_" });
}
const mdEscLead=line=>line.replace(/^(\s*)(#{1,6}(?=\s)|[-+](?=\s)|>|```|~~~)/,"$1\\$2").replace(/^(\s*\d{1,9})([.)])(?=\s)/,"$1\\$2")
  .replace(/^(\s*)([-*_])(?=(\s*\2){2,}\s*$)/,"$1\\$2");
function ltInlineMD(node,cell){
  let out="";
  node.childNodes.forEach(n=>{
    if(n.nodeType===3){ let v=mdEscText(n.nodeValue.replace(/ /g," ").replace(/\u200b/g,"")); if(cell) v=v.replace(/\|/g,"\\|"); out+=v; return }
    if(n.nodeType!==1) return;
    const tg=n.tagName;
    if(tg==="UL"||tg==="OL") return;
    if(tg==="BR"){ out+="\\\n"; return }
    if(tg==="CODE"){ const c=n.textContent; if(c) out+="`"+c.replace(/`/g,"'")+"`"; return }
    const inner=ltInlineMD(n,cell);
    const fw=n.style&&(n.style.fontWeight==="bold"||Number(n.style.fontWeight)>=600);
    const it=n.style&&n.style.fontStyle==="italic";
    const st=n.style&&/line-through/.test(n.style.textDecoration||n.style.textDecorationLine||"");
    const wrap=m=>{ const lead=inner.match(/^\s*/)[0], tail=inner.match(/\s*$/)[0]; return lead+m+inner.trim()+m+tail };
    if(!inner.trim()) out+=inner;
    else if(tg==="A") out+="["+inner.trim()+"]("+(n.getAttribute("href")||"")+")";
    else if(tg==="B"||tg==="STRONG"||fw) out+=wrap("**");
    else if(tg==="I"||tg==="EM"||it) out+=wrap("*");
    else if(tg==="S"||tg==="STRIKE"||tg==="DEL"||st) out+=wrap("~~");
    else if(/^(P|DIV|LI)$/.test(tg)) out+=(out&&!/\s$/.test(out)?" ":"")+inner;
    else out+=inner;
  });
  return out;
}
const mdLines=s=>s.replace(/[ \t]+/g," ").split("\n").map((l,i)=>mdEscLead(l.trim())).join("\n");
function ltToMD(root){
  const out=[];
  const list=(nd,depth)=>{
    const ol=nd.tagName==="OL", start=Number(nd.getAttribute("start"))||1, pad="   ".repeat(depth), lines=[];
    let k=0;
    [...nd.children].forEach(li=>{
      /* A list straight inside a list (what indenting makes) belongs to the
         item before it. */
      if(li.tagName==="UL"||li.tagName==="OL"){ const l=list(li,depth+1); if(l.trim()) lines.push(l); return }
      if(li.tagName!=="LI") return;
      const tx=mdLines(ltInlineMD(li)).replace(/\n/g,"\n"+pad+"   ");
      if(tx.trim()||li.querySelector("ul,ol")) lines.push(pad+(ol?(start+k)+". ":"- ")+tx.replace(/^\s+/,""));
      k++;
      li.querySelectorAll(":scope>ul,:scope>ol").forEach(c=>lines.push(list(c,depth+1)));
    });
    return lines.join("\n");
  };
  const blockOf=(n,acc)=>{
    if(n.nodeType===3){ const tx=n.nodeValue.trim(); if(tx) acc.push(mdEscLead(mdEscText(tx))); return }
    if(n.nodeType!==1) return;
    const tg=n.tagName;
    if(/^H[1-6]$/.test(tg)){ const tx=mdLines(ltInlineMD(n)).replace(/\\\n/g," "); if(tx.trim()) acc.push("#".repeat(Math.min(3,+tg[1]))+" "+tx.replace(/^\\/,"")); return }
    if(tg==="UL"||tg==="OL"){ const l=list(n,0); if(l.trim()) acc.push(l); return }
    if(tg==="HR"){ acc.push("---"); return }
    if(tg==="PRE"){ acc.push("```\n"+n.textContent.replace(/\n$/,"")+"\n```"); return }
    if(tg==="BLOCKQUOTE"){ const inner=[]; n.childNodes.forEach(c=>blockOf(c,inner));
      if(inner.length) acc.push(inner.join("\n\n").split("\n").map(l=>l?"> "+l:">").join("\n")); return }
    if(tg==="TABLE"){
      const rows=[...n.querySelectorAll("tr")].map(tr=>[...tr.children].map(c=>ltInlineMD(c,true).replace(/\\\n|\n/g," ").trim()));
      if(!rows.length) return;
      const w=Math.max(...rows.map(r=>r.length)), pad=r=>r.concat(Array(w).fill("")).slice(0,w);
      const al=[...n.querySelectorAll("tr:first-child>*")].map(c=>c.style.textAlign==="right"?"--:":c.style.textAlign==="center"?":-:":"---");
      acc.push(["| "+pad(rows[0]).join(" | ")+" |","|"+pad(al.length?al:[]).map(a=>a||"---").join("|")+"|"]
        .concat(rows.slice(1).map(r=>"| "+pad(r).join(" | ")+" |")).join("\n"));
      return;
    }
    if(/^(P|DIV|SECTION)$/.test(tg)&&n.querySelector("ul,ol,p,div,h1,h2,h3,h4,blockquote,pre,table,hr")){ n.childNodes.forEach(c=>blockOf(c,acc)); return }
    const tx=mdLines(ltInlineMD(n));
    if(tx.replace(/\\\n/g,"").trim()) acc.push(tx);
  };
  root.childNodes.forEach(c=>blockOf(c,out));
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
  if(LT.rv&&!ltRv()) LT.rv=false;
  ltRvBar();
  $("#lt-tools").hidden=LT.tab!=="write"||LT.rv;
  if((LT.tab!=="write"||LT.rv)&&LTF.open) ltFindClose();
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
    ta.oninput=()=>{ LT.mdText=ta.value; LT.dirty=true; $("#lt-save").disabled=false; paintStatus() };
  }else{
    ltFont(h.font); ltFont(h.name_font);
    const [wmm,hmm]=LT_PAPER[h.paper]||LT_PAPER.a4;
    const W=Math.min(700,Math.max(480,st.clientWidth-60)), u=W/wmm, pt=u*0.3528;
    const mg=h.margins||{}, to=LT.meta.to;
    const toLines=Array.isArray(to)?to:String(to||"").split("\n").filter(x=>x.trim());
    st.innerHTML='<div class="lt-paper" id="lt-paper" style="width:'+W+'px;min-height:'+(hmm*u)+'px;'+
      'padding:'+(ltMM(mg.top)*u)+'px '+(ltMM(mg.right)*u)+'px '+(ltMM(mg.bottom)*u)+'px '+(ltMM(mg.left)*u)+'px;'+
      'box-sizing:border-box;font-family:\''+esc(h.font||"Source Sans 3")+'\',sans-serif;font-size:'+(10.5*pt)+'px;'+
      'line-height:1.52;color:'+esc(h.body_color||"#000")+';--lt-rule:'+esc(h.rule_color||"#000")+';--lt-grey:'+esc(h.contact_color||"#555")+'">'+
      '<div class="lh" id="lt-lh" role="group" aria-label="Letterhead">'+
        '<span class="tag" data-noi18n>'+ltHeadTag(h)+'</span>'+
        '<div class="lhf" contenteditable="true" spellcheck="false" data-k="name" data-ph="Your name" aria-label="Name on the letterhead" '+
          'style="font-family:\''+esc(h.name_font||h.font)+'\',sans-serif;font-size:'+(24*pt)+'px;line-height:1.1;'+
          'font-weight:'+(h.name_bold?700:400)+';color:'+esc(h.name_color)+'">'+esc(h.name)+'</div>'+
        '<div class="lhf" contenteditable="true" spellcheck="true" data-k="headline" data-ph="Headline (optional)" aria-label="Headline on the letterhead" '+
          'style="font-size:'+(11*pt)+'px;color:'+esc(h.headline_color)+'">'+esc(h.headline||"")+'</div>'+
        '<div class="lhf" contenteditable="true" spellcheck="false" data-k="contact" data-ph="Contact details, separated by •" aria-label="Contact line on the letterhead" '+
          'style="margin-top:'+(4*pt)+'px;font-size:'+(9.5*pt)+'px;color:'+esc(h.contact_color)+'">'+
          (h.contact||[]).map(esc).join(' • ')+'</div>'+
        '<div style="margin-top:'+(6*pt)+'px;border-top:'+Math.max(1,0.6*pt)+'px solid '+esc(h.rule_color)+'"></div>'+
      '</div>'+
      (toLines.length?'<div style="margin-top:'+(16*pt)+'px">'+toLines.map(esc).join("<br>")+'</div>':'')+
      '<div class="dl" id="lt-dl" role="button" tabindex="0" style="margin-top:'+(16*pt)+'px;text-align:right;color:'+esc(h.contact_color)+'" title="Set the place and date beside the letter">'+
        esc(LT.doc.date_line||"")+'</div>'+
      '<div class="subj" id="lt-subj" contenteditable="true" spellcheck="true" data-ph="Subject" '+
        'aria-label="Subject" style="margin-top:'+(18*pt)+'px;font-weight:700">'+esc(LT.meta.subject||"")+'</div>'+
      '<div class="ltbody" id="lt-edit" contenteditable="true" spellcheck="true" aria-label="The letter" '+
        'style="margin-top:'+(10*pt)+'px;display:flex;flex-direction:column;gap:'+(8*pt)+'px;text-align:justify">'+
        ltToHTML(LT.body)+'</div>'+
      '<div class="lhf" contenteditable="true" spellcheck="false" data-k="signature" data-ph="Signature" aria-label="Signature" '+
        'style="margin-top:'+(24*pt)+'px;font-weight:700">'+esc(h.signature||h.name)+'</div>'+
    '</div>';
    try{ document.execCommand("styleWithCSS",false,false); document.execCommand("defaultParagraphSeparator",false,"p") }catch(e){}
    const ed=$("#lt-edit"), sj=$("#lt-subj");
    ed.oninput=()=>{
      /* A prompt typed into is a paragraph of the letter now. */
      ed.querySelectorAll(".lt-prompt").forEach(p=>{ if(ltFlat(p.textContent)!==p.dataset.orig) p.classList.remove("lt-prompt") });
      ltChanged() };
    ed.addEventListener("click",()=>{
      const sel=document.getSelection(), n=sel.anchorNode, p=n&&(n.nodeType===1?n:n.parentElement).closest(".lt-prompt");
      if(p&&sel.isCollapsed){ const r=document.createRange(); r.selectNodeContents(p); sel.removeAllRanges(); sel.addRange(r) }
    });
    sj.oninput=()=>{ LT.meta.subject=sj.textContent.replace(/\s+/g," ").trim(); ltDirty() };
    sj.onkeydown=e=>{ if(e.key==="Enter"){ e.preventDefault(); ed.focus() } };
    [ed,sj].forEach(el=>el.onpaste=e=>{ e.preventDefault();
      document.execCommand("insertText",false,(e.clipboardData||window.clipboardData).getData("text/plain")) });
    ed.onkeydown=ltKeys;
    if(LT.rv){ ltPanel(); return ltRvPage() }
    ltHeadWire(h);
    const dl=$("#lt-dl"), toPlace=()=>{ const f=$("#lt-place"); if(f){ f.focus(); f.select() } };
    dl.onclick=toPlace; dl.onkeydown=e=>{ if(e.key==="Enter"){ e.preventDefault(); toPlace() } };
    ltPageMark();
    ltFindRun(true); ltToolState();
  }
  ltPanel();
}
/* The letterhead is the CV's, and editable here for this letter only: what
   is typed over it is kept in the letter's header, and the CV is left alone.
   The tag above it says which is which, and puts the CV's back. */
function ltHeadTag(h){
  const cv=esc((h.cv||t("the CV")).split("/").pop().replace(/\.ya?ml$/,""));
  const own=(h.overridden||[]).length;
  return '<span>'+(own?esc(t("Edited for this letter"))
      :esc(t("From {cv}",{cv}))+' <span class="dim">· '+esc(t("click a line to change it for this letter"))+'</span>')+'</span>'+
    (own?'<button type="button" data-lh="reset">'+esc(t("Use the CV’s"))+'</button>':'')+
    (h.cv?'<button type="button" data-lh="open">'+esc(t("Open the CV"))+'</button>':'');
}
function ltHeadRead(el){
  const v=el.innerText.replace(/\s+/g," ").trim();
  return el.dataset.k==="contact"?v.split(/\s*[•·|]\s*/).filter(Boolean):v;
}
function ltHeadWire(h){
  const from=h.from_cv||{};
  $$("#lt-paper .lhf").forEach(el=>{
    el.oninput=()=>{
      const k=el.dataset.k, v=ltHeadRead(el);
      const same=JSON.stringify(v)===JSON.stringify(k==="signature"?(LT.meta.letterhead&&LT.meta.letterhead.name)||from.name:from[k]);
      const over=Object.assign({},LT.meta.letterhead||{});
      if(same||(k!=="headline"&&(!v||!v.length))) delete over[k]; else over[k]=v;
      LT.meta.letterhead=Object.keys(over).length?over:null;
      h.overridden=Object.keys(over);
      $("#lt-lh .tag").innerHTML=ltHeadTag(h);
      ltDirty();
    };
    el.onkeydown=e=>{ if(e.key==="Enter"){ e.preventDefault(); el.blur() } };
    el.onpaste=e=>{ e.preventDefault();
      document.execCommand("insertText",false,(e.clipboardData||window.clipboardData).getData("text/plain").replace(/\s+/g," ")) };
    /* An emptied name or signature is the CV's again, shown as such. */
    el.onblur=()=>{ const k=el.dataset.k;
      if((k==="name"||k==="signature")&&!el.innerText.trim()) el.textContent=k==="name"?from.name:(h.name||from.name) };
  });
  $("#lt-lh").onclick=e=>{ const b=e.target.closest("[data-lh]"); if(!b) return;
    if(b.dataset.lh==="reset"){ LT.meta.letterhead=null; ltDirty(); ltSave().then(()=>ltPaint()) }
    else if(h.cv){
      (LT.dirty?ltSave():Promise.resolve()).then(()=>{ LT.path=null; openDoc(h.cv) }) }
  };
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
  ltFindRun(true);
}
/* A letter saves itself a moment after you stop typing, as a CV's preview
   renders itself: a letter left with unsaved words and then navigated away
   from lost them, while the status bar said "all saved". */
function ltDirty(){
  LT.dirty=true; LT.gen=(LT.gen||0)+1; $("#lt-save").disabled=false;
  clearTimeout(LT.auto); LT.auto=setTimeout(()=>{ if(LT.dirty) ltSave() },1200);
  if(S.view==="letter") paintStatus();
  const w=$("#lt-words"); if(w) w.textContent=ltWords(LT.body);
  const m=$("#lt-meterfill"); if(m) m.style.width=Math.min(100,ltWords(LT.body)/250*100)+"%";
  const sh=$("#lt-short"); if(sh) sh.hidden=!ltShort(ltWords(LT.body));
}
/* Short is worth saying once there is a letter to speak of, not while the
   page is still empty. */
const ltShort=n=>n>=20&&n<120;
/* Words of the letter, as the server counts them: every block's text, not
   its markup, and not a writing prompt. */
const ltWords=b=>{ const ps=ltPrompts(), texts=[];
  const walk=bl=>bl.forEach(x=>{
    if(x.t==="p"){ if(!ps.has(ltFlat(x.text))) texts.push(x.text) }
    else if(x.t==="h"||x.t==="code") texts.push(x.text);
    else if(x.t==="ul"||x.t==="ol"){ const w=nd=>nd.items.forEach(it=>{ texts.push(it.text); it.children.forEach(w) }); w(x) }
    else if(x.t==="quote") walk(x.blocks);
    else if(x.t==="table") texts.push(...x.head,...x.rows.flat());
  });
  walk(mdParse(b));
  return texts.reduce((n,tx)=>n+(tx.replace(/\[([^\]]+)\]\([^)]+\)/g,"$1").match(/[\p{L}\p{N}][\p{L}\p{N}'’_-]*/gu)||[]).length,0) };

function ltPanel(){
  if(LT.rv&&ltRv()) return ltRvPanel();
  const j=ltJob(), n=ltWords(LT.body), m=LT.meta, docs=(S.state&&S.state.documents)||[];
  const cvs=docs.filter(d=>d.group!=="Cover letters"&&!d.letter);
  const today=!m.date||m.date==="today";
  const lang=m.language||"en";
  const fits=LT.pages==null?(LT.rendering?"Checking the page…":"Not laid out yet.")
    :LT.pages===1?"Fits on one page.":"Runs to "+LT.pages+" pages. Letters read best on one.";
  const scaffold=ltPrompts().size>0;
  const say="In CV Studio, write the cover letter "+LT.path+(j?" for my "+j.company+" application, against its posting.":".");
  $("#lt-panel").innerHTML=
    '<div style="display:flex;flex-direction:column;gap:8px"><h4>Length</h4>'+
      '<span><span class="big" id="lt-words">'+n+'</span> <span class="muted2">of about 250 words</span></span>'+
      '<div class="lt-meter'+(n>400?" over":"")+'"><i id="lt-meterfill" style="width:'+Math.min(100,n/250*100)+'%"></i></div>'+
      '<span class="muted2">'+fits+'</span>'+
      '<span class="muted2" id="lt-short"'+(ltShort(n)?'':' hidden')+'>'+t("Short: half a page is what recruiters ask for most often.")+'</span></div>'+
    '<hr>'+
    '<div style="display:flex;flex-direction:column;gap:10px"><h4>This letter</h4><div class="fg">'+
      '<label>For</label><span>'+(j?'<b style="font-weight:600">'+esc(j.company)+'</b> · '+esc(j.title)
        :'<span class="muted2">No application</span>')+'</span>'+
      '<label for="lt-cv">Letterhead from</label><select id="lt-cv">'+cvs.map(d=>'<option value="'+esc(d.path)+'"'+
        (d.path===(m.looks_like||(LT.doc.head||{}).cv)?" selected":"")+'>'+esc(docTitle(d)+(d.translation_of||cvs.some(o=>o!==d&&docTitle(o)===docTitle(d))?" · "+langOf(d.lang||"en").native:""))+'</option>').join("")+'</select>'+
      '<label for="lt-lang">Language</label><select id="lt-lang">'+((S.state&&S.state.languages)||[]).map(l=>
        '<option value="'+l.code+'"'+(l.code===lang?" selected":"")+'>'+esc(l.native)+'</option>').join("")+'</select>'+
      '<label for="lt-place">Written from</label><input id="lt-place" value="'+esc(m.place||"")+'" placeholder="City">'+
      '<label for="lt-date">Date</label><span class="today"><label style="display:flex;gap:5px;align-items:center;color:var(--t800)">'+
        /* Sent, "today" means the day it went out, as the export prints it. */
        '<input type="checkbox" id="lt-today"'+(today?" checked":"")+'>'+(LT.doc.sent_on?esc(t("Sent date")):"Today")+'</label>'+
        '<input type="date" id="lt-date" value="'+(today?"":esc(String(m.date)))+'"'+(today?" hidden":"")+'></span>'+
      '<label for="lt-to">Addressed to</label><textarea id="lt-to" placeholder="Optional. Printed above the date.">'+
        esc(Array.isArray(m.to)?m.to.join("\n"):(m.to||""))+'</textarea>'+
    '</div></div>'+
    '<hr>'+
    (scaffold?sayBox("To have your AI client write it, ask it:",say)+'<hr>':'')+
    '<div class="muted2" style="font-size:12.5px">Click anywhere on the page and type: the letterhead, '+
      'the subject, the letter and the signature. The toolbar sets headings, bold, italic, lists, quotes, '+
      'tables and code, and Markdown works as you type. A letterhead changed here is for this letter only; '+
      'the CV keeps its own.</div>';
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
  clearTimeout(LT.auto);
  const body=LT.tab==="md"&&LT.mdText!=null?{path:LT.path,text:LT.mdText}
    :{path:LT.path,meta:LT.meta,body:LT.body};
  const gen=LT.gen, path=LT.path;
  LT.saving=true; if(S.view==="letter") paintStatus();
  try{
    const r=await post("/api/save",body);
    if(LT.path!==path) return;
    LT.doc=r; LT.savedAt=Date.now();
    /* Typed while the save was on its way: keep those words, and go again. */
    if(LT.gen!==gen){ LT.auto=setTimeout(()=>{ if(LT.dirty) ltSave() },600); return }
    LT.meta=Object.assign({},r.meta); LT.body=r.body; LT.mdText=null; LT.dirty=false;
    $("#lt-save").disabled=true;
    ltRender();
  }catch(e){ toast(e.message,true) }
  finally{ LT.saving=false; if(S.view==="letter") paintStatus() }
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
  LT.tab=b.dataset.lt; LT.rv=false; ltPaint();
});
document.addEventListener("keydown",e=>{
  if(S.view!=="letter") return;
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="s"){ e.preventDefault(); ltSave().then(()=>toast("Saved")) }
  else if((e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey&&e.key.toLowerCase()==="f"&&LT.tab==="write"){
    e.preventDefault(); ltFindOpen() }
  else if(e.key==="Escape"&&LTF.open&&$("#sheet").hidden){ e.preventDefault(); ltFindClose(true) }
});

/* The toolbar above the page: undo and redo, the four things a letter prints,
   and find and replace. Every edit goes through the browser's own editing
   commands, so the undo history is the browser's and holds all of them. */
const LT_MOD=/Mac|iPhone|iPad/.test(navigator.platform)?"⌘":"Ctrl+";
$$("#lt-tools [data-c]").forEach(b=>{
  b.title=t(b.getAttribute("aria-label"))+(b.dataset.k?" ("+LT_MOD+b.dataset.k.replace("Shift+",LT_MOD==="⌘"?"⇧":"Shift+")+")":"");
});
/* Where a command lands: the selection if it is in the body, else the end of
   the body, so a button pressed with the caret in the subject or nowhere
   still does something you can see. */
function ltFocus(){
  const ed=$("#lt-edit"); if(!ed) return null;
  const sel=document.getSelection();
  if(sel.rangeCount&&ed.contains(sel.anchorNode)&&ed.contains(sel.focusNode)){ ed.focus(); return ed }
  ed.focus();
  const r=document.createRange(); r.selectNodeContents(ed); r.collapse(false);
  sel.removeAllRanges(); sel.addRange(r);
  return ed;
}
const ltLinkAt=()=>{ const s=document.getSelection(), ed=$("#lt-edit");
  const n=s.rangeCount&&s.anchorNode, a=n&&(n.nodeType===1?n:n.parentElement).closest("a");
  return a&&ed&&ed.contains(a)?a:null };
async function ltLink(){
  if(!ltFocus()) return;
  const sel=document.getSelection(), a=ltLinkAt();
  const saved=sel.getRangeAt(0).cloneRange();
  const url=await askText({title:a?t("Edit the link"):t("Add a link"),body:a?t("Empty it to remove the link."):"",
    ok:a?t("Save"):t("Add link"),input:{value:a?a.getAttribute("href"):"https://",placeholder:"https://"}});
  if(url==null) return;
  $("#lt-edit").focus(); sel.removeAllRanges(); sel.addRange(saved);
  const u=url.trim();
  if(a){
    const r=document.createRange(); r.selectNodeContents(a); sel.removeAllRanges(); sel.addRange(r);
    document.execCommand("unlink");
    if(u) document.execCommand("createLink",false,u);
  }else if(u&&u!=="https://"){
    /* Nothing selected, the address is the text. */
    if(sel.isCollapsed) document.execCommand("insertHTML",false,'<a href="'+esc(u)+'">'+esc(u)+'</a>');
    else document.execCommand("createLink",false,u);
  }
  ltChanged();
}
function ltCommand(c){
  if(c==="find") return LTF.open?ltFindClose():ltFindOpen();
  if(c==="link") return ltLink();
  if(!ltFocus()) return;
  if(c==="code") ltCode();
  else if(c==="table") ltTable();
  else document.execCommand(c);
  ltChanged(); ltToolState();
}
/* Inline code: the selection in a code span, or out of one. */
function ltCode(){
  const sel=document.getSelection(), n=sel.anchorNode, c=n&&(n.nodeType===1?n:n.parentElement).closest("code");
  if(c&&$("#lt-edit").contains(c)){ const r=document.createRange(); r.selectNode(c); sel.removeAllRanges(); sel.addRange(r);
    document.execCommand("insertText",false,c.textContent); return }
  const tx=sel.toString()||"code";
  document.execCommand("insertHTML",false,"<code>"+esc(tx)+"</code>\u200b");
}
/* A table to start from: a header row and one row; Tab walks the cells and
   adds a row after the last. */
function ltTable(){
  /* After the block the caret is in, as a block of its own, never inside a
     paragraph, a list or a quote; then straight into its first cell. */
  const ed=$("#lt-edit"), sel=document.getSelection(), n=sel.anchorNode;
  let top=n&&(n.nodeType===1?n:n.parentElement); while(top&&top.parentElement!==ed) top=top.parentElement;
  const tb=document.createElement("table");
  tb.innerHTML='<thead><tr><th>'+esc(t("Heading"))+'</th><th>'+esc(t("Heading"))+'</th></tr></thead><tbody><tr><td><br></td><td><br></td></tr></tbody>';
  const after=document.createElement("p"); after.innerHTML="<br>";
  if(top&&top.textContent.trim()===""&&top.tagName==="P") top.replaceWith(tb); else if(top) top.after(tb); else ed.append(tb);
  tb.after(after);
  const r=document.createRange(); r.selectNodeContents(tb.querySelector("th")); sel.removeAllRanges(); sel.addRange(r);
}
/* Block styles from the menu: paragraph, headings, quote, code block. The
   selection is kept across the menu taking focus. */
const ltStyle=$("#lt-style");
ltStyle.addEventListener("mousedown",()=>{ const s=document.getSelection(), ed=$("#lt-edit");
  LT.keep=s.rangeCount&&ed&&ed.contains(s.anchorNode)?s.getRangeAt(0).cloneRange():null });
ltStyle.onchange=()=>{
  const ed=$("#lt-edit"); if(!ed) return;
  ed.focus();
  if(LT.keep){ const s=document.getSelection(); s.removeAllRanges(); s.addRange(LT.keep) } else ltFocus();
  document.execCommand("formatBlock",false,ltStyle.value);
  ltChanged(); ltToolState();
};
$("#lt-tools").onmousedown=e=>{ if(e.target.closest("[data-c]")) e.preventDefault() };
$("#lt-tools").onclick=e=>{ const b=e.target.closest("[data-c]"); if(b) ltCommand(b.dataset.c) };
function ltToolState(){
  if(S.view!=="letter"||LT.tab!=="write") return;
  const sel=document.getSelection(), ed=$("#lt-edit");
  const inside=!!(ed&&sel.rangeCount&&ed.contains(sel.anchorNode));
  const q=c=>{ try{ return inside&&document.queryCommandState(c) }catch(e){ return false } };
  const set=(c,v)=>{ const b=$('#lt-tools [data-c="'+c+'"]'); if(b) b.setAttribute("aria-pressed",String(!!v)) };
  set("bold",q("bold")); set("italic",q("italic")); set("strikeThrough",q("strikeThrough"));
  set("insertUnorderedList",q("insertUnorderedList")); set("insertOrderedList",q("insertOrderedList"));
  set("link",inside&&ltLinkAt());
  const n=inside&&sel.anchorNode, el=n&&(n.nodeType===1?n:n.parentElement);
  set("code",!!(el&&el.closest("code")));
  const blk=el&&el.closest("h1,h2,h3,h4,h5,h6,blockquote,pre,p,li,td,th,div");
  const tag=blk&&blk!==ed?blk.tagName.toLowerCase():"p";
  ltStyle.value=/^h[4-6]$/.test(tag)?"h3":["h1","h2","h3","blockquote","pre"].includes(tag)?tag
    :(el&&el.closest("blockquote")?"blockquote":"p");
}
document.addEventListener("selectionchange",ltToolState);
/* Shortcuts the browser does not give a contenteditable by itself. Bold,
   italic, undo and redo it does. */
function ltKeys(e){
  if(ltAutoformat(e)||ltTabKey(e)) return;
  if((e.metaKey||e.ctrlKey)&&e.altKey&&/^Digit[0-3]$/.test(e.code)){
    e.preventDefault(); ltFocus(); document.execCommand("formatBlock",false,e.code==="Digit0"?"p":"h"+e.code.slice(-1));
    ltChanged(); ltToolState(); return }
  if(!(e.metaKey||e.ctrlKey)||e.altKey) return;
  const k=e.key.toLowerCase();
  if(e.shiftKey&&k==="x"){ e.preventDefault(); return ltCommand("strikeThrough") }
  if(e.shiftKey&&(e.code==="Digit7"||k==="&")){ e.preventDefault(); return ltCommand("insertOrderedList") }
  if(!e.shiftKey&&k==="e"){ e.preventDefault(); return ltCommand("code") }
  if(k==="k"&&!e.shiftKey){ e.preventDefault(); ltLink() }
  else if(e.shiftKey&&(e.code==="Digit8"||k==="*")){ e.preventDefault(); ltCommand("insertUnorderedList") }
  else if(k==="y"&&!e.shiftKey&&LT_MOD!=="⌘"){ e.preventDefault(); ltCommand("redo") }
}

/* Markdown as you type: at the start of a line, "# " to "### " make a
   heading, "- " or "* " a bullet list, "1. " a numbered one, "> " a quote,
   and "```" a code block; "---" and Enter draws a rule. */
function ltAutoformat(e){
  if(e.metaKey||e.ctrlKey||e.altKey||(e.key!==" "&&e.key!=="Enter")) return false;
  const sel=document.getSelection(); if(!sel.rangeCount||!sel.isCollapsed) return false;
  const ed=$("#lt-edit"), n=sel.anchorNode, el=n&&(n.nodeType===1?n:n.parentElement);
  const blk=el&&el.closest("p,div,li,h1,h2,h3,blockquote"); if(!blk||!ed.contains(blk)||blk===ed&&false) return false;
  if(blk.closest("pre,td,th")) return false;
  const pre=document.createRange(); pre.setStart(blk,0); pre.setEnd(sel.anchorNode,sel.anchorOffset);
  const typed=pre.toString();
  let act=null;
  if(e.key===" "){
    if(/^#{1,3}$/.test(typed)) act=()=>document.execCommand("formatBlock",false,"h"+typed.length);
    else if(/^[-*+]$/.test(typed)&&blk.tagName!=="LI") act=()=>document.execCommand("insertUnorderedList");
    else if(/^1[.)]$/.test(typed)&&blk.tagName!=="LI") act=()=>document.execCommand("insertOrderedList");
    else if(typed===">") act=()=>document.execCommand("formatBlock",false,"blockquote");
  }else if(typed==="```") act=()=>document.execCommand("formatBlock",false,"pre");
  /* Enter on an empty line of a quote leaves the quote. */
  else if(!typed.trim()&&blk.closest("blockquote")&&!blk.textContent.trim()){
    e.preventDefault(); document.execCommand("formatBlock",false,"p"); ltChanged(); ltToolState(); return true }
  else if(/^(---|\*\*\*|___)$/.test(typed)) act=()=>document.execCommand("insertHorizontalRule");
  if(!act) return false;
  e.preventDefault();
  sel.removeAllRanges(); sel.addRange(pre); document.execCommand("delete");
  act(); ltChanged(); ltToolState();
  return true;
}
/* Tab in a list nests the item (Shift+Tab lifts it); in a table it goes to
   the next cell, and past the last one adds a row. */
function ltTabKey(e){
  if(e.key!=="Tab"||e.metaKey||e.ctrlKey||e.altKey) return false;
  const sel=document.getSelection(), n=sel.anchorNode, el=n&&(n.nodeType===1?n:n.parentElement);
  if(!el||!$("#lt-edit").contains(el)) return false;
  const cell=el.closest("td,th");
  if(cell){
    e.preventDefault();
    const cells=[...cell.closest("table").querySelectorAll("th,td")], i=cells.indexOf(cell);
    let next=cells[i+(e.shiftKey?-1:1)];
    if(!next&&!e.shiftKey){ const tr=cell.closest("tr"), row=document.createElement("tr");
      row.innerHTML=[...tr.children].map(()=>"<td><br></td>").join("");
      (cell.closest("tbody")||cell.closest("table")).append(row); next=row.firstElementChild; ltChanged() }
    if(next){ const r=document.createRange(); r.selectNodeContents(next); sel.removeAllRanges(); sel.addRange(r) }
    return true;
  }
  if(el.closest("li")){ e.preventDefault(); document.execCommand(e.shiftKey?"outdent":"indent"); ltChanged(); return true }
  return false;
}

/* Find and replace, in the body. Matches are painted with the CSS highlight
   API where there is one, which leaves the letter's own markup alone; without
   it the count and the scrolling still work. Replacing goes through
   insertText, so each replacement is one undo step. */
const LTF={open:false, hits:[], i:-1};
const LT_HL=typeof CSS!=="undefined"&&CSS.highlights&&typeof Highlight!=="undefined";
function ltFindOpen(){
  if(LT.rv) return;
  LTF.open=true; $("#lt-find").hidden=false;
  $('#lt-tools [data-c="find"]').setAttribute("aria-pressed","true");
  const sel=document.getSelection(), ed=$("#lt-edit"), q=$("#lt-q");
  const picked=sel.rangeCount&&ed&&ed.contains(sel.anchorNode)?sel.toString().trim():"";
  if(picked&&picked.indexOf("\n")<0) q.value=picked;
  q.focus(); q.select();
  ltFindRun();
}
function ltFindClose(back){
  LTF.open=false; LTF.hits=[]; LTF.i=-1; $("#lt-find").hidden=true;
  $('#lt-tools [data-c="find"]').setAttribute("aria-pressed","false");
  if(LT_HL){ CSS.highlights.delete("lt-find"); CSS.highlights.delete("lt-find-cur") }
  if(back){ const ed=$("#lt-edit"); if(ed) ed.focus() }
}
/* Every match as a range. The text is read across nodes, so a phrase with a
   bold word in the middle is still found. */
function ltFindRun(keep){
  if(!LTF.open) return;
  const q=$("#lt-q").value;
  LTF.hits=[];
  /* Everything you can edit on the page, in page order: the letterhead, the
     subject, the letter and the signature. A match never spans two of them. */
  const hosts=$$("#lt-paper [contenteditable=true]");
  if(q) for(const ed of hosts){
    const nodes=[], w=document.createTreeWalker(ed,NodeFilter.SHOW_TEXT);
    let x, text="";
    while((x=w.nextNode())){ nodes.push([x,text.length]); text+=x.nodeValue }
    if(!nodes.length) continue;
    const hay=text.toLocaleLowerCase(), needle=q.toLocaleLowerCase();
    const at=off=>{ let k=nodes.length-1; while(k>0&&nodes[k][1]>off) k--; return [nodes[k][0],off-nodes[k][1]] };
    for(let p=hay.indexOf(needle); p>=0; p=hay.indexOf(needle,p+needle.length)){
      const r=document.createRange(), s=at(p), e=at(p+needle.length-1);
      r.setStart(s[0],s[1]); r.setEnd(e[0],e[1]+1);
      LTF.hits.push(r);
    }
  }
  LTF.i=LTF.hits.length?Math.min(Math.max(keep?LTF.i:0,0),LTF.hits.length-1):-1;
  ltFindPaint();
}
function ltFindPaint(scroll){
  const n=LTF.hits.length, q=$("#lt-q").value;
  $("#lt-qn").textContent=!q?"":n?t("{i} of {n}",{i:LTF.i+1,n}):t("No matches");
  $$('#lt-find [data-f="one"],#lt-find [data-f="all"],#lt-find [data-f="prev"],#lt-find [data-f="next"]')
    .forEach(b=>b.disabled=!n);
  if(LT_HL){
    CSS.highlights.set("lt-find",new Highlight(...LTF.hits));
    if(n) CSS.highlights.set("lt-find-cur",new Highlight(LTF.hits[LTF.i]));
    else CSS.highlights.delete("lt-find-cur");
  }
  if(scroll&&n){
    const st=$("#lt-stage"), r=LTF.hits[LTF.i].getBoundingClientRect(), b=st.getBoundingClientRect();
    if(r.top<b.top+40||r.bottom>b.bottom-40) st.scrollTop+=r.top-b.top-b.height/3;
  }
}
function ltFindStep(d){
  if(!LTF.hits.length) return;
  LTF.i=(LTF.i+d+LTF.hits.length)%LTF.hits.length;
  ltFindPaint(true);
}
function ltReplace(all){
  if(!LTF.hits.length) return;
  const ed=$("#lt-edit"), sel=document.getSelection(), by=$("#lt-r").value;
  const from=document.activeElement;
  /* Last first, so the ranges not yet replaced still point at their text.
     Each in its own field, which hears the change as if it were typed. */
  const which=all?LTF.hits.slice().reverse():[LTF.hits[LTF.i]];
  which.forEach(r=>{
    const n=r.startContainer, host=(n.nodeType===1?n:n.parentElement).closest("[contenteditable=true]")||ed;
    host.focus(); sel.removeAllRanges(); sel.addRange(r);
    if(by) document.execCommand("insertText",false,by); else document.execCommand("delete") });
  const n=which.length;
  if(all) LTF.i=0;
  ltChanged();
  ltFindPaint(true);
  if(from&&from!==ed) from.focus();
  if(all) toast(t("Replaced {n}",{n}));
}
$("#lt-q").oninput=()=>{ ltFindRun(); ltFindPaint(true) };
$("#lt-q").onkeydown=e=>{
  if(e.key==="Enter"){ e.preventDefault(); ltFindStep(e.shiftKey?-1:1) }
  else if(e.key==="Escape"){ e.preventDefault(); e.stopPropagation(); ltFindClose(true) }
};
$("#lt-r").onkeydown=e=>{
  if(e.key==="Enter"){ e.preventDefault(); ltReplace(e.metaKey||e.ctrlKey) }
  else if(e.key==="Escape"){ e.preventDefault(); e.stopPropagation(); ltFindClose(true) }
};
$("#lt-find").onclick=e=>{ const b=e.target.closest("[data-f]"); if(!b) return;
  const f=b.dataset.f;
  if(f==="close") ltFindClose(true);
  else if(f==="prev"||f==="next") ltFindStep(f==="next"?1:-1);
  else ltReplace(f==="all");
};

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
    download("/api/letter/export?path="+encodeURIComponent(LT.path)+"&format="+b.dataset.f+tok()) };
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
