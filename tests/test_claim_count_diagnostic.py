"""Count estimates remain diagnostics; they cannot determine semantic quality."""
from litprism.gates import evaluate


def report(count):
    return evaluate({'key_conclusions': ''.join(f'第{i}条候选 [P1 p.1]。' for i in range(count))},
                    [('key_conclusions', '结论')], expected=(12, 22))


def test_above_estimate_stays_flagged_without_claiming_fabrication():
    gates = report(26)
    gate = next(g for g in gates.results if g.name == 'section_coverage')
    assert not gate.ok and gate.action == 'flag' and not gates.needs_repair
    assert '26' in gate.detail and '12–22' in gate.detail
    assert '高于陈述数估计' in gate.detail
    assert '疑似重复或编造' not in gate.detail
    assert '数量提示' in gate.detail


def test_within_estimate_does_not_certify_completeness_or_correctness():
    gates = report(22)
    gate = next(g for g in gates.results if g.name == 'section_coverage')
    assert gate.ok and gate.action == 'pass' and not gates.failed
    assert '不代表内容完整或正确' in gate.detail
