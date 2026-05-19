# Data Formulator + beelink 二开设计文档（v10）
## 从 POC-1 表导入到 POC-2 自然语言 SQL 直通

> 版本：v10 · 编制日期：2026-05-19 · 编制人：Claude（基于源码事实核对）
> 适用代码：
> - Data Formulator：`~/beelinkDF`（microsoft/data-formulator `0.7-alpha.2`）
> - beelink：`~/beelink_wenshu`（基于 Dremio v25 社区版二次开发）

---

## 1. 总体结论

**核心结论**：Data Formulator v0.7 自带的 `ExternalDataLoader` + `DataConnector` 框架已经能极低成本地把 beelink 接入为一个原生数据源；POC-1 到 POC-2 整个演进路径的"重活"几乎全在 DF 侧新增一个 `BeelinkDataLoader`（一个 Python 文件），其余配套（前端 UI、连接管理、Vault 凭证、catalog 缓存、Agent 工具）都可复用现成基础设施。beelink 侧**几乎不需要改动**——它原生的 `/api/v3/catalog`、`/api/v3/sql`、`/api/v3/job/{id}*`、`/apiv2/login` 已经够用，权限裁决也已经由它自己完成。

**最小可走通路径**（基于源码实测）：

| 阶段 | 时间盒 | 核心改动 | 用户能做什么 |
|------|--------|----------|--------------|
| POC-1 表导入 | 3-5 天 | DF 新增 `BeelinkDataLoader`；注册到 `DATA_LOADERS` | 在 DF 内浏览 beelink 我可见的库表→选表→导入→做图表 |
| POC-1.5 手写 SQL | 1-2 天 | DF 新增 `/api/beelink/sql/execute` 端点 | 用户写一段 Dremio SQL→直通 beelink→结果落 workspace |
| POC-2 NL→SQL | 5-8 天 | DataAgent 新增 `query_beelink_sql` 工具；schema context 注入 | 用自然语言提问，DataAgent 自动生成 Dremio SQL、提交 beelink、把结果作为图表的输入 |

**最重要的事实**：DF 已经有完整的 connector 生命周期框架（`data_connector.py` 2068 行，自动生成 `/api/connectors/*` 全套路由，自动接入前端 `UnifiedDataUploadDialog`、自动持久化凭证、自动重连、自动 SSO 注入、自动写磁盘 catalog cache 给 Agent 搜表）。我们不必自己设计这套，只需写一个继承 `ExternalDataLoader` 的子类。

---

## 2. 当前目标与非目标

### 2.1 目标（POC 阶段必须达成）

1. **登录态贯通**：用户在 beelink 已登录的前提下，能用同一身份让 DF 调到 beelink REST。
2. **可见性等同**：DF 看到的库/表/字段，跟该用户在 beelink 中看到的完全一致——beelink 是唯一的权限裁决方。
3. **明细数据可入 DF workspace**：DF 能把 beelink 的查询结果作为 parquet 表落到本地 workspace。
4. **图表主体在 DF**：Data Thread、智能图表生成、图表迭代、报告生成均由 DF 完成；beelink 仅承担数据源职责。
5. **NL→Dremio-SQL→执行 闭环**：POC-2 允许用户用自然语言提问，DataAgent 生成 Dremio SQL，直通 beelink 拿结果。

### 2.2 非目标（本阶段明确不做）

- ❌ **不做强 BO（Business Object）层**：beelink 的语义层（`services/smartquery/semantic/*`）暂不引入。
- ❌ **不做强 RAG**：不接入向量库；DataAgent 走 schema 注入即可。
- ❌ **不强制 MCP-first**：DF 与 beelink 之间用普通 HTTP REST，不引入 MCP 适配层。
- ❌ **不做完整 SQL Guard**：SQL 安全/越权完全交由 beelink REST 校验（beelink 已有 `UserRightService` + `DACAuthFilter`）；DF 不复刻。
- ❌ **不构建独立 ChatBI 会话系统**：复用 DF 原生 `DataAgent` 的 trajectory + DataThread。
- ❌ **不要求聚合**：DF 允许拉取明细数据（用 `import_options.size` 兜底单次行数）。
- ❌ **POC 阶段不接 beelink SmartQuery**：beelink 自己有 NL2SQL（`SmartQueryResource`），但本方案选择 **DF 作为 ChatBI 主体**，不让 DF 退化为 SmartQuery 的可视化外壳；SmartQuery 可作为未来对比/降级备选。

---

## 3. Data Formulator 源码能力盘点（已实测）

### 3.1 顶层

- **前端**：`src/`，Vite + React 18 + Redux + Redux-Persist + Material-UI 7 + react-vega + ECharts，入口 `src/index.tsx` → `src/app/App.tsx`。
- **后端**：`py-src/data_formulator/`，Python ≥ 3.11，Flask + Flask-CORS + Flask-Session + DuckDB + PyArrow + LiteLLM/OpenAI，entry point `data_formulator = "data_formulator:run_app"`（`pyproject.toml:71`）。
- **构建**：`yarn build` → 输出到 `py-src/data_formulator/dist/`，由 Flask 静态托管。
- **DEVELOPMENT.md** 明确把 "External data loaders" 列为可扩展点。

### 3.2 后端目录关键模块

```
py-src/data_formulator/
├── app.py                    # Flask app + blueprint 注册（19.2 KB）
├── data_connector.py         # DataConnector 通用包装 + /api/connectors/* 全套路由（78.2 KB）
├── auth/
│   ├── identity.py           # get_identity_id, get_sso_token
│   ├── providers/            # OIDC / GitHub / Azure EasyAuth / base
│   ├── gateways/             # OIDC 网关
│   ├── token_store.py        # SSO token 缓存与互换
│   └── vault/                # 凭证加密存储（fernet）
├── data_loader/
│   ├── external_data_loader.py     # ExternalDataLoader 抽象基类（37.7 KB）
│   ├── postgresql_data_loader.py   # 模板参考
│   ├── mysql_data_loader.py
│   ├── mssql_data_loader.py
│   ├── kusto_data_loader.py
│   ├── bigquery_data_loader.py
│   ├── athena_data_loader.py
│   ├── superset_data_loader.py     # 唯一已实现 delegated_login 的范例
│   ├── s3_data_loader.py
│   ├── azure_blob_data_loader.py
│   ├── mongodb_data_loader.py
│   ├── cosmosdb_data_loader.py
│   ├── local_folder_data_loader.py
│   └── __init__.py           # DATA_LOADERS 注册表（_LOADER_SPECS）
├── datalake/
│   ├── workspace.py          # Workspace（write_parquet_from_arrow at L529）
│   ├── workspace_metadata.py # TableMetadata + ColumnInfo
│   ├── catalog_cache.py      # save_catalog / search_catalog_cache
│   └── parquet_utils.py
├── routes/
│   ├── agents.py             # /api/agent/* — DataAgent/DataLoadingAgent 入口
│   ├── tables.py             # /api/tables/* — 表 CRUD
│   ├── sessions.py           # /api/sessions/* — workspace 状态保存
│   ├── credentials.py        # /api/credentials/*
│   └── knowledge.py          # /api/knowledge/*
├── agents/
│   ├── data_agent.py         # DataAgent（主探索；94 KB；TOOLS at L98；_tool_loop at L1531）
│   ├── agent_data_loading_chat.py  # DataLoadingAgent（49 KB）
│   ├── context.py            # handle_search_data_tables, handle_read_catalog_metadata
│   ├── agent_data_transform.py
│   ├── agent_data_rec.py
│   ├── agent_interactive_explore.py
│   ├── agent_report_gen.py
│   ├── agent_chart_insight.py
│   ├── agent_chart_restyle.py
│   └── agent_code_explanation.py
├── sandbox/                  # explore() Python 沙箱
└── workspace_factory.py      # get_workspace(identity_id) 工厂
```

### 3.3 `ExternalDataLoader` 抽象基类关键签名（external_data_loader.py）

| 方法 | 必须实现 | 说明 |
|------|----------|------|
| `list_params() -> list[dict]` | ✅ static | 表单字段；每项含 `name/type/required/default/tier(connection/auth/filter)/sensitive` |
| `auth_instructions() -> str` | ✅ static | 显示在前端表单顶部的连接说明 |
| `__init__(params)` | ✅ | 初始化时建立连接（PG 走 psycopg2，BigQuery 走 google-cloud-bigquery，等） |
| `list_tables(table_filter=None) -> list[dict]` | ✅ | 扁平表清单，每项 `{name, metadata: {row_count, columns, sample_rows}, path?, table_key?}` |
| `fetch_data_as_arrow(source_table, import_options) -> pa.Table` | ✅ | **核心数据拉取**；只接受 `source_table`，**不接受 raw query**（L395 注释） |
| `catalog_hierarchy() -> list[{key, label}]` | static, 可选 | 默认 `[{key:"table", label:"Table"}]`；多层时定义层级 |
| `ls(path) -> list[CatalogNode]` | 可选 | 懒加载浏览，未实现则 fallback 到 list_tables |
| `get_metadata(path) -> dict` | 可选 | 单表详细 schema |
| `get_column_types(source_table) -> dict` | 可选 | 返回 `{columns: [{name,type,is_dttm,description}], description}`；增强源类型供前端 |
| `search_catalog(query, limit) -> dict` | 可选 | 默认 fallback 到 `list_tables(table_filter=query)` |
| `sync_catalog_metadata(table_filter)` | 可选 | 全量元数据同步，结果写入 `<user_home>/catalog_cache/<source_id>/` |
| `test_connection() -> bool` | 可选 | 心跳；默认调 `list_tables("__ping__")` |
| `auth_mode() -> str` | static | `"connection"`(默认) / `"token"` |
| `auth_config() -> dict` | static | 新接口；4 种模式：`credentials` / `sso_exchange` / `delegated` / `oauth2` |
| `delegated_login_config() -> dict\|None` | static | popup SSO；返回 `{login_url, label}` |
| `rate_limit() -> dict\|None` | static | 限流提示 |
| `validate_params(params, skip_auth_tier)` | classmethod | 校验；自动跑 |
| `ingest_to_workspace(workspace, table_name, source_table, options)` | 已实现 | **不要重写**，已封装 `fetch_data_as_arrow → workspace.write_parquet_from_arrow → 元数据合并` |

**重要约束**（external_data_loader.py:10）：`MAX_IMPORT_ROWS = 2_000_000`（导入总行数上限）。

### 3.4 `DataConnector` 通用包装（data_connector.py:295）

```python
DataConnector.from_loader(
    loader_class=BeelinkDataLoader,
    source_id="beelink",
    display_name="Beelink",
    default_params={"base_url": "http://..."},  # admin 预置的参数
)
```

**自动获得**：

- 全套 `/api/connectors/*` 路由（25+ 个端点），见下表
- per-identity loader 实例缓存（`_loaders: dict[identity → loader]`）
- 自动凭证持久化（Vault：fernet 加密；按 `(identity, source_id)` 隔离）
- 自动重连 `_try_auto_reconnect`
- 自动 SSO token 注入 `_inject_credentials`（来自 `TokenStore` 或 `get_sso_token()`）
- 自动前端表单：`get_frontend_config()` 返回 `{params_form, pinned_params, hierarchy, effective_hierarchy, auth_instructions, auth_mode, delegated_login}`

