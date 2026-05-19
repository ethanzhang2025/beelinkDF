# beelink 集成辅助脚本

## Phase 0：REST 全链路验证

`beelink_phase0_probe.py` —— 在写任何 DF 端代码之前，先用这个脚本确认本机
能跑通 beelink 的 6 个关键 REST 端点：

1. `POST /apiv2/login`（注意 V2 API 挂在 `/apiv2/*`，不是 `/login`）
2. `GET /api/v3/catalog`
3. `GET /api/v3/catalog/{id}?maxChildren=...`
4. `POST /api/v3/sql`
5. `GET /api/v3/job/{id}`（轮询）
6. `GET /api/v3/job/{id}/results?offset=&limit=`

> **关键事实**：beelink/Dremio 的 JAX-RS RestServerV2 在
> `dac/backend/.../server/RestServerV2.java:40` 用 `@RestApiServer(pathSpec="/apiv2/*")`
> 挂载。所以 `LogInLogOutResource.java` 上的 `@Path("/login")` 实际 URL 是
> **`/apiv2/login`**。这是初版设计文档据 `@Path("/login")` 直接推断时漏掉的
> Application 前缀。真机验证后已修正脚本与 `BeelinkDataLoader`。

### 运行方式

脚本只用 Python stdlib，不需要装任何第三方依赖：

```bash
# 1) 复制样例配置
cp scripts/beelink_phase0.env.example scripts/.env
$EDITOR scripts/.env   # 填上真实的 BEELINK_URL/USER/PASS

# 2) 加载环境变量并运行
set -a; source scripts/.env; set +a
python3 scripts/beelink_phase0_probe.py
```

或者一次性命令：

```bash
BEELINK_URL=http://localhost:9047 \
BEELINK_USER=zhangsan01 \
BEELINK_PASS='your-password' \
BEELINK_TEST_SQL='SELECT 1 AS n' \
python3 scripts/beelink_phase0_probe.py
```

### 输出与退出码

- 每一步输出 `[N] 标题` + `✓ PASS / ✗ FAIL / · SKIP`
- 全部通过：退出码 0，最后打印 `全部链路通过 ✓`
- 任一步失败：立即退出，退出码 1，附 HTTP 响应摘要（前 500 字符）
- 缺必需环境变量：退出码 2

### 调试建议

- 401 报错：先在 beelink Web UI 用同一账号登录确认密码正确
- catalog 列出来是空：账号权限不足；让管理员授权 SOURCE 后再试
- jobState 一直 RUNNING：检查 beelink Job 队列；脚本超时 30s 后退出
- TLS 报错：开发环境置 `BEELINK_INSECURE=1`；生产保持 0

### 必需环境变量

| 必需 | 名称 | 说明 |
|------|------|------|
| ✅ | `BEELINK_URL` | beelink 服务地址，无尾斜杠，例 `http://localhost:8998` |
| ✅ | `BEELINK_USER` | beelink 用户名 |
| ✅ | `BEELINK_PASS` | beelink 密码（仅本机进程内使用，绝不入仓） |
| ❌ | `BEELINK_SOURCE_ID` | 要展开 children 的 catalog id；空则自动选顶层首个 SOURCE |
| ❌ | `BEELINK_TEST_SQL` | 测试 SQL，默认 `SELECT 1 AS n` |
| ❌ | `BEELINK_TIMEOUT` | HTTP 单次超时秒，默认 15 |
| ❌ | `BEELINK_INSECURE` | 是否忽略 TLS 证书（1/0），默认 1 |

### 默认 SQL 为什么是 `SELECT 1 AS n`

`SELECT 1 AS one` 看似无害，但 Dremio 的 Calcite 解析器把 **`one` 当作保留字**
（解析器期望此处出现 `<IDENTIFIER>`），整条 SQL 会被拒绝并报：

```
Encountered "AS one" at line 1, column 10. Was expecting one of: ...
```

为了让默认值"开箱即跑"，改为 `SELECT 1 AS n`。如果你需要换成业务表的轻量
`SELECT`，避免用 `one / two / three / day / month / year / level / value` 等
常见 Calcite 保留字作为别名。

### 与 Phase 2 / DF UI 验证的关系

Phase 0 通过 = 证明 BeelinkDataLoader 在本机所需的所有 REST 都可用，
然后可以放心写 / 调试 `py-src/data_formulator/data_loader/beelink_data_loader.py`。

DF 侧通过 REST 验证 BeelinkDataLoader 时，额外注意：

* **`import-data` 要求 active workspace**。前端 UnifiedDataUploadDialog 会自动
  注入 `X-Workspace-Id` 头；用 curl 直调时要先建一个：

  ```bash
  curl -X POST -H "Content-Type: application/json" -H "X-Identity-Id: local:<you>" \
       http://127.0.0.1:5500/api/sessions/create -d '{"id":"default","name":"default"}'
  ```

  然后所有 `import-data` / `list-tables` 调用补上 `-H "X-Workspace-Id: default"`。
* `POST /api/connectors` 入参用 **`loader_type`**（不是 `source_type`），
  `POST /api/connectors/get-catalog-tree` 等共用动作端点用 **`connector_id`**
  （格式 `<loader_type>:<safe-source-id>`，如 `beelink:beelink-main`）。

### 本阶段范围

本目录脚本与 `BeelinkDataLoader` 只覆盖 **Phase 0（验证）+ Phase 2（POC-1 表导入）**。

明确**不包含**：

* Phase 3 / POC-1.5 —— 手写 SQL 直通端点（`POST /api/beelink/sql/execute`）
* Phase 4 / POC-2 —— 自然语言 → Dremio SQL（DataAgent 工具扩展）
* DataAgent 任何改动；前端任何改动；beelink Java 任何改动。

这些项在 `docs/beelink-poc-design/df_beelink_poc1_to_poc2_design_v10.md`
里已有完整设计，待 POC-1 用户验收通过后再开新分支推进。
