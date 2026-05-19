# POC-1 → POC-2B 演示与验收手册

> 日期：2026-05-19
> 适用分支：`feat/beelink-poc2b-dataagent-tool`（HEAD ≥ `db5d0f7`）
> 目标读者：接手演示 / 验收 / 评审 POC 的工程师与产品方
> 范围：Phase 0 / POC-1 / POC-1.5 / POC-2A / POC-2B 的可复现验证步骤

阅读顺序建议：§1 环境 → §2 启动顺序 → §3 ~ §7 五个阶段验证 → §8 常见问题 → §9 安全说明。

---

## 1. 环境变量准备

**不要**把任何真实凭证写入文件或 commit。所有敏感值走 shell `export`，进程退出即消失。

| 变量 | 用途 | 示例（占位） |
|---|---|---|
| `BEELINK_URL` | beelink 服务地址（无尾斜杠） | `http://localhost:8998` |
| `BEELINK_USER` | beelink 用户名 | `<你的 beelink 账号>` |
| `BEELINK_PASS` | beelink 密码 | `<你的 beelink 密码>` |
| `DF_URL` | Data Formulator 服务地址 | `http://127.0.0.1:5500` |
| `DF_CONNECTOR_ID` | 已注册的 beelink connector id | `beelink:beelink-main` |
| `DF_WORKSPACE_ID` | DF workspace 标识 | `default` |
| `DF_IDENTITY_ID` | DF 身份标识（HTTP 头） | `local:beelink` |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（仅 POC-2A/2B 需要） | `<你的 DeepSeek API Key>` |

示例（仅占位符，请替换为真实值，**不要写进 .env 提交**）：

```bash
export BEELINK_URL=http://localhost:8998
export BEELINK_USER='<beelink 用户名>'
export BEELINK_PASS='<beelink 密码>'
export DF_URL=http://127.0.0.1:5500
export DF_CONNECTOR_ID=beelink:beelink-main
export DF_WORKSPACE_ID=default
export DF_IDENTITY_ID=local:beelink
export DEEPSEEK_API_KEY='<DeepSeek API Key>'
```

---

## 2. 启动顺序

### 2.1 启动 beelink

beelink 实例：`$BEELINK_URL`。
启动方式见 `~/beelink_wenshu/` 项目本身的 README；本手册不复制 beelink 启动细节。

冒烟检查：

```bash
curl -s --max-time 3 -o /dev/null -w "%{http_code}\n" "$BEELINK_URL/"
# 期望：200
```

### 2.2 启动 Data Formulator

```bash
cd ~/beelinkDF && source .venv/bin/activate
nohup setsid .venv/bin/python -m data_formulator --port 5500 --host 127.0.0.1 \
  < /dev/null > /tmp/df.log 2>&1 &
disown
sleep 6 && curl -s --max-time 3 -o /dev/null -w "%{http_code}\n" "$DF_URL/"
# 期望：200

# 关闭命令（必要时）
# PIDS=$(pgrep -f "data_formulator.*--port 5500"); [ -n "$PIDS" ] && kill $PIDS
```

> `setsid + nohup + disown` 是必要组合，避免 background bash 把 Flask 一起 kill（v10 文档踩坑 #6）。

### 2.3 确认 connector lazy-load

DF 重启后 `_user_connector_key` 注册表为空，首次跨阶段调用前必须先触发一次 connector 列表，让 vault auto-reconnect：

```bash
H_BASE=(-H "X-Identity-Id: $DF_IDENTITY_ID" -H "X-Workspace-Id: $DF_WORKSPACE_ID" -H "Content-Type: application/json")
curl -s "${H_BASE[@]}" "$DF_URL/api/connectors" >/dev/null
```

### 2.4 确认 workspace / session

DF 端 `import-data` / `import-sql` / `nl2sql-import` 都要求活动 workspace。若是新环境，先建一个：

```bash
curl -s "${H_BASE[@]}" -X POST "$DF_URL/api/sessions/create" \
  -d "{\"id\":\"$DF_WORKSPACE_ID\",\"name\":\"$DF_WORKSPACE_ID\"}"
```

---

## 3. Phase 0 验证

**目的**：确认本机 beelink REST 6 步全可用，是 POC 链路的"地基"。

```bash
BEELINK_URL="$BEELINK_URL" BEELINK_USER="$BEELINK_USER" BEELINK_PASS="$BEELINK_PASS" \
  python3 scripts/beelink_phase0_probe.py
```

**预期通过项**（依次输出 `[OK]`）：

