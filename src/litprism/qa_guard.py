"""Narrow deterministic risk checks, not a general entailment classifier.

Only cited text is inspected. Ambiguous table attribution and explicit use/not
use reversals are held for review; passing these checks proves no completeness.
"""
from __future__ import annotations

import re
import unicodedata

from .textutil import parse_citations

_NEGATIVE_USE = re.compile(r'(?:(?:did|do|does)\s+not\s+use\s+(?:the\s+)?|(?:未|没有|不)使用\s*)([A-Za-z][A-Za-z0-9_-]{2,})', re.I)
_CATEGORY = re.compile(r'single[- ]model|ensemble|单模型|集成', re.I)
_NUMBERS = re.compile(r'(?<![A-Za-z0-9_.])\d+(?:\.\d+)?')
_LOCATORS = re.compile(r'第\s*\d+(?:\.\d+)*\s*[节章表图]'
                       r'|\b(?:section|sec\.?|table|figure|fig\.?)\s*\d+(?:\.\d+)*', re.I)


def _category(text):
    kinds = set()
    for match in _CATEGORY.finditer(text):
        before = text[max(0, match.start()-18):match.start()]
        if re.search(r'不是|并非|而非|非|\bnot\b|rather than', before, re.I):
            continue
        kinds.add('ensemble' if re.search(r'ensemble|集成', match.group(), re.I) else 'single')
    return next(iter(kinds)) if len(kinds) == 1 else None


def answer_risks(answer, excerpts):
    by_tag = {e['tag']: e for e in excerpts}
    issues = []
    for clause in re.findall(r'[^。！？\n]+[。！？]?(?:\s*\[P\d+[^\]]*\])*', answer):
        sources = [by_tag[tag] for tag, _ in parse_citations(clause) if tag in by_tag]
        if not sources:
            continue
        plain = re.sub(r'\[P\d+[^\]]*\]', '', clause)
        for source in sources:
            text = unicodedata.normalize('NFKC', source['text'])
            for match in _NEGATIVE_USE.finditer(text):
                entity = match.group(1)
                if entity.lower() in ('any', 'the', 'this', 'additional') or not re.search(re.escape(entity), plain, re.I):
                    continue
                positive = re.search(r'在\s*'+re.escape(entity)+r'[^。；]{0,60}上|使用\s*'+re.escape(entity)
                                     +r'|\b(?:using|used|use|on)\s+'+re.escape(entity), plain, re.I)
                negative = re.search(r'(?:未|没有|不|并非|不是)[^。；]{0,12}'+re.escape(entity)
                                     +r'|\bnot\b[^.;]{0,12}'+re.escape(entity), plain, re.I)
                if positive and not negative:
                    issues.append(f"[{source['tag']}] 存在明确未使用 {entity} 的原文，回答的使用/任务归属需核对。")
            category = _category(plain)
            if not category:
                continue
            # Section/table/figure identifiers are locators, not measurements.
            # Retain all remaining values so a locator cannot hide a bad row.
            numbers = set(_NUMBERS.findall(_LOCATORS.sub('', plain)))
            bound = {}
            legacy = set()
            for line in text.splitlines():
                if '【表格行】' not in line:
                    continue
                caption = line.split('{', 1)[0]
                kind = _category(caption)
                for number in _NUMBERS.findall(line.split('{', 1)[-1]):
                    bound.setdefault(number, set()).add(kind)
                    if '邻近表题' in line:
                        legacy.add(number)
            tables = set(re.findall(r'\bTable\s+(\d+(?:\.\d+)*)', text, re.I))
            for number in numbers:
                kinds = bound.get(number, set())
                if number in legacy and len(tables) > 1:
                    issues.append(f"[{source['tag']}] 旧表格行仅关联邻近表题且存在多个表号，需重新核对数值归属。")
                elif kinds and None not in kinds and category not in kinds:
                    issues.append(f"[{source['tag']}] 数值 {number} 的单模型/集成归属与表题不一致。")
                elif not kinds and len(tables) > 1 and re.search(r'(?m)^\s*'+re.escape(number)+r'%?\s*$', text):
                    issues.append(f"[{source['tag']}] 数值 {number} 混在多个表题中且未绑定完整表格行，需核对归属。")
    return list(dict.fromkeys(issues))