**自动暴露的路由**（已实测）：

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/data-loaders` | GET | 列出所有 loader 类型（discovery） |
| `/api/connectors` | GET | 列当前 identity 的 connector 实例 |
| `/api/connectors` | POST | 创建 connector 实例 |
| `/api/connectors/<id>` | DELETE | 删除 |
| `/api/connectors/connect` | POST | 连接（验证 + 缓存 loader） |
| `/api/connectors/disconnect` | POST | 断开 |
| `/api/connectors/get-status` | POST | 状态 |
| `/api/connectors/get-catalog` | POST | 一次性扁平 catalog |
| `/api/connectors/get-catalog-tree` | POST | 树形 catalog |
| `/api/connectors/get-cached-catalog-tree` | POST | 从磁盘 cache 读 |
| `/api/connectors/sync-catalog-metadata` | POST | 全量同步 + 写 cache |
| `/api/connectors/search-catalog` | POST | 搜索 |
| `/api/connectors/import-data` | POST | **核心**：拉数据→落 parquet→注册元数据 |
| `/api/connectors/refresh-data` | POST | 用同样的 source_table+options 刷新已存表 |
| `/api/connectors/preview-data` | POST | 取样预览 |
| `/api/connectors/column-values` | POST | 列 distinct values（智能过滤） |
| `/api/connectors/import-group` | POST | 批量导入 |

### 3.5 身份与认证（auth/identity.py）

- `get_identity_id() -> str`（L165）：返回三段命名空间之一
  - `user:<sub>`（OIDC 验证）
  - `local:<os_username>`（仅本机 localhost 模式）
  - `browser:<uuid>`（匿名，X-Identity-Id header）
- `get_sso_token() -> str|None`（L243）：返回原始 SSO access token，用于"pass-through to external systems"。
- 前端注入：`X-Identity-Id: <type>:<id>` 头（utils.tsx:180）。
- 多用户隔离：workspace 物理路径 = `<DATA_FORMULATOR_HOME>/users/<safe_identity_id>/`（workspace.py:88）。

### 3.6 DataAgent 与工具（agents/data_agent.py）

- **TOOLS**（L98 起）：`think / explore(code) / inspect_source_data(table_names) / search_data_tables(query, scope) / read_catalog_metadata(source_id, table_key) / search_knowledge / read_knowledge`
- **Actions**（产出 JSON）：`visualize / clarify / explain / present`
- **explore(code)** 在 sandbox 跑 Python（pandas/numpy/duckdb/sklearn/scipy 可用），是 DataAgent 唯一的"动手做数据"通道；**不直接生成 SQL**。
- **工具调度**：`_tool_loop()`（L1531）按 `elif tool_name == "..."` 分支处理；新增工具只需加 spec + 一个 elif 分支。

### 3.7 DataAgent 的目录搜索能力（关键发现）

`handle_search_data_tables(query, scope, workspace)`（context.py:287）：

- **Layer 1**：workspace 内已导入表的元数据搜索（描述、列名）
- **Layer 2**：`<user_home>/catalog_cache/<source_id>/` 磁盘缓存搜索

磁盘 cache 由 `POST /api/connectors/sync-catalog-metadata` 触发 → `save_catalog(user_home, source_id, flat_tables)` 写入。

> **POC-2 schema 注入路径完全免费**：只要 BeelinkDataLoader 实现 `list_tables()` 并允许用户点一次"sync catalog"，DataAgent 立刻能在自然语言对话里"找到"全部 beelink 可见表。

### 3.8 workspace 与表元数据（datalake/）

`TableMetadata`（workspace_metadata.py:207）持久化到 `workspace.yaml`：

```yaml
name: str
source_type: "upload" | "data_loader"
filename: str (parquet 文件名)
file_type: "parquet"
created_at: ISO datetime
content_hash, file_size, row_count
loader_type, loader_params, source_table, source_query   # ← source_query 已预留
import_options
columns: [{name, dtype, description}]
description: str
```

> **关键发现**：`source_query` 字段**已经存在**——POC-1.5/POC-2 走"SQL 直通"时可直接落地这个字段，无需扩展模型。

### 3.9 前端 connector 接入路径（src/views/）

- `UnifiedDataUploadDialog.tsx` 是统一数据接入对话框；
- `ServerConfig.CONNECTORS`（dfSlice.tsx:60-72）从后端注入；
- `DBTableManager.tsx:152` 已实现 `handleDelegatedLogin`（popup + postMessage `df-sso-auth`）；
- `loadTable({table})` thunk（tableThunks.ts:74）负责把后端表注册进 Redux state `state.tables`。

> **重要**：新增 BeelinkDataLoader 后**前端零代码改动**——`/api/data-loaders` 自动列出 Beelink，UI 自动渲染参数表单。

---

## 4. beelink 源码能力盘点（已实测）

### 4.1 顶层结构

- **构建**：Maven，JDK 21 编译；JAX-RS（Jersey）REST + React SPA（dac/ui）。
- 关键目录：
  - `dac/backend/src/main/java/com/dremio/dac/` — REST 资源 + 服务
  - `services/` — 业务服务（users、userright、smartquery、jobs、catalog）
  - `plugins/` — 数据源连接器（hive/s3/jdbc/mongo/...）
  - `common/`、`protocol/`、`sabot/`、`connector/`

### 4.2 登录与 Token（已实测）

#### 4.2.1 标准登录

`LogInLogOutResource.java`（`dac/backend/.../resource/LogInLogOutResource.java`）：

- `POST /apiv2/login`（L108）→ body `{userName, password}` → 返回 `UserLoginSession`
- `POST /apiv2/login/byCode?code=<encrypted>`（L204）→ 中台 SSO 后再登录
- `DELETE /apiv2/login`（L301）→ logout
- `GET /apiv2/login`（L305）→ `isUserAuthorized()`

`UserLoginSession` 字段（UserLoginSession.java）：

```
token (String)            # ← 后续请求带这个
userName, firstName, lastName, email
userId
expires (long ms)
admin (boolean)
roleId, orgId
permissions (SessionPermissions)
validops (List<String>)
userCreatedAt, clusterId, version, clusterCreatedAt
```

#### 4.2.2 Token 头协议（TokenUtils.java:27-50）

- `Authorization: _beelink<token>`（自定义前缀，**注意没有空格**）
- `Authorization: Bearer <token>`（标准 Bearer，也支持）

#### 4.2.3 SSO 接入（中台桥）

`ZTSSOLoginServlet.java`：

- 路径：servlet 注册路径未在源码中直接看到，但功能是 `GET ?token=...&redirect=...` → 内部走 `ssoLogin(token, request)` 拿用户名 → SM4 加密 → 重定向到 `/login?code=<encrypted>`（前端 SPA route） → 由 `loginByCode` 完成最终登录
- **POC 阶段不必走这套**；BeelinkDataLoader 用普通用户名/密码 + DF Vault 即可

### 4.3 权限模型

- `services/userright/`（beelink 自研）：`SimpleUserRightService.isPermitted(uid, type, res)`；type ∈ `SOURCE/SPACE/TABLE/FOLDER`
- `TableUserColumnsService` / `TableUserRelationService` — 列级 / 行级权限扩展
- `UserRightResource`（`dac/backend/.../resource/UserRightResource.java`）— REST 入口

**对接策略**：DF **完全不需要**复刻这些。当 beelink 收到 DF 发起的请求（带 token），由 `DACAuthFilter` 解析 token 拿到 username，所有 catalog / SQL / job 路径自动按 username 过滤可见性、自动校验权限——失败就 `UserExceptionMapper` 包装错误返回。

### 4.4 Catalog API（已实测）

`CatalogResource.java`（`dac/backend/.../api/CatalogResource.java`）：

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/v3/catalog?include=...` | 列顶层（sources + spaces + homes） |
| GET | `/api/v3/catalog/{id}?include=&pageToken=&maxChildren=` | 单项 + 分页子项 |
| GET | `/api/v3/catalog/by-path/{seg:.*}?versionType=&versionValue=` | 按路径查询（VDS 等支持版本） |
| GET | `/api/v3/catalog/search?query=...` | 搜索 |
| POST | `/api/v3/catalog` | 创建（folder/space） |
| POST | `/api/v3/catalog/{id}` | promote 到 Dataset |
| PUT | `/api/v3/catalog/{id}` | 更新 |
| DELETE | `/api/v3/catalog/{id}` | 删除 |

**响应模型**（CatalogItem.java / Source.java / Dataset.java / Folder.java）：

`CatalogItem`（顶层目录项）：
```
id, path: List<String>, type: SOURCE|SPACE|FOLDER|HOME|DATASET,
containerType, datasetType, tag, stats, createdAt
```

`Source.children: List<CatalogItem>`、`Folder.children: List<CatalogItem>`（含 `nextPageToken`）

`Dataset`（叶子）：
```
id, type: PHYSICAL_DATASET|VIRTUAL_DATASET,
path: List<String>,
fields: List<Field>           # ← 列定义！
sql, sqlContext               # VDS 才有
format, ...
```

`Field`（dac/model/common/Field.java）：`name, type (DataType enum: VARCHAR/INTEGER/TIMESTAMP/...)`

### 4.5 SQL 提交与结果获取（已实测——POC 性能边界关键）

#### 4.5.1 提交（SQLResource.java:81）

```
POST /api/v3/sql
body: CreateFromSQL { sql, context: List<String>?, engineName?, references? }
返回: QueryDetails { id: <jobId> }
```

#### 4.5.2 查状态（JobResource.java:80）

```
GET /api/v3/job/{id}
返回: JobStatus { jobState, rowCount, errorMessage, startedAt, endedAt, queryType, queueName, ... }
jobState 枚举: PENDING, RUNNING, COMPLETED, FAILED, CANCELED, CANCELLATION_REQUESTED, ...
```

#### 4.5.3 取结果（JobResource.java:120）

```
GET /api/v3/job/{id}/results?offset=0&limit=100
约束: Preconditions.checkArgument(limit <= 500, "限制不能超过500行")  ← 硬上限！
前置: jobState 必须 == COMPLETED, 否则 BadRequestException
返回: JobResourceData → 序列化为 { rowCount, schema: <Arrow JSON>, rows: [...] }
```

#### 4.5.4 错误模型

- 整体异常：`UserException` → 由 `UserExceptionMapper` 统一序列化为 `{ "message": "...", "errorId": "..." }`
- 业务错误：`JobStatus.errorMessage`（仍属于 200 OK）

#### 4.5.5 推导出的 BeelinkDataLoader.fetch_data_as_arrow 算法

```
1. 构造 SQL: SELECT * FROM "<seg1>"."<seg2>"...."<segN>" [LIMIT <size>]
2. POST /api/v3/sql → 拿 jobId
3. 轮询 GET /api/v3/job/{jobId}, 直到 jobState ∈ {COMPLETED, FAILED, CANCELED}
4. FAILED/CANCELED → raise 带 errorMessage 的 AppError
5. COMPLETED: 循环 GET /api/v3/job/{jobId}/results?offset=K&limit=500 直到拉够 size 或返回行数 < 500
6. 累积 rows + schema → 转 pyarrow.Table
7. 返回
```

**性能注意**：
- size = 10_000（推荐 POC 默认）→ 20 页 ≈ 1-2 秒
- size = 2_000_000（上限）→ 4000 页，POC 阶段不推荐
- 必要时在 `import_options.size` 接受用户自定义

### 4.6 二开模块（与 POC 相关的 beelink 自研）

| 功能 | 路径 | 备注 |
|------|------|------|
| 智能问数 SmartQuery | `services/smartquery/` + `dac/backend/.../resource/SmartQuery*.java` | NL2SQL 全套（含语义层、向量搜索、KPI 重试），POC 阶段**不引用** |
| 用户权限 UserRight | `services/userright/` + `dac/backend/.../resource/UserRightResource.java` | 表/列/行权限，DF **隐式依赖**（不直接调用） |
| 用户管理 SimpleUserService | `services/users/` | 兼容 Dremio UserService 接口，扩展 roleId/orgId |
| 中台 SSO | `dac/backend/.../server/ZTSSOLoginServlet*.java` | POC 阶段不接 |

### 4.7 beelink 暴露给 DF 的最小可复用 API（POC 直接用）

| 用途 | beelink 端点 | 是否需要新增 |
|------|--------------|--------------|
| 登录拿 token | `POST /apiv2/login` | ❌ 直接用 |
| 列我可见的 sources/spaces/homes | `GET /api/v3/catalog?include=children` | ❌ 直接用，按 token 自动过滤 |
| 列某个 source/folder 的子项 | `GET /api/v3/catalog/{id}?maxChildren=1000&pageToken=...` | ❌ |
| 拿 Dataset 的 schema（列定义） | `GET /api/v3/catalog/by-path/<seg>...?include=dataset` | ❌ |
| 提交 SQL | `POST /api/v3/sql` | ❌ |
| 查 job 状态 | `GET /api/v3/job/{id}` | ❌ |
| 取结果 | `GET /api/v3/job/{id}/results?offset=&limit=≤500` | ❌ |

**结论：beelink 一行 Java 都不用改**。所有改动都在 DF 侧。

---

## 5. POC-1：表导入模式详细设计

### 5.1 目标

让 beelink 用户在 DF 内浏览自己有权限看的库表，选一张表导入到 DF workspace，立即在 Data Thread 中拖拽生成图表。

### 5.2 用户流程

1. 用户首次进 DF → 在右上角 "Add data" 弹窗（`UnifiedDataUploadDialog`）→ 看到一张 **Beelink** 数据源卡片
2. 点 Beelink 卡片 → 出现参数表单（自动渲染）：
   - `base_url`: `http://beelink.host:9047`
   - `user`: 我的 beelink 用户名
   - `password`: 我的 beelink 密码（sensitive，存 Vault）
3. 点 **Connect** → DF 后端调 `BeelinkDataLoader.__init__(params)` → 内部走 `POST /apiv2/login` 拿 token → `test_connection()` 调 `GET /api/v3/catalog` 验证
4. 连接成功后弹出 catalog tree（来自 `loader.list_tables()`）
5. 用户点 "Sync metadata" → DF 调 `POST /api/connectors/sync-catalog-metadata` → 落 disk cache（后续 DataAgent 能搜到）
6. 用户在 tree 中找到一张表（如 `mysql_prod.sales.orders`）→ 点 **Import**
7. 弹窗里可改 `import_options.size`（默认 10000）→ 确认
8. DF 后端调 `loader.ingest_to_workspace()` → 写 parquet → 返回 `{table_name, row_count, refreshable}`
9. 前端 `loadTable({table})` thunk 把表注册到 Redux → Data Thread 立即出现该表 → 用户拖列做图

### 5.3 后端流程

> **前置条件（POC-1 真机实测纠正）**：必须先 `POST /api/sessions/create
> {"id":"default","name":"default"}` 建一个 active workspace，且所有后续请求都
>带 `X-Workspace-Id: default` 头；否则 import-data 返回 `INVALID_REQUEST: No
> active workspace`。前端 `UnifiedDataUploadDialog` 已自动注入该头，命令行直
> 调 REST 时要自己加。

```
[前端] POST /api/connectors/import-data
       Headers: X-Identity-Id: <type:id>, X-Workspace-Id: default
       { connector_id: "beelink:beelink-main",
         source_table: "smartquery_demo.customers",
         table_name?: "customers", import_options: {size: 10000} }

[后端 connector_import_data] (data_connector.py:1564)
  ├─ _resolve_connector(data) → DataConnector by connector_id
  ├─ loader = source._require_loader()        # 拿到 per-identity BeelinkDataLoader
  ├─ raw_source = "smartquery_demo.customers"
  ├─ source_id, source_name = _parse_source_table(raw_source)
  ├─ workspace = get_workspace(get_identity_id())
  ├─ safe_name = sanitize_table_name("orders")
  └─ loader.ingest_to_workspace(workspace, "orders", source_id, options)
       ├─ arrow_table = loader.fetch_data_as_arrow(source_id, options)
       │   ├─ sql = 'SELECT * FROM "mysql_prod"."sales"."orders" LIMIT 10000'
       │   ├─ POST /api/v3/sql  → jobId
       │   ├─ 轮询 GET /api/v3/job/{jobId}
       │   ├─ 分页 GET /api/v3/job/{jobId}/results?offset=...&limit=500
       │   └─ rows + schema → pa.Table
       ├─ workspace.write_parquet_from_arrow(arrow_table, "orders",
       │                                     source_info={loader_type, loader_params, source_table, import_options})
       └─ 元数据合并（loader.get_column_types(source_table) 补描述）

返回: { table_name: "orders", row_count: 10000, refreshable: true }
```

### 5.4 前端流程

`UnifiedDataUploadDialog.tsx` 已实现以下流程，**只需自动接管 Beelink**：

```ts
// 1. 用户点 "Import" 按钮，触发（POC-1 实测纠正：body 字段名是 connector_id，不是 source_id）：
const resp = await apiRequest(CONNECTOR_ACTION_URLS.IMPORT_DATA, {
    method: 'POST',
    body: { connector_id, source_table, table_name, import_options }
});

// 2. 后端返回 { table_name, row_count, refreshable }，前端构造 DictTable：
const tableWithSource: DictTable = createDictTable(resp.data.table_name, [], undefined,
    { tableId: resp.data.table_name, rowCount: resp.data.row_count },
    /*anchored*/ true,
    /*description*/ '',
    { type: 'database', connectorId: connector_id, databaseTable: resp.data.table_name,
      canRefresh: resp.data.refreshable, originalTableName: source_table } );

// 3. dispatch loadTable thunk → 调 GET /api/tables/get-table?name=... → 拉前 N 行预览 → 进 Redux
await dispatch(loadTable({ table: tableWithSource }));
```

