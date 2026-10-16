"""记忆的硬边界（Guards）。

这三条**不靠提示词请求**，而是做成可执行检查：

1. 记忆只能影响表达偏好，不能影响事实判断
2. 记忆内容不得作为引用来源
3. 记忆不得改变核验结论

做法分两层：

* **注入层**：任何把记忆放进上下文的路径，都必须经过 :func:`guard_text` 拼接约束说明，
  并把记忆标记为 ``preference`` 类型（区别于 ``evidence``）。
* **校验层**：回答产出后，:func:`detect_memory_as_fact` 扫描是否出现
  "根据我的记忆……" 这类把记忆当事实的表述；
  :func:`validate_citations` 拒绝把记忆 id 当作引用来源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# 注入时必须前置的约束说明（不可省略、不可被调用方覆盖）
MEMORY_GUARD_TEXT = """【关于下方"用户偏好"的使用约束】
以下是用户本人设定的偏好，只能用于调整**表达方式**（用词、详略、排序、格式）。
严禁用于以下任何用途：
- 不得作为事实依据。禁止出现"根据我的记忆……""你之前说过……因此这篇论文……"这类表述。
- 不得作为引用来源。引用只能来自知识库中的原文片段或已核验文献。
- 不得改变任何核验结论。用户说"这篇我确认过"不能替代白名单里的留痕确认流程。
若偏好与文献内容冲突，一律以文献为准，并说明冲突。"""

# 记忆注入时使用的分隔标记
MEMORY_BLOCK_OPEN = "<user-preferences>"
MEMORY_BLOCK_CLOSE = "</user-preferences>"


@dataclass
class GuardViolation:
    code: str
    message: str
    where: str = ""
    severity: str = "error"


@dataclass
class GuardResult:
    violations: list[GuardViolation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(v.severity == "error" for v in self.violations)

    def add(self, code: str, message: str, where: str = "", severity: str = "error") -> None:
        self.violations.append(GuardViolation(code, message, where, severity))

    def summary(self) -> str:
        if not self.violations:
            return "通过"
        return "；".join(f"[{v.code}] {v.message}" for v in self.violations)


# ---------------------------------------------------------------------------
# 注入层
# ---------------------------------------------------------------------------
def render_memory_block(entries: Iterable[dict]) -> str:
    """把记忆渲染成带约束说明的注入块。

    调用方**不得**自行拼接记忆文本 —— 必须走这里，否则约束说明会被绕过。
    """
    items = [e for e in entries if (e.get("value") or "").strip()]
    if not items:
        return ""
    lines = [MEMORY_GUARD_TEXT, "", MEMORY_BLOCK_OPEN]
    for e in items:
        kind = e.get("kind") or "misc"
        key = e.get("key")
        label = f"{key}" if key else _kind_label(kind)
        lines.append(f"- [{label}] {e['value']}")
    lines.append(MEMORY_BLOCK_CLOSE)
    return "\n".join(lines)


def _kind_label(kind: str) -> str:
    return {
        "research_interest": "研究方向",
        "term_preference": "术语偏好",
        "style_preference": "表达偏好",
        "misc": "其他偏好",
    }.get(kind, kind)


# ---------------------------------------------------------------------------
# 校验层
# ---------------------------------------------------------------------------
# 把记忆当事实的典型措辞
_MEMORY_AS_FACT_PATTERNS = [
    re.compile(r"根据(?:我的)?记忆"),
    re.compile(r"你(?:之前|上次|曾经)(?:说过|提到过|告诉过我)"),
    re.compile(r"按你(?:说|讲)的"),
    re.compile(r"我记得你"),
    re.compile(r"based on (?:my|your) memory", re.I),
    re.compile(r"as you (?:told|mentioned) me", re.I),
]

# 把记忆当作引用来源的标记
_MEMORY_CITE_PATTERNS = [
    re.compile(r"\[mem[:_][^\]]+\]", re.I),
    re.compile(r"\[偏好[^\]]*\]"),
]


def detect_memory_as_fact(text: str) -> GuardResult:
    """扫描回答中是否把记忆当成了事实依据。"""
    r = GuardResult()
    if not text:
        return r
    for pat in _MEMORY_AS_FACT_PATTERNS:
        m = pat.search(text)
        if m:
            r.add(
                "mem.as_fact",
                f"出现了把用户偏好当事实依据的表述「{m.group(0)}」。"
                "记忆只能影响表达方式，不能作为事实来源。",
                where=m.group(0),
            )
    return r


def validate_citations(
    text: str,
    *,
    valid_excerpt_tags: set[str] | None = None,
    memory_ids: set[str] | None = None,
) -> GuardResult:
    """在常规引用校验之上，额外拒绝「把记忆当引用来源」。"""
    r = GuardResult()
    if not text:
        return r

    for pat in _MEMORY_CITE_PATTERNS:
        m = pat.search(text)
        if m:
            r.add(
                "mem.as_citation",
                f"引用了记忆条目「{m.group(0)}」。记忆不得作为引用来源 —— "
                "引用只能来自原文片段或已核验文献。",
                where=m.group(0),
            )

    if memory_ids:
        from ..textutil import parse_citations

        cited = {tag.upper() for tag, _ in parse_citations(text)}
        for mid in memory_ids:
            if mid.upper() in cited:
                r.add(
                    "mem.id_cited",
                    f"引用了记忆条目 id {mid}。记忆不得作为引用来源。",
                    where=mid,
                )
    return r


def assert_memory_preference_only(kind: str) -> None:
    """记忆写入时的类型守卫。

    只允许偏好类 kind —— 防止有人把"事实性内容"塞进记忆层，
    从而绕过核验流程。
    """
    allowed = {"research_interest", "term_preference", "style_preference", "misc"}
    if kind not in allowed:
        raise ValueError(
            f"记忆类型 {kind!r} 不被允许。只接受偏好类：{sorted(allowed)}。"
            "事实性内容必须走知识库（经核验），不能进记忆层。"
        )


def format_result(r: GuardResult) -> str:
    if not r.violations:
        return "✅ 记忆边界检查通过"
    return "\n".join(f"  {'✗' if v.severity == 'error' else '!'} [{v.code}] {v.message}" for v in r.violations)
