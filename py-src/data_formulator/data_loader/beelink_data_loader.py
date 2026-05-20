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
    infer_source_metadata_status,
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
# POC-1.5：用户可通过 import_options.timeout 覆盖 _wait_for_job 默认值，
# 但封顶 10 分钟——避免误传过大值卡死 worker 与 HTTP 连接
_MAX_JOB_WAIT = 600.0
# 走 list_tables 时控制对 beelink catalog 的递归预算，防止误用打满 beelink
_LIST_TABLES_MAX_NODES = 5000
# beelink token 自定义头前缀（TokenUtils.java:27）
_BEELINK_AUTH_PREFIX = "_beelink"
# Dremio Arrow 时间/日期类型名，用于 is_dttm 推断
_TEMPORAL_ARROW_NAMES = {"timestamp", "date", "time", "datetime"}

# Arrow Field describer 的常见 type name → Dremio SQL 类型名映射。
# 命中后给前端一个熟悉的 SQL 类型字面量；未命中则保留原值由上层兜底。
_ARROW_TO_SQL_TYPE: dict[str, str] = {
    "utf8": "VARCHAR",
    "largeutf8": "VARCHAR",
    "varchar": "VARCHAR",
    "string": "VARCHAR",
    "int": "INTEGER",
    "int8": "INTEGER",
    "int16": "INTEGER",
    "int32": "INTEGER",
    "int64": "BIGINT",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "floatingpoint": "DOUBLE",
    "float": "DOUBLE",
    "float32": "DOUBLE",
    "float64": "DOUBLE",
    "double": "DOUBLE",
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",
    "decimal": "DECIMAL",
    "decimal128": "DECIMAL",
    "decimal256": "DECIMAL",
    "date": "DATE",
    "date32": "DATE",
    "date64": "DATE",
    "time": "TIME",
    "time32": "TIME",
    "time64": "TIME",
    "timestamp": "TIMESTAMP",
    "datetime": "TIMESTAMP",
}


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
        # beelink 路径深度不固定：普通 source 直挂表（mysql_local.foo 两段），
        # 外接 SQL 类 source（mysql / tpcds_12domain）含 schema/db/CONTAINER
        # 层（mysql.dbA.orders 三段；iceberg_lake.warehouse.fact_x 三/四段）。
        # 这里只声明"source + table"两层占位，真正的多层渲染由
        # ``_tables_to_catalog_tree`` 覆写按每张表的真实 path 现搭。
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
    # CONTAINER：beelink 对外接 SQL 数据源（mysql / tpcds_12domain 等）中
    # schema/db 层的标记；不纳入会让该 source 整棵 DATASET 子树被吞掉。
    _CONTAINER_TYPES = {"SOURCE", "SPACE", "HOME", "FOLDER", "CONTAINER"}
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
                        # 带上完整 beelink path，``_tables_to_catalog_tree``
                        # 覆写版按其真实深度建多级 namespace；
                        # ``_source_name = name`` 保证 import / preview / sample
                        # 走 source_table.split(".") 拼回原始路径，行为不变。
                        "path": list(cpath),
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

    # ── Catalog tree ──────────────────────────────────────────
    #
    # 覆写框架版以按每张表的真实 beelink path 现搭多级 namespace 节点。
    # 框架默认实现按 ``catalog_hierarchy`` 静态深度截断 path，会把
    # ``mysql.dbA.orders`` 这种 3 段路径裁成 ``dbA → orders``，丢掉
    # ``mysql`` 顶层。此处不依赖固定深度：
    #
    # * len(path)=2 (普通 source → 表)：保持 ``source → table`` 两层，
    #   与历史 test / smartquery_demo / tpcds_sf10 渲染完全一致。
    # * len(path)≥3 (含 CONTAINER 中间层)：source → schema/db → … → table，
    #   每段都作为 namespace 节点，让前端可逐层展开。
    #
    # 节点字段对齐 ExternalDataLoader._tables_to_catalog_tree：
    # namespace 携带累计 path 与 children；table 叶携带 _source_name +
    # source_metadata_status，import / preview 流程零改动。

    def _tables_to_catalog_tree(self, tables: list[dict[str, Any]]) -> list[dict]:
        from collections import OrderedDict

        root: "OrderedDict[str, dict]" = OrderedDict()

        def _ensure_namespace(cursor: "OrderedDict[str, dict]",
                              segments: list[str]) -> "OrderedDict[str, dict]":
            cumulative: list[str] = []
            for seg in segments:
                cumulative.append(seg)
                node = cursor.get(seg)
                if node is None or node.get("node_type") != "namespace":
                    node = {
                        "name": seg,
                        "node_type": "namespace",
                        "path": list(cumulative),
                        "metadata": None,
                        "children": OrderedDict(),
                    }
                    cursor[seg] = node
                cursor = node["children"]
            return cursor

        for t in tables:
            segments = list(t.get("path") or [])
            if not segments:
                # 回退：缓存里旧记录无 path 字段时按点分 name 还原
                raw = (t.get("name") or "").strip()
                if not raw:
                    continue
                segments = raw.split(".")

            orig_name = t.get("name") or ".".join(segments)
            meta = t.get("metadata")
            table_key = t.get("table_key")
            if table_key:
                meta = {**(meta or {}), "table_key": table_key}
            merged = {**(meta or {}), "_source_name": orig_name}
            if "source_metadata_status" not in merged:
                merged["source_metadata_status"] = infer_source_metadata_status(meta)

            leaf_name = segments[-1]
            leaf_node = {
                "name": leaf_name,
                "node_type": "table",
                "path": list(segments),
                "metadata": merged,
            }

            if len(segments) == 1:
                root.setdefault(leaf_name, leaf_node)
                continue

            parent_children = _ensure_namespace(root, segments[:-1])
            parent_children[leaf_name] = leaf_node

        def _materialize(cursor: "OrderedDict[str, dict]") -> list[dict]:
            result: list[dict] = []
            for node in cursor.values():
                if node.get("node_type") == "namespace":
                    result.append({
                        "name": node["name"],
                        "node_type": "namespace",
                        "path": node["path"],
                        "metadata": None,
                        "children": _materialize(node["children"]),
                    })
                else:
                    result.append(node)
            return result

        return _materialize(root)

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
            # 命中映射时前端拿到 VARCHAR/INTEGER/TIMESTAMP 等熟悉字面量；
            # 未命中时保留原始 Arrow 名（如 Utf8/Int32），不误伤未知类型。
            sql_type = _map_arrow_to_sql_type(arrow_type)
            columns.append({
                "name": field.get("name") or "",
                "type": sql_type or arrow_type or "VARCHAR",
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

    # ── POC-1.5：用户手写 SQL 直通 ───────────────────────────
    #
    # 与 fetch_data_as_arrow 的差异：
    # * 不再按 source_table + import_options 拼 SELECT；SQL 由调用方一字不改提供。
    # * 权限 / 字段越权 / 语法错误全由 beelink 服务端裁决，DF 不做 SQL 解析。
    # * 仍受 import_options.size 限制（默认 10000，硬上限 MAX_IMPORT_ROWS）；
    #   beelink 单页 500 行硬上限仍由 _fetch_results_paginated 自动分页处理。

    def fetch_sql_as_arrow(
        self,
        sql: str,
        import_options: dict[str, Any] | None = None,
    ) -> pa.Table:
        """直接把 ``sql`` 提交给 beelink，结果以 pa.Table 返回。

        ``import_options``:
            * ``size``：取行数上限（默认 10000，硬封顶 ``MAX_IMPORT_ROWS``）。
            * ``timeout``：等 job COMPLETED 的秒数（默认 ``_DEFAULT_JOB_WAIT``，
              封顶 ``_MAX_JOB_WAIT``）。非法值 / 越界值都回落默认。

        Raises
        ------
        ValueError
            ``sql`` 为空。
        BeelinkAPIError
            job 失败 / 超时 / 取结果失败 / 空结果且无可用 schema 等
            （由底层 ``_exec_sql_to_arrow`` 抛出，原始错误信息透传，
            不吞 beelink 的权限或字段越权报错）。
        """
        if not sql or not sql.strip():
            raise ValueError("sql is required")
        return self._exec_sql_to_arrow(
            sql.strip(),
            max_rows=_clamp_size(import_options),
            max_wait=_clamp_timeout(import_options),
        )

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

    def _exec_sql_to_arrow(
        self,
        sql: str,
        *,
        max_rows: int,
        max_wait: float | None = None,
    ) -> pa.Table:
        """提交 SQL → 轮询 → 分页拉结果 → 转 pa.Table。

        ``max_wait=None`` 时走 ``_wait_for_job`` 的默认值，POC-1 表导入路径
        ``fetch_data_as_arrow`` 不传该参数 → 行为完全不变。
        """
        submit = self._request("POST", "/api/v3/sql", json_body={"sql": sql})
        if not isinstance(submit, dict) or "id" not in submit:
            raise BeelinkAPIError(f"beelink /api/v3/sql 返回结构异常：{submit!r}")
        job_id = submit["id"]
        logger.debug("beelink job 提交成功 id=%s sql=%s", job_id, sql)
        if max_wait is None:
            self._wait_for_job(job_id)
        else:
            self._wait_for_job(job_id, max_wait=max_wait)
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
                # message 同时含英文 "timeout" 关键词，让 connector_errors classifier
                # 落到 DB_CONNECTION_FAILED(retry=True) 而非 fallback DATA_LOAD_ERROR
                raise BeelinkAPIError(
                    f"beelink job {job_id} 等待超时（timeout after {max_wait}s, "
                    f"last state={state}）",
                )
            time.sleep(delay)
            delay = min(delay * 1.4, 2.0)

    def _fetch_results_paginated(self, job_id: str, *, max_rows: int) -> pa.Table:
        """按 beelink 单页硬上限 500 行分页累积，最多取 max_rows 行。"""
        rows_all: list[dict[str, Any]] = []
        offset = 0
        schema_info: Any = None
        encoded_id = urllib.parse.quote(job_id, safe="")
        while len(rows_all) < max_rows:
            limit = min(_BEELINK_PAGE_SIZE, max_rows - len(rows_all))
            payload = self._request(
                "GET", f"/api/v3/job/{encoded_id}/results?offset={offset}&limit={limit}",
            )
            # 首页就抓 schema：空结果时也能据此构造列骨架
            if schema_info is None and isinstance(payload, dict):
                schema_info = payload.get("schema")
            rows = (payload or {}).get("rows") or []
            if not rows:
                break
            rows_all.extend(rows)
            offset += len(rows)
            if len(rows) < limit:
                # beelink 返回少于请求量，说明已是最后一页
                break

        if not rows_all:
            # 空结果：从 results payload 的 schema 还原一个零行但有列的 pa.Table，
            # 否则下游 write_parquet_from_arrow 会拿到零列零行的表，无法落 parquet。
            return _empty_table_from_schema(schema_info)

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


def _clamp_timeout(opts: dict[str, Any] | None) -> float:
    """POC-1.5：把 import_options.timeout 限制在 (0, _MAX_JOB_WAIT]。

    非法 / 缺失 / 越界一律回落 ``_DEFAULT_JOB_WAIT``，避免单条慢 SQL 误传
    超大 timeout 拖死 worker。"""
    if not opts or opts.get("timeout") is None:
        return _DEFAULT_JOB_WAIT
    try:
        n = float(opts["timeout"])
    except (TypeError, ValueError):
        return _DEFAULT_JOB_WAIT
    if n <= 0:
        return _DEFAULT_JOB_WAIT
    return min(n, _MAX_JOB_WAIT)


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


def _map_arrow_to_sql_type(arrow_type: str | None) -> str | None:
    """把常见 Arrow 类型名映射为 Dremio SQL 类型名；未命中返回 None。"""
    if not arrow_type:
        return None
    return _ARROW_TO_SQL_TYPE.get(arrow_type.lower())


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


def _empty_table_from_schema(schema_info: Any) -> pa.Table:
    """空结果集兜底：从 ``/results`` payload 的 schema 拼一个零行但有列的 pa.Table。

    POC-1 阶段不解析 Arrow Field 嵌套类型，所有列统一用 ``pa.string()``——
    反正零行，类型不影响数据，仅保证 parquet 有列骨架可写。schema 不可用时
    直接抛 BeelinkAPIError，让用户知道查询无结果且无 schema 可用。
    """
    # schema_info 可能是 {"fields": [...]} 或直接 [...] 两种形态
    if isinstance(schema_info, dict):
        fields = schema_info.get("fields")
    elif isinstance(schema_info, list):
        fields = schema_info
    else:
        fields = None

    if isinstance(fields, list):
        col_names: list[str] = [
            f["name"] for f in fields
            if isinstance(f, dict) and f.get("name")
        ]
    else:
        col_names = []

    if not col_names:
        raise BeelinkAPIError(
            "beelink 查询无结果，且响应中没有可用 schema，无法构造空结果表",
        )

    empty_str_col = pa.array([], type=pa.string())
    return pa.table({name: empty_str_col for name in col_names})


def _safe_table_from_rows(rows: list[dict[str, Any]]) -> pa.Table:
    """兜底：把所有字段当字符串构 pa.Table，确保最差情况下也能落到 parquet。"""
    if not rows:
        return pa.table({})
    cols = sorted({k for r in rows for k in r.keys()})

    def _stringify_column(col: str) -> "pa.Array":
        values = [None if (v := r.get(col)) is None else str(v) for r in rows]
        return pa.array(values, type=pa.string())

    return pa.table({c: _stringify_column(c) for c in cols})
