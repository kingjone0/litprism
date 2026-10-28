"""大模型客户端。

支持三类后端，统一接口 ``chat()`` / ``chat_json()``：

* ``openai`` / ``deepseek``  —— 任意 OpenAI 兼容端点（走 /chat/completions）
* ``ollama``                 —— 本地 Ollama 的 OpenAI 兼容层
* ``mock``                   —— 离线占位模型：不联网、不需要 Key，
                                用启发式规则产出结构化结果，用于跑通流程与单元测试。
                                其输出在 UI 中始终带 "mock" 标记，绝不冒充真实模型结论。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

from .config import Config, LLMConfig
from .tasks import TaskCancelled, checkpoint
from .textutil import truncate


def safe_failure(exc):
    """Structured diagnostics only: never response bodies, URLs or headers."""
    result = {'type': type(exc).__name__, 'kind': 'service_error'}
    seen = set()
    current = exc
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        status = getattr(current, 'http_status', None)
        if type(status) is int and 100 <= status <= 599:
            result['http_status'] = status
        name = type(current).__name__.lower()
        if 'timeout' in name:
            kind = 'connect_timeout' if 'connect' in name else 'read_timeout' if 'read' in name else 'timeout'
            if result['kind'] not in ('connect_timeout', 'read_timeout'):
                result['kind'] = kind
        current = current.__cause__ or current.__context__
    return result


class LLMError(RuntimeError):
    def __init__(self, message='', *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class ContextWindowError(LLMError):
    pass


class OutputLimitError(LLMError):
    pass


# ---------------------------------------------------------------------------
# JSON 解析：容忍模型输出的 ```json 代码块、前后废话、尾随逗号
# ---------------------------------------------------------------------------
def parse_json_loose(text: str) -> dict | list | None:
    if not text:
        return None
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # 截取第一个 { 到最后一个 }
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            frag = t[i : j + 1]
            frag = re.sub(r",(\s*[}\]])", r"\1", frag)   # 去尾随逗号
            try:
                return json.loads(frag)
            except json.JSONDecodeError:
                continue
    return None


# ---------------------------------------------------------------------------
# OpenAI 兼容客户端
# ---------------------------------------------------------------------------
def _review_output_cap(default: int = 4096) -> int:
    """逐条语义复核的单次输出上限。

    默认 4096 与既有行为一致（不静默提高）。但推理模型的**思考 token 也计入这个额度**，
    撞到上限会熔断整篇复核（``OutputLimitError`` → 其余陈述全部降级待审）。
    需要时用 ``LITPRISM_REVIEW_MAX_TOKENS`` 显式放宽，例如小论文实测用 8192。
    """
    try:
        return max(1024, int(os.environ.get('LITPRISM_REVIEW_MAX_TOKENS', str(default))))
    except ValueError:
        return default


def _review_read_timeout(default: int = 60) -> int:
    """语义复核的读取超时。默认 60 秒不变。

    推理模型偶发卡住超过 60 秒（实测），整篇复核会因此中断。
    需要放宽时用 ``LITPRISM_REVIEW_READ_TIMEOUT`` 显式指定。
    """
    try:
        return max(10, int(os.environ.get('LITPRISM_REVIEW_READ_TIMEOUT', str(default))))
    except ValueError:
        return default


def _review_connect_timeout(default: int = 10) -> int:
    """Connect and read stalls are separate; preserve the default unless explicit."""
    try:
        return max(1, int(os.environ.get('LITPRISM_REVIEW_CONNECT_TIMEOUT', str(default))))
    except ValueError:
        return default


def output_allocation(cfg: LLMConfig, messages: list[dict], *, meter=None) -> dict:
    """Allocate from model capacity and the complete, estimated input envelope.

    There is no user task ceiling in automatic mode. A 5% input-estimation
    margin (at least 256 tokens) covers formatting/tokenizer uncertainty. This
    is an estimate, not a promise that every provider will accept the request.
    """
    from .context.meter import Meter
    meter = meter or Meter()
    estimated = sum(meter.estimate_message(m) for m in messages) + 8
    margin = max(256, math.ceil(estimated * .05))
    ceiling = cfg.model_max_output_tokens
    window = cfg.model_window_tokens
    if not cfg.automatic_output or not ceiling or not window:
        return {'max_tokens': cfg.max_tokens, 'output_mode': 'legacy_unknown_capability',
                'estimated_input_tokens': estimated}
    remaining = window - estimated - margin
    if remaining <= 0:
        raise ContextWindowError('完整请求估算已占满模型上下文，未发送模型请求。')
    return {'max_tokens': min(ceiling, remaining), 'output_mode': 'automatic',
            'estimated_input_tokens': estimated, 'input_safety_tokens': margin,
            'model_window_tokens': window, 'model_max_output_tokens': ceiling}


class OpenAICompatClient:
    supports_semantic_review = True
    def __init__(self, cfg: LLMConfig):
        if requests is None:
            raise LLMError("缺少 requests 依赖：python -m pip install requests")
        self.cfg = cfg
        self.base_url = cfg.effective_base_url
        self.model = cfg.effective_model
        self.name = f"{cfg.provider}:{self.model}"

    def chat(self, messages: list[dict], *, json_mode: bool = False, task: str | None = None,
             temperature: float | None = None, max_tokens: int | None = None, on_delta=None) -> str:
        self.last_usage = None
        self.last_reasoning = ''
        self.last_request_settings = {}
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
        }
        allocation = output_allocation(self.cfg, messages, meter=getattr(self, 'meter', None))
        if self.cfg.automatic_output:
            payload['max_tokens'] = allocation['max_tokens']
        if self.cfg.model_max_output_tokens:
            payload['max_tokens'] = min(payload['max_tokens'], self.cfg.model_max_output_tokens)
        if json_mode and self.cfg.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if on_delta is not None:
            payload.update(stream=True, stream_options={'include_usage': True})
        bounded = task in ('semantic_review', 'semantic_cross_review')
        if bounded and not self.cfg.automatic_output:
            cap = self.cfg.cross_review_max_tokens if task == 'semantic_cross_review' else _review_output_cap(self.cfg.review_max_tokens)
            payload['max_tokens'] = min(payload['max_tokens'], cap)
        request_timeout = ((min(self.cfg.timeout, _review_connect_timeout()), min(self.cfg.timeout, _review_read_timeout(self.cfg.review_read_timeout)))
                           if bounded else self.cfg.timeout)
        self.last_request_settings = {'task': task, 'max_tokens': payload['max_tokens'],
                                      'temperature': payload['temperature'], 'timeout': request_timeout}
        self.last_request_settings.update({k: v for k, v in allocation.items() if k != 'max_tokens'})

        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"

        last_err: Exception | None = None
        single_attempt = getattr(self, 'durable_single_attempt', False) or on_delta is not None
        for attempt in range(1 if single_attempt else (2 if bounded else 3)):
            self.last_request_settings['attempts'] = attempt+1
            try:
                checkpoint('正在调用模型；最终显示前检查引用')
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=request_timeout,
                    **({'stream': True} if on_delta is not None else {}),
                )
                checkpoint()
                if resp.status_code >= 400:
                    if any(w in resp.text.lower() for w in ('context_length', 'maximum context', 'too many tokens')):
                        raise ContextWindowError('context_length_exceeded')
                    # 端点不支持 response_format 时自动降级重试
                    if not single_attempt and resp.status_code in (400, 404, 422) and "response_format" in payload:
                        payload.pop("response_format", None)
                        continue
                    raise LLMError(f"HTTP {resp.status_code}: {truncate(resp.text, 400)}", http_status=resp.status_code)
                if on_delta is not None:
                    from .streaming import provider_stream
                    try:
                        return provider_stream(resp, 'openai', self, on_delta)
                    finally:
                        resp.close()
                data = resp.json()
                self.last_usage = data.get('usage')
                if data['choices'][0].get('finish_reason') == 'length':
                    raise OutputLimitError('模型达到输出 token 上限，结果不完整；已保留待审，请核对服务能力与截断用量。')
                message = data['choices'][0]['message']
                reasoning = message.get('reasoning_content')
                if isinstance(reasoning, str):
                    self.last_reasoning = reasoning
                return message.get('content') or ""
            except (ContextWindowError, OutputLimitError, TaskCancelled):
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                if single_attempt:
                    break
                if bounded:
                    if isinstance(e, requests.exceptions.ConnectTimeout) and attempt == 0:
                        time.sleep(.5)
                        continue
                    break
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"调用大模型失败（{self.name}）：{last_err}", http_status=getattr(last_err, 'http_status', None)) from last_err

    def chat_json(self, messages: list[dict], *, task: str | None = None) -> dict:
        text = self.chat(messages, json_mode=True, task=task)
        return self._parse_json(text, task)

    def chat_json_stream(self, messages: list[dict], *, task=None, on_delta):
        text = self.chat(messages, json_mode=True, task=task, on_delta=on_delta)
        return OpenAICompatClient._parse_json(text, task)

    @staticmethod
    def _parse_json(text, task):
        if task in ('semantic_review', 'semantic_cross_review'):
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError) as exc:
                raise LLMError('语义复核没有返回完整严格 JSON，未放行。') from exc
        else:
            parsed = parse_json_loose(text)
        if not isinstance(parsed, dict):
            raise LLMError('模型未返回合法的 JSON 对象，请重试或检查模型的结构化输出能力。')
        return parsed  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 离线 mock 客户端
# ---------------------------------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[。！？!?\.])\s+|\n{2,}")


def _sentences(text: str) -> list[str]:
    out = []
    for s in _SENT_SPLIT.split(text or ""):
        s = " ".join((s or "").split())
        if 20 <= len(s) <= 400:
            out.append(s)
    return out


def _pick(sents: list[str], cues: list[str], n: int = 3) -> list[str]:
    scored = []
    for s in sents:
        low = s.lower()
        score = sum(1 for c in cues if c in low)
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, s in scored:
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= n:
            break
    if not out:
        out = sents[:n]
    return out


class MockClient:
    """离线占位模型：用启发式规则模拟结构化输出，保证无 Key 也能跑通全流程。"""

    name = "mock:offline"

    def __init__(self, cfg: LLMConfig | None = None):
        self.cfg = cfg or LLMConfig(provider="mock")

    # -- 内部：从 messages 里还原"用户给的正文" --
    @staticmethod
    def _user_blob(messages: list[dict]) -> str:
        return "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")

    def chat(self, messages: list[dict], *, json_mode: bool = False, task: str | None = None, **kwargs) -> str:
        if task == 'skill':
            blob = self._user_blob(messages)
            match = re.search(r'\[(P\d+)\][^\n]*?(?:第 (\d+) 页)?[^\n]*\n([^\n]+)', blob)
            if not match:
                return '[mock 离线模式] 没有可演示的原文片段。'
            header = blob[match.start():].split('\n', 1)[0]
            page = re.search(r'第 (\d+) 页', header)
            ref = '['+match.group(1)+(f' p.{page.group(1)}' if page else '')+']'
            return '[mock 离线模式 · 仅展示原文，不执行语义任务]\n\n'+match.group(3)+' '+ref
        return json.dumps(self._dispatch(messages, task), ensure_ascii=False)

    def chat_json(self, messages: list[dict], *, task: str | None = None) -> dict:
        return self._dispatch(messages, task)

    def _dispatch(self, messages: list[dict], task: str | None) -> dict:
        blob = self._user_blob(messages)
        if task == "extract":
            return self._extract(blob)
        if task == "concepts":
            return self._concepts(blob)
        if task == "qa":
            return self._qa(blob)
        if task == "compare":
            return self._compare(blob)
        if task == "recommend":
            return {"reasons": [], "note": "mock 模式不做语义推理"}
        return {"note": "mock 模式：未识别的任务类型"}

    # -- 各类任务的启发式实现 --
    def _extract(self, blob: str) -> dict:
        # 去掉拼装上下文时加的标记行（【标题】…、### 章节名），避免污染抽取出的句子
        body = re.sub(r"^【[^】]*】.*$", "", blob, flags=re.M)
        body = re.sub(r"^#{1,6}\s+.*$", "", body, flags=re.M)
        sents = _sentences(body)
        rq = _pick(sents, ["we address", "we study", "this paper", "本文", "研究问题", "aim to", "we investigate"], 2)
        method = _pick(sents, ["we propose", "we present", "we introduce", "method", "framework", "提出", "方法", "architecture"], 3)
        exp = _pick(sents, ["dataset", "experiment", "we evaluate", "baseline", "实验", "数据集", "benchmark"], 3)
        concl = _pick(sents, ["we find", "results show", "we conclude", "结果表明", "结论", "outperform"], 3)
        innov = _pick(sents, ["novel", "for the first time", "contribution", "创新", "首次", "our key"], 2)
        limit = _pick(sents, ["limitation", "future work", "局限", "未来工作"], 2)
        return {
            "research_question": " ".join(rq) or "（mock 未能定位，请配置真实模型）",
            "methodology": " ".join(method),
            "experiment_design": " ".join(exp),
            "key_conclusions": " ".join(concl),
            "innovations": " ".join(innov),
            "limitations": " ".join(limit),
            "field": "",
            "keywords": [],
            "evidence": {"note": "由离线占位模型按关键词启发式抽取，非语义理解结果"},
            "_mock": True,
        }

    def _concepts(self, blob: str) -> dict:
        # 抽取形如 "Term (ABBR)" 或全大写缩写的候选术语
        terms = []
        for m in re.finditer(r"\b([A-Z][A-Za-z\-]{3,}(?:\s[A-Z][A-Za-z\-]{2,}){0,3})\s*\(([A-Z]{2,6})\)", blob):
            terms.append({"term": m.group(1), "abbr": m.group(2), "category": "缩写/方法"})
        for m in re.finditer(r"\b([A-Z]{3,6})\b", blob):
            t = m.group(1)
            if t not in {x.get("abbr") for x in terms} and len(terms) < 12:
                terms.append({"term": t, "abbr": t, "category": "缩写"})
        seen, out = set(), []
        for t in terms:
            if t["term"].lower() in seen:
                continue
            seen.add(t["term"].lower())
            out.append(
                {
                    "term": t["term"],
                    "definition": "（mock 模式不生成释义，请配置真实模型以获得术语解释）",
                    "category": t.get("category", "术语"),
                }
            )
        return {"concepts": out[:12]}

    _EXCERPT_HEAD = re.compile(
        r"^\[P(?P<tag>\d+)\]\s*论文：《(?P<title>.*?)》"
        r"(?:（(?P<year>\d{4})）)?"
        r"(?:\s*第\s*(?P<page>\d+)\s*页)?"
        r"(?:\s*·\s*章节：(?P<section>.*?))?\s*$"
    )

    def _parse_excerpts(self, blob: str) -> list[dict]:
        """把用户消息里的片段还原成 [{tag, page, title, body}]。"""
        out: list[dict] = []
        cur: dict | None = None
        for line in blob.split("\n"):
            m = self._EXCERPT_HEAD.match(line.strip())
            if m:
                cur = {
                    "tag": f"P{m.group('tag')}",
                    "page": int(m.group("page")) if m.group("page") else None,
                    "title": m.group("title"),
                    "body": [],
                }
                out.append(cur)
            elif cur is not None:
                cur["body"].append(line)
        for e in out:
            e["body"] = "\n".join(e["body"]).strip()
        return out

    def _qa(self, blob: str) -> dict:
        excerpts = self._parse_excerpts(blob)
        if not excerpts:
            return {
                "answer": "[mock 离线模式] 未能解析出可用片段。",
                "confidence": "low",
                "insufficient_evidence": True,
                "_mock": True,
            }

        cues = ["we propose", "we present", "we use", "attention", "the model", "training",
                "results", "dataset", "本文", "方法", "结果", "提出"]
        picked: list[tuple[dict, str]] = []
        for e in excerpts:
            sents = _sentences(e["body"])
            for s in _pick(sents, cues, 2):
                if s not in [p[1] for p in picked]:
                    picked.append((e, s))

        if not picked:
            return {
                "answer": "现有片段不足以回答该问题，缺少与该问题直接相关的原文依据。",
                "confidence": "low",
                "insufficient_evidence": True,
                "_mock": True,
            }

        parts: list[str] = []
        for e, s in picked[:5]:
            cite = f"[{e['tag']}" + (f" p.{e['page']}]" if e["page"] else "]")
            parts.append(f"{truncate(s, 260)} {cite}")

        return {
            "answer": (
                "[mock 离线模式 · 仅演示引用与校验链路，非真实模型解读]\n\n"
                + "\n\n".join(parts)
            ),
            "confidence": "low",
            "insufficient_evidence": False,
            "evidence_map": [
                {"claim": truncate(s, 90), "sources": [e["tag"] + (f" p.{e['page']}" if e["page"] else "")]}
                for e, s in picked[:5]
            ],
            "_mock": True,
        }

    _BLOCK_HEAD = re.compile(r"^\[P(?P<tag>\d+)\]\s*《(?P<title>.*?)》")
    _FIELD_LINE = re.compile(r"^(?P<label>研究问题|方法论|实验设计|关键结论|创新点|局限|核验状态)：\s*(?P<value>.*)$")
    _DIM_LABELS = ["研究问题", "方法论", "实验设计", "关键结论", "创新点", "局限"]

    def _compare(self, blob: str) -> dict:
        """离线对比：直接解析提示词里已给出的结构化字段，拼成对比矩阵。"""
        if re.search(r'^论文 D\d+：', blob, re.M):
            snippets = re.findall(r'^\[(P\d+)\] (D\d+) (p\.\d+|无页码)\n([^\n]+)', blob, re.M)
            values = {}
            for tag, doc, page, text in snippets:
                values.setdefault(doc, truncate(text, 220)+' ['+tag+(' '+page if page != '无页码' else '')+']')
            first = snippets[0] if snippets else None
            ref = (' ['+first[0]+(' '+first[2] if first[2] != '无页码' else '')+']') if first else ''
            return {'summary': '[mock 离线模式] 以下仅对照原文片段，不构成语义比较。'+ref,
                    'dimensions': [{'dimension': '原文摘录', 'values': values}], '_mock': True}
        papers: dict[str, dict] = {}
        cur: dict | None = None
        for line in blob.split("\n"):
            m = self._BLOCK_HEAD.match(line.strip())
            if m:
                cur = {"tag": f"P{m.group('tag')}", "title": m.group("title"), "fields": {}}
                papers[cur["tag"]] = cur
                continue
            fm = self._FIELD_LINE.match(line.strip())
            if fm and cur is not None:
                cur["fields"][fm.group("label")] = fm.group("value").strip()
        if len(papers) < 2:
            return {
                "summary": "[mock 离线模式] 未能解析出两篇以上论文的结构化字段；请先对相关论文运行 analyze。",
                "dimensions": [], "convergence": [], "divergence": [], "_mock": True,
            }

        tags = list(papers)
        dims = []
        for label in self._DIM_LABELS:
            dims.append({
                "dimension": label,
                "values": {t: (papers[t]["fields"].get(label) or "未提及") for t in tags},
            })

        def missing(p: dict) -> bool:
            v = p["fields"].get("实验设计", "")
            return (not v) or "未提取" in v or "需先运行" in v

        note = []
        if any(missing(p) for p in papers.values()):
            note.append("部分论文尚未生成结构化解读（未运行 analyze），对应字段显示为「未提及」。")
        note.append("本对比由离线占位模型拼装，仅演示流程；配置真实模型后可获得语义层面的对比结论。")

        return {
            "summary": (
                f"共对比 {len(tags)} 篇论文："
                + "；".join(f"[{t}]《{papers[t]['title']}》" for t in tags)
                + "。"
                + ("".join(note))
            ),
            "dimensions": dims,
            "convergence": [],
            "divergence": [],
            "evidence_map": [],
            "_mock": True,
        }


# ---------------------------------------------------------------------------
class AnthropicClient:
    supports_semantic_review = True
    """Anthropic Messages 协议的最小实现。

    与 OpenAI 兼容协议的差异：system 单独一个字段、认证用 x-api-key、
    返回结构是 content 数组。
    """

    def __init__(self, cfg: LLMConfig):
        if requests is None:
            raise LLMError("缺少 requests 依赖：python -m pip install requests")
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")
        self.model = cfg.model
        self.name = f"anthropic:{self.model}"

    def chat_json(self, messages: list[dict], *, task: str | None = None) -> dict:
        return OpenAICompatClient._parse_json(self.chat(messages, json_mode=True, task=task), task)

    chat_json_stream = OpenAICompatClient.chat_json_stream

    def chat(self, messages: list[dict], *, json_mode: bool = False, task: str | None = None,
             temperature: float | None = None, max_tokens: int | None = None, on_delta=None) -> str:
        self.last_usage = None
        self.last_reasoning = ''
        self.last_request_settings = {}
        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        convo = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "messages": convo,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if on_delta is not None:
            payload['stream'] = True
        allocation = output_allocation(self.cfg, messages, meter=getattr(self, 'meter', None))
        if self.cfg.automatic_output:
            payload['max_tokens'] = allocation['max_tokens']
        if self.cfg.model_max_output_tokens:
            payload['max_tokens'] = min(payload['max_tokens'], self.cfg.model_max_output_tokens)
        bounded = task in ('semantic_review', 'semantic_cross_review')
        if bounded and not self.cfg.automatic_output:
            cap = self.cfg.cross_review_max_tokens if task == 'semantic_cross_review' else _review_output_cap(self.cfg.review_max_tokens)
            payload['max_tokens'] = min(payload['max_tokens'], cap)
        request_timeout = ((min(self.cfg.timeout, _review_connect_timeout()), min(self.cfg.timeout, _review_read_timeout(self.cfg.review_read_timeout)))
                           if bounded else self.cfg.timeout)
        self.last_request_settings = {'task': task, 'max_tokens': payload['max_tokens'],
                                      'temperature': payload['temperature'], 'timeout': request_timeout}
        self.last_request_settings.update({k: v for k, v in allocation.items() if k != 'max_tokens'})

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.cfg.api_key or "",
            "anthropic-version": "2023-06-01",
        }

        last_err: Exception | None = None
        single_attempt = getattr(self, 'durable_single_attempt', False) or on_delta is not None
        for attempt in range(1 if single_attempt else (2 if bounded else 3)):
            self.last_request_settings['attempts'] = attempt+1
            try:
                checkpoint('正在调用模型；最终显示前检查引用')
                resp = requests.post(
                    f"{self.base_url}/messages", headers=headers, json=payload,
                    timeout=request_timeout,
                    **({'stream': True} if on_delta is not None else {}),
                )
                checkpoint()
                if resp.status_code >= 400:
                    if any(w in resp.text.lower() for w in ('context_length', 'maximum context', 'too many tokens')):
                        raise ContextWindowError('context_length_exceeded')
                    raise LLMError(f"HTTP {resp.status_code}: {truncate(resp.text, 400)}", http_status=resp.status_code)
                if on_delta is not None:
                    from .streaming import provider_stream
                    try:
                        return provider_stream(resp, 'anthropic', self, on_delta)
                    finally:
                        resp.close()
                data = resp.json()
                self.last_usage = data.get('usage')
                blocks = data.get("content") or []
                if data.get('stop_reason') == 'max_tokens':
                    raise OutputLimitError('模型达到输出 token 上限，结果不完整；已保留待审，请核对服务能力与截断用量。')
                self.last_reasoning = '\n\n'.join(b['thinking'] for b in blocks
                                                  if b.get('type') == 'thinking' and isinstance(b.get('thinking'), str))
                return "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
            except (ContextWindowError, OutputLimitError, TaskCancelled):
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                if single_attempt:
                    break
                if bounded:
                    if isinstance(e, requests.exceptions.ConnectTimeout) and attempt == 0:
                        time.sleep(.5)
                        continue
                    break
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"调用大模型失败（{self.name}）：{last_err}", http_status=getattr(last_err, 'http_status', None)) from last_err


def build_llm(cfg: Config):
    """按配置构造客户端；provider=mock 或缺少 Key 时自动降级为离线 mock。"""
    from .backup import require_restored_credentials
    require_restored_credentials(cfg)
    provider = (cfg.llm.provider or "mock").lower()
    if provider == "mock":
        return MockClient(cfg.llm)
    if provider in ("ollama",):
        return OpenAICompatClient(cfg.llm)
    if not cfg.llm.api_key:
        # 没有 Key 时不报错，降级为 mock 并保持流程可用
        return MockClient(cfg.llm)
    return OpenAICompatClient(cfg.llm)


def build_llm_for(
    cfg: Config,
    *,
    base_url: str,
    api_key: str,
    model: str,
    protocol: str = "openai",
    temperature: float | None = None,
    max_tokens: int | None = None,
):
    """按指定提供商参数构造客户端（用于 chat 的模型选择）。

    ``protocol`` 为 ``anthropic`` 时走 Messages 协议，否则走 OpenAI 兼容协议。
    """
    from copy import deepcopy

    lc: LLMConfig = deepcopy(cfg.llm)
    lc.provider = "custom"
    lc.base_url = base_url.rstrip("/")
    lc.api_key = api_key
    lc.model = model
    if temperature is not None:
        lc.temperature = temperature
    if max_tokens is not None:
        lc.max_tokens = max_tokens

    from .context.meter import Meter
    client = AnthropicClient(lc) if (protocol or 'openai').lower() == 'anthropic' else OpenAICompatClient(lc)
    client.meter = Meter(cfg)
    return client


def build_active_llm(kb, cfg: Config, *, allow_mock=False, mock_factory=None):
    """Shared Web/acceptance runtime; never falls back from a broken selection."""
    from copy import deepcopy
    from urllib.parse import urlparse

    from .backup import require_restored_credentials
    from .providers import ProviderStore, apply_active_window

    store = ProviderStore(kb, cfg.data_path)
    active, model_id = store.get_active()
    if active is None:
        require_restored_credentials(cfg)
        if allow_mock and (cfg.llm.provider or '').lower() == 'mock':
            return (mock_factory or build_llm)(cfg)
        raise ValueError('尚未选择模型：请先在设置里选择提供商与模型，再开始调用。')
    if not model_id:
        raise ValueError('当前提供商没有选定模型。')
    key = store.secrets.get(active.api_key_ref)
    if not key and urlparse(active.base_url).hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('当前提供商的密钥未配置或无法解密，请检查设置。')
    runtime = deepcopy(cfg)
    capability = apply_active_window(runtime, kb, require_active=True)
    client = build_llm_for(runtime, base_url=active.base_url, api_key=key or '', model=model_id,
                           protocol=active.protocol, max_tokens=runtime.llm.max_tokens)
    client.runtime_config = runtime
    client.capability_missing = capability.get('capability_missing')
    return client


def active_llm_status(kb, cfg: Config) -> dict:
    """Read current selection/credentials without dispatching a model request."""
    from .providers import ProviderStore
    active, model = ProviderStore(kb, cfg.data_path).get_active()
    try:
        client = build_active_llm(kb, cfg, allow_mock=True)
    except ValueError as exc:
        return {'provider': active.name if active else None, 'model': model,
                'protocol': active.protocol if active else None,
                'ready': False, 'mode': 'needs_configuration', 'note': str(exc)}
    if active is None:
        return {'provider': 'mock', 'model': client.name, 'ready': True,
                'mode': 'offline', 'note': '离线占位模型，不调用网络'}
    runtime = client.runtime_config
    return {'provider': active.name, 'provider_id': active.id, 'model': model,
            'protocol': active.protocol, 'ready': True, 'mode': 'selected',
            'model_window_tokens': runtime.llm.model_window_tokens,
            'model_max_output_tokens': runtime.llm.model_max_output_tokens,
            'capability_missing': client.capability_missing, 'note': ''}


def llm_status(cfg: Config) -> dict:
    from .backup import BackupError, require_restored_credentials
    try:
        require_restored_credentials(cfg)
    except BackupError as exc:
        return {'provider': cfg.llm.provider, 'ready': False, 'mode': 'needs_configuration', 'note': str(exc)}
    provider = (cfg.llm.provider or "mock").lower()
    if provider == "mock":
        return {"provider": "mock", "ready": True, "mode": "offline", "note": "离线占位模型，不调用网络"}
    if provider == "ollama":
        return {"provider": "ollama", "ready": True, "mode": "local", "model": cfg.llm.effective_model}
    ready = bool(cfg.llm.api_key)
    return {
        "provider": provider,
        "ready": ready,
        "mode": "api" if ready else "offline-fallback",
        "model": cfg.llm.effective_model,
        "note": "" if ready else "未检测到 API Key，已降级为离线 mock 模型",
    }