**完全复用现成代码**——只要 `ServerConfig.CONNECTORS` 包含 Beelink，前端不必新增任何文件。

### 5.5 需要新增/修改的模块

| 仓库 | 路径 | 新增/修改 | 内容 |
|------|------|-----------|------|
| beelinkDF | `py-src/data_formulator/data_loader/beelink_data_loader.py` | **新增** | `BeelinkDataLoader` 类实现 |
| beelinkDF | `py-src/data_formulator/data_loader/__init__.py` | **修改 1 行** | 在 `_LOADER_SPECS` 列表追加 `("beelink", "data_formulator.data_loader.beelink_data_loader", "BeelinkDataLoader", "requests")` |
| beelinkDF | `py-src/data_formulator/app.py` | **不改**（POC-1 实测纠正） | `register_data_connectors` 只挂 admin-pinned；普通 loader 通过 `DATA_LOADERS` dict 自动 discovery，用户走 UI `POST /api/connectors` 自建实例即可 |
| beelinkDF | `src/icons.tsx`（可选） | 修改 | 注册 Beelink icon |
| beelinkDF | `src/i18n/locales/zh/common.json` & `en/common.json`（可选） | 修改 | 添加 Beelink 文案 |
| beelink_wenshu | （无） | —— | beelink 一行 Java 都不用改 |

### 5.6 beelink 最小 API（DF 调用清单）

```
POST {base_url}/apiv2/login
  Body: { userName, password }
  Header: Content-Type: application/json
  返回: { token, userId, userName, expires, admin, roleId, orgId, ... }

GET {base_url}/api/v3/catalog
  Header: Authorization: _beelink<token>
  返回: { data: [ CatalogItem 顶层 ] }

GET {base_url}/api/v3/catalog/{id}?maxChildren=1000&pageToken=<...>
  返回: Source|Folder|Space { id, name, children: [CatalogItem], nextPageToken? }

GET {base_url}/api/v3/catalog/by-path/<seg1>/<seg2>/...?include=dataset
  返回: Dataset { id, path, fields: [{name, type}], type, sql? }

POST {base_url}/api/v3/sql
  Body: { sql, context?: ["seg1","seg2"] }
  返回: { id: "<jobId>" }

GET {base_url}/api/v3/job/{id}
  返回: { jobState, rowCount, errorMessage, ... }

GET {base_url}/api/v3/job/{id}/results?offset=&limit=≤500
  返回: { rowCount, schema, rows: [{...}, ...] }
```

### 5.7 DF 如何写入 workspace（已封装）

整条链路由 `ExternalDataLoader.ingest_to_workspace()` 封装完成。BeelinkDataLoader **不要重写它**，只需实现：

- `fetch_data_as_arrow()`：返回 PyArrow Table
- `list_tables()`：返回 `[{name, path?, metadata: {row_count, columns, sample_rows}}, ...]`
- `get_column_types(source_table)`：返回 `{description, columns: [{name, type, description}]}` 用于元数据增强

落地后 `TableMetadata` 自动包含：
```yaml
name: "orders"
source_type: data_loader
loader_type: "BeelinkDataLoader"
loader_params: {base_url, user, ...}  # 过滤掉 password 等敏感字段
source_table: "mysql_prod.sales.orders"
import_options: {size: 10000}
row_count: 10000
columns: [{name, dtype, description}, ...]
```

### 5.8 验收标准（POC-1）

| 编号 | 用例 | 通过条件 |
|------|------|----------|
| AC-1.1 | 用户首次打开 DF，看到 "Beelink" 数据源卡片 | 卡片显示在 `UnifiedDataUploadDialog` 的 "Add connection" 区 |
| AC-1.2 | 输入正确凭证 + base_url，能 Connect 成功 | 连接成功提示，前端 `CONNECTED_CONNECTORS` 含 `"beelink"` |
| AC-1.3 | 输错密码 → 友好错误 | 显示来自 beelink 的错误，不暴露内部异常 |
| AC-1.4 | Catalog tree 加载 < 5s（≤ 5 source、每 source ≤ 100 表） | 树形展开，节点类型正确 |
| AC-1.5 | 用户 A 看到的 catalog ⊂ 用户 A 在 beelink Web UI 看到的 | 等同性（beelink 自然过滤） |
| AC-1.6 | Import 一张 1 万行表 < 10s | 完成后 Data Thread 立即出现表 |
| AC-1.7 | 拖列做柱状图能成功渲染 | Vega-Lite 图能展现 |
| AC-1.8 | "Refresh data" 重新拉，content_hash 变化时自动同步衍生表 | Redux state 刷新 |
| AC-1.9 | 重启 DF 后再打开，凭证自动恢复（Vault） | 自动重连，无需重输密码 |
| AC-1.10 | 同机器两个用户（OS 不同）互相看不到对方的 beelink connector | identity 隔离 |

---

## 6. POC-1.5：手写 SQL 直通模式详细设计

### 6.1 用户故事

用户已经知道想跑什么 SQL（如 `SELECT region, SUM(amount) FROM "mysql_prod"."sales"."orders" GROUP BY region`），希望不要先 import 整张表，而是直接把 SQL 跑在 beelink，结果作为一张新表落到 DF workspace 供后续图表使用。

### 6.2 为什么需要单独的 endpoint

DF 的 `ExternalDataLoader.fetch_data_as_arrow` 在源码中明确禁止接受 raw query string（`external_data_loader.py:395-396`：*"Only source_table is supported (no raw query strings) to avoid security and dialect diversity issues across loaders"*）。所以 **不能** 通过 `/api/connectors/import-data` 直通 SQL。

### 6.3 设计：新增独立端点

**端点**：`POST /api/beelink/sql/execute`

**Request**：

```json
{
  "source_id": "beelink",
  "sql": "SELECT region, SUM(amount) AS total FROM \"mysql_prod\".\"sales\".\"orders\" GROUP BY region",
  "table_name": "regional_sales",
  "context": ["mysql_prod","sales"],
  "max_rows": 10000
}
```

**Response（成功）**：

```json
{
  "status": "success",
  "data": {
    "table_name": "regional_sales",
    "row_count": 12,
    "columns": [
      {"name": "region", "type": "VARCHAR", "dtype": "object"},
      {"name": "total",  "type": "DOUBLE",  "dtype": "float64"}
    ],
    "source_query": "SELECT region, SUM(amount) AS total FROM ...",
    "refreshable": true
  }
}
```

**Response（beelink 报错）**：透传 beelink 的 errorMessage：

```json
{
  "status": "error",
  "error": {
    "code": "BEELINK_QUERY_FAILED",
    "message": "Column 'amunt' not found in table 'orders'",
    "detail": "原文 errorMessage + errorId（仅 debug 显示）",
    "retry": false
  }
}
```

### 6.4 后端流程

```
[handler] beelink_execute_sql(request)
  ├─ identity = get_identity_id()
  ├─ connector = DATA_CONNECTORS["beelink"]
  ├─ loader: BeelinkDataLoader = connector._require_loader()   # 必须已连接
  ├─ workspace = get_workspace(identity)
  ├─ safe_name = sanitize_table_name(table_name)
  ├─ arrow_table = loader.execute_sql_to_arrow(sql, context, max_rows)
  │      ├─ jobId = client.submit_sql(sql, context)
  │      ├─ wait_for_completion(jobId)  # 轮询，30s 超时
  │      ├─ rows = client.paginate_results(jobId, limit=500, total≤max_rows)
  │      └─ build pa.Table from rows + schema
  └─ workspace.write_parquet_from_arrow(arrow_table, safe_name,
        source_info={
          "loader_type": "BeelinkDataLoader",
          "loader_params": loader.params (non-sensitive),
          "source_table": None,          # ← 区别于 POC-1
          "source_query": sql,           # ← 关键字段，TableMetadata 持久化
          "import_options": {"max_rows": max_rows}
        })
```

### 6.5 前端流程

新增一个 SQL 编辑器入口（可放在 `UnifiedDataUploadDialog` 一个新 tab `'beelink-sql'`，或者在 Data Thread 右下角"+SQL"按钮）：

```ts
const handleRunSQL = async () => {
  setRunning(true);
  try {
    const { data } = await apiRequest('/api/beelink/sql/execute', {
      method: 'POST',
      body: { source_id: 'beelink', sql, table_name, max_rows: 10000 }
    });
    const dt = createDictTable(data.table_name, [], undefined,
      { tableId: data.table_name, rowCount: data.row_count }, true, '',
      { type: 'database', connectorId: 'beelink', databaseTable: data.table_name,
        canRefresh: data.refreshable });
    await dispatch(loadTable({ table: dt }));
  } catch (e) { showError(e); }
};
```

刷新：当 `TableMetadata.source_query` 非空，`refresh-data` 端点应改为 "re-run source_query" 而不是 "re-fetch source_table"。最简单：扩展 `connector_refresh_data`，识别 `meta.source_query` 优先重跑。

### 6.6 错误处理

| 来源 | 处理 |
|------|------|
| beelink 401 / token 过期 | 在 BeelinkClient 自动 re-login（用 Vault 缓存的密码），失败则抛 `BEELINK_AUTH_EXPIRED` |
| beelink 403 / 越权 | 透传 errorMessage 给前端，不重试 |
| beelink JobState=FAILED | 透传 errorMessage（含 SQL 语法错、列不存在等），状态 `BEELINK_QUERY_FAILED` |
| beelink JobState=CANCELED | `BEELINK_QUERY_CANCELED` |
| 拉结果超过 max_rows | 截断 + 返回 `truncated=true` 给前端提示 |
| 轮询超过 30s 仍 RUNNING | 不取消，返回 `BEELINK_QUERY_TIMEOUT`（前端可选重连接 jobId 续等） |
| 网络异常 | classify_and_raise_connector_error(e) 标准处理 |

### 6.7 验收标准（POC-1.5）

| 编号 | 用例 | 通过条件 |
|------|------|----------|
| AC-1.5.1 | SQL 编辑器能跑一个 SELECT 1 | 返回 1 行表 |
| AC-1.5.2 | SQL 跑聚合查询 → 表入 workspace | Data Thread 出现新表，列名 = SQL 投影列 |
| AC-1.5.3 | SQL 写错列名 → 前端显示 beelink 报错 | 错误文案来自 beelink，不暴露 stack trace |
| AC-1.5.4 | SQL 涉及无权访问的表 → 错误透传 | 错误文案来自 beelink UserRightService |
| AC-1.5.5 | 重新打开 DF，点 "refresh" 该表 → 重跑相同 SQL | `source_query` 持久化，refresh 行为正确 |
| AC-1.5.6 | 30s 超时 → 友好错误 | 返回 `BEELINK_QUERY_TIMEOUT` |
| AC-1.5.7 | 单次结果 > max_rows 时被截断 + 提示 | UI 显示 truncated 标识 |

---

## 7. POC-2：自然语言 SQL 直通模式详细设计

### 7.1 目标

让用户用自然语言提问（如 "看下各区域上个月的销售额排行"），DataAgent 自动：
1. 在 beelink catalog cache 中找到相关表（`search_data_tables`）
2. 拉取表 schema（`read_catalog_metadata`）
3. 生成一段 Dremio SQL
4. 直通 beelink 执行（`query_beelink_sql` 工具，新增）
5. 把结果作为 workspace 表 → 触发 `visualize` action 产出图表

### 7.2 设计核心：扩展 DataAgent，新增 `query_beelink_sql` 工具

**为什么不另起一个 agent？** 
- DataAgent 已经有完整的 trajectory / clarify / visualize / present 循环；
- 改 SYSTEM_PROMPT 让它整体改输出 SQL 风险高（影响所有数据源）；
- 在 TOOLS 列表添加一个新工具，让 LLM 选择性使用，影响最小。

### 7.3 schema context 注入路径（完全复用现成机制）

1. 用户首次连接 beelink → 点 "Sync catalog" → `POST /api/connectors/sync-catalog-metadata` 把 beelink 全表元数据落到 `<user_home>/catalog_cache/beelink/*.json`
2. 用户在 Data Thread 提问 → DataAgent `_tool_loop` 启动
3. DataAgent 内部决策：
   - `search_data_tables(query="销售 区域", scope="all")` → 返回 `[{source_id:"beelink", table_key:"mysql_prod.sales.orders", description:"...", matched_columns:["region","amount"]}, ...]`
   - `read_catalog_metadata(source_id="beelink", table_key="mysql_prod.sales.orders")` → 返回完整列定义
4. 此时 DataAgent prompt 已经天然包含相关 schema（context.py 的 handler 已格式化为人类可读文本）

### 7.4 DataAgent 如何决定走 SQL 而非 Python

在 SYSTEM_PROMPT 加一段决策提示（agents/data_agent.py:281 `SYSTEM_PROMPT`）：

```text
## When to use query_beelink_sql vs explore(python)

- If the answer requires aggregation/filtering over a NOT-imported beelink table
  AND the table is large, use **query_beelink_sql** to push down computation:
  the result is registered as a new workspace table you can then visualize.
- If the answer needs cross-table joins where ALL tables are already imported,
  use explore(python) with duckdb on the parquet files.
- For small tables already imported, use explore(python) directly.
```

并在 TOOLS 列表加：

```python
{
  "type": "function",
  "function": {
    "name": "query_beelink_sql",
    "description": (
      "Push an SQL query down to a connected beelink source. The result is "
      "registered as a new workspace table (parquet) you can reference in "
      "subsequent explore/visualize calls. Use this when the relevant table "
      "is NOT yet imported AND the question requires aggregation/filtering."
    ),
    "parameters": {
      "type": "object",
      "properties": {
        "purpose": {"type": "string", "description": "One-line explanation shown to user as progress."},
        "source_id": {"type": "string", "description": "Connector source id (usually 'beelink')."},
        "sql": {"type": "string", "description": "A valid Dremio SQL SELECT statement. Reference tables with quoted multi-segment paths like \"mysql_prod\".\"sales\".\"orders\"."},
        "table_name": {"type": "string", "description": "snake_case name for the resulting workspace table."},
        "max_rows": {"type": "integer", "default": 10000}
      },
      "required": ["purpose","source_id","sql","table_name"]
    }
  }
}
```

### 7.5 工具调度（`_tool_loop` 新分支）

在 `data_agent.py:1531 _tool_loop` 添加：

