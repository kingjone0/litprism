"""多轮接线的回归：历史可理解指代，但不能绕过本轮证据与偏好守卫。"""

import copy

import pytest
from fastapi.testclient import TestClient

from litprism.agents import QAAgent
from litprism.config import Config
from litprism.context.dialogue import build_dialogue
from litprism.context.meter import Meter
from litprism.context.policy import SelectionResult
from litprism.context.seam import Seam
from litprism.context.summarizer import ExtractiveSummarizer, Summarizer
from litprism.db import KnowledgeBase
from litprism.memory.store import MemoryStore
from litprism.memory.view import TurnViewStore


class RecordingLLM:
    name = "test"

    def __init__(self):
        self.requests = []
        self.answer = "该方法只采用注意力机制。[P1 p.3]"

    def chat_json(self, messages, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        return {"answer": self.answer, "confidence": "high"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    cfg.ensure_dirs()
    kb = KnowledgeBase(cfg)
    conv = kb.add_conversation("注意力机制研究")
    pid = kb.add_paper(title="Attention Study", verify_level="verified", verify_status="verified")
    kb.add_chunks(pid, [{"text": "The method uses only attention mechanisms.", "page": 3, "section": "方法"}])
    chunk = dict(kb._con.execute("SELECT * FROM chunks WHERE paper_id = ?", (pid,)).fetchone())
    hit = {**chunk, "paper_title": "Attention Study", "score": 1.0}
    queries = []

    def retrieve(kb, query, **kwargs):
        queries.append(query)
        return [hit]

    monkeypatch.setattr("litprism.agents.search_local", retrieve)
    llm = RecordingLLM()
    yield cfg, kb, conv, llm, queries, hit
    kb.close()


def test_api_followup_sends_history_preferences_and_authoritative_sources(env, monkeypatch):
    cfg, kb, conv, llm, queries, hit = env
    from litprism.web import server

    monkeypatch.setattr(server, "KnowledgeBase", lambda cfg: kb)
    monkeypatch.setattr(server, "build_llm", lambda cfg: llm)
    MemoryStore(kb, cfg).save("回答请使用简短中文", kind="style_preference")
    client = TestClient(server.create_app(cfg))
    first = client.post(f"/api/chat/{conv}", json={"question": "注意力方法是什么？"})
    assert first.status_code == 200, first.text
    followup = client.post(f"/api/chat/{conv}", json={"question": "它有哪些局限？"})
    assert followup.status_code == 200, followup.text
    prompt = "\n".join(m["content"] for m in llm.requests[-1])
    assert "注意力方法是什么？" in prompt and "该方法只采用注意力机制" in prompt
    assert "回答请使用简短中文" in prompt and "不得作为事实依据" in prompt
    assert prompt.count("【问题】它有哪些局限？") == 1
    assert "注意力方法是什么？" in queries[-1]
    assert "历史引用，本轮无效" in prompt
    source = followup.json()["sources"][0]["component"]["items"][0]
    assert source["page"] == 3 and source["text"] == hit["text"]
    # 伪造保存的页码，重新打开会话时仍以数据库原文为准。
    kb._con.execute("UPDATE messages SET citations = REPLACE(citations, '\"page\": 3', '\"page\": 999') WHERE role = 'assistant'")
    kb._con.commit()
    loaded = client.get(f"/api/conversations/{conv}/messages").json()
    assert loaded["sources"][0]["component"]["items"][0]["page"] == 3
    view = TurnViewStore(kb).latest(conv)
    assert view.read_refs["history"] and view.read_refs["resident"]
    assert view.read_refs["retrieved"] == [hit["id"]]


def test_no_retrieval_refuses_despite_history_and_memory(env, monkeypatch):
    cfg, kb, conv, llm, _, _ = env
    kb.append_message(conv, "assistant", "旧回答声称实验很好。[P1 p.3]")
    MemoryStore(kb, cfg).save("偏好肯定的表达", kind="style_preference")
    monkeypatch.setattr("litprism.agents.search_local", lambda *a, **kw: [])
    result = QAAgent(kb, cfg, llm).ask("它的实验如何？", conversation_id=conv)
    assert result.insufficient_evidence and not llm.requests


@pytest.mark.parametrize("answer", [
    "根据我的记忆，该方法效果最好。[P1 p.3]",
    "该方法效果最好。[mem_fake]",
    "该方法效果最好。[P99 p.3]",
    "该方法效果最好。[P1 p.999]",
])
def test_history_does_not_relax_citation_or_memory_guards(env, answer):
    cfg, kb, conv, llm, _, _ = env
    kb.append_message(conv, "assistant", "旧轮次使用 [P99 p.3]")
    llm.answer = answer
    result = QAAgent(kb, cfg, llm).ask("局限是什么？", conversation_id=conv)
    assert not result.citation_check_passed and result.citation_issues


def test_compacted_originals_are_replaced_by_one_summary(env):
    cfg, kb, conv, llm, _, _ = env
    old = kb.append_message(conv, "user", "UNIQUE_OLD_ORIGINAL " + "注意力的研究背景。" * 400)
    kb.append_message(conv, "assistant", "该研究讨论了注意力机制。" * 400)
    kb.append_message(conv, "user", "接下来研究方法局限。")
    msgs = kb.get_messages(conv)
    tokens = sum(Meter(cfg).estimate_message(m) for m in msgs[:2])
    selection = SelectionResult(ok=True, start_seq=1, end_seq=2,
                                shadowed_refs=[m["ref"] for m in msgs[:2]], shadowed_tokens=tokens)
    seam = Seam(kb, cfg)
    tx, _ = seam.begin(conv, "nudge", selection)
    summary = Summarizer(cfg, backend=ExtractiveSummarizer()).summarize(msgs[:2], tokens)
    assert summary.ok
    assert seam.commit(tx, summary).ok
    context = build_dialogue(kb, cfg, conv, None)
    assert old["ref"] not in [m["ref"] for m in context.history]
    checkpoints = [m for m in context.history if m["kind"] == "compact-checkpoint"]
    assert len(checkpoints) == 1
    history, refs = context.prompt(Meter(cfg), 4000)
    assert history.count("历史摘要:") == 1
    assert old["ref"] not in refs


def test_history_budget_and_conversation_isolation(env):
    cfg, kb, conv, llm, queries, _ = env
    kb.append_message(conv, "user", "OLD_LARGE " * 400)
    kb.append_message(conv, "user", "注意力机制")
    other = kb.add_conversation("另一个研究")
    kb.append_message(other, "user", "PRIVATE_OTHER_CONVERSATION")
    context = build_dialogue(kb, cfg, conv, None)
    text, _ = context.prompt(Meter(cfg), 100)
    assert "注意力机制" in text and "OLD_LARGE" not in text
    assert "PRIVATE_OTHER_CONVERSATION" not in text
    assert Meter(cfg).estimate_text(text) <= 100
    assert context.prompt(Meter(cfg), 0) == ("", [])
    QAAgent(kb, cfg, llm).ask("全新的量子计算问题", conversation_id=conv)
    assert queries[-1] == "全新的量子计算问题"
