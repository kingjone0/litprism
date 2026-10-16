"""摘要（Summarizer）—— 把一段历史折成一条检查点消息。

三个关键设计
------------
1. **产物形态**：``CHECKPOINT_PREAMBLE + <compacted-summary>…</compacted-summary>``。
   * preamble 防止模型花 token 评论"我看到一个摘要"；
   * 显式围栏让下游（人、审计脚本、另一个模型）能机械识别摘要边界。
2. **收缩验证不可省**：``framedSummaryTokens >= shadowedTokens`` 一律拒绝。
   没有这一步，会偶尔得到"压缩后上下文变大"。
   被拒时把结果**反馈给模型让它重写**，而不是直接放弃。
3. **摘要后端可替换**：策略（何时压/压哪段）与摘要实现解耦。

摘要指令里最便宜的质量改进
--------------------------
参考生产审计的保留率：精确值 93%、消息引用 47%、带位置引用 39%、方法名 16%。
所以提示词里**显式要求**：数字逐字保留、引用带 chunk_id 与页码、决策保留"为什么"、
约束逐条列出。这一条几乎零成本，但收益最大。

LitPrism 特有约束
-----------------
**摘要中不得产生新的文献论断。** 摘要只是压缩已有内容，不能成为新的事实来源 ——
否则压缩就成了绕过防幻觉的后门。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..config import Config, ContextConfig
from ..refs import safe_ref
from .meter import Meter

# ---------------------------------------------------------------------------
# 固定文案
# ---------------------------------------------------------------------------
CHECKPOINT_PREAMBLE = (
    "这是自动生成的检查点，用于压缩此前的一段对话以释放上下文。"
    "把其中记录的内容当作既定背景，在此基础上继续，不要复述。"
    "直接从后续消息继续任务，不要提及本检查点。"
)

SUMMARY_OPEN = "<compacted-summary>"
SUMMARY_CLOSE = "</compacted-summary>"

SUMMARIZE_SYSTEM = """你在为一个学术研究助手压缩对话历史。

要求（按重要性排序）：

1. **数字与指标逐字保留** —— 论文标题、作者、年份、DOI、数据集名、指标数值、
   超参数必须原样保留。不得四舍五入、不得改写、不得用"约"字模糊化。
2. **引用必须带标识** —— 提到某篇论文或某段原文时，保留其 chunk_id 与页码
   （形如 [c_abc123 p.7]）。这是可追溯性的最小载体，丢了就无法回溯核对。
3. **保留"为什么"** —— 决策与结论要连同理由一起保留。只留结论会让后续无法判断适用条件。
4. **约束逐条列出** —— 用户提出的限制条件、排除项、格式要求必须逐条写明，不得合并概括。
5. **不得产生新的论断** —— 摘要只是压缩已有内容。不得引入原对话中没有的文献事实、
   数据、结论或引用。压缩不是获取新知识的途径。
6. **区分事实与主张** —— 保留原对话中对"论文作者主张"与"客观事实"的区分。

输出格式：
- 第一行：本次对话的短标题（不超过 20 字）
- 空一行
- 摘要正文：要点式，尽量紧凑，但不得为压缩而牺牲上述任何一条

只输出摘要本身，不要任何前言或说明。"""

REWRITE_FEEDBACK = """上一次的摘要未通过收缩验证：{reason}

