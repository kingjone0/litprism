"""安装验收工作流的默认 tag 必须指向真实发布，不能是占位符。"""
import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'install-lifecycle.yml'


def test_default_release_tag_is_a_real_version():
    text = WORKFLOW.read_text(encoding='utf-8')
    match = re.search(r"^\s+default:\s*'([^']+)'\s*$", text, re.M)
    assert match, '工作流缺少默认 release tag'
    tag = match.group(1)
    assert re.fullmatch(r'v\d+\.\d+\.\d+', tag), f'默认 tag 仍是占位值：{tag}'
    assert '-ci.' not in tag, f'默认 tag 不能停留在预发布占位：{tag}'
