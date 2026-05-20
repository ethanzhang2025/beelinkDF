# -*- coding: utf-8 -*-
"""
最小 ChatBI 演示入口（POC 阶段，非产品化）。

GET /chatbi             — 单页 HTML 演示
GET /chatbi/llm-defaults — 报告服务端 LLM key 是否就绪 + 默认 model（不返 key 明文）
POST /chatbi/agent-stream — 服务端代理：从 env 注入 api_key 转发到 /api/agent/data-agent-streaming

前端三类意图路由（都不调 LLM）：
  1. list_tables(source)         → /api/connectors/get-catalog-tree
  2. describe_table(source, t)   → /api/connectors/preview-data（展 columns）
  3. sample_table(source, t)     → /api/connectors/preview-data（展 rows）
其余分析问题 → /chatbi/agent-stream（DataAgent + query_beelink_sql）。

通用化：无任何表名/字段名的硬编码字典；table 列表/字段含义均来自运行时
catalog 与通用规则推测。
"""
from __future__ import annotations

import json as _json
import os
import urllib.request
import urllib.error
from pathlib import Path

import yaml
from flask import Blueprint, Response, request, jsonify


chatbi_bp = Blueprint("chatbi", __name__)

_SEMANTIC_DIR = Path(__file__).parent / "semantic"


def _load_semantic(source: str) -> dict:
    """加载 source 对应的轻语义 YAML；找不到/解析失败返 {}。"""
    if not source:
        return {}
    safe = source.replace("/", "_").replace("..", "_")
    path = _SEMANTIC_DIR / f"{safe}.yaml"
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _semantic_to_prompt(sem: dict) -> str:
    """把语义对象压成 LLM prompt 上下文字符串（紧凑、稳定，避免无关字段）。"""
    if not sem:
        return ""
    lines = []
    src = sem.get("source") or "?"
    lines.append("[Semantic context for source `{}`]".format(src))
    if sem.get("display_name") or sem.get("description"):
        lines.append("- domain: {}".format(sem.get("display_name") or ""))
        desc = (sem.get("description") or "").strip()
        if desc:
            lines.append("  " + desc.replace("\n", " ").strip())
    tables = sem.get("tables") or {}
    if tables:
        lines.append("- tables:")
        for t_name, t in tables.items():
            cols = (t or {}).get("columns") or {}
            col_brief = ", ".join(
                "{}({})".format(c, (cv or {}).get("display") or "")
                for c, cv in cols.items()
            )
            lines.append("  * {}: {} | pk={} | columns: {}".format(
                t_name, (t or {}).get("purpose") or "",
                (t or {}).get("primary_key") or "?", col_brief))
    joins = sem.get("joins") or []
    if joins:
        lines.append("- joins:")
        for j in joins:
            lines.append("  * {l}.{lk} = {r}.{rk}".format(
                l=j.get("left"), lk=j.get("left_key"),
                r=j.get("right"), rk=j.get("right_key")))
    metrics = sem.get("metrics") or []
    if metrics:
        lines.append("- metrics:")
        for m in metrics:
            lines.append("  * {n} = {e}  (on {t}; {d})".format(
                n=m.get("name"), e=m.get("expr"),
                t=m.get("table"), d=m.get("desc") or ""))
    typical = sem.get("typical_questions") or []
    if typical:
        lines.append("- typical_questions:")
        for tq in typical:
            lines.append("  * Q: {q} → {h}".format(
                q=tq.get("q"), h=tq.get("sql_hint") or ""))
    lines.append("[Rules]")
    lines.append("- Use ONLY the tables/columns above; do NOT invent.")
    lines.append("- Prefer direct SELECT via `query_beelink_sql`; only inspect catalog if a SELECT actually fails.")
    lines.append("- If joining, use the listed join keys.")
    lines.append("- If a needed field truly doesn't exist (e.g. amount missing), say so and substitute with COUNT(*).")
    return "\n".join(lines)


@chatbi_bp.route("/chatbi/semantic", methods=["GET"])
def chatbi_semantic():
    """返回 source 的精简语义上下文（前端注入 DataAgent prompt 用）。
    GET /chatbi/semantic?source=smartquery_demo
    Response: { has_semantic: bool, prompt: "...", raw: {...} }
    """
    source = (request.args.get("source") or "").strip()
    sem = _load_semantic(source)
    return jsonify({
        "has_semantic": bool(sem),
        "prompt": _semantic_to_prompt(sem),
        "raw": sem,
    })


def _server_api_key() -> str:
    """从服务端环境变量读 LLM key；仅供 /chatbi/agent-stream 内部注入用，不外泄。"""
    return os.environ.get("DEEPSEEK_API_KEY", "")


@chatbi_bp.route("/chatbi/llm-defaults", methods=["GET"])
def chatbi_llm_defaults():
    """报告服务端是否有 LLM key 与默认 model 配置；不返回 key 明文。"""
    return jsonify({
        "has_key": bool(_server_api_key()),
        "endpoint": "openai",
        "model": "deepseek-chat",
        "api_base": "https://api.deepseek.com",
    })


