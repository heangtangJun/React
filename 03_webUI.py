"""
03_webUI.py —— 极简 Web UI 窗口（对话交流 + HITL 审批闭环）

设计目标（一步到位）：
1. 对话交流：提交问题后自动轮询 /agent/status，直到 completed 直接把回答显示在窗口里；
2. HITL 审批：当任务 interrupted 时，窗口内弹出审批卡片，支持 接受/拒绝/修改参数/直接反馈 四种操作，
   调用 /agent/resume 后继续轮询，形成完整闭环；
3. 最简单的前端：单个 HTML 页面（内联 CSS/JS），由本文件的 FastAPI 直接托管；
4. 零 CORS、零后端改动：本服务在 8002 端口既提供页面，又把 /api/* 反向代理到后端 8001，浏览器只与同源 8002 通信。

启动前置：PostgreSQL、Redis、Celery worker、后端(01_backendServer.py, :8001) 均已运行。
启动方式：python 03_webUI.py  然后浏览器打开 http://localhost:8002
"""

import sys
import asyncio
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# 后端地址与本 UI 服务端口
BACKEND_BASE_URL = "http://localhost:8001"
UI_HOST = "0.0.0.0"
UI_PORT = 8002

app = FastAPI(title="ReAct Agent Web UI", description="极简对话式前端窗口（含 HITL 审批）")


# ----------------------------- 反向代理到后端 -----------------------------
async def _proxy(method: str, path: str, json_body=None):
    """把请求转发到后端，统一异常处理，避免浏览器直接跨域访问后端。"""
    url = f"{BACKEND_BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(method, url, json=json_body)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return JSONResponse(status_code=resp.status_code, content=data)
    except httpx.HTTPError as e:
        # 后端未启动等连接错误
        return JSONResponse(status_code=502, content={
            "detail": f"无法连接后端 {BACKEND_BASE_URL}，请确认 01_backendServer.py 与 Celery worker 已启动。错误：{e}"
        })


@app.post("/api/invoke")
async def api_invoke(request: Request):
    body = await request.json()
    return await _proxy("POST", "/agent/invoke", body)


@app.post("/api/resume")
async def api_resume(request: Request):
    body = await request.json()
    return await _proxy("POST", "/agent/resume", body)


@app.get("/api/status/{user_id}/{session_id}/{task_id}")
async def api_status(user_id: str, session_id: str, task_id: str):
    return await _proxy("GET", f"/agent/status/{user_id}/{session_id}/{task_id}")


@app.get("/api/system")
async def api_system():
    return await _proxy("GET", "/system/info")


@app.post("/api/longterm")
async def api_longterm(request: Request):
    body = await request.json()
    return await _proxy("POST", "/agent/write/longterm", body)