1. `POST /apiv2/login` 取 token
2. `GET /api/v3/catalog` 列出 SOURCE
3. `GET /api/v3/catalog/{id}?maxChildren=...` 展开顶层 SOURCE 的 children
4. `POST /api/v3/sql` 提交 `SELECT 1 AS n`
5. `GET /api/v3/job/{id}` 轮询直至 `COMPLETED`
6. `GET /api/v3/job/{id}/results?offset=0&limit=500` 读到 `n=1`

**任何一步失败**：参考 `scripts/README.md` 调试建议（401 改密码、catalog 空 → 授权 SOURCE、jobState 卡 RUNNING → 检查 beelink Job 队列）。

---

## 4. POC-1 验证：import-data

**目的**：DF 标准表导入端到端通。

```bash
curl -s --max-time 30 "${H_BASE[@]}" -X POST "$DF_URL/api/connectors/import-data" \
  -d "{
    \"connector_id\": \"$DF_CONNECTOR_ID\",
    \"source_table\": \"smartquery_demo.customers\",
    \"table_name\": \"customers\",
    \"import_options\": {\"size\": 1000}
  }"
```

**预期返回**：

```json
{
  "table_name": "customers",
  "row_count": 1000,
  "columns": [{"name": "id", "type": "..."}, ...],
  "refreshable": true,
  "source_query": "SELECT * FROM \"smartquery_demo\".\"customers\" LIMIT 1000",
  ...
}
```

关键校验：
- `row_count` 与 beelink 端实际表的 `LIMIT 1000` 行数一致
- `refreshable: true`（表导入可刷新）
- `source_query` 为 DF 自动构造的 `SELECT * FROM ... LIMIT N`

---

## 5. POC-1.5 验证：import-sql

**目的**：用户手写 SQL 直通导入。

### 5.1 最小冒烟用例

```bash
curl -s --max-time 20 "${H_BASE[@]}" -X POST "$DF_URL/api/connectors/import-sql" \
  -d "{
    \"connector_id\": \"$DF_CONNECTOR_ID\",
    \"sql\": \"SELECT 42 AS n\",
    \"table_name\": \"t_smoke\"
  }"
```

**预期返回**：

```json
{
  "table_name": "t_smoke",
  "row_count": 1,
  "columns": [{"name": "n", "type": "..."}],
  "source_query": "SELECT 42 AS n",
  ...
}
```

关键校验：
- `row_count == 1`
- `source_query` 与提交的 SQL **逐字一致**（手写 SQL 不经 DF 改写）
- 列名 `n`（**避免** `one / two / three / day / month / year / level / value` 等 Calcite 保留字作别名，会被解析器拒绝）

### 5.2 多段引号 SQL（业务表）

```bash
curl -s --max-time 30 "${H_BASE[@]}" -X POST "$DF_URL/api/connectors/import-sql" \
  -d "{
    \"connector_id\": \"$DF_CONNECTOR_ID\",
    \"sql\": \"SELECT gender, COUNT(*) AS cnt FROM \\\"smartquery_demo\\\".\\\"customers\\\" GROUP BY gender\",
    \"table_name\": \"customers_by_gender\"
  }"
```

预期 `row_count` 为业务表中 distinct gender 数量。

---

## 6. POC-2A 验证：nl2sql-import

**目的**：独立后端 NL2SQL —— DF 后端单次调 LLM 生成 SQL → beelink 执行 → 落 workspace。

### 6.1 推荐模型

| 模型 | 适用 | 备注 |
|---|---|---|
| `deepseek-chat` | ✅ POC-2A | V3 非 thinking，最稳 |
| `deepseek-v4-pro` | ✅ POC-2A | thinking 模式，单次调用 OK |
| `deepseek-v4-flash` | ✅ POC-2A | thinking 模式，单次调用 OK |

POC-2A 是单次调用，thinking 与非 thinking 都可用。

### 6.2 curl 示例（DeepSeek Pro）

```bash
[ -n "$DEEPSEEK_API_KEY" ] || { echo "请先 export DEEPSEEK_API_KEY"; exit 1; }

jq -n --arg key "$DEEPSEEK_API_KEY" --arg cid "$DF_CONNECTOR_ID" \
  '{connector_id: $cid,
    question: "按 gender 统计客户数",
    table_keys: ["smartquery_demo.customers"],
    table_name: "q_by_gender",
    model: {endpoint: "openai", model: "deepseek-v4-pro",
            api_key: $key, api_base: "https://api.deepseek.com",
            api_version: null, is_global: false},
    import_options: {size: 10000, timeout: 60}}' \
  | curl -s --max-time 60 "${H_BASE[@]}" -X POST "$DF_URL/api/connectors/nl2sql-import" -d @-
```

