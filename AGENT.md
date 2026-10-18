# AGENT.md · LitPrism 交接速查

更新：2026-10-18。本文只保留开发接手约定；当前行为与验收见 [当前状态](docs/当前实现与验收状态.md)，原需求、阶段映射与 N01–N11 见 [实施计划](docs/实现计划.md)，其余见 [文档导航](docs/README.md)。历史进度不在这里重复维护。

## 当前基线

- 路线：Electron + Python sidecar + FastAPI/原生 JS；Tauri 是历史备选。当前 schema v7；最新测试及无窗口验收结果统一见当前状态页。
- 当前增量包含长文计划真实范围、完整请求预算、全阶段用量，以及单篇 N01 分阶段预算与持久恢复。详见 [N01 契约](docs/阶段预算与可续验进度-2026-10-18.md)。旧“重新解读”、对比、QA、Skill、CLI/MCP 尚未统一接入持久续跑。
- 机器支持、人工确认、原文覆盖是不同状态。真实固定样本结果有限；fastText、GPT-3、RMSNorm 的不同版本/片段/回放不能拼成当前整篇长论文质量通过。
- 下一项 N02 须先固定真实样本、指标与调用预算。F3、打包、D3 转写继续暂停；不增加本地 embedding、模型或向量库。
- 用户本轮授权清理文档并在主工作树提交；不包含 push 或打包。按 Git 实际状态判断提交结果，不在文档硬写当前提交哈希。

## 工作树与运行隔离

主工作树是 `E:/aicoding/paper-research-agent`（`main`），关联工作树是 `E:/codex-litprism`（`codex-work`）。共用 Git 对象库，但工作区和索引独立；不要复制出第三份仓库、删除或重建关联工作树。操作前核对目标目录、分支和未提交修改，不覆盖另一工作树的新改动。

关联工作树应用数据使用 `LITPRISM_HOME=E:/codex-litprism-data`。测试使用临时库并清除测试进程的 `LITPRISM_HOME`；不能为检查配置而打开用户主库触发迁移。Key 只走 SecretStore，不进源码、文档、日志、测试夹具或提交。

## 验证命令

必须使用指定 Python；系统 python 缺 pytest。只修改当前进程环境。

```powershell
$py = 'C:/Users/18434/.workbuddy/binaries/python/envs/default/Scripts/python.exe'
Remove-Item Env:LITPRISM_HOME -ErrorAction SilentlyContinue
& $py -m pytest tests -p no:cacheprovider

# 无窗口真实本地 API + 临时数据库 + 离线模型，不发送用户论文
$env:NODE_PATH = 'C:/Users/18434/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
& $py tools/check_workflows.py
```

pyproject 已含 `-q`，不重复添加。GUI 项目不意味着要开可见窗口验证；使用测试、后台 API 和无窗口浏览器。源码测试通过不代表安装包或真实模型学术质量通过。扩大付费调用前明确目标和预算，`tools/check_live_plan.py --live` 等不是默认接手自检。

## 不能削弱的边界

1. 卖点是“不编”：多源真实性核验、检索与引用回溯、导入守卫共同生效。`verified`/`manual_confirmed` 可引用；`likely` 需人工确认；`not_found` 是源正常但无匹配，`unchecked` 是源失败，不能混用。
2. 无召回直接拒答；来源编号、实际 chunk、物理页、范围和当前可信状态需匹配。生成期间失效与导出时失效都要复核。待审、冲突、拒绝或撤销不得进入正常结论。
3. 机器语义复核保证范围仅为解读六字段和对比；严格 JSON、来源白名单、预算和失败待审不放宽。复杂表格无法恢复则待核对；旧文献须重导入，不自动重写块 ID。
4. N01 成功记录仅精确复用；失败/started 不自动重试，未知尝试占预算。来源、模型/协议、参数、实现或分析变化即拒绝恢复；回放不算新质量样本。
5. 偏好记忆不能承载事实、作证据或引用来源；事实需走知识快照与审核。GenUI 的来源/页码/可信等级由数据库回填，模型不可覆盖。
6. Skill 仅声明式，不能执行目录中的代码，也不能移除引用校验。网页搜索摘要先待审，不能当论文全文或自动可信知识。
7. 配置先校验再合并落盘；拒绝 api_key 配置写入，不修改 config.example.yaml。保存用户配置与只读资源分根，frozen 临时解包目录不能承载用户数据。

## 排障前先确认观测

- 本机 curl 必须带 `--noproxy '*'`。先发一条可控请求确认访问日志能观测，再判断“前端没有请求”；sidecar stdout 是握手管道。
- onefile 有启动器和真实进程：按握手 pid 先回收真实进程，再收启动器，避免孤儿占端口。
- 本机 Chromium 曾出现 GPU 进程 `0xC0000005`；保留现有 Electron 禁用 GPU/沙箱措施。启动前清除进程环境 `ELECTRON_RUN_AS_NODE`，不改全局变量。
- sidecar 同源加载页面，token 由 URL hash 传递；API 需 token，页面骨架/静态资源和 ping 按既有豁免规则。不要为旧 Tauri 的跨源推断引入额外改动。
- 原始 PDF、响应、临时库和截图只留本地 `_selftest.*`。不强制加入 Git，不为修历史链接重跑真实服务。

## 核心代码入口

| 范围 | 文件 |
|---|---|
| Schema/核验/来源 | `schema.py`、`verify.py`、`evidence.py`、`knowledge.py` |
| 长文计划与解读 | `reading.py`、`plan.py`、`extract.py`、`gates.py` |
| 语义/用量/持久运行 | `semantics.py`、`usage.py`、`analysis_runs.py` |
| 问答/对比/技能/上下文 | `agents.py`、`skills/`、`context/`、`memory/` |
| API 与 UI | `web/server.py`、`web/analysis_run_routes.py`、`web/semantic_routes.py`、`web/static/` |
| 桌面与数据路径 | `sidecar.py`、`config.py`、`secrets.py`、`desktop-electron/main.js` |

以上 Python 路径相对于 `src/litprism/`；桌面入口相对于仓库根。改动后运行适当回归，提交、push、打包分别按用户授权执行。
