# beelinkDF 工作规则

## 当前项目线

当前项目是 beelinkDF / Data Formulator + beelink 智能问数。
不要带入 macOS 期货终端上下文。

当前主要分支：

```
feat/chatbi-native-df-integration
```

当前产品方向：

- 以 Data Formulator 作为智能问数二开主体，beelink 作为数据源和权限裁决方
- `/chatbi` 是 POC / 产品化过渡入口
- DF 原生页是后续主要分析承载页

## 默认禁区

除非用户明确授权，不要修改：

- beelink Java
- BeelinkDataLoader
- `data_connector.py`
- DataAgent 核心逻辑（`agents/data_agent.py`）
- `agents/nl2sql.py`
- redux workspace 核心状态机
- 大规模前端架构
- 新 npm 依赖

不要提交任何密钥、token、cookie、密码或真实 API key。

## 默认工作方式

每轮任务默认自行完成：

1. `git status` / `git diff` 自检
2. 明确当前分支
3. 最小范围实现
4. `npm run build`
5. `python -m py_compile py-src/data_formulator/chatbi_demo.py`
6. 如修改其他 Python 文件，对相关 Python 文件也执行 `py_compile`
7. 真机验收关键链路（见下）
8. `git diff` 确认未改禁区
9. `git commit`
10. `git push`

除非任务明确说"只分析不改代码"，否则完成低风险实现后可以 commit + push。

## 默认验收链路

每轮前端 / 产品化修改后，默认检查：

- `/chatbi` 可正常提问
- `/chatbi` 查询结果可点击"在 DF 原生界面继续分析"
- DF 原生页能进入结果表分析态
- 普通图表渲染正常
- `deepseek-chat` 默认模型不被破坏
- 默认中文 UI 不被破坏
- 系统消息中文化不被破坏

## 输出规则

最终输出尽量简短，只输出：

1. 改动文件
2. 根因或修复内容
3. build / py_compile 结果
4. 真机验收结果
5. commit hash
6. push 结果
7. 是否未改禁区

不要输出大段过程日志。
不要重复解释已知背景。
不要每次复述禁区，除非发现风险。

## 当前已知状态

已完成：

- POC-1 BeelinkDataLoader 导入表
- POC-1.5 `import-sql` 手写 SQL
- POC-2A NL2SQL API
- POC-2B DataAgent `query_beelink_sql` tool
- POC-2C-lite 同轮相同 SQL `reused=true`
- `/chatbi` 支持表清单、字段探索、样例预览、简单聚合、轻语义 join 问答
- `/chatbi` 结果可跳转 DF 原生分析态
- DF 原生页产品外壳已收敛为"智能问数"
- DeepSeek `deepseek-chat` 默认模型已接入（`DEEPSEEK_API_KEY` 存在即自动注册全局模型）
- 系统消息已做展示层中文化（`src/views/MessageSnackbar.tsx` 的 `localizeMessage`）
- EN / 中文切换已隐藏，默认中文（`src/i18n/index.ts` lng=zh）
- Chart Insight 在非 vision 模型下禁用并显示中文 tooltip
- 首次加载慢的主要原因是 `DataFormulator.js` 主 bundle 约 7.4 MB（未 gzip）

当前待重点关注：

- 首屏加载加速：优先定位 Flask / Vite build 产物 serve 链路，评估 gzip / cache 的最小优化
- **不要优先做 code splitting 大重构**
- **不要优先处理 `table-265247` orphan table 状态机问题**

## 暂存方向（待评估）

方案 C：DF 原生页内置轻量 Ask 入口

- 在 DF 首页 / 分析页顶部加智能问数输入框
- 复用现有 `/chatbi/agent-stream` 后端能力
- 提问成功后结果表直接进 DataThread（复用 `loadWorkspaceTableByName` thunk）
- 不重写 `/chatbi`，不做大规模前端架构调整
