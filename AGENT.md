# AGENT.md · LitPrism 交接速查

更新：2026-10-25。本文只保留开发接手约定；当前行为与验收见 [当前状态](docs/当前实现与验收状态.md)，原需求、阶段映射与 N01–N12 见 [实施计划](docs/实现计划.md)，其余见 [文档导航](docs/README.md)。历史进度不在这里重复维护。

## 当前执行点（2026-10-25）

默认检索/材料补齐和N10累计改动已完成回归。最新真实x1/x2各首次1QA、严格118/140块、零搜索/扩展/重试，共99464已知token；x1仍漏答Transformer下一词训练目标，x2主要对比有据但保留局部归属措辞，旧固定特征细节未再触发不代表根治。整体语义未通过，准确正文不因此强制人审；原生成和失败不回写。

提交前1407项Python、完整无窗口工作流、13组N10和31项Node模拟通过，计数分开。本轮用户已授权一次累计提交，不push/打包；_selftest、原PDF/模型响应/库及未跟踪src-tauri不纳入。真实数据仅隔离副本，日常库未动。下一步按[唯一实施计划](docs/实现计划.md)先离线固化比较维度漏答与归属措辞例子，再决定小样本真实复验；不加全量模型审核或embedding。证据与历史统一见[当前状态](docs/当前实现与验收状态.md)、[最新实测](docs/N04多轮追问与受控压缩验收-2026-10-24.md#x1x2跨论文首次真实复验与提交核验)。

## 当前基线

- 路线：Electron + Python sidecar + FastAPI/原生 JS；**Tauri 工程已于 2026-10-19 删除**（本机 Chromium GPU 崩溃后退场），electron-builder 打包已跑通但安装验收（N11）未做。当前 schema **v15**（v15知识依赖/本轮外部证据授权；v14持久对话请求/调用记录；v12 材料快照+块/分析版本绑定；v13 章节保留原文标题并新增 `chunks.section_role`）；**覆盖口径按章节**：`extract.section_claim_counts()` 把陈述引用的块归到章节（参考文献不计），`complete` 要求"每节至少一条陈述 + 无零产出 + 无未过闸门"；**不要用"某条指令的块有没有被引用"判覆盖**——whole 模式允许跨节取证，那样会把"跨节概括"误判成漏读。结构损坏（未知字段/JSON 走形）单独重试一次，原始响应 2KB 记入 `coverage.structure_failures`。
解读按**章节分步**：章节优先读 PDF 书签（`ingest.pdf_outline`），单条指令的陈述配额上限 `MAX_CLAIMS_PER_DIRECTIVE=24`，指令间用**有界概要**（`extract.bounded_brief`，只回放"读过哪些节、各出几条"，不回放原文）。最新测试及无窗口验收结果统一见当前状态页。
- N05 漏提补录与 N06 质量面板已实现（2026-10-19）：右栏「补一条」必须绑原文原句；`GET /api/paper/{pid}/quality` 分字段给出已审/接受率口径的精度/可用率/复核覆盖/已采集漏提占比，与 `tools/eval_extraction.py` 共用 `litprism.quality` 聚合，**同一份数据只允许一个数字**。旧接口键 `recall` 表示已采集漏提占比，不是真实召回率。比例分母为 0 一律 `None`（界面 n/a），不显示 0%。
- 当前增量包含长文计划真实范围、完整请求预算、全阶段用量，以及单篇 N01 持久运行与恢复（阶段调用预算已取消）。详见 [N01 契约](docs/阶段预算与可续验进度-2026-10-18.md)。新 Web QA 已接持久请求与显式续验；旧“重新解读”、对比、旧QA/API、Skill、CLI/MCP尚未统一接入持久续跑。
- 机器支持、人工确认、原文覆盖是不同状态。真实固定样本结果有限；fastText、GPT-3、RMSNorm 的不同版本/片段/回放不能拼成当前整篇长论文质量通过。
- N02 已完成首轮受控实测：[协议与结果](docs/N02真实链路验收.md)。18 个正反例和 5 个差异控制通过；两轮论文仍有提取配额、输出截断/复核熔断和跨页来源误判，不能称整篇通过。本轮另做 N04 固定短论文/对话验收，不改变 N02 整篇未通过结论；原反例与失败记录仍保留。
- 现行/修订目标分开：见 [当前设计与流程图](docs/当前设计与功能流程图.md)。目标为可选全文批量辅助复核、人工可直接审核；能力缺失不再静默：未选模型时真实入口直接报错；`active_limits()` 返回 `capability_missing`，运行记录顶部提示；选模后自动刷新一次能力目录。
候选陈述使用确定性 ID 放入 `_candidates`；2026-10-22 已在提取完成、机器复核前写入独立 `ac_` 候选快照（复用 analysis_versions，无 schema 升级），不覆盖旧正式解读。`semantic_review=false` 或后续复核失败时可直接在右栏核对、修正、确认入库/撤销；候选未经审核不作为正式结论或正常笔记导出。快照绑定材料和父分析哈希，材料/分析变化不自动沿用审核。不是每条指令完成即落库，提取尚未完成时仍无本轮候选快照。
复核层在能力已知时已按窗口批量并复用提取全文前缀（240 条合成数据：240 次 → 1 次），能力未知入口仍为逐条小来源；差异层同轮接线（240 条 → 6 次或 1 次，覆盖完整）。候选协作闭环的验证独立于批量调用；复杂来源质量仍须另验。待审隔离和来源放行规则继续生效。
- Flash 能力默认 1M/384K，API 能力优先，数据只读、任务输出自动分配；保留 P 系列规划。思考只作会话展示，不入证据；见 [能力与展示](docs/模型能力与思考展示-2026-10-19.md)。
- N01、N02 验收工具、能力数据、思考展示与六模块文档已纳入当前源码；分支同步和提交状态以 Git 为准。F3、打包、D3 继续暂停，不增加本地 embedding、模型或向量库。新的提交、push、打包仍分别按用户授权执行。

功能职责见 [六模块导航](docs/modules/README.md)：M01 来源发现 → M02 论文/网页资料入库 → M03 阅读与审核 → M04 问答研究 → M05 知识产出，M06 为运行基础。论文库可以只有题录/摘要，搜索摘要不等于正文；Unpaywall/SearXNG 仅有配置/设想，不能计为已接通来源。统一候选池、arXiv 自动取全文、本地上传与完整度已接通；普通网页正文抓取及任意出版商取全文未实现。

N12 已实现并通过离线验收：重复导入生成材料快照，默认只读/检索 current，旧块 ID/审核哈希不重写；重复相同材料幂等，低完整度材料不替换当前全文。旧分析可查看但不能当作当前原文自动复用或导出。旧库全部已有块只保守归为 legacy 快照，不猜测历史混入版本。详见 [N12 契约](docs/N12原文材料版本化-2026-10-20.md)。

N08 已接通备份/预检/新目录恢复与迁移/重启切换及取消，见 [N08 契约](docs/N08备份恢复与目录迁移-2026-10-20.md)。N12/N08 已纳入当前源码，提交与同步状态以 Git 为准；最新全量 **1407 项通过**（本地真实 PDF 已执行，无 skipped），本轮无窗口完整研究工作流通过，包括来源/审核/撤销与持久恢复。客户端真实生成实验、操作者对照、UI桩验收和学术语义质量分别报告，不混作通过。日常库仅只读盘点，真实导入/解读与 chat 均使用隔离验收根；未迁移用户库、未打包。N07 策略 A/B 已取消（分批只作自动兜底）。

N04 当前修复：Web 与验收工具共用 build_active_llm；v3 按实际引用统计，范围有效性与回答可用性分开。显式论文范围、能力已知且装得下时优先完整材料；默认先检索相关论文，再在实际窗口内补齐命中论文当前材料/章节，保留知识和外部证据；否则按片段/章节/概览/逐篇取证，追问恢复实际来源句柄。有限否定/表格归属风险进入 needs_review，安全正文与有效来源链接可重开，草稿不入事实历史。原四篇24题基线仍保留：13满足/9失败/1服务错误/1待核。新一轮同四篇同24题：192/192引用有效，18满足/1明确错误/3待核/2服务失败，整体仍未通过；章节号误拦已离线修复，不回写实测。新Transformer partial，49候选/43机器支持/6待审，运行中确认候选提前落库。新轮共35调用、548564已知token、2未知用量，未反复补跑；见 [新轮报告](docs/N04反例修复与复验-2026-10-22.md)。后续已补程序送达记录、限定年份/百分比条件配对（含显式表格）、虚构解析告警/材料范围隔离及聚焦回答。22旧稿只隔离b1/x2/x4，未误拦f2。另六题定向实测为5answered/1显式错页被阻止，6调用153949token、无服务失败；不能代替24题通过。实测后QA提示只生成[P#]，物理页由来源链接/右栏展示，显式错页仍拦截；当时该提示仅离线/UI验证；后续2题格式探针27/27引用有效，1answered/1规则误拦待审，保留实测，括号条件误拦仅离线修正。保留人工路径，服务稳定性与任意条件语义另验，不增语料或embedding。

N04 对话人工审核已接通：新 Web 回复保存 `qr_` 不可变快照，绑定本轮实际送达来源、材料版本和内容哈希。回复后链接进入右栏核对草稿/选择原文/修订正文/确认入库/拒绝/撤销；错页、未知编号及来源失效不能直接确认。审核功能复用 semantic_runs/reviews（当前整体schema v15），通用对比审核接口不能绕过专用校验。人工裁决后原回复仅作审计，历史事实投影与覆盖它的旧摘要均排除，修订知识需重新检索。已有历史回复不猜测补齐当时未保存的来源快照。新增24条审核回归与4条条件表达正反例、完整1112项和无窗口工作流通过；真实质量另记，不等同全部语义已通过。

N04 新Web聊天入口已接持久请求（schema v14，chat_runs/calls），先登记调用、先保存响应，再原子发布回复及审核快照。刷新/重启不自动重发；失败或结果未知须明确重试，精确成功响应复用；结束请求不抹除未知费用。恢复绑定来源/材料/历史/偏好/审核/模型与实现，变化要求新问题。旧聊天API不自动获得恢复。v15备份要求匹配schema，v14/v13归档先用匹配版本恢复再迁移。完整契约见[对话持久化与续验](docs/N04对话请求持久化与续验-2026-10-23.md)；本轮无新增真实付费调用、未操作日常库。

N04冻结版原24题（2026-10-23）已通过新持久Web入口执行完毕：19answered/4refused/1连接失败，192/192实际引用有效、23/23有引用题范围正确。离线原文要点核对21满足/2待核/1服务失败，整体仍未通过：x1过度判为缺少下一词目标信息；x2以相关工作来源支撑BERT固定特征归属。f4为connect_timeout，保留首次失败/未知用量，未重试。24逻辑调用，471728已知token、1未知用量；来源/材料边界在独立备份零模型调用通过。收尾另修状态DTO读取竞态：先取活跃标志再取持久快照，避免旧状态与新inactive拼接；确定性旧实现反例失败、新实现通过，未追加真实请求。后续先修陈述-对象-直接来源对应与缺失信息措辞，不扩大语料/正则或补跑挑结果。见[冻结版报告](docs/N04冻结版24题验收-2026-10-23.md)。

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
| 问答/对比/技能/上下文 | `agents.py`、`qa_retrieval.py`、`qa_evidence.py`、`qa_conditions.py`、`qa_reviews.py`、`qa_unit_reviews.py`、`provenance.py`、`chat_evidence.py`、`skills/`、`context/`、`memory/` |
| API 与 UI | `web/server.py`、`web/analysis_run_routes.py`、`web/semantic_routes.py`、`web/static/` |
| 备份与恢复 | `backup.py`、`web/backup_routes.py`、`web/static/backup-ui.js` |
| 桌面与数据路径 | `sidecar.py`、`config.py`、`secrets.py`、`desktop-electron/main.js` |

以上 Python 路径相对于 `src/litprism/`；桌面入口相对于仓库根。改动后运行适当回归，提交、push、打包分别按用户授权执行。

用户交付偏好：今后每次完成任务，结尾明确提示**下一步任务**；有新反例时注明先修哪项、用离线还是实际服务验证，不只泛称“继续完善”。

## 专项验证与历史

- `npm run test:desktop`：31项Node窗口几何/文件/事件模拟，无需运行Electron。
- `tools/check_workflows.py --suite n10`：13组无窗口界面专项；指定隔离`LITPRISM_HOME`及本地`LITPRISM_UI_OUTPUT`。完整研究流程仍使用默认suite。
- N10实机/安装边界见[专项报告](docs/N10体验与管理验收-2026-10-25.md)，N09集成边界见[专项报告](docs/N09配置与集成验收-2026-10-24.md)。旧推进记录见[归档](docs/history/N04与N10阶段推进记录-2026-10-25.md)；其中旧“下一步”不覆盖当前主计划。
