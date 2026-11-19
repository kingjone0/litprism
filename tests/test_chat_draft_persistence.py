"""聊天输入草稿跨刷新持久：localStorage 直写镜像，发送/删会话时同步清除，存储不可用静默降级。"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'src' / 'litprism' / 'web' / 'static'


def _app_js():
    return (STATIC / 'app.js').read_text(encoding='utf-8')


def test_draft_mirror_uses_namespaced_key_and_defensive_storage():
    text = _app_js()
    assert "CHAT_DRAFT_PREFIX = 'litprism-chat-draft:'" in text, '草稿键未沿用 litprism-* 命名约定'
    # 每个存储操作都必须在 try/catch 内：隐私模式/禁用存储抛异常时降级为仅内存，不阻断界面
    for op in ('localStorage.setItem', 'localStorage.removeItem', 'localStorage.getItem'):
        assert re.search(r'try \{[^}]*' + re.escape(op), text, re.S), op


def test_draft_lifecycle_touchpoints():
    text = _app_js()
    # 切换视图/会话时 remember、渲染时恢复、输入即写，三处共用同一组辅助函数
    assert 'if (input?.dataset.convId) setChatDraft(input.dataset.convId, input.value);' in text
    assert 'input.value = getChatDraft(convId);' in text
    assert 'input.oninput = () => setChatDraft(convId, input.value);' in text
    # 发送与删除会话都走同一清除函数（内存与存储同步清，不留孤儿键）
    assert 'clearChatDraft(convId);' in text, '发送后未清除草稿'
    assert 'clearChatDraft(cid);' in text, '删除会话后未清除草稿'
    # 起始问题按钮同样写入草稿：点完立即刷新不丢
    assert re.search(r"\$\$\('\.starter', box\)\.forEach\(b => b\.onclick = \(\) => \{ setChatDraft\(convId", text)
