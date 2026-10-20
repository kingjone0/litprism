# AGENT.md · LitPrism 交接速查

更新：2026-10-20。本文只保留开发接手约定；当前行为与验收见 [当前状态](docs/当前实现与验收状态.md)，原需求、阶段映射与 N01–N12 见 [实施计划](docs/实现计划.md)，其余见 [文档导航](docs/README.md)。历史进度不在这里重复维护。

## 当前基线

- 路线：Electron + Python sidecar + FastAPI/原生 JS；**Tauri 工程已于 2026-10-19 删除**（本机 Chromium GPU 崩溃后退场），electron-builder 打包已跑通但安装验收（N11）未做。当前 schema **v12**（新增材料快照、块/分析版本绑定与历史分析归档）；最新测试及无窗口验收结果统一见当前状态页。
- N05 漏提补录与 N06 质量面板已实现（2026-10-19）：右栏「补一条」必须绑原文原句；`GET /api/paper/{pid}/quality` 分字段给出已审/接受率口径的精度/可用率/复核覆盖/已采集漏提占比，与 `tools/eval_extraction.py` 共用 `litprism.quality` 聚合，**同一份数据只允许一个数字**。旧接口键 `recall` 表示已采集漏提占比，不是真实召回率。比例分母为 0 一律 `None`（界面 n/a），不显示 0%。
- 当前增量包含长文计划真实范围、完整请求预算、全阶段用量，以及单篇 N01 持久运行与恢复（阶段调用预算已取消）。详见 [N01 契约](docs/阶段预算与可续验进度-2026-10-18.md)。旧“重新解读”、对比、QA、Skill、CLI/MCP 尚未统一接入持久续跑。
- 机器支持、人工确认、原文覆盖是不同状态。真实固定样本结果有限；fastText、GPT-3、RMSNorm 的不同版本/片段/回放不能拼成当前整篇长论文质量通过。
- N02 已完成首轮受控实测：[协议与结果](docs/N02真实链路验收.md)。18 个正反例和 5 个差异控制通过；两轮论文仍有提取配额、输出截断/复核熔断和跨页来源误判，不能称整篇通过。后续真实语义验收尚未恢复；原反例与失败记录仍保留。
- 现行/修订目标分开：见 [当前设计与流程图](docs/当前设计与功能流程图.md)。目标为可选全文批量辅助复核、人工可直接审核；能力缺失不再静默：未选模型时真实入口直接报错；`active_limits()` 返回 `capability_missing`，运行记录顶部提示；选模后自动刷新一次能力目录。
候选陈述使用确定性 ID 放入 `_candidates`；注意实际写库仍在整个解读末尾，复核前并未持久化。`semantic_review=false` 可跳过机器复核，但当前写审核接口仍只查 report.items，因此跳过后的候选直接确认尚未闭环（N03）。
复核层在能力已知时已按窗口批量并复用提取全文前缀（240 条合成数据：240 次 → 1 次），能力未知入口仍为逐条小来源；差异层同轮接线（240 条 → 6 次或 1 次，覆盖完整）。已有人工接口不等于复核前候选工作流已完成。待审隔离和来源放行规则继续生效。
- Flash 能力默认 1M/384K，API 能力优先，数据只读、任务输出自动分配；保留 P 系列规划。思考只作会话展示，不入证据；见 [能力与展示](docs/模型能力与思考展示-2026-10-19.md)。
- N01、N02 验收工具、能力数据、思考展示与六模块文档已纳入当前源码；分支同步和提交状态以 Git 为准。F3、打包、D3 继续暂停，不增加本地 embedding、模型或向量库。新的提交、push、打包仍分别按用户授权执行。

功能职责见 [六模块导航](docs/modules/README.md)：M01 来源发现 → M02 论文/网页资料入库 → M03 阅读与审核 → M04 问答研究 → M05 知识产出，M06 为运行基础。论文库可以只有题录/摘要，搜索摘要不等于正文；Unpaywall/SearXNG 仅有配置/设想，不能计为已接通来源。统一候选池、arXiv 自动取全文、本地上传与完整度已接通；普通网页正文抓取及任意出版商取全文未实现。

