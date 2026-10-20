# 文献棱镜 LitPrism

> 面向个人学术研究的**论文知识解读 Agent** —— 把 PDF 论文变成结构化知识，
> 用权威数据库核验每一篇文献的真实性，并支持问答、对比与推荐。

```
文件 / 学术候选 ──▶ 解析与真实性核验 ──▶ 论文材料库 ──▶ 结构化解读
                                             │               │
网页摘要 / 手工知识 ──▶ 待审来源快照 ──────────┼──▶ 人工核对与知识确认
                                             │               │
                          带来源问答 / 跨论文研究 ◀── 可信知识快照

材料与知识均保留来源、版本和审核状态；备份恢复不改变可信状态。
```

当前以 **Electron + Python sidecar** 为桌面路线，支持回复来源链接、右栏原文和人工审核。源码已经具备完整原文计划、有界语义复核与用量统计，但**尚未完成真实长论文的整体自动质量验收**。

当前 schema v12；当前源码包含 N01/N02、人工补录与质量面板（N05/N06）、材料版本化（N12）、备份恢复（N08）、模型能力与思考展示；实际提交与同步状态以 Git 为准。N02 固定控制集通过，整篇论文质量仍未通过；最新回归及用量见 [当前实现与验收状态](docs/当前实现与验收状态.md)。尚未重新打包，旧 exe 不含最新改动；F3 与本地 embedding 暂停，入口见 [文档导航](docs/README.md)。

备份入口在 **设置 → 备份与恢复**：先下载或预检，再恢复到新目录，校验通过后可安排重启切换；原目录保留、密钥需重新配置。范围及 CLI 命令见 [N08 使用说明](docs/N08备份恢复与目录迁移-2026-10-20.md)。

最初需求、方案变化与剩余功能计划见 [设计与功能实施总计划](docs/实现计划.md)，区分已有实现、待验收、未实现和暂停方向；列入计划不表示当前开始开发。

---

## 为什么需要它

通用大模型解读论文有三个反复出现的坑：

| 问题 | 本项目的做法 |
|---|---|
| **编造文献**：凭空生成不存在的论文标题、作者、DOI | 使用 **Crossref / OpenAlex / arXiv 等来源**检索核验；只有自动核验通过或记录依据的人工确认才可导入、引用 |
| **无依据发挥**：答案超出论文实际内容 | 问答只基于检索到的原文片段；片段不足直接拒答；回答中的 `[P1 p.3]` 引用会**逐条回溯校验** |
| **过度简化 / 主观臆断**：把作者主张说成定论 | 解读与对比增加有界陈述复核，待审/不符项保留来源，不进入正常结论；机器支持仍不等于人工确认 |

---

## 快速开始

本项目在 Windows 工作树中运行时，先设置独立数据根。以下命令使用本机指定环境，不会打开桌面窗口；导入和核验可能访问网络，真实模型解读会计费。

```powershell
$py = 'C:/Users/18434/.workbuddy/binaries/python/envs/default/Scripts/python.exe'
$env:LITPRISM_HOME = 'E:/codex-litprism-data'

# 首次安装依赖（已有环境可跳过）
& $py -m pip install -r requirements.txt
& $py run.py init

# 导入时强制核验；--verify 保留为显式用法，不能省略该标记来绕过守卫
& $py run.py add sample_papers/ --verify
& $py run.py ask '这些论文在方法论上的核心差异是什么？'
& $py run.py compare 'Attention Is All You Need' 'BERT'

# 启动本地 Web 服务；不会自动打开浏览器
& $py run.py serve
```

未配置真实模型时，推理使用明确标注的 **mock**，语义复核标记未执行；配置真实 Key 也不等于论文质量自动验收通过。真实性核验等网络能力不会因为使用 mock 就自动改成离线。

桌面源码与 Web 服务共用界面。当前不重新打包；本地 API 验证与鉴权说明见 [AGENTS.md](AGENTS.md)。

---

当前功能按 [六个模块](docs/modules/README.md)整理；来源发现、论文/网页资料入库、知识沉淀是不同阶段，整体流程及与原设计/P 系列的差异见 [设计总览](docs/当前设计与功能流程图.md)。其中人工与可选全文批量机器复核是修订目标，尚未全部落地。

## 配置

配置模板是 `config/config.example.yaml`；首次运行会在数据根生成 `config/config.yaml`。设置 `LITPRISM_HOME` 后配置与数据随该根隔离，不要误改主工作树的配置。

**桌面/Web 模型设置**支持提供商、模型选择和 API 协议选择：OpenAI Chat Completions / Anthropic Messages。密钥通过 SecretStore 加密留存，数据库只存引用（若平台降级为弱存储会明确提示，不能将 Base64 混淆称作加密）；当前选中配置无效会报错，不静默改用 mock。

模型能力只读展示，不提供窗口或任务输出上限的编辑控件。获取模型列表时读取并缓存 API 能力，缺项使用已确认默认值。当前 Command Code Flash 的 API 返回 1M 上下文，未返回最大输出；按用户确认使用 384K 最大输出，提取、复核和问答自动分配额度，推理也占输出。聊天可折叠查看服务独立返回的思考内容，尚非流式。详见 [能力数据与展示边界](docs/模型能力与思考展示-2026-10-19.md)。

