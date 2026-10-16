# 文献棱镜 LitPrism

> 面向个人学术研究的**论文知识解读 Agent** —— 把 PDF 论文变成结构化知识，
> 用权威数据库核验每一篇文献的真实性，并支持问答、对比与推荐。

```
导入 PDF ──▶ 解析/章节识别 ──▶ 题录补全 ──▶ 真实性核验 ──▶ 结构化解读 ──▶ 知识库
                                                                          │
                              问答（带引用）◀── 检索 ◀──────────────────────┤
                              多篇对比        ◀── 结构化字段 ───────────────┤
                              文献推荐        ◀── 外部数据库检索 + 核验 ────┘
```

---

## 为什么需要它

通用大模型解读论文有三个反复出现的坑：

| 问题 | 本项目的做法 |
|---|---|
| **编造文献**：凭空生成不存在的论文标题、作者、DOI | 任何外部文献必须先通过 **Crossref / OpenAlex / arXiv 多源交叉核验**；未核到的一律标注并**拒绝导入本地库** |
| **无依据发挥**：答案超出论文实际内容 | 问答只基于检索到的原文片段；片段不足直接拒答；回答中的 `[P1 p.3]` 引用会**逐条回溯校验** |
| **过度简化 / 主观臆断**：把作者主张说成定论 | 提示词强制区分"原文事实 / 作者主张 / 模型推断"；缺失字段留空并说明，不做补全 |

---

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
python -m pip install -r requirements.txt
# 若默认源不可用： python -m pip install -r requirements.txt -i https://pypi.org/simple/

# 2. 初始化（查看状态、数据源、模型）
python run.py init

# 3. 导入论文（自动解析 → 题录补全 → 真实性核验 → 结构化解读）
python run.py add sample_papers/ --verify

# 4. 提问（中英文都可以）
python run.py ask "这些论文在方法论上的核心差异是什么？"

# 5. 跨论文对比
python run.py compare "Attention Is All You Need" "BERT"

# 6. 按研究方向推荐文献（结果全部经核验，可一键导入本地库）
python run.py recommend --topic "large language model in-context learning" --import

# 7. 浏览器界面
python run.py serve        # http://127.0.0.1:8765
```

> 未配置大模型 API Key 时，系统自动使用**离线占位模型（mock）**跑通全流程，
> 输出会明确标注 `mock`，不会冒充真实模型结论。配置 Key 后即可获得真正的语义解读。

---

## 配置

复制 `config/config.example.yaml` 为 `config/config.yaml` 后按需修改。

**接入大模型**（三选一，或用环境变量自动探测）：

```yaml
llm:
  provider: openai            # openai | deepseek | ollama | mock
  base_url: https://api.openai.com/v1
  api_key: ""                 # 建议用环境变量 LITPRISM_API_KEY，不要写进文件
  model: gpt-4o-mini
  temperature: 0.1            # 学术场景建议低温
```

支持的自动探测环境变量：`OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` /
`MOONSHOT_API_KEY` / `ZHIPU_API_KEY`。本地模型可用 `provider: ollama`（无需 Key、无需联网）。

**核验数据源**（`sources` 段）：

| 数据源 | 是否需要 Key | 说明 |
|---|---|---|
| Crossref | 否（建议填 `mailto`） | 覆盖最广的 DOI 注册机构，期刊论文首选 |
| OpenAlex | 否（建议填 `mailto`） | 开放学术图谱，提供 related_works / citing 用于推荐 |
| arXiv | 否 | 预印本，含摘要 |
| Semantic Scholar | 需要免费 Key | 补充来源，默认关闭 |

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
| ❔ `unchecked` | 数据源全部请求失败 | **禁止**（绝不误判为"不存在"） |

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

### 第 3 层 · 导入守卫 + 全量留痕

- `import_record()` 拒绝 `not_found` / `unchecked` 的文献进入本地知识库，
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
│   ├── db.py                   SQLite 知识库（论文/分块/FTS5/解读/术语/关联/核验日志）
│   ├── ingest.py               PDF·DOCX·HTML·TXT·MD·TeX 解析 → 章节识别 → 分块
│   ├── enrich.py               题录用权威数据库校正
│   ├── llm.py                  OpenAI 兼容 / Ollama / 离线 mock 客户端
│   ├── prompts.py              全部提示词（学术规范 + 防幻觉约束）
│   ├── extract.py              结构化解读（研究问题/方法/实验/结论/创新点/局限/术语）
│   ├── verify.py               ★ 论文真实性核验（多源交叉验证）
│   ├── search.py               本地 BM25 检索 + 外部检索 + 导入
│   ├── relations.py            跨论文关联（TF-IDF 余弦 + 主题簇）
│   ├── agents.py               ★ 问答 / 对比 / 推荐 三个 Agent
│   ├── cli.py                  命令行
│   └── web/                    FastAPI + 单文件前端
├── tests/test_core.py          49 个测试，聚焦防幻觉与解析正确性
├── docs/                       设计文档
├── sample_papers/              示例论文（3 篇真实 arXiv 论文）
└── data/                       运行时生成：知识库、缓存、导出笔记
```

---

## 测试

```bash
python -m pytest tests -v
```

测试覆盖：标题相似度（含词序敏感性）、引用解析、章节识别与合并、分块页码准确性、
知识库去重与级联删除、**核验四态判定（含网络故障不误判）**、
**虚构引用检测**、**未核验导入拒绝**、跨论文关联。

---

## 已知局限

- **扫描版 PDF** 无文本层，需先 OCR；导入时会给出明确警告。
- **跨语言检索**依赖内置词表（约 200 组中英学术术语）+ 可选 LLM 改写；
  领域非常冷僻的术语可能召回不足，此时建议用论文原文的语言提问。
- **`not_found` 不等于"编造"**：新预印本、小众期刊、非英文文献可能尚未被收录，
  系统一律要求人工复核而非自动判定。
- 章节识别基于标题词典，非标准结构的论文（如无小标题的短文）会退化为单节。
- 跨论文关联使用 TF-IDF 词法相似度，只是**线索**；实质性关系判断请用 `compare`。

---

## 许可

MIT
