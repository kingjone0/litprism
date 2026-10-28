# AGENT.md · LitPrism交接速查

更新：2026-10-28。先读[当前状态](docs/当前实现与验收状态.md)和[唯一实施计划](docs/实现计划.md)，文档入口见[导航](docs/README.md)。本文保留开发约定、已验证环境及铁律，逐轮实测已归[历史](docs/history/README.md)。

## 当前执行点

当前分支codex-work，冻结[13项台账](docs/history/前端交互专项排查-2026-10-28.md)已随aeac23c提交。随后Tauri清理、整体复核、G01/G02/G03及G05a随本次集中提交收口（父aeac23c，标识见Git记录）；未push/打包。最新G05a改前1486项、修后1501项，52组N10及完整无窗口流程通过，界面0脚本异常/浏览器外部请求。前批47/45组、72组合及31项Node分别保留，不与本轮或真实学术质量混算。最近真实QA范围见[实用报告](docs/history/N04默认联网与真实追问验收-2026-10-26.md)。

G01全库可达性已收口；G02/G03将Web论文初读/重读统一为N01持久路径，移除独立“分阶段解读”按钮，先展示当前快照材料范围。预检不足不构造模型/建运行；摘要/未知有足够材料仍可明确范围后阅读，不加普遍人工审核。新建与精确恢复区分，旧分析保留，未知显式重试，回执丢失先查记录；同应用同论文活动运行防重。WinError5配置替换反例补最多4次/140ms恢复，永久失败报错并保留旧文件。G05a流式已实现：两协议实际正文/服务思考增量，经只读events订阅；草稿不进事实/知识，完成后仍校验，刷新/重连不新增QA，后台重启不保留未完成草稿。真实DeepSeek合成协议两轮保留首失败未知、次增量成功198token，非学术质量通过。下一步G04固定验收。当前证据`_selftest.g05a-stream-20261028/`，前批保留；首次脚本/环境及hidden按钮失败见[整体复核](docs/整体设计与实现缺口复核-2026-10-28.md)。

## 实现基线与接手要点

- Electron + Python sidecar + FastAPI/原生JS，schema v15。Tauri已退出；旧exe不含当前改动，F3/打包、D3、本地embedding及全量模型复核暂停，N07策略A/B已取消。
- P系列复用固定全文前缀和真实范围，超窗完整分批；章节覆盖不以某条指令块是否被引用判定，参考文献不计。PDF书签优先、每指令配额上限24、指令间有界概要是当前实现，不是完整理解证明。
- 结构走形单独重试一次并记录coverage.structure_failures；未产出uncovered_units与有产出但未过闸门flagged_units分开。不要把章节送达、条数达标或缓存命中算学术质量通过。
- 全部提取完成后、机器复核前保存独立ac_候选快照，绑定材料和父分析哈希。关闭或失败仍能直接人工审核；候选未审不作正式结论。不是每条指令完成即保存。
- 能力已知时语义/差异复核复用全文并按窗口批量；旧能力未知入口保留兼容约束。同字段同批/跨批检查不等于跨字段全量一致性；机器支持、人工确认和覆盖是不同状态。
- 新Web默认相关全库论文/可信知识＋实际联网摘要，选论文/分组只用该范围；空选择不限定，非法ID报错。补齐命中论文当前材料时保留知识/外部证据，完整送达不代表语义通过。
- 新QA只用一份answer_units正文，内部question_items/addresses不展示、不认证语义。来源/位置记录不等于支持关系；准确回答可按需整理，只有明确证据风险走原隔离路径。
- 人工裁决、来源失效与材料换版影响原回答/摘要事实投影及知识依赖；新知识必须重新检索。原回答、旧快照、未知费用保留，不猜测修补历史记录。
- Web论文初读/重读已统一N01持久恢复，新Web聊天有独立持久协议；兼容analyze API/普通任务、对比、Skill及一次性CLI/MCP未统一。N08只恢复新目录并安排重启切换；N09同根选模，缺凭据不退mock。
- N05漏提补录/N06面板、N12材料版本和N08备份已接通；已采集漏提占比的兼容字段recall不是真实召回。分母缺失为None/n/a，不给虚假的0%或100%。
- Flash能力API优先、缺项按已确认1M/384K，只读数据且输出自动分配，思考不作证据。当前接口/参数细节见状态页与六模块，不复制历史数值作为新限制。

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

