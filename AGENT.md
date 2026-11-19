# AGENT.md · LitPrism交接速查

更新：2026-11-20。先读[当前状态](docs/当前实现与验收状态.md)，文档入口见[导航](docs/README.md)。本文保留开发约定、已验证环境及铁律，逐轮实测已归[历史](docs/history/README.md)。

## 当前执行点

gemi 工作树（`E:/antigravity`）2026-11-20 三批收口：①G06 提取中候选即时持久化（`91d5e50`/`a498d6a`）+ 草稿跨刷新（`afdc8cd`/`7c40831`）；②前端现代化批次（`2cc8265`，modern-web-guidance 对照：IME 安全提交、toast 播报、首帧主题防闪烁、弹窗滚动隔离、平台 API 替换）+ `syncBatchActions` 加载竞态修复（`c2ae4b0`）；③G07 原始附件逐版本保管（`985d8f0`，schema v17：内容寻址 `data/attachments/`、上传/CLI/下载三捕获点、备份可选包含、材料列表带附件与取回路由；v17 之前历史材料不回填）。全套 pytest 1686 项（2项既有跳过）、默认完整离线工作流、N10 56组通过；未 push/打包。

G08 真实环境：恢复演练脚本 `tools/backup_drill.py` 已对主树真实库（`E:/aicoding/paper-research-agent/data`，schema v15）完成本机演练——只读在线快照、schema gate 如实拒绝直恢、演练副本经真实迁移升 v17 全链校验通过（3 论文/389 块），源库未动，报告在 `_selftest.g08-drill-20261120/`。CI 升级基线参数化为 `old_tag` 输入；多屏/真实GPU/杀软/跨机 DPAPI 列外部核对清单 [G08清单](docs/G08真实环境验收清单-2026-11-20.md)。下一个候选方向：G07 网页正文获取闭环（按实际需求）或 push/合并 main（需授权）。

N04 长上下文校准及 11 轮会话回放已提交 `f116279`。Web 首条消息保存后锁定范围，换范围从顶部按钮新建。有效历史及摘要用于追问，当前证据遵守范围和失效规则。N02/N03 本批收口包含 fastText 目标复验和 GPT‑3 v4 独立全文回放。GPT‑3 的四条定向真实复核首次通过，隔整页图表续文和同块错行继续保留为回归。后续按实际阅读和人工核对反馈处理。 [N04 长会话与上下文占用](docs/N04长会话与上下文占用验收-2026-11-18.md) · [fastText 复杂来源](docs/N02-N03复杂来源离线核对-2026-11-19.md) · [GPT‑3 独立样本](docs/N02-N03独立样本GPT3离线复验-2026-11-19.md)

codex-work 的 N02/N03 语义复核与 N04 预算收口已随 `0d93d6a` 合并进 main；两树无未合并提交。主树记载用户于11-14人工确认N02一次、无逐条存档。[渐进研究闭环](docs/渐进研究闭环架构对照与验收-2026-11-16.md)的3QA/1扩展、79837token与选择性沉淀结果保持。[N04独立Compare](docs/N04独立多篇对比验收-2026-11-16.md)两篇118块的首次4次调用/77140token及原结果保持。2026-11-18用原始响应和隔离浏览器完成C1–C4：按实际窗口安排多来源复核，送审20→31条，无原始判定的11条保持待核对；D2来源不能裁决D1；待核对/未提及与人工裁决分开，窄屏表格和预览、来源撤回409通过。全套1578项与完整无窗口通过，无新调用，不将机器记录当作准确率。日常库、旧快照和主树保持；未push或打包。[N04术语入口](docs/N04术语入口验收-2026-11-18.md)已验两篇首响（两次、36477 token）：术语列表与严格范围会话通过；按原响应和负例已离线复验术语回源、缺证状态与同全文2/4→各1次；新格式只验证定位，释义未审核；旧未完成解读按新版本绑定拒绝续跑。新格式单篇 43 块首次一调用、16399 token，BPE 英德/英法归属正确，14 条术语回源与宽窄屏通过；用同一首响离线恢复 5/7 个定位降级，补充片段显式保留，余两段注明术语关联待核对；小幅扩展范围不当作失败。定位规则变更纳入持久解读版本守卫。

产品是辅助研究工具，六字段缺项/计数提示/其他单元未审均非追问或选择性沉淀的前置闸门。以语义、对象和条件正确为裁决标准，接受忠实转述，不追求逐字相同或穷尽细节；本轮用户明确委托代理裁决，不代表产品今后默认自动可信入库。历史提取质量与本次追问补证分别记录，详见[N01报告](docs/N01短论文六字段验收-2026-10-29.md)。

