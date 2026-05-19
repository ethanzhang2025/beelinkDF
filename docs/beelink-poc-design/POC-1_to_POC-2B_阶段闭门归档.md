# POC-1 → POC-2B 阶段闭门归档

> 日期：2026-05-19
> 分支：`feat/beelink-poc2b-dataagent-tool`
> POC-2B 代码基线 HEAD：`aa7228d28046c3f55c3fb72666664aa0b0da4d2c`
> 状态：阶段闭门，不进入 POC-2C，不改前端，不改 beelink Java

---

## 1. 总体结论

POC-1 至 POC-2B 四阶段已全部完成、真机验收通过、commit 已 push。在不改前端、不改 beelink Java 一行的前提下，**Data Formulator + beelink 已经形成最小 ChatBI 闭环**：

- **beelink**（基于 Dremio v25 二开）作为**数据源与权限裁决方**：所有 SQL 最终在 beelink 上执行，beelink 决定数据可见性、行列裁权与下推优化；
- **Data Formulator**（v0.7-alpha.2）作为**智能图表主体与 DataAgent 容器**：负责连接器、workspace、表注册、DataAgent 自循环、图表渲染；
- **LLM 通道**：
  - **DeepSeek Chat（V3 非 thinking）** — POC-2B DataAgent tool loop 推荐模型，自循环兼容；
  - **DeepSeek Pro / Reasoner（V4 / R1 thinking）** — POC-2A 独立 API 单次调用可用；**当前不建议**用于 DataAgent tool loop，原因见 §8。

POC-2B 完成后即可在 DF 主界面通过 DataAgent 自然语言提问，由 LLM 生成 SQL → 走 `query_beelink_sql` tool 在 beelink 上执行 → 落入 workspace → 由 DataAgent 后续 `explore` / `visualize` 工具出图，形成完整 NL → SQL → 数据 → 图表链路。

---

## 2. 阶段成果

| 阶段 | 描述 | 关键产物 |
|---|---|---|
| **POC-1** | beelink 表导入 | `BeelinkDataLoader`（722 行）+ `_LOADER_SPECS` 注册；端点 `POST /api/connectors/import-data` |
| **POC-1.5** | 用户手写 SQL 直通导入 | `BeelinkDataLoader.fetch_sql_as_arrow`；端点 `POST /api/connectors/import-sql`；加固：`_clamp_timeout` + job 超时英文关键词 |
| **POC-2A** | 独立后端 NL2SQL 导入 API | `agents/nl2sql.py`（421 行：`NL2SQL_SYSTEM_PROMPT` / `run_nl2sql` / `execute_sql_to_workspace` / `NL2SQLLLMError`）；端点 `POST /api/connectors/nl2sql-import`；加固：LLM 异常分流到 `LLM_AUTH_FAILED / LLM_RATE_LIMIT / LLM_TIMEOUT / LLM_UNKNOWN_ERROR` |
| **POC-2B** | DataAgent 接入 beelink SQL tool | `data_agent.py` 第 9 个 tool `query_beelink_sql` + `_handle_query_beelink_sql`；SYSTEM_PROMPT 末尾追加决策提示；`self._identity_id` 持久化；与 POC-2A 共用 `execute_sql_to_workspace` helper |

所有阶段：beelink Java 零改动；DF 前端 `src/` 零改动；DF 后端仅追加/加固，不破坏既有行为。

---

## 3. 分支与 commit 汇总

| 分支 | 含义 | 顶端 commit | 备注 |
|---|---|---|---|
| `feat/beelink-poc1-loader` | POC-1 表导入 | `e408adf` | 已 push |
| `feat/beelink-poc1.5-sql-pass-through` | POC-1.5 手写 SQL 直通 | `82baa3d` | 已 push；基于 POC-1 顶端 |
| `feat/beelink-poc2-nl2sql` | POC-2A 独立 NL2SQL API | `40f2fc4` | 已 push；基于 POC-1.5 顶端 |
| **`feat/beelink-poc2b-dataagent-tool`** | **POC-2B DataAgent tool（当前）** | **代码基线 `aa7228d`** + 交接文档 `d73a820` | 已 push；基于 POC-2A 顶端 |

