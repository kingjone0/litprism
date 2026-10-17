"""Skill 清单规范（skill.yaml）。

目录结构::

    <数据目录>/skills/<skill-id>/
        skill.yaml      清单（必需）
        prompt.md       提示词模板（必需）
        README.md       可选说明

**Skill 目录只读加载，框架不执行目录内任何脚本。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# 固定能力表（不可扩展）
# ---------------------------------------------------------------------------
CAPABILITIES: dict[str, str] = {
    "search_passages": "检索原文片段（含 paper_id / page / section / text）",
    "search_papers": "检索题录（含核验状态）",
    "get_analysis": "取结构化解读（研究问题/方法/实验/结论/创新点/局限）",
    "list_collections": "列出分组树与所含论文",
    "get_graph": "取知识图谱数据（节点 + 边）",
    "verify_paper": "核验论文真实性（五态判定 + 候选匹配 + 证据）",
    "compare_papers": "多篇论文对比（六维度矩阵）",
}

INPUT_TYPES = ("collection", "papers", "topic", "text", "number")

OUTPUT_TYPES = ("deliverable", "answer", "analysis")

DELIVERABLE_TYPES = ("review", "comparison", "notes", "peer_review")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass
class SkillInput:
    key: str
    label: str = ""
    type: str = "text"
    required: bool = False
    placeholder: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label or self.key,
            "type": self.type,
            "required": self.required,
            "placeholder": self.placeholder,
        }


@dataclass
class ContextSpec:
    """声明式上下文预取。

    一期用「声明式预取」而非「工具调用循环」：Skill 写清需要哪些检索，
    框架在调用模型前一次性取好并注入。理由是**防幻觉优先于灵活度** ——
    证据集中在上下文里，引用校验才能精确；工具循环下证据散在多轮对话里。
    """

    capability: str
    args: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"capability": self.capability, **self.args}


@dataclass
class SkillManifest:
    id: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    schema_version: int = SCHEMA_VERSION
    trigger: dict = field(default_factory=dict)
    inputs: list[SkillInput] = field(default_factory=list)
    context: list[ContextSpec] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    output: dict = field(default_factory=dict)
    model: dict = field(default_factory=dict)
    # 运行时填充
    path: Path | None = None
    prompt: str = ""
    raw: dict = field(default_factory=dict)
    load_error: str = ""

    # ---- 便捷属性 ----
    @property
    def prefix(self) -> str:
        return str(self.trigger.get("prefix") or "")

    @property
    def keywords(self) -> list[str]:
        return [str(k) for k in (self.trigger.get("keywords") or [])]

    @property
    def output_type(self) -> str:
        return str(self.output.get("type") or "answer")

    @property
    def deliverable_type(self) -> str:
        return str(self.output.get("deliverable_type") or "notes")

    @property
    def citation_required(self) -> bool:
        return bool(self.output.get("citation_required", True))

    @property
    def ok(self) -> bool:
        return not self.load_error

    def declared_vars(self) -> set[str]:
        """``prompt.md`` 里用到的全部 ``{{var}}``。"""
        return set(_VAR_RE.findall(self.prompt or ""))

    def input_keys(self) -> set[str]:
        return {i.key for i in self.inputs}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "schema_version": self.schema_version,
            "trigger": self.trigger,
            "inputs": [i.to_dict() for i in self.inputs],
            "context": [c.to_dict() for c in self.context],
            "capabilities": self.capabilities,
            "output": self.output,
            "model": self.model,
            "path": str(self.path) if self.path else None,
            "ok": self.ok,
            "load_error": self.load_error,
        }


class ManifestError(ValueError):
    """清单无法解析。"""


def parse_manifest(raw: dict, *, path: Path | None = None) -> SkillManifest:
    """把 yaml 字典解析成清单对象。**不做校验** —— 校验交给 validate.py。"""
    if not isinstance(raw, dict):
        raise ManifestError(f"清单必须是映射（dict），收到 {type(raw).__name__}")

    sid = str(raw.get("id") or "").strip()

    inputs: list[SkillInput] = []
    for item in raw.get("inputs") or []:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        inputs.append(
            SkillInput(
                key=str(item["key"]),
                label=str(item.get("label") or ""),
                type=str(item.get("type") or "text"),
                required=bool(item.get("required", False)),
                placeholder=str(item.get("placeholder") or ""),
            )
        )

    context: list[ContextSpec] = []
    for item in raw.get("context") or []:
        if not isinstance(item, dict) or not item.get("capability"):
            continue
        args = {k: v for k, v in item.items() if k != "capability"}
        context.append(ContextSpec(capability=str(item["capability"]), args=args))

    caps = [str(c) for c in (raw.get("capabilities") or [])]

    return SkillManifest(
        id=sid,
        name=str(raw.get("name") or sid),
        version=str(raw.get("version") or "1.0.0"),
        description=str(raw.get("description") or ""),
        author=str(raw.get("author") or ""),
        schema_version=int(raw.get("schema_version") or 0),
        trigger=raw.get("trigger") or {},
        inputs=inputs,
        context=context,
        capabilities=caps,
        output=raw.get("output") or {},
        model=raw.get("model") or {},
        path=path,
        raw=raw,
    )


def load_manifest(skill_dir: Path) -> SkillManifest:
    """从目录加载清单与提示词。**任何异常都转成 load_error，不向外抛。**"""
    skill_dir = Path(skill_dir)
    manifest_path = skill_dir / "skill.yaml"
    prompt_path = skill_dir / "prompt.md"

    try:
        if not manifest_path.exists():
            raise ManifestError(f"缺少 skill.yaml（目录：{skill_dir}）")
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        manifest = parse_manifest(raw, path=skill_dir)
    except Exception as e:  # noqa: BLE001 - 加载失败不应让应用启动失败
        placeholder = SkillManifest(
            id=skill_dir.name,
            name=skill_dir.name,
            path=skill_dir,
            load_error=f"{type(e).__name__}: {e}",
        )
        return placeholder

    try:
        if prompt_path.exists():
            manifest.prompt = prompt_path.read_text(encoding="utf-8")
        else:
            manifest.load_error = "缺少 prompt.md"
    except Exception as e:  # noqa: BLE001
        manifest.load_error = f"读取 prompt.md 失败：{type(e).__name__}: {e}"

    return manifest
