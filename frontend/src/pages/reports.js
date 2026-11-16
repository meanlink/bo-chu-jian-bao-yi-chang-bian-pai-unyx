import {jobs,reports} from '../api/entities.js';
import {download} from '../api/client.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const STATUS_LABELS={pending:'待发送',claimed:'发送中',archived:'已归档',missed:'错过',superseded:'已作废'};
const WEEKDAYS=[[1,'一'],[2,'二'],[3,'三'],[4,'四'],[5,'五'],[6,'六'],[7,'日']];
const clock=v=>new Date(v*1000).toLocaleString('zh-CN',{hour12:false});
const statusTag=s=>'<span class="status '+esc(s)+'">'+esc(STATUS_LABELS[s]||s)+'</span>';

function slotLine(row){
 // 场次行：接收地当地时刻 + 时区偏移，悬浮显示 UTC 时间
 const tz=row.timezone||row.scheduled_timezone||'';
 const local=row.local_time||row.scheduled_local||'';
 return '<strong>'+esc(local||clock(row.scheduled_for))+'</strong><small>'+esc(tz)+
  (row.offset_text?' · '+esc(row.offset_text):'')+' · UTC '+esc(clock(row.scheduled_for))+'</small>';
}

function calendarForm(m,approved,plan){
 const zones=m.timezones||[];
 const weekdays=(plan&&plan.weekdays)||[1,2,3,4,5];
 const time=(plan&&plan.local_time)||'08:30';
 const tz=(plan&&plan.timezone)||'Asia/Shanghai';
 return '<div class="calendar-fields" id="calendar-fields"><div class="parameter-grid">'+
  '<label>接收地时区<select name="timezone">'+zones.map(z=>'<option value="'+esc(z.value)+'" '+(z.value===tz?'selected':'')+'>'+esc(z.label)+'（'+esc(z.value)+'）</option>').join('')+'</select></label>'+
  '<label>当地钟点<input name="local_time" type="time" required value="'+esc(time)+'"></label></div>'+
  '<fieldset class="weekday-picker"><legend>星期（按接收地当地星期）</legend>'+WEEKDAYS.map(([v,l])=>
   '<label><input type="checkbox" name="weekdays" value="'+v+'" '+(weekdays.includes(v)?'checked':'')+'><span>周'+l+'</span></label>').join('')+
  '</fieldset><p class="hint">按接收地时区逐个当地日期换算：夏令时跳过的钟点顺延到当天首个有效时刻；重复钟点只发较早一次。</p></div>';
}

function intervalFields(plan){
 return intervalFormValues(plan)+'<p class="hint">固定秒数周期沿用原有行为，适合验收；跨时区值班请使用日历计划。</p>';
}

function planRow(r){
 const calendar=r.schedule_type==='calendar';
 return '<article><div><strong>'+esc(r.name)+'</strong><p>'+esc(r.destination)+' · '+esc(r.rules)+'</p><small>下次 '+
  (calendar?esc(new Date(r.next_run*1000).toLocaleString('zh-CN',{hour12:false})):esc(date(r.next_run)))+
  ' · 版本 '+r.schedule_revision+(r.enabled?'':' · 已暂停')+'</small></div><div class="row-actions"><button class="button small" data-open="'+r.id+'">场次与依据</button><button class="button small" data-reschedule="'+r.id+'" '+(session.user.role!=='admin'?'disabled':'')+'>改期</button><button class="button small" data-toggle="'+r.id+'" '+(session.user.role!=='admin'?'disabled':'')+'>'+(r.enabled?'暂停':'启用')+'</button></div></article>';
}