@chatbi_bp.route("/chatbi/agent-stream", methods=["POST"])
def chatbi_agent_stream():
    """服务端代理：从 env 注入 api_key，再以流式转发到 /api/agent/data-agent-streaming。
    前端只需发不含 api_key 的请求；浏览器侧从始至终不持有 key。"""
    body = request.get_json(silent=True) or {}
    api_key = _server_api_key()
    if not api_key:
        return jsonify({"error": "DEEPSEEK_API_KEY 未在服务端环境变量中设置"}), 503
    model = body.get("model") or {}
    if not isinstance(model, dict):
        return jsonify({"error": "invalid model field"}), 400
    model["api_key"] = api_key
    model.setdefault("endpoint", "openai")
    model.setdefault("api_base", "https://api.deepseek.com")
    model.setdefault("api_version", None)
    model.setdefault("is_global", False)
    body["model"] = model

    upstream_url = request.host_url.rstrip("/") + "/api/agent/data-agent-streaming"
    req = urllib.request.Request(
        upstream_url, data=_json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    for h in ("X-Identity-Id", "X-Workspace-Id"):
        v = request.headers.get(h)
        if v:
            req.add_header(h, v)

    try:
        upstream = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code,
                        mimetype=e.headers.get("Content-Type", "application/json"))
    except urllib.error.URLError as e:
        return jsonify({"error": "upstream URLError: " + str(e.reason)}), 502

    def relay():
        try:
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    return Response(relay(),
                    mimetype=upstream.headers.get("Content-Type", "application/x-ndjson"))


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
  .topbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 4px; }
  .topbar a.df-entry { font-size: 13px; color: #2563eb; text-decoration: none; border: 1px solid #2563eb; padding: 4px 10px; border-radius: 4px; background: #fff; }
  .topbar a.df-entry:hover { background: #eff6ff; }

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

  .statusline { display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
                font-size: 13px; color: #374151; min-height: 22px; }
  .statusline .dot { display: inline-block; width: 10px; height: 10px;
                     border: 2px solid #93c5fd; border-top-color: transparent;
                     border-radius: 50%; animation: spin .8s linear infinite; }
  .statusline.done .dot { border: none; background: #16a34a; animation: none; }
  .statusline.fail .dot { border: none; background: #dc2626; animation: none; }

  .headline { font-size: 16px; color: #111827; font-weight: 600;
              padding: 8px 0 4px; line-height: 1.4; }
  .meta { color: #6b7280; font-size: 12px; }
  .chartbox { margin-top: 10px; padding-top: 8px; border-top: 1px dashed #e5e7eb; }
  .chartbox .ctitle { font-size: 13px; font-weight: 600; color: #1f2937; margin: 2px 0 8px; }

  #stage { margin-top: 14px; }
  .qcard { border: 1px solid #d1d5db; border-radius: 8px; margin-bottom: 14px; background: #fff; overflow: hidden; }
  .qhead { padding: 10px 14px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; }
  .qhead .qlabel { font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: .5px; }
  .qhead .qtext { font-size: 15px; color: #111827; margin-top: 2px; word-break: break-word; }
  .qbody { padding: 12px 14px; }
  .card { border: 1px solid #e5e7eb; border-radius: 6px; margin: 10px 0; background: #fafafa; }
  .card-head { padding: 7px 10px; font-size: 13px; font-weight: 600; border-bottom: 1px solid #e5e7eb; }
  .card-head.ok      { background: #ecfdf5; color: #065f46; }
  .card-head.reused  { background: #f0f9ff; color: #075985; }
  .card-head.err     { background: #fef2f2; color: #991b1b; }
  .card-head.clarify { background: #fff7ed; color: #9a3412; }
  .card-head.final   { background: #eef2ff; color: #3730a3; }
  .card-body { padding: 10px 12px; font-size: 13px; }
  pre.code { background: #1e293b; color: #e2e8f0; border-radius: 4px;
             padding: 8px 10px; font-size: 12px; overflow: auto; max-height: 220px;
             font-family: ui-monospace, Menlo, monospace; margin: 6px 0; }

  table.data { border-collapse: collapse; font-size: 12.5px; margin-top: 8px; min-width: 320px; }
  table.data th, table.data td { border: 1px solid #d1d5db; padding: 4px 10px; text-align: left; vertical-align: top; }
  table.data th { background: #f3f4f6; }
  table.data td.num { text-align: right; font-variant-numeric: tabular-nums; }

  .barchart { margin-top: 12px; max-width: 560px; }
  .barchart .bar-row { display: grid; grid-template-columns: 110px 1fr 64px; gap: 8px;
                       align-items: center; margin: 4px 0; font-size: 12.5px; }
  .barchart .bar-label { color: #374151; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .barchart .bar-track { background: #f3f4f6; border-radius: 3px; height: 14px; }
  .barchart .bar-fill { background: #2563eb; height: 100%; border-radius: 3px; }
  .barchart .bar-value { color: #1f2937; font-variant-numeric: tabular-nums; text-align: right; }

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
  <div class="topbar">
    <h1>beelink ChatBI 演示 (POC)</h1>
    <a class="df-entry" href="/" title="不经问数直接进入 Data Formulator 原生分析页">→ 数据分析</a>
  </div>
  <div class="sub">DataAgent + <code>query_beelink_sql</code> 智能问数。api_key 由服务端管，浏览器不持有。</div>

  <form id="form">
    <fieldset>
      <legend>数据源</legend>
      <div class="row">
        <label>connector_id <input id="connector_id" value="beelink:beelink-main"></label>
        <label>source（schema） <input id="source" value="smartquery_demo"></label>
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
        <label>api_key（服务端无 env 时填）
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
  // ========================================================================
  // 基础 DOM / 状态
  // ========================================================================
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

  // 运行时缓存：第一次 list_tables 后填充，供后续意图解析表名用
  var state = { tables: [] };

  // 启动时探测：服务端是否已配 LLM key —— 若有则隐藏 api_key 字段
  var SERVER_HAS_KEY = false;
  (async function () {
    try {
      var r = await fetch('/chatbi/llm-defaults');
      if (!r.ok) return;
      var d = await r.json();
      if (d && d.has_key) {
        SERVER_HAS_KEY = true;
        var lbl = $('api_key').parentElement;
        if (lbl) lbl.style.display = 'none';
      }
    } catch (e) {}
  })();

  function el(tag, opts) {
    var n = document.createElement(tag);
    if (opts) {
      if (opts.class) n.className = opts.class;
      if (opts.text != null) n.textContent = opts.text;
    }
    return n;
  }
  function debugLog(line) { debugPreEl.textContent += line + '\n'; }
  function isNumber(v) { return typeof v === 'number' && isFinite(v); }
  function isNumericArr(arr) { return arr.length > 0 && arr.every(isNumber); }

  // ========================================================================
  // 通用：表格 / 柱状图
  // ========================================================================
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
  function renderBarChart(labels, values) {
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
  // 通用图表检测：任意 "单分类列 + 单数值列" → 柱状图（不针对特定列名）
  function maybeChart(columns, rows) {
    if (columns.length !== 2 || rows.length === 0 || rows.length > 30) return null;
    var pairs = rows.map(function (r) { return [r[columns[0]], r[columns[1]]]; });
    var values = pairs.map(function (p) { return p[1]; });
    if (!isNumericArr(values)) return null;
    pairs.sort(function (a, b) { return b[1] - a[1]; });
    return {
      labels: pairs.map(function (p) { return p[0]; }),
      values: pairs.map(function (p) { return p[1]; }),
      labelCol: columns[0], valueCol: columns[1]
    };
  }
  // headline 单位推测：根据数值列名（典型来自 SUM/COUNT 的 alias）猜中文量词
  // 未命中返空字符串，宁可不加也不要错配（如订单数加"人"）
  function inferUnit(valueCol) {
    if (!valueCol) return '';
    var v = String(valueCol).toLowerCase();
    if (/(customer_count|customer_cnt|user_count|user_cnt|people_count|客户数|用户数|人数)/.test(v)) return ' 人';
    if (/(order_count|order_cnt|orders|订单数|单数|笔数)/.test(v)) return ' 单';
    if (/(amount|sales|revenue|gmv|金额|销售额|营收)/.test(v)) return ' 元';
    if (/(quantity|qty|件数|数量)/.test(v)) return ' 件';
    return '';
  }

  // 通用字段含义推测（无表名/字段名硬编码）
  function inferColumnPurpose(name) {
    var n = String(name || '').toLowerCase();
    if (n === 'id' || /_id$/.test(n)) return 'ID';
    if (/^(name|title|label)$/.test(n) || /_name$/.test(n)) return '名称';
    if (/(_at|_time|_date|_ts|date|time)$/.test(n)) return '日期时间';
    if (/(amount|price|cost|qty|count|total|sum|avg|num)/.test(n)) return '数值指标';
    if (/^(desc|description|remark|note)$/.test(n)) return '描述';
    if (/(_type|_status|_kind|_category)$/.test(n) || /^type$|^status$/.test(n)) return '类型/状态';
    return '待确认';
  }

  // ========================================================================
  // 状态行 / 会话卡 / 通用渲染
  // ========================================================================
  function setStatus(sess, text, kind) {
    sess.statusText.textContent = text;
    sess.status.classList.remove('done', 'fail');
    if (kind === 'done') sess.status.classList.add('done');
    else if (kind === 'fail') sess.status.classList.add('fail');
  }
  function newSession(question, cfg) {
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
    return Object.assign({}, cfg, {
      card: card, status: status, statusText: statusText, content: content,
      hasSuccess: false, questionText: question
    });
  }
  function renderError(sess, msg) {
    var card = el('div', {class: 'card'});
    card.appendChild(el('div', {class: 'card-head err', text: '✖ 错误'}));
    var body = el('div', {class: 'card-body'});
    body.appendChild(el('pre', {class: 'code', text: String(msg).slice(0, 2000)}));
    card.appendChild(body);
    sess.content.appendChild(card);
  }
  // 通用：单结果卡（标题 + headline + meta + 表格 + 可选图表 + 可选 SQL 折叠）
  // opts.openInDfTableName 非空时，在卡片末尾追加"在 DF 原生界面继续分析"按钮，
  // 链接到 /?table=<encoded>，由 DataFormulator.tsx 消费 URL 参数把表挂进 store。
  function renderResultCard(sess, opts) {
    var card = el('div', {class: 'card'});
    card.appendChild(el('div', {class: 'card-head ok', text: opts.title}));
    var body = el('div', {class: 'card-body'});
    if (opts.headline) body.appendChild(el('div', {class: 'headline', text: opts.headline}));
    if (opts.meta) body.appendChild(el('div', {class: 'meta', text: opts.meta}));
    if (opts.sql) {
      var det = el('details');
      det.appendChild(el('summary', {text: '查看执行 SQL'}));
      det.appendChild(el('pre', {class: 'code', text: opts.sql}));
      body.appendChild(det);
    }
    if (opts.rows && opts.columns) {
      var chartCfg = opts.chart || maybeChart(opts.columns, opts.rows);
      if (chartCfg) {
        var chartBox = el('div', {class: 'chartbox'});
        chartBox.appendChild(el('div', {class: 'ctitle',
          text: chartCfg.labelCol + ' 分布（按 ' + chartCfg.valueCol + ' 降序）'}));
        chartBox.appendChild(renderBarChart(chartCfg.labels, chartCfg.values));
        body.appendChild(chartBox);
      }
      var sortedRows = opts.rows.slice();
      if (chartCfg) sortedRows.sort(function (a, b) { return b[chartCfg.valueCol] - a[chartCfg.valueCol]; });
      var tbl = renderTable(opts.columns, sortedRows, {limit: opts.limit || 50});
      body.appendChild(el('div', {class: 'meta', text: opts.tableLabel || '明细表'}));
      body.appendChild(tbl.table);
      if (tbl.shown < tbl.total) {
        body.appendChild(el('div', {class: 'meta',
          text: '仅显示前 ' + tbl.shown + ' / ' + tbl.total + ' 行。'}));
      }
    }
    if (opts.openInDfTableName) {
      body.appendChild(renderOpenInDfLink(opts.openInDfTableName, sess.workspace));
    }
    card.appendChild(body);
    sess.content.appendChild(card);
  }
  // ChatBI 结果表 → DF 原生界面：跳 /?ws=<workspace>&table=<encoded>
  // 同时带 ws 让主页自动切到 chatbi 当前 workspace（默认 default），消费完两个参数都会被清理。
  function renderOpenInDfLink(tableName, workspace) {
    var wrap = el('div', {class: 'meta'});
    var a = el('a', {text: '在 DF 原生界面继续分析'});
    var ws = workspace || 'default';
    a.href = '/?ws=' + encodeURIComponent(ws) + '&table=' + encodeURIComponent(tableName);
    a.style.cssText = 'display:inline-block;margin-top:6px;padding:4px 10px;'
      + 'border:1px solid #1976d2;border-radius:4px;color:#1976d2;'
      + 'text-decoration:none;font-size:13px;';
    wrap.appendChild(a);
    return wrap;
  }

  // ========================================================================
  // 三类元数据意图：list_tables / describe_table / sample_table
  // ========================================================================
  function isListTablesIntent(q) {
    if (!q) return false;
    if (/字段|列名|有哪些列|表结构|schema/i.test(q)) return false;
    var hasTableWord = /表/.test(q) || /\btables?\b/i.test(q);
    if (!hasTableWord) return false;
    return /有哪些|哪些|列出|清单|有什么|什么表|多少张|多少个|多少表|几张|几个表|how\s*many|list\s*tables|table\s*list/i.test(q);
  }
  function isDescribeTableIntent(q) {
    if (!q) return false;
    return /字段|列名|有哪些列|表结构|schema|describe/i.test(q);
  }
  function isSampleTableIntent(q) {
    if (!q) return false;
    return /前\s*\d*\s*行|样例|示例数据|示例行|预览数据|\bsample\b|\bpreview\b/i.test(q);
  }
  // 通用表名提取：先用 source.<name> 模式；否则在运行时缓存的 state.tables 内匹配
  function extractTableName(q, source) {
    var srcPattern = new RegExp(source.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\.([A-Za-z_][A-Za-z0-9_]*)');
    var m = q.match(srcPattern);
    if (m) return m[1];
    for (var i = 0; i < state.tables.length; i++) {
      var name = state.tables[i];
      if (new RegExp('(^|[^A-Za-z0-9_])' + name + '($|[^A-Za-z0-9_])', 'i').test(q)) return name;
    }
    return null;
  }

  // ---- list_tables: catalog-tree ----
  async function fetchSourceTables(sess) {
    var resp = await fetch('/api/connectors/get-catalog-tree', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace },
      body: JSON.stringify({ connector_id: sess.connectorId })
    });
    if (!resp.ok) throw new Error('get-catalog-tree HTTP ' + resp.status);
    var json = await resp.json();
    var tree = (json && json.data && json.data.tree) || [];
    for (var i = 0; i < tree.length; i++) {
      if (tree[i].name === sess.source) {
        return (tree[i].children || []).map(function (c) { return c.name; });
      }
    }
    return [];
  }
  async function runListTables(sess) {
    setStatus(sess, '查询 catalog…');
    try {
      await fetch('/api/connectors', {
        method: 'GET',
        headers: { 'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace }
      });
      var tables = await fetchSourceTables(sess);
      state.tables = tables.slice();   // 运行时缓存供后续意图解析表名
      var rows = tables.map(function (n) { var r = {}; r['表名'] = n; return r; });
      renderResultCard(sess, {
        title: '📚 ' + sess.source + ' 表清单',
        headline: sess.source + ' 当前共有 ' + tables.length + ' 张表。',
        meta: '来源 connector ' + sess.connectorId + ' · 走 /api/connectors/get-catalog-tree（不经 DataAgent）',
        columns: ['表名'], rows: rows, limit: 200, tableLabel: '表名清单'
      });
      sess.hasSuccess = true;
      setStatus(sess, '完成', 'done');
    } catch (e) {
      setStatus(sess, '出错', 'fail');
      renderError(sess, String(e && e.message || e));
    }
  }

  // ---- describe_table & sample_table: 都走 preview-data ----
  async function fetchPreview(sess, table) {
    await fetch('/api/connectors', {
      method: 'GET',
      headers: { 'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace }
    });
    var resp = await fetch('/api/connectors/preview-data', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
                 'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace },
      body: JSON.stringify({
        connector_id: sess.connectorId,
        source_table: sess.source + '.' + table
      })
    });
    if (!resp.ok) throw new Error('preview-data HTTP ' + resp.status);
    var json = await resp.json();
    var data = (json && json.data) || {};
    return {
      columns: data.columns || [],
      rows: data.rows || [],
      total_row_count: data.total_row_count
    };
  }
  async function runDescribeTable(sess, table) {
    setStatus(sess, '查询字段…');
    try {
      var pv = await fetchPreview(sess, table);
      if (!pv.columns.length) throw new Error('未取到字段');
      var src = sess.source + '.' + table;
      var rows = pv.columns.map(function (c) {
        var r = {};
        r['字段名'] = c.name;
        r['类型'] = (c.type || '?') + (c.source_type ? ' (' + c.source_type + ')' : '');
        r['推测含义'] = inferColumnPurpose(c.name);
        return r;
      });
      renderResultCard(sess, {
        title: '📋 ' + src + ' 字段清单',
        headline: src + ' 共 ' + pv.columns.length + ' 个字段。',
        meta: '走 /api/connectors/preview-data（不经 DataAgent）· 含义为通用规则推测',
        columns: ['字段名', '类型', '推测含义'], rows: rows, limit: 200
      });
      sess.hasSuccess = true;
      setStatus(sess, '完成', 'done');
    } catch (e) {
      setStatus(sess, '出错', 'fail');
      renderError(sess, String(e && e.message || e));
    }
  }
  async function runSampleTable(sess, table) {
    setStatus(sess, '查询样例…');
    try {
      var pv = await fetchPreview(sess, table);
      if (!pv.rows.length) throw new Error('未取到样例行');
      var src = sess.source + '.' + table;
      var cols = pv.columns.map(function (c) { return c.name; });
      renderResultCard(sess, {
        title: '📄 ' + src + ' 样例数据',
        headline: src + ' 样例 ' + pv.rows.length + ' 行（共 ' + (pv.total_row_count || '?') + ' 行）。',
        meta: '走 /api/connectors/preview-data（不经 DataAgent）',
        columns: cols, rows: pv.rows, limit: 50
      });
      sess.hasSuccess = true;
      setStatus(sess, '完成', 'done');
    } catch (e) {
      setStatus(sess, '出错', 'fail');
      renderError(sess, String(e && e.message || e));
    }
  }

  // ========================================================================
  // 第 4 类：simple_aggregate —— "按 X 统计/分组/分布" 通用聚合（不调 LLM）
  // ========================================================================
  function isSimpleAggregateIntent(q) {
    if (!q) return false;
    return /按\s*[A-Za-z_][A-Za-z0-9_]*\s*(?:统计|分组|分布|分类)/.test(q)
        || /[A-Za-z_][A-Za-z0-9_]*\s*(?:分布|占比)/.test(q)
        || /(?:统计|聚合).{0,4}[A-Za-z_][A-Za-z0-9_]*\s*(?:数量|数|count)/i.test(q);
  }
  function extractGroupByField(q) {
    // "按 <field> 统计/分组/分布/分类"
    var m = q.match(/按\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:统计|分组|分布|分类)/);
    if (m) return m[1];
    // "<field> 分布/占比"
    m = q.match(/([A-Za-z_][A-Za-z0-9_]*)\s*(?:分布|占比)/);
    if (m) return m[1];
    // "按 <field>" 兜底
    m = q.match(/按\s*([A-Za-z_][A-Za-z0-9_]*)/);
    if (m) return m[1];
    return null;
  }
  // 给 LLM-free 路径用：拿 source 下所有表（带运行时缓存）
  async function ensureTables(sess) {
    if (state.tables.length > 0) return state.tables;
    var tables = await fetchSourceTables(sess);
    state.tables = tables.slice();
    return tables;
  }
  // 检索"含字段 field 的表名列表"——并发对每张表调 preview-data 看 columns
  async function findTablesContainingField(sess, field) {
    var tables = await ensureTables(sess);
    var probes = tables.map(function (t) {
      return fetchPreview(sess, t)
        .then(function (pv) {
          var hit = (pv.columns || []).some(function (c) { return c.name === field; });
          return hit ? t : null;
        })
        .catch(function () { return null; });
    });
    var settled = await Promise.all(probes);
    return settled.filter(function (x) { return !!x; });
  }
  async function runSimpleAggregate(sess, field) {
    setStatus(sess, '查找含字段「' + field + '」的表…');
    try {
      await fetch('/api/connectors', {
        method: 'GET',
        headers: { 'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace }
      });
      var hits = await findTablesContainingField(sess, field);
      if (hits.length === 0) {
        setStatus(sess, '未找到字段', 'fail');
        renderError(sess, '当前 source `' + sess.source + '` 下未找到字段「' + field + '」。');
        return;
      }
      if (hits.length > 1) {
        var card = el('div', {class: 'card'});
        card.appendChild(el('div', {class: 'card-head clarify',
          text: '❓ 字段「' + field + '」出现在多张表'}));
        var body = el('div', {class: 'card-body'});
        body.appendChild(el('div', {text: '请在问题里指定表名，例如：按 ' + field + ' 统计 ' + hits[0] + ' 表中数量。'}));
        body.appendChild(el('div', {class: 'meta', text: '候选表：' + hits.join(', ')}));
        card.appendChild(body); sess.content.appendChild(card);
        setStatus(sess, '需要澄清', 'done');
        return;
      }

      var table = hits[0];
      var sql = 'SELECT "' + field + '", COUNT(*) AS cnt FROM "' + sess.source + '"."' + table + '" GROUP BY "' + field + '" ORDER BY cnt DESC';
      setStatus(sess, '在 beelink 执行聚合 SQL…');
      // 用 POC-1.5 端点 /api/connectors/import-sql 跑聚合并落 workspace
      // table_name 取确定性命名，便于复用排查；workspace 自动 _2/_3 后缀去重
      var outName = '__agg_' + sess.source + '_' + table + '_by_' + field;
      var resp = await fetch('/api/connectors/import-sql', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json',
                   'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace },
        body: JSON.stringify({
          connector_id: sess.connectorId,
          sql: sql,
          table_name: outName,
          import_options: { size: 10000 }
        })
      });
      if (!resp.ok) {
        var bodyText = await resp.text();
        throw new Error('import-sql HTTP ' + resp.status + ': ' + bodyText.slice(0, 300));
      }
      var json = await resp.json();
      var data = (json && json.data) || {};
      var savedName = data.table_name || outName;
      // 从 list-tables 拿 sample_rows（含真实数据）
      var ltResp = await fetch('/api/tables/list-tables', {
        method: 'GET',
        headers: { 'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace }
      });
      var ltJson = await ltResp.json();
      var tables2 = (ltJson && ltJson.data && ltJson.data.tables) || [];
      var found = tables2.find(function (t) { return t.name === savedName; });
      var rows = (found && found.sample_rows) || [];
      var cols = ((found && found.columns) || []).map(function (c) { return c.name; });
      if (!rows.length) throw new Error('未读到聚合结果样例行');

      // 条目化 headline：从原问题文本推单位（"客户数"→人 / "订单数"→单 / ...）
      // cnt 列名本身没有语义，所以单位推测以问题文本为准
      var unit = inferUnit(sess.questionText || '') || inferUnit(field) || '';
      var top = rows.slice(0, 10);
      var headlineText = top.map(function (r) {
        return String(r[field]) + ' ' + r['cnt'] + unit;
      }).join('，') + '。';

      renderResultCard(sess, {
        title: '📊 按 ' + field + ' 统计数量',
        headline: headlineText,
        meta: '自动选择表：' + sess.source + '.' + table + ' · 共 ' + rows.length + ' 个分组 · 走 /api/connectors/import-sql（不经 DataAgent）',
        sql: sql,
        columns: cols, rows: rows, limit: 200,
        openInDfTableName: savedName
      });
      sess.hasSuccess = true;
      setStatus(sess, '完成', 'done');
    } catch (e) {
      setStatus(sess, '出错', 'fail');
      renderError(sess, String(e && e.message || e));
    }
  }

  // ========================================================================
  // DataAgent 分支：分析问题
  // ========================================================================
  // 工具 → 状态行文案
  var TOOL_STATUS = {
    'query_beelink_sql':    '正在执行 beelink SQL',
    'explore':              '正在生成回答',
    'search_data_tables':   '正在查找数据表',
    'inspect_source_data':  '正在查找数据表',
    'read_catalog_metadata':'正在查找数据表',
    'search_knowledge':     '正在查找数据表',
    'read_knowledge':       '正在查找数据表'
  };
  async function renderBeelinkResult(sess, evt) {
    var parsed;
    try { parsed = typeof evt.stdout === 'string' ? JSON.parse(evt.stdout) : evt.stdout; }
    catch (e) { parsed = null; }
    var ok = parsed && parsed.status === 'ok';
    if (!ok) { debugLog('  [query_beelink_sql failed] ' + String(evt.error || evt.stdout || '').slice(0, 400)); return; }
    sess.hasSuccess = true;
    var reused = !!parsed.reused;

    // 拿到结果后异步拉 workspace sample_rows 渲染表格 / 图表
    var card = el('div', {class: 'card'});
    card.appendChild(el('div', {class: 'card-head ' + (reused ? 'reused' : 'ok'),
      text: reused ? '🗄️ beelink SQL · 复用本轮已执行 SQL' : '🗄️ beelink 执行 SQL · 成功'}));
    var body = el('div', {class: 'card-body'});
    var headlineSlot = el('div', {class: 'headline'});
    body.appendChild(headlineSlot);
    body.appendChild(el('div', {class: 'meta',
      text: '表名 ' + (parsed.table_name || '(无)') + ' · 行数 ' + (parsed.row_count == null ? '-' : parsed.row_count)}));
    if (parsed.sql || parsed.source_query) {
      var det = el('details');
      det.appendChild(el('summary', {text: '查看执行 SQL'}));
      det.appendChild(el('pre', {class: 'code', text: parsed.sql || parsed.source_query}));
      body.appendChild(det);
    }
    var dataSlot = el('div', {class: 'meta', text: '正在读取样例行…'});
    body.appendChild(dataSlot);
    card.appendChild(body);
    sess.content.appendChild(card);

    // 从 /api/tables/list-tables 找该 table_name 的 sample_rows
    try {
      var resp = await fetch('/api/tables/list-tables', {
        method: 'GET',
        headers: { 'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace }
      });
      var json = await resp.json();
      var tables = (json && json.data && json.data.tables) || [];
      var found = null;
      for (var i = 0; i < tables.length; i++) {
        if (tables[i].name === parsed.table_name) { found = tables[i]; break; }
      }
      dataSlot.innerHTML = '';
      if (!found || !Array.isArray(found.sample_rows) || found.sample_rows.length === 0) {
        dataSlot.textContent = '未能读取样例行（可去 workspace 查看「' + parsed.table_name + '」）。';
        return;
      }
      var cols = (found.columns || []).map(function (c) { return c.name; });
      var chartCfg = maybeChart(cols, found.sample_rows);
      if (chartCfg) {
        // 单位按 valueCol 语义推测；未命中 → 不加单位，避免"订单数 2041 人"这种错配
        var unit = inferUnit(chartCfg.valueCol);
        headlineSlot.textContent = chartCfg.labels.map(function (lab, i) {
          return String(lab) + ' ' + chartCfg.values[i] + unit;
        }).join('，') + '。';
        var chartBox = el('div', {class: 'chartbox'});
        chartBox.appendChild(el('div', {class: 'ctitle',
          text: chartCfg.labelCol + ' 分布（按 ' + chartCfg.valueCol + ' 降序）'}));
        chartBox.appendChild(renderBarChart(chartCfg.labels, chartCfg.values));
        dataSlot.appendChild(chartBox);
      }
      var sortedRows = found.sample_rows.slice();
      if (chartCfg) sortedRows.sort(function (a, b) { return b[chartCfg.valueCol] - a[chartCfg.valueCol]; });
      var tbl = renderTable(cols, sortedRows, {limit: 50});
      dataSlot.appendChild(el('div', {class: 'meta', text: '明细表'}));
      dataSlot.appendChild(tbl.table);
      if (tbl.shown < tbl.total) {
        dataSlot.appendChild(el('div', {class: 'meta', text: '仅显示前 ' + tbl.shown + ' / ' + tbl.total + ' 行。'}));
      }
      if (parsed.table_name) dataSlot.appendChild(renderOpenInDfLink(parsed.table_name, sess.workspace));
    } catch (e) { dataSlot.textContent = '读取样例行失败：' + String(e && e.message || e); }
  }
  function renderClarify(sess, evt) {
    var card = el('div', {class: 'card'});
    card.appendChild(el('div', {class: 'card-head clarify', text: '❓ Agent 需要澄清'}));
    var body = el('div', {class: 'card-body'});
    if (evt.thought) body.appendChild(el('div', {class: 'meta', text: evt.thought}));
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
    // 本轮若已有结构化 SQL/路由结果（hasSuccess=true），LLM 自由文本可能与
    // 表格/图表数字不一致（幻觉）。展示层直接降级为固定中文指引，原始
    // LLM 文本通过 debugLog 保留在调试详情里以便排查。
    if (sess.hasSuccess) {
      body.appendChild(el('div', {text: '根据上方查询结果，详情请见表格与图表。'}));
      try {
        var c0 = evt && evt.content;
        var raw = typeof c0 === 'string' ? c0
                : (c0 && typeof c0 === 'object' ? (c0.thought || c0.answer || c0.summary || JSON.stringify(c0)) : '');
        if (raw) debugLog('  [llm-final suppressed] ' + String(raw).slice(0, 400));
      } catch (e) { /* 调试日志失败可忽略 */ }
    } else {
      var c = evt.content;
      var text = '';
      if (typeof c === 'string') text = c;
      else if (c && typeof c === 'object') text = c.thought || c.answer || c.summary || '';
      if (text.length > 1000) text = text.slice(0, 1000) + '…（已截断，详见调试详情）';
      body.appendChild(el('div', {text: text || '(无文本回答)'}));
    }
    card.appendChild(body);
    sess.content.appendChild(card);
  }
  async function handleAgentEvent(sess, evt) {
    var t = evt.type;
    if (t === 'thinking_text') {
      setStatus(sess, 'Agent 正在分析');
      if (evt.content) debugLog('  [thinking] ' + String(evt.content).slice(0, 400));
    } else if (t === 'tool_start') {
      setStatus(sess, TOOL_STATUS[evt.tool] || ('正在执行 ' + (evt.tool || '工具')));
    } else if (t === 'tool_result') {
      if (evt.tool === 'query_beelink_sql') await renderBeelinkResult(sess, evt);
    } else if (t === 'clarify') {
      if (sess.hasSuccess) setStatus(sess, '完成', 'done');
      else { setStatus(sess, '需要澄清', 'done'); renderClarify(sess, evt); }
    } else if (t === 'action') {
      if (!sess.hasSuccess) setStatus(sess, '需要操作', 'done');
    } else if (t === 'completion' || t === 'result') {
      setStatus(sess, '完成', 'done');
      renderCompletion(sess, evt);
    } else if (t === 'error') {
      setStatus(sess, '出错', 'fail');
      renderError(sess, JSON.stringify(evt, null, 2).slice(0, 1000));
    }
    window.scrollTo(0, document.body.scrollHeight);
  }

  async function runDataAgent(sess, question) {
    // 拉取轻语义上下文（如有；找不到则为空字符串），注入 prompt
    var semanticPrompt = '';
    try {
      var semResp = await fetch('/chatbi/semantic?source=' + encodeURIComponent(sess.source));
      if (semResp.ok) {
        var semJson = await semResp.json();
        if (semJson && semJson.prompt) semanticPrompt = semJson.prompt;
      }
    } catch (e) {}

    var baseGuardrail = [
      '[Context]',
      'Current beelink data source schema: `' + sess.source + '`.',
      '',
      '[Behavior — follow strictly]',
      '- For analysis questions (group by / sum / count / top-N / 按 X 统计 / 各 X), issue the SELECT directly via `query_beelink_sql`. Do NOT pre-list tables via INFORMATION_SCHEMA — only inspect metadata if a SELECT actually fails with "table not found".',
      '- Stay within schema `' + sess.source + '`. If a table name is mentioned without schema prefix, interpret it as `' + sess.source + '.<table>`.',
      '- If the user mentions a column like "gender" / "age_group" / "category" without table, assume the most plausible table in this schema.',
      '- Do not invent tables or columns. If a SELECT fails, stop probing and answer with what was found.'
    ].join('\n');
    var guardrail = baseGuardrail + (semanticPrompt ? '\n\n' + semanticPrompt : '') + '\n\n[User question]';
    var fullQ = guardrail + '\n' + question;

    var streamUrl = SERVER_HAS_KEY ? '/chatbi/agent-stream' : '/api/agent/data-agent-streaming';
    var modelCfg = {
      endpoint: $('endpoint').value.trim(), model: modelEl.value,
      api_base: $('api_base').value.trim(),
      api_version: null, is_global: false
    };
    if (!SERVER_HAS_KEY) modelCfg.api_key = $('api_key').value;
    var body = { model: modelCfg, input_tables: [], primary_tables: [], user_question: fullQ };

    try {
      await fetch('/api/connectors', {
        method: 'GET',
        headers: { 'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace }
      });
      var resp = await fetch(streamUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json',
                   'X-Identity-Id': sess.identity, 'X-Workspace-Id': sess.workspace },
        body: JSON.stringify(body)
      });
      if (!resp.ok) {
        var errText = await resp.text();
        await handleAgentEvent(sess, {type: 'error', status: resp.status, body: errText.slice(0, 1000)});
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
          try { await handleAgentEvent(sess, JSON.parse(line)); } catch (e) {}
        }
      }
      if (buf.trim()) {
        debugLog(buf);
        try { await handleAgentEvent(sess, JSON.parse(buf)); } catch (e) {}
      }
    } catch (e) {
      await handleAgentEvent(sess, {type: 'error', message: String(e && e.message || e)});
    }
  }

  // ========================================================================
  // 提交：意图路由
  // ========================================================================
  function setBusy(busy, msg) {
    submitBtn.disabled = busy;
    statusEl.innerHTML = (busy ? '<span class="spinner"></span>' : '') + (msg || '');
  }
  $('form').addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var cfg = {
      identity: $('identity').value.trim(),
      workspace: $('workspace').value.trim(),
      connectorId: $('connector_id').value.trim(),
      source: $('source').value.trim()
    };
    var question = $('question').value.trim();
    if (!cfg.identity || !cfg.workspace) { alert('请填 X-Identity-Id 与 X-Workspace-Id'); return; }
    if (!cfg.connectorId || !cfg.source) { alert('请填 connector_id 与 source'); return; }
    if (!question) { alert('请填问题'); return; }

    debugPreEl.textContent = '';
    setBusy(true, '请求中…');
    var sess = newSession(question, cfg);

    // 路由优先级：sample > describe > list_tables > DataAgent
    try {
      if (isSampleTableIntent(question)) {
        var tName = extractTableName(question, cfg.source);
        if (tName) { await runSampleTable(sess, tName); setBusy(false, '完成'); return; }
      }
      if (isDescribeTableIntent(question)) {
        var tName2 = extractTableName(question, cfg.source);
        if (tName2) { await runDescribeTable(sess, tName2); setBusy(false, '完成'); return; }
        // 未识别出表名 → 退回 DataAgent
      }
      if (isSimpleAggregateIntent(question)) {
        var field = extractGroupByField(question);
        if (field) { await runSimpleAggregate(sess, field); setBusy(false, '完成'); return; }
      }
      if (isListTablesIntent(question)) {
        await runListTables(sess); setBusy(false, '完成'); return;
      }
      // 其他：分析问题走 DataAgent
      if (!SERVER_HAS_KEY && !$('api_key').value) {
        setBusy(false); renderError(sess, '服务端无 LLM key，请在表单填 api_key。');
        return;
      }
      // describe/sample 想要解析表名但失败时，先尝试主动 list_tables 填充缓存，再重试一次
      if ((isDescribeTableIntent(question) || isSampleTableIntent(question)) && state.tables.length === 0) {
        try {
          var preTables = await fetchSourceTables(sess);
          state.tables = preTables.slice();
          var tName3 = extractTableName(question, cfg.source);
          if (tName3) {
            if (isSampleTableIntent(question)) { await runSampleTable(sess, tName3); setBusy(false, '完成'); return; }
            await runDescribeTable(sess, tName3); setBusy(false, '完成'); return;
          }
        } catch (e) {}
      }
      await runDataAgent(sess, question);
      setBusy(false, '完成');
    } catch (e) {
      renderError(sess, String(e && e.message || e));
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
