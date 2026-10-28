"""主题只有一个标识：data-theme 取主题 id，不再有 data-skin 这套并行表述。"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'src' / 'litprism' / 'web' / 'static'
THEME_IDS = {'paper', 'paper-dark', 'minimal', 'glass'}


def test_no_parallel_skin_attribute():
    for name in ('app.js', 'app.css', 'index.html'):
        text = (STATIC / name).read_text(encoding='utf-8')
        assert 'data-skin' not in text, f'{name} 仍在用 data-skin'
        assert 'dataset.skin' not in text, f'{name} 仍在写 dataset.skin'


def test_theme_attribute_values_are_known_ids():
    text = (STATIC / 'app.css').read_text(encoding='utf-8')
    used = set(re.findall(r'data-theme="([a-z-]+)"', text))
    assert used, 'CSS 里至少要引用一个主题'
    assert used <= THEME_IDS, f'出现了未知主题：{used - THEME_IDS}'


def test_registry_has_no_palette_or_skin_fields():
    text = (STATIC / 'app.js').read_text(encoding='utf-8')
    block = text[text.index('const THEMES = ['):text.index('const THEME_IDS')]
    assert 'palette:' not in block and 'skin:' not in block, '主题注册表不该再有 palette/skin 两套字段'
