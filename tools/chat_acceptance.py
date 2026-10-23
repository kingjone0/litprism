"""N04 对话侧验收：固定问题集 → 四项指标 + 逐题记录。

它测什么
--------
只测**结构性**的四项，不测"答案对不对"（那要人读）：

| 指标 | 定义 | 能自动判定吗 |
|---|---|---|
| 引用命中率 | 展示的引用条目中，能解析到真实可引用片段的占比 | 能 |
| 范围遵守率 | 引用全部落在会话范围内的题数 / 有引用的题数 | 能 |
| 拒答正确率 | 应拒答题中正确拒答的占比；另记"误拒"（应答题却拒答） | 能（需题目标注） |
| 追问可用率 | 多轮追问中，引用指向预期论文的占比 | 能（需题目标注） |

口径：拒答与答错分开统计 —— 拒答是安全行为，答错才是缺陷。
引用命中 ≠ 内容正确：只证明引用指向真实片段。

两种模式
--------
* ``--mode stub``（默认）：确定性桩模型，只验证**度量管道**能跑通。
  数字**不代表模型质量**，输出会明确标注。
* ``--mode live``：真实模型，需 ``--yes`` 确认，会产生费用。

用法
----
    $PY tools/chat_acceptance.py --home <隔离库目录>                    # 桩模式
    $PY tools/chat_acceptance.py --home <目录> --mode live --yes        # 真实模型
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

CITABLE = ('verified', 'manual_confirmed')


class StubLLM:
    """确定性桩：引用第一个片段，用来验证度量管道。

    它**永远**会给出引用，所以桩模式下引用命中率必然接近 100% ——
    这正是为什么桩模式的数字不能当质量结论。
    """

    name = 'stub:chat-acceptance'

    def chat(self, messages, *, json_mode=False, task=None, **kwargs):
        blob = '\n'.join(m.get('content', '') for m in messages if m.get('role') == 'user')
        head = re.search(r'\[(P\d+)\][^\n]*\n(?:第 (\d+) 页)?', blob)
        if not head:
            return '（桩）没有可引用的原文片段。'
        tag, page = head.group(1), head.group(2)
        ref = f'[{tag} p.{page}]' if page else f'[{tag}]'
        return f'（桩模型，仅用于验证度量管道，不代表真实质量）根据原文 {ref}，该问题在片段中有直接表述。'

    def chat_json(self, messages, *, task=None, **kwargs):
        # QAAgent 走 chat_json(task='qa') 取 {'answer': ...}
        if task == 'qa':
            return {'answer': self.chat(messages), 'confidence': 'low'}
        return {}


def resolve_titles(kb, titles):
    """标题子串 → 论文 id。解析不到直接报错，不静默降级成"全部"。"""
    if titles is None:
        return None
    ids = []
    for title in titles:
        row = kb._con.execute(
            'SELECT id FROM papers WHERE deleted_at IS NULL AND (title LIKE ? OR display_title LIKE ?)'
            ' ORDER BY rowid LIMIT 2', (f'%{title}%', f'%{title}%')).fetchall()
        if len(row) != 1:
            raise SystemExit(f'问题集标题必须唯一命中论文：{title}（命中 {len(row)}）')
        ids.append(row[0]['id'])
    return ids


def build_llm(mode, cfg, kb=None):
    if mode == 'stub':
        return StubLLM()
    from litprism.llm import build_active_llm
    if kb is None:
        raise ValueError('真实验收必须显式传入隔离知识库。')
    return build_active_llm(kb, cfg)


def check_citations(kb, excerpts, answer):
    """Check distinct references actually written in the draft, including invalid ones."""
    from litprism.evidence import citation_issues, stored_excerpts
    from litprism.textutil import parse_citations
    by_tag = {e['tag']: e for e in excerpts or []}
    rows = []
    for tag, page in dict.fromkeys(parse_citations(answer)):
        e = by_tag.get(tag)
        row = {'tag': tag, 'page': page, 'chunk_id': None, 'paper_id': None, 'valid': False}
        if e:
            ref = '[' + tag + (f' p.{page}' if page is not None else '') + ']'
            try:
                stored = stored_excerpts(kb, [e])[0]
                row.update(chunk_id=stored['id'], paper_id=stored.get('paper_id'),
                           valid=not citation_issues(ref, [stored]))
            except ValueError:
                pass
        rows.append(row)
    return rows


def run_case(agent, kb, case, conv_id, scope_ids):
    import time

    from litprism.agents import QAAgent

    user = kb.append_message(conv_id, 'user', case['question'], meta={'scope': scope_ids})
    started = time.monotonic()
    ans = QAAgent(kb, agent['cfg'], agent['llm']).ask(
        case['question'], paper_ids=scope_ids, conversation_id=conv_id, before_seq=user['seq'])
    draft = ans.draft_answer or ans.answer
    cites = check_citations(kb, ans.excerpts, draft)
    # Publish only checked references; retain the blocked draft separately for audit.
    published = [e for e in ans.excerpts if e['tag'] in {c['tag'] for c in cites if c['valid']}] if (
        ans.status in ('answered', 'refused', 'needs_review') and ans.citation_check_passed) else []
    kb.append_message(conv_id, 'assistant', ans.answer, citations=published,
                      citation_check={'passed': ans.citation_check_passed, 'issues': ans.citation_issues},
                      meta={'status': ans.status, 'scope': scope_ids, 'usage': ans.usage,
                            'guard_issues': ans.guard_issues,
                            'blocked_answer': draft if ans.status in ('invalid_citation', 'needs_review') else ''})
    return {
        'id': case['id'], 'kind': case.get('kind', 'answerable'), 'question': case['question'],
        'status': ans.status, 'refused': ans.status == 'refused',
        'error': ans.status == 'error', 'citation_check_passed': ans.citation_check_passed,
        'citations': len(cites), 'valid_citations': sum(c['valid'] for c in cites),
        'in_scope': all(c['valid'] and (scope_ids is None or c['paper_id'] in scope_ids) for c in cites),
        'papers': sorted({c['paper_id'] for c in cites if c['valid'] and c['paper_id']}),
        'usage': ans.usage, 'elapsed_seconds': round(time.monotonic()-started, 3),
        'retrieval': ans.retrieval, 'answer': ans.answer, 'draft_answer': draft,
        'request_settings': ans.request_settings,
        'guard_issues': ans.guard_issues,
    }


def usable(row):
    return (not row['error'] and not row['refused'] and row.get('citation_check_passed', True)
            and row.get('status', 'answered') == 'answered'
            and row['citations'] > 0 and row['valid_citations'] == row['citations'])


def quality_passed(rows, cases_by_id):
    if not rows:
        return False
    for row in rows:
        case = cases_by_id[row['id']]
        if case.get('kind') == 'refuse':
            if row['error'] or not row['refused'] or row.get('status') != 'refused':
                return False
        elif not usable(row) or not row['in_scope']:
            return False
        if case.get('kind') == 'followup':
            expected = case.get('_expect_ids') or []
            if not expected or not row.get('parent_usable') or not set(expected) <= set(row['papers']):
                return False
        if case.get('kind') == 'cross' and len(row['papers']) < case.get('expect_min_papers', 2):
            return False
    return True


def judge(rows, cases_by_id):
    """把逐题记录折算成四项指标。分母为 0 一律 None（显示 n/a）。"""
    def ratio(num, den):
        return (num / den) if den else None

    with_cites = [r for r in rows if r['citations']]
    total_cites = sum(r['citations'] for r in rows)
    valid = sum(r['valid_citations'] for r in rows)

    expected_refuse = [r for r in rows if cases_by_id[r['id']].get('kind') == 'refuse']
    answerable = [r for r in rows if cases_by_id[r['id']].get('kind') in ('answerable', 'cross')]
    correct_refuse = [r for r in expected_refuse if r['refused'] and not r['error'] and r.get('status', 'refused') == 'refused']
    false_refuse = [r for r in answerable if r['refused']]

    followups = [r for r in rows if cases_by_id[r['id']].get('kind') == 'followup']
    followup_ok = 0
    for r in followups:
        expect = cases_by_id[r['id']].get('_expect_ids') or []
        if usable(r) and r.get('parent_usable', True) and expect and set(expect) <= set(r['papers']):
            followup_ok += 1

    cross = [r for r in rows if cases_by_id[r['id']].get('kind') == 'cross']
    cross_ok = 0
    for r in cross:
        need = cases_by_id[r['id']].get('expect_min_papers', 2)
        if usable(r) and len(r['papers']) >= need:
            cross_ok += 1

    return {
        '调用失败': {'num': sum(1 for r in rows if r['error']), 'den': len(rows),
                    'ratio': ratio(sum(1 for r in rows if r['error']), len(rows))},
        '引用命中率': {'num': valid, 'den': total_cites, 'ratio': ratio(valid, total_cites)},
        '范围遵守率': {'num': sum(1 for r in with_cites if r['in_scope'] and r['valid_citations'] == r['citations']), 'den': len(with_cites),
                     'ratio': ratio(sum(1 for r in with_cites if r['in_scope'] and r['valid_citations'] == r['citations']), len(with_cites))},
        '拒答正确率': {'num': len(correct_refuse), 'den': len(expected_refuse),
                     'ratio': ratio(len(correct_refuse), len(expected_refuse)),
                     'false_refuse': len(false_refuse), 'answerable': len(answerable)},
        '追问可用率': {'num': followup_ok, 'den': len(followups), 'ratio': ratio(followup_ok, len(followups))},
        '跨论文引用': {'num': cross_ok, 'den': len(cross), 'ratio': ratio(cross_ok, len(cross))},
    }


def pct(v):
    return 'n/a' if v is None else f'{v * 100:.0f}%'


def sweep(kb, cases, values, top_k=8):
    """扫 min_score：看能不能把"应拒答题"的证据压到 0，同时不伤"应答题"。

    只跑检索、不调模型 —— 阈值是检索层的事，用桩或真实模型都会污染结论。
    走**生产同一条路径**（``expand=True``），但 ``llm=None`` 关掉 LLM 改写：
    关键词切分 + 中英词表 + 原文分支仍然生效，结果可复现。
    （``expand=False`` 是另一条更弱的直查路径，用它扫出来的结论不适用。）
    """
    from litprism.search import search_local

    rows = []
    original = kb.cfg.retrieval.min_score
    try:
        for case in cases:
            scope = resolve_titles(kb, case.get('scope_titles'))
            line = {'id': case['id'], 'kind': case.get('kind', 'answerable'),
                    'question': case['question'], 'hits': {}, 'papers': {}, 'top_score': None}
            for value in values:
                kb.cfg.retrieval.min_score = value
                hits = search_local(kb, case['question'], top_k=top_k,
                                    paper_ids=scope, expand=True, llm=None)
                line['hits'][value] = len(hits)
                line['papers'][value] = len({h['paper_id'] for h in hits})
                if value == min(values):
                    line['top_score'] = round(hits[0]['score'], 2) if hits else None
            rows.append(line)
    finally:
        kb.cfg.retrieval.min_score = original
    return rows


def verdict(rows, values):
    """判断绝对阈值能否把"应拒答"与"应答题"分开。

    关键判据不是"能不能压到 0"，而是**分数有没有序**：
    如果无关问题的最高分 ≥ 相关问题的最高分，那么任何绝对阈值都会先误杀应答题。
    """
    def avg(kind, value):
        picked = [r['hits'][value] for r in rows if r['kind'] == kind]
        return sum(picked) / len(picked) if picked else None

    def top(kind):
        picked = [r['top_score'] for r in rows if r['kind'] == kind and r['top_score'] is not None]
        return max(picked) if picked else None

    refuse_top, answer_top = top('refuse'), top('answerable')
    ordered = (refuse_top is not None and answer_top is not None and refuse_top < answer_top)

    out = []
    for value in values:
        refuse = avg('refuse', value)
        answer = avg('answerable', value)
        cross = avg('cross', value)
        separable = refuse == 0 and answer and answer >= 1
        out.append({'min_score': value, 'refuse_avg_hits': refuse,
                    'answerable_avg_hits': answer, 'cross_avg_hits': cross,
                    'separable': bool(separable)})
    return out, {'refuse_top_score': refuse_top, 'answerable_top_score': answer_top,
                 'scores_ordered': ordered}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--home', required=True, help='隔离数据目录（含 data/litprism.db）')
    ap.add_argument('--cases', default=str(Path(__file__).with_name('chat_cases.json')))
    ap.add_argument('--mode', choices=['stub', 'live'], default='stub')
    ap.add_argument('--sweep', default=None,
                    help='只扫检索阈值，不调模型：--sweep 0,0.5,1,2,3')
    ap.add_argument('--yes', action='store_true', help='确认真实模型调用会产生费用')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    if args.mode == 'live' and not args.yes:
        raise SystemExit('真实模式会产生费用；确认后加 --yes。')

    # 必须在 import config 之前设：data_path 在模块导入时就由 APP_ROOT 决定了
    os.environ['LITPRISM_HOME'] = args.home
    if args.out and Path(args.out).exists():
        raise SystemExit('输出文件已存在，请使用新路径，保留先前失败及基线记录。')

    from litprism.config import Config, _from_dict, _validate
    from litprism.db import KnowledgeBase

    home = Path(args.home).resolve()
    if not (home / 'data' / 'litprism.db').is_file():
        raise SystemExit('隔离目录缺少 data/litprism.db；先准备固定样本副本，不创建或访问其他库。')
    config_file = home / 'config' / 'config.yaml'
    if config_file.is_file():
        import yaml
        cfg = _from_dict(yaml.safe_load(config_file.read_text(encoding='utf-8')) or {})
    else:
        cfg = Config()
    _validate(cfg)
    cfg.app.data_dir = str(home / 'data')
    cfg.export.notes_dir = str(home / 'notes')
    kb = KnowledgeBase(cfg)
    frozen = {'cases_sha256': hashlib.sha256(Path(args.cases).read_bytes()).hexdigest(),
              'schema': kb._con.execute('PRAGMA user_version').fetchone()[0],
              'code_sha256': {name: hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
                              for name in ('tools/chat_acceptance.py', 'src/litprism/agents.py',
                                           'src/litprism/qa_retrieval.py', 'src/litprism/context/dialogue.py',
                                           'src/litprism/search.py', 'src/litprism/llm.py')},
              'materials': [dict(r) for r in kb._con.execute(
                  'SELECT id,paper_id,content_hash FROM materials ORDER BY id')]}
    try:
        spec = json.loads(Path(args.cases).read_text(encoding='utf-8'))
        cases = spec['cases']
        by_id = {c['id']: c for c in cases}

        if args.sweep:
            values = [float(v) for v in args.sweep.split(',') if v.strip()]
            rows = sweep(kb, cases, values)
            summary, scores = verdict(rows, values)
            out = {'mode': 'sweep', 'top_k': 8, 'values': values,
                   'rows': rows, 'verdict': summary, 'scores': scores}
            target = Path(args.out) if args.out else home / 'chat_sweep.json'
            target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')

            print(f'{"题号":<6}{"类型":<12}' + ''.join(f'{v:>7}' for v in values))
            for r in rows:
                print(f'{r["id"]:<6}{r["kind"]:<12}' + ''.join(f'{r["hits"][v]:>7}' for v in values))
            print(f'\n{"min_score":<10}{"应拒答题均命中":>14}{"应答题均命中":>14}{"跨论文均命中":>14}  可分?')
            for s in summary:
                print(f'{s["min_score"]:<10}{s["refuse_avg_hits"]:>14.1f}'
                      f'{s["answerable_avg_hits"]:>14.1f}{s["cross_avg_hits"]:>14.1f}'
                      f'  {"是" if s["separable"] else "否"}')
            ok = [s['min_score'] for s in summary if s['separable']]
            print(f'\n应拒答题最高分 {scores["refuse_top_score"]} ｜ '
                  f'应答题最高分 {scores["answerable_top_score"]}')
            if not scores['scores_ordered']:
                print('❌ 分数**无序**：无关问题的 BM25 分不低于相关问题 —— '
                      '任何绝对阈值都会先误杀应答题。阈值不是杠杆，要换判据（相关性筛选）。')
            elif ok:
                print(f'✅ 存在可分区间：min_score ≥ {min(ok)} 时，应拒答题证据为 0 且应答题仍有证据。')
            else:
                print('❌ 分数有序但扫到的区间里没有分离点，扩大扫描范围再看。')
            print(f'明细：{target}')
            return 0

        llm = build_llm(args.mode, cfg, kb)
        agent = {'cfg': cfg, 'llm': llm}
        runtime = getattr(llm, 'runtime_config', cfg)
        frozen['runtime'] = {'model': getattr(llm, 'name', 'unknown'),
                             'window_tokens': runtime.context.window_tokens,
                             'max_output_tokens': runtime.llm.model_max_output_tokens,
                             'top_k': runtime.retrieval.top_k, 'min_score': runtime.retrieval.min_score}

        # 预解析标题 → id；followup 复用被追问那题的会话与范围
        completed = {}
        rows = []
        for case in cases:
            parent = case.get('followup_of')
            if parent and parent not in completed:
                raise ValueError('追问必须排在其父题之后。')
            base = by_id[parent] if parent else case
            scope_ids = completed[parent]['scope'] if parent else resolve_titles(kb, base.get('scope_titles'))
            expect = (resolve_titles(kb, case['expect_cite_titles']) if 'expect_cite_titles' in case
                      else by_id[parent]['_expect_ids'] if parent else resolve_titles(kb, base.get('expect_cite_titles')))
            case['_expect_ids'] = expect

            cid = completed[parent]['conversation_id'] if parent else kb.add_conversation(
                f'验收 {case["id"]}', scope={'paper_ids': scope_ids} if scope_ids is not None else None)
            row = run_case(agent, kb, case, cid, scope_ids)
            row['scope'] = scope_ids
            row['conversation_id'] = cid
            row['parent_usable'] = usable(completed[parent]) if parent else True
            completed[case['id']] = row
            rows.append(row)
            print(f'· {case["id"]} [{row["kind"]}] 拒答={row["refused"]} 引用={row["citations"]}')

        metrics = judge(rows, by_id)
    finally:
        kb.close()

    out = {'version': 3, 'mode': args.mode, 'cases_file': args.cases, 'metrics': metrics, 'rows': rows,
           'quality_passed': quality_passed(rows, by_id) if args.mode == 'live' else None,
           'quality_scope': 'structural_checks_only_semantics_require_human_review', 'frozen': frozen}
    target = Path(args.out) if args.out else home / ('chat_acceptance-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')

    print()
    failed = metrics['调用失败']['num']
    if args.mode == 'stub':
        print('⚠️  桩模式：只验证度量管道，数字不代表模型质量。')
    if failed:
        print(f'❌ {failed}/{metrics["调用失败"]["den"]} 题调用失败 —— 以下指标不可用，先修失败再看数字。')
    print(f'{"指标":<10}{"值":>8}   分子/分母')
    for name, m in metrics.items():
        extra = ''
        if name == '拒答正确率':
            extra = f'（误拒 {m["false_refuse"]}/{m["answerable"]} 应答题）'
        print(f'{name:<10}{pct(m["ratio"]):>8}   {m["num"]}/{m["den"]}{extra}')
    print(f'\n逐题记录：{target}')
    return 2 if failed else (int(not out['quality_passed']) if args.mode == 'live' else 0)


if __name__ == '__main__':
    raise SystemExit(main())