**预期返回**：

```json
{
  "table_name": "q_by_gender",
  "row_count": <distinct gender 数>,
  "columns": [{"name":"gender","type":"..."},{"name":"<count列>","type":"..."}],
  "source_query": "SELECT gender, COUNT(*) ... FROM \"smartquery_demo\".\"customers\" GROUP BY gender",
  "reason": "<LLM 生成 SQL 的简短理由>",
  ...
}
```

关键校验：
- `source_query` 是合法 `SELECT` 或 `WITH` 开头（经 `_is_select_or_with` 校验）
- `reason` 字段非空（说明 LLM 走通 JSON-only prompt）
- 同 `table_name` 重复调用时，第 2 次表名自动 `_2`（`workspace.get_fresh_name`）

### 6.3 错误码自检

故意把 `api_key` 改为空串或错值，预期错误码：

| 故障注入 | 期望错误码 |
|---|---|
| 空 `api_key` / 错 key | `LLM_AUTH_FAILED` |
| 故意造 429（高频调） | `LLM_RATE_LIMIT` |
| `model: "non-existent"` | `LLM_UNKNOWN_ERROR` |
| `question: ""` | `INVALID_REQUEST` |
| `connector_id: "beelink:nonexistent"` | `CONNECTOR_ERROR` |

---

## 7. POC-2B 验证：DataAgent + query_beelink_sql

**目的**：DataAgent 自循环中通过 `query_beelink_sql` tool 在 beelink 上跑 LLM 生成的 SQL，结果落 workspace 供后续 `visualize` / `explore` 出图。

### 7.1 模型强制要求

| 模型 | DataAgent tool loop | 备注 |
|---|---|---|
| **`deepseek-chat`** | ✅ **推荐** | V3 非 thinking；唯一兼容 |
| `deepseek-v4-pro` | ❌ **不推荐** | thinking 模式，`reasoning_content` 未被 DataAgent `_call_llm_once` 回传，报 400 |
| `deepseek-v4-flash` | ❌ **不推荐** | 同上 |
| `deepseek-reasoner` | ❌ **不推荐** | 同上 |

**严格用 `deepseek-chat`**。修复 thinking 兼容需改 `_call_llm_once` 主循环，留待后续分支。

### 7.2 通过 UI 验证（推荐路径）

1. 浏览器打开 `$DF_URL`，进入 Data Threads；
2. 顶部 Model 选 `deepseek-chat`（`api_base: https://api.deepseek.com`，key 用 `$DEEPSEEK_API_KEY`，不要写文件保存）；
3. 在 DataAgent 输入框提问：
   > **看下 smartquery_demo.customers 各 gender 客户数**
4. 预期 DataAgent 行为：
   - 调用 `query_beelink_sql` tool，参数 `sql` 形如 `SELECT gender, COUNT(*) AS cnt FROM "smartquery_demo"."customers" GROUP BY gender`
   - tool 返回成功，workspace 新增一张表（如 `customers_by_gender`）
   - 后续 `visualize` 或 `explore` tool 出柱状图 / 表格
   - `final_answer` 给出结论文本

### 7.3 通过 curl 验证（流式 NDJSON）

```bash
[ -n "$DEEPSEEK_API_KEY" ] || { echo "请先 export DEEPSEEK_API_KEY"; exit 1; }

# 必须先触发 connector lazy-load（§2.3）
curl -s "${H_BASE[@]}" "$DF_URL/api/connectors" >/dev/null

jq -n --arg key "$DEEPSEEK_API_KEY" \
  '{model: {endpoint: "openai", model: "deepseek-chat",
            api_key: $key, api_base: "https://api.deepseek.com",
            api_version: null, is_global: false},
    input_tables: [], primary_tables: [],
    user_question: "看下 smartquery_demo.customers 各 gender 客户数。"}' \
  | curl -s --max-time 180 "${H_BASE[@]}" -X POST "$DF_URL/api/agent/data-agent-streaming" -d @-
```

**预期 NDJSON 流**（逐行 JSON，按顺序观察）：

1. `{"type":"tool_call","name":"query_beelink_sql", "arguments":{...}}`
2. `{"type":"tool_result","name":"query_beelink_sql","result":{"table_name":"...","row_count":<n>,...}}`
3. `{"type":"tool_call","name":"visualize"...}` 或 `"explore"`
4. `{"type":"final_answer","content":"..."}`

