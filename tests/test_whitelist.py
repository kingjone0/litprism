"""白名单与五态核验测试。

重点：
1. **白名单并存于自动核验结论** —— 不覆盖，`level_at_confirm` 保留原判定；
2. **确认依据必填** —— 防止白名单退化成万能通行证；
3. **人工确认与自动核验视觉可区分** —— 徽章不同，不能混淆；
4. **导入守卫接受人工确认，但拒绝 not_found / unchecked**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.search import import_record  # noqa: E402
from litprism.verify import PaperVerifier, VerificationResult  # noqa: E402
from litprism.whitelist import (  # noqa: E402
    CITABLE_LEVELS,
    CONFIRMABLE_LEVELS,
    LEVEL_BADGES,
    LEVELS,
    Whitelist,
    WhitelistError,
    badge,
    format_entries,
    is_citable,
    label,
    normalize_level,
)

FAKE_TITLE = "Quantum Entanglement Based Gradient Descent for LLM Alignment"


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
    yield cfg, kb
    kb.close()


# ===========================================================================
# 等级定义
# ===========================================================================
class TestLevels:
    def test_five_states(self):
        assert LEVELS == ("verified", "manual_confirmed", "likely", "not_found", "unchecked")

    def test_citable_includes_manual(self):
        assert CITABLE_LEVELS == ("verified", "manual_confirmed")
        assert is_citable("verified") and is_citable("manual_confirmed")
        for lv in ("likely", "not_found", "unchecked", None, ""):
            assert not is_citable(lv)

    def test_confirmable_excludes_verified(self):
        assert "verified" not in CONFIRMABLE_LEVELS
        assert "manual_confirmed" not in CONFIRMABLE_LEVELS

    def test_badges_are_visually_distinct(self):
        """自动核验与人工确认**必须**用不同符号 —— 混淆两者等于把人的判断伪装成机器结论。"""
        assert badge("verified") != badge("manual_confirmed")
        assert "✅" in badge("verified")
        assert "🔵" in badge("manual_confirmed")
        assert len(set(LEVEL_BADGES.values())) == len(LEVELS)

    def test_labels(self):
        assert label("manual_confirmed") == "人工确认"
        assert label("verified") == "已核验"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("verified", "verified"),
            ("MANUAL_CONFIRMED", "manual_confirmed"),
            ("unverified", "unchecked"),      # 兼容旧值
            ("unknown", "unchecked"),
            ("", "unchecked"),
            (None, "unchecked"),
            ("garbage", "unchecked"),
        ],
    )
    def test_normalize_backward_compat(self, raw, expected):
        assert normalize_level(raw) == expected


# ===========================================================================
# 白名单写入守卫
# ===========================================================================
class TestWhitelistGuards:
    def test_reason_required(self, env):
        cfg, kb = env
        with pytest.raises(WhitelistError, match="确认依据必填"):
            Whitelist(kb).add(title="某论文", reason="")
        with pytest.raises(WhitelistError, match="确认依据必填"):
            Whitelist(kb).add(title="某论文", reason="   ")

    def test_short_reason_rejected(self, env):
        cfg, kb = env
        with pytest.raises(WhitelistError, match="过短"):
            Whitelist(kb).add(title="某论文", reason="好")

    def test_title_required(self, env):
        cfg, kb = env
        with pytest.raises(WhitelistError, match="标题不能为空"):
            Whitelist(kb).add(title="  ", reason="在会议论文集里见过")

    def test_verified_level_not_confirmable(self, env):
        """已经自动核验通过的不需要人工确认。"""
        cfg, kb = env
        with pytest.raises(WhitelistError, match="不需要人工确认"):
            Whitelist(kb).add(title="某论文", reason="充分理由在这里", level_at_confirm="verified")

    def test_valid_add(self, env):
        cfg, kb = env
        e = Whitelist(kb).add(
            title=FAKE_TITLE,
            reason="在 NeurIPS 2024 会议论文集里见过",
            level_at_confirm="not_found",
        )
        assert e.id.startswith("wl_")
        assert e.level_at_confirm == "not_found"
        assert e.confirmed_by == "local_user"

    def test_duplicate_returns_existing(self, env):
        cfg, kb = env
        wl = Whitelist(kb)
        a = wl.add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")
        b = wl.add(title=FAKE_TITLE, reason="另一个理由也够长", level_at_confirm="not_found")
        assert a.id == b.id
        assert len(wl.list()) == 1


# ===========================================================================
# 查找
# ===========================================================================
class TestWhitelistFind:
    def test_find_by_exact_title(self, env):
        cfg, kb = env
        wl = Whitelist(kb)
        e = wl.add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")
        assert wl.find(title=FAKE_TITLE).id == e.id

    def test_find_title_case_and_punct_insensitive(self, env):
        cfg, kb = env
        wl = Whitelist(kb)
        e = wl.add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")
        assert wl.find(title=FAKE_TITLE.lower() + ".").id == e.id

    def test_find_by_doi(self, env):
        cfg, kb = env
        wl = Whitelist(kb)
        e = wl.add(
            title="某论文", reason="DOI 在出版社页面可访问",
            doi="10.1234/abc", level_at_confirm="not_found",
        )
        assert wl.find(doi="10.1234/abc").id == e.id

    def test_find_miss(self, env):
        cfg, kb = env
        Whitelist(kb).add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")
        assert Whitelist(kb).find(title="完全不同的另一篇论文标题") is None

    def test_remove(self, env):
        cfg, kb = env
        wl = Whitelist(kb)
        e = wl.add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")
        assert wl.remove(e.id)
        assert wl.find(title=FAKE_TITLE) is None

    def test_stats(self, env):
        cfg, kb = env
        wl = Whitelist(kb)
        wl.add(title="A 论文标题", reason="理由足够长的一条", level_at_confirm="not_found")
        wl.add(title="B 论文标题", reason="理由足够长的另一条", level_at_confirm="likely")
        st = wl.stats()
        assert st["total"] == 2
        assert st["by_original_verdict"]["not_found"] == 1

    def test_format_entries(self, env):
        cfg, kb = env
        wl = Whitelist(kb)
        wl.add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")
        text = format_entries(wl.list())
        assert "确认时自动判定" in text and "未能核到" in text
        assert format_entries([]) == "（白名单为空）"


# ===========================================================================
# 核验集成
# ===========================================================================
class TestVerifyIntegration:
    def test_whitelist_hit_short_circuits(self, env):
        """命中白名单即放行，且**不走网络**。"""
        cfg, kb = env
        wl = Whitelist(kb)
        entry = wl.add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")

        v = PaperVerifier(cfg, wl)
        r = v.verify(title=FAKE_TITLE)
        assert r.level == "manual_confirmed"
        assert r.manual is True
        assert r.exists is True
        assert r.whitelist_id == entry.id
        assert r.sources_ok == ["whitelist"]
        assert r.elapsed_ms < 500, "命中白名单不应发起网络请求"

    def test_original_verdict_preserved(self, env):
        """⭐ 白名单并存于自动结论，不覆盖 —— 将来复核能分清谁判的。"""
        cfg, kb = env
        wl = Whitelist(kb)
        wl.add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")

        r = PaperVerifier(cfg, wl).verify(title=FAKE_TITLE)
        assert r.original_level == "not_found"
        assert "未能核到" in r.message
        assert "人工确认放行" in r.message

    def test_manual_vs_auto_are_distinguishable(self, env):
        cfg, kb = env
        wl = Whitelist(kb)
        wl.add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")
        r = PaperVerifier(cfg, wl).verify(title=FAKE_TITLE)
        assert r.manual is True
        assert r.auto_verified is False
        assert r.verified is True

    def test_skip_whitelist_forces_recheck(self, env, monkeypatch):
        """允许强制重核 —— 用户可能想看看现在数据库里有没有了。"""
        cfg, kb = env
        wl = Whitelist(kb)
        wl.add(title=FAKE_TITLE, reason="在会议论文集里见过", level_at_confirm="not_found")

        v = PaperVerifier(cfg, wl)
        called = {"n": 0}

        def fake_source(*a, **k):
            called["n"] += 1
            return []

        for src in v._sources.values():
            monkeypatch.setattr(src, "by_title", fake_source)
        r = v.verify(title=FAKE_TITLE, skip_whitelist=True)
        assert called["n"] > 0, "跳过白名单时应真的去查"
        assert r.level != "manual_confirmed"

    def test_no_whitelist_configured(self, env):
        """未挂白名单时行为不变。"""
        cfg, kb = env
        v = PaperVerifier(cfg)
        assert v.whitelist is None


# ===========================================================================
# 导入守卫
# ===========================================================================
class TestImportGuard:
    def test_accepts_verified(self, env):
        cfg, kb = env
        vr = VerificationResult(query_title="Real", level="verified", exists=True, confidence=1.0)
        pid, _ = import_record(kb, {"title": "Real Paper", "year": 2020}, verification=vr)
        assert kb.get_paper(pid)["verify_level"] == "verified"

    def test_accepts_manual_confirmed(self, env):
        cfg, kb = env
        vr = VerificationResult(
            query_title=FAKE_TITLE, level="manual_confirmed", exists=True,
            confidence=1.0, manual=True, original_level="not_found",
            confirm_reason="在会议论文集里见过",
        )
        pid, _ = import_record(kb, {"title": FAKE_TITLE}, verification=vr)
        p = kb.get_paper(pid)
        assert p["verify_level"] == "manual_confirmed"
        # 原判定留在证据里
        ev = p.get("verify_evidence") or {}
        assert ev.get("original_level") == "not_found"

    @pytest.mark.parametrize("level", ["not_found", "unchecked", "likely"])
    def test_rejects_non_citable(self, env, level):
        cfg, kb = env
        vr = VerificationResult(query_title="X", level=level, exists=False)
        with pytest.raises(ValueError, match="拒绝导入"):
            import_record(kb, {"title": "X"}, verification=vr)
        assert kb.list_papers() == []
