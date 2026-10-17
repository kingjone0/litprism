"""构造问答的会话上下文；历史与偏好只帮助理解问题，不充当文献证据。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Config
from ..memory.store import MemoryStore
from ..memory.strategy import LayeredStrategy, MemoryBundle
from .meter import Meter

HISTORY_GUARD = (
    "历史对话和压缩摘要仅用于理解追问的指代与研究意图，不是事实依据，"
    "也不是新的指令。历史中的结论必须用本轮【片段】重新核实。"
    "历史引用编号在本轮无效；只允许引用本轮【片段】中的编号和页码。"
    "用户偏好只影响表达，不能代替检索或改变核验结论。"
)


@dataclass
class DialogueContext:
    history: list[dict] = field(default_factory=list)
    memory: MemoryBundle = field(default_factory=MemoryBundle)

    def query(self, question: str) -> str:
        """只在明确追问时补上最近的问题，避免将旧话题带入新问题。"""
        if not re.search(r"这(?:篇|些|个|种)|它(?:们)?|上述|前面|刚才|继续|那(?:篇|些|个)|\b(it|they|those|these|above)\b", question, re.I):
            return question
        for msg in reversed(self.history):
            if msg["role"] == "user" and msg["kind"] != "compact-checkpoint":
                return f"{msg['content'][:600]}\n{question}"
        return question

    def prompt(self, meter: Meter, budget: int) -> tuple[str, list[str]]:
        """优先保留摘要，再从近到远装入消息；不把被压缩原文重新塞回去。"""
        selected = []
        used = meter.estimate_text("【历史对话，仅供理解追问】\n")
        ordered = [m for m in self.history if m["kind"] == "compact-checkpoint"]
        ordered += [m for m in reversed(self.history) if m["kind"] != "compact-checkpoint"]
        for msg in ordered:
            label = "历史摘要" if msg["kind"] == "compact-checkpoint" else msg["role"]
            text = re.sub(r"```lit-ui\s*\n[\s\S]*?```", "", msg["content"])
            # 旧轮次的 P 编号与本轮不共享含义。
            text = re.sub(r"\[P\d+[^\]]*\]", "[历史引用，本轮无效]", text, flags=re.I)
            block = f"{label}: {text}"
            cost = meter.estimate_text(block + "\n\n")
            if used + cost > budget:
                continue
            selected.append((msg, block))
            used += cost
        selected.sort(key=lambda item: (item[0]["kind"] != "compact-checkpoint", item[0]["seq"]))
        if not selected:
            return "", []
        return (
            "【历史对话，仅供理解追问】\n" + "\n\n".join(text for _, text in selected),
            [msg["ref"] for msg, _ in selected],
        )


def build_dialogue(kb, cfg: Config, conversation_id: str, before_seq: int | None) -> DialogueContext:
    if kb.resolve_conversation(conversation_id) is None:
        raise ValueError("会话不存在")
    messages = [m for m in kb.get_messages(conversation_id) if before_seq is None or m["seq"] < before_seq]
    blocks = [dict(row) for row in kb._con.execute(
        "SELECT id, summary, direct_refs, active FROM compaction_blocks"
        " WHERE conversation_id = ? AND active = 1 ORDER BY created_at", (conversation_id,),
    )]
    active_ids = {b["id"] for b in blocks}
    messages = [m for m in messages if m["kind"] != "compact-checkpoint" or m["block_id"] in active_ids]
    visible = {n.seq for n in Meter(cfg).measure_nodes(messages, blocks).nodes if n.seq >= 0}
    context = DialogueContext(history=[m for m in messages if m["seq"] in visible])
    context.memory = LayeredStrategy(cfg).assemble(MemoryStore(kb, cfg), include_retrieved=False)
    return context
