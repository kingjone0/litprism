"""生成式 UI（`lit-ui`）的围栏解析、校验与**框架注入**。

设计要点
--------
模型在回复里输出 ``lit-ui`` 围栏 JSON，前端渲染成结构化组件。但有两类字段
**不接受模型输入**，由框架查库填充：

1. **核验徽章** —— 模型只给 ``paper_id``，框架填真实核验状态。
   否则模型可以把一篇 ``not_found`` 的论文渲染成「✅ 已核验」，
   等于在 UI 层开了个后门，把第三层防线的价值抹掉。
2. **引用页码** —— 模型只给 ``chunk_id``，框架从 ``chunks`` 表取出页码、章节、原文。
   这一条直接堵死「页码编造」这个幻觉通道 —— 模型连编页码的机会都没有，
   因为它根本不用填页码。

结论：生成式 UI 不但不削弱防幻觉，反而**多了一层结构性保障** ——
把原本靠模型自觉的字段，改成框架注入。这是纯文本渲染做不到的。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 组件白名单
# ---------------------------------------------------------------------------
COMPONENTS: dict[str, dict] = {
    "paper-card": {
        "desc": "单篇论文卡片：题录 + 核验徽章",
        "requires": ["paper_id"],
        "injected": ["title", "authors", "year", "venue", "verify_level", "verify_badge", "citable"],
    },
    "paper-list": {
        "desc": "论文列表（检索 / 推荐结果）",
        "requires": ["paper_ids"],
        "injected": [],
    },
    "analysis": {
        "desc": "结构化解读（六维度）",
        "requires": ["paper_id"],
        "injected": ["title", "verify_level", "verify_badge"],
    },
    "comparison-table": {
        "desc": "多篇对比矩阵",
        "requires": ["paper_ids"],
        "injected": [],
    },
    "concept-list": {
        "desc": "术语表",
        "requires": ["items"],
        "injected": [],
    },
    "citation-list": {
        "desc": "引用片段（页码、章节、原文由框架注入）",
        "requires": ["chunk_ids"],
        "injected": ["page", "section", "text", "paper_title", "paper_id"],
    },
    "verify-result": {
        "desc": "核验结果（五态 + 候选匹配 + 证据）",
        "requires": [],
        "injected": ["level", "badge", "message", "confidence", "matches"],
    },
    "callout": {
        "desc": "提示块：区分原文事实 / 作者主张 / 模型推断",
        "requires": ["text"],
        "injected": [],
    },
}

CALLOUT_KINDS = ("fact", "claim", "inference", "warn", "note")

# 限制（借鉴 dsh-genui 的健壮性规则）
MAX_NODES = 200
MAX_DEPTH = 8
MAX_TEXT_LEN = 4000
MAX_ITEMS = 60

_FENCE_RE = re.compile(r"```lit-ui[ \t]*\r?\n(.*?)```", re.S)


@dataclass
class Issue:
    code: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"{'✗' if self.severity == 'error' else '!'} [{self.code}] {self.message}"


@dataclass
class Fence:
    """一个解析出来的围栏。"""

    raw: str
    start: int
    end: int
    component: dict | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.component is not None and not self.error


@dataclass
class Processed:
    """处理后的消息：文本段与组件段交替。"""

    segments: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    degraded: bool = False          # 是否有围栏降级为纯文本

    @property
    def has_component(self) -> bool:
        return any(s["kind"] == "component" for s in self.segments)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_fences(text: str) -> list[Fence]:
    """找出全部 ``lit-ui`` 围栏。**解析失败不抛异常**，标记在 ``Fence.error`` 上。"""
    out: list[Fence] = []
    for m in _FENCE_RE.finditer(text or ""):
        body = m.group(1).strip()
        fence = Fence(raw=body, start=m.start(), end=m.end())
        if not body:
            fence.error = "围栏内容为空"
            out.append(fence)
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            fence.error = f"JSON 解析失败：{e.msg}（第 {e.lineno} 行第 {e.colno} 列）"
            out.append(fence)
            continue
        if not isinstance(data, dict):
            fence.error = f"围栏内容必须是 JSON 对象，收到 {type(data).__name__}"
            out.append(fence)
            continue
        fence.component = data
        out.append(fence)
    return out


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate_component(comp: dict) -> list[Issue]:
    """校验单个组件。返回问题列表（空 = 通过）。"""
    issues: list[Issue] = []

    ctype = comp.get("type")
    if not ctype:
        return [Issue("type.missing", "缺少 type 字段")]
    if ctype not in COMPONENTS:
        return [
            Issue(
                "type.unknown",
                f"未知组件 {ctype!r}。可用：{sorted(COMPONENTS)}。"
                "组件是白名单制的 —— 模型无法注入 HTML 或脚本。",
            )
        ]

    spec = COMPONENTS[ctype]
    for key in spec["requires"]:
        if key not in comp:
            issues.append(Issue("field.missing", f"{ctype} 缺少必需字段 {key!r}"))

    # 节点与深度限制
    nodes, depth = _measure(comp)
    if nodes > MAX_NODES:
        issues.append(Issue("limit.nodes", f"节点数 {nodes} 超过上限 {MAX_NODES}", "warning"))
    if depth > MAX_DEPTH:
        issues.append(Issue("limit.depth", f"嵌套深度 {depth} 超过上限 {MAX_DEPTH}", "warning"))

    # callout 的 kind
    if ctype == "callout" and comp.get("kind") and comp["kind"] not in CALLOUT_KINDS:
        issues.append(
            Issue("callout.kind", f"kind {comp['kind']!r} 不合法。可用：{list(CALLOUT_KINDS)}", "warning")
        )

    return issues


def _measure(obj: Any, depth: int = 1) -> tuple[int, int]:
    """统计节点数与最大深度。"""
    if isinstance(obj, dict):
        n, d = 1, depth
        for v in obj.values():
            sn, sd = _measure(v, depth + 1)
            n += sn
            d = max(d, sd)
        return n, d
    if isinstance(obj, list):
        n, d = 1, depth
        for v in obj:
            sn, sd = _measure(v, depth + 1)
            n += sn
            d = max(d, sd)
        return n, d
    return 1, depth


def sanitize_text(text: Any, limit: int = MAX_TEXT_LEN) -> str:
    """字符串截断。防止模型输出超长单字段把页面撑爆。"""
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------------------
# 框架注入
# ---------------------------------------------------------------------------
def enrich(comp: dict, kb: Any) -> dict:
    """按组件类型注入框架字段。**这些字段以数据库为准，模型给的值一律覆盖。**"""
    ctype = comp.get("type")
    out = dict(comp)

    if ctype == "paper-card":
        p = _get_paper(kb, comp.get("paper_id"))
        if p:
            out.update(_paper_fields(p))
        else:
            out["_missing"] = True
            out.setdefault("title", f"（未找到论文 {comp.get('paper_id')}）")

    elif ctype == "paper-list":
        ids = comp.get("paper_ids") or []
        items = []
        for pid in ids[:MAX_ITEMS]:
            p = _get_paper(kb, pid)
            items.append(_paper_fields(p) if p else {"id": pid, "_missing": True, "title": f"（未找到 {pid}）"})
        out["items"] = items

    elif ctype == "analysis":
        p = _get_paper(kb, comp.get("paper_id"))
        if p:
            for k in ("title", "verify_level", "verify_badge"):
                out[k] = _paper_fields(p)[k]
        payload = ((kb.get_analysis(p['id']) if p else None) or {}).get('payload') or {}
        out['_mock'] = bool(payload.get('_mock'))
        out['_model'] = payload.get('_model')
        fields = []
        if p and _validated_analysis(kb, p['id']):
            for key in ('research_question', 'methodology', 'experiment_design', 'key_conclusions', 'innovations', 'limitations'):
                if payload.get(key):
                    fields.append({'key': key, 'text': sanitize_text(payload[key])})
        out["fields"] = fields

    elif ctype == "comparison-table":
        ids = comp.get("paper_ids") or []
        out["papers"] = [
            {"id": pid, **{k: v for k, v in _paper_fields(p).items() if k in ("title", "year", "verify_badge")}}
            if (p := _get_paper(kb, pid)) else {"id": pid, "title": f"（未找到 {pid}）"}
            for pid in ids[:8]
        ]
        dims = []
        for d in (comp.get("dimensions") or [])[:10]:
            if isinstance(d, dict):
                dims.append(
                    {
                        "key": str(d.get("key") or ""),
                        "label": str(d.get("label") or d.get("key") or ""),
                        "values": [sanitize_text(_validated_analysis(kb, pid).get(str(d.get('key') or '')) or '未提及', 600) for pid in ids[:8]],
                    }
                )
        out["dimensions"] = dims

    elif ctype == "citation-list":
        ids = comp.get("chunk_ids") or []
        items = []
        for cid in ids[:MAX_ITEMS]:
            chunk = _get_chunk(kb, cid)
            if chunk:
                items.append(
                    {
                        "chunk_id": cid,
                        "paper_id": chunk.get("paper_id"),
                        "paper_title": chunk.get("paper_title"),
                        # ⭐ 页码 / 章节 / 原文由框架注入 —— 模型连编的机会都没有
                        "page": chunk.get("page"),
                        "section": chunk.get("section"),
                        "text": sanitize_text(chunk.get("text")),
                        'source_kind': chunk.get('source_kind', 'paper'), 'url': chunk.get('url'),
                        'trust_level': chunk.get('trust_level'), 'citable': chunk.get('citable'),
                    }
                )
            else:
                items.append({"chunk_id": cid, "_missing": True})
        out["items"] = items

    elif ctype == "verify-result":
        out.update(_verify_fields(kb, comp))

    elif ctype == "concept-list":
        items = []
        for it in (comp.get("items") or [])[:MAX_ITEMS]:
            if isinstance(it, dict):
                items.append(
                    {"term": str(it.get("term") or ""), "definition": sanitize_text(it.get("definition"), 800)}
                )
        out["items"] = items

    elif ctype == "callout":
        out["text"] = sanitize_text(comp.get("text"))
        if out.get("kind") not in CALLOUT_KINDS:
            out["kind"] = "note"

    return out


def _validated_analysis(kb: Any, pid: str) -> dict:
    paper = _get_paper(kb, pid)
    if not paper or paper.get('verify_level') not in ('verified', 'manual_confirmed'):
        return {}
    payload = ((kb.get_analysis(pid) if kb else None) or {}).get('payload') or {}
    return payload if (payload.get('_citation_check') or {}).get('passed') else {}


def _get_paper(kb: Any, paper_id: Any) -> dict | None:
    if not paper_id or kb is None:
        return None
    try:
        return kb.get_paper(str(paper_id))
    except Exception:  # noqa: BLE001
        return None


def _get_chunk(kb: Any, chunk_id: Any) -> dict | None:
    if not chunk_id or kb is None:
        return None
    try:
        row = kb._con.execute(
            "SELECT c.id AS chunk_id, c.paper_id, c.page, c.section, c.text, p.title AS paper_title, p.url, p.verify_level AS trust_level, p.deleted_at"
            " FROM chunks c LEFT JOIN papers p ON p.id = c.paper_id WHERE c.id = ?",
            (str(chunk_id),),
        ).fetchone()
        if row:
            data = dict(row)
            data['citable'] = not data['deleted_at'] and data['trust_level'] in ('verified', 'manual_confirmed')
            return data
        from .knowledge import source_chunk
        return source_chunk(kb, str(chunk_id))
    except Exception:  # noqa: BLE001
        return None


def _paper_fields(p: dict) -> dict:
    from .whitelist import badge, is_citable

    level = p.get("verify_level") or p.get("verify_status")
    return {
        "paper_id": p.get("id"),
        "title": p.get("display_title") or p.get("title"),
        "authors": p.get("authors") or [],
        "year": p.get("year"),
        "venue": p.get("venue"),
        "doi": p.get("doi"),
        # ⭐ 核验状态以数据库为准，不接受模型输入
        "verify_level": level,
        "verify_badge": badge(level),
        "citable": is_citable(level),
    }


def _verify_fields(kb: Any, comp: dict) -> dict:
    from .whitelist import badge, is_citable

    pid = comp.get("paper_id")
    p = _get_paper(kb, pid)
    if p is None and comp.get("title") and kb is not None:
        # 退化为按标题在本地库找
        try:
            for cand in kb.list_papers():
                if str(comp["title"]).lower() in (cand.get("title") or "").lower():
                    p = cand
                    break
        except Exception:  # noqa: BLE001
            p = None

    if p is None:
        return {
            "level": "unchecked",
            "badge": badge("unchecked"),
            "message": "本地库中没有这篇文献，无法给出核验结论。可用 verify 命令实时核验。",
            "confidence": 0.0,
            "matches": [],
        }

    level = p.get("verify_level") or "unchecked"
    evidence = p.get("verify_evidence") or {}
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            evidence = {}
    return {
        "paper_id": p.get("id"),
        "title": p.get("title"),
        "level": level,
        "badge": badge(level),
        "citable": is_citable(level),
        "message": evidence.get("message") or "",
        "confidence": p.get("verify_confidence") or 0.0,
        "matches": evidence.get("matches") or [],
    }


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------
def render_fallback(comp: dict) -> str:
    """把组件降级成纯文本。**绝不出现空白** —— 渲染失败也要让内容可读。"""
    ctype = comp.get("type")
    if ctype == "paper-card":
        return f"📄 {comp.get('title', '')}（{comp.get('year') or '年份未知'}）{comp.get('verify_badge', '')}"
    if ctype == "paper-list":
        return "\n".join(
            f"📄 {i.get('title', '')}（{i.get('year') or '年份未知'}）{i.get('verify_badge', '')}"
            for i in comp.get("items") or []
        )
    if ctype == "analysis":
        head = f"【{comp.get('title', '')} 的结构化解读】"
        body = "\n".join(f"· {f.get('key')}：{f.get('text')}" for f in comp.get("fields") or [])
        return f"{head}\n{body}"
    if ctype == "comparison-table":
        rows = []
        for d in comp.get("dimensions") or []:
            rows.append(f"· {d.get('label')}：{' | '.join(d.get('values') or [])}")
        return "【对比】\n" + "\n".join(rows)
    if ctype == "concept-list":
        return "【术语】\n" + "\n".join(f"· {i.get('term')}：{i.get('definition')}" for i in comp.get("items") or [])
    if ctype == "citation-list":
        return "【引用片段】\n" + "\n".join(
            f"· {i.get('paper_title') or i.get('paper_id')} p.{i.get('page')}：{sanitize_text(i.get('text'), 200)}"
            for i in comp.get("items") or []
        )
    if ctype == "verify-result":
        return f"【核验】{comp.get('badge', '')} {comp.get('message', '')}"
    if ctype == "callout":
        return f"【{comp.get('kind', 'note')}】{comp.get('text', '')}"
    return ""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def process(text: str, kb: Any = None) -> Processed:
    """解析 → 校验 → 注入 → 切分成文本段与组件段。

    渲染失败的围栏**降级为纯文本**并保留原始 JSON 提示，
    保证用户永远能看到内容，而不是一片空白。
    """
    result = Processed()
    fences = parse_fences(text)
    if not fences:
        if text:
            result.segments.append({"kind": "text", "text": text})
        return result

    cursor = 0
    for f in fences:
        # 围栏前的普通文本
        if f.start > cursor:
            chunk = text[cursor:f.start].rstrip()
            if chunk:
                result.segments.append({"kind": "text", "text": chunk})

        if not f.ok:
            result.issues.append(f.error)
            result.degraded = True
            result.segments.append({"kind": "text", "text": _degrade_note(f)})
            cursor = f.end
            continue

        issues = validate_component(f.component)
        errors = [i for i in issues if i.severity == "error"]
        for i in issues:
            result.issues.append(str(i))

        if errors:
            result.degraded = True
            result.segments.append({"kind": "text", "text": _degrade_note(f, "; ".join(str(i) for i in errors))})
            cursor = f.end
            continue

        try:
            enriched = enrich(f.component, kb)
        except Exception as e:  # noqa: BLE001
            result.degraded = True
            result.issues.append(f"注入失败：{type(e).__name__}: {e}")
            result.segments.append({"kind": "text", "text": render_fallback(f.component)})
            cursor = f.end
            continue

        result.segments.append({"kind": "component", "component": enriched})
        cursor = f.end

    tail = text[cursor:].strip()
    if tail:
        result.segments.append({"kind": "text", "text": tail})

    return result


def _degrade_note(fence: Fence, reason: str = "") -> str:
    why = reason or fence.error or "未知原因"
    body = render_fallback(fence.component or {}) if fence.component else fence.raw
    note = f"[该段结构化内容未能渲染（{why}），以下为纯文本回退]"
    return f"{note}\n{body}" if body else note


def describe_components() -> str:
    lines = ["lit-ui 组件（白名单）", ""]
    for name, spec in COMPONENTS.items():
        lines.append(f"  {name:<18} {spec['desc']}")
        if spec["requires"]:
            lines.append(f"    {' ' * 16}必需：{', '.join(spec['requires'])}")
        if spec["injected"]:
            lines.append(f"    {' ' * 16}框架注入（模型不得提供）：{', '.join(spec['injected'])}")
    return "\n".join(lines)
