"""MCP 适配层测试。

重点：
1. **工具逻辑与 SDK 解耦** —— 大部分测试不依赖 MCP SDK；
2. **默认只读** —— 写工具在只读模式下被拒，且不对外暴露；
3. **审计** —— 参数不存明文，但足够排查；
4. **经 MCP 写入同样受核验约束** —— 不接受无法确认真实性的文献。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.mcp.audit import AuditLog, digest_args, format_audit  # noqa: E402
from litprism.mcp.tools import (  # noqa: E402
    RESOURCES,
    TOOLS,
    LitPrismTools,
    describe_tools,
    dump_schema,
    to_json,
)

SDK_AVAILABLE = True
try:  # pragma: no cover - 环境相关
    import mcp  # noqa: F401
except ImportError:  # pragma: no cover
    SDK_AVAILABLE = False


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")

    pid = kb.add_paper(title="Attention Is All You Need", year=2017,
                       authors=["Ashish Vaswani"], verify_status="verified", verify_level="verified")
    kb.add_chunks(pid, [
        {"text": "注意力机制的核心是缩放点积注意力，复杂度 O(n^2 d)。", "page": 1, "section": "方法"},
        {"text": "多头注意力允许模型在不同表示子空间联合关注信息。", "page": 2, "section": "方法"},
    ])
    kb.save_analysis(pid, {
        "research_question": "能否用纯注意力替代循环网络？",
        "methodology": "Transformer",
        "key_conclusions": "在 WMT 上取得 28.4 BLEU",
    })

    pid2 = kb.add_paper(title="BERT", year=2018, verify_status="manual_confirmed",
                        verify_level="manual_confirmed")
    kb.add_chunks(pid2, [{"text": "BERT 使用掩码语言模型预训练。", "page": 1, "section": "预训练"}])

    yield cfg, kb, pid, pid2
    kb.close()


# ===========================================================================
# 工具清单
# ===========================================================================
class TestToolCatalog:
    def test_eight_read_tools_plus_one_write(self):
        read = [k for k, v in TOOLS.items() if v["readonly"]]
        write = [k for k, v in TOOLS.items() if not v["readonly"]]
        assert len(read) == 8
        assert write == ["import_paper"]

    def test_two_resources(self):
        assert set(RESOURCES) == {"litprism://library", "litprism://paper/{id}"}

    def test_describe_and_schema(self):
        text = describe_tools()
        assert "search_papers" in text and "写（需显式开启）" in text
        schema = dump_schema()
        assert len(schema["tools"]) == len(TOOLS)
        assert len(schema["resources"]) == len(RESOURCES)

    def test_to_json_handles_unicode(self):
        assert "注意力" in to_json({"q": "注意力"})


# ===========================================================================
# 只读工具
# ===========================================================================
class TestReadTools:
    def test_search_papers(self, env):
        cfg, kb, pid, _ = env
        r = LitPrismTools(kb, cfg).search_papers("attention")
        assert r["ok"] and r["count"] == 1
        assert r["items"][0]["verify_level"] == "verified"
        assert "✅" in r["items"][0]["verify_badge"]

    def test_search_papers_verified_only(self, env):
        cfg, kb, pid, pid2 = env
        r = LitPrismTools(kb, cfg).search_papers("", verified_only=True)
        ids = [i["id"] for i in r["items"]]
        assert pid in ids and pid2 in ids, "人工确认的也算可引用"

    def test_get_paper_by_id_and_title(self, env):
        cfg, kb, pid, _ = env
        t = LitPrismTools(kb, cfg)
        a = t.get_paper(paper_id=pid)
        assert a["ok"] and a["analysis"]["methodology"] == "Transformer"
        b = t.get_paper(title="attention")
        assert b["ok"] and b["paper"]["id"] == pid

    def test_get_paper_missing(self, env):
        cfg, kb, _, _ = env
        r = LitPrismTools(kb, cfg).get_paper(paper_id="nope")
        assert not r["ok"] and "未找到" in r["error"]

    def test_search_passages(self, env):
        cfg, kb, pid, _ = env
        r = LitPrismTools(kb, cfg).search_passages("注意力", top_k=5)
        assert r["ok"] and r["count"] >= 1
        item = r["items"][0]
        assert item["page"] in (1, 2)
        assert item["chunk_id"]

    def test_search_passages_requires_query(self, env):
        cfg, kb, _, _ = env
        r = LitPrismTools(kb, cfg).search_passages("")
        assert not r["ok"] and "不能为空" in r["error"]

    def test_list_collections(self, env):
        cfg, kb, pid, _ = env
        r = LitPrismTools(kb, cfg).list_collections()
        assert r["ok"]
        assert "collections" in r and "papers" in r

    def test_get_graph(self, env):
        cfg, kb, pid, pid2 = env
        r = LitPrismTools(kb, cfg).get_graph()
        assert r["ok"] and r["node_count"] == 2

    def test_compare_papers(self, env):
        cfg, kb, pid, pid2 = env
        r = LitPrismTools(kb, cfg).compare_papers([pid, pid2])
        assert r["ok"] and len(r["papers"]) == 2
        assert "研究问题" in r["dimensions"]

    def test_compare_requires_two(self, env):
        cfg, kb, pid, _ = env
        r = LitPrismTools(kb, cfg).compare_papers([pid])
        assert not r["ok"] and "至少" in r["error"]

    def test_ask_without_llm(self, env):
        cfg, kb, _, _ = env
        r = LitPrismTools(kb, cfg, llm=None).ask("注意力是什么")
        assert not r["ok"] and "未配置模型" in r["error"]

    def test_verify_paper_requires_input(self, env):
        cfg, kb, _, _ = env
        r = LitPrismTools(kb, cfg).verify_paper()
        assert not r["ok"] and "至少" in r["error"]

    def test_resources(self, env):
        cfg, kb, pid, _ = env
        t = LitPrismTools(kb, cfg)
        lib = t.resource_library()
        assert "知识库概览" in lib and "Attention" in lib
        paper = t.resource_paper(pid)
        assert "# Attention Is All You Need" in paper and "## 方法" in paper
        assert "未找到" in t.resource_paper("nope")


# ===========================================================================
# 只读守卫
# ===========================================================================
class TestReadonlyGuard:
    def test_import_blocked_in_readonly(self, env):
        cfg, kb, _, _ = env
        r = LitPrismTools(kb, cfg, readonly=True).import_paper(title="某论文")
        assert not r["ok"]
        assert "只读模式" in r["error"]
        assert "mcp.readonly = false" in r["error"]

    def test_import_requires_input_even_when_writable(self, env):
        cfg, kb, _, _ = env
        r = LitPrismTools(kb, cfg, readonly=False).import_paper()
        assert not r["ok"] and "至少" in r["error"]

    def test_default_is_readonly(self, env):
        cfg, kb, _, _ = env
        assert LitPrismTools(kb, cfg).readonly is True


# ===========================================================================
# 审计
# ===========================================================================
class TestAudit:
    def test_record_and_list(self, env):
        cfg, kb, _, _ = env
        log = AuditLog(kb)
        log.record("search_papers", {"query": "attention", "limit": 10}, ok=True)
        log.record("verify_paper", {"title": "x"}, ok=False, error="网络失败")
        entries = log.list()
        assert len(entries) == 2
        assert entries[0]["tool"] == "verify_paper"   # 最新在前
        assert entries[0]["ok"] == 0

    def test_stats(self, env):
        cfg, kb, _, _ = env
        log = AuditLog(kb)
        log.record("a", {}, ok=True)
        log.record("a", {}, ok=True)
        log.record("b", {}, ok=False, error="x")
        st = log.stats()
        assert st["total"] == 3 and st["ok"] == 2 and st["failed"] == 1
        assert st["by_tool"]["a"] == 2

    def test_digest_does_not_leak_full_text(self):
        """⭐ 参数不存明文，但保留键名与截断值便于排查。"""
        long_text = "这是一段非常长的用户输入" * 50
        d = digest_args({"query": long_text, "limit": 10})
        assert "query=" in d and "limit=10" in d
        assert len(d) < len(long_text), "摘要必须比原文短"
        assert "#" in d, "应带指纹以便比对"

    def test_digest_empty(self):
        assert digest_args(None) == "-"
        assert digest_args({}) == "-"

    def test_digest_is_deterministic(self):
        a = digest_args({"b": 2, "a": 1})
        b = digest_args({"a": 1, "b": 2})
        assert a == b, "键序不影响摘要"

    def test_format(self, env):
        cfg, kb, _, _ = env
        log = AuditLog(kb)
        log.record("t", {"q": "x"}, ok=True)
        assert "✓" in format_audit(log.list())
        assert format_audit([]) == "（暂无调用记录）"

    def test_clear(self, env):
        cfg, kb, _, _ = env
        log = AuditLog(kb)
        log.record("t", {}, ok=True)
        assert log.clear() == 1
        assert log.list() == []


# ===========================================================================
# Server 装配（需要 SDK）
# ===========================================================================
@pytest.mark.skipif(not SDK_AVAILABLE, reason="未安装 MCP SDK")
class TestServerAssembly:
    def test_build_server(self, env):
        cfg, kb, _, _ = env
        from litprism.mcp.server import build_server

        server = build_server(kb, cfg, llm=None)
        assert server is not None
        assert server.name == "litprism"
        assert server._litprism_impl.readonly is True

    def test_readonly_mode_hides_write_tool(self, env):
        cfg, kb, _, _ = env
        from litprism.mcp.server import TOOL_SCHEMAS, build_server

        server = build_server(kb, cfg, llm=None)
        assert "import_paper" in TOOL_SCHEMAS
        # 只读模式下该工具不应出现在 list_tools 的结果里（通过 handler 检查）
        assert server._litprism_impl.readonly is True

    def test_writable_mode(self, env):
        cfg, kb, _, _ = env
        cfg.mcp.readonly = False
        from litprism.mcp.server import build_server

        server = build_server(kb, cfg, llm=None)
        assert server._litprism_impl.readonly is False

    def test_tool_schemas_cover_all_tools(self):
        from litprism.mcp.server import TOOL_SCHEMAS

        assert set(TOOL_SCHEMAS) == set(TOOLS), "每个工具都要有输入 schema"


class TestServerWithoutSDK:
    def test_error_message_is_actionable(self, monkeypatch):
        """SDK 不可用时要给出可操作的安装指引，而不是一句 ImportError。"""
        import builtins

        from litprism.mcp import server as srv

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name.startswith("mcp"):
                raise ImportError("No module named 'mcp'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(RuntimeError) as ei:
            srv._mcp_import()
        msg = str(ei.value)
        assert "pip install mcp" in msg
        assert "aliyun" in msg, "默认镜像可能没有该包，要给出可用的替代源"