**CLI 配置**仍使用 YAML / 环境变量；不会保证自动沿用桌面 ProviderStore 的选模。下面是已有配置结构示例（不是服务能力或模型推荐）：

```yaml
llm:
  provider: openai            # openai | deepseek | ollama | mock
  base_url: https://api.openai.com/v1
  api_key: ""                 # 建议用环境变量 LITPRISM_API_KEY，不要写进文件
  model: gpt-4o-mini
  temperature: 0.1            # 学术场景建议低温
```

支持的自动探测环境变量：`OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` /
`MOONSHOT_API_KEY` / `ZHIPU_API_KEY`。代码仍保留 `provider: ollama` 的兼容入口；这不表示本轮安装了本地模型或 embedding，当前不推进该部署。

**核验数据源**（`sources` 段）：

下表描述当前示例配置，不保证服务最新账户政策或在线可用性；实际接入需按服务要求配置。

| 数据源 | 本项目默认配置 | 用途 |
|---|---|---|
| Crossref | 启用；可填 `mailto` | DOI 与题录检索 |
| OpenAlex | 启用；可填 `mailto` | 学术题录、相关/引用线索 |
| arXiv | 启用 | 预印本与摘要 |
| Semantic Scholar | 默认关闭；可配置 API Key | 补充学术来源 |

---

## 命令一览

| 命令 | 作用 |
|---|---|
| `init` | 初始化并查看知识库状态、模型、数据源 |
| `add <路径...>` | 导入 PDF/DOCX/HTML/TXT/MD/TeX，可加 `--verify` 核验、`--no-analyze` 跳过解读 |
| `list` | 列出论文（含核验状态） |
| `show <论文>` | 查看某篇论文的结构化解读 |
| `analyze <论文> \| --all` | 生成结构化解读（`--force` 忽略缓存） |
| `search <关键词>` | 检索本地库；`--external` 同时检索权威数据库 |
| `ask <问题>` | 针对论文内容提问（`-p` 限定论文，`--show-evidence` 打印原文片段） |
| `compare <论文A> <论文B> ...` | 多篇论文对比 |
| `recommend` | 推荐文献（`--topic` 方向 / `--paper` 种子论文 / `--import` 导入） |
| `verify [标题] --doi <DOI>` | 核验一篇论文是否真实存在 |
| `import [标题] --doi <DOI>` | 核验通过后导入本地库 |
| `concepts` | 查看专业术语表 |
| `relations` | 跨论文关联分析（相似度 + 主题簇） |
| `export` | 导出 Markdown 解读笔记 |
| `stats` / `config` | 知识库统计 / 当前生效配置 |
| `serve` | 启动 Web 界面 |
| `backup create/inspect/restore/migrate/activate` | 一致备份、预检、新目录恢复与迁移、安排重启切换；见 N08 使用说明 |

论文可以用 **id / id 前缀 / DOI / 标题片段** 任意一种方式引用。

---

## 防幻觉机制（三层）

### 第 1 层 · 文献真实性核验

`verify.py` 对同一篇论文在多个权威数据库并发检索，按标题相似度 + 作者/年份佐证给出判定：

| 判定 | 含义 | 是否可引用 |
|---|---|---|
| ✅ `verified` | 相似度 ≥ 0.92 或 DOI 精确命中 | 可以 |
| ⚠️ `likely` | 相似度 ∈ [0.82, 0.92) | 需人工确认 |
| ❌ `not_found` | 数据库正常响应但无匹配 | **禁止**（但不等于"该文献是编造的"） |
| ❔ `unchecked` | 未核验或数据源请求失败 | **禁止**（绝不误判为"不存在"） |
| 🔵 `manual_confirmed` | 用户填写依据后人工放行，保留原自动结果 | 可以，但明确显示为人工确认 |

两个关键设计：

- **区分 `not_found` 与 `unchecked`**。网络故障时若返回 `not_found`，会把真实论文误标成虚构，
  这是比"漏检"更严重的错误。
- **标题相似度对词序敏感**。`"Attention Is All You Need"` 与 `"Is Attention All You Need?"`
  得分 0.91（落入 `likely`）而不是 1.00，避免把同名异文当成同一篇。

### 第 2 层 · 检索增强问答 + 引用回溯

- 只把检索到的原文片段交给模型，提示词禁止使用片段外知识；
- 无召回时**直接拒答**，而不是让模型"合理推测"；
- 回答产出后，`parse_citations()` 逐条解析 `[P1 p.3]` 并校验：
  - 引用的片段编号是否真实存在（防"引用 [P9] 但只给了 5 个片段"）；
  - 标注页码是否与该片段实际页码一致（防"页码随口编"）。

人工可信知识还有独立来源状态：网页搜索摘要与手动知识先为候选，右栏检查后填写依据才能确认；撤销后不再召回。回复链接保存稳定来源 ID，不沿用其他轮次的 P 编号。长论文的原文送达覆盖、引用机械检查、机器语义支持和人工确认分开显示。