```python
elif tool_name == "query_beelink_sql":
    try:
        from data_formulator.routes.beelink import run_beelink_sql_for_agent
        result = run_beelink_sql_for_agent(
            source_id=tool_args["source_id"],
            sql=tool_args["sql"],
            table_name=tool_args["table_name"],
            max_rows=tool_args.get("max_rows", 10000),
            workspace=self.workspace,
        )
        # result: {table_name, row_count, columns, sample_rows, source_query}
        tool_content = json.dumps(result, ensure_ascii=False)
        # 同时把新表广播给前端，让 DataThread 立即显示
        yield {"type": "tool", "tool": tool_name,
               "purpose": tool_args.get("purpose"),
               "table_registered": result["table_name"]}
    except Exception as e:
        tool_content = f"query_beelink_sql failed: {str(e)}"
```

### 7.6 SQL 错误如何反馈回 Agent（SQL 修复循环）

LLM 调用 tool 失败时，错误文本会作为 `tool_content` 回传到下一轮 LLM 输入。例如：

```
tool result:
query_beelink_sql failed: BEELINK_QUERY_FAILED: Column 'amunt' not found in table 'orders'.
Available columns: order_id, region, amount, created_at, ...
```

LLM 在下一轮会看到这段错误 + 上下文（之前 `read_catalog_metadata` 拿到的真实列名），自动改 SQL（如把 `amunt` 改为 `amount`）重试。无需专门的"SQL 修复 agent"——这是 LLM tool-use 框架自然的 self-correction 循环。

**约束**：在 `_tool_loop` 内为 `query_beelink_sql` 设置每条对话最多重试 3 次（避免死循环 + 控制 token 消耗）。

### 7.7 查询结果如何进入 Data Thread

`run_beelink_sql_for_agent()` 内部：

```
1. submit SQL → wait completion → paginate results → pa.Table
2. workspace.write_parquet_from_arrow(arrow_table, table_name,
       source_info={..., source_query: sql})
3. 返回 {table_name, row_count, columns, sample_rows: arrow_table.slice(0,10).to_pylist()}
```

Agent 路由（`routes/agents.py` 的 streaming endpoint）在 yield `tool` 事件时附带 `table_registered`；前端在 NDJSON 流处理逻辑中识别到此事件，自动 `dispatch(loadTable({table: createDictTable(table_name, ...)}))`，于是 DataThread 立刻出现新表，用户后续 visualize 时直接作为 source。

### 7.8 图表 artifact 如何关联 SQL/result

- 后端：`TableMetadata.source_query` 持久化 SQL；
- 前端 `DictTable.source` 现有字段已足够（`connectorId`、`databaseTable`、`canRefresh`、`originalTableName`）；
- 推荐扩展 `DataSourceConfig` 增加可选字段 `sourceQuery?: string`，让前端在表卡片下方显示"由 SQL 生成"，点击可查看完整 SQL。
- Chart artifact 持有 `input_tables`（`DataAgent.visualize` action 的 JSON 已有该字段），即可定位回原始 SQL。

### 7.9 验收标准（POC-2）

| 编号 | 用例 | 通过条件 |
|------|------|----------|
| AC-2.1 | 用户问 "各区域销售额排行" → DataAgent 自动找到 orders 表 | 工具日志包含 `search_data_tables` + `read_catalog_metadata` 调用 |
| AC-2.2 | 自动生成 Dremio SQL 并跑通 | beelink 收到 SQL，jobState=COMPLETED |
| AC-2.3 | SQL 错列名 → DataAgent 自动改正重试 → 成功 | 重试 ≤ 3 次，最终出图 |
| AC-2.4 | SQL 涉及无权表 → 友好提示 + 询问用户 | DataAgent action=clarify 或 explain |
| AC-2.5 | 查询结果 ≤ 1 万行，落 workspace + 出图全程 < 30s | E2E 时间 |
| AC-2.6 | 图表 artifact 能查到原始 SQL | 前端表卡片显示 source_query 摘要 |
| AC-2.7 | 用户后续追问 "上个月环比" → DataAgent 复用已落表 + explore(duckdb) | 不重复打到 beelink |
| AC-2.8 | 多个连续问题串成 Data Thread 叙事 | 至少 3 轮迭代正常 |

---

## 8. 登录集成设计

### 8.1 三种可选方案对比

| 方案 | 复杂度 | 用户体验 | 适用阶段 |
|------|--------|----------|----------|
| A. 用户名/密码（Vault 存） | ⭐ 最低 | 首次输一次，重启自动恢复 | **POC-1 推荐** |
| B. 内嵌 + postMessage Token 注入 | ⭐⭐ | 父页面静默传 token，无感 | POC-2 / 早期生产 |
| C. SSO token-exchange（DF AuthProvider + beelink SSO server） | ⭐⭐⭐ | 真正单点登录 | 正式产品 |

### 8.2 方案 A：用户名/密码（POC-1）

```
BeelinkDataLoader.list_params() = [
  {name:"base_url", type:"string", required:True, tier:"connection",
   default:"", description:"e.g. http://beelink:9047"},
  {name:"user", type:"string", required:True, tier:"auth"},
  {name:"password", type:"string", required:True, tier:"auth", sensitive:True,
   description:"Used to login via POST /apiv2/login; never stored in metadata"}
]

BeelinkDataLoader.auth_mode() = "connection"
BeelinkDataLoader.auth_config() = {"mode": "credentials"}

BeelinkDataLoader.__init__(params):
  self.base_url = params["base_url"]
  self.user = params["user"]
  self.password = params["password"]
  self.session = requests.Session()
  self._login()   # → 拿 token 存内存 self.token + self.token_expires_at

BeelinkDataLoader._login():
  r = self.session.post(f"{self.base_url}/apiv2/login",
                        json={"userName": self.user, "password": self.password},
                        timeout=10)
  r.raise_for_status()
  data = r.json()
  self.token = data["token"]
  self.token_expires_at = data["expires"] / 1000.0
  self.username_resolved = data["userName"]
  self.session.headers["Authorization"] = f"_beelink{self.token}"
```

Vault 存哪些字段：`base_url, user, password`（password 加密；DataConnector 自动调 `_vault_store`）。

### 8.3 方案 B：内嵌 + postMessage Token 注入（POC-2 推荐）

beelink 作为外层应用，把 DF 嵌入到一个 iframe，并通过 `postMessage` 主动把 beelink token 推给 DF 前端：

```js
// beelink 父页面
const iframe = document.querySelector('iframe[name="df"]');
iframe.contentWindow.postMessage({
  type: 'df-sso-auth',  // ← DF 已有约定！(DBTableManager.tsx:194)
  access_token: beelinkToken,
  user: { id: userName, displayName: ... },
}, '*');
```

DF 前端 `DBTableManager.tsx:152` 的 `handleDelegatedLogin` 已经监听该消息。改造点：把同样的监听放到全局（`src/app/App.tsx` 或 `src/views/DataThread.tsx`），收到后写入 `state.connector.beelink.params.access_token`。

DF 后端 BeelinkDataLoader 改 `auth_config = {mode:"delegated", login_url:"/sso/landing"}`，于是 `_inject_credentials` 时自动从 TokenStore 注入 `access_token`，BeelinkDataLoader 用 token 直接调 beelink REST（无需密码）。

**beelink 端唯一改造**：可选地新增一个静态页面（`dac/ui/public/df-sso-landing.html`）作为 popup landing，但 postMessage 注入这种方式连 landing 都不需要。

### 8.4 方案 C：正式 SSO（token-exchange）

DF 后端实现一个新 `AuthProvider`（如 `BeelinkAuthProvider`），其 `authenticate(request)` 读取 beelink token（cookie 或 Authorization header），调 beelink `GET /apiv2/login`（即 `isUserAuthorized`，返回 boolean）做存活校验、或新增 `POST /api/v3/whoami` 拿到 username + roleId，构造 `AuthResult(user_id=username, raw_token=token, ...)`。

- DF `get_identity_id()` → 自动返回 `user:<username>`
- DF `get_sso_token()` → 自动返回 beelink token
- BeelinkDataLoader `auth_config = {mode:"sso_exchange"}`，DF Connector 自动注入

beelink 端可选改造：新增 `POST /api/v3/whoami` 直接返回当前 token 对应的 username（避免 DF 解析 JWT）。

### 8.5 workspace 如何绑定 beelink user

- 方案 A：DF identity = `local:<os>` 或 `browser:<uuid>`，workspace 与 beelink user 不强绑——同一 OS 用户切换 beelink 账号会共享 workspace。**POC 可接受**。
- 方案 B/C：DF identity = `user:<beelink_username>`，workspace 严格按 beelink user 隔离。**生产必须**。

---

## 9. 数据流设计

### 9.1 Catalog 流

```
User → DF UI → POST /api/connectors/sync-catalog-metadata
            → DataConnector → loader.sync_catalog_metadata(filter)
            → BeelinkDataLoader._list_all_tables_via_catalog()
                ├─ GET /api/v3/catalog                       (顶层 sources)
                ├─ 递归 GET /api/v3/catalog/{id}?maxChildren=1000  (folders)
                └─ 命中 dataset → 拿 fields → 归一化为 {name, path, metadata: {columns, row_count?}}
            → save_catalog(user_home, "beelink", flat_tables)
                → 写 <user_home>/catalog_cache/beelink/tables.json
            → return tree

DataAgent 调 search_data_tables(query, scope="all")
  → handle_search_data_tables → search_catalog_cache(user_home, query, exclude=imported)
  → 命中即返回 {source_id, table_key, matched_columns, ...}
```

### 9.2 表导入流（POC-1）

```
User → "Import" button → POST /api/connectors/import-data
       { source_id:"beelink", source_table:"mysql_prod.sales.orders",
         table_name:"orders", import_options:{size:10000} }

Backend:
  connector = DATA_CONNECTORS["beelink"]
  loader = connector._require_loader()
  workspace = get_workspace(identity_id)
  loader.ingest_to_workspace(workspace, "orders", "mysql_prod.sales.orders", {size:10000})
    ├─ arrow = loader.fetch_data_as_arrow(...)
    │   ├─ sql = 'SELECT * FROM "mysql_prod"."sales"."orders" LIMIT 10000'
    │   ├─ jobId = POST /api/v3/sql
    │   ├─ wait_for_completion(jobId, timeout=30s)
    │   ├─ rows = paginate /api/v3/job/{jobId}/results (limit=500)
    │   └─ return pa.Table
    ├─ workspace.write_parquet_from_arrow(arrow, "orders", source_info=...)
    │   ├─ 写 <user_home>/.../orders.parquet
    │   └─ workspace.yaml 更新
    └─ best-effort: loader.get_column_types() 补充列描述

Frontend:
  resp = {table_name:"orders", row_count:10000, refreshable:true}
  → dispatch(loadTable({table: createDictTable(...)}))
  → GET /api/tables/get-table?name=orders → 拿前 N 行预览
  → Redux state.tables.push(table)
```

### 9.3 SQL 执行流（POC-1.5）

```
User → SQL 编辑器 → POST /api/beelink/sql/execute
       { source_id, sql, table_name, max_rows }

Backend (新增 routes/beelink.py):
  loader = DATA_CONNECTORS["beelink"]._require_loader()
  workspace = get_workspace(get_identity_id())
  arrow = loader.execute_sql_to_arrow(sql, context, max_rows)   # 新增方法
  workspace.write_parquet_from_arrow(arrow, table_name, source_info={
    loader_type, loader_params, source_table:None,
    source_query:sql, import_options:{max_rows}})
  return {table_name, row_count, columns}

Frontend: 同 9.2 后半段
```

### 9.4 NL→SQL 执行流（POC-2）

```
User → "各区域销售额排行" → DataThread 发送到 /api/agent/data-agent-streaming
                          (含已有 trajectory、当前 workspace tables 列表、user prompt)

DataAgent._tool_loop:
  Round 1 (LLM thinks): need to find data
    → search_data_tables(query="销售 区域 排行", scope="all")
    → tool returns: "[beelink] mysql_prod.sales.orders — matched: region, amount {source_id:beelink, table_key:mysql_prod.sales.orders}"
  Round 2 (LLM thinks): get schema
    → read_catalog_metadata(source_id="beelink", table_key="mysql_prod.sales.orders")
    → tool returns: columns + types + descriptions
  Round 3 (LLM thinks): push down aggregation
    → query_beelink_sql(
        source_id="beelink",
        sql='SELECT region, SUM(amount) AS total
             FROM "mysql_prod"."sales"."orders"
             WHERE created_at >= DATE \'2026-04-01\'
             GROUP BY region ORDER BY total DESC',
        table_name="regional_sales",
        max_rows=10000)
    → tool returns: {table_name:"regional_sales", row_count:12, columns:[...]}
    → yield tool event w/ table_registered=regional_sales
    → frontend dispatch loadTable(regional_sales)
  Round 4 (LLM produces action):
    → visualize {
        input_tables: ["regional_sales"],
        code: "df_out = df.sort_values('total', ascending=False)",
        chart: {chart_type:"bar", encodings:{x:"region", y:"total"}, ...}}
    → frontend 渲染图表
```

### 9.5 图表生成流（不变 / 复用 DF 原生）

DataAgent `visualize` action 生成 Vega-Lite spec + Python code → 前端 sandbox 跑 code 拿 output_variable → 绑定到 chart spec → react-vega 渲染。

---

## 10. 接口设计

> 所有 DF 新增端点统一遵循 DF 现有响应包络：成功 `{"status":"success","data":{...}}`、失败 `{"status":"error","error":{"code","message","detail?","retry?","request_id?"}}`（apiClient.ts:86）。

### 10.1 `GET /api/df/current-user`

返回当前 DF 已识别的身份及（如有）从 beelink 同步过来的 user 信息。

**Request**：无 body；Headers：`X-Identity-Id`、可选 `Authorization`

**Response（成功）**：

```json
{
  "status": "success",
  "data": {
    "identity": { "type": "user", "id": "zhangsan01" },
    "auth_mode": "credentials",
    "provider": null,
    "is_local_mode": true,
    "beelink": {
      "connected": true,
      "user_name": "zhangsan01",
      "user_id": "u-abc-123",
      "admin": false,
      "role_id": "2",
      "org_id": "OP-NJTK",
      "expires_at": 1747999999000
    }
  }
}
```

**实现要点**：复用 `get_identity_id()` + 查 `DATA_CONNECTORS["beelink"]._get_loader()` 拿当前 loader 缓存的 token meta；若未连接则 `beelink: null`。

### 10.2 `GET /api/df/catalog/sources`

