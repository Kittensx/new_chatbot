/* Local AI v0.6.2c: self-contained offline GUI. No CDN, tracking, or unsafe HTML from chats. */
'use strict';
const token = document.querySelector('meta[name="localai-token"]').content;
const $ = id => document.getElementById(id);
let state = null, page = 'chat', section = 'personas', uploads = [], hits = [];
let searchChecked = new Set(), editorId = 'balanced', year = new Date().getFullYear(), month = new Date().getMonth() + 1;
let lastJob = '', pollTimer = null, jobPollBusy = false;
// Reuse unchanged message DOM during polling. Rebuilding it every 1.4 seconds
// closes <details> panels and interrupts text selection, scrolling and focus.
let renderedThreadKey = null;
let webProviderDraft = null;
let webModeDraft = null; // UI draft survives background polling until Apply
const webBatchSelection = new Set(); // preserve checkboxes between status polls
const thinkingPanelState = new Map();
// Polling must not overwrite local edits or accept a state snapshot issued
// before an acknowledged settings update. Settings persist immediately on toggle.
let stateRequestNo = 0, lastAppliedStateRequest = 0, minAcceptableStateRequest = 0;
const settingDrafts = {thoughts:null,tools:null};
const settingRevisions = {thoughts:0,tools:0};
const settingSaves = {thoughts:false,tools:false};
const TOOL_FIELDS = [['permCalc','calculator'],['permClock','local_clock'],
  ['permCalRead','calendar_read'],['permCalWrite','calendar_propose'],
  ['permThoughtPin','thought_pin'],['permThoughtRead','thought_read']];