G01/G02/G03/G05a守卫保持。新证据`_selftest.g04-source-fix-20261029/`、`_selftest.g04-short-source-live-20261029/`与`_selftest.g04-source-probe-20261029/`，原四轮哈希不变；协议诊断不冒充N01完整验收。partial另有计数flag，不能只解释为跳过复核。新解析才获得精确章节，旧块/历史不自动重写。缺引用重提不处理语义或传输未知，不扩大无指令兼容入口，不自动可信入库。

## 实现基线与接手要点

- Electron + Python sidecar + FastAPI/原生JS，schema v17。Tauri已退出；已发布 v0.1.1 安装包；云端 10 步验收与本地中文路径验收全部通过，仅多屏/真实GPU/杀软无法在自动环境覆盖；关联工作树不会随 Git 同步取得本地安装产物。D3、本地embedding及全量模型复核暂停，N07策略A/B已取消。
- P系列复用固定全文前缀和真实范围，超窗完整分批；目标以块/字符区间为准、章节标签仅导航。PDF书签在该物理页匹配完整标题，同页多章分别定位；未定位保留明确页级范围/告警，不猜边界，参考文献仍排除。每指令配额上限24和指令间有界概要不是完整理解证明。
- 结构走形单独重试一次并记录coverage.structure_failures；未产出uncovered_units与有产出但未过闸门flagged_units分开。不要把章节送达、条数达标或缓存命中算学术质量通过。
- 提取过程中逐指令即时更新独立ac_候选快照（带`_incomplete`标志和进度），全部提取完成后保存终态候选并写回正式分析；两类快照均绑定材料和父分析哈希。关闭或失败仍能直接人工审核；候选未审不作正式结论。
- 能力已知时语义/差异复核复用全文并按窗口批量；旧能力未知入口保留兼容约束。同字段同批/跨批检查不等于跨字段全量一致性；机器支持、人工确认和覆盖是不同状态。
- 新Web默认相关全库论文/可信知识＋实际联网摘要，选论文/分组只用该范围；空选择不限定，非法ID报错。补齐命中论文当前材料时保留知识/外部证据，完整送达不代表语义通过。
- 独立Compare按解析后的论文ID保序去重，任一非法选择报错，至少两篇不同论文；只读选中当前原文。复核前、复核后及快照保存事务内重验当前材料，旧材料仍可审计但不能据此发布新对比。复核按实际窗口安排，文献单元格的支持和反证须属于该篇；人工裁决与原机器/报告快照分开，待核对不能算未提及。
- 新QA只用一份answer_units正文，内部question_items/addresses不展示、不认证语义。来源/位置记录不等于支持关系；准确回答可按需整理，只有明确证据风险走原隔离路径。
- 人工裁决、来源失效与材料换版影响原回答/摘要事实投影及知识依赖；新知识必须重新检索。原回答、旧快照、未知费用保留，不猜测修补历史记录。
- Web论文初读/重读已统一N01持久恢复，新Web聊天有独立持久协议；兼容analyze API/普通任务、对比、Skill及一次性CLI/MCP未统一。N08只恢复新目录并安排重启切换；N09同根选模，缺凭据不退mock。
- N05漏提补录/N06面板、N12材料版本和N08备份已接通；已采集漏提占比的兼容字段recall不是真实召回。分母缺失为None/n/a，不给虚假的0%或100%。
- Flash能力API优先、缺项按已确认1M/384K，只读数据且输出自动分配，思考不作证据。当前接口/参数细节见状态页与六模块，不复制历史数值作为新限制。

## 工作树与运行隔离

主工作树是 `E:/aicoding/paper-research-agent`（`main`），关联工作树是 `E:/codex-litprism`（`codex-work`）和 `E:/antigravity`（`gemi`）。共用 Git 对象库，但工作区和索引独立；不要复制出新仓库、删除或重建关联工作树。操作前核对目标目录、分支和未提交修改，不覆盖另一工作树的新改动。

关联工作树应用数据使用 `LITPRISM_HOME=E:/codex-litprism-data`。测试使用临时库并清除测试进程的 `LITPRISM_HOME`；不能为检查配置而打开用户主库触发迁移。Key 只走 SecretStore，不进源码、文档、日志、测试夹具或提交。

## 前端约定（2026-10-28）

- **主题只有一个属性** `data-theme="paper|paper-dark|minimal|glass"`；按明暗批量命中写
  `:is([data-theme="paper-dark"], [data-theme="minimal"])`。不要再引入 `data-skin` 之类的第二维度，
  `tests/test_theme_attribute.py` 会拦。改主题走 `setTheme(id)`，入口在设置 → 通用。
