"""查询扩展：让中文提问能命中英文文献，反之亦然。

BM25 是词法检索，中文问题直接去匹配英文正文必然全军覆没。本模块提供两层扩展：

1. **内置中英学术词表**（确定性、零成本）：把查询里的中英术语互相翻译，
   例如"方法论" → ``method / methodology / approach``，"few-shot" → ``少样本 / 小样本``。
2. **LLM 查询改写**（可选）：让模型把问题改写成 5~10 个检索关键词，
   覆盖同义词与领域惯用表达。失败时静默回退到第 1 层。

扩展后的多个查询用 OR 组合成一条 FTS5 表达式，由 BM25 排序决定相关性。
"""

from __future__ import annotations

import re
from itertools import zip_longest

from .textutil import build_fts_query

# ---------------------------------------------------------------------------
# 中英学术术语对照表（用于跨语言检索扩展）
# ---------------------------------------------------------------------------
GLOSSARY: dict[str, list[str]] = {
    # 研究元概念
    "方法论": ["method", "methodology", "approach"],
    "方法": ["method", "approach", "methodology"],
    "研究问题": ["research question", "problem statement", "problem"],
    "创新": ["contribution", "novel", "innovation", "novelty"],
    "创新点": ["contribution", "novelty", "key contribution"],
    "贡献": ["contribution", "contribute"],
    "结论": ["conclusion", "conclude", "finding"],
    "局限": ["limitation", "limitations", "future work"],
    "局限与未来工作": ["limitation", "future work"],
    "实验": ["experiment", "experimental", "evaluation"],
    "实验设计": ["experimental setup", "experiment design", "evaluation setup"],
    "数据集": ["dataset", "datasets", "corpus"],
    "基线": ["baseline", "baselines"],
    "评估": ["evaluation", "evaluate", "assessment"],
    "评价指标": ["metric", "evaluation metric"],
    "指标": ["metric", "metrics"],
    "结果": ["result", "results", "finding"],
    "消融": ["ablation", "ablation study"],
    "对比": ["comparison", "compare", "contrast"],
    "差异": ["difference", "differ", "distinction"],
    "相同点": ["similarity", "common", "consistency"],
    "核心": ["key", "core", "main", "central"],
    "主要": ["main", "primary", "major"],
    "综述": ["survey", "review", "overview"],
    "相关工作": ["related work", "prior work", "background"],
    "局限": ["limitation", "limitations"],
    "论文": ["paper", "article", "work"],
    "文献": ["literature", "reference"],
    "作者": ["author", "authors"],
    "引用": ["citation", "cite", "reference"],
    "发表": ["publish", "publication"],
    # 方法与模型
    "模型": ["model", "models"],
    "架构": ["architecture", "framework", "structure"],
    "框架": ["framework", "architecture"],
    "算法": ["algorithm", "algorithms"],
    "网络": ["network", "networks"],
    "神经网络": ["neural network", "neural networks"],
    "深度学习": ["deep learning"],
    "机器学习": ["machine learning"],
    "强化学习": ["reinforcement learning"],
    "迁移学习": ["transfer learning"],
    "对比学习": ["contrastive learning"],
    "自监督": ["self-supervised", "self supervision"],
    "无监督": ["unsupervised"],
    "监督学习": ["supervised learning"],
    "半监督": ["semi-supervised"],
    "知识蒸馏": ["knowledge distillation"],
    "注意力": ["attention", "self-attention"],
    "自注意力": ["self-attention", "attention"],
    "编码器": ["encoder"],
    "解码器": ["decoder"],
    "卷积": ["convolution", "convolutional"],
    "循环神经网络": ["recurrent", "rnn", "lstm"],
    "变换器": ["transformer", "transformers"],
    "语言模型": ["language model", "language models", "lm"],
    "大语言模型": ["large language model", "llm", "large language models"],
    "自然语言处理": ["natural language processing", "nlp"],
    "计算机视觉": ["computer vision", "vision"],
    "语音识别": ["speech recognition", "asr"],
    "生成": ["generation", "generate", "generative"],
    "检索": ["retrieval", "retrieve", "search"],
    "排序": ["ranking", "rank"],
    "分类": ["classification", "classify"],
    "回归": ["regression"],
    "聚类": ["clustering", "cluster"],
    "分词": ["tokenization", "tokenize"],
    "嵌入": ["embedding", "embeddings", "representation"],
    "表示学习": ["representation learning"],
    "推理": ["inference", "reasoning"],
    "提示": ["prompt", "prompting"],
    "微调": ["fine-tuning", "fine-tune", "finetuning"],
    "预训练": ["pre-training", "pretrain", "pretrained"],
    "零样本": ["zero-shot"],
    "少样本": ["few-shot"],
    "多样本": ["many-shot"],
    "上下文学习": ["in-context learning", "in context learning"],
    "思维链": ["chain of thought", "chain-of-thought", "cot"],
    # 训练细节
    "训练": ["training", "train"],
    "优化": ["optimization", "optimizer", "optimize"],
    "损失函数": ["loss", "loss function", "objective"],
    "梯度": ["gradient", "gradients"],
    "反向传播": ["backpropagation", "back-propagation"],
    "参数": ["parameter", "parameters", "weights"],
    "超参数": ["hyperparameter", "hyper-parameters"],
    "正则化": ["regularization", "regularisation"],
    "丢弃": ["dropout"],
    "批归一化": ["batch normalization", "batchnorm", "layer normalization"],
    "学习率": ["learning rate"],
    "批大小": ["batch size"],
    "收敛": ["convergence", "converge"],
    "过拟合": ["overfitting", "overfit"],
    "泛化": ["generalization", "generalisation", "generalize"],
    "鲁棒性": ["robustness", "robust"],
    "效率": ["efficiency", "efficient"],
    "复杂度": ["complexity", "computational cost"],
    "规模": ["scale", "scaling", "size"],
    "数据增强": ["data augmentation", "augmentation"],
    # 评价指标
    "准确率": ["accuracy", "acc"],
    "精确率": ["precision"],
    "召回率": ["recall"],
    "f1值": ["f1", "f1 score"],
    "困惑度": ["perplexity"],
    "bleu": ["bleu"],
    "rouge": ["rouge"],
    "平均精度": ["average precision", "map", "mean average precision"],
    "错误率": ["error rate", "wer"],
    # 数据与文本
    "文本": ["text", "document"],
    "图像": ["image", "images", "visual"],
    "语音": ["speech", "audio"],
    "视频": ["video"],
    "语料": ["corpus", "corpora"],
    "词表": ["vocabulary", "vocab"],
    "标注": ["annotation", "annotate", "label"],
    "标签": ["label", "labels"],
    "序列": ["sequence", "sequences"],
    "上下文": ["context", "contextual"],
    "语义": ["semantic", "semantics", "meaning"],
    "语法": ["syntax", "syntactic"],
    "特征": ["feature", "features"],
    "输入": ["input", "inputs"],
    "输出": ["output", "outputs"],
    "噪声": ["noise", "noisy"],
    "采样": ["sampling", "sample"],
    "分布": ["distribution", "distributions"],
    # 学术写作
    "假设": ["hypothesis", "assumption"],
    "证明": ["proof", "prove", "theorem"],
    "定理": ["theorem"],
    "引理": ["lemma"],
    "推导": ["derivation", "derive"],
    "公式": ["equation", "formula"],
    "复杂度分析": ["complexity analysis"],
    "显著性": ["significance", "significant"],
    "置信区间": ["confidence interval"],
    "可复现性": ["reproducibility", "reproducible"],
    "基准测试": ["benchmark", "benchmarks"],
    "性能": ["performance"],
    "提升": ["improve", "improvement", "gain"],
    "下降": ["decrease", "degradation", "drop"],
    "优势": ["advantage", "strength", "benefit"],
    "不足": ["limitation", "drawback", "shortcoming"],
}

