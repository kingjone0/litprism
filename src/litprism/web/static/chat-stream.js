/* Live, untrusted previews only. Final messages still come from the checked result API. */
const chatStreams = new Map(), chatStreamOwners = new Map();

function renderChatStream(state) {
  if (chatStreams.get(state.id) !== state || chatStreamOwners.get(state.convId) !== state.id
      || S.activeView !== 'chat' || S.activeConv !== state.convId) return;
  const host = $('#chatStream');
  if (!host || state.status === 'completed') return;
  const follow = host.scrollHeight-host.scrollTop-host.clientHeight<64;
  let card = $('#chatLiveDraft');
  if (card?.dataset.run !== state.id) {card?.remove();card=null;}
  if (!card) {
    card=document.createElement('article');card.id='chatLiveDraft';card.className='chat-live-draft';
    card.dataset.run=state.id;card.setAttribute('aria-label','生成中的回复');card.setAttribute('aria-live','off');
    card.innerHTML=`<div class="task-progress task-progress-inline chat-live-controls"><span role="status" data-live-state></span><button class="btn sm" type="button" data-live-cancel>停止生成</button></div>
      <details class="chat-reasoning" data-live-thought hidden><summary>思考过程</summary><pre></pre></details>
      <div class="reading-text" data-live-text></div><p class="source-intro" data-live-error role="status" hidden></p>
      <button type="button" class="btn sm" data-live-reconnect hidden>重新连接</button>`;
    host.append(card);
    $('summary',card).onclick=()=>{state.manualReasoning=true;};
  }
  $('#pending')?.remove();
  const stopped=state.terminal || !state.active;
  card.setAttribute('aria-label',stopped?'未完成的回复草稿':'生成中的回复');
  $('.chat-live-controls',card).classList.toggle('task-progress',!stopped);
  $('[data-live-state]',card).textContent=stopped ? '生成已停止 · 内容未校验'
    : state.cancelling ? '等待停止生成' : state.stage==='checking' ? '正在核对来源'
    : state.stage==='waiting' ? '正在等待模型回复' : '生成中 · 内容待校验';
  const cancel=$('[data-live-cancel]',card);cancel.hidden=stopped;
  cancel.disabled=!state.task_id || state.cancelRequested || state.cancelling;
  cancel.onclick=async()=>{
    state.cancelRequested=true;renderChatStream(state);
    try {await api('/api/tasks/'+encodeURIComponent(state.task_id)+'/cancel',{method:'POST'});}
    catch(error){state.cancelRequested=false;state.error=error.message;renderChatStream(state);}
  };
  const thought=$('[data-live-thought]',card);thought.hidden=!state.reasoning;
  $('pre',thought).textContent=state.reasoning;
  if (!state.manualReasoning) thought.open=!!state.reasoning&&!state.text;
  $('[data-live-text]',card).textContent=state.text;
  const error=$('[data-live-error]',card);error.hidden=!state.error;error.textContent=state.error||'';
  const retry=$('[data-live-reconnect]',card);retry.hidden=!state.error || state.terminal;
  retry.onclick=()=>observeChatStream(state.id,state.convId,true);
  if(follow)host.scrollTop=host.scrollHeight;
}

function queueChatStream(state) {
  if (state.frame) return;
  state.frame=requestAnimationFrame(()=>{state.frame=null;renderChatStream(state);});
}

function clearChatStream(rid) {
  const state=chatStreams.get(rid);
  if (!state) return;
  state.controller?.abort();chatStreams.delete(rid);
  if (chatStreamOwners.get(state.convId)===rid) chatStreamOwners.delete(state.convId);
  if ($('#chatLiveDraft')?.dataset.run===rid) $('#chatLiveDraft').remove();
}

async function readChatStream(state,controller) {
  await ENV_READY;
  const env=apiEnv(),headers={Accept:'text/event-stream'};
  if(env.token)headers['X-LitPrism-Token']=env.token;
  const response=await fetch((env.base||'')+'/api/chat-runs/'+encodeURIComponent(state.id)+'/events',
    {headers,signal:controller.signal,cache:'no-store'});
  if(!response.ok || !response.body)throw new Error('增量连接不可用，后台请求不会因此重发。');
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
  try {
    for(;;) {
      const part=await reader.read();if(controller.signal.aborted)return;
      buffer+=decoder.decode(part.value||new Uint8Array(),{stream:!part.done});
      let boundary;
      while((boundary=/\r?\n\r?\n/.exec(buffer))) {
        const frame=buffer.slice(0,boundary.index);buffer=buffer.slice(boundary.index+boundary[0].length);
        const data=frame.split(/\r?\n/).filter(line=>line.startsWith('data:')).map(line=>line.slice(5).replace(/^ /,'')).join('\n');
        if(!data)continue;
        const event=JSON.parse(data);
        if(event.run_id!==state.id)throw new Error('增量请求标识不一致。');
        if(event.reset){state.text=event.text||'';state.reasoning=event.reasoning||'';}
        else {state.text+=event.text_delta||'';state.reasoning+=event.reasoning_delta||'';}
        Object.assign(state,{status:event.status,active:event.active,stage:event.stage,terminal:event.terminal,
          task_id:event.task_id,cancelling:event.cancelling,error:null});
        queueChatStream(state);
        if(event.terminal) {
          if(event.status==='completed') {
            clearChatStream(state.id);
            if(!S.sending&&S.activeView==='chat'&&S.activeConv===state.convId)await loadChat();
          }
          return;
        }
      }
      if(part.done)throw new Error('增量连接已断开，后台可能仍在处理。');
    }
  } finally {await reader.cancel().catch(()=>{});reader.releaseLock();}
}

function observeChatStream(rid,convId,reconnect=false) {
  let state=chatStreams.get(rid);
  chatStreamOwners.set(convId,rid);
  if(state?.running&&!reconnect){queueChatStream(state);return state;}
  if(state?.terminal&&!reconnect){queueChatStream(state);return state;}
  if(!state) {
    state={id:rid,convId,text:'',reasoning:'',status:'queued',stage:'waiting',active:true};
    chatStreams.set(rid,state);
    for(const [id,old]of chatStreams)if(chatStreams.size>64&&old.terminal)clearChatStream(id);
  }
  state.controller?.abort();state.controller=new AbortController();
  const controller=state.controller;state.running=true;state.error=null;state.terminal=false;
  queueChatStream(state);
  (async()=>{
    try {
      for(let attempt=0;attempt<3;attempt++) {
        try{await readChatStream(state,controller);return;}
        catch(error){
          if(controller.signal.aborted)return;
          if(attempt===2){state.error=error.message;queueChatStream(state);return;}
          await new Promise(resolve=>setTimeout(resolve,200*(attempt+1)));
        }
      }
    } finally {if(state.controller===controller)state.running=false;}
  })();
  return state;
}
