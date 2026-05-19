# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""POC-2A：beelink 独立 NL2SQL 后端能力（不接 DataAgent）。

定位
----
接收用户自然语言问题 + 调用方指定的 ``table_keys`` → 用 LLM 生成单条 Dremio
SQL → 复用 POC-1.5 ``BeelinkDataLoader.fetch_sql_as_arrow`` 执行 → 落 workspace。

边界
----
* 不调用 DataAgent / SYSTEM_PROMPT / TOOLS；自带最小 system prompt，且 LLM
  调用**不传 tools**（与 DataAgent 的 ``_call_llm`` 解耦）。
* 不依赖 catalog_cache —— schema 现取 ``loader.get_column_types(table_key)``。
* 浅校验只拦非 SELECT/WITH；不做 AST / SQL Guard / SQL 修复 Agent。
* 不抽 import-sql 的执行写入 helper —— 直接复用 ``fetch_sql_as_arrow`` +
  ``workspace.write_parquet_from_arrow``，与 POC-1.5 行为一致。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import litellm
import openai

from data_formulator.datalake.parquet_utils import sanitize_table_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 自定义异常 —— 让端点层区分"LLM 端错"与"caller 错 / beelink 端错"
# ---------------------------------------------------------------------------

class NL2SQLLLMError(RuntimeError):
    """LLM 调用 / 输出失败的统一异常。

    端点层捕获后用 ``error_handler.classify_and_wrap_llm_error`` 分到
    ``LLM_AUTH_FAILED / LLM_RATE_LIMIT / LLM_TIMEOUT / LLM_UNKNOWN_ERROR`` 等
    LLM 系列错误码——而不是错误地落到 connector classifier 的
    ``CONNECTOR_AUTH_FAILED / DB_CONNECTION_FAILED`` 上让用户排错走错方向。

    构造时务必用 ``raise NL2SQLLLMError(...) from exc`` 保留 ``__cause__``，
    让 classifier 能拿到原始异常的文本做关键词匹配。
    """


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 单次请求最多注入的表数；超过抛 ValueError，避免 prompt 爆炸
_MAX_TABLE_KEYS = 10
# 单表最多注入的列数；超过截断尾部，提示 LLM 缩窄问题
_MAX_COLUMNS_PER_TABLE = 30
# LLM 单次调用超时（秒）；与 DataAgent _call_llm_once 用 120 一致
_LLM_TIMEOUT_SECONDS = 120
# JSON 解析失败时的二次提示
_JSON_RETRY_INSTRUCTION = (
    "Your previous response was not valid JSON. "
    "Output ONLY the JSON object — no markdown, no ```json fences, "
    "no explanatory prose."
)


NL2SQL_SYSTEM_PROMPT = """You are a Dremio SQL generator integrated into Data Formulator.

The user asks a question in natural language. Produce ONE valid Dremio SQL
statement that answers it, using only the tables and columns provided in the
schema context.

## Strict rules

1. Output ONLY a single JSON object — no markdown, no ```sql/```json fences,
   no explanatory prose outside the JSON.
2. JSON shape:
   {"sql": "<dremio sql>", "rationale": "<one short Chinese sentence>"}
   If the question is too ambiguous to write SQL with confidence, instead:
   {"sql": null, "rationale": "<reason>", "needs_clarification": "<one question to ask the user>"}
3. SQL must start with SELECT or WITH. NEVER emit INSERT/UPDATE/DELETE/DROP/
   CREATE/ALTER/TRUNCATE/MERGE/GRANT — the calling layer will reject those.
4. Reference tables with quoted multi-segment paths exactly as shown in the
   schema, e.g. "smartquery_demo"."customers". Never invent paths.
5. Use only the columns listed in the schema context. Never invent columns.
6. Dremio SQL is ANSI-like — CTE (WITH), window functions, GROUP BY, ORDER BY,
   date_trunc, CAST are all available. Use them when helpful.
7. Avoid Calcite reserved words as aliases (e.g. one/two/day/month/year/level/
   value); prefer short identifiers like n, cnt, total.
8. rationale must be a short Chinese sentence explaining the SQL strategy."""


# ---------------------------------------------------------------------------
# 浅校验：SELECT / WITH
# ---------------------------------------------------------------------------

