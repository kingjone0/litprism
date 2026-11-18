"""Automatic compaction for Web QA, using local extractive summaries."""

from dataclasses import replace

from ..refs import make_ref
from .dialogue import build_dialogue
from .meter import Meter
from .policy import Policy
from .seam import Seam
from .summarizer import ExtractiveSummarizer, Summarizer, frame_summary


def compact_dialogue(kb, cfg, conversation_id, tail_seq, *, dialogue=None, history_budget=None):
    """Caller serializes the compaction with the run or reply transaction."""
    meter = Meter(cfg)
    policy = Policy(cfg)
    messages = kb.get_messages(conversation_id)
    if not messages or messages[-1]['seq'] != tail_seq:
        return None
    measurement = meter.measure(kb, conversation_id)
    before_send = history_budget is not None
    trigger, aggressive = 'pressure', False
    if not before_send:
        decision = policy.decide_trigger(
            measurement, messages=messages, protected=policy.protected_zones(measurement, messages),
            anchor_seq=kb.resolve_conversation(conversation_id)['compact_anchor_seq'] or 0,
        )
        if not decision.should_compact:
            return None
        trigger, aggressive = decision.trigger, decision.emergency_truncate
        dialogue = build_dialogue(kb, cfg, conversation_id, None)

    selection = policy.select(measurement, messages=messages, aggressive=aggressive,
                              retain_tokens=history_budget,
                              eligible_refs={m['ref'] for m in dialogue.history})
    if not selection.ok:
        return None

    chosen = set(selection.shadowed_refs)
    selected = [m for m in messages if m['ref'] in chosen]
    summary = Summarizer(cfg, backend=ExtractiveSummarizer()).summarize(selected, selection.shadowed_tokens)
    if before_send and summary.ok:
        summary = _fit_summary(summary, dialogue, chosen, tail_seq + 1, meter, history_budget)
        if summary is None:
            return None

    seam = Seam(kb, cfg)
    tx_id, _ = seam.begin(conversation_id, trigger, selection)
    if not tx_id:
        return None
    if not summary.ok:
        seam.fail(tx_id, 'summary', summary.error)
        return None
    result = seam.commit(tx_id, summary)
    if not result.ok:
        return None
    return {'trigger': trigger, 'range': selection.range_text, 'shadowed_tokens': selection.shadowed_tokens,
            'saved_per_turn': result.ledger['saved_per_turn'], 'block_id': result.block_id,
            'checkpoint_ref': result.replacement_ref, 'phase': 'before_send' if before_send else 'after_answer'}


def _fit_summary(summary, dialogue, chosen, seq, meter, budget):
    """Shorten the extract if necessary to keep the newest full turn with it."""
    ref = make_ref(seq)
    checkpoint = {'ref': ref, 'seq': seq, 'role': 'user', 'kind': 'compact-checkpoint'}
    projected = [m for m in dialogue.history if m['ref'] not in chosen] + [checkpoint]
    preview = replace(dialogue, history=projected)
    lines = summary.body.splitlines()
    for count in range(len(lines), 1, -1):
        body = '\n'.join(lines[:count])
        framed = frame_summary(body)
        checkpoint['content'] = framed
        if ref in preview.prompt(meter, budget)[1]:
            return replace(summary, body=body, framed=framed, framed_tokens=meter.estimate_text(framed))
    return None
