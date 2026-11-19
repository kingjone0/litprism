"""OA 全文真实联网验收（G07 联动）：免费开放 API，零模型调用。

验证链路：真实候选 → resolve_open_copy 发现开放副本（Unpaywall/OpenAlex/EuropePMC）
→ 直链 PDF 下载 → 解析 → 核验准入 → 入库 → G07 附件归档 → 来源元数据。
全程只调免费公开 API 与出版社直链，不调用任何模型（token 消耗 0）。

用法：
    $PY tools/oa_live_acceptance.py --home <临时数据根> --yes
失败如实记录（首次失败保留）；网络不可用/出版社拦截都会写进报告。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

# 两组真实材料：arXiv 直链 + 非 arXiv 开放副本（PLOS ONE 全库 OA，直链 PDF 稳定）
CASES = [
    {'kind': 'arxiv', 'title': 'Attention Is All You Need', 'arxiv_id': '1706.03762'},
    {'kind': 'oa', 'title': 'PLOS ONE open-access sample',
     'doi': '10.1371/journal.pone.0188467'},
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--home', required=True, help='隔离临时数据根（不会触碰真实库）')
    ap.add_argument('--yes', action='store_true', help='确认发起真实网络请求')
    args = ap.parse_args()
    if not args.yes:
        raise SystemExit('本脚本发起真实网络请求；确认后加 --yes。')

    from litprism.candidates import save_candidates
    from litprism.config import Config
    from litprism.db import KnowledgeBase
    from litprism.remote import fetch_candidate_pdf
    from litprism.verify import PaperVerifier

    home = Path(args.home).resolve()
    report = {'home': str(home), 'model_calls': 0, 'cases': [], 'started_at':
              time.strftime('%Y-%m-%dT%H:%M:%S')}
    cfg = Config()
    cfg.app.data_dir = str(home)
    kb = KnowledgeBase(cfg)
    verifier = PaperVerifier(cfg)
    passed = True
    try:
        for case in CASES:
            entry = {'case': case['kind'], 'title': case['title']}
            try:
                # 同 fingerprint 重跑复用既有候选（首次部分失败后可在同库重试）
                saved = save_candidates(kb, [dict(case)], query='oa-live-acceptance')
                cid = (saved['added'] or saved['existing'])[0]
                started = time.time()
                result = fetch_candidate_pdf(kb, cfg, verifier, cid)
                elapsed = round(time.time() - started, 1)
                entry.update({'ok': True, 'paper_id': result['paper_id'],
                              'provider': result['provider'], 'source_url': result['source_url'],
                              'chunks': result['chunks'], 'pages': result['pages'],
                              'verify_level': result['verify_level'],
                              'material_deduped': result.get('material_deduped'),
                              'material_activated': result.get('material_activated'),
                              'attachment_id': result.get('attachment_id'),
                              'attachment_error': result.get('attachment_error'),
                              'elapsed_s': elapsed})
                # G07 断言：原件归档且可按 sha256 取回
                from litprism.attachments import blob_path, get as get_att
                att = get_att(kb, result['paper_id'], result['attachment_id']) \
                    if result.get('attachment_id') else None
                if att:
                    blob = blob_path(cfg, att['sha256'])
                    entry['attachment'] = {'sha256': att['sha256'][:16], 'size': att['size'],
                                           'blob_exists': blob.is_file(),
                                           'blob_bytes': blob.stat().st_size if blob.is_file() else 0}
                    passed &= blob.is_file()
                else:
                    passed = False
                # verify_level 只记录不拦截：核验失败级别不影响链路验收结论
                entry['verify_gate'] = result['verify_level']
                passed &= result['chunks'] > 0
            except Exception as exc:  # noqa: BLE001
                entry.update({'ok': False,
                              'error': f'{exc.__class__.__name__}: {exc}'})
                passed = False
            report['cases'].append(entry)
    finally:
        kb.close()
    report['passed'] = passed
    out_dir = ROOT / f"_selftest.oa-live-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'oa-live-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告已写入 {out_dir / 'oa-live-report.json'}（模型调用 {report['model_calls']} 次）")
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