function reschedulePanel(plan){
 const calendar=plan.schedule_type==='calendar';
 return '<section class="panel plan-detail" data-reschedule-panel="'+plan.id+'"><div class="section-title"><div><h2>改期 · '+esc(plan.name)+'</h2><span>当前 '+esc(plan.rules)+' · 版本 '+plan.schedule_revision+'</span></div><button class="text-button" data-close-reschedule>收起 ✕</button></div>'+
  '<form id="reschedule-form" data-plan="'+plan.id+'">'+
  '<input type="hidden" name="job_id" value="'+plan.job_id+'">'+
  '<div class="parameter-grid"><label>简报名称<input name="name" required minlength="2" value="'+esc(plan.name)+'"></label><label>业务接收组<input name="destination" required value="'+esc(plan.destination)+'"></label></div>'+
  '<label>排程方式<select name="schedule_type" id="reschedule-type"><option value="calendar" '+(calendar?'selected':'')+'>日历（接收地时区 · 星期 · 钟点）</option><option value="interval" '+(calendar?'':'selected')+'>固定秒数周期（原有）</option></select></label>'+
  '<div id="reschedule-fields">'+(calendar?calendarForm(workspace.meta,[],plan):intervalFormValues(plan))+'</div>'+
  '<p class="hint">改期成功后，旧规则下尚未发布的场次立即作废；已归档记录保留原定当地时刻、时区和计划版本。</p>'+
  '<button type="submit" class="primary">确认改期</button></form></section>';
}

function intervalFormValues(plan){
 const seconds=(plan&&plan.interval_seconds)||86400;
 return '<div class="parameter-grid" id="interval-fields"><label>分发周期<select name="interval_seconds">'+[[60,'每分钟（验收）'],[3600,'每小时'],[86400,'每日'],[604800,'每周']].map(([v,l])=>'<option value="'+v+'" '+(v===seconds?'selected':'')+'>'+l+'</option>').join('')+'</select></label><label>首次执行<select name="delay_seconds"><option value="0">立即</option><option value="60">1 分钟后</option><option value="3600">1 小时后</option></select></label></div>';
}

function detailPanel(detail){
 const upcoming=(detail.upcoming||[]).map(s=>'<li>'+slotLine({scheduled_for:s.scheduled_for,timezone:s.timezone,local_time:s.local,offset_text:s.offset_text})+(s.note?'<em>'+esc(s.note)+'</em>':'')+'</li>').join('');
 const rows=(detail.occurrences||[]).map(o=>'<tr><td>'+esc(o.local_time)+'</td><td>'+esc(o.timezone)+'<small> '+esc(o.offset_text||'')+'</small></td><td>'+statusTag(o.status)+'</td><td class="note-cell">'+esc(o.note||'—')+'</td><td>'+(o.delivery_id?'#'+o.delivery_id:'—')+'</td></tr>').join('');
 const history=(detail.history||[]).map(h=>{
  const outcome=h.outcome||{},catchUp=outcome.catch_up;
  return '<li><div>'+slotLine(h)+' '+statusTag(h.status)+'</div><p>'+esc(h.reason||outcome.reason||'—')+'</p>'+
   (catchUp?'<small>补发依据：'+esc(catchUp.local_time)+'（'+esc(catchUp.timezone)+'）· '+esc(STATUS_LABELS[catchUp.status]||catchUp.status)+(catchUp.delivery_id?' · 归档 #'+catchUp.delivery_id:'')+'</small>':'')+
   (outcome.id?'<small>调度记录 #'+outcome.id+' · '+esc(outcome.kind)+'</small>':'')+'</li>';
 }).join('');
 return '<section class="panel plan-detail" data-detail="'+detail.id+'"><div class="section-title"><div><h2>'+esc(detail.name)+'</h2><span>'+esc(detail.rules)+' · 版本 '+detail.schedule_revision+'</span></div><button class="text-button" data-close-detail>收起 ✕</button></div>'+
  '<div class="two-columns"><div><h3>后续应发场次</h3><ol class="slot-list">'+upcoming+'</ol>'+
  '<h3>补发与错过依据</h3>'+(history?'<ul class="reason-list">'+history+'</ul>':'<p class="hint">目前没有错过或作废的场次。停机跨过多个应发时点时，只补最近一场，其余会在此列出原因。</p>')+'</div>'+
  '<div><h3>场次流转</h3><div class="table-wrap"><table><thead><tr><th>当地时刻</th><th>时区</th><th>状态</th><th>说明</th><th>归档</th></tr></thead><tbody>'+rows+'</tbody></table></div></div></div></section>';
}