const SETTING_GROUPS = {
  thoughts:{
    source:'thought_settings', action:'thought.settings', argument:'settings', status:'thoughtSettingsStatus',
    read:()=>({allow_ai_pinning:flag('allowAgentPins'),
      automatic_recall:flag('recallPinnedThoughts'),max_active_ai_pins:Number(val('maxThoughtPins'))}),
    apply:s=>{
      $('allowAgentPins').checked=Boolean(s.allow_ai_pinning);
      $('recallPinnedThoughts').checked=Boolean(s.automatic_recall);
      $('maxThoughtPins').value=s.max_active_ai_pins??25;
    },
    validate:s=>{
      if (!Number.isInteger(s.max_active_ai_pins) || s.max_active_ai_pins<1 || s.max_active_ai_pins>100)
        throw new Error('Maximum AI-owned pins must be between 1 and 100.');
    },
  },
  tools:{
    source:'tool_permissions', action:'permissions.save', argument:'permissions', status:'toolPermissionsStatus',
    read:()=>Object.fromEntries(TOOL_FIELDS.map(([id,key])=>[key,flag(id)])),
    apply:s=>{for (const [id,key] of TOOL_FIELDS)$(id).checked=Boolean(s[key]);},
    validate:()=>{},
  },
};
function settingStatus(name, message, error=false) {
  const node=$(SETTING_GROUPS[name].status);
  node.textContent=message;
  node.classList.toggle('setting-error',error);
}
function paintSettings(name, saved) {
  // Changes made while a model is answering stay visible through each poll.
  if (settingDrafts[name]!==null) return;
  SETTING_GROUPS[name].apply(saved||{});
}
function stageSettings(name, saveImmediately=true) {
  settingDrafts[name]=SETTING_GROUPS[name].read();
  settingRevisions[name]++;
  settingStatus(name,saveImmediately?'Saving changes…':'Unsaved changes · will save on leaving field');
  if (saveImmediately) void run(()=>persistSettings(name));
}
async function persistSettings(name) {
  if (settingSaves[name]) return; // the active save will pick up newer edits
  const spec=SETTING_GROUPS[name];
  settingSaves[name]=true;
  try {
    while (settingDrafts[name]!==null) {
      const wanted={...settingDrafts[name]}, revision=settingRevisions[name];
      spec.validate(wanted);
      settingStatus(name,'Saving changes…');
      try {
        await act(spec.action,{[spec.argument]:wanted},false);
      } catch (e) {
        // Keep the user's selections for a retry rather than losing them.
        settingStatus(name,'Save failed — click Save to retry',true);
        throw e;
      }
      minAcceptableStateRequest=Math.max(minAcceptableStateRequest,stateRequestNo+1);
      if (state)state[spec.source]={...wanted};
      if (revision===settingRevisions[name]) {
        settingDrafts[name]=null;
        spec.apply(wanted);
        settingStatus(name,'Saved · takes effect on the next model request');
      }
      // Refresh the other panels; only newer server snapshots can be applied.
      void run(refresh);
    }
  } catch (e) {
    if (!$(spec.status).classList.contains('setting-error'))
      settingStatus(name,'Not saved — correct the value and click Save',true);
    throw e;
  } finally {
    settingSaves[name]=false;
  }
}
const el = (tag, cls='', txt='') => {
  const n = document.createElement(tag); if (cls) n.className=cls;
  if (txt !== null) n.textContent=String(txt); return n;
};
const clear = n => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
const val = id => $(id)?.value ?? '';
const flag = id => Boolean($(id)?.checked);
const fmtDate = stamp => { try { return stamp ? new Date(stamp).toLocaleString() : ''; } catch { return stamp || ''; }};
const short = (x,n=210) => String(x||'').length > n ? String(x||'').slice(0,n)+'…' : String(x||'');
const humanDuration = v => {const seconds=Number(v)||0;const d=Math.floor(seconds/86400),h=Math.floor(seconds%86400/3600),m=Math.floor(seconds%3600/60),s=seconds%60;return [d&&`${d}d`,h&&`${h}h`,m&&`${m}m`,`${s}s`].filter(Boolean).join(' ');};
const confirmAction = message => window.confirm(message);
function notice(message, bad=false) {
  const t=$('toast'); t.textContent=message; t.className='toast visible'+(bad?' error':'');
  $('statusText').textContent=message;
  if (t._tid) clearTimeout(t._tid);
  t._tid=setTimeout(()=>t.className='toast',6000);
}
async function request(path, body=null) {
	// Line 114 updated state:
  const res = await fetch(path.startsWith('http') ? path : 'http://127.0.0.1:8000' + path, {method:body?'POST':'GET', cache:'no-store',
    headers: {'X-LocalAI-Token': token,...(body?{'Content-Type':'application/json'}:{})},
    ...(body?{body:JSON.stringify(body)}:{})});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Local request failed');
  return data;
}
async function act(action, extra={}, reload=true) {
  const r=await request('/api/action',{action,...extra});
  if (reload) await refresh();
  return r.result;
}
async function refresh() {
  const seq=++stateRequestNo;
  const updated=await request('/api/state');
  if (seq<minAcceptableStateRequest || seq<lastAppliedStateRequest) return;
  lastAppliedStateRequest=seq;
  state=updated; render();
  if (state.job && ['running','ready'].includes(state.job.status)) startPolling();
  else stopPolling();
}
async function run(fn) { try { return await fn(); } catch(e) { notice(e.message||String(e),true); } }
function switchPage(target) {
  page=target;
  document.querySelectorAll('[data-page].tab').forEach(n=>n.classList.toggle('on',n.dataset.page===target));
  document.querySelectorAll('.page').forEach(n=>n.classList.toggle('active',n.id==='page-'+target));
}
function switchStudio(target) {
  section=target;
  document.querySelectorAll('[data-studio]').forEach(n=>n.classList.toggle('on',n.dataset.studio===target));
  document.querySelectorAll('.studio-section').forEach(n=>n.classList.toggle('active',n.id==='studio-'+target));
  if (target==='tools') void run(loadCalendar);
}
function render() {
  if (!state) return;
  $('engineStatus').textContent=state.system.configured ? '● Local Ollama · '+state.system.model : '○ Sandbox not configured';
  $('systemModel').textContent=state.system.model||'Not installed';
  $('systemWorkspace').textContent=state.system.workspace;
  renderSidebar(); renderChat(); renderContext(); renderStudio();
  switchPage(page); switchStudio(section);
}
function renderSidebar() {
  const root=clear($('conversationTree'));
  const needle=val('chatFilter').toLowerCase();
  const folders=[{id:'',name:'Unfiled conversations'},...(state.folders||[])];
  for (const f of folders) {
    const list=(state.conversations||[]).filter(c=>(c.folder_id||'')===f.id && c.name.toLowerCase().includes(needle));
    if (!list.length && needle) continue;
    const label=el('div','folderhead'); label.append(el('span','',f.name));
    if (f.id) { const manage=el('button','', '⋯'); manage.title='Rename or remove folder';
      manage.onclick=()=>run(async()=>{
        const name=prompt('Rename folder (leave blank to delete):',f.name); if(name===null) return;
        if (name.trim()) await act('folder.rename',{id:f.id,name});
        else if(confirmAction('Delete this folder? Conversations will remain available.')) await act('folder.delete',{id:f.id});
      }); label.append(manage); }
    root.append(label);
    for (const c of list) {
      const b=el('button','chatrow'+(state.active_id===c.id?' active':''),c.name);
      b.title=c.name; b.onclick=()=>run(async()=>{ await act('chat.select',{id:c.id}); switchPage('chat'); }); root.append(b);
    }
  }
}
function renderChat() {
  const c=state.current||{};
  $('conversationTitle').textContent=c.name||'Conversation';
  $('chatProject').value=c.project||'';
  $('chatTiming').textContent=`${(c.items||[]).length} saved entries · ${c.updated_at?fmtDate(c.updated_at):'Local history'}`;
  const view=c.view_mode==='thinking'?'thinking':'chat';
  $('cleanView').classList.toggle('on',view==='chat');
  $('thinkingView').classList.toggle('on',view==='thinking');
  $('cleanView').setAttribute('aria-pressed',String(view==='chat'));
  $('thinkingView').setAttribute('aria-pressed',String(view==='thinking'));
  $('captureThinking').checked=Boolean(c.save_thinking);
  const ts=state.session_time||{};
  const seconds=ts.conversation_elapsed_seconds;
  $('sessionElapsed').textContent=`Started: ${ts.first_user_local?fmtDate(ts.first_user_local):'No messages yet'} · Duration: ${seconds===null?'Unknown':humanDuration(seconds)} · Current local: ${ts.current_local?fmtDate(ts.current_local):'Unavailable'}`;
  $('selectedCount').textContent=state.selected_count||0;
  const ps=state.personas||{}; const choices=clear($('chatPersona'));
  for (const [id,p] of Object.entries(ps)) { const o=el('option','',p.name); o.value=id;choices.append(o); }
  choices.value=c.profile||'balanced';
  const pol=state.memory_policy||{};
  $('policyGlobal').checked=Boolean(pol.global);
  $('policyProject').checked=Boolean(pol.project);
  $('policyHandoff').checked=Boolean(pol.handoff);
  const scroll=$('messages');
  const active=ps[c.profile]||ps.balanced||{};
  // Preserve existing nodes while the backend polls for a pending reply.
  // Include all visible message fields: a new answer, an edited reply or a
  // changed thinking trace must trigger a rebuild, not leave stale content.
  const threadKey=JSON.stringify([state.active_id,view,active.agent_name||'',
    (c.items||[]).map(m=>[m.id,m.kind,m.role,m.content,m.timestamp,
      m.thinking,m.tool_activity,m.timing,m.attachments])]);
  if (renderedThreadKey!==threadKey) {
    const nearBottom=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<90;
    const oldScrollTop=scroll.scrollTop;
    // Snapshot states BEFORE removing DOM. Keys include conversation and
    // message identity so expanding a panel never opens the wrong reply.
    for (const panel of scroll.querySelectorAll('details.thinking-detail[data-thought-key]'))
      thinkingPanelState.set(panel.dataset.thoughtKey,panel.open);
    // Transfer live open/closed disclosure state into the final saved reply.
    const livePanelOpens=new Map();
    for(const panel of scroll.querySelectorAll('details.thinking-detail[data-live-round]'))
      livePanelOpens.set(Number(panel.dataset.liveRound),panel.open);
    const latestAssistant=(c.items||[]).filter(m=>m.role==='assistant').at(-1)?.id;
    clear(scroll);
    for (const msg of (c.items||[])) {
      const line=el('div','message');
      const icon=el('div','avatar'+(msg.role==='user'?' user':msg.kind==='reference'?' reference':''),msg.kind==='reference'?'▤':msg.role==='user'?'YOU':'AI');
      const main=el('div','message-content');const head=el('div','sender',msg.kind==='reference'?'Reference':msg.role==='assistant'?(msg.timing?.agent_name||active.agent_name||'Qwen'):'You');
      head.append(el('span','timestamp',fmtDate(msg.timestamp)));
      main.append(head,el('div','message-text',msg.content));
      if(view==='thinking' && msg.role==='assistant') {
        const thoughts=Array.isArray(msg.thinking)?msg.thinking:[];
        const activities=Array.isArray(msg.tool_activity)?msg.tool_activity:[];
        if(thoughts.length) {
          for(let index=0;index<thoughts.length;index++) {
            const part=thoughts[index];
            const panel=el('details','thinking-detail');
            const key=[state.active_id,msg.id,index,part.round].join(':');
            panel.dataset.thoughtKey=key;
            panel.open=thinkingPanelState.has(key)?thinkingPanelState.get(key)===true:
              (msg.id===latestAssistant && livePanelOpens.has(part.round) ? livePanelOpens.get(part.round) : false);
            panel.addEventListener('toggle',()=>{if(panel.isConnected) thinkingPanelState.set(key,panel.open);});
            const summary=el('summary','',`Model-exposed thinking · round ${part.round}`);
            const content=el('pre','',part.text||'');
            panel.append(summary,content);main.append(panel);
          }
        } else {
          main.append(el('div','thinking-note',c.save_thinking?'No thinking field was returned or captured for this reply.':'No recorded thinking for this reply. Enable capture for future replies.'));
        }
        if(activities.length) {
          const box=el('div','tool-activity');box.append(el('b','','Tool activity'));
          for(const a of activities)box.append(el('div','',`${a.tool||'tool'} · ${a.status||'unknown'}`));
          main.append(box);
        }
        if(msg.timing?.generation_seconds!==undefined) main.append(el('div','thinking-note',`Generation: ${msg.timing.generation_seconds}s · finished ${fmtDate(msg.timing.assistant_finished_at_utc||msg.timestamp)}`));
      }
      if (msg.attachments?.length) { const imgs=el('div','message-images');
        for (const file of msg.attachments) {
          if (!/^[a-f0-9]{32}\.(png|jpg|webp)$/.test(file)) continue;
          const i=el('img');i.src='/api/assets/'+file;i.alt='Attached image';i.loading='lazy';imgs.append(i);
        } main.append(imgs);
      }
      line.append(icon,main); scroll.append(line);
    }
    if (!(c.items||[]).length) scroll.append(el('div','empty','This conversation is empty. Type a message or attach a file to begin.'));
    scroll.scrollTop=nearBottom?scroll.scrollHeight:oldScrollTop;
    renderedThreadKey=threadKey;
  }
  renderUploads();
  const job=state.job, busy=job && ['running','ready'].includes(job.status);
  $('sendButton').disabled=Boolean(busy);
  $('sendButton').textContent=busy?'Generating response…':'Send ↗';
  if (job?.status==='failed') $('chatTiming').textContent='Model error: '+short(job.error,140);
  renderLiveProgress();
  $('tokenLabel').textContent='Context: '+state.selected_count+' selected entries · 128k capacity, when shown by Pi, means tokens (not /workspace disk space)';
}
// A live assistant bubble is fed by streaming Ollama frames through /api/job.
// Do not rebuild chat history or the expanded <details> elements on each poll.
function renderLiveProgress() {
  const job=state?.job||null;
  const view=(state?.current?.view_mode==='thinking')?'thinking':'chat';
  const active=job && ['running','ready'].includes(job.status) && job.conversation_id===state.active_id;
  const scroll=$('messages');
  let line=$('liveResponse');
  if (!active) { if (line)line.remove(); return; }
  const nearBottom=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<100;
  if (!line) {
    line=el('div','message streaming-message');line.id='liveResponse';
    const icon=el('div','avatar','AI');
    const main=el('div','message-content');
    const head=el('div','sender',(job.agent_name || state.personas?.[state.current?.profile]?.agent_name || 'Qwen')+' · live');head.id='liveHeader';
    const phase=el('div','thinking-note','Connecting to local model…');phase.id='liveStage';
    const preview=el('div','message-text','Waiting for first output…');preview.id='liveAnswer';
    const thoughtArea=el('div','live-thoughts');thoughtArea.id='liveThoughts';
    const activity=el('div','live-tools');activity.id='liveTools';
    main.append(head,phase,thoughtArea,activity,preview);
    line.append(icon,main);scroll.append(line);
  }
  const stage=job.stage||'Generating';
  $('liveStage').textContent=`${stage} · elapsed ${humanDuration(Math.floor(job.elapsed_seconds||0))}`;
  $('sendButton').textContent=`Generating · ${humanDuration(Math.floor(job.elapsed_seconds||0))}`;
  const parts=Array.isArray(job.rounds)?job.rounds:[];
  const text=parts.map(p=>p.content||'').join('');
  $('liveAnswer').textContent=text || (stage==='Thinking'?'Preparing response…':'Waiting for response text…');
  const thoughtArea=$('liveThoughts');
  thoughtArea.hidden=view!=='thinking';
  if(view==='thinking') {
    if(!job.capture_thinking) {
      let notice=thoughtArea.querySelector('.thinking-note');
      if (!notice) {clear(thoughtArea);notice=el('div','thinking-note');thoughtArea.append(notice);}
      notice.textContent='Live thinking was not requested for this response. Enable "Request & save model-exposed thinking" before sending the next message.';
    } else {
      if(thoughtArea.querySelector('.thinking-note'))clear(thoughtArea);
      for (const part of parts) {
        const round=part.round||1;
        let panel=Array.from(thoughtArea.querySelectorAll('details[data-live-round]'))
          .find(n=>Number(n.dataset.liveRound)===round);
        if (!panel) {
          panel=el('details','thinking-detail');panel.open=true;
          panel.dataset.liveRound=String(round);
          panel.append(el('summary','',`Live model-exposed thinking · round ${round}`),el('pre','','Waiting for thinking output…'));
          thoughtArea.append(panel);
        }
        const pre=panel.querySelector('pre');
        if(pre) {
          const atEnd=pre.scrollHeight-pre.scrollTop-pre.clientHeight<35;
          const fresh=part.thinking||'No thinking emitted during this round.';
          if(pre.textContent!==fresh){pre.textContent=fresh;if(atEnd)pre.scrollTop=pre.scrollHeight;}
        }
      }
    }
  }
  const toolArea=$('liveTools');
  toolArea.hidden=view!=='thinking';
  const activities=job.tool_activity||[];
  const toolKey=JSON.stringify(activities);
  if(toolArea.dataset.key!==toolKey) {
    clear(toolArea);toolArea.dataset.key=toolKey;
    if(activities.length) {
      toolArea.className='tool-activity';toolArea.append(el('b','','Live local tools'));
      for(const a of activities)toolArea.append(el('div','',`${a.tool||'tool'} · ${a.status||'unknown'}`));
    }
  }
  if(nearBottom)scroll.scrollTop=scroll.scrollHeight;
}