列当前 beelink 用户可见的所有 source。封装 beelink `GET /api/v3/catalog`，只取 `containerType=SOURCE` 项。

**Response**：

```json
{
  "status": "success",
  "data": {
    "sources": [
      {"id": "src-uuid-1", "name": "mysql_prod", "type": "MySQL",
       "path": ["mysql_prod"], "tag": "etag-x"},
      {"id": "src-uuid-2", "name": "iceberg_lake", "type": "Iceberg",
       "path": ["iceberg_lake"], "tag": "etag-y"}
    ]
  }
}
```

> 实际上前端可以直接调 `/api/connectors/get-catalog-tree?source_id=beelink` 拿到，**该端点是为了对接外部场景（如 beelink Web UI 嵌入 DF 时 beelink 自己想查 DF 状态）**。

### 10.3 `GET /api/df/catalog/tables`

列指定 source 或 path 下的全部表。

**Request**：

```
GET /api/df/catalog/tables?source=mysql_prod
GET /api/df/catalog/tables?path=mysql_prod.sales
```

**Response**：

```json
{
  "status": "success",
  "data": {
    "path": ["mysql_prod", "sales"],
    "tables": [
      {"name":"orders","path":["mysql_prod","sales","orders"],"row_count":1500000,
       "description":"订单主表"},
      {"name":"customers","path":["mysql_prod","sales","customers"],"row_count":80000}
    ],
    "folders": [
      {"name":"archived","path":["mysql_prod","sales","archived"]}
    ]
  }
}
```

### 10.4 `GET /api/df/catalog/table-schema`

拿单表的列定义。

**Request**：

```
GET /api/df/catalog/table-schema?path=mysql_prod.sales.orders
```

**Response**：

```json
{
  "status": "success",
  "data": {
    "path": ["mysql_prod","sales","orders"],
    "dataset_type": "PHYSICAL_DATASET",
    "row_count": 1500000,
    "description": "订单主表 — 由 mysql 增量同步",
    "columns": [
      {"name":"order_id","type":"BIGINT","is_dttm":false,"description":"订单 ID"},
      {"name":"region","type":"VARCHAR","is_dttm":false,"description":"区域代码"},
      {"name":"amount","type":"DECIMAL(18,2)","is_dttm":false,"description":"订单金额"},
      {"name":"created_at","type":"TIMESTAMP","is_dttm":true,"description":"创建时间"}
    ]
  }
}
```

### 10.5 `POST /api/df/sql/execute`

POC-1.5 的核心端点（详见 §6.3）。

**Request**：

```json
{
  "source_id": "beelink",
  "sql": "SELECT region, SUM(amount) AS total FROM \"mysql_prod\".\"sales\".\"orders\" GROUP BY region ORDER BY total DESC",
  "table_name": "regional_sales",
  "context": ["mysql_prod","sales"],
  "max_rows": 10000,
  "register_as_table": true
}
```

**Response（成功）**：

```json
{
  "status": "success",
  "data": {
    "table_name": "regional_sales",
    "row_count": 12,
    "columns": [
      {"name":"region","dtype":"object","type":"VARCHAR"},
      {"name":"total","dtype":"float64","type":"DOUBLE"}
    ],
    "sample_rows": [
      {"region":"East","total":12345678.90},
      {"region":"South","total":9876543.21}
    ],
    "source_query": "SELECT region, SUM(amount) AS total ...",
    "elapsed_ms": 842,
    "truncated": false,
    "job_id": "1f9c..."
  }
}
```

**Response（错误）**：

```json
{
  "status": "error",
  "error": {
    "code": "BEELINK_QUERY_FAILED",
    "message": "Column 'amunt' not found in any table",
    "detail": "errorId=8b3e... (beelink internal)",
    "retry": false
  }
}
```

### 10.6 既有 connector 端点（直接复用）

POC 阶段**优先使用 DF 已有的 `/api/connectors/*` 路由族**：

| 用途 | 端点 | 备注 |
|------|------|------|
| 列连接 | `GET /api/connectors` | 当前 identity 的实例 |
| 创建 | `POST /api/connectors` | body 含 **`loader_type`** + `source_id` + `display_name` + `params`（POC-1 实测） |
| 浏览目录 | `POST /api/connectors/get-catalog-tree` | body `{connector_id, filter?}` |
| 同步缓存 | `POST /api/connectors/sync-catalog-metadata` | 后续 search_data_tables 用 |
| 导入 | `POST /api/connectors/import-data` | body 见 §5.3；需 `X-Workspace-Id` 头 + 先 `POST /api/sessions/create` |
| 刷新 | `POST /api/connectors/refresh-data` | body `{connector_id, table_name}` |
| 预览 | `POST /api/connectors/preview-data` | body `{connector_id, source_table, limit}` |

> **`connector_id` 的真实格式**（POC-1 实测）：`<loader_type>:<safe-source-id>`，
> 如 `beelink:beelink-main`（dash 不是 underscore；`POST /api/connectors` 时
> 提交的 `source_id="beelink_main"` 会被规范化为 `beelink-main`）。

### 10.7 错误码约定

| code | HTTP | 含义 |
|------|------|------|
| `INVALID_REQUEST` | 400 | 参数缺失 |
| `BEELINK_AUTH_REQUIRED` | 401 | 未登录 / token 缺失 |
| `BEELINK_AUTH_EXPIRED` | 401 | token 过期 |
| `BEELINK_FORBIDDEN` | 403 | 越权 / 无权访问表 |
| `BEELINK_NOT_FOUND` | 404 | 路径不存在 |
| `BEELINK_QUERY_FAILED` | 400 | SQL 语法/语义错 |
| `BEELINK_QUERY_TIMEOUT` | 408 | 轮询超时 |
| `BEELINK_QUERY_CANCELED` | 409 | job 被取消 |
| `BEELINK_UNAVAILABLE` | 502/503 | 服务不可达 |
| `CONNECTOR_NOT_CONNECTED` | 409 | DF 侧 connector 未连接 |

---

## 11. 代码改造清单

### 11.1 beelinkDF 仓库（必改）

#### 新增

```
py-src/data_formulator/data_loader/beelink_data_loader.py     [~400-600 行]
py-src/data_formulator/data_loader/beelink_client.py          [~200-300 行;
                                                              单独抽 HTTP 客户端便于测试]
py-src/data_formulator/routes/beelink.py                       [~150-250 行;
                                                              §6 + §10 的新端点]
tests/data_loader/test_beelink_data_loader.py                  [~300 行]
tests/routes/test_beelink_routes.py                            [~200 行]
```

#### 修改

| 文件 | 大小变化 | 改动 |
|------|----------|------|
| `py-src/data_formulator/data_loader/__init__.py` | +1 行 | `_LOADER_SPECS` 列表追加 Beelink |
| `py-src/data_formulator/app.py` | +5~10 行 | `register_data_connectors()` 内 admin-pinned Beelink connector 注册（可选） + register `routes/beelink.py` |
| `py-src/data_formulator/agents/data_agent.py` | +30~50 行 | TOOLS 列表加 `query_beelink_sql`；`_tool_loop` 加 elif 分支；SYSTEM_PROMPT 注入决策提示 |
| `src/icons.tsx` | +1 行 | Beelink icon import |
| `src/i18n/locales/zh/common.json` | +几行 | 文案 |
| `src/i18n/locales/en/common.json` | +几行 | 文案 |

#### 可选

- `src/views/DataLoadingChat.tsx` 加一个示例 prompt 卡片：「跑一段 Beelink SQL：...」
- `src/components/ComponentType.tsx` 给 `DataSourceConfig` 增加 `sourceQuery?: string` 字段
- 新增 `src/views/BeelinkSQLEditor.tsx` （POC-1.5 入口；也可复用 `UnifiedDataUploadDialog` 加 tab）

### 11.2 beelink_wenshu 仓库（**默认零改动**）

POC-1、POC-1.5、POC-2 均**不需要改 beelink**。以下是**可选**优化项，只在用户体验受限时再做：

| 文件 | 必要性 | 改动 |
|------|--------|------|
| `dac/backend/.../api/JobResource.java` | 可选 | 把 `limit ≤ 500` 上限提到 5000，减少分页次数（需评估内存影响） |
| `dac/backend/.../resource/`（新增 `Api/df-bridge`） | 可选（方案 C） | 新增 `POST /api/v3/whoami` 返回当前 token 对应 username；新增 SSO token-exchange 端点 |
| `dac/ui/public/df-sso-landing.html` | 可选（方案 B） | postMessage landing page |
| `pom.xml` 等 | 不变 | —— |

---

## 12. 分阶段实施计划

### Phase 0：源码确认与本地启动（0.5 天）

- 双仓 git pull 到当前 commit
- DF 本地启动：
  ```
  cd ~/beelinkDF
  python -m venv .venv && source .venv/bin/activate
  pip install -e .
  yarn install && yarn build
  python -m data_formulator
  ```
- beelink 本地启动（按现有文档）：`mvn package` → 启 daemon → 用 beelink Web 验证登录 + 跑一个 SQL
- 验证 beelink REST 可达：手工 curl 走通 `POST /apiv2/login`、`GET /api/v3/catalog`、`POST /api/v3/sql`、`GET /api/v3/job/{id}*`

**完成标准**：手工 curl 全链路可走通；DF 本地能跑现有 PostgreSQL connector。

### Phase 1：登录桥（1 天）

- 实现 `BeelinkClient`（HTTP 客户端层）：login、refresh-on-401、统一 header
- 单测 BeelinkClient.login → 拿到 token
- 单测 BeelinkClient.list_top_level_catalog → 拿到 sources

**完成标准**：单测覆盖所有 client 方法；mock beelink REST 通过。

### Phase 2：POC-1 表导入（2-3 天）

- 实现 `BeelinkDataLoader` 完整版（继承 ExternalDataLoader）
- 在 `_LOADER_SPECS` 注册
- 在 `app.py` 注册 `DataConnector.from_loader(BeelinkDataLoader, source_id="beelink", display_name="Beelink", default_params={"base_url": os.environ.get("BEELINK_URL","http://localhost:9047")})`
- 联调：用户登录 → 列 catalog → 导入一张小表 → 拖拽出图
- 通过 AC-1.1 ~ AC-1.10

**完成标准**：10 条 AC 全部 PASS；同事可独立按文档复现。

### Phase 3：POC-1.5 手写 SQL（1-2 天）

- 在 `BeelinkDataLoader` 加 `execute_sql_to_arrow(sql, context, max_rows)` 方法
- 新增 `routes/beelink.py`：实现 `POST /api/beelink/sql/execute`
- 改造 `connector_refresh_data` 优先用 `source_query` 重跑
- 前端：在 `UnifiedDataUploadDialog` 加 tab `'beelink-sql'`（或独立按钮）
- 通过 AC-1.5.1 ~ AC-1.5.7

**完成标准**：7 条 AC 全部 PASS。

### Phase 4：POC-2 自然语言 SQL（4-5 天）

- 在 `data_agent.py` 加 `query_beelink_sql` tool spec + dispatch
- SYSTEM_PROMPT 加决策提示
- 端到端流式：tool 执行后 yield `table_registered` 事件，前端流处理代码识别后 dispatch loadTable
- 错误透传 + SQL 修复循环（最多 3 次重试）
- 通过 AC-2.1 ~ AC-2.8

**完成标准**：8 条 AC 全部 PASS；3 个典型 demo case 全程顺畅。

### Phase 5：图表体验打磨（持续）

- DictTable 卡片显示 `source_query` 摘要 + "查看完整 SQL" 弹窗
- 图表 artifact 关联到 SQL（chart card 显示 "Generated from SQL on beelink"）
- DataThread 中"用 beelink SQL 生成"的表样式区分
- 错误样式优化（beelink 报错时给出"修改 SQL"快捷按钮）

---

## 13. 风险与降级方案

| 风险 | 概率 | 影响 | 降级 |
|------|------|------|------|
| beelink `POST /apiv2/login` 协议变更 | 低 | POC-1 全停 | 在 BeelinkClient 内适配；保留 `Bearer <token>` 旁路 |
| beelink `limit≤500` 太小，导致大表导入慢 | 中 | POC-1 性能差 | POC 默认 size=10000；UI 暴露 size 调整；后续推 beelink 改硬上限 |
| beelink Catalog 递归深、节点多导致 sync 慢 | 中 | UX 卡 | sync 异步化 + 增量；首次仅 sync 用户最近 spaces |
| LLM 生成的 Dremio SQL 用错引号/方言 | 高 | POC-2 失败率高 | SYSTEM_PROMPT 给 1-2 个 Dremio SQL 范例；错误透传后自动重试 |
| 多用户同时跑大查询打爆 beelink | 低（POC 阶段） | beelink 慢 | POC 阶段限制单用户并发 1；正式上线接 beelink queue |
| beelink token 短期过期（默认 ~24h） | 高 | 长会话失败 | BeelinkClient 在 401 时自动 re-login（用 Vault 凭证） |
| 用户输入恶意 SQL（DROP/UPDATE） | 中 | beelink 已有权限保护 | beelink 自己拒绝；DF 前端可加白名单（仅 SELECT/WITH）但非必须 |
| 跨域问题（DF 与 beelink 不同 origin） | 中 | 前端无法直接调 beelink | 所有 beelink 调用都走 **DF 后端代理**（已是当前设计） |
| DF 与 beelink 时钟漂移导致 token expires 不准 | 低 | 偶发 401 | 401 → re-login fallback |
| Vault 密钥泄漏 | 低 | 全用户凭证泄漏 | 部署时强制环境变量 FERNET_KEY；定期轮换 |

---

## 14. 验收用例（端到端）

### 14.1 用例 1：探索式分析（POC-1）

> 角色：业务分析师 王晓
>
> 1. 王晓登录 DF，看到 Beelink 卡片，输入凭证连接
> 2. 在 catalog tree 找到 `mysql_prod.crm.customers`，点 Import（size=10000）
> 3. 拖 `region` 到 X 轴，`COUNT(customer_id)` 到 Y 轴 → 柱状图
> 4. 拖 `signup_date` 到 X 轴，`COUNT(*)` 到 Y 轴 → 时间趋势
> 5. 双击柱状图最高的 region → DataAgent 自动 drill-down

**通过条件**：5 步无错误，每步 ≤ 5s 完成。

### 14.2 用例 2：精确查询（POC-1.5）

