'use strict';
const $ = (s, root = document) => root.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {user:null, config:null, projects:[], project:null, view:'home', book:null, register:false, answerSource:null};
let poller, recorder, stream, recordingTimer, recordingProject, elapsed = 0, toastTimer;
let passwordResetToken=null;
function captureResetToken(){try{const token=new URLSearchParams(location.hash.slice(1)).get('reset');if(token){passwordResetToken=token;history.replaceState(null,'',location.pathname+location.search);}}catch{}}
captureResetToken();
const drafts = {};
let largeText = false, simpleMode = false;
try{simpleMode=localStorage.getItem('studio-simple-mode')==='true';}catch{}
window.StudioExtensions=window.StudioExtensions||[];
const extensionPush=Array.prototype.push;
window.StudioExtensions.push=function(...items){const count=extensionPush.apply(this,items);queueMicrotask(()=>{if(state.user)render();});return count;};
try { largeText = localStorage.getItem('studio-large-text') === 'true'; } catch {}
function applyTextSize(){document.body.classList.toggle('large-text',largeText||simpleMode);document.body.classList.toggle('simple-mode',simpleMode&&!!state.project);}
function textSizeButton(){return `<button type="button" class="quiet text-size-button" data-action="text-size" aria-pressed="${largeText}">${largeText?'恢复标准字号':'大字阅读'}</button>`;}
applyTextSize();
const brand = '<div class="brand"><span class="brand-mark">拾</span><div><span class="brand-name">拾光</span><small>LIFE IN WORDS</small></div></div>';
const bookArt = '<div class="book-scene" aria-hidden="true"><div class="book"><small>A LIFE WORTH TELLING</small><div class="book-title">岁月有声</div><small>把回忆，留给未来</small></div></div>';
function toast(message, error=false){const box=$('#toast');box.textContent=message;box.className='show'+(error?' error':'');clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.className='',6000);}
async function api(path, options={}){
  const headers={'X-Studio-Request':'1',...options.headers};
  if(options.body && !(options.body instanceof FormData)){headers['Content-Type']='application/json';options.body=JSON.stringify(options.body);}
  const response=await fetch('/api'+path,{...options,headers});
  if(!response.ok){let detail;try{detail=(await response.json()).detail;}catch{detail='服务暂时不可用';}throw new Error(typeof detail==='string'?detail:'请检查填写的内容是否完整、长度是否合适');}
  return response.json();
}
const purl = suffix => `/projects/${state.project.id}${suffix}`;
const busy = () => state.project?.jobs.some(j=>['queued','running','cancel_requested'].includes(j.status));
const date = value => new Date(value).toLocaleDateString('zh-CN');
const statusTag = (text,warn=false) => `<span class="tag ${warn?'warn':''}">${esc(text)}</span>`;
function auth(){
  $('#app').innerHTML=`<div class="auth"><section class="auth-story">${brand}<span class="eyebrow">EVERY LIFE IS A STORY</span><h1>一生的故事，<br>值得被好好记住。</h1><p>从一段亲切的聊天开始，把那些平凡而珍贵的日子，慢慢写成一本属于自己的书。</p>${bookArt}<p class="small">在线慢慢聊 · 线下用心听 · 留下一本书</p></section><section class="auth-form"><div class="auth-preferences">${textSizeButton()}</div><span class="eyebrow">YOUR STORY STARTS HERE</span><h2>${state.register?'开启您的拾光旅程':'欢迎回到拾光'}</h2><p class="muted small">${state.register?'创建账户，为自己或家人留住回忆。':'登录后，接着上一次的故事慢慢讲。'}</p><form id="auth-form">${state.register?'<div class="field"><label for="name">您的称呼</label><input id="name" name="name" autocomplete="name" maxlength="80" required placeholder="我们该如何称呼您"></div>':''}<div class="field"><label for="email">邮箱</label><input id="email" name="email" type="email" autocomplete="email" required placeholder="name@example.com"></div><div class="field"><label for="password">密码</label><input id="password" name="password" type="password" minlength="10" maxlength="128" autocomplete="${state.register?'new-password':'current-password'}" required placeholder="至少 10 个字符"></div><button class="primary" type="submit">${state.register?'创建账户':'进入我的工作室'} →</button></form>${!state.register?'<p class="auth-footer"><button type="button" class="quiet" data-action="forgot-password">忘记密码</button></p>':''}<p class="auth-footer">${state.register?'已经有账户？':'第一次来到拾光？'} <button class="quiet" data-action="toggle-auth">${state.register?'登录':'创建账户'}</button></p><div class="notice">${state.config?.mode==='demo'?'当前为演示模式：可体验完整流程，AI 不会转写录音或自动润色，也不会调用外部模型。':'您的录音与文字将发送到运营方配置的 AI 服务，用于转写和书稿生成。请取得讲述者授权后使用。'}</div></section></div>`;
}
function nav(){
  const items=state.project?[['overview','项目总览'],['interview','在线访谈'],['offline','线下采访'],['sources','素材与逐字稿'],['editorial','事实与大纲'],['manuscript','书稿与审校'],['delivery','排版交付'],['operations','照片与交付管理']]:[['home','我的故事'],['guide','使用指南']];
  return items.map(([id,label],i)=>`<button class="nav-btn ${state.view===id?'active':''}" data-view="${id}"><span class="nav-index">0${i+1}</span>${label}</button>`).join('');
}
function shell(content){
  const saved={};document.querySelectorAll('[data-draft]').forEach(el=>{saved[el.dataset.draft]=el.type==='checkbox'?el.checked:el.value;});
  Object.assign(drafts,saved);
  $('#app').innerHTML=`<div class="shell"><aside class="sidebar">${brand}<nav>${nav()}</nav><div class="sidebar-bottom"><p class="sidebar-note">把平凡的日子，<br>写成值得珍藏的故事。</p>${state.project?'<button class="quiet" data-action="home">← 所有故事</button>':''}<div class="account"><span class="avatar">${esc(state.user.name.slice(0,1))}</span><span class="account-text">${esc(state.user.name)}</span><button class="quiet" data-action="logout" title="退出登录">↗</button></div></div></aside><div class="workspace"><header class="topbar"><span>${state.project?'我的故事 / '+esc(state.project.name):'我的工作室'} <button class="quiet mobile-home" data-action="home">首页</button></span><div class="actions">${textSizeButton()}<button type="button" data-action="simple-mode" aria-pressed="${simpleMode}">${simpleMode?'完整工作台':'专心讲述'}</button>${statusTag(state.config.mode==='demo'?'演示模式 · 无外部 AI':'真实 AI 模式',state.config.mode==='demo')}<button class="quiet mobile-home" data-action="logout">退出</button></div></header><main class="main">${content}<div class="footer-note">拾光 · 每一段人生，都有值得留下的光</div></main></div></div>`;
  document.querySelectorAll('[data-draft]').forEach(el=>{if(Object.hasOwn(drafts,el.dataset.draft)){if(el.type==='checkbox')el.checked=drafts[el.dataset.draft];else el.value=drafts[el.dataset.draft];}});
  const log=$('.chat-log');if(log)log.scrollTop=log.scrollHeight;
}
function heading(title,subtitle,actions=''){return `<div class="title-row"><div><h1>${title}</h1><p class="muted">${subtitle}</p></div><div class="actions">${actions}</div></div>`;}
function render(){
  applyTextSize();
  if(!state.user){auth();return;}
  const views={home:homeView,guide:guideView,overview:overviewView,interview:interviewView,offline:offlineView,sources:sourcesView,manuscript:manuscriptView,delivery:deliveryView};
  for(const extension of window.StudioExtensions)Object.assign(views,extension.views||{});
  if(simpleMode&&state.project&&state.view==='interview')views.interview=simpleInterviewView;
  shell((views[state.view]||(()=>'<div class="notice">页面模块正在载入，请稍后重试。</div>'))());
}
function homeView(){
  return `${heading('我的故事','让回忆有处安放，让故事继续流传。','<button class="primary" data-action="new">＋ 创建一本自传</button>')}${localRecordingPanel(true)}<section class="hero"><div><span class="eyebrow">THE STORIES THAT MAKE US</span><h1>不必擅长写作，<br>只要愿意讲述。</h1><p>故乡的老屋、年轻时的远行、家人的笑容……<br>从一件小事开始，我们陪您慢慢整理。</p><button class="primary" data-action="new">开始记录人生 →</button></div>${bookArt}</section><div class="stats"><div class="stat"><strong>${state.projects.length.toString().padStart(2,'0')}</strong><span>正在记录的人生</span></div><div class="stat"><strong>${state.projects.reduce((a,p)=>a+p.source_count,0).toString().padStart(2,'0')}</strong><span>已珍藏的素材</span></div><div class="stat"><strong>${state.projects.reduce((a,p)=>a+p.book_count,0).toString().padStart(2,'0')}</strong><span>已生成的书稿版本</span></div></div><div class="section-heading"><h2>故事书架</h2><span class="muted small">每一本，都独一无二</span></div>${state.projects.length?`<div class="grid">${state.projects.map(p=>`<button class="card project-card" data-project="${p.id}">${statusTag(p.book_count?'已有书稿':'正在采集')}<h2>${esc(p.title)}</h2><p class="muted small">${esc(p.name)} 的人生故事 · ${esc(p.style)}</p><div class="card-footer"><span>${p.source_count} 份素材 · ${p.book_count} 个版本</span><span>翻开故事 ↗</span></div></button>`).join('')}</div>`:'<div class="empty"><h2>书架上的第一本，留给您。</h2><p class="muted">可以为自己记录，也可以陪父母一起讲述。</p><button data-action="new">创建人物档案</button></div>'}`;
}
function guideView(){return `${heading('把故事写成书','两种采访方式，同一套整理与交付流程。')}<div class="grid"><section class="card"><span class="eyebrow">01 / ONLINE</span><h2>和 AI 慢慢聊</h2><p>创建人物档案 → 用文字或分段录音回答 → 核对识别结果 → AI 继续追问 → 确认素材 → 生成章节。</p><p class="muted small">录音需要浏览器麦克风权限，部署后使用 HTTPS。当前是轮次式访谈，支持手动播放问题。</p></section><section class="card"><span class="eyebrow">02 / OFFLINE</span><h2>带着问题去见面</h2><p>下载采访手册 → 人工访谈录音 → 回到工作室上传 → 自动分段转写 → 修订人名与年代 → 生成书稿。</p><p class="muted small">线下指采访现场无需连接本网站；上传后的 AI 处理仍需联网。方言识别能力由配置的模型决定。</p></section><section class="card"><h2>书稿审校</h2><p>每个章节附带来源索引和待核实事项。先处理疑点，再确认定稿。素材变更会使旧稿失效，生成新版本后再审阅。</p></section><section class="card"><h2>交给线下排版</h2><p>下载审阅 Word；所有章节审校后，由创建者登记讲述者对当前版本和用途的确认，再下载正式 Word 和交付 ZIP。平台不自动出版、印刷或收款。</p></section></div>`;}
function jobsView(){
  const jobs=state.project.jobs.filter(j=>j.status!=='done').slice(0,6);
  const names={generate:'书稿生成',transcribe:'录音识别',interview:'AI 追问',editorial_plan:'事实与大纲',editorial_draft:'按大纲撰稿',editorial_check:'全书核查'};
  const labels={queued:'等待处理',running:'正在处理',failed:'未完成',cancel_requested:'正在停止',cancelled:'已取消'};
  return jobs.length?`<div class="jobs">${jobs.map(j=>`<div class="job ${esc(j.status)}"><div><strong>${esc(names[j.kind]||j.kind)}</strong> · ${esc(j.cancel_requested?'正在停止':labels[j.status]||j.status)}<small>${esc(j.error||j.progress||'任务已保存，请稍候')}</small></div><div class="actions">${j.cancel_requested?statusTag('正在停止'):j.status==='failed'?`<button data-retry="${esc(j.id)}" ${busy()?'disabled':''}>重试</button>`:['queued','running'].includes(j.status)?`<button data-cancel-job="${esc(j.id)}">取消任务</button>`:statusTag(labels[j.status]||'后台任务')}</div></div>`).join('')}</div>`:'';
}
function steps(){const p=state.project,b=p.books[0];return `<div class="steps"><div class="step done">01 人物档案</div><div class="step ${p.sources.length?'done':''}">02 采访素材</div><div class="step ${b?'done':''}">03 自传书稿</div><div class="step ${b&&b.revision===p.revision&&b.chapters.every(c=>c.approved)&&b.release?.valid?'done':''}">04 审校交付</div></div>`;}
function overviewView(){
  const p=state.project;return `${heading(esc(p.title),`${esc(p.name)} 的人生故事 · 创建于 ${date(p.created_at)}`)}${steps()}${jobsView()}<div class="grid"><section class="card"><span class="eyebrow">TALK TO REMEMBER</span><h2>今天，想聊些什么？</h2><p class="muted">不用按时间顺序，也不必一口气讲完。<br>想到什么，就从什么开始。</p><button class="primary" data-view="interview">继续在线访谈 →</button></section><section class="card"><span class="eyebrow">LISTEN IN PERSON</span><h2>面对面，听一段人生</h2><p class="muted">带上采访提纲和录音设备。<br>回来后，把记录交给拾光整理。</p><button data-view="offline">准备线下采访 →</button></section><section class="card"><h3>当前进度</h3><p>${p.sources.length} 份素材，其中 ${p.sources.filter(s=>s.confirmed).length} 份已确认<br>${p.books.length} 个书稿版本</p><div class="actions"><button data-view="sources">核对素材</button><button data-view="manuscript">审阅书稿</button></div></section><section class="card"><h3>一起把故事写好</h3><p class="muted small">项目成员可以查看录音、编辑素材、审阅与导出书稿。仅创建者可以添加、移除成员或删除项目。</p>${p.members.map(m=>`<p class="small">${esc(m.name)} · ${esc(m.email)} ${p.owner_id===state.user.id?`<button class="quiet" data-remove-member="${m.id}">移除</button>`:''}</p>`).join('')}${p.owner_id===state.user.id?'<form id="member-form"><label for="member-email">协作编辑的已注册邮箱</label><div class="actions"><input id="member-email" type="email" name="email" placeholder="editor@example.com" required><button type="submit">添加协作者</button></div></form>':''}</section></div><details><summary>查看操作记录与数据管理</summary><div class="card">${p.audit.map(a=>`<div class="audit-line">${esc(a.action)}<small>${esc(a.actor)} · ${new Date(a.created_at).toLocaleString('zh-CN')}</small></div>`).join('')}${p.owner_id===state.user.id?'<p class="small muted">删除项目会移除录音、逐字稿、对话、稿件与操作记录。</p><button class="danger" data-action="delete-project">删除整个项目</button>':''}</div></details>`;
}
function interviewView(){
  const p=state.project;return `${heading('慢慢讲，我们在听','每一段回忆都珍贵。不想回答时，可以跳过或休息。')}${jobsView()}${localRecordingPanel()}<div class="split"><section class="chat-box"><header class="chat-header"><span>✧ 拾光采访助手</span>${statusTag(state.config.mode==='demo'?'固定提纲演示':'根据回答继续追问')}</header><div class="chat-log" aria-live="polite">${p.messages.map(m=>`<div class="bubble ${m.role}"><span class="speaker">${m.role==='user'?esc(p.name):'拾光'}</span>${esc(m.content)}</div>`).join('')}</div><form id="message-form" class="chat-compose"><label for="answer">写下或录下您的回答</label><textarea id="answer" name="text" data-draft="answer-${p.id}" maxlength="10000" placeholder="我记得小时候……" ${busy()?'disabled':''}></textarea><div class="actions"><button class="primary" type="submit" ${busy()?'disabled':''}>发送回答 ↑</button><button type="button" data-action="record" class="${activeRecording?'recording':''}" ${busy()?'disabled':''}>${activeRecording?'■ 结束并试听':'● 录一段话'}</button><button type="button" data-action="speak">朗读问题</button><button type="button" data-action="skip" ${busy()?'disabled':''}>跳过</button><button type="button" data-action="pause" ${busy()?'disabled':''}>休息</button></div><p class="record-note" id="record-status">录音完成后先核对识别文字，再作为回答发送。建议每段 1–3 分钟。</p></form></section><aside class="stack"><section class="card"><span class="eyebrow">CHAPTERS OF YOUR LIFE</span><h2>我们可以聊聊</h2>${state.config.questions.map(([title],i)=>`<div class="topic"><b>0${i+1}</b>${esc(title)}</div>`).join('')}</section><div class="notice">请尽量说出人物、地点和大约年代。记不清也没关系，我们会留下待核实的地方。<br><br>随时可以暂停，已发送的回答会保存在账户中。</div><button data-view="sources">查看与核对全部素材 →</button></aside></div>`;
}
function offlineView(){
  return `${heading('面对面，把故事留下','采访现场无需使用网站。录音完成后，再回来整理。',`<a class="button primary" href="/api${purl('/template')}">↓ 下载采访手册</a>`)}<div class="notice">建议每次采访 20–40 分钟；开场说明录音用途并取得同意。一次只问一个问题，允许跳过与休息。记录人名、地名的准确写法，以及不希望进入书稿的内容。</div><div class="split"><section class="card">${state.config.questions.map(([title,qs],i)=>`<div class="template-section"><span class="eyebrow">CHAPTER 0${i+1}</span><h2>${esc(title)}</h2><ol>${qs.map(q=>`<li>${esc(q)}</li>`).join('')}</ol></div>`).join('')}</section><aside class="stack"><div class="card"><h3>出发前的准备</h3><p>① 手机或录音笔，提前试录<br>② 安静、舒适的空间<br>③ 打印好的采访手册<br>④ 老照片或熟悉的小物件<br>⑤ 讲述者的明确同意</p><p class="muted small">补问提示：那是什么时候？还有谁在场？具体发生了什么？当时您怎么想？</p></div><div class="card"><h3>采访完成了？</h3><p class="muted">上传录音，或者粘贴已有的文字记录。</p><button class="primary" data-view="sources">导入采访素材 →</button></div></aside></div>`;
}
function sourcesView(){
  const p=state.project;return `${heading('素材与逐字稿','先把名字与年代核对好，再把回忆写成书。')}${jobsView()}${localRecordingPanel()}${busy()?'<div class="notice">任务处理期间，素材编辑暂时锁定。您可以继续浏览其他页面。</div>':''}<div class="grid"><form id="upload-form" class="card"><h3>导入采访录音</h3><div class="upload-zone"><span class="serif">一段声音，一份珍藏</span><label for="audio-file" class="small muted">支持 MP3 / M4A / WAV / WEBM / OGG / FLAC / MP4，最大 ${state.config.max_upload_mb} MB</label><input id="audio-file" name="file" type="file" accept=".mp3,.m4a,.wav,.webm,.ogg,.flac,.mp4" required ${busy()?'disabled':''}><button type="submit" ${busy()?'disabled':''}>上传并识别</button></div></form><form id="text-form" class="card"><h3>添加文字记录</h3><label for="source-name">素材标题</label><input id="source-name" name="name" required maxlength="120" placeholder="例如：关于童年的第一次采访" data-draft="new-name-${p.id}"><label for="source-text">采访记录</label><textarea id="source-text" name="text" required maxlength="100000" placeholder="粘贴真实的采访逐字稿或笔记……" data-draft="new-text-${p.id}"></textarea><button type="submit" ${busy()?'disabled':''}>保存文字素材</button></form></div><div class="section-heading"><h2>已珍藏 ${p.sources.length} 份素材</h2><span class="muted small">修改素材后，已有书稿需要重新生成</span></div><div class="stack">${p.sources.map(s=>`<form class="card source-card" data-source-form="${s.id}"><div class="title-row"><h3>${esc(s.name)}</h3>${statusTag(s.included===0||s.included===false?'不入书':s.confirmed?'已确认':'待核对',!s.confirmed)}</div>${s.has_audio?`<audio controls preload="none" src="/api${purl(`/sources/${s.id}/audio`)}"></audio>`:''}<label for="name-${s.id}">素材标题</label><input id="name-${s.id}" name="name" value="${esc(s.name)}" maxlength="120" required data-draft="sn-${s.id}" ${busy()?'disabled':''}><label for="text-${s.id}">逐字稿（只保留允许进入书稿的内容）</label><textarea class="source-text" id="text-${s.id}" name="text" maxlength="100000" data-draft="st-${s.id}" ${busy()?'disabled':''}>${esc(s.text)}</textarea><label class="check source-inclusion"><input type="checkbox" name="included" data-draft="si-${s.id}" ${s.included===0||s.included===false?'':'checked'} ${busy()?'disabled':''}><span>允许这份素材用于新书稿</span></label><p class="small muted">取消后点击“保存修订”：该素材仍保留在项目中，不进入新书稿。此操作不是删除，也不会撤回已发生的 AI 处理或自动清除历史对话。</p>${JSON.parse(s.segments).length?`<details><summary>查看原始识别的分段时间（修订文字不会改变原始记录）</summary>${JSON.parse(s.segments).map(seg=>`<p class="timestamp">${Math.floor(seg.start/60)}:${String(seg.start%60).padStart(2,'0')} – ${Math.floor(seg.end/60)}:${String(Math.floor(seg.end%60)).padStart(2,'0')}<br>${esc(seg.text)}</p>`).join('')}</details>`:''}<div class="actions"><button type="submit" ${busy()?'disabled':''}>保存修订</button><button class="primary" type="submit" name="confirm" value="yes" ${busy()?'disabled':''}>核对无误，确认素材</button>${s.kind==='audio'&&s.text?`<button type="button" data-use-answer="${s.id}" ${busy()?'disabled':''}>填入在线回答</button>`:''}<button class="quiet danger" type="button" data-delete-source="${s.id}" ${busy()?'disabled':''}>删除素材</button></div></form>`).join('')||'<div class="empty"><p class="muted">还没有素材。从一段录音或一段回忆开始。</p></div>'}</div>`;
}
function selectedBook(){return state.project.books.find(b=>b.id===state.book)||state.project.books[0];}
function manuscriptView(){
  const p=state.project,b=selectedBook();
  return `${heading('让回忆，成为章节','生成后逐章审阅，保留讲述者自己的声音。',`<button class="primary" data-action="generate" ${busy()?'disabled':''}>✧ ${p.books.length?'生成新版本':'生成自传初稿'}</button>`)}${jobsView()}${state.config.mode==='demo'?'<div class="notice warning">演示模式仅按素材原文组稿，不进行 AI 润色。您仍可体验审校、版本与导出流程。</div>':''}${b?`<div class="title-row"><select class="version-select" id="book-select" aria-label="选择书稿版本">${p.books.map((v,i)=>`<option value="${v.id}" ${v.id===b.id?'selected':''}>第 ${p.books.length-i} 版 · ${date(v.created_at)}${v.mode==='demo'?' · 演示':''}</option>`).join('')}</select><a class="button" href="/api${purl('/export')}?book_id=${b.id}&draft=true">↓ 下载审阅 Word</a></div>${b.revision!==p.revision?'<div class="notice warning">素材已更新。这一版的来源已过期，不能确认为定稿或正式交付，请生成新版本。</div>':''}<div class="stack">${b.chapters.map((ch,i)=>`<form class="card" data-chapter-form="${ch.id}"><input type="hidden" name="expected_revision" value="${ch.edit_revision??0}" data-draft="cr-${ch.id}"><div class="title-row"><span class="eyebrow">CHAPTER ${String(i+1).padStart(2,'0')}</span><div class="actions">${statusTag(ch.approved?'已确认定稿':'待审校',!ch.approved)}<button type="button" data-move-chapter="${ch.id}" data-direction="-1" ${i===0?'disabled':''} aria-label="将第 ${i+1} 章上移">↑ 上移</button><button type="button" data-move-chapter="${ch.id}" data-direction="1" ${i===b.chapters.length-1?'disabled':''} aria-label="将第 ${i+1} 章下移">↓ 下移</button></div></div><label for="ct-${ch.id}">章节标题</label><input id="ct-${ch.id}" name="title" value="${esc(ch.title)}" required maxlength="200" data-draft="ct-${ch.id}"><p class="chapter-meta">来源：${JSON.parse(ch.source_ids).map(id=>esc(p.sources.find(s=>s.id===id)?.name||'已删除素材')).join('、')} · ${ch.body.length} 字</p><label for="cb-${ch.id}">正文</label><textarea id="cb-${ch.id}" class="chapter-body" name="body" required data-draft="cb-${ch.id}">${esc(ch.body)}</textarea><label for="cq-${ch.id}">待核实事项（每行一条；核实解决后删除相应条目）</label><textarea id="cq-${ch.id}" name="questions" data-draft="cq-${ch.id}" placeholder="没有待核实事项时留空">${esc(JSON.parse(ch.questions).join('\n'))}</textarea><div class="actions"><button type="submit">保存修改</button><button class="primary" type="submit" name="approve" value="yes" ${b.revision!==p.revision?'disabled':''}>已核实，确认定稿</button><button type="button" class="quiet" data-reload-chapter="${ch.id}">重新载入服务器版本</button></div></form>`).join('')}</div>`:'<div class="empty"><h2>您的故事，正在等待成章。</h2><p class="muted">先在「素材与逐字稿」中核对并确认所有允许入书的素材，再生成第一版书稿。</p><button data-view="sources">去整理素材</button></div>'}`;
}
function deliveryView(){
  const p=state.project,b=selectedBook(),included=p.sources.filter(s=>s.included!==0&&s.included!==false);
  const reviewed=!!(b&&b.revision===p.revision&&b.chapters.length&&b.chapters.every(c=>c.approved));
  const ready=reviewed&&!!b.release?.valid;
  const owner=p.owner_id===state.user.id;
  const scopeLabel=scope=>scope==='publication_review'?'提交出版机构审核':'家庭留存';
  let releaseContent='';
  if(b){
    releaseContent=`<section class="card release-card"><h2>确认这一本书的交付用途</h2><p class="muted">章节审校后，还需由讲述者或合法授权人确认这一个版本。此处由创建者登记线下确认，不是实名认证或电子签名，也不代表取得出版许可。</p>${b.release?`<div class="notice ${b.release.valid?'':'warning'}">${b.release.valid?'当前版本已有有效确认':'已有确认已失效，请核对变更后的版本重新登记'}<br>确认人：${esc(b.release.signer)} · 用途：${scopeLabel(b.release.scope)} · ${date(b.release.created_at)}${owner&&b.release.valid?'<div class="actions"><button type="button" class="danger" data-action="revoke-release">撤回当前版本确认</button></div>':''}</div>`:''}${owner?`${b.release?.valid?'<details><summary>更改用途或重新登记确认</summary>':''}<form id="release-form"><input type="hidden" name="expected_content_sha256" value="${esc(b.content_sha256)}"><div class="grid"><div class="field"><label for="release-signer">讲述者或合法授权人的姓名</label><input id="release-signer" name="signer" maxlength="80" required data-draft="rs-${b.id}" placeholder="填写实际确认人"></div><div class="field"><label for="release-relationship">确认人身份</label><select id="release-relationship" name="relationship" data-draft="rr-${b.id}"><option value="self">讲述者本人</option><option value="authorized_representative">合法授权人</option></select></div></div><div class="field"><label for="release-scope">本次允许的用途</label><select id="release-scope" name="scope" data-draft="rp-${b.id}"><option value="family">家庭留存</option><option value="publication_review">提交出版机构审核</option></select></div><div class="field"><label for="release-evidence">线下确认记录</label><textarea id="release-evidence" name="evidence_note" maxlength="2000" required data-draft="re-${b.id}" placeholder="填写确认时间、方式，以及留存凭据的位置。不要填写身份证号等无关个人信息。"></textarea></div><label class="check"><input type="checkbox" name="confirmed" required><span>我已让讲述者或合法授权人审阅当前已保存的完整版本，并确认以上用途；我保管相关授权凭据。未保存的编辑内容不属于本次确认。</span></label><button type="submit" class="primary" ${reviewed?'':'disabled'}>${reviewed?'记录当前版本确认':'请先完成全部章节审校'}</button></form>${b.release?.valid?'</details>':''}`:'<p class="notice">请联系项目创建者登记讲述者或合法授权人对当前版本的确认。</p>'}<p class="small muted">修改章节、调整顺序或更改素材后，需要重新确认。家庭留存与提交出版机构审核均不等于允许公开发行；公开出版和销售另行办理。</p></section>`;
  }
  return `${heading('把故事，交到手中','线上完成内容，线下完成排版、装帧与制作。')}${steps()}${b?`<div class="title-row"><select class="version-select" id="book-select" aria-label="选择交付版本">${p.books.map((v,i)=>`<option value="${v.id}" ${v.id===b.id?'selected':''}>第 ${p.books.length-i} 版 · ${date(v.created_at)}</option>`).join('')}</select>${statusTag(ready?'已具备交付条件':reviewed?'等待讲述者版本确认':'尚需完成审校',!ready)}</div>`:''}${releaseContent}<div class="grid"><section class="card"><span class="eyebrow">EDITORIAL HANDOFF</span><h2>排版交付包</h2><p class="muted">包含正式 Word、Markdown 正文、最小来源索引和交付清单。不附逐字稿、原始识别分段、录音、内部操作记录或采访模板。</p>${ready?`<a class="button primary" href="/api${purl('/export')}?book_id=${b.id}&format=zip&draft=false">↓ 下载交付 ZIP</a>`:'<button disabled>完成审校与版本确认后开放下载</button>'}</section><section class="card"><span class="eyebrow">THE MANUSCRIPT</span><h2>Word 书稿</h2><p class="muted">含扉页、章节目录和正文，方便线下编辑继续设计。审阅稿附带待核实事项，并标注不可用于印刷。</p><div class="actions">${b?`<a class="button" href="/api${purl('/export')}?book_id=${b.id}&draft=true">下载审阅稿</a>`:''}${ready?`<a class="button primary" href="/api${purl('/export')}?book_id=${b.id}&draft=false">下载正式稿</a>`:''}</div></section></div><div class="card delivery-checklist"><h3>交付前，请一起确认</h3><p>${included.length&&included.every(s=>s.confirmed)?'✓':'○'} 允许入书的逐字稿中，姓名、年代、地点已核对<br>${b&&b.revision===p.revision?'✓':'○'} 当前书稿与最新素材一致<br>${reviewed?'✓':'○'} 待核实问题已处理，所有章节均已确认<br>${ready?'✓':'○'} 讲述者或合法授权人已确认当前版本与用途</p><p class="muted small">最终出版、印刷和销售由线下团队办理。项目内的录音和 AI 处理同意不等同于公开发行授权。</p><button data-view="manuscript">返回书稿审校 →</button></div>`;
}
async function refresh(){if(!state.project)return;state.project=await api(`/projects/${state.project.id}`);render();schedulePoll();}
function schedulePoll(){clearTimeout(poller);if(busy()){poller=setTimeout(async()=>{try{await refresh();}catch(e){toast(e.message,true);}},1800);}}
async function openProject(id){clearTimeout(poller);releasePreview();state.project=await api('/projects/'+id);state.view=simpleMode?'interview':'overview';state.book=null;await loadRecoveries();render();schedulePoll();}
async function goHome(){clearTimeout(poller);state.project=null;state.answerSource=null;state.view='home';state.projects=await api('/projects');releasePreview();await loadRecoveries();render();}
function modal(html){$('#modal').innerHTML=`<button class="close quiet" data-action="close" aria-label="关闭">✕</button>${html}`;$('#modal').showModal();}
function showResetModal(){modal('<h2>设置新密码</h2><p class="muted small">设置完成后，请用新密码重新登录。</p><form id="password-reset-form"><label for="reset-password">新密码</label><input id="reset-password" name="password" type="password" autocomplete="new-password" minlength="10" maxlength="128" required><button class="primary" type="submit">更新密码</button></form>');}
window.addEventListener('hashchange',()=>{captureResetToken();if(passwordResetToken&&state.config)showResetModal();});
function createModal(){modal('<h2>开启一本人生之书</h2><p class="muted small">先认识一下故事的主人公，往后慢慢聊。</p><form id="create-form"><div class="field"><label for="p-name">讲述者姓名 / 称呼</label><input id="p-name" name="name" maxlength="80" required placeholder="例如：陈爷爷"></div><div class="field"><label for="p-title">自传书名</label><input id="p-title" name="title" maxlength="120" required placeholder="例如：我的岁月，我的家"></div><div class="field"><label for="p-year">出生年份（可留空或填写约数）</label><input id="p-year" name="birth_year" maxlength="20" placeholder="例如：1950 年左右"></div><div class="field"><label for="p-style">文字风格</label><select id="p-style" name="style"><option>质朴亲切</option><option>温暖叙事</option><option>纪实克制</option></select></div><label class="check"><input type="checkbox" name="consent" required><span>我已取得讲述者或合法授权人的同意，允许保存录音与文字、发送到配置的 AI 服务进行转写和写作，并由我添加的项目成员审阅。此同意不包含公开发行授权。</span></label><button class="primary" type="submit">创建人物档案 →</button></form>');}
function clearDrafts(keys){for(const key of keys){delete drafts[key];document.querySelectorAll('[data-draft]').forEach(el=>{if(el.dataset.draft===key)el.removeAttribute('data-draft');});}}
async function upload(file){const form=new FormData();form.append('file',file);await api(purl('/audio'),{method:'POST',body:form});toast('录音已保存，后台识别任务已提交');state.view='sources';await refresh();}
const recordingScope = () => state.user && state.project ? [state.user.id,state.project.id] : null;
let pendingRecordings = [], recordingPreview = null, activeRecording = null;
let recordingStartPending = false, recordingTransfer = false, localRecordingConsent = false;
const storage = () => { if(!window.RecordingStore)throw new Error('录音恢复模块尚未加载，请刷新页面后重试。');return window.RecordingStore; };
const recordActive = () => !!activeRecording || recordingStartPending || recordingTransfer;
function releasePreview(){if(recordingPreview?.url)URL.revokeObjectURL(recordingPreview.url);recordingPreview=null;}
async function loadRecoveries(){
  const userId=state.user?.id;
  if(!userId){pendingRecordings=[];return;}
  try{const rows=await storage().list(userId);if(state.user?.id===userId)pendingRecordings=rows;}
  catch(error){if(state.user?.id===userId)toast(error.message,true);}
}
function localRecordingPanel(allProjects=false){
  const rows=pendingRecordings.filter(row=>allProjects||row.projectId===state.project?.id);
  if(!rows.length)return '';
  return `<section class="card local-recordings"><h2>本机尚存 ${rows.length} 段录音</h2><p class="small muted">录音缓存在当前浏览器，上传成功后清除。共享设备上的其他使用者可能访问网站数据；不用时请下载或清除。刷新可恢复已保存分片，突然关闭可能丢失最后一秒或未写入片段，部分中断录音可能无法播放。</p>${rows.map(row=>{const preview=recordingPreview?.id===row.id?recordingPreview:null;return `<article class="local-recording" data-local-recording="${esc(row.id)}"><h3>${new Date(row.createdAt).toLocaleString('zh-CN')} 的录音</h3><p class="small">${row.bytes?`${(row.bytes/1024).toFixed(1)} KB · ${row.chunks} 个已保存片段`:'尚未保存完整声音片段'}${allProjects?` · ${esc(state.projects.find(p=>p.id===row.projectId)?.title||'原项目不可访问')}`:''}</p>${row.status==='recording'||row.status==='uploading'?'<p class="notice warning">上次未正常结束，或另一标签页仍在录制。请确认其他标签页已经停止，再恢复、试听或下载；网络中断时请先检查服务器素材，避免重复上传。</p>':''}${row.status==='uploaded'?'<p class="notice">服务器已接收，剩余本机副本可安全清除。</p>':''}${row.note?`<p class="notice warning">${esc(row.note)}</p>`:''}${preview?`<audio controls preload="metadata" src="${esc(preview.url)}"></audio><p class="small muted">请先重听，再确认上传。无法播放时可先下载保留，交由人工检查。</p>`:''}<div class="actions"><button type="button" data-preview-recording="${esc(row.id)}" ${row.bytes?'':'disabled'}>恢复并试听</button>${preview&&row.status!=='uploaded'?`<button type="button" class="primary" data-upload-recording="${esc(row.id)}" ${recordingTransfer||busy()?'disabled':''}>确认并上传</button>`:''}<button type="button" data-download-recording="${esc(row.id)}" ${row.bytes?'':'disabled'}>下载备份</button><button type="button" class="quiet danger" data-discard-recording="${esc(row.id)}">清除本机副本</button></div></article>`;}).join('')}</section>`;
}
async function previewRecording(id){
  const row=pendingRecordings.find(row=>row.id===id);
  if(!row)throw new Error('本机录音已不存在，请刷新页面。');
  if((row.status==='recording'||row.status==='uploading')&&!confirm('请先确认其他拾光标签页已停止录音或上传。是否恢复这份已保存的录音片段？'))return;
  if(recordingPreview?.id!==id){const loaded=await storage().read(state.user.id,row.projectId,id);releasePreview();recordingPreview={...loaded,url:URL.createObjectURL(loaded.blob)};}
  if(!recordingPreview.blob.size)throw new Error('还没有可恢复的录音片段。');
  render();
}
function recordingFilename(row){const extension=row.mime.includes('mp4')?'m4a':row.mime.includes('ogg')?'ogg':'webm';return `在线访谈-${row.id}.${extension}`;}
function downloadBlob(blob,name){const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
async function downloadRecording(id){
  const row=pendingRecordings.find(row=>row.id===id);if(!row)throw new Error('本机录音不存在。');
  const value=recordingPreview?.id===id?recordingPreview:await storage().read(state.user.id,row.projectId,id);
  downloadBlob(value.blob,recordingFilename(value));toast('已开始下载；请确认本机文件完整后，再清除浏览器副本。');
}
async function discardRecording(id){
  const row=pendingRecordings.find(row=>row.id===id);if(!row)return;
  if(!confirm('清除后无法从此浏览器恢复。请确认已经上传或下载备份，是否继续？'))return;
  await storage().remove(state.user.id,row.projectId,id);if(recordingPreview?.id===id)releasePreview();await loadRecoveries();render();
}
async function uploadRecording(id){
  const row=pendingRecordings.find(row=>row.id===id);
  if(!row||recordingPreview?.id!==id)throw new Error('请先恢复并试听这段录音。');
  if(recordingTransfer)return;
  recordingTransfer=true;const userId=state.user.id;const preview=recordingPreview;
  try{
    const file=new File([preview.blob],recordingFilename(row),{type:row.mime});
    if(file.size>state.config.max_upload_mb*1024*1024)throw new Error('录音超过上传上限，请下载后分段处理。');
    await storage().update(userId,row.projectId,id,{status:'uploading',note:''});
    const form=new FormData();form.append('file',file);form.append('recording_id',id);
    const result=await api(`/projects/${row.projectId}/audio`,{method:'POST',body:form});
    // Receipt is saved before deletion so a failed local cleanup does not prompt another upload.
    await storage().update(userId,row.projectId,id,{status:'uploaded',uploadedSourceId:result.id,note:''});
    try{await storage().remove(userId,row.projectId,id);}catch{toast('上传成功，但本机缓存尚未清除，请点击“清除本机副本”。',true);}
    releasePreview();await loadRecoveries();
    toast('录音已上传，识别完成后请核对文字并发送回答。');
    if(state.project?.id===row.projectId){state.view=simpleMode?'interview':'sources';await refresh();}else render();
  }catch(error){
    try{await storage().update(userId,row.projectId,id,{status:'ready',note:'上传未确认成功，录音仍保留本机。请先检查服务器是否已有这段录音，再重试或下载备份。'});}catch{}
    await loadRecoveries();render();toast(error.message,true);
  }finally{recordingTransfer=false;render();}
}
async function finishRecording(context){
  if(context.finishing)return;context.finishing=true;clearInterval(recordingTimer);
  context.stream.getTracks().forEach(track=>{track.onended=null;track.stop();});
  await context.writes;
  const blob=new Blob(context.memory,{type:context.mime});
  let row;
  try{row=await storage().update(context.userId,context.projectId,context.id,{status:'ready',note:context.failure?'本地保存不完整；当前页面仍有完整录音，请立即下载备份，离开页面后可能只能恢复部分内容。':context.reason||''});}
  catch{row={id:context.id,userId:context.userId,projectId:context.projectId,mime:context.mime,createdAt:context.createdAt,status:'ready',chunks:context.memory.length,bytes:blob.size,note:'无法写入本机存储，请立即下载备份；关闭页面会丢失未保存内容。'};context.failure=true;}
  activeRecording=null;recorder=null;stream=null;recordingStartPending=false;
  releasePreview();recordingPreview={...row,blob,url:URL.createObjectURL(blob),memoryOnly:context.failure};
  await loadRecoveries();
  if(!pendingRecordings.some(item=>item.id===row.id))pendingRecordings.unshift(row);
  else if(context.failure)pendingRecordings=pendingRecordings.map(item=>item.id===row.id?{...row,bytes:blob.size}:item);
  render();toast(context.reason||'录音已保存在本机，请重听后确认上传。',!!context.failure);
}
function stopRecording(reason=''){
  const context=activeRecording;if(!context)return;
  context.reason=reason||context.reason;
  if(context.recorder.state!=='inactive'){try{context.recorder.stop();}catch{finishRecording(context);}}
  else setTimeout(()=>finishRecording(context),100);
}
async function record(){
  if(activeRecording){stopRecording();return;}
  if(recordingStartPending||recordingTransfer)return;
  if(!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder)throw new Error('当前浏览器无法录音，请使用 HTTPS/localhost 下的现代浏览器，或直接上传录音文件。');
  if(!localRecordingConsent){if(!confirm('为防止刷新丢失，录音会按分片保存在这台设备的浏览器中，上传成功后清除。共享设备上请及时下载或清除，并在退出前处理未上传录音。浏览器突然关闭仍可能丢失最后一秒或未写入内容。是否开始？'))return;localRecordingConsent=true;}
  recordingStartPending=true;
  const scope=recordingScope();if(!scope){recordingStartPending=false;throw new Error('请先打开一个故事。');}
  let context;
  try{
    const type=['audio/webm;codecs=opus','audio/webm','audio/mp4','audio/ogg'].find(value=>MediaRecorder.isTypeSupported(value));
    const row=await storage().create(scope[0],scope[1],type||'audio/webm');
    context={...row,sequence:0,memory:[],writes:Promise.resolve(),failure:false,finishing:false,reason:''};
    try{context.stream=await navigator.mediaDevices.getUserMedia({audio:true});}catch(error){await storage().remove(scope[0],scope[1],row.id);throw error;}
    context.recorder=new MediaRecorder(context.stream,type?{mimeType:type}:undefined);context.mime=context.recorder.mimeType||type||'audio/webm';
    activeRecording=context;recorder=context.recorder;stream=context.stream;elapsed=0;
    recorder.ondataavailable=event=>{
      if(!event.data.size)return;context.memory.push(event.data);const sequence=context.sequence++;
      context.writes=context.writes.then(()=>storage().append(context.userId,context.projectId,context.id,sequence,event.data)).catch(()=>{context.failure=true;stopRecording('本地空间不足或保存失败，已停止录音。请立即下载当前页面中的录音备份。');});
    };
    recorder.onstop=()=>finishRecording(context);
    recorder.onerror=()=>stopRecording('录音设备发生异常，已保存收到的声音片段。请重听或下载检查。');
    stream.getTracks().forEach(track=>{track.onended=()=>stopRecording('麦克风连接已结束，已保存收到的声音片段。');});
    recorder.start(1000);recordingStartPending=false;render();
    recordingTimer=setInterval(()=>{elapsed++;const note=$('#record-status');if(note)note.textContent=`正在录音 ${Math.floor(elapsed/60)}:${String(elapsed%60).padStart(2,'0')} · 每秒保存分片 · 10 分钟后停止`;if(elapsed>=600)stopRecording('已达到 10 分钟，录音已保存。请重听后上传，休息一下再继续。');},1000);
  }catch(error){recordingStartPending=false;context?.stream?.getTracks().forEach(track=>track.stop());if(context?.id)try{await storage().remove(scope[0],scope[1],context.id);}catch{}activeRecording=null;throw new Error(error.name==='NotAllowedError'?'未获得麦克风权限；可以在浏览器设置中允许，或使用上传录音。':error.message);}
}
async function logout(){
  if(recordActive())throw new Error('请先结束并保存录音，再退出登录。');
  await loadRecoveries();
  if(!confirm(pendingRecordings.length?`这台浏览器还有 ${pendingRecordings.length} 段录音。退出将清除本账号的本机录音和未保存文字，未上传且未备份的内容无法恢复。请先下载或上传。确定退出并清除？`:'退出将清除本账号的浏览器录音缓存和未保存文字，不影响服务器中已保存的内容。是否退出？'))return;
  if(state.user)try{await storage().clearUser(state.user.id);}catch{if(!confirm('浏览器暂时无法自动清理本机录音缓存。是否仍退出，并由您手动清除此网站的浏览器数据？'))return;}
  await api('/logout',{method:'POST'});clearTimeout(poller);releasePreview();pendingRecordings=[];localRecordingConsent=false;state.user=null;state.project=null;state.answerSource=null;for(const key of Object.keys(drafts))delete drafts[key];if('speechSynthesis'in window)speechSynthesis.cancel();render();
}
function simpleInterviewView(){
  const p=state.project,last=[...p.messages].reverse().find(m=>m.role==='assistant');
  const waiting=p.sources.filter(s=>s.kind==='audio'&&s.name.startsWith('在线访谈-')&&s.text&&!s.text.startsWith('采访者：')).slice(-1)[0];
  return `${heading('我们慢慢聊','先听问题，再讲一段。随时可以休息。')}${jobsView()}<section class="card focused-interview"><p class="speaker">拾光想听听</p><p class="focused-question">${esc(last?.content||'今天想和我们讲讲什么？')}</p><div class="actions"><button class="primary record-large ${activeRecording?'recording':''}" type="button" data-action="record" ${busy()?'disabled':''}>${activeRecording?'■ 结束并试听':'● 开始讲述'}</button><button type="button" data-action="speak">朗读问题</button><button type="button" data-action="pause" ${busy()?'disabled':''}>先休息</button></div><p class="record-note" id="record-status">每段建议 1–3 分钟。停止后可重听，确认后再上传。</p></section>${localRecordingPanel()}${waiting?`<form class="card" id="simple-recording-answer-form"><h2>听写好了，请核对</h2><label for="simple-audio-answer">您的这段话</label><input type="hidden" name="source_id" value="${esc(waiting.id)}"><textarea id="simple-audio-answer" name="text" data-draft="simple-${waiting.id}" maxlength="10000" required>${esc(waiting.text)}</textarea><button class="primary" type="submit" ${busy()?'disabled':''}>文字没问题，发送并继续</button></form>`:''}<details class="card" ${state.answerSource?'open':''}><summary>也可以打字回答</summary><form id="message-form"><label for="answer">您的回答</label><textarea id="answer" name="text" data-draft="answer-${p.id}" maxlength="10000" required></textarea><button type="submit" class="primary" ${busy()?'disabled':''}>发送回答 ↑</button><button type="button" data-action="skip" ${busy()?'disabled':''}>跳过这个问题</button></form></details><p class="small muted">要管理素材、修改书稿或处理照片，可点击上方“完整工作台”。本机录音在共享设备上需要及时清除。</p>`;
}

async function handleClick(event){
  const target=event.target.closest('button,[data-project]');if(!target||target.disabled)return;
  if(recordActive()&&target.dataset.action!=='record'&&target.dataset.action!=='speak'){toast('请先结束录音，保存这段回忆。');event.preventDefault();return;}
  if(recordingPreview?.memoryOnly&&(target.dataset.view||target.dataset.project||['home','new','record','simple-mode','text-size'].includes(target.dataset.action))){toast('本地保存不完整，请先下载当前录音备份，再清除本机副本后继续。',true);return;}
  for(const extension of window.StudioExtensions)if(extension.click&&await extension.click(target,event))return;
  if(target.dataset.previewRecording){await previewRecording(target.dataset.previewRecording);return;}
  if(target.dataset.downloadRecording){await downloadRecording(target.dataset.downloadRecording);return;}
  if(target.dataset.uploadRecording){await uploadRecording(target.dataset.uploadRecording);return;}
  if(target.dataset.discardRecording){await discardRecording(target.dataset.discardRecording);return;}
  if(target.dataset.cancelJob){await api(purl(`/jobs/${target.dataset.cancelJob}/cancel`),{method:'POST'});toast('已请求取消任务');await refresh();return;}
  if(target.dataset.view){state.view=target.dataset.view;render();return;}
  if(target.dataset.project){await openProject(target.dataset.project);return;}
  if(target.dataset.moveChapter){const b=selectedBook(),ids=b.chapters.map(ch=>ch.id),from=ids.indexOf(target.dataset.moveChapter),to=from+Number(target.dataset.direction);if(from<0||to<0||to>=ids.length)return;[ids[from],ids[to]]=[ids[to],ids[from]];await api(purl(`/books/${b.id}/order`),{method:'POST',body:{chapter_ids:ids}});toast('章节顺序已更新；交付前需重新确认当前版本。未保存的文字仍保留在编辑框中。');await refresh();return;}
  if(target.dataset.reloadChapter){if(!confirm('重新载入会放弃这一章尚未保存的修改。继续？'))return;const id=target.dataset.reloadChapter;clearDrafts(['ct-'+id,'cb-'+id,'cq-'+id,'cr-'+id]);await refresh();return;}
  if(target.dataset.retry){await api(purl(`/jobs/${target.dataset.retry}/retry`),{method:'POST'});await refresh();return;}
  if(target.dataset.deleteSource){if(!confirm('删除这份素材和原始录音？已有书稿将需要重新生成。'))return;await api(purl(`/sources/${target.dataset.deleteSource}`),{method:'DELETE'});await refresh();return;}
  if(target.dataset.removeMember){if(!confirm('移除后，该账户将失去这个项目的访问权限。继续？'))return;await api(purl(`/members/${target.dataset.removeMember}`),{method:'DELETE'});await refresh();return;}
  if(target.dataset.useAnswer){const source=state.project.sources.find(s=>s.id===target.dataset.useAnswer);state.answerSource=source.id;drafts['answer-'+state.project.id]=drafts['st-'+source.id]??source.text;state.view='interview';render();toast('已填入回答框，请核对后发送；音频素材仍保留在素材库中。');return;}
  switch(target.dataset.action){
    case 'simple-mode':simpleMode=!simpleMode;try{localStorage.setItem('studio-simple-mode',String(simpleMode));}catch{}if(simpleMode&&state.project)state.view='interview';render();break;
    case 'text-size':largeText=!largeText;try{localStorage.setItem('studio-large-text',String(largeText));}catch{}applyTextSize();render();break;
    case 'forgot-password':modal('<h2>找回账户</h2><p class="muted small">如果该邮箱已注册，我们将发送重设链接。请勿将收到的链接转发给他人。</p><form id="password-request-form"><label for="recovery-email">注册邮箱</label><input id="recovery-email" name="email" type="email" maxlength="180" autocomplete="email" required><button class="primary" type="submit">发送重设链接</button></form>');break;
    case 'toggle-auth':state.register=!state.register;render();break;
    case 'close':if($('#password-reset-form'))passwordResetToken=null;$('#modal').close();break;
    case 'new':createModal();break;
    case 'home':await goHome();break;
    case 'logout':await logout();break;
    case 'revoke-release':if(confirm('撤回后将停止这一版本的正式下载；已下载的副本无法撤回。继续？')){await api(purl(`/books/${selectedBook().id}/release`),{method:'DELETE'});toast('已撤回当前版本确认');await refresh();}break;
    case 'generate':await api(purl('/generate'),{method:'POST'});toast('已提交生成任务；已有版本会保留');await refresh();break;
    case 'delete-project':if(confirm('确定永久删除整个项目？录音、逐字稿、书稿和此浏览器对应的录音缓存都将删除，无法撤销。')){const pid=state.project.id;await api(purl(''),{method:'DELETE'});try{for(const row of pendingRecordings.filter(row=>row.projectId===pid))await storage().remove(state.user.id,pid,row.id);}catch{toast('服务器项目已删除，但本机录音缓存未清除，请在首页手动处理。',true);}await goHome();}break;
    case 'record':await record();break;
    case 'speak':if(!('speechSynthesis' in window))throw new Error('当前浏览器不支持朗读');speechSynthesis.cancel();const question=[...state.project.messages].reverse().find(m=>m.role==='assistant');const speech=new SpeechSynthesisUtterance(question?.content||'');speech.lang='zh-CN';speech.rate=.85;speechSynthesis.speak(speech);break;
    case 'skip':case 'pause':await api(purl('/messages'),{method:'POST',body:{text:target.dataset.action==='skip'?'跳过':'休息'}});await refresh();break;
  }
}
$('#modal').addEventListener('cancel',()=>{if($('#password-reset-form'))passwordResetToken=null;});
document.addEventListener('click',event=>{handleClick(event).catch(e=>toast(e.message,true));});
document.addEventListener('change',event=>{if(event.target.id==='book-select'){state.book=event.target.value;render();}});
document.addEventListener('submit',async event=>{
  event.preventDefault();const form=event.target,submitter=event.submitter;
  if(recordActive()){toast('请先结束并保存录音。');return;}
  const data=Object.fromEntries(new FormData(form));const buttons=[...form.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
  try{
    for(const extension of window.StudioExtensions)if(extension.submit&&await extension.submit(form,submitter,data))return;
    if(form.id==='password-request-form'){const result=await api('/password/request',{method:'POST',body:{email:data.email}});$('#modal').close();toast(result.message||'如果邮箱已注册，重设链接将发送到该邮箱。');}
    else if(form.id==='password-reset-form'){if(!passwordResetToken)throw new Error('重设链接已失效，请重新申请。');await api('/password/reset',{method:'POST',body:{token:passwordResetToken,password:data.password}});passwordResetToken=null;$('#modal').close();clearTimeout(poller);releasePreview();pendingRecordings=[];state.user=null;state.project=null;for(const key of Object.keys(drafts))delete drafts[key];render();toast('密码已更新，请重新登录。');}
    else if(form.id==='auth-form'){state.user=await api(state.register?'/register':'/login',{method:'POST',body:data});await goHome();}
    else if(form.id==='create-form'){const p=await api('/projects',{method:'POST',body:{...data,consent:!!data.consent}});$('#modal').close();await openProject(p.id);}
    else if(form.id==='simple-recording-answer-form'){await api(purl('/messages'),{method:'POST',body:{text:data.text,source_id:data.source_id}});clearDrafts(['simple-'+data.source_id]);await refresh();}
    else if(form.id==='message-form'){await api(purl('/messages'),{method:'POST',body:{text:data.text,source_id:state.answerSource}});state.answerSource=null;clearDrafts(['answer-'+state.project.id]);await refresh();}
    else if(form.id==='upload-form'){if(!data.file.size)throw new Error('请选择录音文件');if(data.file.size>state.config.max_upload_mb*1024*1024)throw new Error('录音文件超过上传限制');await upload(data.file);}
    else if(form.id==='text-form'){await api(purl('/sources'),{method:'POST',body:data});clearDrafts(['new-name-'+state.project.id,'new-text-'+state.project.id]);toast('文字素材已保存，请核对后确认');await refresh();}
    else if(form.id==='member-form'){await api(purl('/members'),{method:'POST',body:data});toast('协作编辑已添加');await refresh();}
    else if(form.dataset.sourceForm){const id=form.dataset.sourceForm,included=!!data.included;if((included||submitter?.name==='confirm')&&!data.text.trim())throw new Error('用于书稿或需要确认的素材，请先填写逐字稿；也可取消入书后保存。');await api(purl('/sources/'+id),{method:'PUT',body:{...data,included,confirmed:submitter?.name==='confirm'}});clearDrafts(['sn-'+id,'st-'+id,'si-'+id]);toast('素材已保存');await refresh();}
    else if(form.id==='release-form'){const b=selectedBook();await api(purl(`/books/${b.id}/release`),{method:'POST',body:{...data,confirmed:!!data.confirmed}});clearDrafts(['rs-'+b.id,'rr-'+b.id,'rp-'+b.id,'re-'+b.id]);toast('已登记当前版本和交付用途的确认');await refresh();}
    else if(form.dataset.chapterForm){const id=form.dataset.chapterForm;await api(purl('/chapters/'+id),{method:'PUT',body:{...data,expected_revision:Number(data.expected_revision),questions:data.questions.split('\n').map(q=>q.trim()).filter(Boolean),approved:submitter?.name==='approve'}});clearDrafts(['ct-'+id,'cb-'+id,'cq-'+id,'cr-'+id]);toast('书稿已保存');await refresh();}
  }catch(e){toast(e.message,true);}finally{buttons.forEach(b=>b.disabled=false);}
});
window.addEventListener('beforeunload',event=>{if(recordActive()||recordingPreview?.memoryOnly){event.preventDefault();event.returnValue='';}});
window.addEventListener('pagehide',()=>{if(activeRecording)stopRecording('页面离开时尝试保存，恢复后请检查录音完整性。');});
window.Studio={get state(){return state;},api,purl,refresh,render,esc,toast,heading,statusTag,selectedBook,busy,clearDrafts};
(async()=>{try{state.config=await api('/config');try{state.user=await api('/me');}catch{}if(state.user)await goHome();else render();if(passwordResetToken)showResetModal();}catch(e){$('#app').textContent='暂时无法连接工作室，请刷新重试。';toast(e.message,true);}})();
