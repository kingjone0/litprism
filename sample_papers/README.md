# 样例论文

这个目录用于存放端到端测试与演示用的论文 PDF。**PDF 本体不入版本库**（约 9.4 MB），
需要时按下面的地址重新下载。

## 下载

```bash
cd sample_papers
curl -sL -o 1706.03762.pdf https://arxiv.org/pdf/1706.03762
curl -sL -o 1810.04805.pdf https://arxiv.org/pdf/1810.04805
curl -sL -o 2005.14165.pdf https://arxiv.org/pdf/2005.14165
```

## 三篇论文

| arXiv ID | 标题 | 年份 | 用途 |
|---|---|---|---|
| `1706.03762` | Attention Is All You Need | 2017 | 英文双栏 · 含公式与架构图 · 15 页 |
| `1810.04805` | BERT: Pre-training of Deep Bidirectional Transformers | 2018 | 英文双栏 · 16 页 |
| `2005.14165` | Language Models are Few-Shot Learners | 2020 | 英文长文 · 75 页 |

三篇都是开放获取，且互相有引用关系 —— 适合验证跨论文关联与对比。

## 为什么要这三篇

- **双栏 + 公式 + 图表**：正好是本地 OCR 处理不了的版面（见 `tools/ocr_probe.py` 的实测）
- **长文（75 页）**：用来压测分块与上下文管理
- **主题相关**：三篇构成一个小主题簇，便于验证知识图谱与推荐

## 导入

```bash
python run.py add sample_papers/ --verify
```