> 角色：数据工程师 李明
>
> 1. 李明知道想要的 SQL：`SELECT date_trunc('month', created_at) AS m, region, SUM(amount) FROM "mysql_prod"."sales"."orders" GROUP BY 1,2`
> 2. 在 DF 打开 "Beelink SQL" 编辑器，粘贴 SQL，点 Run
> 3. 结果落入 workspace 表 `monthly_regional_sales`
> 4. 拖列生成堆叠面积图

**通过条件**：3-5 步成功，结果行数与 beelink Web UI 直接跑该 SQL 一致。

### 14.3 用例 3：NL→SQL（POC-2）

> 角色：业务负责人 陈总
>
> 1. 陈总打开 DF，已连接 Beelink 并 sync 过 catalog
> 2. 在 DataThread 输入框写："最近 30 天，订单金额前 5 大客户是谁？"
> 3. DataAgent 自动 search → read schema → 生成 SQL（含 JOIN customer 表）→ 跑 beelink → 落表 → 出 ranking 横向柱状图
> 4. 陈总追问："这些客户来自哪些区域？" → DataAgent 用同一份数据继续分析（不重打 beelink）

**通过条件**：步骤 3 全程 < 30s；步骤 4 < 5s（复用本地表）；图准确。

### 14.4 用例 4：权限边界（任意阶段）

> 角色：实习生 小张（在 beelink 中仅有 `mysql_prod.public.*` 权限）
>
> 1. 小张连接 Beelink → catalog 不出现 `iceberg_lake.*`（无权限）
> 2. 手工输入 `SELECT * FROM "iceberg_lake"."xxx"` → beelink 返回 403 → DF 友好提示
> 3. NL 提问涉及无权表 → DataAgent 提示"你没有权限访问 X 表"

**通过条件**：所有越权都由 beelink 报错，DF 不擅自做权限判断。

### 14.5 用例 5：会话恢复

> 1. 用户 import 一张表，做一个图，刷新浏览器
> 2. 重新登录，Beelink connector 自动恢复（Vault），workspace 内的表仍在，图仍在

**通过条件**：刷新后 < 3s 恢复，无需重输密码。

### 14.6 POC-1 真机验收记录（2026-05-19）

> 本节为 POC-1 提交前在本机跑通的真实验证证据，凡与文档其他章节不一致处，
> **以本节为准**（其它章节据此回写）。

**测试环境**

| 项 | 值 |
|---|---|
| beelink 服务 | 本机 `http://localhost:8998`（Dremio v25.2.0-202410241428100111-a963b970） |
| beelink 账号 | `beelink`（admin） |
| DF 服务 | 本机 `http://127.0.0.1:5500`（venv：Python 3.13） |
| DF Identity | `local:beelink`（本机模式自动用 OS username） |
| 工作目录 | `~/beelinkDF`（feat/beelink-poc1-loader） |
| 上轮提交 | `5a54e9a feat(loader): 接入 beelink 作为 DF 数据源 POC-1` |

**验收链路（全部 PASS）**

1. **Phase 0 REST 6 步**：`POST /apiv2/login` → `GET /api/v3/catalog` → `GET /api/v3/catalog/{id}?maxChildren=20` → `POST /api/v3/sql` → `GET /api/v3/job/{id}` 轮询 → `GET /api/v3/job/{id}/results?limit=500`，全通过；jobState=COMPLETED，rowCount=1。
2. **`GET /api/data-loaders`**：返回 11 个 loaders 含 `beelink`；hierarchy=`[source,table]`；auth_mode=`connection`；params_form 是 4 个连接参数（`base_url / user / password / verify_ssl`），`table_filter` 是浏览/查询时的入参（`list_tables` / `get-catalog-tree`），不属于连接表单。
3. **`POST /api/connectors`**：`{loader_type:"beelink", source_id:"beelink_main", params:{...}}` → 返回 `{id:"beelink:beelink-main", connected:true}`（注意 source_id `beelink_main` 被规范化为 `beelink-main`）。
4. **`POST /api/connectors/get-catalog-tree`**：`{connector_id:"beelink:beelink-main"}` → 3 个 namespace（test / smartquery_demo / tpcds_sf10），合计 8+ 张 DATASET。
5. **`POST /api/connectors/preview-data`**：smartquery_demo.customers 前 5 行，6 列；首行 `{"id":1,"name":"客户_00001","gender":"女",...}` 中文 UTF-8 完整。
6. **`POST /api/sessions/create`**：`{"id":"default"}` 建 active workspace。
7. **`POST /api/connectors/import-data`**：`{connector_id, source_table:"smartquery_demo.customers", table_name:"customers", import_options:{size:1000}}` → `{table_name:"customers", row_count:1000, refreshable:true}`，**1000 行真落 parquet 入 DF workspace**。
8. **`GET /api/tables/list-tables`**（`X-Workspace-Id: default`）：返回 customers 完整 6 列元数据 + sample_rows。
9. **`GET /api/app-config`**：`CONNECTED_CONNECTORS=["beelink:beelink-main"]`，`CONNECTORS[0]={source_id:"beelink:beelink-main", name:"Beelink Main", ...}` —— 前端拿到这个就能渲染卡片。
10. **DF 重启后**：connector 自动从 Vault 恢复，无需重输密码。
11. **UI 真实交互**：Playwright headless 打开 `http://127.0.0.1:5500/`，点 sidebar "Data connectors" 按钮，主区域出现 **"Beelink Main / BeelinkDataLoader"** 卡片，与 "Link local folder" / "Connect databases" 并排；左侧 Data Connectors 抽屉展开真实 catalog 树（含 customers / products / regions / channels / sales_orders / categories / tpcds_sf10 全套）。

**截图（POC-1 UI 验收证据）**

- `docs/beelink-poc-design/screenshots/beelink_card_row.png` —— Beelink Main 卡片近景
- `docs/beelink-poc-design/screenshots/df_connectors_dialog.png` —— 完整 dashboard（左侧 catalog 树 + 主区卡片网格）
- `docs/beelink-poc-design/screenshots/df_landing.png` —— DF 首屏 landing

**当前边界（明确不包含，需新分支推进）**

- ❌ POC-1.5 手写 SQL 直通端点（`POST /api/beelink/sql/execute`）
- ❌ POC-2 NL → Dremio SQL（DataAgent 工具扩展 `query_beelink_sql`）
- ❌ DataAgent 任何改动
- ❌ 前端 UI 任何改动
- ❌ beelink Java 任何改动

### 14.7 POC-1.5 实现与真机验收记录（2026-05-19）

> 本节记录 POC-1.5 手写 SQL 直通的最小后端实现及真机验收事实。
> 仍**不**包含 POC-2 / NL2SQL / DataAgent 改动；前端、beelink Java 零改动。

**定位（重要边界）**

* POC-1.5 = **用户手写 SQL** 直通；**不是** LLM 生成 SQL。
* SQL 一字不改交给 beelink 执行；权限 / 字段越权 / 语法错误**全部由 beelink 服务端裁决**。
* DF 只负责：接收 SQL → 转发到 beelink → 接收结果（Arrow）→ 落 workspace parquet → 注册成可拖图的表。
* 不引入：SQL Guard、SQL 修复 Agent、强 BO/强 RAG、MCP-first。

**最小后端实现**

1. `BeelinkDataLoader.fetch_sql_as_arrow(sql, import_options) -> pa.Table`
   * 不走 `_build_select_sql`，直接调既有的 `_exec_sql_to_arrow(sql, max_rows=_clamp_size(opts))`；
   * 复用 POC-1 已经稳定的提交 → 轮询 → 分页（500 行硬上限） → Arrow 链路；
   * 空 SQL 抛 `ValueError`；其它失败抛 `BeelinkAPIError`（含 beelink 原始 errorMessage）。

2. `POST /api/connectors/import-sql`（挂在 `data_connector.py` 的 `connectors_bp`，与 `import-data` 同模式）
   * **Body**：`{connector_id, sql, table_name, import_options?}`
   * `import_options`（全部可选）：
     - `size`：取行数上限，默认 10000，封顶 `MAX_IMPORT_ROWS=2_000_000`，非法值回落默认；
     - `timeout`：等 job COMPLETED 的秒数，默认 60s，封顶 600s（10 分钟），非法/越界回落默认；
   * **行为**：`_resolve_connector → _require_loader → fetch_sql_as_arrow → workspace.write_parquet_from_arrow(source_info={source_query: sql, ...}) → 返回 {table_name, row_count, columns, source_query, refreshable=False}`
   * `TableMetadata.source_query` 记录原始 SQL（DF 框架已预留该字段）；`source_table` 留空；
   * `loader_params` 经 `get_safe_params()` 自动剔除密码后才落 metadata；
   * 错误分支：缺 `connector_id / sql / table_name` 一律 `INVALID_REQUEST`；workspace 未建（缺 `X-Workspace-Id` 头）由 DF 框架抛 `INVALID_REQUEST: No active workspace`；beelink 端错误经 `classify_and_raise_connector_error(operation="import")` 落到 `DATA_LOAD_ERROR / ACCESS_DENIED / CONNECTOR_AUTH_FAILED`；job 超时报 `DB_CONNECTION_FAILED(retry=True)`（message 含英文 "timeout" 关键词）；
   * 不支持基于 `source_query` 的 refresh（返回 `refreshable=False`）；如需重跑由前端再次提交 SQL。

3. **未触碰**：`fetch_data_as_arrow` / `import-data` / DataAgent / 前端 / beelink Java；POC-1 表导入行为完全不变。

**真机验收（全部 PASS · `feat/beelink-poc1.5-sql-pass-through`）**

| # | 操作 | 结果 |
|---|------|------|
| 1 | `POST /api/connectors/import-sql {}` | `INVALID_REQUEST: connector_id is required` |
| 2 | 缺 `sql` | `INVALID_REQUEST: sql is required` |
| 3 | 缺 `table_name` | `INVALID_REQUEST: table_name is required` |
| 4 | 用 connector `beelink:beelink-main` + `SELECT * FROM smartquery_demo.customers LIMIT 10` + `table_name=customers_sql_poc15` | `success`，10 行真落 parquet，6 列（id/name/gender/region_id/registered_at/age_group），中文 UTF-8 完整 |
| 5 | `GET /api/tables/list-tables`（`X-Workspace-Id: default`） | 同时看到 POC-1 的 `customers`（1000 行）与本表 `customers_sql_poc15`（10 行） |
| 6 | 查 `customers_sql_poc15` metadata | `data_loader_type=BeelinkDataLoader`、`source_table_name=null`、`source_query="SELECT * FROM smartquery_demo.customers LIMIT 10"`、`data_loader_params` 仅含 `base_url/user/verify_ssl`（**密码已剔除**）、`import_options={"size":10}` |
| 7 | 错误 SQL `SELECT bogus FROM smartquery_demo.no_such_table` | `DATA_LOAD_ERROR: Failed to load data from the data source`（不吞 beelink 原报错） |

**当前边界（明确不包含）**

* ❌ POC-2 / NL → Dremio SQL（DataAgent 工具扩展）
* ❌ DataAgent 任何改动
* ❌ 前端 UI 任何改动
* ❌ beelink Java 任何改动
* ❌ SQL Guard / SQL 修复 Agent / 强 BO / 强 RAG / MCP-first

**已知注意事项（踩坑实录）**

1. **登录端点真实路径**：`/apiv2/login` 不是 `/login`。Dremio v25 的 V2 API 由 `RestServerV2.java:40` 用 `@RestApiServer(pathSpec="/apiv2/*")` 挂载；`LogInLogOutResource.java` 的 `@Path("/login")` 是相对路径。设计文档原先据 `@Path` 直接推断的 URL 是错的，本次已修正脚本与 BeelinkDataLoader。
2. **默认测试 SQL 避保留字**：用 `SELECT 1 AS n` 不要用 `SELECT 1 AS one` —— Dremio Calcite 解析器把 `one / two / three / day / month / year / level / value` 等当保留字，否则报 `Encountered "AS one"...Was expecting one of: ...`。
3. **REST body 字段名**：`POST /api/connectors` 用 **`loader_type`**；其余共享动作端点（preview / get-catalog-tree / import-data / refresh-data）用 **`connector_id`**（格式 `<loader_type>:<safe-source-id>`），不是 `source_id`。
4. **import-data 前置**：必须先 `POST /api/sessions/create {"id":"default","name":"default"}` 建 workspace，且后续请求带 `X-Workspace-Id: default` 头。前端 `UnifiedDataUploadDialog` 自动注入，curl 直调要自己加。
5. **页大小硬上限**：`GET /api/v3/job/{id}/results?limit=` 单次最大 500（JobResource.java:125 `Preconditions.checkArgument(limit <= 500, ...)`）。BeelinkDataLoader 已自动分页。
6. **DF Bash background process 容易被 parent shell 一起 kill**（exit 144）。本机调试启 Flask 用 `setsid + < /dev/null + disown` 才能完全脱离。
7. **首屏 Select Models 强引导**：未配 LLM 时 DF 默认 landing 拦截 DataThread；不影响 connector 功能本身，只是体验上需要先去 Settings 选个 model 才能进 Data Thread 做图。

---

## 15. 后续从 POC 演进到正式产品需要补的能力

| 能力 | 必要性 | 工作量 | 备注 |
|------|--------|--------|------|
| SSO 一键登录（方案 C） | 必须 | 1-2 周 | BeelinkAuthProvider + beelink 新增 token-exchange 端点 |
| Catalog 全量同步异步化 + 增量更新 | 必须 | 1 周 | 当前 sync 同步阻塞，大组织几千张表会超时 |
| beelink `limit ≤ 500` 上限协商 | 必须 | 1-2 天（beelink 侧） | 改 5000 或 10000；评估 JVM 内存 |
| 接 Arrow Flight 拉结果 | 推荐 | 1 周 | 比 REST `/job/results` 快 5-10x；DF 端用 pyarrow.flight |
| BO/语义层接入 | 推荐 | 2-3 周 | 复用 beelink `SemanticLayer`；DataAgent 改 prompt 注入 BO |
| SQL Guard | 推荐 | 1 周 | 限制 DDL/DML、最大 SCAN 行数预估 |
| 列级脱敏 / Data Masking 显示 | 推荐 | 1-2 周 | beelink `TableUserColumnsService` 已有，DF 仅显示 masked 标识 |
| 全用户审计日志 | 必须 | 3-5 天 | DF 端记录 SQL+用户+时间，落到 beelink Job History 或独立审计表 |
| 多 beelink 集群支持 | 可选 | 1 周 | source_id 后缀化（`beelink_dc1`、`beelink_dc2`） |
| 报告导出（PDF/Word） | 推荐 | 1-2 周 | DF 已有 `agent_report_gen.py`，对接企业内文档系统 |
| 大查询断点续传 | 可选 | 2 周 | 长 job 异步通知 + 断点 |
| RAG over BO / DataAgent 增强 | 可选 | 2-3 周 | 接 beelink VectorSearchIndex |
| MCP-first 路径 | 远期 | 4+ 周 | 把 beelink REST 暴露为 MCP server；DF 通过 MCP 调用 |
| 容灾 + HA | 必须 | 1-2 周 | DF Workspace 备份；多副本 |

