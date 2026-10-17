"""Skill 框架测试。

**框架的正确性不依赖任何内置 Skill** —— 全部用临时目录构造的夹具验证。
这正是「建框架、不装内容」这个决定的关键：如果框架只在自己人写的 Skill 上验证过，
很容易把「恰好够用」当成「足够通用」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from litprism.config import Config  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.skills.manifest import (  # noqa: E402
    CAPABILITIES,
    SCHEMA_VERSION,
    load_manifest,
    parse_manifest,
)
from litprism.skills.registry import SkillRegistry, format_entries  # noqa: E402
from litprism.skills.runner import SkillRunner, _render_template, format_result  # noqa: E402
from litprism.skills.validate import validate  # noqa: E402


def make_skill(root: Path, skill_id: str, *, manifest: dict | None = None, prompt: str | None = None,
               extra_files: dict | None = None) -> Path:
    """在临时目录里造一个 Skill。"""
    d = root / skill_id
    d.mkdir(parents=True, exist_ok=True)
    base = {
        "schema_version": SCHEMA_VERSION,
        "id": skill_id,
        "name": f"测试技能 {skill_id}",
        "version": "1.0.0",
        "description": "用于测试的夹具 Skill",
        "trigger": {"prefix": f"/{skill_id}", "keywords": [skill_id]},
        "inputs": [{"key": "topic", "label": "主题", "type": "text", "required": True}],
        "context": [{"capability": "search_passages", "query": "{{topic}}", "top_k": 5}],
        "capabilities": ["search_passages"],
        "output": {"type": "answer", "citation_required": True},
    }
    if manifest:
        base.update(manifest)
    (d / "skill.yaml").write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    (d / "prompt.md").write_text(
        prompt if prompt is not None else "请围绕 {{topic}} 整理要点，并标注来源 [P1 p.1]。",
        encoding="utf-8",
    )
    for name, content in (extra_files or {}).items():
        (d / name).write_text(content, encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path / "data")
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg, db_path=tmp_path / "t.db")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    yield cfg, kb, skills_dir
    kb.close()


# ===========================================================================
# 清单解析
# ===========================================================================
class TestManifest:
    def test_parse_minimal(self):
        m = parse_manifest({"schema_version": 1, "id": "demo", "name": "示例"})
        assert m.id == "demo" and m.name == "示例"
        assert m.ok  # 解析阶段不校验

    def test_parse_full(self, tmp_path):
        d = make_skill(tmp_path, "demo")
        m = load_manifest(d)
        assert m.ok, m.load_error
        assert m.id == "demo"
        assert m.citation_required is True
        assert m.output_type == "answer"
        assert [i.key for i in m.inputs] == ["topic"]
        assert m.context[0].capability == "search_passages"
        assert "{{topic}}" in m.prompt

    def test_declared_vars(self, tmp_path):
        d = make_skill(tmp_path, "demo", prompt="用 {{topic}} 和 {{focus}} 做点什么")
        m = load_manifest(d)
        assert m.declared_vars() == {"topic", "focus"}

    def test_missing_manifest_sets_error(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        m = load_manifest(d)
        assert not m.ok
        assert "skill.yaml" in m.load_error

    def test_broken_yaml_sets_error_not_raises(self, tmp_path):
        d = tmp_path / "broken"
        d.mkdir()
        (d / "skill.yaml").write_text("id: [unclosed", encoding="utf-8")
        (d / "prompt.md").write_text("x", encoding="utf-8")
        m = load_manifest(d)   # 不应抛异常
        assert not m.ok

    def test_missing_prompt_sets_error(self, tmp_path):
        d = tmp_path / "noprompt"
        d.mkdir()
        (d / "skill.yaml").write_text(
            yaml.safe_dump({"schema_version": 1, "id": "noprompt", "name": "x"}), encoding="utf-8"
        )
        m = load_manifest(d)
        assert not m.ok and "prompt.md" in m.load_error


# ===========================================================================
# 校验
# ===========================================================================
class TestValidation:
    def test_valid_skill_passes(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo"))
        r = validate(m)
        assert r.ok, [str(i) for i in r.errors]

    def test_schema_version_mismatch(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", manifest={"schema_version": 99}))
        r = validate(m)
        assert not r.ok and any(i.code == "schema.version" for i in r.errors)

    def test_id_pattern(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "Bad_ID"))
        r = validate(m)
        assert not r.ok and any(i.code == "id.pattern" for i in r.errors)

    def test_id_must_match_dirname(self, tmp_path):
        d = make_skill(tmp_path, "demo", manifest={"id": "other-name"})
        m = load_manifest(d)
        r = validate(m)
        assert not r.ok
        assert any(i.code == "id.dirname" for i in r.errors)

    def test_illegal_capability(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", manifest={
            "capabilities": ["search_passages", "run_shell"],
        }))
        r = validate(m)
        assert not r.ok
        issue = next(i for i in r.errors if i.code == "capabilities.illegal")
        assert "run_shell" in issue.message
        assert "能力表固定不可扩展" in issue.message

    def test_context_illegal_capability(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", manifest={
            "context": [{"capability": "exec_code"}],
            "capabilities": ["search_passages"],
        }))
        r = validate(m)
        assert not r.ok and any(i.code == "context.illegal" for i in r.errors)

    def test_undeclared_prompt_variable(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", prompt="处理 {{topic}} 与 {{mystery}}"))
        r = validate(m)
        assert not r.ok
        issue = next(i for i in r.errors if i.code == "prompt.undeclared_var")
        assert "mystery" in issue.message

    def test_empty_prompt(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", prompt="   "))
        r = validate(m)
        assert not r.ok and any(i.code == "prompt.empty" for i in r.errors)

    def test_invalid_input_type(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", manifest={
            "inputs": [{"key": "topic", "type": "file_upload"}],
        }))
        r = validate(m)
        assert not r.ok and any(i.code == "inputs.type" for i in r.errors)

    def test_invalid_output_type(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", manifest={
            "output": {"type": "magic", "citation_required": True},
        }))
        r = validate(m)
        assert not r.ok and any(i.code == "output.type" for i in r.errors)

    def test_invalid_deliverable_type(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", manifest={
            "output": {"type": "deliverable", "deliverable_type": "novel", "citation_required": True},
        }))
        r = validate(m)
        assert not r.ok and any(i.code == "output.deliverable_type" for i in r.errors)

    def test_unused_required_input_is_warning(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", manifest={
            "inputs": [
                {"key": "topic", "required": True},
                {"key": "unused", "required": True},
            ],
        }))
        r = validate(m)
        assert r.ok, "未使用的必填输入只是警告，不阻止加载"
        assert any(i.code == "inputs.unused" and i.severity == "warning" for i in r.warnings)

    def test_citation_hint_warning(self, tmp_path):
        m = load_manifest(make_skill(tmp_path, "demo", prompt="随便写点什么，不提引用。"))
        r = validate(m)
        assert r.ok
        assert any(i.code == "output.citation_hint" for i in r.warnings)

    def test_code_file_is_warned_not_loaded(self, tmp_path):
        """⭐ 仅声明式：目录里的可执行文件不会被加载或执行，但必须提示用户。"""
        m = load_manifest(make_skill(tmp_path, "demo", extra_files={"evil.py": "print('hi')"}))
        r = validate(m)
        assert any(i.code == "security.code_file" for i in r.warnings)
        issue = next(i for i in r.warnings if i.code == "security.code_file")
        assert "不会被加载或执行" in issue.message

    def test_load_error_short_circuits(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        r = validate(load_manifest(d))
        assert not r.ok and any(i.code == "load.failed" for i in r.errors)

    def test_capability_table_is_fixed(self):
        assert set(CAPABILITIES) == {
            "search_passages", "search_papers", "get_analysis",
            "list_collections", "get_graph", "verify_paper", "compare_papers",
        }


# ===========================================================================
# 注册表
# ===========================================================================
class TestRegistry:
    def test_empty_state(self, env):
        cfg, kb, skills_dir = env
        reg = SkillRegistry(cfg, skills_dir)
        assert reg.discover() == []
        assert reg.stats()["empty"] is True
        assert "尚未添加 Skill" in reg.empty_state_text()

    def test_discovers_valid_skill(self, env):
        cfg, kb, skills_dir = env
        make_skill(skills_dir, "alpha")
        reg = SkillRegistry(cfg, skills_dir)
        entries = reg.discover()
        assert len(entries) == 1
        assert entries[0].id == "alpha" and entries[0].ok
        assert entries[0].status == "正常"

    def test_skips_hidden_and_underscore_dirs(self, env):
        cfg, kb, skills_dir = env
        make_skill(skills_dir, "alpha")
        (skills_dir / ".hidden").mkdir()
        (skills_dir / "_draft").mkdir()
        reg = SkillRegistry(cfg, skills_dir)
        assert [e.id for e in reg.discover()] == ["alpha"]

    def test_broken_skill_does_not_break_others(self, env):
        cfg, kb, skills_dir = env
        make_skill(skills_dir, "good")
        bad = skills_dir / "bad"
        bad.mkdir()
        (bad / "skill.yaml").write_text("id: [broken", encoding="utf-8")
        reg = SkillRegistry(cfg, skills_dir)
        entries = reg.discover()
        assert len(entries) == 2
        good = next(e for e in entries if e.id == "good")
        assert good.ok

    def test_usable_only_filter(self, env):
        cfg, kb, skills_dir = env
        make_skill(skills_dir, "good")
        make_skill(skills_dir, "bad", manifest={"capabilities": ["nope"]})
        reg = SkillRegistry(cfg, skills_dir)
        reg.discover()
        assert [e.id for e in reg.list(usable_only=True)] == ["good"]

    def test_match_by_prefix_and_keyword(self, env):
        cfg, kb, skills_dir = env
        make_skill(skills_dir, "alpha", manifest={
            "trigger": {"prefix": "/alpha", "keywords": ["阿尔法"]},
        })
        reg = SkillRegistry(cfg, skills_dir)
        reg.discover()
        assert reg.match("/alpha 做点什么").id == "alpha"
        assert reg.match("帮我做个阿尔法分析").id == "alpha"
        assert reg.match("完全无关的话") is None

    def test_reload(self, env):
        cfg, kb, skills_dir = env
        reg = SkillRegistry(cfg, skills_dir)
        assert reg.discover() == []
        make_skill(skills_dir, "later")
        assert len(reg.reload()) == 1

    def test_stats_and_format(self, env):
        cfg, kb, skills_dir = env
        make_skill(skills_dir, "alpha")
        reg = SkillRegistry(cfg, skills_dir)
        reg.discover()
        st = reg.stats()
        assert st["total"] == 1 and st["usable"] == 1
        assert "alpha" in format_entries(reg.list())
        assert format_entries([]) == "（暂无 Skill）"


# ===========================================================================
# 执行器
# ===========================================================================
class FakeLLM:
    def __init__(self, reply: str = "根据片段，结论如下 [P1 p.1]。"):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, messages, task=None, **kwargs):
        self.calls.append(messages)
        return self.reply


class TestRunner:
    def _setup(self, env, **over):
        cfg, kb, skills_dir = env
        make_skill(skills_dir, "demo", **over)
        reg = SkillRegistry(cfg, skills_dir)
        reg.discover()
        return cfg, kb, reg

    def test_missing_required_input(self, env):
        cfg, kb, reg = self._setup(env)
        r = SkillRunner(kb, cfg, llm=FakeLLM(), registry=reg).run("demo", {})
        assert not r.ok and "缺少必填输入" in r.error

    def test_unknown_skill(self, env):
        cfg, kb, reg = self._setup(env)
        r = SkillRunner(kb, cfg, llm=FakeLLM(), registry=reg).run("nope", {"topic": "x"})
        assert not r.ok and "未找到" in r.error

    def test_invalid_skill_rejected(self, env):
        cfg, kb, skills_dir = env
        make_skill(skills_dir, "bad", manifest={"capabilities": ["nope"]})
        reg = SkillRegistry(cfg, skills_dir)
        reg.discover()
        r = SkillRunner(kb, cfg, llm=FakeLLM(), registry=reg).run("bad", {"topic": "x"})
        assert not r.ok and "不可用" in r.error

    def test_dry_run_does_not_call_model(self, env):
        cfg, kb, reg = self._setup(env)
        llm = FakeLLM()
        r = SkillRunner(kb, cfg, llm=llm, registry=reg).run("demo", {"topic": "注意力"}, dry_run=True)
        assert r.ok and not llm.calls
        assert "dry-run" in r.answer

    def test_global_constraint_is_prepended(self, env):
        """⭐ 全局学术规范由框架强制前置，Skill 作者无法移除。"""
        from litprism.prompts import SYSTEM_ACADEMIC

        cfg, kb, reg = self._setup(env)
        llm = FakeLLM()
        SkillRunner(kb, cfg, llm=llm, registry=reg).run("demo", {"topic": "注意力"})
        system = llm.calls[0][0]["content"]
        assert system.startswith(SYSTEM_ACADEMIC[:60])
        assert "当前任务由 Skill 定义" in system

    def test_template_variables_rendered(self, env):
        cfg, kb, reg = self._setup(env)
        llm = FakeLLM()
        SkillRunner(kb, cfg, llm=llm, registry=reg).run("demo", {"topic": "稀疏注意力"})
        system = llm.calls[0][0]["content"]
        assert "稀疏注意力" in system
        assert "{{topic}}" not in system

    def test_citation_check_passes_with_valid_tag(self, env):
        cfg, kb, reg = self._setup(env)
        # 先塞一条能被检索到的分块，让 excerpts 非空
        pid = kb.add_paper(title="Attention Paper")
        kb.add_chunks(pid, [{"text": "注意力机制的核心是缩放点积。", "page": 1, "section": "方法"}])
        r = SkillRunner(kb, cfg, llm=FakeLLM("结论 [P1 p.1]。"), registry=reg).run(
            "demo", {"topic": "注意力机制"}
        )
        assert r.ok
        assert r.excerpts, "应检索到片段"
        assert r.citation_check.passed, r.citation_check.issues

    def test_citation_check_flags_wrong_page(self, env):
        cfg, kb, reg = self._setup(env)
        pid = kb.add_paper(title="Attention Paper")
        kb.add_chunks(pid, [{"text": "注意力机制的核心是缩放点积。", "page": 1, "section": "方法"}])
        r = SkillRunner(kb, cfg, llm=FakeLLM("结论 [P1 p.99]。"), registry=reg).run(
            "demo", {"topic": "注意力机制"}
        )
        assert r.ok
        assert not r.citation_check.passed
        assert any("99" in i for i in r.citation_check.issues)

    def test_citation_check_flags_missing_tags(self, env):
        cfg, kb, reg = self._setup(env)
        pid = kb.add_paper(title="Attention Paper")
        kb.add_chunks(pid, [{"text": "注意力机制的核心是缩放点积。", "page": 1, "section": "方法"}])
        r = SkillRunner(kb, cfg, llm=FakeLLM("结论 [P9 p.1]。"), registry=reg).run(
            "demo", {"topic": "注意力机制"}
        )
        assert not r.citation_check.passed
        assert any("P9" in i for i in r.citation_check.issues)

    def test_uncited_answer_flagged(self, env):
        cfg, kb, reg = self._setup(env)
        pid = kb.add_paper(title="Attention Paper")
        kb.add_chunks(pid, [{"text": "注意力机制的核心是缩放点积。", "page": 1, "section": "方法"}])
        r = SkillRunner(kb, cfg, llm=FakeLLM("这是一个完全没有引用标记的回答。"), registry=reg).run(
            "demo", {"topic": "注意力机制"}
        )
        assert not r.citation_check.passed
        assert any("没有任何来源标记" in i for i in r.citation_check.issues)

    def test_deliverable_saved(self, env):
        cfg, kb, reg = self._setup(env, manifest={
            "output": {"type": "deliverable", "deliverable_type": "review", "citation_required": False},
        })
        r = SkillRunner(kb, cfg, llm=FakeLLM("综述正文"), registry=reg).run(
            "demo", {"topic": "注意力"}
        )
        assert r.ok and r.deliverable_id
        row = kb._con.execute("SELECT * FROM deliverables WHERE id = ?", (r.deliverable_id,)).fetchone()
        assert row["type"] == "review" and row["skill_id"] == "demo"

    def test_undeclared_capability_not_executed(self, env):
        """⭐ 未在 capabilities 声明的能力绝不放行 —— 校验已拦，运行时再兜一层。"""
        cfg, kb, skills_dir = env
        # 手工构造一个「校验能过但 context 里多写了能力」的清单（正常流程校验会拦）
        d = make_skill(skills_dir, "sneaky", manifest={
            "capabilities": ["search_passages"],
            "context": [
                {"capability": "search_passages", "query": "{{topic}}", "top_k": 2},
                {"capability": "get_graph"},
            ],
        })
        m = load_manifest(d)
        # 绕过校验，直接看 runner 的行为
        from litprism.skills.registry import SkillEntry
        from litprism.skills.validate import ValidationResult

        reg = SkillRegistry(cfg, skills_dir)
        reg._entries = {"sneaky": SkillEntry(manifest=m, validation=ValidationResult(skill_id="sneaky"))}
        reg._loaded = True

        r = SkillRunner(kb, cfg, llm=FakeLLM("x"), registry=reg).run("sneaky", {"topic": "注意力"})
        assert "get_graph" not in r.prefetched, "未声明的能力不得执行"

    def test_format_result(self, env):
        cfg, kb, reg = self._setup(env)
        r = SkillRunner(kb, cfg, llm=FakeLLM("x"), registry=reg).run("demo", {"topic": "a"})
        assert "执行完成" in format_result(r)
        assert "执行失败" in format_result(
            type(r)(ok=False, skill_id="x", error="boom")
        )


class TestTemplateRendering:
    def test_renders_all_vars(self):
        assert _render_template("A {{x}} B {{ y }}", {"x": "1", "y": "2"}) == "A 1 B 2"

    def test_missing_var_marked(self):
        out = _render_template("值：{{x}}", {})
        assert "未提供 x" in out

    def test_no_vars(self):
        assert _render_template("纯文本", {}) == "纯文本"
