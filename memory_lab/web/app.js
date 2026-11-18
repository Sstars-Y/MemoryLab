"use strict";
const $ = id => document.getElementById(id);
let csrf = "", state = null, currentSession = "", pendingFiles = [], activeRequest = "", lastRequest = "", firstOrdinal = null, searchCursor = 0;
const titles = {chat:"对话",search:"历史与来源",episodes:"经历整理",recycle:"回收站",data:"导出与快照",settings:"设置"};
const formatTime = value => new Date(value*1000).toLocaleString("zh-CN", {hour12:false});
const idempotency = () => "web_"+crypto.randomUUID().replaceAll("-", "");
function node(tag, text, className) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(className)n.className=className; return n; }
function status(text, error=false) { $("status").textContent=text; $("status").classList.toggle("error",error); }
async function api(path, data, options={}) {
  const init={credentials:"same-origin",...options,headers:{...(options.headers||{})}};
  if(data!==undefined){init.method="POST";init.headers["Content-Type"]="application/json";init.headers["X-CSRF-Token"]=csrf;init.body=JSON.stringify(data);}
  const response=await fetch("/api"+path,init); let result;
  try{result=await response.json();}catch{throw Error("服务响应无效，请检查本地服务是否运行");}
  if(!response.ok)throw Error(result.error||(response.status===404?"当前后端没有此接口。请在启动窗口按 Ctrl+C，重新双击启动实验版.cmd，再按 Ctrl+F5 刷新网页。":`操作失败（HTTP ${response.status}）`));return result;
}
function handle(action){return async event=>{try{await action(event);}catch(error){status(error.message,true);}};}
function button(text, action, className=""){const b=node("button",text,className);b.type="button";b.addEventListener("click",handle(action));return b;}
function details(title, content){$("detail-title").textContent=title;$("detail-body").replaceChildren(content);if(!$("detail").open)$("detail").showModal();}
$("close-detail").onclick=()=>$("detail").close();
async function refresh(){state=await api("/state");csrf=state.csrf;$("provider-label").textContent=state.provider;$("save-label").textContent=state.stats.recording?"保存已开启":"临时交流 · 暂停保存";$("recording").textContent=state.stats.recording?"暂停保存":"重新开启";$("recording").setAttribute("aria-checked",String(state.stats.recording));$("sessions").replaceChildren(...state.sessions.map(session=>{const b=button(session.title,async()=>{currentSession=session.id;sessionStorage.setItem("memorylab_session",currentSession);await loadHistory();await refresh();});b.classList.toggle("current",session.id===currentSession);return b;}));$("diagnostics").textContent=JSON.stringify(state.stats,null,2)+(state.worker_error?"\n后台最近状态："+state.worker_error:"");$("embedding-status").textContent=state.embedding;renderConnectionState();}
async function ensureSession(){if(!currentSession||!state.sessions.some(s=>s.id===currentSession)){currentSession=(await api("/sessions",{title:"对话 · "+new Date().toLocaleString("zh-CN")})).id;sessionStorage.setItem("memorylab_session",currentSession);await refresh();}}
async function loadHistory(appendOlder=false){if(!currentSession)return;const result=await api(`/sessions/${encodeURIComponent(currentSession)}${appendOlder&&firstOrdinal?"?before="+firstOrdinal:""}`);const rows=result.messages;const container=$("conversation");if(!appendOlder)container.replaceChildren();if(rows.length){firstOrdinal=appendOlder?Math.min(firstOrdinal,rows[0].ordinal):rows[0].ordinal;const fragment=document.createDocumentFragment();rows.forEach(row=>fragment.append(message(row)));if(appendOlder)container.prepend(fragment);else container.append(fragment);}for(const row of result.temporary)container.append(message(row));if(!container.children.length){const empty=node("div",undefined,"empty");empty.append(node("h2","从这一次交流开始"),node("p","原话会保留，回忆可以核对。先说点什么吧。"));container.append(empty);}$("older").classList.toggle("hidden",!firstOrdinal||firstOrdinal<=1||rows.length<80);if(!appendOlder)container.scrollTop=container.scrollHeight;}
function message(row, recycle=false){const card=node("article",undefined,`message ${row.role}${row.temporary?" temporary":""}`);const meta=node("div",undefined,"message-meta");meta.append(node("span",(row.role==="user"?"你":"助手")+" · "+(row.temporary?"临时交流":recycle?"回收站":"已保存")),node("time",formatTime(row.created)));card.append(meta,node("div",row.body,"message-body"));for(const revision of row.corrections||[])card.append(node("div",`${revision.kind==="error"?"纠正":"变化 / 修订"}：${revision.correction}${revision.event_time?" · "+revision.event_time:""}`,"correction"));for(const attachment of row.attachments||[]){const media=node("div",undefined,"media");media.append(node("small",`【${attachment.mime.startsWith("image/")?"图片":"语音"}】@${attachment.id}`));if(!row.temporary){const url=`/api/${recycle?"recycle-attachments":"attachments"}/${encodeURIComponent(attachment.id)}`;if(attachment.mime.startsWith("image/")){const image=document.createElement("img");image.src=url;image.alt="消息图片附件";image.loading="lazy";media.append(image);}else{const audio=document.createElement("audio");audio.controls=true;audio.preload="none";audio.src=url;media.append(audio);}if(!recycle)media.append(button("识别 / 重试",async()=>{status("正在识别，将向配置的识别服务发送此附件……");await api(`/attachments/${attachment.id}/recognize`,{});status("识别结果已保存");await loadHistory();}));}media.append(node("small",attachment.recognition?"识别文字（可能有误）："+attachment.recognition:"尚未识别；未将本机路径当作媒体输入。"));card.append(media);}if(!row.temporary&&!recycle){const tools=node("div",undefined,"message-tools");tools.append(button("原文与前后文",()=>showContext(row.id)),button("纠正 / 变化",()=>showCorrection(row)),button("遗忘…",()=>showDelete(row.id)));if(row.role==="assistant")tools.append(button("本次历史来源",()=>showSources(`/messages/${row.id}/sources`)));card.append(tools);}return card;}
async function showContext(mid){const result=await api(`/messages/${mid}`);const wrap=node("div");result.messages.forEach(row=>wrap.append(message(row)));details("原文与相邻记录",wrap);}
function showCorrection(row){const form=node("form");const kind=document.createElement("select");for(const [value,label] of [["error","原记录有误"],["change","之后发生变化"],["transcription","文字 / 识别修订"]]){const option=node("option",label);option.value=value;kind.append(option);}const text=document.createElement("textarea");text.rows=5;text.required=true;text.placeholder="说明正确内容及依据；原话会继续保留。";const date=document.createElement("input");date.type="date";form.append(node("p",row.body),kind,text,node("label","已知事件时间（可留空）"),date,button("保存纠正",async()=>{await api("/corrections",{message_id:row.id,text:text.value,kind:kind.value,event_time:date.value||null});$("detail").close();status("纠正已保存，相关派生记忆将重建");await loadHistory();await refresh();},"primary"));details("纠正与历史变化",form);}
async function showDelete(mid){const result=await api(`/messages/${mid}/delete-preview`);const wrap=node("div");wrap.append(node("p","默认选中本轮用户发言及对应完成回答。可以调整选择；不会扩展删除其他对话。"));const selected=[];for(const row of result.messages){const label=node("label",undefined,"delete-option");const checkbox=document.createElement("input");checkbox.type="checkbox";checkbox.checked=true;selected.push([checkbox,row.id]);label.append(checkbox,node("span",(row.role==="user"?"你：":"助手：")+row.body));wrap.append(label);}wrap.append(button("将所选记录移入 7 天回收站",async()=>{const ids=selected.filter(([c])=>c.checked).map(([,id])=>id);await api("/forget",{message_ids:ids});$("detail").close();status("所选记录已退出日常记忆");await loadHistory();await refresh();},"danger"));details("确认遗忘范围",wrap);}
async function showSources(path){const result=await api(path);const wrap=node("div");wrap.append(node("p",result.notice));for(const source of result.sources){const card=node("div",undefined,"source-card");card.append(node("p",`${source.message_id} · 版本 ${source.revision}${source.changed?" · 来源后来已变化":""}`,"mono"));for(const media of source.media||[])card.append(node("p",`${media.attachment_id}：实际发送 ${media.sent}`,"muted"));if(source.message)card.append(message(source.message));else card.append(node("p","来源当前不可用或属于临时交流。"));wrap.append(card);}details("实际提供给模型的历史来源",wrap);}
async function poll(requestId){activeRequest=requestId;lastRequest=requestId;sessionStorage.setItem("memorylab_request",JSON.stringify({id:requestId,session:currentSession}));$("stop").classList.remove("hidden");$("send").disabled=true;try{while(activeRequest===requestId){const result=await api(`/requests/${requestId}?session_id=${encodeURIComponent(currentSession)}`);$("draft").textContent=result.draft?"生成中 · 未保存\n"+result.draft:"";$("draft").classList.toggle("hidden",!result.draft);if(!["pending","running"].includes(result.status)){status(result.status==="completed"?(result.recording?"回答已完成并保存":"回答已完成，仅用于临时交流"):(result.error||"生成已停止，草稿已丢弃"),result.status==="failed");await loadHistory();await refresh();if(result.status==="failed"||result.status==="cancelled"||result.status==="interrupted"){const retry=button("重试这条用户消息",async()=>{const retried=await api("/chat",{session_id:currentSession,text:"",retry_of:requestId,idempotency_key:idempotency()});retry.remove();await poll(retried.request_id);});$("conversation").append(retry);}break;}await new Promise(resolve=>setTimeout(resolve,180));}}finally{activeRequest="";sessionStorage.removeItem("memorylab_request");$("draft").classList.add("hidden");$("stop").classList.add("hidden");$("send").disabled=false;}}
$("composer").addEventListener("submit",handle(async event=>{event.preventDefault();await ensureSession();const text=$("message").value;const result=await api("/chat",{session_id:currentSession,text,attachments:pendingFiles.map(f=>f.id),idempotency_key:idempotency()});$("message").value="";pendingFiles=[];$("pending-files").textContent="";await loadHistory();await poll(result.request_id);}));
$("message").addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.shiftKey&&!event.isComposing){event.preventDefault();$("composer").requestSubmit();}});
$("files").addEventListener("change",handle(async()=>{await ensureSession();for(const file of $("files").files){if(pendingFiles.length>=6)throw Error("每条消息最多 6 个附件");const response=await fetch(`/api/uploads?session_id=${encodeURIComponent(currentSession)}&filename=${encodeURIComponent(file.name)}`,{method:"POST",credentials:"same-origin",headers:{"X-CSRF-Token":csrf},body:file});const result=await response.json();if(!response.ok)throw Error(result.error);pendingFiles.push({...result,name:file.name});}$("files").value="";$("pending-files").textContent=pendingFiles.map(f=>f.name+" · 临时待发送").join("，");}));
$("stop").onclick=handle(async()=>{if(activeRequest)await api(`/requests/${activeRequest}/stop`,{session_id:currentSession});});
$("older").onclick=handle(()=>loadHistory(true));
$("new-session").onclick=handle(async()=>{currentSession="";await ensureSession();firstOrdinal=null;await loadHistory();showView("chat");});
$("recording").onclick=handle(async()=>{await api("/recording",{enabled:!state.stats.recording});pendingFiles=[];$("pending-files").textContent="";await refresh();await loadHistory();status(state.stats.recording?"长期保存已开启，临时交流未补存":"已暂停保存，允许读取旧记忆");});
async function showView(name){for(const v of document.querySelectorAll(".view"))v.classList.toggle("hidden",v.id!=="view-"+name);for(const b of document.querySelectorAll("nav button"))b.classList.toggle("selected",b.dataset.view===name);$("view-title").textContent=titles[name];if(name==="recycle")await loadRecycle();if(name==="episodes")await loadEpisodes();if(name==="data")await loadProducts();if(name==="settings")fillSettings();}
for(const b of document.querySelectorAll("nav button"))b.onclick=handle(()=>showView(b.dataset.view));
async function search(append=false){const params=new URLSearchParams({q:$("query").value,role:$("role").value,semantic:String($("semantic").checked),cursor:String(searchCursor)});if($("since").value)params.set("since",String(new Date($("since").value+"T00:00:00").getTime()/1000));if($("until").value)params.set("until",String(new Date($("until").value+"T23:59:59").getTime()/1000));const result=await api("/search?"+params);if(!append)$("search-results").replaceChildren();result.messages.forEach(row=>$("search-results").append(message(row)));status(result.degraded||`找到 ${result.messages.length} 条记录`);$("more-search").classList.toggle("hidden",result.messages.length<30||$("semantic").checked);}
$("search-form").onsubmit=handle(async event=>{event.preventDefault();searchCursor=0;await search();});$("more-search").onclick=handle(async()=>{searchCursor+=30;await search(true);});
async function loadEpisodes(){const result=await api("/episodes");$("episodes").replaceChildren();for(const episode of result.episodes){const card=node("article",undefined,"episode-card");card.append(node("p",formatTime(episode.created),"muted"),node("div",episode.summary,"summary"));for(const mid of JSON.parse(episode.source_ids))card.append(button("来源 "+mid.slice(0,16),()=>showContext(mid),"quiet"));$("episodes").append(card);}if(!result.episodes.length)$("episodes").append(node("p","暂无经历整理。消息原文仍可检索。","muted"));}
$("retry-jobs").onclick=handle(async()=>{await api("/jobs/retry",{});status("已安排重试");});
async function loadRecycle(){const result=await api("/recycle");$("recycle-list").replaceChildren();for(const item of result.items){const card=node("article",undefined,"recycle-card");card.append(node("p",`${item.status} · 移入 ${formatTime(item.created)} · 到期 ${formatTime(item.expires)}`));for(const row of item.messages)card.append(message(row,true));if(item.status==="recycled"){card.append(button("恢复所选记录",async()=>{await api(`/recycle/${item.id}/restore`,{});await loadRecycle();await loadHistory();}),button("永久清理…",()=>{const wrap=node("div");wrap.append(node("p","将清理这些记录、附件与受管副本，相关快照可能失效。此操作无法撤销。"),button("确认永久清理",async()=>{await api(`/recycle/${item.id}/purge`,{});$("detail").close();await loadRecycle();},"danger"));details("永久清理",wrap);},"danger"));}$("recycle-list").append(card);}if(!result.items.length)$("recycle-list").append(node("p","回收站为空。","muted"));}
async function loadProducts(){const result=await api("/products");$("products").replaceChildren();for(const product of result.products){const card=node("div",undefined,"product-card");card.append(node("strong",product.kind==="snapshot"?"本人格快照":"会话导出"),node("p",`${product.id} · ${formatTime(product.created)} · ${product.status}`));if(product.status==="active"){if(product.kind==="snapshot"){card.append(button("校验",async()=>{await api(`/snapshots/${product.id}/verify`,{});status("快照校验通过");}),button("恢复…",()=>{const wrap=node("div");wrap.append(node("p","恢复将切换至这个快照的数据，并应用当前生命周期清单。快照之后新增的消息不会出现在恢复结果中。"),button("验证并恢复",async()=>{await api(`/snapshots/${product.id}/restore`,{});$("detail").close();await refresh();currentSession="";await ensureSession();await loadHistory();await loadProducts();status("快照已恢复，索引正在重建");},"primary"));details("恢复快照",wrap);}));}else for(const filename of ["transcript.md","messages.jsonl","episodes.md","export-manifest.json"]){const a=node("a",filename);a.href=`/api/exports/${product.id}/file?name=${encodeURIComponent(filename)}`;a.download=filename;card.append(a);}}$("products").append(card);}}
$("export-form").onsubmit=handle(async event=>{event.preventDefault();await ensureSession();const result=await api("/exports",{session_id:currentSession,include_media:$("include-media").checked,include_persona:$("include-persona").checked});status(`导出完成，共 ${result.message_count} 条消息`);await loadProducts();});$("snapshot").onclick=handle(async()=>{status("正在固定写入视图并校验媒体……");await api("/snapshots",{});status("快照已完成");await loadProducts();});
const configFields=["mode","base_url","model","summary_model","context_limit","output_limit","request_timeout","thinking_mode","vision","audio","vision_model","asr_model"];
let configBusy=false, configFilled=false, configDirty=false;
function normalizedBase(value) {
  const url=new URL(value.trim());
  if(!["http:","https:"].includes(url.protocol)||url.username||url.password||url.search||url.hash)throw Error("请填写不含凭据、参数或片段的 HTTP(S) API 基址");
  return url.href.replace(/\/$/,"").replace(/\/(chat\/completions|audio\/transcriptions|models)$/,"");
}
function connectionFeedback(text,error=false){$("config-feedback").textContent=text;$("config-feedback").classList.toggle("error",error);}
function renderConnectionState(){
  const active=!!state?.connection?.active;
  const outdated=state!==null&&!state.connection;
  $("connection-status").textContent=outdated?"后端尚未更新：请在启动窗口按 Ctrl+C，重新双击启动实验版.cmd，再按 Ctrl+F5 刷新。":active?`已启用：${state.config.model} · ${state.config.base_url}`:"当前使用模拟模型；填写设置后保存并启用";
  $("connection-status").classList.toggle("error",outdated);
  $("clear-key").disabled=configBusy||!active;
  updateConnectionForm();
}
function updateConnectionForm(){
  const remote=$("mode").value==="remote";
  $("remote-fields").disabled=configBusy||!remote;
  $("remote-fields").classList.toggle("hidden",!remote);
  $("probe-help").classList.toggle("hidden",!remote);
  $("base_url").required=remote;$("model").required=remote;
  const outdated=state!==null&&!state.connection;
  $("test-config").disabled=configBusy||!remote||outdated;
  $("load-models").disabled=configBusy||!remote||outdated;
  $("save-config").textContent=remote?"验证并启用":"启用模拟模型";
  $("save-config").disabled=configBusy||outdated;$("mode").disabled=configBusy;
  let sameEndpoint=false;
  try{sameEndpoint=normalizedBase($("base_url").value)===state?.config?.base_url;}catch{}
  $("key-status").textContent=state?.connection?.has_key&&sameEndpoint?"当前连接的密钥已在内存中；留空沿用，填写新值则替换。重启后需重新填写。":"未设置此地址的密钥；无认证的本机服务可留空。更换地址不会沿用旧密钥。";
  try{$("endpoint-preview").textContent=$("base_url").value.trim()?"聊天接口："+normalizedBase($("base_url").value)+"/chat/completions":"";}catch{$("endpoint-preview").textContent="地址格式无效，请填写完整 API 基址。";}
}
function fillSettings(){
  if(!configFilled||!configDirty){
    for(const key of configFields){if($(key).type==="checkbox")$(key).checked=!!state.config[key];else $(key).value=state.config[key]??(key==="request_timeout"?180:key==="thinking_mode"?"default":"");}
    configFilled=true;
  }
  $("persona-id").textContent=state.persona.persona_id;$("persona-name").value=state.persona.name;$("instructions").value=state.persona.instructions;
  renderConnectionState();
}
function configInput(){
  const value={};for(const key of configFields)value[key]=$(key).type==="checkbox"?$(key).checked:$(key).type==="number"?Number($(key).value):$(key).value.trim();
  value.api_key=$("api_key").value.trim();value.use_existing_key=true;return value;
}
async function connectionAction(action){
  if(configBusy)return;
  configBusy=true;renderConnectionState();
  try{await action();}catch(error){connectionFeedback(error.message,true);status(error.message,true);}
  finally{configBusy=false;renderConnectionState();}
}
$("config-form").addEventListener("input",()=>{configDirty=true;connectionFeedback("设置有修改；测试通过后保存才会启用。");updateConnectionForm();});
$("mode").addEventListener("change",()=>{configDirty=true;updateConnectionForm();});
$("base_url").addEventListener("input",()=>{$("model-options").replaceChildren();$("models-status").textContent="地址已修改，可重新获取模型列表，也可手动填写模型名。";updateConnectionForm();});
$("toggle-key").onclick=()=>{const show=$("api_key").type==="password";$("api_key").type=show?"text":"password";$("toggle-key").textContent=show?"隐藏":"显示";$("toggle-key").setAttribute("aria-pressed",String(show));};
$("load-models").onclick=handle(async()=>{
  if(!$("base_url").reportValidity())return;
  const input=configInput();await connectionAction(async()=>{
    connectionFeedback("正在获取模型列表……");const result=await api("/config/models",input);
    $("model-options").replaceChildren(...result.models.map(id=>{const option=node("option");option.value=id;return option;}));
    $("base_url").value=result.base_url||input.base_url;
    $("models-status").textContent=result.models.length?`已获取 ${result.models.length} 个模型；点击聊天模型输入框选择，也可手动填写。`:"服务返回空列表；可手动填写模型名后测试回复。";
    connectionFeedback("模型列表已获取。请选择模型并测试回复；当前连接未切换。");
  });
});
$("test-config").onclick=handle(async()=>{
  if(!$("config-form").reportValidity())return;
  const input=configInput();await connectionAction(async()=>{
    connectionFeedback(`正在测试文字流式回复……每个模型请求最长等待 ${input.request_timeout} 秒。`);const result=await api("/config/test",input);
    $("base_url").value=result.base_url||input.base_url;
    const probeNotice=result.probe_thinking_disabled?.length?" 本次对 Qwen3.5 关闭思考，只验证基础回复；未验证开启思考的耗时。":"";
    connectionFeedback(`回复测试通过 · ${result.models.join("、")} · ${result.latency_ms} 毫秒。当前连接未切换，请保存并启用。${probeNotice}`);
  });
});
$("config-form").onsubmit=handle(async event=>{
  event.preventDefault();const input=configInput();await connectionAction(async()=>{
    connectionFeedback(input.mode==="remote"?`正在验证模型并保存……每个模型请求最长等待 ${input.request_timeout} 秒。`:"正在切换到模拟模型……");
    const saved=await api("/config",input);$("api_key").value="";$("api_key").type="password";$("toggle-key").textContent="显示";$("toggle-key").setAttribute("aria-pressed","false");
    configDirty=false;await refresh();fillSettings();
    const notice=input.mode==="remote"?"模型已验证并启用。密钥仅驻留内存，重启后需重新启用。"+(saved.test?.probe_thinking_disabled?.length?" 验证已关闭 Qwen3.5 思考；日常聊天遵循高级设置的思考模式。":""):"已启用模拟模型，当前远程密钥已从连接中移除。";
    connectionFeedback(notice);status(notice);
  });
});
$("clear-key").onclick=handle(()=>connectionAction(async()=>{
  await api("/config/credentials/clear",{});$("api_key").value="";configDirty=false;await refresh();fillSettings();
  connectionFeedback("当前连接密钥已清除，已切换到模拟模型。");status("远程连接已停用");
}));
$("persona-form").onsubmit=handle(async event=>{event.preventDefault();await api("/persona",{name:$("persona-name").value,instructions:$("instructions").value});await refresh();status("人格设定已更新，身份编号保持不变");});$("embedding").onclick=handle(async()=>{status("正在从项目内加载 BGE-M3……");await api("/embedding",{});await refresh();status("本地 embedding 已启用，正在安排索引");});$("open-users").onclick=handle(()=>api("/users/open",{}));$("quit").onclick=handle(async()=>{await api("/quit",{});status("本地服务已退出，可以关闭网页。");$("send").disabled=true;});
async function init(){try{const token=new URLSearchParams(location.hash.slice(1)).get("token");if(token){await api("/bootstrap",{token});window.history.replaceState(null,"",location.pathname);}await refresh();currentSession=sessionStorage.getItem("memorylab_session")||"";await ensureSession();await loadHistory();const pending=JSON.parse(sessionStorage.getItem("memorylab_request")||"null");if(pending&&pending.session===currentSession)await poll(pending.id);}catch(error){status(error.message,true);}}
init();

