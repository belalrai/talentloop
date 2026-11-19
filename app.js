const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const api=async(p,o)=>{const r=await fetch("/api"+p,o);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.detail||r.statusText||"Request failed (HTTP "+r.status+")"+(r.status==413?": file too large for Vercel (max 4 MB)":r.status==504?": timed out":""));return j};
let cands=[],tpls=[],channel="Email";
const titles={overview:"Overview",parse:"Parse CVs",sheet:"Spreadsheet",outreach:"Outreach"};
function go(v){$$(".view").forEach(e=>e.classList.toggle("on",e.id===v));$$(".nav").forEach(b=>b.setAttribute("aria-current",b.dataset.v===v));$("#title").textContent=titles[v];}
$$(".nav").forEach(b=>b.onclick=()=>go(b.dataset.v));
const fmt=n=>Number(n).toLocaleString();
async function load(){
 const [s,c,t]=await Promise.all([api("/stats"),api("/candidates"),api("/templates")]);
 $("#sParsed").textContent=fmt(s.parsed);$("#sList").textContent=fmt(s.candidates);$("#sTpl").textContent=fmt(s.templates);
 $("#sMail").textContent=fmt(s.email);$("#sWa").textContent=fmt(s.whatsapp);$("#sFail").textContent=fmt(s.failed);
 const max=Math.max(1,...s.roles.map(r=>r.n));
 $("#roles").innerHTML=s.roles.map(r=>`<span><i style="background:#1ea3a0;width:${8+r.n/max*90}px"></i>${esc(r.role)} (${r.n})</span>`).join("")||"No candidates yet.";
 cands=c;
 $("#recent tbody").innerHTML=c.slice(0,8).map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.role)}</td><td>${esc(r.location)}</td><td>${esc((r.source||"").split(":")[0])}</td></tr>`).join("");
 const cur=$("#tpl").value;tpls=t;
 $("#tpl").innerHTML=t.map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join("");
 if(cur)$("#tpl").value=cur;
 if(!$("#body").value)pick();
}
function pick(){const t=tpls.find(x=>x.id==$("#tpl").value)||tpls[0];if(!t)return;$("#subj").value=t.subject;$("#body").value=t.body;render();}
function fill(s,c){const f=(c.name||"there").split(" ")[0];return s.replace(/{first_name}/g,f).replace(/{job_title}/g,$("#job").value).replace(/{company}/g,$("#co").value);}
function render(){const c=cands[0]||{name:"Maria Papadaki"};$("#prev").textContent=(channel==="Email"?"Subject: "+fill($("#subj").value,c)+"\n\n":"")+fill($("#body").value,c);}
$("#tpl").onchange=pick;["#subj","#body","#job","#co"].forEach(s=>$(s).oninput=render);
$$(".seg button").forEach(b=>b.onclick=()=>{channel=b.dataset.ch;$$(".seg button").forEach(x=>x.setAttribute("aria-pressed",x===b));render();});
async function upload(path,file){const fd=new FormData();fd.append("file",file);return api(path,{method:"POST",body:fd});}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function parseFiles(files){
 files=[...files].slice(0,25);if(!files.length)return;
 $("#parseWrap").style.display="";
 $("#parseRows").innerHTML=files.map((f,i)=>`<tr id="pr${i}"><td>${esc(f.name)}</td><td>Waiting</td><td></td><td></td><td></td><td></td></tr>`).join("");
 let ok=0;
 for(let i=0;i<files.length;i++){
  const c=$("#pr"+i).children;
  for(let a=0;a<4;a++){
   c[1].textContent=a?"Retrying…":"Parsing…";
   try{const p=await upload("/parse",files[i]);c[1].textContent="Done ✓";
    [p.name,p.email,p.phone,p.current_role].forEach((v,k)=>c[k+2].textContent=v||"");ok++;break;
   }catch(err){
    if(/429|rate|quota|exhausted/i.test(err.message)&&a<3){c[1].textContent="Rate limit, waiting 30s…";await sleep(30000);continue;}
    c[1].textContent="Failed: "+err.message;c[1].style.whiteSpace="normal";break;}
  }
 }
 $("#parseSum").textContent=ok+" of "+files.length+" CVs parsed and saved.";load();
}
$("#cv").onchange=e=>{const fl=[...e.target.files];e.target.value="";parseFiles(fl);};
const dz=document.querySelector("label[for=cv]");dz.ondragover=e=>e.preventDefault();dz.ondrop=e=>{e.preventDefault();parseFiles(e.dataTransfer.files);};
$("#csv").onchange=async e=>{const f=e.target.files[0];if(!f)return;
 try{const r=await upload("/sheet",f),m=r.matched;
  $("#sheetInfo").textContent=r.count+" candidates imported. Columns matched: "+Object.keys(m).map(k=>k+" "+(m[k]?"✓":"✗")).join(", ");
  $("#sheetTbl").innerHTML="<thead><tr><th>Name</th><th>Email</th><th>Phone</th></tr></thead><tbody>"+r.candidates.slice(0,50).map(c=>`<tr><td>${esc(c.name)}</td><td>${esc(c.email)}</td><td>${esc(c.phone)}</td></tr>`).join("")+"</tbody>";
  $("#toOutreach").disabled=!r.count;load();
 }catch(err){$("#sheetInfo").textContent=err.message;}
 e.target.value="";};
$("#toOutreach").onclick=()=>{go("outreach");render();};
$("#saveTpl").onclick=async()=>{const name=prompt("Template name");if(!name)return;await api("/templates",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,subject:$("#subj").value,body:$("#body").value})});await load();$("#sendMsg").textContent="Template saved.";};
$("#send").onclick=async()=>{
 if(!confirm("Send this "+channel+" message to every candidate with a "+(channel==="Email"?"email":"phone number")+" who hasn’t received this job yet? Make sure they have agreed to be contacted."))return;
 $("#send").disabled=true;
 try{const r=await api("/send",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({channel,template_id:+$("#tpl").value||null,subject:$("#subj").value,body:$("#body").value,job:$("#job").value,company:$("#co").value})});
  $("#sendMsg").textContent="Sending "+r.queued+" "+channel+" messages. Overview counters update as they finish.";setTimeout(load,4000);
 }catch(err){$("#sendMsg").textContent=err.message;}
 $("#send").disabled=false;};
load().catch(e=>{$("#sendMsg").textContent=e.message});