2026-10-24 本机运行时变动：上面的默认启动器所指旧 `WorkBuddy/resources/vendor/python` 失效；实际已安装运行时位于 `E:/Program Files/WorkBuddyai/resources/vendor/python/python.exe`（3.13.14）。确认该现象后可临时复用原cp313依赖，**不改全局环境、虚拟环境或目录链接**：

```powershell
Remove-Item Env:LITPRISM_HOME -ErrorAction SilentlyContinue
& 'E:/Program Files/WorkBuddyai/resources/vendor/python/python.exe' -c "import site,sys;site.addsitedir('C:/Users/18434/.workbuddy/binaries/python/envs/default/Lib/site-packages');import pytest;raise SystemExit(pytest.main(sys.argv[1:]))" tests
```

运行验收脚本时同样先 `site.addsitedir(...)`，再 `runpy.run_path('tools/check_workflows.py',run_name='__main__')`；仍须指定隔离LITPRISM_HOME和NODE_PATH。此为本机已验证的临时兼容路径，先核对文件存在和Python ABI，不擅自换成系统Python。

2026-10-28全套测试的临时目录注意：不要把pytest的`--basetemp`放在`_selftest.*`祖先内；离线回放路径守卫的负例会因此变为合法隔离路径，破坏测试前提。全套用上面的标准pytest临时目录，权限拒绝时保留日志并按获准方式执行；应用/浏览器验收及原始证据仍留独立`_selftest.*`。不要为消除此环境失败而修改数据守卫或测试断言。

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
| 语义/用量/持久运行 | `semantics.py`、`usage.py`、`analysis_runs.py`、`chat_runs.py` |
| 问答/对比/技能/上下文 | `agents.py`、`qa_retrieval.py`、`qa_evidence.py`、`qa_conditions.py`、`qa_question_items.py`、`qa_reviews.py`、`qa_unit_reviews.py`、`provenance.py`、`chat_evidence.py`、`skills/`、`context/`、`memory/` |
| API 与 UI | `web/server.py`、`web/analysis_run_routes.py`、`web/semantic_routes.py`、`web/static/` |
| 备份与恢复 | `backup.py`、`web/backup_routes.py`、`web/static/backup-ui.js` |
| 桌面与数据路径 | `sidecar.py`、`config.py`、`secrets.py`、`desktop-electron/main.js` |

以上 Python 路径相对于 `src/litprism/`；桌面入口相对于仓库根。改动后运行适当回归，提交、push、打包分别按用户授权执行。

用户交付偏好：今后每次完成任务，结尾明确提示**下一步任务**；有新反例时注明先修哪项、用离线还是实际服务验证，不只泛称“继续完善”。

## 专项验证与历史

- `npm run test:desktop`：31项Node窗口几何/文件/事件模拟，无需运行Electron。
- `tools/check_workflows.py --suite n10`：当前52组无窗口界面专项；指定隔离`LITPRISM_HOME`及本地`LITPRISM_UI_OUTPUT`。完整研究流程仍用默认suite；既往72页面组合另计，不与52组相加冒算测试项数。
- N10实机/安装边界见[专项报告](docs/N10体验与管理验收-2026-10-25.md)，N09集成边界见[专项报告](docs/N09配置与集成验收-2026-10-24.md)。旧推进记录见[归档](docs/history/N04与N10阶段推进记录-2026-10-25.md)；其中旧“下一步”不覆盖当前主计划。