任意一步缺失或 `tool_result` 中 `errorMessage` 非空且 12 轮内未自愈，记入失败。

---

## 8. 常见问题

| # | 现象 | 根因 | 处置 |
|---|---|---|---|
| 1 | `DEEPSEEK_API_KEY 未设置` 提示 | 未 `export` 或当前 shell 丢失变量 | `export DEEPSEEK_API_KEY=...`；不要写文件保存 |
| 2 | DataAgent / NL2SQL 报 `Connector not found` | DF 重启后 connector lazy-load 未触发 | 先 `curl $DF_URL/api/connectors`（§2.3）再发流 |
| 3 | `import-data` / `import-sql` 报 `No active workspace` | 未建 workspace 或未带 `X-Workspace-Id` 头 | 走 §2.4 建 workspace；所有 curl 用 `H_BASE` 数组 |
| 4 | DataAgent 报 `400 Bad Request` 含 `reasoning_content` | 误用 `deepseek-v4-pro/flash/reasoner` | 切回 `deepseek-chat`（§7.1） |
| 5 | workspace 中间表越来越多（`_2 / _3 / ...`） | DataAgent 多轮调用 `query_beelink_sql` 同名 table；`workspace.get_fresh_name` 自动加后缀避免覆盖 | 演示后手动清理 workspace；POC-2C 才考虑抑制 |
| 6 | `import-sql` 报 Calcite 解析错 | 别名命中保留字（`one/two/three/day/...`） | 改用 `n` 等普通别名 |
| 7 | DataAgent 反复调 `query_beelink_sql` 但 SQL 一直错 | LLM 不熟悉表 schema 或字段语义不清 | 在问题里显式给表名 + 关键字段；或先用 POC-1 表导入让 DataAgent 走 `explore` |
| 8 | 返回 `ACCESS_DENIED` / `CONNECTOR_AUTH_FAILED` | beelink 端权限或凭证失效 | 由 beelink 返回，**权限是 beelink 裁决**；让管理员授权 SOURCE 或检查 beelink 账号 |
| 9 | `import-data` 走 5000 条 limit 报错 | beelink `JobResource.java:125` 硬上限 500/页 | `BeelinkDataLoader` 已自动 500/页分页；不要手动绕过 |
| 10 | TLS 证书报错 | 自签证书 | Phase 0 脚本设 `BEELINK_INSECURE=1`；生产环境务必 `0` 并配可信证书 |

---

## 9. 安全说明

| 项 | 规则 |
|---|---|
| API key | 仅 `export DEEPSEEK_API_KEY=...`，**不**写入 `.env` 入仓，**不**写入 curl 脚本，**不**写入 commit message |
| beelink 密码 | 同上，仅本机 shell 变量；演示文档里只写 `<占位符>` |
| `.env` 文件 | 项目根 `.gitignore` 已忽略；任何 `*.env` 都不入仓 |
| curl 文件 | 演示用 curl 命令请直接在 shell 跑，不要保存为 `.sh` 提交；如需保存模板，把所有凭证字段写成 `$VAR` |
| 截图 / 录屏 | 截图 / 录屏前确保浏览器开发者工具 Network 面板已关闭，避免 `Authorization` / `Cookie` 头被录入 |
| Token / Cookie | beelink 登录 token 仅存内存，**不**打印、**不**截图、**不**写日志 |
| 演示视频 | 上传前过一遍 §1 变量名，确保画面不出现真实 host / 真实账号；模型 key 不出现在 URL 或 headers |

---

## 10. 配套 smoke 脚本（可选）

`scripts/beelink_poc_smoke.py` 提供 POC-1 / POC-1.5 / POC-2A 的一键 HTTP 验证（纯 stdlib，不依赖 `requests`）。
POC-2B 因走 NDJSON 流 + DataAgent 多轮，**不在 smoke 脚本范围内**，按 §7 通过 UI / curl 验证。

用法：

```bash
# 只跑 POC-1
python3 scripts/beelink_poc_smoke.py --poc1

# 跑 POC-1 / 1.5 / 2A 全套
python3 scripts/beelink_poc_smoke.py --poc1 --poc15 --poc2a

# Phase 0 直接调既有探测脚本
python3 scripts/beelink_poc_smoke.py --phase0
```

脚本严格不打印 `api_key / Authorization / Cookie` 等敏感字段；任何一步失败退出码非 0。详见脚本顶端 docstring。

---

## 修订日志

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-05-19 | POC-1 → POC-2B 演示与验收手册首版。配套 `scripts/beelink_poc_smoke.py`。 |