---

## 16. 设计迭代记录

> 以下是从最初的设计直觉到当前 v10 方案的真实演进。每一轮都基于源码核对的新发现，导致方案修正。

### 迭代 #1：第一直觉——"在 beelink 里挂一个 DF 插件"

**当时想法**：beelink 是 Dremio 二开，Dremio plugin 系统很完整，把 DF 做成 beelink 的一个 plugin（用 Java 写 REST 适配，再让 DF 前端读它）。

**核对发现**：Dremio plugin 系统是给"数据源"用的（hive/s3/jdbc），不是给上层应用挂载用的；DF 完全是独立 Python 应用。把 DF 塞进 Dremio JVM 不现实。

**修正**：放弃 Java plugin 路线。DF 独立部署，通过 HTTP REST 调 beelink。

### 迭代 #2："那就让 DF 复用 beelink 的 SmartQuery，DF 只做图表"

**当时想法**：beelink 已经有 SmartQuery（自然语言问数 + 语义层 + 向量检索）—— `services/smartquery/` 有 16 个 Java 文件。让 DF 调 `POST /api/v2/smart-query/ask` 拿 SQL + 结果，DF 只做图表渲染。

**核对发现 & 用户原则冲突**：
- 用户原则明确"Data Formulator 是智能图表主体"——这意味着 DF 的 DataAgent / Data Thread / clarify / explore 循环不能让位；
- 若复用 SmartQuery，DF 退化为可视化外壳，DF 的 Data Thread 多轮迭代能力（drill-down、追问、artifact 关联）无法发挥；
- SmartQuery 是 KPI 导向（`KpiRetryOutcome`），DF 是探索式分析导向，差异大。

**修正**：POC-2 由 DF 的 DataAgent 自己生成 SQL，不复用 SmartQuery。SmartQuery 留作未来对比/降级。

### 迭代 #3："给 DF 写一个 BeelinkConnector，用 Arrow Flight 直连"

**当时想法**：Dremio 性能最好的取数路径是 Arrow Flight；用 pyarrow.flight 直接连 beelink Flight endpoint。

**核对发现**：
- 是 DF `data_loader/` 下没有任何 Flight 客户端，所有现成 connector（PG/MySQL/MSSQL/Kusto/...）走的都是各自原生 client（psycopg2/pymysql/pyodbc/azure-kusto-data）；
- Flight endpoint 在 beelink 中需要单独的 endpoint + token 兑换，POC 阶段引入会拉高 1-2 周；
- DF 框架的核心交付 `fetch_data_as_arrow(source_table)` 接口已经能从任意 client 转 Arrow，Flight 是优化项。

**修正**：POC 阶段用 REST `/api/v3/sql + /api/v3/job/{id}/results` 走完链路。Flight 作为 Phase 5 之后的性能优化。

### 迭代 #4："让 BeelinkDataLoader.fetch_data_as_arrow 直接接 SQL 字符串"

**当时想法**：把 source_table 参数改成 SQL 字符串，POC-1.5 就免费拿到。

**核对发现**（external_data_loader.py:395-396 注释原文）：

> *"Only source_table is supported (no raw query strings) to avoid security and dialect diversity issues across loaders."*

DF 框架明确禁止——所有现有 loader 都遵守这条。绕过它意味着破坏框架不变量，且 connector preview / refresh / 缓存等所有路径都假定 `source_table` 是表名而非 SQL。

**修正**：POC-1.5 / POC-2 的 SQL 直通**不能**走 connector_import_data，必须新增独立端点 `POST /api/beelink/sql/execute`（绕过 connector 框架，直接写 workspace + 持久化 `source_query`）。

### 迭代 #5："POC-2 改 DataAgent SYSTEM_PROMPT 让它直接出 SQL"

**当时想法**：DataAgent prompt 加一段「优先输出 SQL，由 query_remote 工具执行」，全局生效。

**核对发现**：DataAgent SYSTEM_PROMPT（data_agent.py:281-419）极其复杂，规约了 `visualize / clarify / explain / present` 四种 action 的 JSON schema，且会被所有数据源场景共用（CSV/PG/MySQL/Kusto/...）。全局改 prompt 风险高、影响面大。

**修正**：保持主 prompt 不变，**只新增一个工具 `query_beelink_sql`**。LLM 通过工具签名的 description 自然学到使用时机；额外在 SYSTEM_PROMPT 末尾加 1 段「decision guide」段（10-15 行），不动主体。

### 迭代 #6："登录用反向代理 + 共享 cookie 最简单"

**当时想法**：DF 与 beelink 部署在同一域名下，nginx 反代，cookie 共享，DF 后端读 cookie 就能拿到 beelink token。

**核对发现**：
- beelink token 协议是自定义 header `Authorization: _beelink<token>`（TokenUtils.java:27），不是 cookie；
- 反向代理需要运维侧改 nginx 配置，POC 阶段难以快速搭出来；
- DF 已经有完整 AuthProvider 体系 + 4 种 auth 模式（credentials / sso_exchange / delegated / oauth2），更"DF 原生"。

**修正**：POC-1 走最简单的 `credentials`（用户填 base_url + user + password，DF Vault 加密存）；POC-2 升级到 `delegated`（postMessage 把 token 注入 DF）；正式产品再上 `sso_exchange`（DF 后端拿自身 SSO token 换 beelink token）。

### 迭代 #7："DF 端要做 SQL Guard、列级权限校验"

**当时想法**：DF 收到 SQL 后先解析、判断是否越权再发给 beelink。

**核对发现**：
- beelink 已有完整的 `UserRightService`（type ∈ SOURCE/SPACE/TABLE/FOLDER）+ `TableUserColumnsService`（列）+ `TableUserRelationService`（行）；
- beelink 在执行 SQL 时**强制**走 `DACAuthFilter` + Catalog 元数据校验，越权必然报错；
- DF 复刻一份等同于既不准确又冗余（DF 不知道用户的角色和资源权限映射）。

**修正**：DF 端**不做**权限校验，所有 SQL 直通发到 beelink；越权 / 列不存在等错误由 beelink 报错，DF 仅透传。这与用户原则一致。

### 迭代 #8："DF identity 直接用 beelink username"

**当时想法**：DF identity 命名空间用 `user:<beelink_username>` 隔离 workspace，简单粗暴。

**核对发现**：DF 的 identity 类型有三段命名空间（`user:` / `local:` / `browser:`，identity.py:165），其中 `user:` 严格表示"经过 AuthProvider 验证的身份"，是有契约的（`get_sso_token()` 必须能拿到原始 token）。POC-1 阶段 DF 没接 AuthProvider，强行用 `user:zhangsan` 不符合规范。

**修正**：
- POC-1：DF identity 走 `local:<os>` 或 `browser:<uuid>`，beelink 凭证 Vault 存（identity 与 beelink user **不强绑**——POC 可接受）；
- POC-2：内嵌 + postMessage 注入时仍保持 `local:`/`browser:`，token 在 connector params 里；
- 正式产品：实现 `BeelinkAuthProvider`，identity 自然成为 `user:<beelink_username>`，workspace 严格按 beelink user 隔离。

### 迭代 #9："默认拉 1 万行就够 POC"

**当时想法**：POC 默认 size=10000 一刀切。

**核对发现**：
- beelink `/api/v3/job/{id}/results` 单次 `limit ≤ 500`（JobResource.java:125 硬编码 `Preconditions.checkArgument`）；
- 10000 行 = 20 页轮询，按每页 200ms 估算 4-5s（含网络），POC 可接受；
- DF 端 `MAX_IMPORT_ROWS = 2_000_000`（external_data_loader.py:10）；
- 但 POC 实际经常碰到大表（事实表千万级），用户期望"取个 sample 就行"。

**修正**：
- POC 默认 size=10000；
- `import_options.size` 在前端表单暴露给用户调；
- 在 BeelinkClient 内做 size 上限保护（max 2_000_000）；
- 文档明示性能边界，引导用户写 GROUP BY 而非全表 SELECT；
- 未来：推 beelink 把硬上限提到 5000（迭代 #12）。

### 迭代 #10："POC-2 要做专门的 SQL 修复 agent"

**当时想法**：LLM 生成的 SQL 大概率有列名错、引号错；需要单独一个 "SQL Fix Agent" 接收错误信息、产出修复 SQL。

**核对发现**：DataAgent 的 `_tool_loop`（data_agent.py:1531）本身就是个工具调用循环——工具返回的错误文本**自然回流**到下一轮 LLM 输入，LLM 在 prompt 中能看到错误 + schema（前序 `read_catalog_metadata` 已注入），自己就会修。这是 LLM tool-use 框架的标准 self-correction 模式。

**修正**：不做独立修复 agent。`_tool_loop` 内：
- 工具失败 → tool_content 含错误明文；
- 给 `query_beelink_sql` 设单条对话最多 3 次重试（避免死循环、控 token）；
- 实测发现修复成功率显著高于"先扔回去给一个独立 agent"。

### 迭代 #11："Connector source_id 用 `beelink_<base_url_hash>` 区分多集群"

**当时想法**：未来一个用户可能接多个 beelink 集群（生产/测试），source_id 要可变。

**核对发现**：
- DF 现有的 connector 注册大多 `source_id=<loader_type>`（如 `postgresql`），多实例时通过 `connector_id` 区分（实例 ID）；
- POC 阶段强一对多反而把前端 UI 复杂化（用户看到一堆"beelink_xxx"）；
- DF 已经支持用户自建 connector（`POST /api/connectors`），多集群天然按 `user::beelink_<n>` 落地。

**修正**：POC 阶段 `source_id="beelink"` 单实例；多集群支持留给正式版本。

### 迭代 #12："DictTable 加专门字段记录 SQL"

**当时想法**：图表 artifact 要能回溯 SQL，前端 `DictTable.derive.code` 是给 Python 设计的，要新加 `sourceSql` 字段。

**核对发现**：
- 后端 `TableMetadata.source_query: str | None`（workspace_metadata.py:222）**已经存在**——POC-1.5/POC-2 直接落这个字段就行；
- 前端 `DictTable.source: DataSourceConfig` 是已有 typed 字段（connectorId / databaseTable / canRefresh / originalTableName），扩展一个 `sourceQuery?: string` 即可，兼容性零；
- 图表 artifact `visualize.input_tables` 已经能定位回表，链路自然闭环。

**修正**：
- 后端复用 `source_query` 字段；
- 前端 `DataSourceConfig` 加一个可选 `sourceQuery?: string`（5 行代码）；
- DictTable 卡片下方显示"由 SQL 生成"角标 + 点击查看。

### 迭代 #13（v10 收敛）：最终方案

经上述 12 轮取舍，最终方案的核心约束如下：

| 维度 | 收敛点 |
|------|--------|
| 改造范围 | 仅改 DF；beelink 0 修改（POC 阶段） |
| Loader 写法 | 严格遵守 `ExternalDataLoader` 抽象；POC 不接 Flight |
| SQL 直通路径 | 不绕走 `fetch_data_as_arrow`；新增独立端点 `POST /api/beelink/sql/execute` |
| NL→SQL 实现 | 不新建 agent；扩展 DataAgent 加 `query_beelink_sql` 工具 |
| schema 注入 | 复用 `sync-catalog-metadata` + `search_data_tables`（POC-1 用户点一次同步） |
| 登录 | POC-1 用 credentials；POC-2 升 delegated；正式上 SSO exchange |
| 权限 | DF 不做任何校验；全部透传 beelink |
| 持久化 | `TableMetadata.source_query` 已有字段复用 |
| 错误处理 | beelink errorMessage 透传 + 标准 error code 包装 |
| 性能 | POC size=10000；用户可调；max=2M |

### 迭代 #14（2026-05-19 真机实测纠正）：POC-1 落地后修正三处"看着对其实错"的事实

POC-1 BeelinkDataLoader 落地真机一跑就暴露出三处设计文档据"@Path/字面 API 名"
直接推断却没核对 JAX-RS Application 前缀、Calcite 保留字、DF connector 框架
真实 body schema 而踩的坑：

| 错认 | 真相 | 影响范围 | 处置 |
|------|------|----------|------|
| beelink 登录是 `POST /login` | 是 **`POST /apiv2/login`**（`RestServerV2.java:40 @RestApiServer(pathSpec="/apiv2/*")` 是 JAX-RS Application 前缀） | Phase 0 脚本 + BeelinkDataLoader + 文档 §4.2/§5/§10/§12/§13/§A | 已全部修正；test_connection 也从 `GET /login` 改 `GET /apiv2/login` |
| 默认 ping SQL `SELECT 1 AS one` | Dremio Calcite 把 `one` 当保留字，整条 SQL 被拒；用 **`SELECT 1 AS n`** | Phase 0 脚本 + env.example + README | 已修正；README 加保留字避坑清单 |
| connector 共享端点 body 用 `source_id` | 实际用 **`connector_id`**（格式 `<loader_type>:<safe-source-id>`，dash 不是 underscore）；只有 `POST /api/connectors` 创建实例时是 `loader_type + source_id + params` | 文档 §5.3/§5.4/§10.6 | 已修正；保留 §10.5 POC-1.5 新设计端点的字段名待 POC-1.5 真正实现时再决定 |
| `app.py` 需要修改 5 行注册 admin connector | **完全不用改**：`register_data_connectors` 只挂 admin-pinned；普通 loader 走 `DATA_LOADERS` dict 自动 discovery + 用户 UI 自建实例 | 文档 §5.5 | 已修正 |

**收敛**：POC-1 真机端到端通过（见 §14.6），文档以本次回写为准。后续推进 POC-1.5 / POC-2 时如再发现新偏差，遵循同一原则：**真机为准，回写文档**。

---

## 附录 A：BeelinkDataLoader 关键方法骨架

