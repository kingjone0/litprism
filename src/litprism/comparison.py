"""对比响应的结构、引文与文献归属检查。"""
from __future__ import annotations

from .evidence import citation_issues
from .textutil import parse_citations


def check_comparison(data, papers, excerpts):
    if not isinstance(data, dict):
        return {}, ['对比结果必须是 JSON 对象。']
    issues = []
    doc_ids = {f'D{i}': p['id'] for i, p in enumerate(papers, 1)}
    by_tag = {e['tag']: e for e in excerpts}
    def check(value):
        if not isinstance(value, str):
            issues.append('对比结论必须是文本。')
        elif value.strip() and value != '未提及':
            issues.extend(citation_issues(value, excerpts))
    summary = data.get('summary') or ''
    check(summary)
    dimensions = data.get('dimensions') or []
    if not isinstance(dimensions, list):
        issues.append('dimensions 必须是数组。')
        dimensions = []
    for d in dimensions:
        if not isinstance(d, dict) or not isinstance(d.get('dimension'), str) or not isinstance(d.get('values'), dict):
            issues.append('对比维度格式错误。')
            continue
        for tag, value in d['values'].items():
            if tag not in doc_ids:
                issues.append(f'未知论文编号 {tag}。')
                continue
            check(value)
            if isinstance(value, str) and value.strip() and value != '未提及':
                cited = [by_tag[t] for t, _ in parse_citations(value) if t in by_tag]
                if not cited or any(e['paper_id'] != doc_ids[tag] for e in cited):
                    issues.append(f'{tag} 单元格引用不属于该论文；跨论文判断应放在一致点/分歧点。')
    for key in ('convergence', 'divergence'):
        values = data.get(key) or []
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            issues.append(f'{key} 必须是数组。')
            values = []
        data[key] = values
        for value in values:
            check(value)
    evidence = data.get('evidence_map') or []
    if not isinstance(evidence, list):
        issues.append('evidence_map 必须是数组。')
        evidence = []
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get('claim'), str):
            issues.append('证据映射格式错误。')
            continue
        refs = item.get('sources') or []
        if isinstance(refs, str):
            refs = [refs]
        item['sources'] = refs
        if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
            issues.append('证据映射来源必须是字符串数组。')
            continue
        issues.extend(citation_issues(' '.join('['+r.strip('[]')+']' for r in refs), excerpts))
    if not summary and not dimensions:
        issues.append('对比结果没有有效结论。')
    data['dimensions'] = dimensions
    data['evidence_map'] = evidence
    return data, list(dict.fromkeys(issues))
