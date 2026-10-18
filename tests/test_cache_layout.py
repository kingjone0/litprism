"""缓存友好的上下文布局测试。

前缀缓存匹配的是**最长公共前缀**。要能命中，必须同时满足：

1. **原文在前、指令在后** —— 变化的只有尾部；
2. **原文序列化字节稳定** —— 同样片段必然渲染出同样字符串。

这两条任何一条不成立，缓存就完全失效（不是"打折"，是"没有"）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.reading import (  # noqa: E402
    PAPER_HEADER,
    TASK_SEPARATOR,
    build_messages,
    excerpt_text,
    excerpt_context,
    prefix_of,
)


def excerpt(i: int, text: str = "", page: int | None = 1) -> dict:
    return {
        "tag": f"P{i}", "id": f"c{i}", "paper_id": "p1", "paper_title": "T",
        "page": page, "text": text or f"第 {i} 段原文内容。", "document_tag": "D1",
    }


class TestByteStability:
    def test_same_excerpts_render_identically(self):
        ex = [excerpt(1), excerpt(2)]
        assert excerpt_context(ex) == excerpt_context(ex)

    def test_render_is_deterministic_across_calls(self):
        """两次独立渲染必须逐字节相同 —— 缓存键依赖这一点。"""
        a = excerpt_context([excerpt(1, "内容 A"), excerpt(2, "内容 B")])
        b = excerpt_context([excerpt(1, "内容 A"), excerpt(2, "内容 B")])
        assert a.encode() == b.encode()

    def test_no_volatile_content(self):
        """渲染结果里不能出现时间戳、随机量等会破坏缓存的东西。"""
        text = excerpt_context([excerpt(1)])
        assert "20" not in text.replace("第 1 段", "")  # 粗略：不含年份样式
        assert "uuid" not in text.lower()

    def test_order_changes_output(self):
        """顺序敏感：调用方必须按 chunk 序号排序，否则前缀会漂移。"""
        a = excerpt_context([excerpt(1), excerpt(2)])
        b = excerpt_context([excerpt(2), excerpt(1)])
        assert a != b


class TestPrefixProperty:
    def test_paper_comes_first(self):
        msgs = build_messages("SYS", "PAPER_TEXT", "DIRECTIVE")
        assert msgs[0]["role"] == "system"
        assert msgs[-1]["content"].startswith("PAPER_TEXT")

    def test_directive_at_the_end(self):
        msgs = build_messages("SYS", "PAPER_TEXT", "DIRECTIVE")
        assert msgs[-1]["content"].endswith("DIRECTIVE")

    def test_different_directives_share_prefix(self):
        """⭐ 这是缓存命中的核心断言。"""
        paper = excerpt_context([excerpt(1), excerpt(2)])
        a = build_messages("SYS", paper, "只看第 3 节，提取方法论")
        b = build_messages("SYS", paper, "只看第 4 节，提取结论")
        assert prefix_of(a, "只看第 3 节，提取方法论") == prefix_of(b, "只看第 4 节，提取结论")

    def test_prefix_equals_paper_plus_separator(self):
        paper = excerpt_context([excerpt(1)])
        msgs = build_messages("SYS", paper, "DIR")
        assert prefix_of(msgs, "DIR") == paper + TASK_SEPARATOR

    def test_different_papers_do_not_share_prefix(self):
        a = build_messages("SYS", excerpt_context([excerpt(1)]), "D")
        b = build_messages("SYS", excerpt_context([excerpt(2)]), "D")
        assert prefix_of(a, "D") != prefix_of(b, "D")

    def test_old_layout_would_not_have_shared_prefix(self):
        """反证：旧布局（指令前置）的公共前缀只有几个字，新布局是整篇原文。"""
        paper = "P" * 5000
        d1, d2 = "只看第 3 节，提取方法论", "只看第 4 节，提取结论"

        old_a, old_b = d1 + "\n\n" + paper, d2 + "\n\n" + paper
        new_a = build_messages("SYS", paper, d1)[-1]["content"]
        new_b = build_messages("SYS", paper, d2)[-1]["content"]

        def common(a: str, b: str) -> int:
            n = 0
            for x, y in zip(a, b):
                if x != y:
                    break
                n += 1
            return n

        old_shared, new_shared = common(old_a, old_b), common(new_a, new_b)
        assert old_shared < 10, f"旧布局只共享 {old_shared} 个字符"
        assert new_shared >= len(paper), f"新布局应共享整篇原文，实际 {new_shared}"
        assert new_shared > old_shared * 100


class TestContentPreserved:
    def test_header_present(self):
        assert excerpt_context([excerpt(1)]).startswith(PAPER_HEADER)

    def test_all_excerpts_included(self):
        ex = [excerpt(i) for i in range(1, 6)]
        text = excerpt_context(ex)
        for e in ex:
            assert e["text"] in text
            assert e["id"] in text

    def test_separator_between_paper_and_task(self):
        msgs = build_messages("SYS", "P", "D")
        assert TASK_SEPARATOR.strip() in msgs[-1]["content"]

    def test_excerpt_text_unchanged(self):
        """原有渲染格式不动 —— 避免影响引用解析。"""
        text = excerpt_text([excerpt(1, "X")])
        assert text.startswith("[P1] D1《T》 p.1 chunk_id=c1")