export async function reportPage(){
 const [list,deliveries,allJobs]=await Promise.all([reports.list(),reports.deliveries(),jobs.list()]);workspace.reports=list;
 const approved=allJobs.filter(j=>j.approved&&j.status==='completed'),m=workspace.meta;
 return heading('05 / DISTRIBUTION','定时分发与归档','按接收地时区、星期和当地钟点安排日历计划；每场从待发送走到已归档，错过与补发都有依据可查。','<button class="button" id="refresh-reports">刷新收件箱 ↻</button>')+
 '<div class="report-grid"><section class="panel"><div class="section-title"><h2>创建分发计划</h2></div><form id="report-form">'+
  '<label>简报名称<input name="name" required minlength="2" value="值班组当地钟点简报"></label>'+
  '<label>已批准分析<select name="job_id" required>'+approved.map(j=>'<option value="'+j.id+'">'+esc(j.name)+'</option>').join('')+'</select></label>'+
  '<label>业务接收组<input name="destination" value="异地值班组 · 本地收件箱" required></label>'+
  '<label>排程方式<select name="schedule_type" id="schedule-type"><option value="calendar">日历（接收地时区 · 星期 · 钟点）</option><option value="interval">固定秒数周期（原有）</option></select></label>'+
  '<div id="schedule-fields">'+calendarForm(m,approved,null)+'</div>'+
  '<div class="preview-box" id="calendar-preview"><div class="section-title"><h3>后续场次预览</h3><span class="hint">按当前规则计算，不落库</span></div><ol class="slot-list"><li class="hint">调整时区、星期或钟点后自动预览。</li></ol></div>'+
  '<button type="submit" class="primary" '+(session.user.role!=='admin'||!approved.length?'disabled':'')+'>保存分发计划</button></form>'+
  '<p class="hint">由后台 worker 按到期时间生成文件。仅负责人可创建、改期或暂停；接收组为本地标签。</p></section>'+
 '<section><div class="section-title"><h2>运行中的计划</h2><span>'+list.filter(r=>r.enabled).length+' 条已启用 / '+list.length+' 条</span></div>'+
  '<div class="schedule-list">'+list.map(planRow).join('')+(!list.length?'<p class="empty">尚无分发计划。</p>':'')+'</div>'+
  '<div id="plan-detail"></div>'+
  '<section class="panel inbox"><div class="section-title"><h2>本地收件箱</h2><span>'+deliveries.length+' 份归档</span></div>'+
   deliveries.map(d=>'<div class="delivery"><div><strong>'+esc(d.filename)+'</strong><p>'+esc(d.destination)+' · '+date(d.created_at)+'</p>'+
    (d.scheduled_for?'<small>原定场次 '+esc(d.scheduled_local||'')+' · '+esc(d.scheduled_timezone||'')+(d.offset_text?' · '+esc(d.offset_text):'')+' · 计划版本 '+esc(d.schedule_revision||1)+'</small><br>':'')+
    '<small>SHA256 '+esc(d.checksum.slice(0,24))+'…</small></div><div><button class="text-button" data-download="'+d.id+'" data-format="csv">CSV ↓</button><button class="text-button" data-download="'+d.id+'" data-format="json">JSON ↓</button></div></div>').join('')+
   (!deliveries.length?'<p class="empty">计划执行后，文件会出现在此处。等待片刻后刷新。</p>':'')+'</section></section></div>';
}

