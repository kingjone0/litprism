

# ---------------------------------------------------------------------------
# 材料完整度：题录/摘要不能被当成整篇论文
# ---------------------------------------------------------------------------

def test_material_level_defaults_and_upgrades_without_downgrade(tmp_path):
    from litprism.config import Config
    from litprism.db import KnowledgeBase, material_label, material_rank

    cfg = Config(); cfg.app.data_dir = str(tmp_path)
    kb = KnowledgeBase(cfg)
    pid = kb.add_paper(title='Only metadata', verify_level='verified', material_level='metadata')
    assert kb.get_paper(pid)['material_level'] == 'metadata'

    # 同年同标题再次导入全文 → 升级
    again = kb.add_paper(title='Only metadata', verify_level='verified', material_level='fulltext')
    assert again == pid and kb.get_paper(pid)['material_level'] == 'fulltext'

    # 再用题录导入同一篇 → 不降级
    kb.add_paper(title='Only metadata', verify_level='verified', material_level='abstract')
    assert kb.get_paper(pid)['material_level'] == 'fulltext'

    assert material_rank('fulltext') > material_rank('abstract') > material_rank('metadata')
    assert material_label('abstract') == '仅题录与摘要'
    kb.close()


def test_migration_infers_material_level_for_existing_rows(tmp_path):
    """老库升级后要能自己判断：有页码块=全文，只有题录摘要块=abstract。"""
    from litprism.config import Config
    from litprism.db import KnowledgeBase

    cfg = Config(); cfg.app.data_dir = str(tmp_path)
    kb = KnowledgeBase(cfg)
    full = kb.add_paper(title='With pages', verify_level='verified')
    kb.add_chunks(full, [{'text': '正文内容', 'page': 3, 'section': '方法'}])
    only = kb.add_paper(title='Abstract only', abstract='摘要文本', verify_level='verified')
    kb.add_chunks(only, [{'text': '标题与摘要', 'page': None, 'section': '题录与摘要'}])
    with kb.tx() as con:
        con.execute('UPDATE papers SET material_level=NULL')
    # 老库不会重跑已应用的迁移，这里直接调用 v9 的回填逻辑
    from litprism.schema import _migrate_v9
    with kb.tx() as con:
        _migrate_v9(con)
    assert kb.get_paper(full)['material_level'] == 'fulltext'
    assert kb.get_paper(only)['material_level'] == 'abstract'
    kb.close()


# ---------------------------------------------------------------------------
# 材料完整度：题录/摘要不能被当成整篇论文
# ---------------------------------------------------------------------------

def test_material_level_defaults_and_upgrades_without_downgrade(tmp_path):
    from litprism.config import Config
    from litprism.db import KnowledgeBase, material_label, material_rank

    cfg = Config(); cfg.app.data_dir = str(tmp_path)
    kb = KnowledgeBase(cfg)
    pid = kb.add_paper(title='Only metadata', verify_level='verified', material_level='metadata')
    assert kb.get_paper(pid)['material_level'] == 'metadata'

    # 同年同标题再次导入全文 → 升级
    again = kb.add_paper(title='Only metadata', verify_level='verified', material_level='fulltext')
    assert again == pid and kb.get_paper(pid)['material_level'] == 'fulltext'

    # 再用题录导入同一篇 → 不降级
    kb.add_paper(title='Only metadata', verify_level='verified', material_level='abstract')
    assert kb.get_paper(pid)['material_level'] == 'fulltext'

    assert material_rank('fulltext') > material_rank('abstract') > material_rank('metadata')
    assert material_label('abstract') == '仅题录与摘要'
    kb.close()


def test_migration_infers_material_level_for_existing_rows(tmp_path):
    """老库升级后要能自己判断：有页码块=全文，只有题录摘要块=abstract。"""
    from litprism.config import Config
    from litprism.db import KnowledgeBase

    cfg = Config(); cfg.app.data_dir = str(tmp_path)
    kb = KnowledgeBase(cfg)
    full = kb.add_paper(title='With pages', verify_level='verified')
    kb.add_chunks(full, [{'text': '正文内容', 'page': 3, 'section': '方法'}])
    only = kb.add_paper(title='Abstract only', abstract='摘要文本', verify_level='verified')
    kb.add_chunks(only, [{'text': '标题与摘要', 'page': None, 'section': '题录与摘要'}])
    with kb.tx() as con:
        con.execute('UPDATE papers SET material_level=NULL')
    # 老库不会重跑已应用的迁移，这里直接调用 v9 的回填逻辑
    from litprism.schema import _migrate_v9
    with kb.tx() as con:
        _migrate_v9(con)
    assert kb.get_paper(full)['material_level'] == 'fulltext'
    assert kb.get_paper(only)['material_level'] == 'abstract'
    kb.close()
