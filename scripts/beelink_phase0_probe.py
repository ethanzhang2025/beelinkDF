#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""beelink REST 全链路验证脚本（Phase 0）。

按设计文档 v10 §12 Phase 0 验证 beelink 是否真的把以下端点开通且可用：
    1) POST  /apiv2/login    （JAX-RS V2 application 挂在 /apiv2/*）
    2) GET   /api/v3/catalog
    3) GET   /api/v3/catalog/{id}?maxChildren=...
    4) POST  /api/v3/sql
    5) GET   /api/v3/job/{id}
    6) GET   /api/v3/job/{id}/results?offset=&limit=

只依赖 Python stdlib，不需要 pip 安装任何第三方库。
凭证通过环境变量传入，绝不硬编码。每一步都明确输出 PASS / FAIL 和
HTTP 响应摘要，方便排障。
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# ---------------------------------------------------------------------------
# 配置 & 工具
# ---------------------------------------------------------------------------

_RESP_SNIPPET_LEN = 500  # 失败时打印响应体的最大字符数，避免泄漏过多敏感数据


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    """读环境变量；required=True 时缺失即退出。"""
    value = os.environ.get(name, default)
    if required and not value:
        sys.stderr.write(f"[ERROR] 缺少必需的环境变量：{name}\n")
        sys.stderr.write("        参考 scripts/beelink_phase0.env.example\n")
        sys.exit(2)
    return value or ""


def _step(idx: int, title: str) -> None:
    print(f"\n[{idx}] {title}")


def _fail(stage: str, exc: Exception, body: Any = None) -> None:
    """打印失败明细并非 0 退出。"""
    print(f"  ✗ FAIL @ {stage}: {exc}")
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            snippet = body[:_RESP_SNIPPET_LEN].decode("utf-8", "replace")
        else:
            snippet = json.dumps(body, ensure_ascii=False)[:_RESP_SNIPPET_LEN]
        print(f"    响应摘要：{snippet}")
    sys.exit(1)


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 15.0,
    insecure: bool = False,
) -> tuple[int, Any]:
    """发请求并尽量解析成 JSON；返回 (status, payload)。

    - 4xx/5xx 不抛异常，统一返回供调用方判断；
    - 仅当 Content-Type 含 application/json 才反序列化，其他原样返回 str。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs: dict[str, str] = {"Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct and raw:
                return resp.status, json.loads(raw)
            return resp.status, raw.decode("utf-8", "replace") if raw else ""
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        try:
            payload: Any = json.loads(raw) if raw else ""
        except ValueError:
            payload = raw.decode("utf-8", "replace")
        return e.code, payload


# ---------------------------------------------------------------------------
# 步骤实现
# ---------------------------------------------------------------------------

def _login(base_url: str, user: str, password: str, *, timeout: float,
           insecure: bool) -> str:
    """走 POST /apiv2/login，返回 token。失败即退出。"""
    _step(1, "POST /apiv2/login → 拿 token")
    status, payload = _http_json(
        "POST", f"{base_url}/apiv2/login",
        body={"userName": user, "password": password},
        timeout=timeout, insecure=insecure,
    )
    if status != 200 or not isinstance(payload, dict) or "token" not in payload:
        _fail("登录", RuntimeError(f"HTTP {status}"), payload)
    print(f"  ✓ PASS  userName={payload.get('userName')} admin={payload.get('admin')} "
          f"roleId={payload.get('roleId')} orgId={payload.get('orgId')}")
    return payload["token"]


def _list_top_level(base_url: str, auth: dict[str, str], *, timeout: float,
                    insecure: bool) -> list[dict[str, Any]]:
    """GET /api/v3/catalog 列顶层；返回 CatalogItem 列表。"""
    _step(2, "GET /api/v3/catalog → 列顶层 sources/spaces/homes")
    status, payload = _http_json(
        "GET", f"{base_url}/api/v3/catalog",
        headers=auth, timeout=timeout, insecure=insecure,
    )
    if status != 200:
        _fail("列顶层 catalog", RuntimeError(f"HTTP {status}"), payload)
    # 不同版本可能直接返回数组，也可能包成 {"data": [...]}
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        _fail("列顶层 catalog", RuntimeError(f"非预期响应结构：{type(items).__name__}"),
              payload)
    print(f"  ✓ PASS  顶层项数：{len(items)}")
    for it in items[:5]:
        ct = it.get("containerType") or it.get("type") or "?"
        path = "/".join(it.get("path") or [])
        iid = (it.get("id") or "")[:12]
        print(f"     - {ct}: {path}  id={iid}…")
    return items


def _expand_children(base_url: str, item_id: str, auth: dict[str, str], *,
                     timeout: float, insecure: bool) -> None:
    """GET /api/v3/catalog/{id}?maxChildren=20。"""
    safe_id = urllib.parse.quote(item_id, safe="")
    _step(3, f"GET /api/v3/catalog/{{id}}?maxChildren=20 → 列 children (id={item_id[:12]}…)")
    status, payload = _http_json(
        "GET", f"{base_url}/api/v3/catalog/{safe_id}?maxChildren=20",
        headers=auth, timeout=timeout, insecure=insecure,
    )
    if status != 200:
        _fail("展开 children", RuntimeError(f"HTTP {status}"), payload)
    children = (payload or {}).get("children") or []
    next_tok = (payload or {}).get("nextPageToken")
    extra = f"  next_page={next_tok[:12]}…" if next_tok else ""
    print(f"  ✓ PASS  children 数：{len(children)}{extra}")
    for c in children[:5]:
        ct = c.get("type") or c.get("containerType") or "?"
        print(f"     - {ct}: {'/'.join(c.get('path') or [])}")


def _submit_sql(base_url: str, sql: str, auth: dict[str, str], *, timeout: float,
                insecure: bool) -> str:
    """POST /api/v3/sql；返回 jobId。"""
    _step(4, "POST /api/v3/sql → 提交 SQL")
    print(f"  sql: {sql}")
    status, payload = _http_json(
        "POST", f"{base_url}/api/v3/sql",
        headers=auth, body={"sql": sql},
        timeout=timeout, insecure=insecure,
    )
    if status != 200 or not isinstance(payload, dict) or "id" not in payload:
        _fail("提交 SQL", RuntimeError(f"HTTP {status}"), payload)
    job_id = payload["id"]
    print(f"  ✓ PASS  jobId={job_id}")
    return job_id


def _wait_for_completion(base_url: str, job_id: str, auth: dict[str, str], *,
                         timeout: float, insecure: bool, max_wait: float = 30.0) -> int:
    """轮询 GET /api/v3/job/{id} 直至 COMPLETED；返回 rowCount。"""
    _step(5, "GET /api/v3/job/{id} → 轮询直至 COMPLETED")
    deadline = time.time() + max_wait
    delay = 0.3
    last_state = ""
    while True:
        status, payload = _http_json(
            "GET", f"{base_url}/api/v3/job/{job_id}",
            headers=auth, timeout=timeout, insecure=insecure,
        )
        if status != 200:
            _fail("查 job 状态", RuntimeError(f"HTTP {status}"), payload)
        state = (payload or {}).get("jobState") or ""
        if state == "COMPLETED":
            row_count = int(payload.get("rowCount") or 0)
            print(f"  ✓ PASS  jobState=COMPLETED  rowCount={row_count}")
            return row_count
        if state in {"FAILED", "CANCELED"}:
            _fail(
                "执行 job",
                RuntimeError(f"jobState={state}  msg={(payload or {}).get('errorMessage')}"),
                payload,
            )
        if time.time() > deadline:
            _fail("等待 job 完成", RuntimeError(f"超时 max_wait={max_wait}s (state={state})"))
        if state != last_state:
            print(f"     · state={state}")
            last_state = state
        time.sleep(delay)
        delay = min(delay * 1.4, 2.0)


def _fetch_results(base_url: str, job_id: str, auth: dict[str, str], *,
                   timeout: float, insecure: bool, limit: int = 500) -> None:
    """GET /api/v3/job/{id}/results?offset=0&limit=≤500。"""
    _step(6, f"GET /api/v3/job/{{id}}/results?offset=0&limit={limit}")
    status, payload = _http_json(
        "GET", f"{base_url}/api/v3/job/{job_id}/results?offset=0&limit={limit}",
        headers=auth, timeout=timeout, insecure=insecure,
    )
    if status != 200:
        _fail("取结果", RuntimeError(f"HTTP {status}"), payload)
    rows = (payload or {}).get("rows") or []
    schema = (payload or {}).get("schema")
    print(f"  ✓ PASS  rows={len(rows)}  schema?={'有' if schema else '无'}")
    for r in rows[:3]:
        print(f"     row: {json.dumps(r, ensure_ascii=False)}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    base_url = _env("BEELINK_URL", required=True).rstrip("/")
    user = _env("BEELINK_USER", required=True)
    password = _env("BEELINK_PASS", required=True)
    explicit_id = _env("BEELINK_SOURCE_ID")
    # 注意：Dremio 把 "one" 当保留字（解析器期望 IDENTIFIER），所以默认别名取 n。
    test_sql = _env("BEELINK_TEST_SQL", "SELECT 1 AS n")
    timeout = float(_env("BEELINK_TIMEOUT", "15"))
    insecure = _env("BEELINK_INSECURE", "1").lower() in ("1", "true", "yes")

    print(f"target = {base_url}  user = {user}  insecure = {insecure}")

    token = _login(base_url, user, password, timeout=timeout, insecure=insecure)
    auth = {"Authorization": f"_beelink{token}"}

    items = _list_top_level(base_url, auth, timeout=timeout, insecure=insecure)

    # 选一个 source 展开；优先用环境变量给的 id
    expand_id = explicit_id or next(
        (it.get("id") for it in items if it.get("containerType") == "SOURCE"),
        None,
    )
    if expand_id:
        _expand_children(base_url, expand_id, auth, timeout=timeout, insecure=insecure)
    else:
        _step(3, "GET /api/v3/catalog/{id} 跳过（无可用 source；可设 BEELINK_SOURCE_ID）")
        print("  · SKIP")

    job_id = _submit_sql(base_url, test_sql, auth, timeout=timeout, insecure=insecure)
    _wait_for_completion(base_url, job_id, auth, timeout=timeout, insecure=insecure)
    _fetch_results(base_url, job_id, auth, timeout=timeout, insecure=insecure)

    print("\n全部链路通过 ✓")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