栈式关系（自下而上）：
```
main
 └─ e408adf  (POC-1 顶端)
     └─ 82baa3d  (POC-1.5 顶端)
         └─ 40f2fc4  (POC-2A 顶端)
             └─ aa7228d  (POC-2B 代码顶端)
                 └─ d73a820  (v2.0 交接文档)
                     └─ <本归档 commit>
```

**POC-2B 阶段闭门代码基线为 `aa7228d28046c3f55c3fb72666664aa0b0da4d2c`**；其上 `d73a820` 是 v2.0 会话交接文档，本归档文档将再叠加一个 docs commit，不引入代码变更。

四个 POC 分支均**未合并 main**。

---

## 4. 核心实现清单

| 文件 | 行数 | 涉及阶段 | 角色 |
|---|---|---|---|
| `py-src/data_formulator/data_loader/beelink_data_loader.py` | 722 | POC-1 / POC-1.5 | beelink loader：登录桥、catalog、`fetch_data_as_arrow`、`fetch_sql_as_arrow`、`_exec_sql_to_arrow`、`_clamp_timeout`、`BeelinkAPIError` |
| `py-src/data_formulator/data_connector.py` | 2260 | POC-1 / POC-1.5 / POC-2A | 三个 HTTP 端点：`import-data` / `import-sql` / `nl2sql-import`；LLM 错误分流 `classify_and_wrap_llm_error` |
| `py-src/data_formulator/agents/nl2sql.py` | 421 | POC-2A / POC-2B | `NL2SQL_SYSTEM_PROMPT`、`_is_select_or_with` 浅校验、`_call_llm_for_sql`、`run_nl2sql`、`execute_sql_to_workspace`（POC-2A/2B 共用 helper）、`NL2SQLLLMError` |
| `py-src/data_formulator/agents/data_agent.py` | 2305 | POC-2B | 第 9 个 TOOLS 条目 `query_beelink_sql`、`_handle_query_beelink_sql` handler、SYSTEM_PROMPT 末尾决策提示、`self._identity_id` 持久化 |
| `scripts/beelink_phase0_probe.py` | 279 | Phase 0 | 纯 stdlib，6 步真机 REST 验证脚本（登录 / catalog / sql submit / job 轮询 / results / cleanup） |
| `docs/beelink-poc-design/screenshots/` | — | UI 验收 | `beelink_card_row.png` / `df_connectors_dialog.png` / `df_landing.png` |
| `docs/beelink-poc-design/df_beelink_poc1_to_poc2_design_v10.md` | 2250 | 全阶段 | 单一权威设计文档；§14.6/7/8/9 四份真机验收 |
| `docs/handoff/会话交接-2026-05-19-v1.0.md` / `v2.0.md` | — | 全阶段 | 阶段冻结快照 |

**未触碰**：`src/`（前端）、`~/beelink_wenshu/`（beelink Java 仓库，只读依赖）、DF agents 中 `data_agent.py` 既有 8 个 tool 行为、`context.py`、`client_utils.py`、`routes/agents.py`。

---

## 5. API / Tool 清单

| 入口 | 类型 | 阶段 | 说明 |
|---|---|---|---|
| `POST /api/connectors/import-data` | HTTP | POC-1 | DF 标准表导入；body `{connector_id, source_table, table_name, import_options}`；行为：beelink `SELECT *` + 分页 ≤500/页 → Arrow → workspace |
| `POST /api/connectors/import-sql` | HTTP | POC-1.5 | 手写 SQL 直通；body `{connector_id, sql, table_name, import_options}`；行为：跳过 `_build_select_sql`，直接 `_exec_sql_to_arrow` |
| `POST /api/connectors/nl2sql-import` | HTTP | POC-2A | 独立 NL2SQL；body `{connector_id, question, table_keys, table_name, model{...}, import_options}`；行为：LLM 单次调 → JSON `{sql,reason}` → `_is_select_or_with` 校验 → 执行 → workspace |
| **DataAgent tool `query_beelink_sql`** | LLM tool | POC-2B | DataAgent 第 9 个 tool；参数 `{purpose, connector_id, sql, table_name, max_rows?}`（前 4 required）；行为：connector lookup → loader → `execute_sql_to_workspace` → 成功返 JSON / 失败保留 errorMessage 前 500 字符供 LLM 自修 |