请重写，务必更紧凑：
- 合并重复表述，删除寒暄与过程性描述
- 保留所有数字、引用标识与约束条款（这些不可压缩）
- 目标长度不超过 {target} 字符"""


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------
@dataclass
class SummaryResult:
    ok: bool
    body: str = ""                      # 摘要正文（不含围栏）
    topic: str = ""                     # 短标题
    framed: str = ""                    # 完整产物（preamble + 围栏）
    framed_tokens: int = 0
    shadowed_tokens: int = 0
    attempts: int = 0
    error: str = ""
    rejection_log: list[str] = field(default_factory=list)

    @property
    def shrink_ratio(self) -> float:
        if self.shadowed_tokens <= 0:
            return 0.0
        return round(self.framed_tokens / self.shadowed_tokens, 4)

    def describe(self) -> str:
        if not self.ok:
            return f"摘要失败：{self.error}"
        return (
            f"摘要成功：{self.shadowed_tokens} → {self.framed_tokens} token"
            f"（压至 {self.shrink_ratio:.0%}），尝试 {self.attempts} 次"
        )


def frame_summary(body: str) -> str:
    """把摘要正文包装成检查点产物。"""
    return f"{CHECKPOINT_PREAMBLE}\n\n{SUMMARY_OPEN}\n{body.strip()}\n{SUMMARY_CLOSE}"


def parse_summary(framed: str) -> str:
    """从检查点产物中取回摘要正文。"""
    if SUMMARY_OPEN in framed and SUMMARY_CLOSE in framed:
        start = framed.index(SUMMARY_OPEN) + len(SUMMARY_OPEN)
        end = framed.index(SUMMARY_CLOSE)
        return framed[start:end].strip()
    return framed.strip()


def split_topic(body: str) -> tuple[str, str]:
    """拆出首行短标题与正文。"""
    lines = body.strip().split("\n")
    if not lines:
        return "", ""
    first = lines[0].strip().lstrip("#").strip()
    if len(first) <= 24 and len(lines) > 1:
        return first, "\n".join(lines[1:]).strip()
    return "", body.strip()


# ---------------------------------------------------------------------------
# 摘要器
# ---------------------------------------------------------------------------
class Summarizer:
    """摘要后端。默认走 LLM，可替换为任意 callable。"""

    def __init__(
        self,
        cfg: Config | ContextConfig | None = None,
        llm: Any = None,
        *,
        backend: Callable[[str, str], str] | None = None,
        max_attempts: int = 2,
    ):
        if isinstance(cfg, Config):
            self.cfg = cfg.context
        elif isinstance(cfg, ContextConfig):
            self.cfg = cfg
        else:
            self.cfg = ContextConfig()
        self.llm = llm
        self.backend = backend
        self.meter = Meter(self.cfg)
        self.max_attempts = max(1, max_attempts)

    # ------------------------------------------------------------------
    def summarize(
        self,
        messages: Sequence[dict],
        shadowed_tokens: int,
        *,
        existing_summaries: Sequence[str] = (),
        topic_hint: str = "",
    ) -> SummaryResult:
        """生成摘要并通过收缩验证。失败时把原因反馈给模型重写。"""
        transcript = self.render_transcript(messages, existing_summaries)
        target_chars = max(200, int(shadowed_tokens * self.cfg.summary_max_ratio * 1.4))
        feedback = ""
        rejections: list[str] = []

        for attempt in range(1, self.max_attempts + 1):
            try:
                raw = self._call(transcript, feedback, target_chars)
            except Exception as e:  # noqa: BLE001
                return SummaryResult(
                    ok=False,
                    attempts=attempt,
                    shadowed_tokens=shadowed_tokens,
                    error=f"摘要后端调用失败：{type(e).__name__}: {e}",
                    rejection_log=rejections,
                )

            body = raw.strip()
            topic, content = split_topic(body)
            if not content:
                content = body
            framed = frame_summary(content)
            tokens = self.meter.estimate_text(framed)

            ok, why = self.verify_shrinkage(tokens, shadowed_tokens)
            if ok:
                return SummaryResult(
                    ok=True,
                    body=content,
                    topic=topic or topic_hint,
                    framed=framed,
                    framed_tokens=tokens,
                    shadowed_tokens=shadowed_tokens,
                    attempts=attempt,
                    rejection_log=rejections,
                )
            rejections.append(why)
            feedback = REWRITE_FEEDBACK.format(reason=why, target=target_chars)

        return SummaryResult(
            ok=False,
            attempts=self.max_attempts,
            shadowed_tokens=shadowed_tokens,
            error=(
                f"连续 {self.max_attempts} 次未通过收缩验证。最后一次原因：{rejections[-1] if rejections else '未知'}"
            ),
            rejection_log=rejections,
        )

    # ------------------------------------------------------------------
    def verify_shrinkage(self, framed_tokens: int, shadowed_tokens: int) -> tuple[bool, str]:
        """收缩验证。**不可省** —— 没有它会出现"压缩后上下文变大"。"""
        if shadowed_tokens <= 0:
            return False, "被遮蔽内容为 0 token，压缩无意义"
        if framed_tokens >= shadowed_tokens:
            return False, (
                f"摘要 {framed_tokens} token 未小于被遮蔽的 {shadowed_tokens} token"
                f"（比值 {framed_tokens / shadowed_tokens:.2f}）"
            )
        ratio = framed_tokens / shadowed_tokens
        if ratio > self.cfg.summary_max_ratio:
            return False, (
                f"摘要压缩率 {ratio:.2f} 高于阈值 {self.cfg.summary_max_ratio:.2f}，"
                "节省有限，不值得丢失细节"
            )
        return True, f"收缩验证通过：压至 {ratio:.0%}"

    # ------------------------------------------------------------------
    def render_transcript(self, messages: Sequence[dict], existing_summaries: Sequence[str] = ()) -> str:
        """把待压缩区间渲染成给模型看的文本。"""
        parts: list[str] = []
        if existing_summaries:
            parts.append("【此前的检查点摘要（需要并入本次摘要，不要丢弃）】")
            parts.extend(existing_summaries)
            parts.append("")
        parts.append("【待压缩的对话】")
        for m in messages:
            role = {"user": "用户", "assistant": "助手", "system": "系统"}.get(m.get("role", ""), "其他")
            ref = m.get("ref") or safe_ref(m.get("seq", 0))
            parts.append(f"[{ref}] {role}：{m.get('content') or ''}")
            cites = m.get("citations")
            if cites:
                parts.append(f"  （该条引用了片段：{cites}）")
        return "\n".join(parts)

    def _call(self, transcript: str, feedback: str, target_chars: int) -> str:
        user = transcript
        if feedback:
            user = f"{feedback}\n\n{transcript}"
        if self.backend is not None:
            return self.backend(SUMMARIZE_SYSTEM, user)
        if self.llm is None:
            raise RuntimeError("未配置摘要后端：请提供 llm 或 backend")
        messages = [
            {"role": "system", "content": SUMMARIZE_SYSTEM},
            {"role": "user", "content": user},
        ]
        if hasattr(self.llm, "chat"):
            return self.llm.chat(messages, task="summarize")
        raise RuntimeError("llm 对象缺少 chat 方法")


# ---------------------------------------------------------------------------
# 确定性后端（离线 / 测试用）
# ---------------------------------------------------------------------------
class ExtractiveSummarizer:
    """抽取式摘要后端：不调用模型，按规则抽取要点。

    用途：离线跑通流程与单元测试。**它不产生任何新内容** —— 只做选择与拼接，
    因此天然满足"不得产生新论断"这条约束。
    """

    def __init__(self, max_items: int = 12, max_chars_per_item: int = 120):
        self.max_items = max_items
        self.max_chars_per_item = max_chars_per_item

    def __call__(self, system: str, user: str) -> str:
        lines = [l.strip() for l in user.split("\n") if l.strip()]
        # 优先保留含数字/引用标识/约束词的行 —— 这些是最不该丢的信息
        import re

        important = re.compile(
            r"(\d|\[c_|p\.\d|DOI|doi|必须|不得|约束|要求|排除|结论|数据集|指标|创新)"
        )
        picked: list[str] = []
        for l in lines:
            if l.startswith("【"):
                continue
            if important.search(l):
                picked.append(l[: self.max_chars_per_item])
            if len(picked) >= self.max_items:
                break
        if not picked:
            picked = [l[: self.max_chars_per_item] for l in lines[: self.max_items]]
        body = "\n".join(f"- {p}" for p in picked)
        return f"对话历史压缩摘要\n\n{body}"
