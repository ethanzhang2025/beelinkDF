#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
beelink POC-1 / POC-1.5 / POC-2A 一键 HTTP 冒烟脚本（纯 stdlib）。

覆盖范围：
  --phase0   委托运行 scripts/beelink_phase0_probe.py
  --poc1     POST /api/connectors/import-data        （表导入）
  --poc15    POST /api/connectors/import-sql         （手写 SQL 直通）
  --poc2a    POST /api/connectors/nl2sql-import      （独立 NL2SQL）

POC-2B（DataAgent + query_beelink_sql）走 NDJSON 流 + 多轮 tool loop，
不在本脚本范围内；演示路径见 docs/beelink-poc-design/POC-1_to_POC-2B_演示与验收手册.md §7。

安全约束：
- 任何敏感字段（api_key / X-Identity-Id / X-Workspace-Id / Authorization / Cookie / 密码）
  绝不打印；脚本输出仅含字段名、行数、表名、错误码、耗时。
- 不写真实凭证到磁盘；所有配置通过环境变量读取。
- 任何一步失败 -> 退出码非 0。

环境变量（缺失时给出明确报错）：
  必需（所有命令公用）：
    DF_URL              例 http://127.0.0.1:5500
    DF_CONNECTOR_ID     例 beelink:beelink-main
    DF_WORKSPACE_ID     例 default
    DF_IDENTITY_ID      例 local:beelink
  POC-1 / POC-1.5 / POC-2A 额外可选：
    SMOKE_SOURCE_TABLE  POC-1 用，默认 smartquery_demo.customers
    SMOKE_SQL           POC-1.5 用，默认 "SELECT 42 AS n"
    SMOKE_QUESTION      POC-2A 用，默认 "按 gender 统计客户数"
    SMOKE_TABLE_KEYS    POC-2A schema 检索表 keys（逗号分隔），默认 smartquery_demo.customers
    SMOKE_HTTP_TIMEOUT  HTTP 单次超时，默认 60 秒
  POC-2A 额外必需：
    DEEPSEEK_API_KEY    DeepSeek API Key
    SMOKE_LLM_MODEL     默认 deepseek-chat（POC-2A 单次调用 v4-pro 也可，但默认稳）
    SMOKE_LLM_API_BASE  默认 https://api.deepseek.com

退出码：
  0 全部通过
  2 缺必需环境变量 / 参数错误
  3 HTTP 调用失败 / 校验未通过
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


# ---------- 通用工具 ----------