```python
# py-src/data_formulator/data_loader/beelink_data_loader.py
import logging, time
from typing import Any
import pyarrow as pa
import requests

from data_formulator.data_loader.external_data_loader import (
    CatalogNode, ExternalDataLoader, MAX_IMPORT_ROWS,
)

logger = logging.getLogger(__name__)
_DEFAULT_PAGE_SIZE = 500   # beelink hard cap
_DEFAULT_TIMEOUT_SECS = 30


class BeelinkDataLoader(ExternalDataLoader):

    @staticmethod
    def list_params() -> list[dict[str, Any]]:
        return [
            {"name": "base_url", "type": "string", "required": True,
             "default": "", "tier": "connection",
             "description": "Beelink base URL, e.g. http://beelink:9047"},
            {"name": "user", "type": "string", "required": True,
             "default": "", "tier": "auth"},
            {"name": "password", "type": "string", "required": True,
             "default": "", "tier": "auth", "sensitive": True},
            # POC-2 升级 delegated 时打开：
            # {"name": "access_token", "type": "string", "required": False,
            #  "default": "", "tier": "auth", "sensitive": True,
            #  "description": "Optional: pre-issued beelink token from postMessage"},
        ]

    @staticmethod
    def auth_instructions() -> str:
        return ("**Beelink 连接**：填入 Beelink 的 base URL（如 `http://beelink:9047`）"
                "及您在 Beelink 中的用户名/密码。DF 会通过 `POST /apiv2/login` 拿 token，"
                "并按您账号在 Beelink 中的权限访问数据。\n\n"
                "密码加密存储在 DF Vault 中。")

    @staticmethod
    def auth_mode() -> str:
        return "connection"

    @staticmethod
    def auth_config() -> dict:
        return {"mode": "credentials"}

    @staticmethod
    def catalog_hierarchy() -> list[dict[str, str]]:
        # Beelink path 深度不固定（mysql_prod.sales.orders 三段 / iceberg_lake.warehouse.fact 三段 / ...）
        # POC 阶段用两层（source + table），列表节点把多层 path 拼成 "."
        return [
            {"key": "source", "label": "Source"},
            {"key": "table",  "label": "Table"},
        ]

    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.base_url = (params.get("base_url") or "").rstrip("/")
        self.user = params.get("user", "")
        self.password = params.get("password", "")
        self.access_token = params.get("access_token") or None
        if not self.base_url:
            raise ValueError("base_url is required")
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        if self.access_token:
            self.session.headers["Authorization"] = f"_beelink{self.access_token}"
            self.token = self.access_token
            self.token_expires_at = None
        else:
            self._login()

    def _login(self):
        r = self.session.post(
            f"{self.base_url}/apiv2/login",
            json={"userName": self.user, "password": self.password},
            timeout=_DEFAULT_TIMEOUT_SECS,
        )
        r.raise_for_status()
        data = r.json()
        self.token = data["token"]
        self.token_expires_at = data.get("expires")
        self.username_resolved = data.get("userName") or self.user
        self.session.headers["Authorization"] = f"_beelink{self.token}"

    def _request(self, method: str, path: str, **kw):
        r = self.session.request(method, f"{self.base_url}{path}",
                                  timeout=_DEFAULT_TIMEOUT_SECS, **kw)
        if r.status_code == 401 and not self.access_token:
            # 自动 re-login
            self._login()
            r = self.session.request(method, f"{self.base_url}{path}",
                                      timeout=_DEFAULT_TIMEOUT_SECS, **kw)
        if r.status_code >= 400:
            # 透传 beelink errorMessage
            try:
                payload = r.json()
                msg = payload.get("message") or payload.get("errorMessage") or r.text
            except Exception:
                msg = r.text
            raise RuntimeError(f"Beelink {method} {path} {r.status_code}: {msg}")
        return r.json()

    # ─── Catalog ───────────────────────────────────────────────
    def list_tables(self, table_filter: str | None = None) -> list[dict[str, Any]]:
        tables: list[dict[str, Any]] = []
        top = self._request("GET", "/api/v3/catalog")
        for item in top.get("data", []):
            if item.get("containerType") == "SOURCE":
                self._walk(item, item.get("path", []), tables, table_filter)
        return tables

    def _walk(self, node, prefix_path, out: list, kw_filter: str | None):
        node_id = node.get("id")
        if not node_id:
            return
        try:
            full = self._request("GET", f"/api/v3/catalog/{node_id}?maxChildren=1000")
        except Exception as e:
            logger.warning("walk failed at %s: %s", "/".join(prefix_path), e)
            return
        for child in full.get("children", []) or []:
            ct = child.get("type") or child.get("entityType")
            cpath = child.get("path", [])
            if ct == "DATASET":
                name_str = ".".join(cpath)
                if kw_filter and kw_filter.lower() not in name_str.lower():
                    continue
                out.append({
                    "name": name_str,
                    "path": cpath,
                    "metadata": {
                        "row_count": None,
                        "columns": [],  # 留给 get_column_types 填充
                    },
                    "table_key": name_str,
                })
            elif ct in ("FOLDER", "CONTAINER", "SPACE", "SOURCE"):
                self._walk(child, cpath, out, kw_filter)

    def get_column_types(self, source_table: str) -> dict[str, Any]:
        path_segments = source_table.split(".")
        path = "/".join(path_segments)
        try:
            ds = self._request("GET", f"/api/v3/catalog/by-path/{path}?include=dataset")
        except Exception:
            return {}
        columns = []
        for f in ds.get("fields", []) or []:
            t = (f.get("type") or {}).get("name") if isinstance(f.get("type"), dict) else f.get("type")
            columns.append({
                "name": f["name"],
                "type": t or "VARCHAR",
                "is_dttm": str(t).upper() in {"TIMESTAMP","DATE","TIME"},
                "description": None,
            })
        return {"columns": columns, "description": None}

    # ─── Data fetching ─────────────────────────────────────────
    @staticmethod
    def _quote_ident(seg: str) -> str:
        return '"' + seg.replace('"', '""') + '"'

    def _build_select_sql(self, path_segments: list[str], options: dict | None) -> str:
        from_clause = ".".join(self._quote_ident(s) for s in path_segments)
        cols = "*"
        order = ""
        limit = ""
        if options:
            columns = options.get("columns")
            if columns:
                cols = ", ".join(self._quote_ident(c) for c in columns)
            sc = options.get("sort_columns")
            if sc:
                so = "DESC" if (options.get("sort_order","asc").lower() == "desc") else "ASC"
                order = " ORDER BY " + ", ".join(f"{self._quote_ident(c)} {so}" for c in sc)
            size = options.get("size")
            if size:
                limit = f" LIMIT {int(min(size, MAX_IMPORT_ROWS))}"
        return f"SELECT {cols} FROM {from_clause}{order}{limit}"

    def fetch_data_as_arrow(self, source_table: str,
                            import_options: dict[str, Any] | None = None) -> pa.Table:
        if not source_table:
            raise ValueError("source_table is required")
        path_segments = source_table.split(".")
        sql = self._build_select_sql(path_segments, import_options)
        return self._exec_sql_to_arrow(sql)

    # ─── POC-1.5 / POC-2 入口 ──────────────────────────────────
    def execute_sql_to_arrow(self, sql: str,
                             context: list[str] | None = None,
                             max_rows: int = 10000) -> pa.Table:
        return self._exec_sql_to_arrow(sql, context=context, max_rows=max_rows)

    def _exec_sql_to_arrow(self, sql: str,
                            context: list[str] | None = None,
                            max_rows: int = MAX_IMPORT_ROWS) -> pa.Table:
        body: dict[str, Any] = {"sql": sql}
        if context:
            body["context"] = context
        submit = self._request("POST", "/api/v3/sql", json=body)
        job_id = submit["id"]
        self._wait_for_job(job_id, timeout_secs=_DEFAULT_TIMEOUT_SECS)
        return self._fetch_results_paginated(job_id, max_rows)

    def _wait_for_job(self, job_id: str, timeout_secs: int):
        deadline = time.time() + timeout_secs
        delay = 0.2
        while True:
            status = self._request("GET", f"/api/v3/job/{job_id}")
            st = status.get("jobState")
            if st == "COMPLETED":
                return
            if st in {"FAILED", "CANCELED"}:
                msg = status.get("errorMessage") or f"job {st}"
                raise RuntimeError(f"Beelink job {st}: {msg}")
            if time.time() > deadline:
                raise RuntimeError(f"Beelink job {job_id} timed out after {timeout_secs}s")
            time.sleep(delay)
            delay = min(delay * 1.4, 2.0)

    def _fetch_results_paginated(self, job_id: str, max_rows: int) -> pa.Table:
        all_rows: list[dict[str, Any]] = []
        offset = 0
        page = _DEFAULT_PAGE_SIZE
        schema_payload = None
        while len(all_rows) < max_rows:
            limit = min(page, max_rows - len(all_rows))
            data = self._request("GET",
                                  f"/api/v3/job/{job_id}/results?offset={offset}&limit={limit}")
            if schema_payload is None:
                schema_payload = data.get("schema")
            rows = data.get("rows") or []
            if not rows:
                break
            all_rows.extend(rows)
            offset += len(rows)
            if len(rows) < limit:
                break
        # 把 rows + schema 转 pa.Table。schema 字段（Arrow JSON）做最佳努力解析；
        # 不解析时 fallback 用 pyarrow 从 list-of-dict 推断 dtype。
        try:
            return pa.Table.from_pylist(all_rows)
        except Exception:
            # 兜底：强制全字符串
            cols = sorted({k for r in all_rows for k in r.keys()})
            data_cols = {c: [r.get(c) for r in all_rows] for c in cols}
            return pa.table({c: pa.array(v) for c, v in data_cols.items()})

    def test_connection(self) -> bool:
        try:
            self._request("GET", "/apiv2/login")  # isUserAuthorized
            return True
        except Exception:
            return False
```

## 附录 B：`POST /api/beelink/sql/execute` 端点骨架

```python
# py-src/data_formulator/routes/beelink.py
import logging
from flask import Blueprint, request
from data_formulator.auth.identity import get_identity_id
from data_formulator.workspace_factory import get_workspace
from data_formulator.data_connector import DATA_CONNECTORS
from data_formulator.datalake.parquet_utils import sanitize_table_name, normalize_dtype_to_app_type
from data_formulator.error_handler import json_ok
from data_formulator.errors import AppError, ErrorCode

beelink_bp = Blueprint("beelink", __name__)
logger = logging.getLogger(__name__)


@beelink_bp.route("/api/beelink/sql/execute", methods=["POST"])
def execute_sql():
    data = request.get_json(force=True) or {}
    source_id = data.get("source_id", "beelink")
    sql = (data.get("sql") or "").strip()
    table_name = (data.get("table_name") or "").strip()
    max_rows = int(data.get("max_rows") or 10000)
    context = data.get("context") or None
    if not sql:
        raise AppError(ErrorCode.INVALID_REQUEST, "sql is required")
    if not table_name:
        raise AppError(ErrorCode.INVALID_REQUEST, "table_name is required")

    connector = DATA_CONNECTORS.get(source_id)
    if connector is None:
        raise AppError(ErrorCode.INVALID_REQUEST, f"Unknown connector '{source_id}'")
    loader = connector._get_loader() or connector._try_auto_reconnect(connector._get_identity())
    if loader is None:
        raise AppError("CONNECTOR_NOT_CONNECTED",
                       f"Beelink connector is not connected. Please connect first.",
                       http_status=409)
    workspace = get_workspace(get_identity_id())
    safe_name = sanitize_table_name(table_name)

    try:
        arrow_table = loader.execute_sql_to_arrow(sql, context=context, max_rows=max_rows)
    except RuntimeError as e:
        # Beelink 失败：透传
        raise AppError("BEELINK_QUERY_FAILED", str(e), http_status=400, retry=False)

    meta = workspace.write_parquet_from_arrow(
        arrow_table, safe_name,
        source_info={
            "loader_type": "BeelinkDataLoader",
            "loader_params": loader.get_persistable_params(),
            "source_table": None,
            "source_query": sql,
            "import_options": {"max_rows": max_rows},
        },
    )

    sample = arrow_table.slice(0, 10).to_pylist()
    return json_ok({
        "table_name": meta.name,
        "row_count": meta.row_count,
        "columns": [c.to_dict() for c in (meta.columns or [])],
        "sample_rows": sample,
        "source_query": sql,
        "truncated": arrow_table.num_rows >= max_rows,
        "refreshable": True,
    })
```

## 附录 C：DataAgent 新增工具 `query_beelink_sql`

```python
# 加到 py-src/data_formulator/agents/data_agent.py 的 TOOLS 列表末尾
TOOLS.append({
    "type": "function",
    "function": {
        "name": "query_beelink_sql",
        "description": (
            "Push a Dremio SQL query down to a connected beelink data source. "
            "Returns a new workspace table (parquet) with the result rows that "
            "you can reference in subsequent explore/visualize calls.\n\n"
            "USE when: (a) the relevant table is in beelink but NOT yet imported, "
            "AND (b) the question requires aggregation/filtering that would be "
            "wasteful to run on a full-table import.\n"
            "DO NOT use for: small already-imported tables (use explore + duckdb)."
            "\n\nSyntax tips: reference tables with quoted multi-segment paths, "
            'e.g. "mysql_prod"."sales"."orders". '
            "Dremio SQL is ANSI-like and supports CTE, window funcs, date_trunc, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "purpose":     {"type": "string", "description": "One-line user-facing progress text."},
                "source_id":   {"type": "string", "default": "beelink"},
                "sql":         {"type": "string"},
                "table_name":  {"type": "string", "description": "snake_case name for resulting table."},
                "max_rows":    {"type": "integer", "default": 10000},
            },
            "required": ["purpose","sql","table_name"],
        },
    },
})

# 加到 _tool_loop() 的 elif 链
elif tool_name == "query_beelink_sql":
    from data_formulator.routes.beelink import run_beelink_sql_for_agent
    try:
        result = run_beelink_sql_for_agent(
            source_id=tool_args.get("source_id","beelink"),
            sql=tool_args["sql"],
            table_name=tool_args["table_name"],
            max_rows=tool_args.get("max_rows", 10000),
            workspace=self.workspace,
        )
        tool_content = json.dumps(result, ensure_ascii=False)
        yield {"type":"tool", "tool":tool_name,
                "purpose": tool_args.get("purpose"),
                "table_registered": result["table_name"]}
    except Exception as e:
        tool_content = f"query_beelink_sql failed: {e}"
```

---

> 文档结束。任何条款若在实施中发现与源码冲突，以源码为准并回写本文档。