所有 HTTP 端点统一通过 `X-Identity-Id` + `X-Workspace-Id` 头识别身份与 workspace；错误码统一走 `connector_errors` / `classify_and_wrap_llm_error` 分流。

---

## 6. 验收结果汇总

所有验收均为真机执行，结果记录于 `df_beelink_poc1_to_poc2_design_v10.md` §14.6 ~ §14.9。

| 验收项 | 用例数 | 结果 | 文档位置 |
|---|---|---|---|
| **Phase 0** beelink REST 6 步真机探测 | 6 | ✅ 全 PASS | `scripts/beelink_phase0_probe.py` 输出 |
| **POC-1** `import-data` 表导入 | 11 | ✅ 全 PASS | §14.6（含 catalog tree / 多表导入 / 列类型映射 / 401 自动重登 / 空结果） |
| **POC-1.5** `import-sql` 手写 SQL 直通 | 7 | ✅ 全 PASS | §14.7（含保留字、`"seg"."seg"` 多段引号、timeout 透传、job 超时关键词） |
| **POC-2A** `nl2sql-import` + DeepSeek V4 Pro | 9 | ✅ 全 PASS | §14.8（含中文 UTF-8、DeepSeek Pro 4 个 NL 用例、LLM 401/429/timeout 分流） |
| **POC-2B** DataAgent + DeepSeek Chat | 3 主 + 6 加固 | ✅ 全 PASS | §14.9（query_beelink_sql ok 链路 + 错误自循环 + 已导入表优先 explore） |
| **回归** POC-1 / 1.5 / 2A 在 POC-2B HEAD 上 | 各阶段抽样 | ✅ 全 PASS | §14.9 末尾回归小节 |

未做的验收（不在本闭门范围）：DataAgent 在 DeepSeek V4 thinking 模型下的循环（已知不兼容，见 §8）、前端 NL2SQL 入口（前端零改动）、并发 / 长跑稳定性、跨 workspace 共享。

---

## 7. 当前边界（未做）

为避免后续接手误解，明确以下**本阶段未做**：

| # | 未做项 | 说明 |
|---|---|---|
| 1 | **POC-2C** | 错误修复自循环增强、重复调用抑制 — 未开始；用例 2 已证既有 `max_tool_rounds=12` 自循环对常见错误够用 |
| 2 | **前端改造** | `src/` 零 diff；DataAgent 入口走 DF 原生输入框；无 ChatBI 专用面板 |
| 3 | **beelink Java 改造** | `~/beelink_wenshu/` 只读依赖；零行修改 |
| 4 | **SQL Guard** | 仅 `_is_select_or_with` 浅校验首词；未做完整 AST / 白名单 / 写操作拦截 |
| 5 | **SQL 修复 Agent** | 错误修复完全依赖 DataAgent 现有 tool loop 自然重试，无独立修复 Agent |
| 6 | **强 BO / 强 RAG / MCP-first** | 无 business object 语义层；无 RAG 向量库；不走 MCP 协议 |
| 7 | **完整 ChatBI 会话系统** | 无会话历史持久化、无多轮上下文压缩策略、无会话级权限编排 |
| 8 | **DataAgent thinking 兼容** | 未改 `_call_llm_once` 主循环回传 `reasoning_content`；V4 Pro/Flash/Reasoner 在 DataAgent loop 下报 400 |

---

## 8. 已知限制