def _env(name: str, *, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"[FAIL] 缺必需环境变量：{name}", file=sys.stderr)
        sys.exit(2)
    return val or ""


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    """发 POST application/json，返回解析后的 JSON dict。失败抛 RuntimeError。"""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason}; body[:500]={body_text[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"URLError: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSONDecodeError: {e}") from e


def _get(url: str, headers: dict[str, str], timeout: int) -> int:
    """发 GET，返回 HTTP 状态码；失败也不抛，仅供 connector lazy-load 触发用。"""
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError:
        return 0


def _base_headers() -> dict[str, str]:
    return {
        "X-Identity-Id": _env("DF_IDENTITY_ID", required=True),
        "X-Workspace-Id": _env("DF_WORKSPACE_ID", required=True),
    }


def _df_url() -> str:
    return _env("DF_URL", required=True).rstrip("/")


def _connector_id() -> str:
    return _env("DF_CONNECTOR_ID", required=True)


def _timeout() -> int:
    return int(_env("SMOKE_HTTP_TIMEOUT", default="60"))


def _print_result(label: str, payload: dict[str, Any]) -> None:
    """只打印安全字段：表名 / 行数 / 列数 / source_query / reason 摘要。"""
    safe = {
        "table_name": payload.get("table_name"),
        "row_count": payload.get("row_count"),
        "columns_count": len(payload.get("columns") or []),
        "source_query_head": (payload.get("source_query") or "")[:200],
        "reason_head": (payload.get("reason") or "")[:200],
        "refreshable": payload.get("refreshable"),
    }
    print(f"[OK] {label}: {json.dumps(safe, ensure_ascii=False)}")


def _trigger_connector_lazyload() -> None:
    url = f"{_df_url()}/api/connectors"
    status = _get(url, _base_headers(), _timeout())
    if status != 200:
        print(f"[WARN] connector lazy-load 触发返回 {status}，后续可能报 Connector not found", file=sys.stderr)


# ---------- 子命令实现 ----------

def run_phase0() -> None:
    print("[..] Phase 0: 委托运行 scripts/beelink_phase0_probe.py")
    script = Path(__file__).parent / "beelink_phase0_probe.py"
    if not script.exists():
        raise RuntimeError(f"找不到 {script}")
    # 透传当前环境（BEELINK_URL / BEELINK_USER / BEELINK_PASS 等由用户 export）
    ret = subprocess.call([sys.executable, str(script)])
    if ret != 0:
        raise RuntimeError(f"Phase 0 probe 退出码 {ret}")
    print("[OK] Phase 0 通过")


def run_poc1() -> None:
    print("[..] POC-1: import-data")
    body = {
        "connector_id": _connector_id(),
        "source_table": _env("SMOKE_SOURCE_TABLE", default="smartquery_demo.customers"),
        "table_name": "smoke_poc1",
        "import_options": {"size": 100},
    }
    t0 = time.time()
    resp = _post_json(f"{_df_url()}/api/connectors/import-data", body, _base_headers(), _timeout())
    elapsed = time.time() - t0
    if not isinstance(resp.get("row_count"), int) or resp["row_count"] < 0:
        raise RuntimeError(f"POC-1 返回缺 row_count 或为负：{json.dumps(resp)[:300]}")
    if not resp.get("columns"):
        raise RuntimeError("POC-1 返回 columns 为空")
    print(f"[OK] POC-1 耗时 {elapsed:.2f}s")
    _print_result("POC-1", resp)


def run_poc15() -> None:
    print("[..] POC-1.5: import-sql")
    sql = _env("SMOKE_SQL", default="SELECT 42 AS n")
    body = {
        "connector_id": _connector_id(),
        "sql": sql,
        "table_name": "smoke_poc15",
    }
    t0 = time.time()
    resp = _post_json(f"{_df_url()}/api/connectors/import-sql", body, _base_headers(), _timeout())
    elapsed = time.time() - t0
    if not isinstance(resp.get("row_count"), int):
        raise RuntimeError(f"POC-1.5 返回缺 row_count：{json.dumps(resp)[:300]}")
    # 手写 SQL 应原样回传
    if resp.get("source_query", "").strip() != sql.strip():
        print(f"[WARN] source_query 与请求 SQL 不一致；请求={sql!r} 返回={resp.get('source_query')!r}", file=sys.stderr)
    print(f"[OK] POC-1.5 耗时 {elapsed:.2f}s")
    _print_result("POC-1.5", resp)


def run_poc2a() -> None:
    print("[..] POC-2A: nl2sql-import")
    api_key = _env("DEEPSEEK_API_KEY", required=True)
    # 解析 table_keys
    raw_keys = _env("SMOKE_TABLE_KEYS", default="smartquery_demo.customers")
    table_keys = [s.strip() for s in raw_keys.split(",") if s.strip()]
    body = {
        "connector_id": _connector_id(),
        "question": _env("SMOKE_QUESTION", default="按 gender 统计客户数"),
        "table_keys": table_keys,
        "table_name": "smoke_poc2a",
        "model": {
            "endpoint": "openai",
            "model": _env("SMOKE_LLM_MODEL", default="deepseek-chat"),
            "api_key": api_key,  # 仅放进 body，不打印
            "api_base": _env("SMOKE_LLM_API_BASE", default="https://api.deepseek.com"),
            "api_version": None,
            "is_global": False,
        },
        "import_options": {"size": 10000, "timeout": 60},
    }
    t0 = time.time()
    resp = _post_json(f"{_df_url()}/api/connectors/nl2sql-import", body, _base_headers(), _timeout())
    elapsed = time.time() - t0
    if not isinstance(resp.get("row_count"), int):
        raise RuntimeError(f"POC-2A 返回缺 row_count：{json.dumps(resp)[:300]}")
    sq = (resp.get("source_query") or "").lstrip().upper()
    if not (sq.startswith("SELECT") or sq.startswith("WITH")):
        raise RuntimeError(f"POC-2A source_query 不是 SELECT/WITH 开头：{sq[:80]!r}")
    if not resp.get("reason"):
        print("[WARN] POC-2A 返回 reason 为空（LLM 未给出理由）", file=sys.stderr)
    print(f"[OK] POC-2A 耗时 {elapsed:.2f}s")
    _print_result("POC-2A", resp)


# ---------- 主入口 ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="beelink POC-1 / POC-1.5 / POC-2A 一键 HTTP 冒烟脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--phase0", action="store_true", help="跑 Phase 0 REST 全链路探测")
    parser.add_argument("--poc1", action="store_true", help="跑 POC-1 import-data")
    parser.add_argument("--poc15", action="store_true", help="跑 POC-1.5 import-sql")
    parser.add_argument("--poc2a", action="store_true", help="跑 POC-2A nl2sql-import（需 DEEPSEEK_API_KEY）")
    args = parser.parse_args()

    if not (args.phase0 or args.poc1 or args.poc15 or args.poc2a):
        parser.print_help()
        return 2

    steps: list[tuple[str, callable]] = []
    if args.phase0:
        steps.append(("phase0", run_phase0))
    # POC-1 / POC-1.5 / POC-2A 都走 DF HTTP，先触发 connector lazy-load
    if args.poc1 or args.poc15 or args.poc2a:
        _trigger_connector_lazyload()
    if args.poc1:
        steps.append(("poc1", run_poc1))
    if args.poc15:
        steps.append(("poc15", run_poc15))
    if args.poc2a:
        steps.append(("poc2a", run_poc2a))

    failures: list[str] = []
    for name, fn in steps:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — 顶层兜底报错
            print(f"[FAIL] {name}: {e}", file=sys.stderr)
            failures.append(name)

    if failures:
        print(f"\n[SUMMARY] 失败步骤：{', '.join(failures)}", file=sys.stderr)
        return 3
    print(f"\n[SUMMARY] 全部通过：{', '.join(n for n, _ in steps)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