# 反向索引：英文词 → 中文候选
_REVERSE: dict[str, list[str]] = {}
for _cn, _ens in GLOSSARY.items():
    for _en in _ens:
        _REVERSE.setdefault(_en.lower(), []).append(_cn)

_CN_RE = re.compile(r"[\u4e00-\u9fff]+")
_EN_RE = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")

# 中文功能字：出现在 bigram 里就把该 bigram 丢掉，避免"的核""是什"这类噪声
_CN_STOP_CHARS = set("的了是在和与对这那有我们什么一个上下中为以及等很就都也还不没有会能可将把被让从到向于其此些")

# 英文停用词
_EN_STOP = {
    "the", "and", "for", "with", "that", "this", "these", "those", "from", "are", "was",
    "were", "has", "have", "had", "not", "but", "can", "may", "will", "would", "should",
    "what", "which", "who", "how", "why", "when", "where", "does", "did", "been", "being",
    "into", "than", "then", "them", "they", "their", "there", "here", "about", "between",
    "using", "used", "use", "based", "such", "also", "more", "most", "some", "any", "all",
    "each", "other", "only", "over", "under", "after", "before", "both", "same", "very",
}

MAX_TERMS = 26
MAX_CJK_BIGRAMS = 10


def keyword_terms(query: str) -> list[str]:
    """从自然语言问题里抽取可检索的关键词。

    英文取内容词（去停用词）；中文取 bigram（去掉含功能字的噪声 bigram），
    短中文串（≤4 字）整体保留。
    """
    out: list[str] = []
    for m in _EN_RE.finditer(query):
        w = m.group(0).lower()
        if w in _EN_STOP or len(w) < 3:
            continue
        out.append(w)
    for run in _CN_RE.findall(query):
        if len(run) <= 4:
            out.append(run)
            continue
        kept = 0
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if any(c in _CN_STOP_CHARS for c in bg):
                continue
            out.append(bg)
            kept += 1
            if kept >= MAX_CJK_BIGRAMS:
                break
    return out