function renderUploads() {
  const n=clear($('attachedFiles'));
  for (let i=0;i<uploads.length;i++) {
    const pill=el('button','button small',uploads[i].name+' ×'); pill.title='Remove attachment';
    pill.onclick=()=>{uploads.splice(i,1);renderUploads();};n.append(pill);
  }
}
function renderContext() {
  const panel=clear($('admittedItems'));
  const selected=new Set(state.current?.selected_ids||[]);
  $('admittedCount').textContent=selected.size+' selected';
  async function sendChat() {
  const message = val('messageInput');

  // ==========================================
  // UPDATED IMPLICIT DATASET AUTO-TAGGER
  // ==========================================
  if (message.trim().startsWith("/tag")) {
    const parts = message.trim().split(/\s+/);
    if (parts.length < 2 || parts[1] === "") {
      notice('System error: Use format `/tag [Trigger_Word]` (e.g., /tag spaceballs)', true);
      return;
    }

    // Automatically imply the sandboxed mount folder path
    const folderPath = "/workspace/dataset";
    const triggerWord = message.trim().substring(4).trim(); // Grabs everything after "/tag"

    notice(`Initializing auto-captioner for sandboxed dataset using trigger: **${triggerWord}**...`);
    \$('messageInput').value = ''; // Clear the input box immediately

    fetch('http://127.0.0', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_path: folderPath, trigger_word: triggerWord })
    })
    .then(res => {
      if(!res.ok) throw new Error("Sandboxed dataset directory inaccessible or server down.");
      return res.json();
    })
    .then(data => {
      const intervalId = setInterval(() => {
        fetch('http://127.0.0')
        .then(res => res.json())
        .then(status => {
          if (status.status === "processing") {
            notice(`Dataset progress: [${status.processed}/${status.total}] Captioning: ${status.current_file}`);
          } else if (status.status === "completed") {
            notice(`🎉 Success! Processed all ${status.total} training images safely.`);
            clearInterval(intervalId);
            void refresh();
          } else if (status.status.startsWith("error")) {
            notice(`❌ Job Failed: ${status.status}`, true);
            clearInterval(intervalId);
          }
        });
      }, 2000);
    })
    .catch(err => notice(`❌ Connection Error: ${err.message}`, true));

    return; // Stop standard chatbot message generation loop completely
  }
  // ==========================================


  if(uploads.reduce((n,f)=>n+Math.ceil(f.size*4/3),0)>25*1024*1024)throw new Error('Combined attachments exceed the request limit. Send fewer files at once.');
  const files=await Promise.all(uploads.map(uploadBase64));
  if(!message.trim()&&!files.length)throw new Error('Enter a message or attach a file.');
  const response=await act('chat.send',{conversation_id:state.active_id,text:message,files},false);
  \$('messageInput').value='';uploads=[];await refresh();notice('Local model is working on your response.');return response;
}

  if (!panel.children.length) panel.append(el('p','hint','No current-conversation entries admitted. Cross-conversation selections are included in the total count.'));
  $('memoryCount').textContent=(state.memories||[]).length+' records';
  const mems=clear($('memoryItems'));
  for (const m of (state.memories||[])) {
    const box=el('div','item');box.append(el('b','',m.label||m.scope),el('p','',short(m.text,400)));
    const badge=el('div','meta',`${m.scope} · ${m.approved?'Approved':'Pending'} · ${m.enabled?'Enabled':'Disabled'}`);
    box.append(badge);
    const actions=el('div','buttons wrap');
    const approve=el('button','button small',m.approved?'Revoke approval':'Approve');
    approve.onclick=()=>run(()=>act('memory.modify',{id:m.id,approved:!m.approved}));
    const toggle=el('button','button small',m.enabled?'Disable':'Enable');toggle.onclick=()=>run(()=>act('memory.modify',{id:m.id,enabled:!m.enabled}));
    const edit=el('button','button small','Edit');edit.onclick=()=>run(async()=>{const text=prompt('Edit memory:',m.text);if(text!==null) await act('memory.modify',{id:m.id,text});});
    const del=el('button','button small danger','Delete');del.onclick=()=>run(async()=>{if(confirmAction('Delete this memory?'))await act('memory.delete',{id:m.id});});
    actions.append(approve,toggle,edit,del);box.append(actions);mems.append(box);
  }
  if (!mems.children.length) mems.append(el('p','hint','No saved memories. Add one for approval below.'));
  paintSettings('thoughts',state.thought_settings);
  const thoughts=clear($('thoughtItems'));
  const records=state.thoughts||[];
  $('thoughtCount').textContent=records.filter(t=>t.status==='active'&&t.pinned).length+' active pins';
  for(const thought of records) {
    const row=el('div','item');
    row.append(el('b','',thought.title),el('div','meta',`${thought.owner==='agent'?'AI-selected':'Your bookmark'} · ${thought.category} · ${thought.status} · ${fmtDate(thought.created_at)}`),el('p','',thought.detail));
    if(thought.why)row.append(el('p','meta','Why: '+thought.why));
    const actions=el('div','buttons wrap');
    const pin=el('button','button small',thought.pinned?'Unpin':'Pin');
    pin.onclick=()=>run(()=>act('thought.modify',{id:thought.id,pinned:!thought.pinned}));
    const change=el('button','button small',thought.status==='active'?'Resolve':'Reopen');
    change.onclick=()=>run(()=>act('thought.modify',{id:thought.id,status:thought.status==='active'?'resolved':'active'}));
    const edit=el('button','button small','Edit');edit.onclick=()=>run(async()=>{const title=prompt('Edit title:',thought.title);if(title===null)return;const detail=prompt('Edit idea:',thought.detail);if(detail!==null)await act('thought.modify',{id:thought.id,title,detail});});
    const del=el('button','button small danger','Delete');del.onclick=()=>run(async()=>{if(confirmAction('Permanently delete this pinned idea?'))await act('thought.delete',{id:thought.id});});
    actions.append(pin,change,edit,del);row.append(actions);thoughts.append(row);
  }
  if(!thoughts.children.length)thoughts.append(el('p','hint','No pinned ideas yet. You can pin one here, or permit Qwen to choose its own.'));
  const box=clear($('requests'));
  $('requestCount').textContent=(state.requests||[]).length+' pending';
  for (const req of state.requests||[]) {
    const row=el('div','item');row.append(el('b','',req.query),el('p','',`${req.item_ids?.length||0} candidate historical entries · ${req.from_model?'Suggested by Qwen':'Manual suggestion'}`));
    const buttons=el('div','buttons');
    const approve=el('button','button small','Approve candidates');approve.onclick=()=>run(()=>act('context.resolve',{request_id:req.id,decision:'approve',ids:req.item_ids||[]}));
    const dismiss=el('button','button small','Dismiss');dismiss.onclick=()=>run(()=>act('context.resolve',{request_id:req.id,decision:'dismiss',ids:[]}));
    buttons.append(approve,dismiss);row.append(buttons);box.append(row);
  }
  if (!box.children.length) box.append(el('p','hint','No pending context requests.'));
  renderSearch();
}
function renderSearch() {
  const tbody=clear($('searchResults'));$('contextStats').textContent=hits.length+' search results';
  if (!hits.length) {const tr=el('tr'),td=el('td','hint','Search your archive without loading it into Qwen.');td.colSpan=6;tr.append(td);tbody.append(tr);return;}
  for (const hit of hits) {
    const id=hit.item_id||hit.id,tr=el('tr');
    const checked=el('input');checked.type='checkbox';checked.checked=searchChecked.has(id);
    checked.onchange=()=>{if(checked.checked)searchChecked.add(id);else searchChecked.delete(id);};
    const td=el('td');td.append(checked);tr.append(td);
    for (const str of [hit.conversation_name||'',hit.role||'',fmtDate(hit.timestamp),short(hit.snippet||'',280)])tr.append(el('td','',str));
    const action=el('td');const preview=el('button','button small','Preview');preview.onclick=()=>showPreview('History search result',hit.snippet||'');action.append(preview);tr.append(action);tbody.append(tr);
  }
}
function showPreview(title,content){$('previewTitle').textContent=title;$('previewContent').textContent=content;$('previewModal').hidden=false;}
function renderStudio() {
  $('personaTimezone').value=state.timezone||'local';
  const select=clear($('personaSelect'));
  for(const [id,p] of Object.entries(state.personas||{})){const opt=el('option','',p.name);opt.value=id;select.append(opt);}
  if(!state.personas?.[editorId])editorId='balanced';
  select.value=editorId;renderProfileEditor();
  renderWebAccess();
  const perms=state.tool_permissions||{};
  paintSettings('tools',perms);
  $('auditEnabled').checked=Boolean(state.audit_enabled);$('auditRetention').value=state.audit_retention||30;
  const audits=clear($('auditRows'));
  for(const item of state.audit_recent||[]){const row=el('div','item');row.append(el('b','',item.event||'Event'),el('p','',fmtDate(item.timestamp||item.at)));audits.append(row);}
  if(!audits.children.length)audits.append(el('p','hint','No audit metadata stored.'));
  const mounts=clear($('mountList'));
  for(const m of state.mounts||[]) {
    const row=el('div','item'); row.append(el('span','wraptext',m.path+' ('+m.mode+')'));
    const actions=el('div','buttons');
    const toggle=el('button','button small',m.mode==='ro'?'Allow editing':'Read only');toggle.onclick=()=>run(async()=>{
      const mode=m.mode==='ro'?'rw':'ro';if(mode==='rw'&&!confirmAction('Grant Pi WRITE access? It could modify or delete files in this directory.'))return;
      await act('mount.mode',{id:m.id,mode});
    });
    const del=el('button','button small danger','Remove');del.onclick=()=>run(()=>act('mount.remove',{id:m.id}));actions.append(toggle,del);row.append(actions);mounts.append(row);
  }
  if (!mounts.children.length) mounts.append(el('p','hint','Only the default Pi workspace is shared.'));
  $('calendarPending').replaceChildren(...(state.calendar_pending||[]).map(p=>{
    const row=el('div','item');row.append(el('b','',p.title),el('p','',fmtDate(p.start_utc)));
    for(const [label,approval] of [['Approve',true],['Dismiss',false]]){
      const b=el('button','button small',label);b.onclick=()=>run(async()=>{await act('calendar.resolve',{id:p.id,approve:approval});await loadCalendar();});row.append(b);
    } return row;
  }));
}
function renderWebAccess() {
  const mode=state.web_mode||'offline';
  $('webMode').value=webModeDraft ?? mode;
  $('webStatus').textContent=mode==='offline'
    ? 'Offline: webpage proposals and retrieval are denied.'
    : 'Web Access: approved pages and searches can be retrieved; downloads and installation remain blocked.';
  const provider=state.web_search_provider||{};
  if(webProviderDraft===null){$('webSearchProvider').value=provider.provider||'bing_rss';$('webSearchEndpoint').value=provider.endpoint||'';}
  $('webProviderStatus').textContent='Selected: '+(provider.available?.[provider.provider]||provider.provider||'Bing RSS')+' · Search requests retain their original provider.';
  const jobs=state.web_jobs||[];
  const jobsByRequest=new Map(jobs.map(j=>[j.request_id,j]));
  const jobList=clear($('webJobs'));
  for(const job of jobs.slice(0,12)){
    const pane=el('div','item');pane.append(el('b','',job.kind+' retrieval · '+job.status),
      el('p','hint',job.stage+' · '+job.elapsed_seconds+'s'+(job.error?' · '+job.error:'')));
    if(job.status==='running'||job.status==='queued'){
      const cancel=el('button','button small danger','Cancel retrieval');
      cancel.onclick=()=>run(()=>act('web.cancel_fetch',{conversation_id:state.active_id,job_id:job.id}));
      pane.append(cancel);
    }
    jobList.append(pane);
  }
  const rows=state.web_requests||[];
  $('webRequestCount').textContent=rows.length+' requests';
  const activePending=new Set(rows.filter(x=>x.status==='pending').map(x=>x.id));
  for(const id of webBatchSelection)if(!activePending.has(id))webBatchSelection.delete(id);
  const list=clear($('webRequests'));
  for(const item of rows) {
    const panel=el('div','item');
    if(item.status==='pending') {
      const line=el('label','checks');
      const check=el('input');check.type='checkbox';check.checked=webBatchSelection.has(item.id);
      check.setAttribute('aria-label','Select request for batch review');
      check.onchange=()=>{if(check.checked)webBatchSelection.add(item.id);else webBatchSelection.delete(item.id);};
      line.append(check,el('span','hint','Select for known-request batch'));panel.append(line);
    }
    const target=item.kind==='search' ? `Search proposal: ${item.query}` : item.url;
    panel.append(el('b','wraptext',target),el('p','hint',
      `${item.status} · ${item.purpose||'No reason supplied'} · ${fmtDate(item.created_at)}${item.kind==='search'?' · provider: '+(item.search_provider?.provider||'legacy Bing RSS'):''}${item.grant_id?' · approved by grant':''}`));
    const btns=el('div','buttons wrap');
    if(item.status==='pending') {
      const approve=el('button','button small',item.kind==='search'?'Approve search proposal':'Approve exact URL');
      approve.onclick=()=>run(()=>act('web.decide',{conversation_id:state.active_id,request_id:item.id,approve:true}));
      const deny=el('button','button small danger','Deny');
      deny.onclick=()=>run(()=>act('web.decide',{conversation_id:state.active_id,request_id:item.id,approve:false}));
      btns.append(approve,deny);
    } else if(item.status==='approved') {
      {
        const read=el('button','button small','Retrieve approved '+(item.kind==='search'?'search':'page'));
        read.onclick=()=>run(async()=>{await act('web.fetch_async',{conversation_id:state.active_id,request_id:item.id});notice('Approved retrieval queued. You can continue using the interface.');});btns.append(read);
      }
      const revoke=el('button','button small danger','Revoke request');
      revoke.onclick=()=>run(()=>act('web.revoke',{conversation_id:state.active_id,request_id:item.id}));
      btns.append(revoke);
    } else if(item.status==='queued'||item.status==='fetching') {
      panel.append(el('p','hint','Background retrieval '+(jobsByRequest.get(item.id)?.status||item.status)+' — see queue below.'));
    } else if(item.status==='error' && item.result?.error) {
      panel.append(el('p','hint','Retrieval failed: '+item.result.error));
    } else if(item.status==='fetched' && item.result?.preview) {
      const preview=el('button','button small','Preview '+(item.kind==='search'?'search results':'fetched text'));
      preview.onclick=()=>showPreview('Retrieved source '+(item.result.source_id||'')+' — untrusted reference',item.result.preview);btns.append(preview);
      if(item.result.source_id){panel.append(el('p','hint','Source: '+item.result.source_id+' · '+fmtDate(item.result.retrieved_at_utc)+' · SHA256: '+item.result.sha256?.slice(0,16)+'…'));}
      if(item.kind==='search')for(const hit of (item.result.hits||[])){
        const follow=el('button','button small','Request result '+hit.index+': '+hit.title.slice(0,36));
        follow.title=hit.url;
        follow.onclick=()=>run(()=>act('web.follow',{conversation_id:state.active_id,search_request_id:item.id,index:hit.index}));
        btns.append(follow);
      }
    }
    panel.append(btns);list.append(panel);
  }
  if(!rows.length)list.append(el('p','hint','No requests in this conversation.'));
  const grants=clear($('webGrants'));
  for(const g of state.web_grants||[]) {
    const card=el('div','item');
    const summary=g.scope==='domain' ? `Exact domain: ${g.domain} (this conversation)` :
      g.scope==='broad'?'All conversations, read-only':'This conversation';
    card.append(el('b','wraptext',summary),el('p','hint',
      `${g.status} · ${g.kinds.join(' + ')} · ${g.used}/${g.quota} requests · expires ${fmtDate(g.expires_at)}`));
    const buttons=el('div','buttons wrap');
    for(const decision of (g.status==='active'?['pause','revoke']:g.status==='paused'?['resume','revoke']:[])) {
      const button=el('button','button small'+(decision==='revoke'?' danger':''),decision[0].toUpperCase()+decision.slice(1));
      button.onclick=()=>run(()=>act('web.grant_action',{conversation_id:state.active_id,grant_id:g.id,decision}));
      buttons.append(button);
    }
    card.append(buttons);grants.append(card);
  }
  if(!(state.web_grants||[]).length)grants.append(el('p','hint','No future-access grants.'));
}
function renderProfileEditor() {
  const p=state.personas?.[editorId]; if(!p)return;
  $('personaDescription').textContent=p.description||'';
  $('editorMode').textContent=editorId.startsWith('user-')?'Custom editable profile':'Preset · clone to edit';
  const fields={pName:'name',pAgent:'agent_name',pUser:'user_name',pGreeting:'greeting',pDescription:'description',pInstructions:'instructions',pInterests:'interests'};
  for(const [id,key] of Object.entries(fields))$(id).value=p[key]||'';
  $('pHumorStyle').value=p.humor.style;$('pHumor').value=p.humor.intensity;$('pInitiative').value=p.humor.initiative;
  $('pRetrieval').value=p.retrieval;$('pGapMode').value=p.time.gap_mode;$('pMinGap').value=p.time.minimum_gap_minutes;
  $('pTime').checked=p.time.enabled;$('pGap').checked=p.time.gap_tracking;$('pSchedule').checked=p.time.schedule_personality;$('pRoleplay').checked=p.roleplay;
}
function readProfile() {
  const p=structuredClone(state.personas[editorId]);
  for(const [id,key] of Object.entries({pName:'name',pAgent:'agent_name',pUser:'user_name',pGreeting:'greeting',pDescription:'description',pInstructions:'instructions',pInterests:'interests'}))p[key]=val(id);
  p.humor={intensity:Number(val('pHumor')),initiative:Number(val('pInitiative')),style:val('pHumorStyle')};
  p.time={enabled:flag('pTime'),gap_tracking:flag('pGap'),schedule_personality:flag('pSchedule'),gap_mode:val('pGapMode'),minimum_gap_minutes:Number(val('pMinGap'))};
  p.retrieval=val('pRetrieval');p.roleplay=flag('pRoleplay');return p;
}
async function loadCalendar() {
  const data=await request(`/api/calendar?year=${year}&month=${month}`);
  const day0=new Date(year,month-1,1),days=new Date(year,month,0).getDate(),offset=day0.getDay();
  $('calendarTitle').textContent=day0.toLocaleDateString(undefined,{month:'long',year:'numeric'});
  const grid=clear($('calendarGrid'));
  for(const label of ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'])grid.append(el('span','',label));
  for(let i=0;i<offset;i++)grid.append(el('span','other',''));
  const events=data.events||[];
  for(let day=1;day<=days;day++) {
    const key=`${year}-${String(month).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
    const b=el('button',events.some(e=>e.start_utc?.startsWith(key))?'hasevents':'',day);
    b.onclick=()=>{$('eventStart').value=key+'T12:00';};grid.append(b);
  }
  const list=clear($('calendarEvents'));
  for(const e of events){const row=el('div','item');row.append(el('b','',e.title),el('p','',fmtDate(e.start_utc)));
    const del=el('button','button small danger','Delete');del.onclick=()=>run(async()=>{if(confirmAction('Delete calendar event?')){await act('calendar.delete',{id:e.id});await loadCalendar();}});
    row.append(del);list.append(row);
  }
  if(!list.children.length)list.append(el('p','hint','No saved events during this period.'));
}
async function uploadBase64(file) {
  if(file.size>12*1024*1024)throw new Error(`${file.name} exceeds the 12 MiB upload limit. Open large files through Pi workspace tools.`);
  return await new Promise((resolve,reject)=>{
    const reader=new FileReader();reader.onerror=()=>reject(new Error('Cannot read '+file.name));
    reader.onload=()=>resolve({name:file.name,data:String(reader.result).split(',')[1]||''});reader.readAsDataURL(file);
  });
}
async function sendChat() {
  if(uploads.reduce((n,f)=>n+Math.ceil(f.size*4/3),0)>25*1024*1024)throw new Error('Combined attachments exceed the request limit. Send fewer files at once.');
  const message=val('messageInput'), files=await Promise.all(uploads.map(uploadBase64));
  if(!message.trim()&&!files.length)throw new Error('Enter a message or attach a file.');
  const response=await act('chat.send',{conversation_id:state.active_id,text:message,files},false);
  $('messageInput').value='';uploads=[];await refresh();notice('Local model is working on your response.');return response;
}
function startPolling() {
  if(pollTimer)return;
  pollTimer=setInterval(()=>run(async()=>{
    if(jobPollBusy)return;
    jobPollBusy=true;
    try {
      const data=await request('/api/job');
      if(!state)return;
      const previous=state.job;
      const current=data.job;
      // An old poll must not replace the job created by a newer send.
      if(previous?.id && current?.id!==previous.id)return;
      state.job=current;
      if(current && ['running','ready'].includes(current.status)) {
        renderLiveProgress();
      } else {
        await refresh();
        if(previous?.status==='running' && current?.status==='complete')notice('The local model answered.');
        else if(current?.status==='failed')notice('Local model error: '+short(current.error,180),true);
      }
    } finally { jobPollBusy=false; }
  }),350);
}
function stopPolling(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}
function selectedSearch(){return [...searchChecked];}
async function processAction(which) {
  if(!state)throw new Error('State not loaded yet.');
  const cid=state.active_id, selected=selectedSearch(), today=new Date();
  switch(which){
    case 'new-chat':{const name=prompt('Conversation name','New conversation');if(name!==null)await act('chat.new',{name:name||'New conversation'});return;}
    case 'new-folder':{const name=prompt('New folder name');if(name)await act('folder.new',{name});return;}
    case 'rename-chat':{const name=prompt('Rename conversation',state.current.name);if(name)await act('chat.rename',{id:cid,name});return;}
    case 'branch-chat':await act('chat.branch',{id:cid});return;
    case 'delete-chat':if(confirmAction('Delete this entire conversation and its saved entries?'))await act('chat.delete',{id:cid});return;
    case 'save-project':await act('chat.project',{id:cid,project:val('chatProject')});return;
    case 'send-chat':await sendChat();return;
    case 'clean-view':await act('chat.view',{conversation_id:cid,mode:'chat'});return;
    case 'erase-thinking':if(confirmAction('Erase all saved model-exposed thinking and tool summaries from this conversation? Final replies remain.'))await act('chat.clear_thinking',{conversation_id:cid});return;
    case 'thinking-view':await act('chat.view',{conversation_id:cid,mode:'thinking'});return;
    case 'inspect-context':{
      const preview=await act('context.inspector',{conversation_id:cid,draft:val('messageInput')},false);
      showPreview('Context Inspector · current draft (no model call)',JSON.stringify(preview,null,2));return;
    }
    case 'inspect-last':{
      const last=await act('context.inspector',{conversation_id:cid,mode:'last'},false);
      showPreview('Context Inspector · actual last send',JSON.stringify(last,null,2));return;
    }
    case 'check-clock':{
      const clock=await act('clock.check',{},false);
      showPreview('Local time & elapsed conversation timing',JSON.stringify(clock,null,2));return;
    }
    case 'pin-thought':{
      await act('thought.user_pin',{conversation_id:cid,title:val('newThoughtTitle'),detail:val('newThoughtDetail'),category:val('newThoughtCategory')});
      $('newThoughtTitle').value='';$('newThoughtDetail').value='';return;
    }
    case 'save-thought-settings':stageSettings('thoughts',false);await persistSettings('thoughts');return;
    case 'clear-images':uploads=[];renderUploads();return;
    case 'context-search':{const q=val('contextSearch').trim();hits=q?(await request('/api/search?q='+encodeURIComponent(q)+'&mode='+encodeURIComponent(val('searchMode')))).hits:[];searchChecked.clear();renderSearch();return;}
    case 'include-hits':case 'exclude-hits':if(!selected.length)throw new Error('Check search results first.');await act('context.set',{conversation_id:cid,ids:selected,include:which==='include-hits'});return;
    case 'expand-hits':{if(!selected.length)throw new Error('Check results to expand.');const r=await act('context.expand',{ids:selected,radius:2,related:true},false);hits=r.hits||[];searchChecked.clear();renderSearch();return;}
    case 'delete-hits':if(selected.length&&confirmAction('Permanently delete checked history entries?')){await act('context.delete',{ids:selected});hits=[];searchChecked.clear();}return;
    case 'rebuild-index':await act('context.rebuild');return;
    case 'exclude-all':if(confirmAction('Exclude all context currently selected for this conversation?'))await act('context.set',{conversation_id:cid,ids:state.current.selected_ids||[],include:false});return;
    case 'propose-context':await act('context.propose',{conversation_id:cid,query:val('suggestQuery')});return;
    case 'add-memory':{const text=val('newMemoryText').trim(),scope=val('newMemoryScope');if(!text)throw new Error('Enter a memory.');await act('memory.add',{text,scope,project:state.current.project||'',persona_id:state.current.profile||'balanced'});$('newMemoryText').value='';return;}
    case 'use-persona':await act('chat.profile',{id:cid,profile:editorId});return;
    case 'clone-persona':{const name=prompt('New custom profile name',state.personas[editorId]?.name+' copy');if(name){const r=await act('persona.clone',{source:editorId,name});editorId=r.id;await refresh();}return;}
    case 'delete-persona':if(editorId.startsWith('user-')&&confirmAction('Delete custom personality profile?')){await act('persona.delete',{id:editorId});editorId='balanced';}return;
    case 'save-persona':if(!editorId.startsWith('user-'))throw new Error('Clone a preset before editing it.');await act('persona.update',{id:editorId,profile:readProfile()});return;
    case 'save-timezone':await act('persona.timezone',{timezone:val('personaTimezone')});return;
    case 'apply-pi-identity':if(confirmAction('Write profile identity to workspace/AGENTS.md for Pi?'))await act('persona.apply_pi',{id:editorId});return;
    case 'calculate':{const r=await act('calculator.calculate',{expression:val('expression')},false);$('calcResult').textContent=r.result;return;}
    case 'save-permissions':stageSettings('tools',false);await persistSettings('tools');return;
    case 'save-web-mode':{
      const mode=webModeDraft ?? val('webMode');
      if(mode==='web'&&!confirmAction('Enable host-mediated webpage proposals and individually approved page reads? Ollama and Pi remain offline.'))return;
      await act('web.mode',{mode});webModeDraft=null;notice(mode==='web'?'Web Access profile enabled; no pages are approved yet.':'Offline profile restored; outstanding approvals expired.');return;
    }
    case 'web-save-provider':{
      const provider=val('webSearchProvider'),endpoint=provider==='searxng_json'?val('webSearchEndpoint').trim():null;
      await act('web.search_provider',{provider,endpoint});webProviderDraft=null;
      notice('Search provider saved. Existing proposals retain their original provider.');return;
    }
    case 'web-propose':{const url=val('webURL').trim();if(!url)throw new Error('Enter an exact public webpage URL.');
      await act('web.request',{conversation_id:state.active_id,url,purpose:val('webPurpose').trim()});
      $('webURL').value='';$('webPurpose').value='';return;}
    case 'web-search':{const query=val('webQuery').trim();if(!query)throw new Error('Enter a search phrase.');
      await act('web.search',{conversation_id:state.active_id,query,purpose:val('webPurpose').trim()});
      $('webQuery').value='';return;}
    case 'web-batch-approve':
    case 'web-batch-deny':{
      const approve=which==='web-batch-approve';const ids=[...webBatchSelection];
      if(!ids.length)throw new Error('Select pending requests in the inbox first.');
      if(!confirmAction((approve?'Approve':'Deny')+` ${ids.length} selected known requests? This does NOT allow future requests.`))return;
      await act('web.decide_batch',{conversation_id:state.active_id,request_ids:ids,approve});
      webBatchSelection.clear();return;
    }
    case 'web-grant':{
      const scope=val('webGrantScope');const kinds=val('webGrantKinds')==='both'?['page','search']:[val('webGrantKinds')];
      const minutes=Number(val('webGrantMinutes')),quota=Number(val('webGrantQuota'));
      const domain=scope==='domain'?val('webGrantDomain').trim():null;
      if(!Number.isInteger(minutes)||minutes<5||minutes>120||!Number.isInteger(quota)||quota<1||quota>50)
        throw new Error('Grant duration: 5–120 minutes; quota: 1–50 new requests.');
      if(scope==='domain'&&kinds.join(',')!=='page')throw new Error('Exact-domain grants cover webpage reads only.');
      if(!confirmAction(`Grant future ${kinds.join(' + ')} approval (${scope}) for up to ${quota} requests and ${minutes} minutes?`))return;
      if(scope==='broad'&&!confirmAction('Confirm BROAD read access for ALL conversations until expiry, revocation or restart. This cannot install or download software.'))return;
      await act('web.grant',{conversation_id:state.active_id,scope,kinds,minutes,quota,domain});
      notice('Bounded web permission granted. Revoke or pause it at any time.');return;
    }

    case 'prev-month':month--;if(month<1){month=12;year--;}await loadCalendar();return;
    case 'next-month':month++;if(month>12){month=1;year++;}await loadCalendar();return;
    case 'add-event':if(confirmAction('Add this event to your local calendar?')){await act('calendar.add',{title:val('eventTitle'),start:val('eventStart'),details:val('eventDetails')});await loadCalendar();}return;
    case 'save-audit':await act('audit.save',{enabled:flag('auditEnabled'),retention_days:Number(val('auditRetention'))});return;
    case 'clear-audit':if(confirmAction('Delete all audit logs? Conversations and calendar data will remain.'))await act('audit.clear');return;
    case 'add-mount':await act('mount.add',{path:val('mountPath')});$('mountPath').value='';return;
    case 'delete-all-history':if(confirmAction('Delete ALL saved history? This cannot be undone without a backup.')&&confirmAction('Final confirmation: delete all conversations?'))await act('history.clear');return;
    case 'close-preview':$('previewModal').hidden=true;return;
    case 'open-installer':notice('Launch Install.cmd in your project folder; this web GUI cannot start Windows programs remotely.');return;
    case 'verify-docker':$('verifyOutput').textContent='Run Run_Checks.cmd or use Verify isolation in the installer. Live verification from this GUI is not enabled.';return;
  }
  throw new Error('This button is not configured: '+which);
}
async function importFiles(id,action) {
  const files=[...($(id).files||[])];if(!files.length)return;
  if(action==='reference.import') {
    const data=await Promise.all(files.map(uploadBase64));
    await act('reference.import',{files:data});notice(files.length+' reference files imported; review selected context.');
  } else {
    if(files.length!==1)throw new Error('Select exactly one backup file.');
    await act(action,{file:await uploadBase64(files[0])});notice('Imported '+files[0].name);
  }
  $(id).value='';
}
async function exportFile(kind) {
  const qs=new URLSearchParams({kind});if(kind==='persona')qs.set('id',editorId);
  const res=await fetch('/api/export?'+qs,{headers:{'X-LocalAI-Token':token},cache:'no-store'});
  if(!res.ok)throw new Error((await res.json()).error||'Export failed');
  const blob=await res.blob(), url=URL.createObjectURL(blob), link=document.createElement('a');
  link.href=url;link.download=`localai-${kind}-${new Date().toISOString().slice(0,10)}.${kind==='history'?'zip':'json'}`;
  if(kind==='selected-pi')link.download='approved-context-for-pi.json';
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);
}
function setupEvents() {
  document.addEventListener('click', e=>{
    const b=e.target.closest('[data-action],button[data-page],button[data-studio],button[data-export]');if(!b)return;
    if(b.dataset.page){switchPage(b.dataset.page);return;}
    if(b.dataset.studio){switchStudio(b.dataset.studio);return;}
    if(b.dataset.export){void run(()=>exportFile(b.dataset.export));return;}
    if(b.dataset.action)void run(()=>processAction(b.dataset.action));
  });
  $('imageFiles').addEventListener('change',e=>{
    const next=[...e.target.files];if(uploads.length+next.length>10)return notice('Attach up to ten files per message.',true);
    uploads.push(...next);renderUploads();e.target.value='';
  });
  const dropZone=document.querySelector('.compose');
  dropZone.addEventListener('dragover',e=>{e.preventDefault();dropZone.classList.add('drag-ready');});
  dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('drag-ready'));
  dropZone.addEventListener('drop',e=>{e.preventDefault();dropZone.classList.remove('drag-ready');
    const files=[...(e.dataTransfer?.files||[])];if(uploads.length+files.length>10) {notice('Too many files; maximum ten.',true);return;}
    uploads.push(...files);renderUploads();
  });
  $('messageInput').addEventListener('keydown',e=>{if(e.key==='Enter'&&e.ctrlKey){e.preventDefault();void run(sendChat);}});
  $('chatFilter').addEventListener('input',()=>{if(state)renderSidebar();});
  $('chatPersona').addEventListener('change',()=>run(()=>act('chat.profile',{id:state.active_id,profile:val('chatPersona')})));
  $('captureThinking').addEventListener('change',()=>run(()=>act('chat.thinking',{conversation_id:state.active_id,enabled:flag('captureThinking')})));
  $('webSearchProvider').addEventListener('change',()=>{webProviderDraft=true;});
  $('webSearchEndpoint').addEventListener('input',()=>{webProviderDraft=true;});
  $('webMode').addEventListener('change',()=>{webModeDraft=val('webMode');$('webStatus').textContent='Profile selected — click Apply execution profile to confirm.';});
  // All permissions can be edited during inference; polling never rolls back
  // either settings panel. A successful toggle is committed immediately.
  for(const id of ['allowAgentPins','recallPinnedThoughts'])
    $(id).addEventListener('change',()=>stageSettings('thoughts'));
  $('maxThoughtPins').addEventListener('input',()=>stageSettings('thoughts',false));
  $('maxThoughtPins').addEventListener('change',()=>stageSettings('thoughts'));
  for(const [id] of TOOL_FIELDS)
    $(id).addEventListener('change',()=>stageSettings('tools'));
  $('personaSelect').addEventListener('change',()=>{editorId=val('personaSelect');renderProfileEditor();});
  for(const id of ['policyGlobal','policyProject','policyHandoff'])$(id).addEventListener('change',()=>run(()=>act('context.policy',{conversation_id:state.active_id,policy:{global:flag('policyGlobal'),project:flag('policyProject'),handoff:flag('policyHandoff')}})));
  for(const [id,action] of [['importHistory','history.import'],['importReferences','reference.import'],['importMemory','memory.import'],['importThoughts','thought.import'],['importPersona','persona.import'],['importCalendar','calendar.import']])$(id).addEventListener('change',()=>run(()=>importFiles(id,action)));
}
setupEvents();void run(refresh);