| # | 限制 | 影响面 | 当前应对 |
|---|---|---|---|
| 1 | **DeepSeek V4 Pro / Flash / R1 thinking 模式在 DataAgent loop 报 400** | POC-2B 模型选择被限制在 V3 系列 | 强制使用 `deepseek-chat`；V4 系列仅在 POC-2A 单次调用使用 |
| 2 | **DataAgent 可能重复调用 `query_beelink_sql`** | workspace 中间表膨胀（表名自动 `_2/_3` 后缀） | 依赖 `workspace.get_fresh_name` 避免覆盖；用户可手动清理；未做调用计数与抑制 |
| 3 | **无前端 ChatBI 专用入口** | 用户需通过 DF 原生 DataAgent 输入框提问，体验非定制 | 不阻塞 POC 闭环；待 Phase 5 决策 |
| 4 | **SQL 校验仅 SELECT/WITH 浅判断** | 可能漏过 LLM 生成的非常规但合法的 DML/DDL 语句首词 | 依赖 beelink 端权限兜底（beelink 是最终裁权方）；不替代正式 SQL Guard |
| 5 | **schema context 不完整** | POC-2A 依赖调用方传 `table_keys`；POC-2B 依赖 DataAgent 在 tool loop 中主动检索 | 无语义层 / 业务字典；复杂业务表需明确告知 LLM |
| 6 | **错误修复无独立 Agent** | 复杂错误链可能在 12 轮 tool loop 内无法收敛 | 当前用例够用；超出阈值时由用户介入或留待 POC-2C |
| 7 | **catalog cache 单文件** | `<workspace_root>/catalog_cache/<safe_source_id>.json` 整体加载 | 表数极多时可能慢；当前真实场景表数可控 |
| 8 | **DF 重启后 connector lazy-load** | DataAgent 首次调用前需先 `GET /api/connectors` 触发 vault auto-reconnect | curl 真机时已记入命令速查；UI 流程自动触发 |

---

## 9. 下一阶段建议（不写代码）

按优先级排列：

### 推荐顺序

1. **可选四 · 合并分支 / 整理 PR**（先做）
   - 推荐方式：**单 PR 把 `feat/beelink-poc2b-dataagent-tool` merge 到 main**，附完整 POC-1/1.5/2A/2B commit 历史
   - 理由：四阶段连贯演进，每步都有真机验收与文档回写，单 PR 让 reviewer 一次看清完整链路，避免 4 次 review 重复解释 POC 边界
   - 替代方案：栈式 4 个 PR 依次合并（仅当组织严格要求"一个 commit 一个 PR"）

2. **可选一 · POC-2C 轻量错误修复 + 重复调用抑制**（次做）
   - 触发条件：业务方报"LLM 写错 SQL 频率高"或 workspace 中间表膨胀困扰
   - 范围：DataAgent tool loop 中对 `query_beelink_sql` 增加调用计数 / 错误分类 / 同 SQL 去重；不引入独立修复 Agent

3. **可选二 · 最小前端入口**（再做）
   - 触发条件：业务方明确投用并报"图表体验差"或"找不到 NL2SQL 入口"
   - 范围：DictTable 卡片显示 `source_query` 摘要 + 错误样式 + DF 主界面增加 NL2SQL 快捷入口（依旧不改 DataAgent 主体）

4. **可选三 · 轻 BO / 语义层**（最后做）
   - 触发条件：业务方反馈"LLM 不理解业务字段"或多团队复用诉求出现
   - 范围：在 `connector_id` 维度挂业务字典 JSON / YAML，注入 SYSTEM_PROMPT；不引入 RAG / 向量库

5. **附加项 · DataAgent thinking 兼容**（与上述并行可做）
   - 触发条件：业务方明确要求 DeepSeek V4 Pro / R1 在 DataAgent 中可用
   - 范围：改 `_call_llm_once` 主循环回传 `reasoning_content`；新分支隔离风险

**默认建议**：先做"可选四"（合并到 main），然后停在主干等业务真实反馈，再决定走 POC-2C 还是前端入口。

---

## 10. 后续接手说明

### 10.1 环境准备

```bash
# DeepSeek API Key（用户层 env，不写文件，不入仓库）
export DEEPSEEK_API_KEY=<你的_DeepSeek_API_Key>

# 进入仓库 + venv
cd ~/beelinkDF && source .venv/bin/activate
git checkout feat/beelink-poc2b-dataagent-tool
```

**严禁**：把 `DEEPSEEK_API_KEY` 写入 `.env` 提交、写入代码常量、写入文档示例。命令速查里的 `$DEEPSEEK_API_KEY` 始终走 shell 变量。

### 10.2 模型选择

| 用法 | 推荐 model | 备注 |
|---|---|---|
| POC-2B DataAgent tool loop | **`deepseek-chat`** | V3 非 thinking；强制 |
| POC-2A 独立 NL2SQL API | `deepseek-v4-pro` 或 `deepseek-chat` | 单次调用，thinking 也可用 |
| POC-1 / POC-1.5 | — | 不涉及 LLM |

