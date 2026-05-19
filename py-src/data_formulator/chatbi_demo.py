# -*- coding: utf-8 -*-
"""
最小 ChatBI 演示入口（POC 阶段，非产品化）。

定位：
- 提供一个独立 HTML 页面 GET /chatbi，纯原生 fetch + NDJSON 流读取，
  零 npm / 零 vite build，便于直接演示 DataAgent + query_beelink_sql 链路。
- 不进入 DF React 体系，不读 UI store；表单字段由演示者临时填入（api_key 仅
  存在于浏览器内存），与 curl 路径完全等价。

不包含：会话历史 / 图表渲染 / BO / RAG / 修复 Agent / SQL Guard。

后端契约（DataAgent streaming，由 routes/agents.py 提供）：
  POST /api/agent/data-agent-streaming
  Headers: X-Identity-Id, X-Workspace-Id
  Body:   { model: {endpoint, model, api_key, api_base, api_version, is_global},
            input_tables: [], primary_tables: [], user_question: "..." }
  Response: 逐行 JSON（NDJSON），事件类型见后端实现。
"""
from __future__ import annotations

from flask import Blueprint, Response


chatbi_bp = Blueprint("chatbi", __name__)


# 单页 HTML：刻意保留在一个文件里，避免新增模板/静态资源目录。
# 仅依赖浏览器原生 fetch + ReadableStream，不引入任何外部脚本。
_CHATBI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>beelink ChatBI 演示入口 (POC)</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 980px; margin: 24px auto; padding: 0 16px; line-height: 1.5; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #888; font-size: 13px; margin-bottom: 16px; }
  fieldset { border: 1px solid #ccc; border-radius: 6px; padding: 12px 16px; margin-bottom: 12px; }
  legend { padding: 0 6px; font-weight: 600; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 8px; }
  .row > label { flex: 1 1 220px; display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
  input, select, textarea { font: inherit; padding: 6px 8px; border: 1px solid #bbb; border-radius: 4px; }
  textarea { width: 100%; min-height: 70px; resize: vertical; }
  button { font: inherit; padding: 8px 16px; border-radius: 4px; border: 1px solid #2563eb;
           background: #2563eb; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .warn { color: #b45309; font-size: 12px; }
  #stream { margin-top: 12px; border: 1px solid #ccc; border-radius: 6px;
            padding: 10px; min-height: 260px; max-height: 60vh; overflow: auto;
            background: #fafafa; font-family: ui-monospace, "JetBrains Mono", Menlo, monospace;
            font-size: 12.5px; white-space: pre-wrap; }
  .evt { border-left: 3px solid #ccc; padding: 4px 8px; margin: 4px 0; background: #fff; }
  .evt-tool_call { border-left-color: #2563eb; }
  .evt-tool_result { border-left-color: #16a34a; }
  .evt-text_delta { border-left-color: #6b7280; background: transparent; padding: 0 8px; }
  .evt-error { border-left-color: #dc2626; background: #fef2f2; }
  .evt-completion { border-left-color: #16a34a; background: #f0fdf4; }
  .evt-header { font-weight: 600; }
  details { margin-top: 4px; }
  details > summary { cursor: pointer; color: #555; font-size: 12px; }
  details pre { margin: 4px 0 0; font-size: 12px; overflow: auto; max-height: 200px; }
</style>
</head>
<body>
  <h1>beelink ChatBI 演示入口 (POC)</h1>
  <div class="sub">
    最小 ChatBI 前端：调用 <code>/api/agent/data-agent-streaming</code>，
    复用 DataAgent + <code>query_beelink_sql</code> tool。
    仅供演示，不持久化任何输入；<b>API key 仅存在于本浏览器内存</b>。
  </div>

  <form id="form">
    <fieldset>
      <legend>身份与 workspace</legend>
      <div class="row">
        <label>X-Identity-Id
          <input id="identity" value="local:beelink">
        </label>
        <label>X-Workspace-Id
          <input id="workspace" value="default">
        </label>
      </div>
    </fieldset>

    <fieldset>
      <legend>LLM 配置</legend>
      <div class="row">
        <label>endpoint
          <input id="endpoint" value="openai">
        </label>
        <label>model
          <select id="model">
            <option value="deepseek-chat" selected>deepseek-chat（推荐，V3 非 thinking）</option>
            <option value="deepseek-v4-pro">deepseek-v4-pro（不推荐，thinking 模式与 DataAgent 自循环不兼容）</option>
            <option value="deepseek-v4-flash">deepseek-v4-flash（不推荐，同上）</option>
            <option value="deepseek-reasoner">deepseek-reasoner（不推荐，同上）</option>
          </select>
        </label>
        <label>api_base
          <input id="api_base" value="https://api.deepseek.com">
        </label>
      </div>
      <div class="row">
        <label>api_key（仅浏览器内存，不上传不入仓）
          <input id="api_key" type="password" autocomplete="off" placeholder="sk-...">
        </label>
      </div>
      <div class="warn" id="model_warn" style="display:none">
        ⚠️ 已选模型为 thinking 模式，DataAgent 主循环目前不处理 reasoning_content 回传，
        通常会报 400。仅建议在 POC-2A 独立 NL2SQL API 使用。
      </div>
    </fieldset>

    <fieldset>
      <legend>问题</legend>
      <textarea id="question" placeholder="例如：看下 smartquery_demo.customers 各 gender 客户数。">看下 smartquery_demo.customers 各 gender 客户数。</textarea>
    </fieldset>

    <button id="submit" type="submit">发送</button>
    <span id="status" class="sub" style="margin-left:12px"></span>
  </form>

  <div id="stream" aria-live="polite"></div>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var streamEl = $('stream');
  var modelEl = $('model');
  var warnEl = $('model_warn');
  var formEl = $('form');
  var statusEl = $('status');
  var submitBtn = $('submit');

  function updateWarn() {
    warnEl.style.display = modelEl.value === 'deepseek-chat' ? 'none' : 'block';
  }
  modelEl.addEventListener('change', updateWarn);
  updateWarn();

  function appendEvent(type, payloadObj) {
    var div = document.createElement('div');
    div.className = 'evt evt-' + (type || 'unknown').replace(/[^a-z0-9_]/gi, '');
    var head = document.createElement('div');
    head.className = 'evt-header';
    head.textContent = type || '(no type)';
    div.appendChild(head);

    // 仅渲染有限字段的摘要 + 折叠完整 JSON，避免噪声满屏
    var summary = summarize(type, payloadObj);
    if (summary) {
      var s = document.createElement('div');
      s.textContent = summary;
      div.appendChild(s);
    }
    var det = document.createElement('details');
    var sum = document.createElement('summary');
    sum.textContent = '完整事件 JSON';
    det.appendChild(sum);
    var pre = document.createElement('pre');
    pre.textContent = JSON.stringify(payloadObj, null, 2);
    det.appendChild(pre);
    div.appendChild(det);
    streamEl.appendChild(div);
    streamEl.scrollTop = streamEl.scrollHeight;
  }

  function summarize(type, p) {
    if (!p) return '';
    if (type === 'text_delta') return p.delta || p.text || '';
    if (type === 'tool_call' || type === 'tool_start') {
      var name = p.name || (p.tool && p.tool.name) || '';
      var args = p.arguments || p.args || (p.tool && p.tool.arguments) || {};
      var pieces = [];
      if (args.sql) pieces.push('sql=' + String(args.sql).slice(0, 200));
      if (args.purpose) pieces.push('purpose=' + args.purpose);
      if (args.table_name) pieces.push('table_name=' + args.table_name);
      if (args.connector_id) pieces.push('connector_id=' + args.connector_id);
      return name + (pieces.length ? '  [' + pieces.join(' | ') + ']' : '');
    }
    if (type === 'tool_result') {
      var r = p.result || p.output || {};
      var bits = [];
      if (r.table_name) bits.push('table=' + r.table_name);
      if (typeof r.row_count !== 'undefined') bits.push('rows=' + r.row_count);
      if (r.errorMessage) bits.push('error=' + String(r.errorMessage).slice(0, 200));
      return bits.join(' | ');
    }
    if (type === 'error') return p.message || JSON.stringify(p);
    if (type === 'completion') return '完成';
    return '';
  }

  function setBusy(busy, msg) {
    submitBtn.disabled = busy;
    statusEl.textContent = msg || '';
  }

  formEl.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var identity = $('identity').value.trim();
    var workspace = $('workspace').value.trim();
    var endpoint = $('endpoint').value.trim();
    var model = modelEl.value;
    var api_base = $('api_base').value.trim();
    var api_key = $('api_key').value;
    var question = $('question').value.trim();

    if (!identity || !workspace) { alert('请填 X-Identity-Id 与 X-Workspace-Id'); return; }
    if (!api_key) { alert('请填 api_key（仅浏览器内存）'); return; }
    if (!question) { alert('请填问题'); return; }

    streamEl.innerHTML = '';
    setBusy(true, '请求中…');

    var body = {
      model: {
        endpoint: endpoint, model: model,
        api_key: api_key, api_base: api_base,
        api_version: null, is_global: false
      },
      input_tables: [],
      primary_tables: [],
      user_question: question
    };

    try {
      // 先触发 connector lazy-load，避免 DF 刚启动时 "Connector not found"
      await fetch('/api/connectors', {
        method: 'GET',
        headers: { 'X-Identity-Id': identity, 'X-Workspace-Id': workspace }
      });

      var resp = await fetch('/api/agent/data-agent-streaming', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Identity-Id': identity,
          'X-Workspace-Id': workspace
        },
        body: JSON.stringify(body)
      });

      if (!resp.ok) {
        var errText = await resp.text();
        appendEvent('error', { status: resp.status, body: errText.slice(0, 1000) });
        setBusy(false, '失败（HTTP ' + resp.status + '）');
        return;
      }

      var reader = resp.body.getReader();
      var decoder = new TextDecoder('utf-8');
      var buf = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += decoder.decode(chunk.value, { stream: true });
        var lines = buf.split('\\n');
        buf = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i].trim();
          if (!line) continue;
          try {
            var obj = JSON.parse(line);
            appendEvent(obj.type, obj);
          } catch (e) {
            appendEvent('raw', { line: line });
          }
        }
      }
      if (buf.trim()) {
        try { appendEvent(JSON.parse(buf).type, JSON.parse(buf)); } catch (e) {}
      }
      setBusy(false, '完成');
    } catch (e) {
      appendEvent('error', { message: String(e && e.message || e) });
      setBusy(false, '异常');
    }
  });
})();
</script>
</body>
</html>
"""


@chatbi_bp.route("/chatbi", methods=["GET"])
def chatbi_page():
    """最小 ChatBI 演示入口。返回单页 HTML。"""
    return Response(_CHATBI_HTML, mimetype="text/html; charset=utf-8")
