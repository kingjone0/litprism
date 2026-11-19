"""前端现代化契约：IME 安全提交、toast 播报、主题首帧、弹窗滚动隔离与声明式关闭。"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'src' / 'litprism' / 'web' / 'static'
THEME_IDS = {'paper', 'paper-dark', 'minimal', 'glass'}


def _read(name):
    return (STATIC / name).read_text(encoding='utf-8')


def test_verify_input_enter_is_ime_safe():
    text = _read('app.js')
    # 纯 Enter 提交必须跳过 IME 组词期（isComposing + keyCode 229 兜底）
    assert 'if (e.isComposing || e.keyCode === 229) return;' in text
    # 聊天 Ctrl+Enter 同样需要守卫：macOS Chromium 组词期携带真实 key 值（审计 F4）
    # 锚守卫代码本身（不锚注释措辞），count>=2 覆盖 verifyInput 与聊天两处
    assert re.search(r"e\.key === 'Enter'\) \{[^}]*?if \(e\.isComposing \|\| e\.keyCode === 229\) return;", text, re.S)
    assert text.count('if (e.isComposing || e.keyCode === 229) return;') >= 2


def test_toasts_are_announced_by_screen_readers():
    html = _read('index.html')
    assert re.search(r'id="toasts"[^>]*aria-live="polite"', html), 'toast 容器缺 aria-live'
    assert re.search(r'id="toasts"[^>]*role="region"', html), 'toast 容器缺 region 语义'
    assert re.search(r'id="toasts"[^>]*aria-label="通知"', html)
    js = _read('app.js')
    assert "node.setAttribute('role', 'alert')" in js, '错误 toast 未标记 alert'


def test_index_html_theme_default_is_valid_and_fouc_guarded():
    html = _read('index.html')
    m = re.search(r'<html lang="zh-CN" data-theme="([a-z-]+)">', html)
    assert m and m.group(1) in THEME_IDS, 'html 静态默认主题必须是合法 id（不得再用 legacy light）'
    # 样式表生效前按存储主题设置属性，仅允许白名单 id
    assert "localStorage.getItem('litprism-theme')" in html
    assert "['paper', 'paper-dark', 'minimal', 'glass'].indexOf(storedTheme)" in html
    assert '<meta name="color-scheme" content="light dark">' in html


def test_modal_dialogs_contain_scroll_chaining():
    css = _read('app.css')
    assert 'dialog:modal { overscroll-behavior: contain; }' in css
    assert re.search(r'dialog::backdrop \{ overflow: hidden; overscroll-behavior: contain; \}', css)


def test_composer_input_grows_via_field_sizing():
    css = _read('app.css')
    assert re.search(r'\.composer-input \{[^}]*field-sizing: content;', css, re.S)
    # 上限沿用既有 max-height，超限回滚为固定高度滚动
    assert 'max-height: 180px' in css


def test_dialog_cancel_uses_invoker_commands_with_js_fallback():
    js = _read('workflows.js')
    assert 'command="close" commandfor="researchTask"' in js
    assert "if (!('commandForElement' in HTMLButtonElement.prototype)) {" in js
    # 命令按钮都在 form 内，必须显式 type="button"（指南 MANDATORY）
    assert re.search(r'<button type="button" class="btn" data-cancel command="close" commandfor="researchTask">', js)


def test_boot_path_has_no_cross_file_load_order_hazards():
    """app.js 先于 workflows.js 执行：boot 路径引用的状态与函数必须定义在 app.js，
    且 openView 必须有就绪闸门（c2ae4b0/批次A 修复的脚本加载顺序竞态，防回退）。"""
    app = _read('app.js')
    wf = _read('workflows.js')
    # 数据依赖必须在 app.js 的 S 字面量内定义（锚定 S 字面量缩进形式，防匹配注释）；
    # workflows.js 不得新增任何顶层 S.* 赋值
    assert re.search(r"^\s+selectedPapers: new Set\(\),$", app, re.M)
    assert re.search(r"^\s+collections: \[\],$", app, re.M)
    assert not re.search(r"^S\.\w+ =", wf, re.M)
    # boot 路径函数必须唯一定义在 app.js
    for fn in ('syncBatchActions', 'confirmReviewLeave', 'paperMaterialLabel', 'collectionLabel'):
        assert app.count(f'function {fn}') == 1 and f'function {fn}' not in wf, fn
    # openView loader：不得急切求值对象字面量，12 个分支齐全
    assert 'library: loadLibrary' not in app
    for view, loader in (('library', 'loadLibrary'), ('paper', 'loadPaperDetail'), ('chat', 'loadChat'),
                         ('deliverables', 'loadDeliverables'), ('skills', 'loadSkills'), ('verify', 'loadVerify'),
                         ('compare', 'loadComparison'), ('memories', 'loadMemories'), ('trash', 'loadTrash'),
                         ('knowledge', 'loadKnowledge'), ('recommend', 'loadCandidates'),
                         ('annotations', 'loadAllAnnotations')):
        assert f"view === '{view}' ? {loader}" in app, view
    # 就绪闸门：workflows.js 置位 + openView 顶部检查
    assert 'window.__WORKFLOWS_READY__ = true' in wf
    assert 'window.__PENDING_VIEWS__ = window.__PENDING_VIEWS__ || []).push' in app
    assert 'window.__WORKFLOWS_READY__' in app