function formPayload(form,extra={}){
 const f=new FormData(form),type=f.get('schedule_type');
 const base={name:f.get('name'),job_id:Number(f.get('job_id')),destination:f.get('destination'),schedule_type:type,...extra};
 if(type==='calendar')return {...base,timezone:f.get('timezone'),local_time:f.get('local_time'),weekdays:f.getAll('weekdays').map(Number),delay_seconds:0};
 return {...base,interval_seconds:Number(f.get('interval_seconds')),delay_seconds:Number(f.get('delay_seconds'))};
}

async function renderPreview(form){
 const box=document.querySelector('#calendar-preview');if(!box)return;
 if(form.querySelector('[name=schedule_type]').value!=='calendar'){box.hidden=true;return;}
 box.hidden=false;
 const payload=formPayload(form);
 if(!payload.weekdays.length||!payload.local_time){box.querySelector('.slot-list').innerHTML='<li class="hint">请选择星期并填写当地钟点。</li>';return;}
 try{
  const p=await reports.preview({timezone:payload.timezone,weekdays:payload.weekdays,local_time:payload.local_time,count:6});
  box.querySelector('.slot-list').innerHTML=p.slots.map(s=>'<li><strong>'+esc(s.local)+'</strong><small>'+esc(p.timezone)+' · '+esc(s.offset_text)+'</small>'+(s.note?'<em>'+esc(s.note)+'</em>':'')+'</li>').join('');
 }catch(e){box.querySelector('.slot-list').innerHTML='<li class="hint">'+esc(e.message)+'</li>';}
}

async function openDetail(id){
 const box=document.querySelector('#plan-detail');
 box.innerHTML='<p class="hint">正在读取场次…</p>';
 try{box.innerHTML=detailPanel(await reports.get(id));box.querySelector('[data-close-detail]').onclick=()=>{box.innerHTML='';};}
 catch(e){box.innerHTML='<p class="hint">'+esc(e.message)+'</p>';}
}

async function openReschedule(id,refresh){
 const box=document.querySelector('#plan-detail');
 box.innerHTML=reschedulePanel(workspace.reports.find(r=>r.id===id));
 box.querySelector('[data-close-reschedule]').onclick=()=>{box.innerHTML='';};
 const form=box.querySelector('#reschedule-form'),type=box.querySelector('#reschedule-type'),fields=box.querySelector('#reschedule-fields');
 type.onchange=()=>{fields.innerHTML=type.value==='calendar'?calendarForm(workspace.meta,[],null):intervalFormValues(null);};
 submit(form,async()=>{
  await reports.reschedule(id,formPayload(form));
  notify('改期已生效，旧场次不再发布');await refresh();
 });
}

export function bindReports(refresh){
 document.querySelector('#refresh-reports').onclick=refresh;
 const form=document.querySelector('#report-form'),type=form.querySelector('#schedule-type'),fields=document.querySelector('#schedule-fields');
 const swap=()=>{fields.innerHTML=type.value==='calendar'?calendarForm(workspace.meta,[],null):intervalFields();renderPreview(form);};
 type.onchange=swap;form.addEventListener('change',e=>{if(e.target.closest('#calendar-fields'))renderPreview(form);});
 form.addEventListener('input',e=>{if(e.target.name==='local_time')renderPreview(form);});
 renderPreview(form);
 submit(form,async f=>{const payload=formPayload(form);await reports.create(payload);notify('计划已保存，后台将按当地钟点归档');await refresh();});
 document.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=async()=>{try{await reports.toggle(b.dataset.toggle);await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>openDetail(b.dataset.open));
 document.querySelectorAll('[data-reschedule]').forEach(b=>b.onclick=()=>openReschedule(Number(b.dataset.reschedule),refresh));
 document.querySelectorAll('[data-download]').forEach(b=>b.onclick=()=>download(b.dataset.download,b.dataset.format).catch(e=>notify(e.message,true)));
}
