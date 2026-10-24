"""Actual SDK client + child process, with isolated config and no external calls."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

from litprism.config import Config, config_to_dict
from litprism.db import KnowledgeBase


def tool():
    path=Path(__file__).resolve().parents[1]/'tools/integration_acceptance.py'
    spec=importlib.util.spec_from_file_location('n09_integration',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def config(tmp_path):
    import yaml
    cfg=Config();cfg.app.data_dir=str(tmp_path/'data');cfg.export.notes_dir=str(tmp_path/'notes')
    cfg.llm.provider='custom'
    for name in ('crossref','openalex','arxiv','semantic_scholar','unpaywall'):
        getattr(cfg.sources,name).enabled=False
    path=tmp_path/'isolated.yaml'
    path.write_text(yaml.safe_dump(config_to_dict(cfg),allow_unicode=True),encoding='utf-8')
    return cfg,path


@pytest.mark.skipif(importlib.util.find_spec('mcp') is None,reason='Optional MCP SDK not installed')
@pytest.mark.parametrize('module',['litprism.mcp.server','litprism'])
def test_real_stdio_handshake_read_error_and_clean_exit(tmp_path,module):
    import anyio
    cfg,path=config(tmp_path);t=tool();kb=KnowledgeBase(cfg)
    pid=kb.add_paper(title='Isolated stdio fixture',verify_level='verified')
    kb.add_chunks(pid,[{'text':'alphatopic fixture source.','page':1}])
    args=['--config',str(path)]+(['mcp','serve'] if module=='litprism' else [])
    async def exercise(session):
        assert {t.name for t in (await session.list_tools()).tools}=={
            'search_papers','get_paper','search_passages','verify_paper','list_collections','get_graph','compare_papers','ask'}
        sources=t.data(await session.call_tool('search_passages',{'query':'alphatopic'}))
        assert sources['ok'] and sources['items'][0]['paper_id']==pid
        failed=await session.call_tool('ask',{'question':'alphatopic'})
        assert failed.is_error and t.data(failed)['ok'] is False
        assert t.data(await session.call_tool('search_papers',{}))['ok']
        return {'checked':True}
    try:
        result=anyio.run(t.wire,tmp_path,cfg,'test',t.python_command(module,args),exercise)
        assert result['exit_code']==0 and result['stdout_json_messages']>=5
        audit=kb._con.execute("SELECT ok,error FROM mcp_audit WHERE tool='ask'").fetchone()
        assert audit['ok']==0 and audit['error']
    finally:kb.close()


@pytest.mark.parametrize('module,exit_code',[('litprism.mcp.server',2),('litprism',1)])
def test_bad_config_child_exits_without_protocol_noise_or_database(tmp_path,module,exit_code):
    cfg,_=config(tmp_path);t=tool()
    args=['--config',str(tmp_path/'missing.yaml')]+(['mcp','serve'] if module=='litprism' else [])
    result=subprocess.run(t.python_command(module,args),env=t.process_env(tmp_path,cfg),cwd=t.ROOT,
        input='',text=True,capture_output=True,encoding='utf-8',timeout=15)
    assert result.returncode==exit_code and result.stdout=='' and result.stderr
    assert not cfg.db_path.exists()
