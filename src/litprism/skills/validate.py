"""Skill 校验器。

设计取向：**能拒绝的早拒绝，不确定的只警告。**
校验失败不静默降级 —— 静默降级会让用户以为 Skill 生效了，实际行为不同，
这比直接报错更糟。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .manifest import (
    CAPABILITIES,
    DELIVERABLE_TYPES,
    INPUT_TYPES,
    OUTPUT_TYPES,
    SCHEMA_VERSION,
    SkillManifest,
)

_ID_PATTERN_DOC = "小写字母或数字开头，只含小写字母/数字/连字符，长度 2–49"


@dataclass
class Issue:
    code: str
    message: str
    severity: str = "error"      # error | warning
    where: str = ""

    def __str__(self) -> str:
        mark = "✗" if self.severity == "error" else "!"
        loc = f" @ {self.where}" if self.where else ""
        return f"{mark} [{self.code}]{loc} {self.message}"


@dataclass
class ValidationResult:
    skill_id: str = ""
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def add(self, code: str, message: str, severity: str = "error", where: str = "") -> None:
        self.issues.append(Issue(code, message, severity, where))

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "校验通过"
        parts = []
        if self.errors:
            parts.append(f"{len(self.errors)} 项错误")
        if self.warnings:
            parts.append(f"{len(self.warnings)} 项警告")
        return "、".join(parts)

    def render(self) -> str:
        if not self.issues:
            return f"✅ {self.skill_id or 'skill'} 校验通过"
        return "\n".join(str(i) for i in self.issues)


def validate(manifest: SkillManifest) -> ValidationResult:
    """校验一份清单。**返回全部问题（不短路）**，便于一次性给出完整诊断。"""
    r = ValidationResult(skill_id=manifest.id)

    # ---- 加载期已有错误：直接报告，不再往下查 ----
    if manifest.load_error:
        r.add("load.failed", manifest.load_error)
        return r

    # ---- schema_version ----
    if manifest.schema_version != SCHEMA_VERSION:
        r.add(
            "schema.version",
            f"schema_version={manifest.schema_version} 与框架支持的版本 {SCHEMA_VERSION} 不匹配。"
            "请按当前版本的规范改写清单。",
            where="schema_version",
        )

    # ---- id ----
    if not manifest.id:
        r.add("id.missing", "缺少 id。", where="id")
    elif not _id_ok(manifest.id):
        r.add(
            "id.pattern",
            f"id {manifest.id!r} 不合法。要求：{_ID_PATTERN_DOC}。",
            where="id",
        )
    elif manifest.path is not None and manifest.id != manifest.path.name:
        r.add(
            "id.dirname",
            f"id {manifest.id!r} 与目录名 {manifest.path.name!r} 不一致。"
            "框架以目录名为准发现 Skill，不一致会导致引用错乱。",
            where="id",
        )

    # ---- name / prompt ----
    if not (manifest.name or "").strip():
        r.add("name.empty", "缺少 name（界面显示名）。", severity="warning", where="name")
    if not (manifest.prompt or "").strip():
        r.add("prompt.empty", "prompt.md 为空。", where="prompt.md")

    # ---- capabilities ----
    if not manifest.capabilities:
        r.add(
            "capabilities.empty",
            "未声明任何 capability。若 Skill 不需要检索上下文，请显式写空列表并说明用途。",
            severity="warning",
            where="capabilities",
        )
    illegal = [c for c in manifest.capabilities if c not in CAPABILITIES]
    if illegal:
        r.add(
            "capabilities.illegal",
            f"含非法 capability：{illegal}。可用能力：{sorted(CAPABILITIES)}。"
            "能力表固定不可扩展 —— Skill 不能自带实现。",
            where="capabilities",
        )

    # ---- context 里的 capability 也必须合法 ----
    for i, spec in enumerate(manifest.context):
        if spec.capability not in CAPABILITIES:
            r.add(
                "context.illegal",
                f"context[{i}] 使用了非法 capability {spec.capability!r}。",
                where=f"context[{i}]",
            )
        elif spec.capability not in manifest.capabilities:
            r.add(
                "context.undeclared",
                f"context[{i}] 使用了 {spec.capability!r}，但未在 capabilities 中声明。",
                severity="warning",
                where=f"context[{i}]",
            )

    # ---- inputs ----
    seen_keys: set[str] = set()
    for i, inp in enumerate(manifest.inputs):
        if inp.key in seen_keys:
            r.add("inputs.duplicate", f"input key {inp.key!r} 重复。", where=f"inputs[{i}]")
        seen_keys.add(inp.key)
        if inp.type not in INPUT_TYPES:
            r.add(
                "inputs.type",
                f"input {inp.key!r} 的 type {inp.type!r} 不合法。可用：{list(INPUT_TYPES)}。",
                where=f"inputs[{i}]",
            )

    # ---- prompt 变量必须已声明 ----
    used = manifest.declared_vars()
    undeclared = sorted(used - manifest.input_keys())
    if undeclared:
        r.add(
            "prompt.undeclared_var",
            f"prompt.md 引用了未声明的变量：{undeclared}。"
            "请在 inputs 里声明，否则渲染时会留下未替换的占位符。",
            where="prompt.md",
        )

    # ---- required 的输入最好被 prompt 用到 ----
    unused = sorted(
        {i.key for i in manifest.inputs if i.required} - used
    )
    if unused:
        r.add(
            "inputs.unused",
            f"required 输入 {unused} 未在 prompt.md 中被引用。"
            "用户填了却用不上，会让输入表单显得多余。",
            severity="warning",
            where="inputs",
        )

    # ---- output ----
    if manifest.output_type not in OUTPUT_TYPES:
        r.add(
            "output.type",
            f"output.type {manifest.output_type!r} 不合法。可用：{list(OUTPUT_TYPES)}。",
            where="output.type",
        )
    if manifest.output_type == "deliverable" and manifest.deliverable_type not in DELIVERABLE_TYPES:
        r.add(
            "output.deliverable_type",
            f"output.deliverable_type {manifest.deliverable_type!r} 不合法。"
            f"可用：{list(DELIVERABLE_TYPES)}。",
            where="output.deliverable_type",
        )

    # ---- 引用要求 ----
    if manifest.citation_required:
        prompt = manifest.prompt or ""
        if not _mentions_citation(prompt):
            r.add(
                "output.citation_hint",
                "citation_required=true，但 prompt.md 未说明引用标注格式（如 [P1 p.3]）。"
                "框架会强制校验引用，但提示词里写清格式能显著提高通过率。",
                severity="warning",
                where="prompt.md",
            )

    # ---- 代码：明确禁止 ----
    skill_dir = manifest.path
    if skill_dir is not None and skill_dir.exists():
        code_files = [
            p.name for p in skill_dir.iterdir()
            if p.is_file() and p.suffix.lower() in (".py", ".js", ".sh", ".bat", ".ps1", ".exe", ".dll")
        ]
        if code_files:
            r.add(
                "security.code_file",
                f"目录内发现可执行文件：{code_files}。"
                "一期只支持声明式 Skill（提示词 + 配置），不支持自定义代码 —— "
                "允许携带代码等于开放任意代码执行入口。这些文件不会被加载或执行。",
                severity="warning",
                where=str(skill_dir),
            )

    return r


def _id_ok(sid: str) -> bool:
    import re

    return bool(re.match(r"^[a-z0-9][a-z0-9-]{1,48}$", sid))


def _mentions_citation(prompt: str) -> bool:
    """提示词里是否**说明了引用标注格式**。

    刻意不用"引用"这个词做判断 —— 像"不提引用"这样的否定表述会被误判为已说明。
    检查格式标记更可靠：模型需要知道的是"怎么标"，而不是"要不要标"。
    """
    markers = ("[P", "chunk_id", "页码", "来源标记", "citation", "p.<", "p.##")
    return any(m in prompt for m in markers)


def format_result(r: ValidationResult) -> str:
    if not r.issues:
        return f"✅ {r.skill_id} 校验通过"
    lines = [f"{'❌' if not r.ok else '⚠️'} {r.skill_id}：{r.summary()}"]
    lines.extend(f"  {i}" for i in r.issues)
    return "\n".join(lines)