N12 已实现并通过离线验收：重复导入生成材料快照，默认只读/检索 current，旧块 ID/审核哈希不重写；重复相同材料幂等，低完整度材料不替换当前全文。旧分析可查看但不能当作当前原文自动复用或导出。旧库全部已有块只保守归为 legacy 快照，不猜测历史混入版本。详见 [N12 契约](docs/N12原文材料版本化-2026-10-20.md)。

N08 已在本轮接通备份/预检/新目录恢复与迁移/重启切换及取消，见 [N08 契约](docs/N08备份恢复与目录迁移-2026-10-20.md)。N12/N08 已纳入当前源码，提交与同步状态以 Git 为准；最新全量 953 项通过，无窗口验收通过。本轮未打开、备份或迁移用户实际库，未调用真实模型或打包。下一项为 N07，但真实付费验收不自动启动。

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
4. N01 成功记录仅精确复用；失败/started 不自动重试，未知尝试保留为结果和费用未知。来源、模型/协议、参数、实现或分析变化即拒绝恢复；回放不算新质量样本。
5. 偏好记忆不能承载事实、作证据或引用来源；事实需走知识快照与审核。GenUI 的来源/页码/可信等级由数据库回填，模型不可覆盖。
6. Skill 仅声明式，不能执行目录中的代码，也不能移除引用校验。网页搜索摘要先待审，不能当论文全文或自动可信知识。
7. 配置先校验再合并落盘；拒绝 api_key 配置写入，不修改 config.example.yaml。保存用户配置与只读资源分根，frozen 临时解包目录不能承载用户数据。
8. N08 只恢复至新目录；先校验再发布、再显式安排重启切换，当前连接不热换库。密钥库不迁移，恢复后缺密钥不能退 mock。预检/恢复不能打开或迁移当前主库；对源库使用在线 backup API，不能直接复制活跃 DB/WAL。

## 排障前先确认观测

- 本机 curl 必须带 `--noproxy '*'`。先发一条可控请求确认访问日志能观测，再判断“前端没有请求”；sidecar stdout 是握手管道。
- onefile 有启动器和真实进程：按握手 pid 先回收真实进程，再收启动器，避免孤儿占端口。
- 本机 Chromium 曾出现 GPU 进程 `0xC0000005`；保留现有 Electron 禁用 GPU/沙箱措施。启动前清除进程环境 `ELECTRON_RUN_AS_NODE`，不改全局变量。
- sidecar 同源加载页面，token 由 URL hash 传递；API 需 token，页面骨架/静态资源和 ping 按既有豁免规则。不要为已删除的 Tauri 跨源方案引入额外改动。
- 原始 PDF、响应、临时库和截图只留本地 `_selftest.*`。不强制加入 Git，不为修历史链接重跑真实服务。

## 核心代码入口

| 范围 | 文件 |
|---|---|
| Schema/材料/核验/来源 | `schema.py`、`materials.py`、`verify.py`、`evidence.py`、`knowledge.py` |
| 长文计划与解读 | `reading.py`、`plan.py`、`extract.py`、`gates.py` |
| 语义/用量/持久运行 | `semantics.py`、`usage.py`、`analysis_runs.py` |
| 问答/对比/技能/上下文 | `agents.py`、`skills/`、`context/`、`memory/` |
| API 与 UI | `web/server.py`、`web/analysis_run_routes.py`、`web/semantic_routes.py`、`web/static/` |
| 备份与恢复 | `backup.py`、`web/backup_routes.py`、`web/static/backup-ui.js` |
| 桌面与数据路径 | `sidecar.py`、`config.py`、`secrets.py`、`desktop-electron/main.js` |

以上 Python 路径相对于 `src/litprism/`；桌面入口相对于仓库根。改动后运行适当回归，提交、push、打包分别按用户授权执行。
