"""跑完整回归并从 JUnit XML 取结论。

为什么要这个：本机 shell 偶尔把 pytest 的 stdout 整个吞掉（0 字节、rc 仍为 0），
看起来像"没跑"；而 `-q` 叠加 pyproject 的 `addopts=-q` 会变成 `-qq`，
连汇总行也一起吞掉。写 XML 再解析与终端输出无关，结论稳定可复现。

用法：$PY tools/run_tests.py
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
os.environ.pop('LITPRISM_HOME', None)

import pytest  # noqa: E402

REPORT = ROOT / '_pytest.xml'


def main() -> int:
    code = pytest.main([str(ROOT / 'tests'), '--no-header', '-o', 'addopts=',
                        '-p', 'no:cacheprovider', f'--junitxml={REPORT}'])
    suite = ET.parse(REPORT).getroot().find('testsuite')
    if suite is None:
        print('无法读取测试报告')
        return 2
    counts = {k: int(suite.get(k, 0)) for k in ('tests', 'failures', 'errors', 'skipped')}
    passed = counts['tests'] - counts['failures'] - counts['errors'] - counts['skipped']
    print(f"tests={counts['tests']} passed={passed} failures={counts['failures']} "
          f"errors={counts['errors']} skipped={counts['skipped']} exit={int(code)}")
    for case in suite.iter('testcase'):
        if case.find('failure') is not None or case.find('error') is not None:
            print('FAILED', case.get('classname'), case.get('name'))
    return int(code)


if __name__ == '__main__':
    raise SystemExit(main())
