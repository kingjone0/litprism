"""消息引用（ref）系统。

设计要点来自《Agent 框架上下文管理设计参考》，这是实现里踩坑最多的细节：

* **固定宽度零填充**：``m00123``。让正则、排序、区间表达都变简单。
* **解析容忍非规范形态，输出永远规范**：模型会写成 ``m123``、``M00123``、`` `m00123` ``、
  ``m 123``，甚至带 markdown 围栏。这些都应当能被解析，但**规范化后统一输出** ``m00123``。
* **自动纠正写反的区间**：模型经常把 start/end 写反（``m00005-m00001``），
  这不值得报错打断流程，纠正即可，但要**标记 corrected 让上层能知情**。
* **错误信息必须可操作**：不能只丢一句 "invalid input"，
  要说清「期望什么形态、实际收到什么、可用的范围是什么」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REF_PREFIX = "m"
REF_WIDTH = 5

# 容忍：可选前缀、可选分隔、可选围栏/引号、大小写
_REF_RE = re.compile(
    r"^[\s`'\"]*"
    r"(?:(?P<prefix>[mM])\s*)?"
    r"(?P<num>\d{1,9})"
    r"[\s`'\"]*$"
)

# 区间分隔符：- – — ~ ～ .. … 到 至
_RANGE_SEP = re.compile(r"\s*(?:-|–|—|~|～|\.\.|…|到|至)\s*")

_FENCE = re.compile(r"^[\s`'\"]+|[\s`'\"]+$")


class RefError(ValueError):
    """ref 解析失败。message 必须是可操作的。"""


def make_ref(seq: int) -> str:
    """序号 → 规范 ref。``123`` → ``'m00123'``"""
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise RefError(f"序号必须是整数，收到 {type(seq).__name__}")
    if seq < 0:
        raise RefError(f"序号不能为负，收到 {seq}")
    return f"{REF_PREFIX}{seq:0{REF_WIDTH}d}"


def parse_ref(text: str) -> int:
    """解析单个 ref 为序号。容忍非规范形态。

    >>> parse_ref('m00123'), parse_ref('m123'), parse_ref('M 123'), parse_ref('`m123`')
    (123, 123, 123, 123)
    """
    if text is None:
        raise RefError("ref 不能为空")
    if isinstance(text, int):
        return text
    s = str(text).strip()
    if not s:
        raise RefError("ref 不能为空字符串")
    m = _REF_RE.match(s)
    if not m:
        raise RefError(
            f"无法解析 ref {s!r}。期望形如 {make_ref(1)} 的引用"
            f"（可选前缀、允许空格与反引号、大小写不敏感）。"
        )
    return int(m.group("num"))


def normalize_ref(text: str) -> str:
    """解析并规范化为固定宽度形态。"""
    return make_ref(parse_ref(text))


def try_parse_ref(text: str) -> int | None:
    """宽容版本：解析失败返回 None，不抛异常。"""
    try:
        return parse_ref(text)
    except RefError:
        return None


def safe_ref(value: object) -> str:
    """**永不抛异常**的 ref 格式化，专用于构造错误消息。

    教训：校验器在报告「seq 非法」时，如果错误消息本身去调 ``make_ref(-1)``，
    就会在报告问题的过程中再抛一个异常，把真正的问题盖掉。
    """
    try:
        return make_ref(value)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return f"<非法:{value!r}>"


def is_valid_ref(text: str) -> bool:
    return try_parse_ref(text) is not None


@dataclass(frozen=True)
class RefRange:
    """规范化的闭区间 [start, end]（序号）。"""

    start: int
    end: int
    corrected: bool = False   # 是否因写反而被自动纠正

    @property
    def start_ref(self) -> str:
        return make_ref(self.start)

    @property
    def end_ref(self) -> str:
        return make_ref(self.end)

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def contains(self, seq: int) -> bool:
        return self.start <= seq <= self.end

    def format(self) -> str:
        return f"{self.start_ref}-{self.end_ref}"

    def __str__(self) -> str:  # pragma: no cover - 便捷
        return self.format()


def parse_range(text: str) -> RefRange:
    """解析区间表达，**自动纠正写反的 start/end**。

    支持 ``m00001-m00005`` / ``m1..m5`` / ``m1~m5`` / ``m1 到 m5`` 等写法。
    单个 ref（如 ``m00003``）会被当作 ``[3, 3]``。
    """
    if text is None:
        raise RefError("区间不能为空")
    s = _FENCE.sub("", str(text))
    if not s:
        raise RefError("区间不能为空字符串")

    parts = [p for p in _RANGE_SEP.split(s) if p.strip()]
    if len(parts) == 1:
        n = parse_ref(parts[0])
        return RefRange(n, n, corrected=False)
    if len(parts) == 2:
        a, b = parse_ref(parts[0]), parse_ref(parts[1])
        if a <= b:
            return RefRange(a, b, corrected=False)
        # 写反了 —— 纠正而不报错，但要标记
        return RefRange(b, a, corrected=True)
    raise RefError(
        f"无法解析区间 {text!r}：识别到 {len(parts)} 个端点。"
        f"期望形如 {make_ref(1)}-{make_ref(5)}（支持 - – — ~ ～ .. … 到 至 作分隔）。"
    )


def try_parse_range(text: str) -> RefRange | None:
    try:
        return parse_range(text)
    except RefError:
        return None


def format_range(start: int, end: int) -> str:
    return RefRange(start, end).format()


# ---------------------------------------------------------------------------
# 序号分配
# ---------------------------------------------------------------------------
def next_seq(max_seq: int | None) -> int:
    """下一个可用序号。"""
    return (max_seq or 0) + 1


def is_contiguous(seqs: list[int]) -> bool:
    """判断一组序号是否连续无洞 —— 不变量 4 要求区间必须完整枚举。"""
    if not seqs:
        return True
    s = sorted(seqs)
    return s == list(range(s[0], s[0] + len(s)))


def has_duplicates(seqs: list[int]) -> bool:
    return len(set(seqs)) != len(seqs)