- **空态不许只写文字指路**：用 `emptyCta(action, label)` 生成按钮，`data-empty-action` 委托到原控件
  （`view:<name>` 前缀表示跳转）。`tests/test_empty_states.py` 拦"点某某按钮"这类写法。
- UI 端到端验收 `tools/check_workflows.py --suite workflows`（需本机 Chrome）。playwright **不进依赖清单**，
  本地按需 `npm i --no-save playwright` 即可，换机器或重装依赖后需再执行一次；
  主题步骤断言的是主题 id，之前点 `#btnTheme` 的脚本早已失效。

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

## 辅助脚本（不常跑，但别当垃圾删）

| 脚本 | 用途 |
|---|---|
| `tools/_probe.js` | 运行时缺口探针：起 sidecar、走遍所有视图与设置面板，收集控制台报错/失败请求/未处理拒绝 |
| `tools/_shot.js`、`tools/window_shot.py` | 出图：前者抓页面，后者用 ctypes 抓桌面窗口（外壳渲染是否有内容只能靠看） |
| `tools/check_workbench.cjs` | 无窗口的前端交互验收（与 `check_workflows` 覆盖面不同，需要 playwright）。**已失真待重建**：durable-chat/搜索 mock 已部分修复（2026-11-20），但更早的界面断言（inspector 显隐等）仍与当前应用漂移，脚本跑不完；验收以 `check_workflows`/`check_n10` 为准，不要引用本脚本通过记录 |
| `tools/_empty_state_check.cjs`、`tools/_theme_check.cjs` | 空态按钮点击、四套主题明暗与分层校验（需运行中的实例与 `BASE_URL`） |

## 阅读标注

表 `annotations`（schema v16）存用户自己选的原文片段与批注：`paper_id / material_id / chunk_id / page / quote / note`，
软删除（`deleted_at`）默认不返回。接口在 `web/annotation_routes.py`：
`GET|POST /api/paper/{pid}/annotations`、`PATCH|DELETE /api/annotations/{aid}`。

- 只存用户选中/输入的文本，**不生成、不改写原文**
- 摘录与批注都有长度上限（4000 / 8000），超出截断而非报错
- 前端：阅读页选中文字 → 浮出「标注这段」；「标注」标签页汇总，可回跳原文或删除
- 导出（Markdown/CSV）尚未做，属 v2

## 取全文

arXiv 走官方直链；其余文献按顺序查**合法开放副本**：
`sources.unpaywall`（需填真实邮箱，占位邮箱跳过）→ `sources.openalex` → `sources.semantic_scholar`（可选 key）→ `sources.europepmc`（生物医学，免密钥）。
命中后把 provider / url / license / retrieved_at 写进候选 `extra.fulltext` 供审计。

- 只认直链 PDF：只有落地页时判 `no_oa`，不去抓 HTML 再猜链接
- 失败分 `no_oa` / `paywalled` / `network_error` / `no_doi`，不能把"没拿到"记成"不存在"
- 下载前校验公网 http(s)、拒绝内网地址；下载后验 `%PDF` 与 80MB 上限
- 下载件导入成功后按文件字节 sha256 归档进 `data/attachments/`（G07，绑定材料版本）；归档失败不阻断导入，结果中显式报告
- **不绕过付费墙、不使用任何账户凭据**；订阅文献由用户自行下载后导入（候选池给「打开原文」链接）
- 导入时开放副本取不回（403/HTML/解析失败）会退回题录导入；「取全文」按钮则明确报错

## 打包

打安装包只有一条命令：`python packaging/release.py`（sidecar → 冒烟 → 安装包 → 校验）。
**不要手工跑 `build.py` + `electron-builder`**——产物落后代码已经因此发生过两次；
脚本会在冒烟里确认打包件建出的库是当前 schema，并校验安装包内 sidecar 与 `packaging/dist` 哈希相同。

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
- `tools/check_workflows.py --suite n10`：当前56组无窗口界面专项；指定隔离`LITPRISM_HOME`及本地`LITPRISM_UI_OUTPUT`。完整研究流程仍用默认suite；既往72页面组合另计，不与56组相加冒算测试项数。
- N10实机/安装边界见[专项报告](docs/N10体验与管理验收-2026-10-25.md)，N09集成边界见[专项报告](docs/N09配置与集成验收-2026-10-24.md)。旧推进记录见[归档](docs/history/N04与N10阶段推进记录-2026-10-25.md)；其中旧“下一步”不覆盖当前主计划。