def _is_select_or_with(sql: str) -> bool:
    """剥前导空白、单行注释 ``--`` 与块注释 ``/* */`` 后，首个 word token
    必须是 SELECT 或 WITH（大小写不敏感）。空字符串 / 非字符串 / 注释吃完
    后无内容 / 首 token 是其它关键词 → False。"""
    if not sql or not isinstance(sql, str):
        return False
    text = sql.strip()
    while text:
        if text.startswith("/*"):
            end = text.find("*/")
            if end == -1:
                return False
            text = text[end + 2:].lstrip()
        elif text.startswith("--"):
            nl = text.find("\n")
            if nl == -1:
                return False
            text = text[nl + 1:].lstrip()
        else:
            break
    m = re.match(r"([A-Za-z_]\w*)", text)
    if not m:
        return False
    return m.group(1).upper() in {"SELECT", "WITH"}


# ---------------------------------------------------------------------------
# Schema 格式化
# ---------------------------------------------------------------------------

def _format_table_schema(table_key: str, col_types: dict[str, Any]) -> str:
    """把 ``loader.get_column_types(table_key)`` 的返回拼成 markdown 块。

    入参 ``col_types`` 形如：

        {"columns": [{"name", "type", "is_dttm", "description"}, ...],
         "description": "可选表描述"}

    超过 ``_MAX_COLUMNS_PER_TABLE`` 的列截断尾部。
    """
    segments = table_key.split(".")
    quoted_path = ".".join(f'"{s}"' for s in segments)
    description = (col_types or {}).get("description") or ""
    columns = (col_types or {}).get("columns") or []
    if not columns:
        raise ValueError(
            f"table_key {table_key!r} 无可用 schema（loader.get_column_types 返回空 columns）"
        )

    truncated = columns[:_MAX_COLUMNS_PER_TABLE]
    lines = [f"## Table: {quoted_path}"]
    if description:
        lines.append(f"Description: {description}")
    lines.append("Columns:")
    for c in truncated:
        name = c.get("name") or ""
        ctype = c.get("type") or "VARCHAR"
        col_desc = c.get("description") or ""
        if col_desc:
            lines.append(f"- {name} ({ctype}) — {col_desc}")
        else:
            lines.append(f"- {name} ({ctype})")
    if len(columns) > _MAX_COLUMNS_PER_TABLE:
        lines.append(
            f"(已省略 {len(columns) - _MAX_COLUMNS_PER_TABLE} 列；如需更多请缩窄问题或拆问)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON 抠取
# ---------------------------------------------------------------------------

def _parse_json_response(text: str) -> dict[str, Any]:
    """从 LLM 文本里抠出 JSON 对象。三种形态都支持：纯 JSON、markdown 围栏、
    含散文但有一个完整 ``{...}`` 块。抠不出抛 ValueError。"""
    if not text or not isinstance(text, str):
        raise ValueError("LLM returned empty text")
    s = text.strip()

    # 剥 markdown 围栏（```json 或 ```）
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()

    try:
        result = json.loads(s)
    except json.JSONDecodeError:
        # 兜底：抠最外层 {...}
        start = s.find("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("LLM response contained no JSON object")
        candidate = s[start:end + 1]
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM JSON parse failed: {exc}") from exc

    if not isinstance(result, dict):
        raise ValueError(
            f"LLM JSON must be an object, got {type(result).__name__}"
        )
    return result


# ---------------------------------------------------------------------------
# LLM 调用（无 tools）
# ---------------------------------------------------------------------------

def _call_llm_for_sql(client, messages: list[dict[str, Any]]) -> str:
    """无 tools 的最小 LLM 调用，返回 assistant content 字符串。

    与 ``data_agent._call_llm_once`` 同款双后端分支（openai / litellm），但
    **不传 tools** —— POC-2A 不需要 DataAgent 的工具集污染上下文。
    """
    try:
        if client.endpoint == "openai":
            oai = openai.OpenAI(
                base_url=client.params.get("api_base") or None,
                api_key=client.params.get("api_key") or "",
                timeout=_LLM_TIMEOUT_SECONDS,
            )
            resp = oai.chat.completions.create(
                model=client.model,
                messages=messages,
            )
            return resp.choices[0].message.content or ""

        params = client.params.copy()
        resp = litellm.completion(
            model=client.model,
            messages=messages,
            drop_params=True,
            **params,
        )
        return resp.choices[0].message.content or ""
    except Exception as exc:
        # 让端点层落到 LLM 系列错误码而非 connector 错误码；__cause__ 保留
        # 原始 openai/litellm 异常文本供 classify_and_wrap_llm_error 关键词匹配
        raise NL2SQLLLMError(f"LLM call failed: {type(exc).__name__}") from exc


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_nl2sql(
    *,
    client,
    loader,
    workspace,
    question: str,
    table_keys: list[str],
    table_name: str,
    import_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POC-2A 核心 pipeline：NL+schema → SQL → 执行 → 落 workspace。

    返回值是 endpoint envelope.data 的内容（不含 ``status`` 外壳）。

    错误传递
    --------
    * ``ValueError``       —— 入参非法 / schema 取不到 / LLM 不输 JSON / SQL 非 SELECT。
    * ``BeelinkAPIError``  —— beelink 端执行错（由 fetch_sql_as_arrow 抛）。
    * 其它 ``Exception``   —— LLM API 失败 / workspace 写入失败。

    调用方在路由层用 ``classify_and_raise_connector_error`` 兜底分类。
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question is required")
    if not isinstance(table_keys, list) or not table_keys:
        raise ValueError("table_keys must contain 1 to 10 items")
    if len(table_keys) > _MAX_TABLE_KEYS:
        raise ValueError(f"too many table_keys (max {_MAX_TABLE_KEYS})")
    if not isinstance(table_name, str) or not table_name.strip():
        raise ValueError("table_name is required")

    import_options = import_options or {}

    # 1) 现拉 schema —— 不依赖 catalog_cache，无论调用方是否 sync 过都能跑
    schema_blocks: list[str] = []
    for tk in table_keys:
        if not isinstance(tk, str) or not tk.strip():
            raise ValueError(f"invalid table_key: {tk!r}")
        col_types = loader.get_column_types(tk)
        schema_blocks.append(_format_table_schema(tk, col_types))
    schema_text = "\n\n".join(schema_blocks)

    # 2) 拼 prompt → LLM（JSON-only retry-once）
    user_msg = (
        f"## Schema context\n\n{schema_text}\n\n"
        f"## User question\n{question.strip()}\n\n"
        "Return only the JSON object as specified."
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": NL2SQL_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    raw = _call_llm_for_sql(client, messages)
    try:
        parsed = _parse_json_response(raw)
    except ValueError:
        logger.info("LLM JSON parse failed; retrying once with explicit JSON-only instruction")
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "system", "content": _JSON_RETRY_INSTRUCTION})
        raw = _call_llm_for_sql(client, messages)
        try:
            parsed = _parse_json_response(raw)
        except ValueError as exc:
            # retry 后仍非 JSON：归为 LLM 端错（不是 caller 错），让端点层
            # 走 LLM_UNKNOWN_ERROR 而非 INVALID_REQUEST
            raise NL2SQLLLMError(
                f"LLM output is not valid JSON after retry: {exc}"
            ) from exc

    rationale = parsed.get("rationale") or ""
    sql_value = parsed.get("sql")
    needs_clarification = parsed.get("needs_clarification")

    # 3) clarification 路径：LLM 主动声明信息不足 → 不写 workspace
    if sql_value is None or (isinstance(sql_value, str) and not sql_value.strip()):
        return {
            "needs_clarification": needs_clarification
                or "请补充更多细节，例如时间范围、统计维度或筛选条件。",
            "rationale": rationale,
            "sql": None,
        }

    if not isinstance(sql_value, str):
        raise ValueError(f"LLM returned invalid sql type: {type(sql_value).__name__}")
    sql = sql_value.strip()

    # 4) 浅校验：只允许 SELECT / WITH，其它一律拒
    if not _is_select_or_with(sql):
        first_80 = sql[:80].replace("\n", " ")
        raise ValueError(
            f"Only SELECT/WITH queries are allowed in POC-2; got: {first_80!r}"
        )

    # 5) 执行 SQL → pa.Table（复用 POC-1.5 fetch_sql_as_arrow，行为锁定）
    arrow_table = loader.fetch_sql_as_arrow(sql, import_options=import_options)

    # 6) 落 workspace —— source_info 与 import-sql 同款；额外把 nl2sql_question
    #    塞进 import_options，便于后续追溯"这个表是哪句话生成的"
    safe_name = sanitize_table_name(table_name.strip())
    source_info = {
        "loader_type": loader.__class__.__name__,
        "loader_params": loader.get_safe_params(),
        "source_query": sql,
        "import_options": {**import_options, "nl2sql_question": question.strip()},
    }
    meta = workspace.write_parquet_from_arrow(
        table=arrow_table,
        table_name=safe_name,
        source_info=source_info,
    )
    return {
        "table_name": meta.name,
        "row_count": meta.row_count,
        "columns": [
            {"name": c.name, "dtype": c.dtype}
            for c in (meta.columns or [])
        ],
        "sql": sql,
        "source_query": sql,
        "rationale": rationale,
        "refreshable": False,
    }