const baseMessage = message;
message = function(row, recycle=false) {
  const card=baseMessage(row,recycle);
  if(!row.temporary&&!recycle){
    const labels={pending:"待整理 / 索引",running:"后台处理中",done:"整理已完成",failed:"整理失败，可到经历页重试",invalid:"来源变化，待重建",not_queued:"尚未排入整理"};
    if(row.processing)card.append(node("small",labels[row.processing.status]||row.processing.status,"muted"));
    for(const attachment of row.attachments||[])card.append(button("修正识别文字 · "+attachment.id.slice(0,14),()=>{
      const wrap=node("div"),text=document.createElement("textarea");text.rows=6;text.value=attachment.recognition||"";
      wrap.append(node("p","修订只影响这条附件的识别文字，不修改原文件，也不冒充用户原话。"),text,button("保存识别修订",async()=>{
        await api(`/attachments/${attachment.id}/correction`,{text:text.value});$("detail").close();await loadHistory();status("识别修订已保存");
      },"primary"));details("修正识别结果",wrap);
    },"quiet"));
  }
  return card;
};
$("clear-recycle").onclick=handle(()=>{
  const wrap=node("div");wrap.append(node("p","将永久清理回收站中所有尚未到期的所选记录及其受管副本，不能撤销。"),button("确认清空回收站",async()=>{
    const result=await api("/recycle/clear",{});$("detail").close();status(result.status==="purge_pending"?`仍有 ${result.pending} 项待清理，请检查状态后重试`:`已清理 ${result.count} 项回收站操作`);await loadRecycle();
  },"danger"));details("清空回收站",wrap);
});

const baseLoadProducts=loadProducts;
loadProducts=async function(){
  await baseLoadProducts();
  for(const card of document.querySelectorAll("#products .product-card")){
    const match=card.textContent.match(/export_[a-f0-9]{32}/);
    if(match&&card.textContent.includes("active")){
      const link=node("a","下载完整 ZIP（含所选附件）");link.href=`/api/exports/${match[0]}/archive`;link.download=match[0]+".zip";card.append(link);
    }
  }
};