### 第 3 层 · 导入守卫 + 留痕

- CLI、Web 上传与题录导入只接受 `verified` / `manual_confirmed`，`likely` 也需先人工确认；
  防止污染后续检索；
- 每一次核验结果都写入 `verify_log` 表，可审计、可复现。

---

## 项目结构

```
paper-research-agent/
├── run.py                      便捷启动器（免设 PYTHONPATH）
├── requirements.txt / pyproject.toml
├── config/
│   ├── config.example.yaml     全部可配置项及说明
│   └── prompts/                提示词模板（另见 src/litprism/prompts.py）
├── src/litprism/
│   ├── config.py               配置加载（YAML + 环境变量 + 默认值）
│   ├── textutil.py             标题归一化/相似度、CJK 分词、FTS 查询、引用解析
│   ├── query.py                查询扩展（中英学术词表 + LLM 改写）
│   ├── db.py / materials.py    SQLite 知识库与当前/历史材料、分析版本
│   ├── backup.py               在线备份、清单校验、新目录恢复与重启切换
│   ├── ingest.py               PDF·DOCX·HTML·TXT·MD·TeX 解析 → 章节识别 → 分块
│   ├── enrich.py               题录用权威数据库校正
│   ├── llm.py                  OpenAI 兼容 / Anthropic / 兼容 Ollama / mock 客户端
│   ├── prompts.py              全部提示词（学术规范 + 防幻觉约束）
│   ├── extract.py              六字段解读、提取闸门、有界修复与保存
│   ├── reading.py / plan.py     固定原文前缀、真实范围指令及预算分批
│   ├── semantics.py / gates.py  陈述复核、同字段差异、机械闸门
│   ├── evidence.py / knowledge.py  来源与人工审核/撤销
│   ├── usage.py / tasks.py     用量归集、任务轮询及协作取消
│   ├── verify.py               ★ 论文真实性核验（多源交叉验证）
│   ├── search.py               本地 BM25 检索 + 外部检索 + 导入
│   ├── relations.py            跨论文关联（TF-IDF 余弦 + 主题簇）
│   ├── agents.py               ★ 问答 / 对比 / 推荐 三个 Agent
│   ├── cli.py                  命令行
│   └── web/                    FastAPI + 多模块原生 JS/CSS 前端
├── tests/                      完整离线回归；最新结果见验收状态
├── docs/                       设计文档
├── sample_papers/              示例材料说明（不保证本机已下载 PDF）
└── data/                       运行时生成：知识库、缓存、导出笔记
```

---

## 测试

```powershell
Remove-Item Env:LITPRISM_HOME -ErrorAction SilentlyContinue
& 'C:/Users/18434/.workbuddy/binaries/python/envs/default/Scripts/python.exe' -m pytest tests
```

`pyproject.toml` 已配置 `-q`，无需重复添加。应用运行前重新设置独立 `LITPRISM_HOME`；不要设置全局环境或共用主库来验证。

回归覆盖核验、引用、范围、计划原文区间、完整请求预算、截断与用量、持久化、导出、人工修正与撤销。`tools/check_workflows.py` 使用临时库和无窗口浏览器验证真实本地 API/UI。详细工具与证据边界见 [当前实现与验收状态](docs/当前实现与验收状态.md)。纯文档整理不重新计为一次测试通过。

---

## 已知局限

- **扫描版 PDF** 无文本层时无法完成当前文本解读；转写能力暂缓，不把零散识别当作可靠正文。
- **长论文质量**未完成整体自动验收。全文可见和缓存命中不能保证模型不漏读；单条/跨批预算不足时保留待审。可用“分阶段解读”查看持久运行记录并人工恢复，阶段调用上限已取消；重启不会自动发请求。旧入口未接入持久续跑，未上报费用仍未知。详见 [阶段预算与恢复](docs/阶段预算与可续验进度-2026-10-18.md)。
- **表格与页码**采用保守恢复和物理页定位；复杂表格会标待核对。旧 PDF/分析需要重导入或重跑，不会自动重写旧来源。
- **管理与集成**已有嵌套分组、批量操作、会话管理和产出编辑；N08 已接一致备份、新目录恢复与重启切换；流式输出、完整 MCP 实测、普通网页正文与非 arXiv 自动全文获取仍未完成。
- **评测**已有漏提补录和质量面板；已采集漏提占比不是真实召回率，完整阅读标注仍待补。固定正反例和单篇实验不代表一般准确率。
- **跨语言检索**依赖内置词表（约 200 组中英学术术语）+ 可选 LLM 改写；
  领域非常冷僻的术语可能召回不足，此时建议用论文原文的语言提问。
- **`not_found` 不等于"编造"**：新预印本、小众期刊、非英文文献可能尚未被收录，
  系统一律要求人工复核而非自动判定。
- 章节识别基于标题词典，非标准结构的论文（如无小标题的短文）会退化为单节。
- 跨论文关联使用 TF-IDF 词法相似度，只是**线索**；实质性关系判断请用 `compare`。

---

## 许可

MIT