### 10.3 启动 beelink

beelink 实例：`http://localhost:8998`（admin：`beelink / beelink@123`）
若未起，依据 `~/beelink_wenshu/` 项目本身的 README 启动（本归档不复制 beelink 启动细节）。

### 10.4 启动 Data Formulator

```bash
cd ~/beelinkDF
nohup setsid .venv/bin/python -m data_formulator --port 5500 --host 127.0.0.1 \
  < /dev/null > /tmp/df.log 2>&1 &
disown
sleep 6 && curl -s --max-time 3 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5500/

# 关闭
PIDS=$(pgrep -f "data_formulator.*--port 5500"); [ -n "$PIDS" ] && kill $PIDS
```

`setsid + nohup + disown` 是必要组合，避免 background bash 把 Flask 一起 kill（踩坑 #6）。

### 10.5 跑 Phase 0 probe

```bash
BEELINK_URL=http://localhost:8998 BEELINK_USER=beelink BEELINK_PASS='<beelink 密码>' \
  python3 scripts/beelink_phase0_probe.py
```

6 步全 PASS 即代表 beelink REST 可用、登录桥可用、SQL job 链路可用。

### 10.6 跑 POC-2A curl 验证

```bash
H='-H X-Identity-Id:local:beelink -H X-Workspace-Id:default -H Content-Type:application/json'
curl -s $H http://127.0.0.1:5500/api/connectors >/dev/null   # 触发 connector lazy-load

jq -n --arg key "$DEEPSEEK_API_KEY" \
  '{connector_id:"beelink:beelink-main",question:"按 gender 统计客户数",
    table_keys:["smartquery_demo.customers"],table_name:"q_demo",
    model:{endpoint:"openai",model:"deepseek-chat",api_key:$key,
           api_base:"https://api.deepseek.com",api_version:null,is_global:false},
    import_options:{size:10000,timeout:60}}' \
  | curl -s --max-time 60 $H -X POST http://127.0.0.1:5500/api/connectors/nl2sql-import -d @-
```

返回 `{sql, table_name, row_count, columns, reason}` 即 PASS。

### 10.7 跑 POC-2B DataAgent 验证

```bash
H='-H X-Identity-Id:local:beelink -H X-Workspace-Id:default -H Content-Type:application/json'
curl -s $H http://127.0.0.1:5500/api/connectors >/dev/null   # 必须先触发 connector lazy-load

jq -n --arg key "$DEEPSEEK_API_KEY" \
  '{model:{endpoint:"openai",model:"deepseek-chat",api_key:$key,
           api_base:"https://api.deepseek.com",api_version:null,is_global:false},
    input_tables:[], primary_tables:[],
    user_question:"用 beelink 数据源 smartquery_demo.customers 按 gender 统计客户数。"}' \
  | curl -s --max-time 180 $H -X POST http://127.0.0.1:5500/api/agent/data-agent-streaming -d @-
```

NDJSON 流中观察到 `query_beelink_sql` tool call → `tool_result` 成功 → 后续 `visualize` / `explore` → `final_answer` 即 PASS。

### 10.8 自检命令

```bash
# 语法 + 注册自检
.venv/bin/python -m py_compile \
  py-src/data_formulator/data_loader/beelink_data_loader.py \
  py-src/data_formulator/agents/nl2sql.py \
  py-src/data_formulator/agents/data_agent.py \
  py-src/data_formulator/data_connector.py

.venv/bin/python -c "from data_formulator.data_loader import DATA_LOADERS; \
                     print('beelink:', 'beelink' in DATA_LOADERS)"
.venv/bin/python -c "from data_formulator.agents.data_agent import TOOLS; \
                     print([t['function']['name'] for t in TOOLS])"
# 期望输出包含 query_beelink_sql
```

---

## 修订日志

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-19 | POC-1 → POC-2B 阶段闭门归档。代码基线 HEAD `aa7228d`，本归档在 v2.0 交接（`d73a820`）之上叠加一个 docs commit，不引入代码变更。 |
