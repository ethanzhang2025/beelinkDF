# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""beelink (基于 Dremio v25 二开) 数据源加载器。

通过 beelink 原生 REST 把目录/表/字段/查询结果接入 Data Formulator。

关键端点（详见设计文档 v10 §4.4~§4.5）：

* ``POST /apiv2/login`` —— 用户名/密码换 token（JAX-RS V2 application 挂在 /apiv2/*）。
* ``GET  /api/v3/catalog`` —— 列顶层 source/space/home。
* ``GET  /api/v3/catalog/{id}?maxChildren=…&pageToken=…`` —— 递归列子项。
* ``GET  /api/v3/catalog/by-path/<seg1>/<seg2>/…?include=dataset`` —— 拿单表 schema。
* ``POST /api/v3/sql`` —— 异步提交 SQL；返回 jobId。
* ``GET  /api/v3/job/{id}`` —— 轮询 jobState。
* ``GET  /api/v3/job/{id}/results?offset=&limit=≤500`` —— 分页拉结果。

设计原则：

* **零 beelink Java 改动**：完全用 beelink 已有 REST。
* **权限透传**：DF 不复刻权限校验，beelink 越权直接报错。
* **token 自治**：401 自动用 Vault 中的用户名/密码重登一次；仍失败则向上抛错。
* **稳健 > 全面**：POC 阶段不追求覆盖 Dremio 全部 catalog 类型，
  只走通 PHYSICAL/VIRTUAL Dataset 路径。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from typing import Any

import pyarrow as pa
import requests

from data_formulator.data_loader.external_data_loader import (
    ExternalDataLoader,
    MAX_IMPORT_ROWS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# beelink/Dremio /api/v3/job/{id}/results 单次 limit 硬上限（JobResource.java:125）
_BEELINK_PAGE_SIZE = 500
# 单次 HTTP 默认超时（秒）
_DEFAULT_HTTP_TIMEOUT = 30.0
# 轮询 jobState 的最长等待时间（秒），POC 阶段保守取 60s
_DEFAULT_JOB_WAIT = 60.0
# 走 list_tables 时控制对 beelink catalog 的递归预算，防止误用打满 beelink
_LIST_TABLES_MAX_NODES = 5000
# beelink token 自定义头前缀（TokenUtils.java:27）
_BEELINK_AUTH_PREFIX = "_beelink"
# Dremio Arrow 时间/日期类型名，用于 is_dttm 推断
_TEMPORAL_ARROW_NAMES = {"timestamp", "date", "time", "datetime"}


# ---------------------------------------------------------------------------
# 自定义异常 —— 用 RuntimeError 子类即可，让 DataConnector 错误分类器统一兜底
# ---------------------------------------------------------------------------

class BeelinkAPIError(RuntimeError):
    """beelink REST 失败的统一异常。携带 HTTP 状态码与响应摘要。"""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


# ---------------------------------------------------------------------------
# Loader 实现
# ---------------------------------------------------------------------------

class BeelinkDataLoader(ExternalDataLoader):
    """把 beelink 作为 DF connector 的实现。

    与 ``PostgreSQLDataLoader`` 等 SQL loader 的差别：

    * 走 HTTP REST 而非 native client；
    * ``source_table`` 是用 ``.`` 拼接的 catalog path（如
      ``mysql_prod.sales.orders``），构造 SQL 时按段加双引号。
    """

    # ── 表单 & 文案 ────────────────────────────────────────────

    @staticmethod
    def list_params() -> list[dict[str, Any]]:
        return [
            {
                "name": "base_url",
                "type": "string",
                "required": True,
                "default": "",
                "tier": "connection",
                "description": "beelink 服务地址，例如 http://beelink:9047",
            },
            {
                "name": "user",
                "type": "string",
                "required": True,
                "default": "",
                "tier": "auth",
                "description": "beelink 用户名（DF 用它通过 POST /apiv2/login 拿 token）",
            },
            {
                "name": "password",
                "type": "string",
                "required": True,
                "default": "",
                "sensitive": True,
                "tier": "auth",
                "description": "beelink 密码（加密存放在 DF Vault，不会写入表元数据）",
            },
            {
                "name": "verify_ssl",
                "type": "boolean",
                "required": False,
                "default": True,
                "tier": "connection",
                "description": "是否校验 TLS 证书。自签证书的开发环境可关掉",
            },
        ]

    @staticmethod
    def auth_instructions() -> str:
        return (
            "**beelink 连接：**\n\n"
            "1. `base_url` 填 beelink 服务地址（含端口，默认 9047）。\n"
            "2. `user` / `password` 是您在 beelink 中的账号；DF 仅在内存中持有 token，"
            "密码加密存放在本地 Vault。\n"
            "3. DF 看到的数据范围、是否可执行 SQL，**完全由 beelink 服务端按您的账号**"
            "权限裁决；越权失败直接由 beelink 报错。\n\n"
            "调试建议：先在 beelink Web UI 用同一账号登录并跑一条 `SELECT 1`，"
            "再回到 DF 连接，可省去大量定位时间。"
        )

    @staticmethod
    def auth_mode() -> str:
        return "connection"

    @staticmethod
    def auth_config() -> dict[str, Any]:
        # POC-1 阶段走静态用户名/密码 + DF Vault；后续 POC-2 可平滑切到
        # {"mode": "delegated", "login_url": "/sso/landing"} 或
        # {"mode": "sso_exchange", "exchange_url": "..."}。
        return {"mode": "credentials"}

    @staticmethod
    def catalog_hierarchy() -> list[dict[str, str]]:
        # beelink 路径深度不固定（mysql_prod.sales.orders 三段；
        # iceberg_lake.warehouse.fact_x 也可能三/四段）。
        # POC 阶段统一暴露两层（source + table），由 list_tables 把多层 path
        # 通过显式 ``path`` 传回，DataConnector 自己会按 path 渲染嵌套树。
        return [
            {"key": "source", "label": "Source"},
            {"key": "table", "label": "Table"},
        ]

    # ── 生命周期 ──────────────────────────────────────────────

    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.base_url: str = (params.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("base_url is required")
        self.user: str = params.get("user", "")
        self.password: str = params.get("password", "")
        # POC-2 SSO 场景可由外层注入；POC-1 用户名/密码登录后 _login 自己填回
        self.access_token: str | None = params.get("access_token") or None
        self.verify_ssl: bool = bool(params.get("verify_ssl", True))

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._token_expires_at: float | None = None
        self.username_resolved: str = self.user

        if self.access_token:
            # 外部已注入 token，直接挂上去；不发起登录
            self._apply_token(self.access_token)
        else:
            if not self.user or not self.password:
                raise ValueError("user/password are required when no access_token is provided")
            self._login()

    # ── 内部 HTTP 助手 ────────────────────────────────────────

    def _apply_token(self, token: str) -> None:
        """把 token 装回 session header；遵守 beelink 自定义前缀协议。"""
        self._session.headers["Authorization"] = f"{_BEELINK_AUTH_PREFIX}{token}"

    def _login(self) -> None:
        """POST /apiv2/login → 拿 token；失败直接抛 BeelinkAPIError。"""
        url = f"{self.base_url}/apiv2/login"
        try:
            resp = self._session.post(
                url,
                json={"userName": self.user, "password": self.password},
                timeout=_DEFAULT_HTTP_TIMEOUT,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BeelinkAPIError(f"beelink /apiv2/login 网络错误：{exc}") from exc
        if resp.status_code != 200:
            raise BeelinkAPIError(
                f"beelink /apiv2/login 失败 HTTP {resp.status_code}: {_brief_body(resp)}",
                status=resp.status_code,
            )
        data = _safe_json(resp)
        if not isinstance(data, dict) or "token" not in data:
            raise BeelinkAPIError(f"beelink /apiv2/login 返回结构异常：{_brief_body(resp)}")
        self._apply_token(data["token"])
        # beelink 返回 expires 是毫秒级时间戳
        try:
            self._token_expires_at = float(data["expires"]) / 1000.0 if data.get("expires") else None
        except (TypeError, ValueError):
            self._token_expires_at = None
        self.username_resolved = data.get("userName") or self.user
        logger.info("beelink 登录成功 user=%s admin=%s", self.username_resolved, data.get("admin"))

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
                 timeout: float = _DEFAULT_HTTP_TIMEOUT, allow_relogin: bool = True) -> Any:
        """统一 HTTP 入口：401 时（仅在使用用户名/密码模式且允许时）自动重登一次。"""
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(
                method, url, json=json_body, timeout=timeout, verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise BeelinkAPIError(f"beelink {method} {path} 网络错误：{exc}") from exc

        # 401 自动重登：仅对用户名/密码模式生效；外部注入 token 模式不触发
        if resp.status_code == 401 and allow_relogin and not self.access_token:
            logger.info("beelink token 失效（401），尝试自动重登 user=%s", self.user)
            self._login()
            return self._request(method, path, json_body=json_body, timeout=timeout,
                                 allow_relogin=False)

        if resp.status_code >= 400:
            raise BeelinkAPIError(
                f"beelink {method} {path} 失败 HTTP {resp.status_code}: {_brief_body(resp)}",
                status=resp.status_code, body=_safe_json(resp),
            )
        return _safe_json(resp)

    # ── Catalog 浏览 ───────────────────────────────────────────

    # list_tables 用到的常量：可展开的容器类型 / 单页 maxChildren
    _CONTAINER_TYPES = {"SOURCE", "SPACE", "HOME", "FOLDER"}
    _CATALOG_PAGE_SIZE = 1000

    def list_tables(self, table_filter: str | None = None) -> list[dict[str, Any]]:
        """递归遍历所有 source/folder/space，收集叶子 DATASET 节点。

        防御措施：
        * 顶层只展开 ``containerType in {SOURCE, SPACE, HOME}``；
        * 整体节点访问数限制在 ``_LIST_TABLES_MAX_NODES`` 以内；
        * 任何子树读失败用 warning 跳过，不让单点异常吞掉整次遍历；
        * ``table_filter`` 走简单大小写不敏感子串匹配。
        """
        keyword = (table_filter or "").strip().lower() or None
        tables: list[dict[str, Any]] = []

        top_items = self._fetch_top_level_containers()
        if top_items is None:
            return tables

        # BFS 队列：(node_dict, accumulated_path_from_root)
        queue: list[tuple[dict[str, Any], list[str]]] = [
            (item, item.get("path") or [])
            for item in top_items
            if item.get("containerType") in {"SOURCE", "SPACE", "HOME"}
        ]

        visited_nodes = 0
        while queue and visited_nodes < _LIST_TABLES_MAX_NODES:
            node, _path = queue.pop(0)
            visited_nodes += 1
            node_id = node.get("id")
            if not node_id:
                continue

            for child in self._iter_children(node_id):
                ctype = child.get("type")  # CatalogItem.type: DATASET/SPACE/SOURCE/FOLDER/HOME
                cpath: list[str] = child.get("path") or []
                if ctype == "DATASET":
                    name = ".".join(cpath)
                    if keyword and keyword not in name.lower():
                        continue
                    tables.append({
                        "name": name,
                        "path": cpath,
                        "metadata": {
                            # 行数 / 列信息留给 get_column_types() 在用户点开时补；
                            # 这里只给最便宜的占位，避免对每张表都打一次 schema 请求。
                            "row_count": None,
                            "columns": [],
                        },
                        "table_key": name,
                    })
                elif ctype in self._CONTAINER_TYPES:
                    queue.append((child, cpath))
                # 其它类型（FILE/FUNCTION 等）POC 阶段忽略

        if visited_nodes >= _LIST_TABLES_MAX_NODES:
            logger.warning(
                "beelink list_tables 触发节点上限 %d，结果可能不完整；建议先按 table_filter 缩窄",
                _LIST_TABLES_MAX_NODES,
            )
        return tables

    def _fetch_top_level_containers(self) -> list[dict[str, Any]] | None:
        """读取 /api/v3/catalog 顶层并归一化成 list；结构异常时返回 None。"""
        top = self._request("GET", "/api/v3/catalog")
        items = top.get("data") if isinstance(top, dict) else top
        if not isinstance(items, list):
            logger.warning("beelink /api/v3/catalog 顶层非 list：%r", type(items))
            return None
        return items

    def _iter_children(self, node_id: str):
        """按 pageToken 分页迭代某个容器节点的 children；单节点失败时静默跳过。"""
        encoded_id = urllib.parse.quote(node_id, safe="")
        page_token: str | None = None
        while True:
            qs = f"maxChildren={self._CATALOG_PAGE_SIZE}"
            if page_token:
                qs += f"&pageToken={urllib.parse.quote(page_token, safe='')}"
            try:
                detail = self._request("GET", f"/api/v3/catalog/{encoded_id}?{qs}")
            except BeelinkAPIError as exc:
                logger.warning("beelink 展开 %s 失败，跳过：%s", node_id[:12], exc)
                return

            yield from (detail or {}).get("children") or []

            page_token = (detail or {}).get("nextPageToken")
            if not page_token:
                return

    def get_column_types(self, source_table: str) -> dict[str, Any]:
        """取单张表的 schema；返回 ``{"columns": [...], "description": ...}``。

        beelink ``Dataset.fields`` 字段是 Apache Arrow Field 的 JSON 描述
        （``org.apache.arrow.vector.types.pojo.Field`` 经 ``APIFieldDescriber``
        序列化），结构形如::

            {"name": "amount", "type": {"name": "decimal", "precision": 18, "scale": 2},
             "nullable": true, "children": [...]}

        因为版本可能略有差异，这里做"尽力而为"的解析：能拿到就用，不行
        就当 VARCHAR，让前端兜底。**TODO**：与 beelink 实际响应对齐字段名。
        """
        if not source_table:
            return {}
        encoded = "/".join(urllib.parse.quote(s, safe="") for s in source_table.split("."))
        try:
            ds = self._request("GET", f"/api/v3/catalog/by-path/{encoded}?include=dataset")
        except BeelinkAPIError as exc:
            logger.warning("beelink 取 dataset schema 失败 path=%s: %s", source_table, exc)
            return {}

        ds = ds or {}
        columns: list[dict[str, Any]] = []
        for field in ds.get("fields") or []:
            if not isinstance(field, dict):
                continue
            arrow_type = _extract_arrow_type_name(field.get("type"))
            columns.append({
                "name": field.get("name") or "",
                "type": arrow_type or "VARCHAR",
                "is_dttm": bool(arrow_type) and arrow_type.lower() in _TEMPORAL_ARROW_NAMES,
                "description": None,
            })
        return {"columns": columns, "description": ds.get("description")}

    # ── 数据拉取 ──────────────────────────────────────────────

    def fetch_data_as_arrow(
        self,
        source_table: str,
        import_options: dict[str, Any] | None = None,
    ) -> pa.Table:
        """根据 source_table（点分路径）+ import_options 构造 SELECT 并执行。"""
        if not source_table:
            raise ValueError("source_table is required")
        segments = source_table.split(".")
        sql = self._build_select_sql(segments, import_options or {})
        return self._exec_sql_to_arrow(sql, max_rows=_clamp_size(import_options))

    # ── 健康检查 ──────────────────────────────────────────────

    def test_connection(self) -> bool:
        """轻量 ping：beelink 的 ``GET /apiv2/login`` 等价于 isUserAuthorized()。"""
        try:
            # 该端点返回 boolean，HTTP 200 即认为连通
            self._request("GET", "/apiv2/login")
            return True
        except BeelinkAPIError as exc:
            logger.info("beelink test_connection 失败：%s", exc)
            return False

    # ── SQL 执行内部细节 ──────────────────────────────────────

    @staticmethod
    def _quote_ident(segment: str) -> str:
        """按 Dremio 习惯把单段路径用双引号包起来，内部双引号转义。"""
        return '"' + segment.replace('"', '""') + '"'

    def _build_select_sql(self, segments: list[str], opts: dict[str, Any]) -> str:
        from_clause = ".".join(self._quote_ident(s) for s in segments)

        columns = opts.get("columns")
        cols_sql = ", ".join(self._quote_ident(c) for c in columns) if columns else "*"

        order_sql = ""
        sort_cols = opts.get("sort_columns")
        if sort_cols:
            direction = "DESC" if (opts.get("sort_order", "asc") or "asc").lower() == "desc" else "ASC"
            order_sql = " ORDER BY " + ", ".join(
                f"{self._quote_ident(c)} {direction}" for c in sort_cols
            )

        limit_sql = f" LIMIT {_clamp_size(opts)}"
        return f"SELECT {cols_sql} FROM {from_clause}{order_sql}{limit_sql}"

    def _exec_sql_to_arrow(self, sql: str, *, max_rows: int) -> pa.Table:
        """提交 SQL → 轮询 → 分页拉结果 → 转 pa.Table。"""
        submit = self._request("POST", "/api/v3/sql", json_body={"sql": sql})
        if not isinstance(submit, dict) or "id" not in submit:
            raise BeelinkAPIError(f"beelink /api/v3/sql 返回结构异常：{submit!r}")
        job_id = submit["id"]
        logger.debug("beelink job 提交成功 id=%s sql=%s", job_id, sql)
        self._wait_for_job(job_id)
        return self._fetch_results_paginated(job_id, max_rows=max_rows)

    def _wait_for_job(self, job_id: str, *, max_wait: float = _DEFAULT_JOB_WAIT) -> None:
        deadline = time.time() + max_wait
        delay = 0.2
        while True:
            payload = self._request("GET", f"/api/v3/job/{urllib.parse.quote(job_id, safe='')}")
            state = (payload or {}).get("jobState")
            if state == "COMPLETED":
                return
            if state in {"FAILED", "CANCELED"}:
                msg = (payload or {}).get("errorMessage") or f"job {state}"
                raise BeelinkAPIError(f"beelink job {state}: {msg}", body=payload)
            if time.time() > deadline:
                raise BeelinkAPIError(
                    f"beelink job {job_id} 等待超时 ({max_wait}s, last state={state})",
                )
            time.sleep(delay)
            delay = min(delay * 1.4, 2.0)

    def _fetch_results_paginated(self, job_id: str, *, max_rows: int) -> pa.Table:
        """按 beelink 单页硬上限 500 行分页累积，最多取 max_rows 行。"""
        rows_all: list[dict[str, Any]] = []
        offset = 0
        encoded_id = urllib.parse.quote(job_id, safe="")
        while len(rows_all) < max_rows:
            limit = min(_BEELINK_PAGE_SIZE, max_rows - len(rows_all))
            payload = self._request(
                "GET", f"/api/v3/job/{encoded_id}/results?offset={offset}&limit={limit}",
            )
            rows = (payload or {}).get("rows") or []
            if not rows:
                break
            rows_all.extend(rows)
            offset += len(rows)
            if len(rows) < limit:
                # beelink 返回少于请求量，说明已是最后一页
                break

        # TODO: schema 字段是 Arrow Field describer 数组，理论上能直接构建 pa.Schema
        # 让导入后的 dtype 更精确（避免 int/decimal 被推断成 float64）。POC-1 阶段
        # 先用 pylist 推断；POC-1.5 / POC-2 再做更严谨的 schema 兜底。
        try:
            return pa.Table.from_pylist(rows_all)
        except Exception as exc:
            logger.warning("pa.Table.from_pylist 失败，回退为全字符串：%s", exc)
            return _safe_table_from_rows(rows_all)


# ---------------------------------------------------------------------------
# 模块级小工具
# ---------------------------------------------------------------------------

def _clamp_size(opts: dict[str, Any] | None) -> int:
    """把 import_options.size 限制在 (0, MAX_IMPORT_ROWS]。POC 默认 10000。"""
    default_size = min(10_000, MAX_IMPORT_ROWS)
    if not opts or opts.get("size") is None:
        return default_size
    try:
        n = int(opts["size"])
    except (TypeError, ValueError):
        return default_size
    if n <= 0:
        return default_size
    return min(n, MAX_IMPORT_ROWS)


def _safe_json(resp: "requests.Response") -> Any:
    """尽量把响应解析为 JSON；失败返回原始文本（截断）。"""
    try:
        return resp.json()
    except ValueError:
        return resp.text[:1000]


def _brief_body(resp: "requests.Response") -> str:
    """生成失败日志用的响应摘要（≤500 字符），优先提取 errorMessage/message。"""
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(data, dict):
        for key in ("errorMessage", "message", "error"):
            if data.get(key):
                return str(data[key])[:500]
    return json.dumps(data, ensure_ascii=False)[:500]


def _extract_arrow_type_name(t: Any) -> str:
    """从 Arrow Field describer 的 type 字段里抠出可读类型名。

    beelink 序列化的 type 可能是：

    * ``str``（最简单，如 ``"VARCHAR"``）；
    * ``dict``，常见 keys: ``"name"``、``"type"``。

    都失败就返回空串，让上层兜底。
    """
    if t is None:
        return ""
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        for key in ("name", "type"):
            v = t.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


def _safe_table_from_rows(rows: list[dict[str, Any]]) -> pa.Table:
    """兜底：把所有字段当字符串构 pa.Table，确保最差情况下也能落到 parquet。"""
    if not rows:
        return pa.table({})
    cols = sorted({k for r in rows for k in r.keys()})

    def _stringify_column(col: str) -> "pa.Array":
        values = [None if (v := r.get(col)) is None else str(v) for r in rows]
        return pa.array(values, type=pa.string())

    return pa.table({c: _stringify_column(c) for c in cols})