def glossary_expand(query: str) -> list[str]:
    """基于内置词表做中英互译扩展，返回附加检索词。"""
    extra: list[str] = []
    low = query.lower()

    # 中文 → 英文
    for cn, ens in GLOSSARY.items():
        if cn in low:
            extra.extend(ens)

    # 英文 → 中文（处理复数与连字符）
    for m in _EN_RE.finditer(low):
        w = m.group(0)
        for key in (w, w.rstrip("s"), w.replace("-", " "), w.replace("-", "")):
            if key in _REVERSE:
                extra.extend(_REVERSE[key])
                break

    # 去重、保序、限长
    seen: set[str] = set()
    out: list[str] = []
    for t in extra:
        t = t.strip()
        if not t or t.lower() in seen or t.lower() in low:
            continue
        seen.add(t.lower())
        out.append(t)
    return out[:MAX_TERMS]


def llm_expand(llm, query: str) -> list[str]:
    """让模型把问题改写成检索关键词；失败返回空列表。"""
    if llm is None or getattr(llm, "name", "").startswith("mock"):
        return []
    messages = [
        {
            "role": "system",
            "content": (
                "你是学术检索助手。把用户的问题改写成 5~10 个用于全文检索的关键词或短语，"
                "中英文都要给出（因为文献可能是中文或英文），覆盖同义词与领域惯用表达。"
                '只输出 JSON：{"keywords": ["...", "..."]}'
            ),
        },
        {"role": "user", "content": query},
    ]
    try:
        data = llm.chat_json(messages, task="expand")
        kws = data.get("keywords") or []
        return [str(k).strip() for k in kws if str(k).strip()][:12]
    except Exception:
        return []


def expand_query(query: str, llm=None, *, use_llm: bool = True) -> list[str]:
    """Bound each branch separately; Chinese fragments cannot evict translations.

    Interleave branches so the short LIKE fallback also sees translations.
    This changes lexical candidates, never the source/trust/range checks.
    """
    branches = [keyword_terms(query)[:MAX_TERMS], glossary_expand(query)]
    if use_llm and llm is not None:
        branches.append(llm_expand(llm, query))
    # 原文本身也作为一路高精度分支（标题式查询能精确命中）
    if query and query.strip() and len(query) <= 80:
        branches.insert(0, [query.strip()])

    seen: set[str] = set()
    out: list[str] = []
    for row in zip_longest(*branches):
        for t in row:
            if t is None:
                continue
            t = t.strip()
            k = t.lower()
            if k in seen or not k:
                continue
            seen.add(k)
            out.append(t)
    return out


def build_or_fts_query(terms: list[str]) -> str:
    """把多个检索词组合成一条 FTS5 OR 表达式（每个词内部仍为 AND/短语）。"""
    parts: list[str] = []
    for t in terms:
        q = build_fts_query(t)
        if q:
            parts.append(f"({q})")
    return " OR ".join(parts)
