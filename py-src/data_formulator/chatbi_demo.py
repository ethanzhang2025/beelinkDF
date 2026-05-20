# -*- coding: utf-8 -*-
"""
最小 ChatBI 演示入口（POC 阶段，非产品化）。

定位：
- GET /chatbi 返回单页 HTML，纯原生 fetch + NDJSON 流读取，零 npm / 零 vite build。
- 演示 DataAgent + query_beelink_sql 链路给业务方看。

WP-A v2：业务方可读视图
- 主区域只显：用户问题 / 进度芯片 / SQL（折叠）/ 表名 / 行数 / 表格 / 最小柱状图 / 最终回答
- 表格与柱状图来自 GET /api/tables/list-tables 的 sample_rows（同 DF UI 数据源）
- thinking_text 只计数不展示原文；tool_start.code/SYSTEM_PROMPT 等大文本不进主区
- 原始 NDJSON 全量放底部"调试详情"，默认折叠

不包含：会话历史持久化 / 复杂图表 / BO / RAG / 修复 Agent / SQL Guard。
"""
from __future__ import annotations

from flask import Blueprint, Response


chatbi_bp = Blueprint("chatbi", __name__)


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
  .qcard { border: 1px solid #d1d5db; border-radius: 8px; margin-bottom: 14px; background: #fff; overflow: hidden; }
  .qhead { padding: 10px 14px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; }
  .qhead .qlabel { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: .5px; }
  .qhead .qtext { font-size: 15px; color: #111827; margin-top: 2px; word-break: break-word; }
  .qbody { padding: 12px 14px; }

  /* 状态行：替代过去的多芯片，只显当前阶段 */
  .statusline { display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
                font-size: 13px; color: #374151; min-height: 22px; }
  .statusline .dot { display: inline-block; width: 10px; height: 10px;
                     border: 2px solid #93c5fd; border-top-color: transparent;
                     border-radius: 50%; animation: spin .8s linear infinite; }
  .statusline.done .dot { border: none; background: #16a34a; animation: none; }
  .statusline.fail .dot { border: none; background: #dc2626; animation: none; }

  /* 主结论 */
  .headline { font-size: 16px; color: #111827; font-weight: 600;
              padding: 8px 0 4px; line-height: 1.4; }
  .meta { color: #6b7280; font-size: 12px; }

  /* 图表卡 */
  .chartbox { margin-top: 10px; padding-top: 8px; border-top: 1px dashed #e5e7eb; }
  .chartbox .ctitle { font-size: 13px; font-weight: 600; color: #1f2937; margin: 2px 0 8px; }

  /* 结果卡 */
  .card { border: 1px solid #e5e7eb; border-radius: 6px; margin: 10px 0; background: #fafafa; }
  .card-head { padding: 7px 10px; font-size: 13px; font-weight: 600; border-bottom: 1px solid #e5e7eb; }
  .card-head.ok      { background: #ecfdf5; color: #065f46; }
  .card-head.reused  { background: #f0f9ff; color: #075985; }
  .card-head.err     { background: #fef2f2; color: #991b1b; }
  .card-head.clarify { background: #fff7ed; color: #9a3412; }
  .card-head.final   { background: #eef2ff; color: #3730a3; }
  .card-body { padding: 10px 12px; font-size: 13px; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: 12.5px; margin: 0; }
  .kv dt { color: #6b7280; }
  .kv dd { margin: 0; word-break: break-all; }
  pre.code { background: #1e293b; color: #e2e8f0; border-radius: 4px;
             padding: 8px 10px; font-size: 12px; overflow: auto; max-height: 220px;
             font-family: ui-monospace, "JetBrains Mono", Menlo, monospace; margin: 6px 0; }
  .muted { color: #6b7280; font-size: 12px; }

  /* 数据表格 */
  table.data { border-collapse: collapse; font-size: 12.5px; margin-top: 8px; min-width: 320px; }
  table.data th, table.data td { border: 1px solid #d1d5db; padding: 4px 10px; text-align: left; vertical-align: top; }
  table.data th { background: #f3f4f6; }
  table.data td.num { text-align: right; font-variant-numeric: tabular-nums; }

  /* 简单柱状图（CSS bar） */
  .barchart { margin-top: 12px; max-width: 560px; }
  .barchart .bar-row { display: grid; grid-template-columns: 110px 1fr 64px; gap: 8px;
                       align-items: center; margin: 4px 0; font-size: 12.5px; }
  .barchart .bar-label { color: #374151; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .barchart .bar-track { background: #f3f4f6; border-radius: 3px; height: 14px; position: relative; }
  .barchart .bar-fill { background: #2563eb; height: 100%; border-radius: 3px; }
  .barchart .bar-value { color: #1f2937; font-variant-numeric: tabular-nums; text-align: right; }
  .barchart .axis { font-size: 11px; color: #6b7280; margin-top: 6px; }

  /* 调试区 */
  details > summary { cursor: pointer; user-select: none; }
  #debug { margin-top: 12px; border: 1px dashed #cbd5e1; border-radius: 6px;
           padding: 8px 12px; background: #fafafa; font-size: 12px; }
  #debug pre { font-family: ui-monospace, Menlo, monospace; font-size: 11px;
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
  <div class="sub">DataAgent + <code>query_beelink_sql</code> 智能问数演示。api_key 仅存浏览器内存，刷新即丢。</div>

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
            <option value="deepseek-v4-pro">deepseek-v4-pro（不推荐）</option>
            <option value="deepseek-v4-flash">deepseek-v4-flash（不推荐）</option>
            <option value="deepseek-reasoner">deepseek-reasoner（不推荐）</option>
          </select>
        </label>
        <label>api_base <input id="api_base" value="https://api.deepseek.com"></label>
      </div>
      <div class="row">
        <label>api_key（仅浏览器内存）
          <input id="api_key" type="password" autocomplete="off" placeholder="sk-...">
        </label>
      </div>
      <div class="warn" id="model_warn" style="display:none">⚠️ thinking 模式与 DataAgent loop 不兼容，通常报 400。</div>
    </fieldset>

    <fieldset>
      <legend>问题</legend>
      <textarea id="question">看下 smartquery_demo.customers 各 gender 客户数。</textarea>
    </fieldset>

    <button id="submit" type="submit">发送</button>
    <span id="status"></span>
  </form>

  <div id="stage"></div>

  <details id="debug">
    <summary>调试详情（thinking_text / 原始事件 JSON）</summary>
    <pre id="debugpre"></pre>
  </details>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var stageEl = $('stage');
  var debugPreEl = $('debugpre');
  var modelEl = $('model');
  var warnEl = $('model_warn');
  var statusEl = $('status');
  var submitBtn = $('submit');

  modelEl.addEventListener('change', function () {
    warnEl.style.display = modelEl.value === 'deepseek-chat' ? 'none' : 'block';
  });

  // ---------- DOM helper ----------
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
  function debugLog(line) { debugPreEl.textContent += line + '\n'; }

  // ---------- 工具 → 状态行文案 ----------
  var TOOL_STATUS = {
    'query_beelink_sql':    '正在执行 beelink SQL',
    'explore':              '正在生成回答',
    'search_data_tables':   '正在查找数据表',
    'inspect_source_data':  '正在查找数据表',
    'read_catalog_metadata':'正在查找数据表',
    'search_knowledge':     '正在查找数据表',
    'read_knowledge':       '正在查找数据表'
  };

  // ---------- 类型判断 ----------
  function isNumber(v) { return typeof v === 'number' && isFinite(v); }
  function isNumericArr(arr) { return arr.length > 0 && arr.every(isNumber); }

  // ---------- 表格 ----------
  function renderTable(columns, rows, opts) {
    opts = opts || {};
    var t = el('table', {class: 'data'});
    var thead = el('thead'), trh = el('tr');
    columns.forEach(function (c) { trh.appendChild(el('th', {text: c})); });
    thead.appendChild(trh); t.appendChild(thead);
    var tbody = el('tbody');
    var shown = rows.slice(0, opts.limit || 50);
    shown.forEach(function (r) {
      var tr = el('tr');
      columns.forEach(function (c) {
        var v = r[c];
        var td = el('td', {text: v == null ? '' : String(v)});
        if (isNumber(v)) td.className = 'num';
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    t.appendChild(tbody);
    return { table: t, shown: shown.length, total: rows.length };
  }

  // ---------- 柱状图（纯 CSS / 不依赖外部库；调用方负责排序） ----------
  function renderBarChart(labels, values, opts) {
    opts = opts || {};
    var max = Math.max.apply(null, values);
    if (!isFinite(max) || max <= 0) max = 1;
    var wrap = el('div', {class: 'barchart'});
    labels.forEach(function (lab, i) {
      var v = values[i];
      var pct = Math.max(0, Math.min(100, (v / max) * 100));
      var row = el('div', {class: 'bar-row'});
      row.appendChild(el('div', {class: 'bar-label', text: String(lab)}));
      var track = el('div', {class: 'bar-track'});
      var fill = el('div', {class: 'bar-fill'});
      fill.style.width = pct.toFixed(1) + '%';
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('div', {class: 'bar-value', text: String(v)}));
      wrap.appendChild(row);
    });
    return wrap;
  }

  // ---------- 抓样例行 ----------
  // 复用 DF 自身 /api/tables/list-tables，确保数据与 DF UI 一致
  async function fetchSampleRows(identity, workspace, tableName) {
    try {
      var resp = await fetch('/api/tables/list-tables', {
        method: 'GET',
        headers: { 'X-Identity-Id': identity, 'X-Workspace-Id': workspace }
      });
      if (!resp.ok) return null;
      var json = await resp.json();
      var tables = (json && json.data && json.data.tables) || [];
      for (var i = 0; i < tables.length; i++) {
        if (tables[i].name === tableName) return tables[i];
      }
      return null;
    } catch (e) { return null; }
  }

  // ---------- 自动出图：单分类 + 单数值 → 柱状图（按数值降序）；否则只表格 ----------
  function maybeChartFromSample(sample) {
    if (!sample || !Array.isArray(sample.sample_rows) || sample.sample_rows.length === 0) return null;
    var cols = (sample.columns || []).map(function (c) { return c.name; });
    if (cols.length !== 2) return null;
    var rows = sample.sample_rows.map(function (r) { return [r[cols[0]], r[cols[1]]]; });
    var values = rows.map(function (p) { return p[1]; });
    if (!isNumericArr(values)) return null;
    if (rows.length > 30) return null;  // 太多类别就不画了
    rows.sort(function (a, b) { return b[1] - a[1]; });  // 按 cnt 降序
    return {
      labels: rows.map(function (p) { return p[0]; }),
      values: rows.map(function (p) { return p[1]; }),
      labelCol: cols[0], valueCol: cols[1]
    };
  }

  // ---------- 主结论文案：'女 989 人，男 975 人，未知 36 人。'  ----------
  function buildHeadline(chartCfg) {
    var unit = /cnt|count|num|人数|总数|总和/i.test(chartCfg.valueCol) ? ' 人' : '';
    var parts = chartCfg.labels.map(function (lab, i) {
      return String(lab) + ' ' + chartCfg.values[i] + unit;
    });
    return parts.join('，') + '。';
  }

  // ---------- 图表标题：'<labelCol> 分布' ----------
  function chartTitle(chartCfg) {
    return chartCfg.labelCol + ' 分布（按 ' + chartCfg.valueCol + ' 降序）';
  }

  // ---------- 业务卡：query_beelink_sql 成功 ----------
  async function renderBeelinkResult(sess, evt) {
    var parsed;
    try { parsed = typeof evt.stdout === 'string' ? JSON.parse(evt.stdout) : evt.stdout; }
    catch (e) { parsed = null; }
    var card = el('div', {class: 'card'});
    var ok = parsed && parsed.status === 'ok';
    var reused = !!(parsed && parsed.reused);
    if (!ok) {
      card.appendChild(el('div', {class: 'card-head err', text: '🗄️ beelink 执行 SQL · 失败'}));
      var body = el('div', {class: 'card-body'});
      body.appendChild(el('pre', {class: 'code', text: String(evt.error || evt.stdout || '').slice(0, 2000)}));
      card.appendChild(body);
      sess.content.appendChild(card);
      return;
    }
    var title = reused ? '🗄️ beelink SQL · 复用本轮已执行 SQL' : '🗄️ beelink 执行 SQL · 成功';
    card.appendChild(el('div', {class: 'card-head ' + (reused ? 'reused' : 'ok'), text: title}));
    var body = el('div', {class: 'card-body'});

    // 主结论占位（拿到 sample 后填）
    var headlineSlot = el('div', {class: 'headline', text: ''});
    body.appendChild(headlineSlot);

    // 元信息（小字）
    body.appendChild(el('div', {class: 'meta',
      text: '表名 ' + (parsed.table_name || '(无)') + ' · 行数 ' + (parsed.row_count == null ? '-' : parsed.row_count)}));

    // SQL 折叠（默认关）
    if (parsed.sql || parsed.source_query) {
      var det = el('details');
      det.appendChild(el('summary', {text: '查看执行 SQL'}));
      det.appendChild(el('pre', {class: 'code', text: parsed.sql || parsed.source_query}));
      body.appendChild(det);
    }

    // 表 / 图占位
    var dataSlot = el('div', {class: 'muted', text: '正在读取样例行…'});
    body.appendChild(dataSlot);
    card.appendChild(body);
    sess.content.appendChild(card);

    // 异步抓 sample_rows
    var sample = await fetchSampleRows(sess.identity, sess.workspace, parsed.table_name);
    dataSlot.innerHTML = '';
    if (!sample || !Array.isArray(sample.sample_rows) || sample.sample_rows.length === 0) {
      dataSlot.textContent = '未能读取样例行，可到 workspace 查看表「' + parsed.table_name + '」。';
      return;
    }

    var chartCfg = maybeChartFromSample(sample);
    if (chartCfg) {
      // 主结论 + 图表 + 表格
      headlineSlot.textContent = buildHeadline(chartCfg);
      var chartBox = el('div', {class: 'chartbox'});
      chartBox.appendChild(el('div', {class: 'ctitle', text: chartTitle(chartCfg)}));
      chartBox.appendChild(renderBarChart(chartCfg.labels, chartCfg.values));
      dataSlot.appendChild(chartBox);
    }
    // 表格（仍展示，业务方可对照原始行）
    var cols = (sample.columns || []).map(function (c) { return c.name; });
    var sortedRows = sample.sample_rows.slice();
    if (chartCfg) {
      // 表格也按降序对齐图表
      sortedRows.sort(function (a, b) { return b[chartCfg.valueCol] - a[chartCfg.valueCol]; });
    }
    var tbl = renderTable(cols, sortedRows, {limit: 50});
    var tableBox = el('div');
    tableBox.appendChild(el('div', {class: 'meta', text: '明细表'}));
    tableBox.appendChild(tbl.table);
    if (tbl.shown < tbl.total) {
      tableBox.appendChild(el('div', {class: 'muted', text: '仅显示前 ' + tbl.shown + ' / ' + tbl.total + ' 行。'}));
    }
    dataSlot.appendChild(tableBox);
  }

  // ---------- 业务卡：clarify / completion / error ----------
  function renderClarify(sess, evt) {
    var card = el('div', {class: 'card'});
    card.appendChild(el('div', {class: 'card-head clarify', text: '❓ Agent 需要澄清'}));
    var body = el('div', {class: 'card-body'});
    if (evt.thought) body.appendChild(el('div', {class: 'muted', text: evt.thought}));
    (evt.questions || []).forEach(function (q) {
      var txt = (q && q.text) || (typeof q === 'string' ? q : JSON.stringify(q));
      body.appendChild(el('div', {text: '• ' + String(txt).slice(0, 800)}));
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
    else if (c && typeof c === 'object') text = c.thought || c.answer || c.summary || '';
    // 防御：极少数情况下 content 是 prompt 级长 object，截到 1000 字符避免铺屏
    if (text.length > 1000) text = text.slice(0, 1000) + '…（已截断，完整内容见调试详情）';
    body.appendChild(el('div', {text: text || '(无文本回答，详见调试详情)'}));
    card.appendChild(body);
    sess.content.appendChild(card);
  }

  function renderError(sess, evt) {
    var card = el('div', {class: 'card'});
    card.appendChild(el('div', {class: 'card-head err', text: '✖ 错误'}));
    var body = el('div', {class: 'card-body'});
    body.appendChild(el('pre', {class: 'code', text: JSON.stringify(evt, null, 2).slice(0, 2000)}));
    card.appendChild(body);
    sess.content.appendChild(card);
  }

  // ---------- 状态行 ----------
  function setStatus(sess, text, kind) {
    sess.statusText.textContent = text;
    sess.status.classList.remove('done', 'fail');
    if (kind === 'done') sess.status.classList.add('done');
    else if (kind === 'fail') sess.status.classList.add('fail');
  }

  // ---------- 会话 ----------
  function newSession(question, identity, workspace) {
    var card = el('div', {class: 'qcard'});
    var head = el('div', {class: 'qhead'});
    head.appendChild(el('div', {class: 'qlabel', text: 'YOU ASKED'}));
    head.appendChild(el('div', {class: 'qtext', text: question}));
    card.appendChild(head);
    var body = el('div', {class: 'qbody'});
    var status = el('div', {class: 'statusline'});
    status.appendChild(el('span', {class: 'dot'}));
    var statusText = el('span', {text: '准备中…'});
    status.appendChild(statusText);
    body.appendChild(status);
    var content = el('div', {class: 'qcontent'});
    body.appendChild(content);
    card.appendChild(body);
    stageEl.appendChild(card);
    return { card: card, body: body, status: status, statusText: statusText,
             content: content, identity: identity, workspace: workspace };
  }

  // ---------- 事件分发 ----------
  async function handleEvent(sess, evt) {
    var t = evt.type;
    if (t === 'thinking_text') {
      setStatus(sess, 'Agent 正在分析');
      if (evt.content) debugLog('  [thinking] ' + String(evt.content).slice(0, 400));
    } else if (t === 'tool_start') {
      setStatus(sess, TOOL_STATUS[evt.tool] || ('正在执行 ' + (evt.tool || '工具')));
    } else if (t === 'tool_result') {
      if (evt.tool === 'query_beelink_sql') await renderBeelinkResult(sess, evt);
    } else if (t === 'clarify') {
      setStatus(sess, '需要澄清', 'done');
      renderClarify(sess, evt);
    } else if (t === 'completion' || t === 'result') {
      setStatus(sess, '完成', 'done');
      renderCompletion(sess, evt);
    } else if (t === 'error') {
      setStatus(sess, '出错', 'fail');
      renderError(sess, evt);
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

    debugPreEl.textContent = '';
    setBusy(true, '请求中…');
    var sess = newSession(question, identity, workspace);

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
        await handleEvent(sess, { type: 'error', status: resp.status, body: errText.slice(0, 1000) });
        setBusy(false, '失败 HTTP ' + resp.status);
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
          debugLog(line);
          try { await handleEvent(sess, JSON.parse(line)); } catch (e) {}
        }
      }
      if (buf.trim()) {
        debugLog(buf);
        try { await handleEvent(sess, JSON.parse(buf)); } catch (e) {}
      }
      setBusy(false, '完成');
    } catch (e) {
      await handleEvent(sess, { type: 'error', message: String(e && e.message || e) });
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
