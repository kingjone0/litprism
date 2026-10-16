"""文献棱镜 LitPrism —— 个人学术研究论文知识解读 Agent。

模块划分::

    config      配置加载
    textutil    文本归一化 / CJK 分词 / FTS 查询构造
    db          SQLite 知识库（论文、片段、结构化解读、术语、关联、核验记录）
    ingest      PDF/HTML/DOCX/TXT/MD/TeX 解析 → 章节识别 → 分块
    llm         OpenAI 兼容 / Ollama / 离线 mock 三类大模型客户端
    prompts     提示词模板（学术规范 + 防幻觉约束）
    extract     核心信息结构化提取（研究问题/方法/实验/结论/创新点/局限）
    verify      论文真实性核验（Crossref / OpenAlex / arXiv / Semantic Scholar 多源交叉验证）
    search      本地 BM25 检索 + 外部文献检索
    relations   跨论文关联分析（TF-IDF 余弦相似度）
    agents      问答 / 对比 / 推荐 三个 Agent
    cli         命令行入口
    web         FastAPI Web 界面
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
