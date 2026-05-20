# -*- coding: utf-8 -*-
"""
最小 ChatBI 演示入口（POC 阶段，非产品化）。

定位：
- 提供一个独立 HTML 页面 GET /chatbi，纯原生 fetch + NDJSON 流读取，
  零 npm / 零 vite build，便于直接演示 DataAgent + query_beelink_sql 链路。
- 不进入 DF React 体系，不读 UI store；表单字段由演示者临时填入（api_key 仅
  存在于浏览器内存），与 curl 路径完全等价。

WP-A 升级：把 NDJSON 流加工成业务方能看懂的卡片视图
- 顶部"问答区"：用户问题 + 进度时间线 + 关键产出卡（query_beelink_sql / explore / clarify / completion）
- 底部"调试区"：原始事件 JSON，默认折叠

不包含：会话历史持久化 / 图表渲染 / BO / RAG / 修复 Agent / SQL Guard。
"""
from __future__ import annotations

from flask import Blueprint, Response


chatbi_bp = Blueprint("chatbi", __name__)


# 单页 HTML：刻意保留在一个文件里，避免新增模板/静态资源目录。
# 仅依赖浏览器原生 fetch + ReadableStream，不引入任何外部脚本。
_CHATBI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>beelink ChatBI 演示 (POC)</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 1080px; margin: 16px auto; padding: 0 16px; line-height: 1.5; color: #1f2937; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: #6b7280; font-size: 13px; margin-bottom: 12px; }
  details > summary { cursor: pointer; user-select: none; }

  /* 表单 */
  fieldset { border: 1px solid #d1d5db; border-radius: 6px; padding: 10px 14px; margin-bottom: 10px; }
  legend { padding: 0 6px; font-weight: 600; font-size: 13px; color: #374151; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .row > label { flex: 1 1 200px; display: flex; flex-direction: column; gap: 3px; font-size: 12.5px; }
  input, select, textarea { font: inherit; padding: 5px 7px; border: 1px solid #cbd5e1; border-radius: 4px; background: #fff; color: #1f2937; }
  textarea { width: 100%; min-height: 56px; resize: vertical; }
  button { font: inherit; padding: 7px 16px; border-radius: 4px; border: 1px solid #2563eb;
           background: #2563eb; color: #fff; cursor: pointer; font-weight: 500; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .warn { color: #b45309; font-size: 11.5px; margin-top: 4px; }

  /* 演示区 */
  #stage { margin-top: 14px; }
  .qcard { border: 1px solid #d1d5db; border-radius: 8px; margin-bottom: 14px;
           background: #fff; overflow: hidden; }
  .qhead { padding: 10px 14px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; }
  .qhead .qlabel { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: .5px; }
  .qhead .qtext { font-size: 15px; color: #111827; margin-top: 2px; word-break: break-word; }
  .qbody { padding: 10px 14px; }

  /* 进度条 */
  .progress { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .pchip { font-size: 11.5px; padding: 3px 8px; border-radius: 999px;
           background: #eef2ff; color: #3730a3; border: 1px solid #c7d2fe; }
  .pchip.beelink { background: #ecfdf5; color: #065f46; border-color: #a7f3d0; }
  .pchip.explore { background: #fef3c7; color: #92400e; border-color: #fde68a; }
  .pchip.search  { background: #ede9fe; color: #5b21b6; border-color: #ddd6fe; }
  .pchip.inspect { background: #e0f2fe; color: #075985; border-color: #bae6fd; }
  .pchip.think   { background: #f3f4f6; color: #374151; border-color: #d1d5db; }

  /* 结果卡 */
  .card { border: 1px solid #e5e7eb; border-radius: 6px; margin: 8px 0; background: #fafafa; }
  .card-head { padding: 6px 10px; font-size: 13px; font-weight: 600; border-bottom: 1px solid #e5e7eb; }
  .card-head.ok { background: #ecfdf5; color: #065f46; }
  .card-head.reused { background: #f0f9ff; color: #075985; }
  .card-head.err { background: #fef2f2; color: #991b1b; }
  .card-head.clarify { background: #fff7ed; color: #9a3412; }
  .card-head.final { background: #eef2ff; color: #3730a3; }
  .card-body { padding: 8px 10px; font-size: 13px; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 12.5px; }
  .kv dt { color: #6b7280; }
  .kv dd { margin: 0; word-break: break-all; }
  pre.code { background: #1e293b; color: #e2e8f0; border-radius: 4px;
             padding: 8px 10px; font-size: 12px; overflow: auto; max-height: 220px;
             font-family: ui-monospace, "JetBrains Mono", Menlo, monospace; margin: 6px 0; }
  pre.txt { background: #f8fafc; color: #1f2937; border: 1px solid #e2e8f0;
            border-radius: 4px; padding: 8px 10px; font-size: 12px; overflow: auto;
            max-height: 220px; font-family: ui-monospace, Menlo, monospace; margin: 6px 0; white-space: pre; }
  table.preview { border-collapse: collapse; font-size: 12.5px; margin-top: 6px; }
  table.preview th, table.preview td { border: 1px solid #d1d5db; padding: 3px 8px; text-align: left; }
  table.preview th { background: #f3f4f6; }
  .muted { color: #6b7280; font-size: 12px; }

  /* 调试区 */
  #raw { margin-top: 12px; border: 1px dashed #cbd5e1; border-radius: 6px; padding: 8px 12px;
         background: #fafafa; font-size: 12px; }
  #raw pre { font-family: ui-monospace, Menlo, monospace; font-size: 11.5px;
             white-space: pre-wrap; max-height: 360px; overflow: auto; margin: 6px 0 0; }

  #status { color: #6b7280; font-size: 12.5px; margin-left: 10px; }
  .spinner { display: inline-block; width: 10px; height: 10px; border: 2px solid #93c5fd;
             border-top-color: transparent; border-radius: 50%;
             animation: spin .8s linear infinite; vertical-align: -1px; margin-right: 4px; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
  <h1>beelink ChatBI 演示 (POC)</h1>
  <div class="sub">
    复用 <code>/api/agent/data-agent-streaming</code> + DataAgent 第 8 个 tool <code>query_beelink_sql</code>。
    api_key 仅存浏览器内存；刷新即丢。
  </div>

  <form id="form">
    <fieldset>
      <legend>身份与 workspace</legend>
      <div class="row">
        <label>X-Identity-Id <input id="identity" value="local:beelink"></label>
        <label>X-Workspace-Id <input id="workspace" value="default"></label>
      </div>
    </fieldset>

    <fieldset>
      <legend>LLM</legend>
      <div class="row">
        <label>endpoint <input id="endpoint" value="openai"></label>
        <label>model
          <select id="model">
            <option value="deepseek-chat" selected>deepseek-chat（推荐）</option>
            <option value="deepseek-v4-pro">deepseek-v4-pro（不推荐，thinking 与 DataAgent loop 不兼容）</option>
            <option value="deepseek-v4-flash">deepseek-v4-flash（不推荐，同上）</option>
            <option value="deepseek-reasoner">deepseek-reasoner（不推荐，同上）</option>
          </select>
        </label>
        <label>api_base <input id="api_base" value="https://api.deepseek.com"></label>
      </div>
      <div class="row">
        <label>api_key（仅浏览器内存）
          <input id="api_key" type="password" autocomplete="off" placeholder="sk-...">
        </label>
      </div>
      <div class="warn" id="model_warn" style="display:none">
        ⚠️ thinking 模式与 DataAgent 主循环不兼容，通常报 400。
      </div>
    </fieldset>

    <fieldset>
      <legend>问题</legend>
      <textarea id="question">看下 smartquery_demo.customers 各 gender 客户数。</textarea>
    </fieldset>

    <button id="submit" type="submit">发送</button>
    <span id="status"></span>
  </form>

  <div id="stage"></div>

  <details id="raw">
    <summary>原始事件 JSON（调试用，默认折叠）</summary>
    <pre id="rawpre"></pre>
  </details>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var stageEl = $('stage');
  var rawPreEl = $('rawpre');
  var modelEl = $('model');
  var warnEl = $('model_warn');
  var statusEl = $('status');
  var submitBtn = $('submit');

  function updateWarn() {
    warnEl.style.display = modelEl.value === 'deepseek-chat' ? 'none' : 'block';
  }
  modelEl.addEventListener('change', updateWarn);
  updateWarn();

  // ---------- 工具：DOM 帮手 ----------
  function el(tag, opts) {
    var n = document.createElement(tag);
    if (opts) {
      if (opts.class) n.className = opts.class;
      if (opts.text != null) n.textContent = opts.text;
      if (opts.html != null) n.innerHTML = opts.html;
    }
    return n;
  }
  function kv(pairs) {
    var dl = el('dl', {class: 'kv'});
    pairs.forEach(function (p) {
      dl.appendChild(el('dt', {text: p[0]}));
      var dd = el('dd');
      if (p[1] instanceof Node) dd.appendChild(p[1]); else dd.textContent = String(p[1] == null ? '' : p[1]);
      dl.appendChild(dd);
    });
    return dl;
  }

  // ---------- 工具：解析 query_beelink_sql 的 stdout JSON ----------
  function parseBeelinkStdout(stdout) {
    try { return typeof stdout === 'string' ? JSON.parse(stdout) : stdout; }
    catch (e) { return null; }
  }

  // ---------- 工具：把 DataFrame print 文本尝试解析成 [[...], [...]] ----------
  // pandas 默认 print 形如 "  gender  customer_count\n0      女             989\n..."
  // 兼容失败时返回 null（不强解析）
  function tryParseDataFrameText(text) {
    if (!text || typeof text !== 'string') return null;
    var lines = text.replace(/\s+$/, '').split('\n').filter(function (l) { return l.length; });
    if (lines.length < 2 || lines.length > 200) return null;
    // 列名行：去 index 列，按 2+ 空格切
    var hdr = lines[0].trim().split(/\s{2,}/);
    if (hdr.length < 1 || hdr.length > 12) return null;
    var rows = [];
    for (var i = 1; i < lines.length; i++) {
      var parts = lines[i].trim().split(/\s{2,}/);
      // pandas 行首是 index，丢掉
      if (parts.length === hdr.length + 1) parts.shift();
      if (parts.length !== hdr.length) return null;
      rows.push(parts);
    }
    return { columns: hdr, rows: rows };
  }

  function renderTable(parsed) {
    var t = el('table', {class: 'preview'});
    var thead = el('thead'), trh = el('tr');
    parsed.columns.forEach(function (c) { trh.appendChild(el('th', {text: c})); });
    thead.appendChild(trh); t.appendChild(thead);
    var tbody = el('tbody');
    parsed.rows.slice(0, 50).forEach(function (r) {
      var tr = el('tr');
      r.forEach(function (v) { tr.appendChild(el('td', {text: v})); });
      tbody.appendChild(tr);
    });
    t.appendChild(tbody);
    return t;
  }

  // ---------- 进度芯片 ----------
  var TOOL_LABEL = {
    'query_beelink_sql': {label: '🗄️ beelink 执行 SQL', cls: 'beelink'},
    'explore':           {label: '🐍 Python 分析',       cls: 'explore'},
    'search_data_tables':{label: '🔍 查找数据表',         cls: 'search'},
    'inspect_source_data':{label:'🔎 检查数据',           cls: 'inspect'},
    'read_catalog_metadata':{label:'📚 读 catalog',       cls: 'inspect'},
    'search_knowledge':  {label: '📖 查知识',             cls: 'search'},
    'read_knowledge':    {label: '📖 读知识',             cls: 'search'}
  };

  // ---------- 会话状态机 ----------
  function newSession(question) {
    var card = el('div', {class: 'qcard'});
    var head = el('div', {class: 'qhead'});
    head.appendChild(el('div', {class: 'qlabel', text: 'YOU ASKED'}));
    head.appendChild(el('div', {class: 'qtext', text: question}));
    card.appendChild(head);
    var body = el('div', {class: 'qbody'});
    var progress = el('div', {class: 'progress'});
    body.appendChild(progress);
    var content = el('div', {class: 'qcontent'});
    body.appendChild(content);
    card.appendChild(body);
    stageEl.appendChild(card);
    return { card: card, body: body, progress: progress, content: content,
             counts: {}, lastExploreStdout: null, finalized: false };
  }

  function bumpProgress(sess, tool) {
    sess.counts[tool] = (sess.counts[tool] || 0) + 1;
    var existing = sess.progress.querySelector('[data-tool="' + tool + '"]');
    var meta = TOOL_LABEL[tool] || {label: '🔧 ' + tool, cls: ''};
    if (!existing) {
      var chip = el('span', {class: 'pchip ' + meta.cls});
      chip.dataset.tool = tool;
      chip.textContent = meta.label + ' ×' + sess.counts[tool];
      sess.progress.appendChild(chip);
    } else {
      existing.textContent = meta.label + ' ×' + sess.counts[tool];
    }
  }

  // ---------- 关键产出卡 ----------
  function renderBeelinkResult(sess, evt) {
    var parsed = parseBeelinkStdout(evt.stdout);
    var card = el('div', {class: 'card'});
    var ok = parsed && parsed.status === 'ok';
    var reused = !!(parsed && parsed.reused);
    var headCls = ok ? (reused ? 'reused' : 'ok') : 'err';
    var title = ok ? (reused ? '🗄️ beelink SQL（命中缓存，复用上次结果）' : '🗄️ beelink 执行 SQL · 成功')
                   : '🗄️ beelink 执行 SQL · 失败';
    card.appendChild(el('div', {class: 'card-head ' + headCls, text: title}));
    var body = el('div', {class: 'card-body'});
    if (parsed && ok) {
      var pairs = [
        ['表名', parsed.table_name || '(无)'],
        ['行数', String(parsed.row_count == null ? '-' : parsed.row_count)],
        ['列', (parsed.columns || []).map(function (c) { return c.name + (c.dtype ? '(' + c.dtype + ')' : ''); }).join(', ') || '(无)']
      ];
      if (reused) pairs.push(['复用', '是（同 SQL 已在本轮执行过）']);
      body.appendChild(kv(pairs));
      if (parsed.sql || parsed.source_query) {
        var det = el('details');
        det.appendChild(el('summary', {text: '查看 SQL'}));
        var pre = el('pre', {class: 'code', text: parsed.sql || parsed.source_query});
        det.appendChild(pre);
        body.appendChild(det);
      }
    } else {
      var errText = (parsed && parsed.errorMessage) || evt.error || evt.stdout || '(无错误信息)';
      body.appendChild(el('pre', {class: 'txt', text: String(errText).slice(0, 2000)}));
    }
    card.appendChild(body);
    sess.content.appendChild(card);
  }

  function renderExploreResult(sess, evt) {
    // 只挑"看起来像 DataFrame 输出"的成功 stdout 入卡，其余进调试区即可
    if (evt.status !== 'ok' || !evt.stdout) return;
    sess.lastExploreStdout = evt.stdout;  // 留一份给收尾时兜底
  }

  function renderClarify(sess, evt) {
    var card = el('div', {class: 'card'});
    card.appendChild(el('div', {class: 'card-head clarify', text: '❓ Agent 需要澄清'}));
    var body = el('div', {class: 'card-body'});
    if (evt.thought) body.appendChild(el('div', {class: 'muted', text: evt.thought}));
    (evt.questions || []).forEach(function (q) {
      var line = el('div', {text: '• ' + (q.text || JSON.stringify(q)).slice(0, 800)});
      body.appendChild(line);
    });
    card.appendChild(body);
    sess.content.appendChild(card);
  }

  function renderCompletion(sess, evt) {
    var card = el('div', {class: 'card'});
    card.appendChild(el('div', {class: 'card-head final', text: '💬 最终回答'}));
    var body = el('div', {class: 'card-body'});
    var c = evt.content;
    var text = '';
    if (typeof c === 'string') text = c;
    else if (c && typeof c === 'object') text = c.thought || c.answer || c.summary || JSON.stringify(c);
    body.appendChild(el('div', {text: text || '(无文本回答)'}));
    card.appendChild(body);
    sess.content.appendChild(card);
  }

  function renderThinking(sess, evt) {
    var txt = (evt.content || '').trim();
    if (!txt) return;
    var d = el('div', {class: 'muted', text: '💭 ' + txt});
    sess.content.appendChild(d);
  }

  function finalizeSession(sess) {
    if (sess.finalized) return;
    sess.finalized = true;
    // 兜底：如果 explore 最后一条 stdout 像 DataFrame 表格，单独入一张卡
    if (sess.lastExploreStdout) {
      var parsed = tryParseDataFrameText(sess.lastExploreStdout);
      if (parsed) {
        var card = el('div', {class: 'card'});
        card.appendChild(el('div', {class: 'card-head ok', text: '📊 Python 分析结果（来自 explore stdout）'}));
        var body = el('div', {class: 'card-body'});
        body.appendChild(renderTable(parsed));
        body.appendChild(el('div', {class: 'muted', text: '仅显示前 50 行；完整数据在 workspace。'}));
        card.appendChild(body);
        sess.content.appendChild(card);
      }
    }
  }

  // ---------- 事件分发 ----------
  function handleEvent(sess, evt) {
    var t = evt.type;
    if (t === 'thinking_text') {
      renderThinking(sess, evt);
    } else if (t === 'tool_start') {
      bumpProgress(sess, evt.tool || 'unknown');
    } else if (t === 'tool_result') {
      if (evt.tool === 'query_beelink_sql') renderBeelinkResult(sess, evt);
      else if (evt.tool === 'explore') renderExploreResult(sess, evt);
      // search_data_tables / inspect_source_data / read_catalog_metadata 默认只走进度芯片
    } else if (t === 'clarify') {
      renderClarify(sess, evt);
    } else if (t === 'completion' || t === 'result') {
      renderCompletion(sess, evt);
    } else if (t === 'error') {
      var card = el('div', {class: 'card'});
      card.appendChild(el('div', {class: 'card-head err', text: '✖ 错误'}));
      var body = el('div', {class: 'card-body'});
      body.appendChild(el('pre', {class: 'txt', text: JSON.stringify(evt, null, 2).slice(0, 2000)}));
      card.appendChild(body);
      sess.content.appendChild(card);
    }
    window.scrollTo(0, document.body.scrollHeight);
  }

  function setBusy(busy, msg) {
    submitBtn.disabled = busy;
    statusEl.innerHTML = (busy ? '<span class="spinner"></span>' : '') + (msg || '');
  }

  // ---------- 提交 ----------
  $('form').addEventListener('submit', async function (ev) {
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

    rawPreEl.textContent = '';
    setBusy(true, '请求中…');
    var sess = newSession(question);

    var body = {
      model: { endpoint: endpoint, model: model, api_key: api_key,
               api_base: api_base, api_version: null, is_global: false },
      input_tables: [], primary_tables: [], user_question: question
    };

    try {
      await fetch('/api/connectors', {
        method: 'GET',
        headers: { 'X-Identity-Id': identity, 'X-Workspace-Id': workspace }
      });
      var resp = await fetch('/api/agent/data-agent-streaming', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json',
                   'X-Identity-Id': identity, 'X-Workspace-Id': workspace },
        body: JSON.stringify(body)
      });
      if (!resp.ok) {
        var errText = await resp.text();
        handleEvent(sess, { type: 'error', status: resp.status, body: errText.slice(0, 1000) });
        setBusy(false, '失败 HTTP ' + resp.status);
        finalizeSession(sess);
        return;
      }
      var reader = resp.body.getReader();
      var dec = new TextDecoder('utf-8');
      var buf = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        var lines = buf.split('\n');
        buf = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i].trim();
          if (!line) continue;
          rawPreEl.textContent += line + '\n';
          try { handleEvent(sess, JSON.parse(line)); }
          catch (e) { /* 解析失败的行只入调试区 */ }
        }
      }
      if (buf.trim()) {
        rawPreEl.textContent += buf + '\n';
        try { handleEvent(sess, JSON.parse(buf)); } catch (e) {}
      }
      finalizeSession(sess);
      setBusy(false, '完成');
    } catch (e) {
      handleEvent(sess, { type: 'error', message: String(e && e.message || e) });
      finalizeSession(sess);
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