# ----------------------------- 前端页面 -----------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>ReAct Agent 对话窗口</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #f0f2f5; color: #1f2329; height: 100vh; display: flex; flex-direction: column; }
  header { background: #2b5cff; color: #fff; padding: 12px 20px; display: flex;
           align-items: center; gap: 12px; flex-wrap: wrap; box-shadow: 0 1px 4px rgba(0,0,0,.15); }
  header h1 { font-size: 17px; font-weight: 600; }
  header .spacer { flex: 1; }
  header input { padding: 6px 10px; border: none; border-radius: 6px; font-size: 13px; width: 180px; }
  header button { padding: 6px 14px; border: none; border-radius: 6px; background: #fff; color: #2b5cff;
                  font-size: 13px; cursor: pointer; font-weight: 600; }
  header button:hover { background: #e6ecff; }
  #status-bar { font-size: 12px; padding: 6px 20px; background: #fff; border-bottom: 1px solid #e5e6eb; color: #86909c; }
  #chat { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 14px; }
  .msg { max-width: 78%; padding: 11px 15px; border-radius: 12px; line-height: 1.6;
         font-size: 14px; white-space: pre-wrap; word-break: break-word; }
  .user { align-self: flex-end; background: #2b5cff; color: #fff; border-bottom-right-radius: 3px; }
  .bot  { align-self: flex-start; background: #fff; border: 1px solid #e5e6eb; border-bottom-left-radius: 3px; }
  .err  { align-self: flex-start; background: #fff1f0; border: 1px solid #ffccc7; color: #cf1322; }
  .sys  { align-self: center; background: transparent; color: #86909c; font-size: 12px; }
  .thinking { align-self: flex-start; color: #86909c; font-size: 13px; }
  .thinking .dot { animation: blink 1.2s infinite; }
  .thinking .dot:nth-child(2){ animation-delay:.2s } .thinking .dot:nth-child(3){ animation-delay:.4s }
  @keyframes blink { 0%,60%,100%{opacity:.2} 30%{opacity:1} }
  .approval { align-self: flex-start; max-width: 82%; background: #fffbe6; border: 1px solid #ffe58f;
              border-radius: 12px; padding: 14px 16px; }
  .approval h3 { font-size: 14px; color: #d48806; margin-bottom: 8px; }
  .approval pre { background: #fff; border: 1px solid #f0f0f0; border-radius: 6px; padding: 8px 10px;
                  font-size: 12px; overflow-x: auto; margin: 8px 0; white-space: pre-wrap; }
  .approval .btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  .approval button { padding: 7px 14px; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; }
  .b-yes { background: #52c41a; color: #fff; } .b-no { background: #ff4d4f; color: #fff; }
  .b-edit, .b-resp { background: #fff; border: 1px solid #d9d9d9 !important; color: #1f2329; }
  .approval textarea { width: 100%; margin-top: 10px; padding: 8px 10px; border: 1px solid #d9d9d9;
                       border-radius: 6px; font-size: 13px; font-family: monospace; resize: vertical; }
  .approval .confirm { margin-top: 8px; background: #2b5cff; color: #fff; }
  footer { padding: 14px 20px; background: #fff; border-top: 1px solid #e5e6eb; display: flex; gap: 10px; }
  footer textarea { flex: 1; padding: 10px 12px; border: 1px solid #d9d9d9; border-radius: 8px;
                    font-size: 14px; resize: none; height: 46px; font-family: inherit; }
  footer button { padding: 0 24px; border: none; border-radius: 8px; background: #2b5cff; color: #fff;
                  font-size: 14px; cursor: pointer; font-weight: 600; }
  footer button:disabled { background: #a9c0ff; cursor: not-allowed; }
</style>
</head>
<body>
<header>
  <h1>🤖 ReAct Agent 对话窗口</h1>
  <div class="spacer"></div>
  <label style="font-size:13px">用户ID</label>
  <input id="userId" value="web_user_1"/>
  <button id="newSessionBtn">＋ 新会话</button>
</header>
<div id="status-bar">就绪</div>
<div id="chat"></div>
<footer>
  <textarea id="input" placeholder="输入问题，Enter 发送，Shift+Enter 换行。例如：帮我预定如家酒店"></textarea>
  <button id="sendBtn">发送</button>
</footer>

<script>
const chat = document.getElementById('chat');
const input = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const statusBar = document.getElementById('status-bar');
const userIdEl = document.getElementById('userId');

let sessionId = uuid();
let currentTaskId = null;
let busy = false;

function uuid() {
  return (crypto.randomUUID ? crypto.randomUUID()
        : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
            const r = Math.random()*16|0, v = c==='x'?r:(r&0x3|0x8); return v.toString(16); }));
}
function userId() { return (userIdEl.value || 'web_user').trim(); }
function setStatus(t) { statusBar.textContent = t; }
function scrollBottom() { chat.scrollTop = chat.scrollHeight; }

function addMsg(text, cls) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  chat.appendChild(div); scrollBottom();
  return div;
}
function addThinking() {
  const div = document.createElement('div');
  div.className = 'thinking';
  div.innerHTML = '智能体思考中<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span>';
  chat.appendChild(div); scrollBottom();
  return div;
}
function setBusy(b) { busy = b; sendBtn.disabled = b; input.disabled = b; }

// 从 completed 的结果里提取最终 AI 回答
function extractAnswer(statusData) {
  try {
    const msgs = statusData.last_response.result.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].type === 'ai' && msgs[i].content && String(msgs[i].content).trim()) {
        return String(msgs[i].content);
      }
    }
    if (msgs.length) return String(msgs[msgs.length-1].content || '(无文本内容)');
  } catch (e) {}
  return '(未解析到回答内容)';
}

async function api(method, path, body) {
  const opt = { method, headers: {'Content-Type':'application/json'} };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  const data = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}

// 轮询任务状态直到终态
async function pollStatus(taskId) {
  const maxAttempts = 80;
  for (let i = 0; i < maxAttempts; i++) {
    await new Promise(r => setTimeout(r, 1200));
    let s;
    try { s = await api('GET', `/api/status/${userId()}/${sessionId}/${taskId}`); }
    catch (e) { setStatus('查询状态失败：' + e.message); continue; }
    const st = s.status;
    setStatus(`任务状态：${st}（第 ${i+1} 次轮询）`);
    if (st === 'completed') return { type:'completed', data:s };
    if (st === 'interrupted') return { type:'interrupted', data:s };
    if (st === 'error') return { type:'error', data:s };
    if (st === 'not_found') return { type:'error', data:{ message:'任务不存在' } };
    // pending / running / idle → 继续等待
  }
  return { type:'timeout' };
}

async function send() {
  if (busy) return;
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  addMsg(text, 'user');
  setBusy(true);

  currentTaskId = uuid();
  const thinking = addThinking();
  setStatus('已提交，等待智能体处理…');
  try {
    await api('POST', '/api/invoke', {
      user_id: userId(), session_id: sessionId, task_id: currentTaskId, query: text,
      system_message: '你会使用工具来帮助用户。如果工具使用被拒绝，请提示用户。'
    });
    const res = await pollStatus(currentTaskId);
    thinking.remove();
    handleResult(res);
  } catch (e) {
    thinking.remove();
    addMsg('请求出错：' + e.message, 'err');
    setStatus('出错');
    setBusy(false);
  }
}

function handleResult(res) {
  if (res.type === 'completed') {
    addMsg(extractAnswer(res.data), 'bot');
    setStatus('完成');
    setBusy(false);
    input.focus();
  } else if (res.type === 'error') {
    const m = (res.data && (res.data.message || (res.data.last_response && res.data.last_response.message))) || '未知错误';
    addMsg('处理出错：' + m, 'err');
    setStatus('出错');
    setBusy(false);
  } else if (res.type === 'timeout') {
    addMsg('等待超时，任务可能仍在后台运行，可稍后重试。', 'err');
    setStatus('超时');
    setBusy(false);
  } else if (res.type === 'interrupted') {
    renderApproval(res.data);
  }
}

// 渲染 HITL 审批卡片
function renderApproval(statusData) {
  const idata = (statusData.last_response && statusData.last_response.interrupt_data) || {};
  const actionReq = idata.action_request || {};
  const desc = idata.description || '智能体需要您的决定';
  setStatus('等待人工审批');

  const card = document.createElement('div');
  card.className = 'approval';
  card.innerHTML =
    `<h3>⚠️ 需要人工审查</h3>` +
    `<div>${escapeHtml(desc)}</div>` +
    `<pre>工具：${escapeHtml(String(actionReq.action || '-'))}\n参数：${escapeHtml(JSON.stringify(actionReq.args || {}, null, 2))}</pre>` +
    `<div class="btns">
       <button class="b-yes">✅ 接受 (accept)</button>
       <button class="b-no">❌ 拒绝 (reject)</button>
       <button class="b-edit">✏️ 修改参数 (edit)</button>
       <button class="b-resp">💬 直接反馈 (response)</button>
     </div>`;
  chat.appendChild(card); scrollBottom();

  card.querySelector('.b-yes').onclick = () => doResume('accept', null, card);
  card.querySelector('.b-no').onclick  = () => doResume('reject', null, card);
  card.querySelector('.b-edit').onclick = () => {
    const ta = promptArea(card, JSON.stringify(actionReq.args || {}, null, 2));
    ta.confirm.onclick = () => {
      let parsed;
      try { parsed = JSON.parse(ta.input.value); }
      catch (e) { alert('参数必须是合法 JSON：' + e.message); return; }
      doResume('edit', { args: parsed }, card);
    };
  };
  card.querySelector('.b-resp').onclick = () => {
    const ta = promptArea(card, '');
    ta.input.placeholder = '输入不调用工具、直接反馈给智能体的信息…';
    ta.confirm.onclick = () => doResume('response', { args: ta.input.value }, card);
  };
}

function promptArea(card, preset) {
  // 移除已有输入区，避免重复
  const old = card.querySelector('.extra'); if (old) old.remove();
  const wrap = document.createElement('div'); wrap.className = 'extra';
  const ta = document.createElement('textarea'); ta.rows = 3; ta.value = preset || '';
  const btn = document.createElement('button'); btn.className = 'confirm'; btn.textContent = '确认提交';
  wrap.appendChild(ta); wrap.appendChild(btn); card.appendChild(wrap); scrollBottom();
  ta.focus();
  return { input: ta, confirm: btn };
}

async function doResume(responseType, args, card) {
  setBusy(true);
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  const thinking = addThinking();
  setStatus(`已提交审批（${responseType}），等待智能体继续…`);
  try {
    await api('POST', '/api/resume', {
      user_id: userId(), session_id: sessionId, task_id: currentTaskId,
      response_type: responseType, args: args
    });
    const res = await pollStatus(currentTaskId);
    thinking.remove();
    card.remove();
    handleResult(res);
  } catch (e) {
    thinking.remove();
    addMsg('恢复出错：' + e.message, 'err');
    setStatus('出错');
    card.querySelectorAll('button').forEach(b => b.disabled = false);
    setBusy(false);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// 新会话
document.getElementById('newSessionBtn').onclick = () => {
  sessionId = uuid(); currentTaskId = null;
  chat.innerHTML = '';
  addMsg('已开启新会话，session_id=' + sessionId, 'sys');
  setStatus('新会话就绪');
  input.focus();
};

sendBtn.onclick = send;
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

// 初始化
(async () => {
  addMsg('欢迎使用 ReAct Agent 对话窗口。当前会话 session_id=' + sessionId, 'sys');
  try { const info = await api('GET', '/api/system'); setStatus(`就绪 | 系统会话总数：${info.sessions_count}`); }
  catch (e) { setStatus('后端不可达：' + e.message); }
  input.focus();
})();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    print(f"Web UI 启动：http://localhost:{UI_PORT}  （反向代理后端 {BACKEND_BASE_URL}）")
    uvicorn.run(app, host=UI_HOST, port=UI_PORT)
