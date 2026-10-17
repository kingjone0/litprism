"""Skill 注册表 —— 发现、加载、校验。

扫描 ``<数据目录>/skills/*/skill.yaml``，逐个加载并校验。
**加载失败不影响应用启动** —— 错误在设置页可见。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from .manifest import SkillManifest, load_manifest
from .validate import ValidationResult, validate


@dataclass
class SkillEntry:
    manifest: SkillManifest
    validation: ValidationResult

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def ok(self) -> bool:
        """校验通过才可用。"""
        return self.manifest.ok and self.validation.ok

    @property
    def status(self) -> str:
        if not self.manifest.ok:
            return "加载失败"
        if not self.validation.ok:
            return "校验失败"
        if self.validation.warnings:
            return "可用（有警告）"
        return "正常"

    def to_dict(self) -> dict:
        d = self.manifest.to_dict()
        d.update(
            {
                "status": self.status,
                "ok": self.ok,
                "validation": {
                    "ok": self.validation.ok,
                    "summary": self.validation.summary(),
                    "errors": [str(i) for i in self.validation.errors],
                    "warnings": [str(i) for i in self.validation.warnings],
                },
            }
        )
        return d


class SkillRegistry:
    """Skill 注册表。"""

    def __init__(self, cfg: Config, skills_dir: Path | None = None):
        self.cfg = cfg
        self._dir = Path(skills_dir) if skills_dir else Path(cfg.data_path) / "skills"
        self._entries: dict[str, SkillEntry] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    @property
    def skills_dir(self) -> Path:
        return self._dir

    def ensure_dir(self) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    # ------------------------------------------------------------------
    def discover(self, *, force: bool = False) -> list[SkillEntry]:
        """扫描目录并加载。结果缓存，``force=True`` 强制重新扫描。"""
        if self._loaded and not force:
            return self.list()

        entries: dict[str, SkillEntry] = {}
        if self._dir.exists():
            for child in sorted(self._dir.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                manifest = load_manifest(child)
                entries[manifest.id or child.name] = SkillEntry(
                    manifest=manifest,
                    validation=validate(manifest),
                )
        self._entries = entries
        self._loaded = True
        return self.list()

    def reload(self) -> list[SkillEntry]:
        return self.discover(force=True)

    # ------------------------------------------------------------------
    def list(self, *, usable_only: bool = False) -> list[SkillEntry]:
        items = sorted(self._entries.values(), key=lambda e: e.name)
        if usable_only:
            items = [e for e in items if e.ok]
        return items

    def get(self, skill_id: str) -> SkillEntry | None:
        return self._entries.get(skill_id)

    def match(self, text: str) -> SkillEntry | None:
        """按触发前缀或关键词匹配。"""
        text = (text or "").strip()
        if not text:
            return None
        for e in self.list(usable_only=True):
            prefix = e.manifest.prefix
            if prefix and text.startswith(prefix):
                return e
        lowered = text.lower()
        for e in self.list(usable_only=True):
            if any(k.lower() in lowered for k in e.manifest.keywords):
                return e
        return None

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        items = self.list()
        return {
            "total": len(items),
            "usable": sum(1 for e in items if e.ok),
            "failed": sum(1 for e in items if not e.ok),
            "dir": str(self._dir),
            "empty": len(items) == 0,
        }

    def empty_state_text(self) -> str:
        return (
            "尚未添加 Skill。\n"
            f"Skill 是声明式的提示词扩展（不含可执行代码），放在：{self._dir}\n"
            "每个 Skill 是一个子目录，内含 skill.yaml 与 prompt.md。"
        )


def format_entries(entries: list[SkillEntry]) -> str:
    if not entries:
        return "（暂无 Skill）"
    lines = []
    for e in entries:
        mark = "●" if e.ok else "✗"
        lines.append(f"  {mark} {e.id:<24} v{e.version:<8} {e.status}")
        if e.manifest.description:
            lines.append(f"      {e.manifest.description}")
        if not e.ok:
            for issue in e.validation.issues:
                lines.append(f"      {issue}")
    return "\n".join(lines)
