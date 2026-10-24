"""Web, CLI and MCP resolve the same selection without silent fallback."""
from types import SimpleNamespace
import importlib.util

import pytest

from test_workflow_guards import env, paper  # noqa: F401
from litprism import cli
from litprism.llm import AnthropicClient, OpenAICompatClient
from litprism.mcp import server as mcp_server
from litprism.providers import ProviderStore

requires_sdk=pytest.mark.skipif(importlib.util.find_spec('mcp') is None,reason='Optional MCP SDK not installed')


def select(kb,cfg,model='integration-selected',protocol='openai',url='http://127.0.0.1:1/v1'):
    store=ProviderStore(kb,cfg.data_path)
    p=store.create(name='Isolated integration provider',base_url=url,protocol=protocol)
    store.set_models(p.id,[{'model_id':model,'enabled':True,'window_tokens':1000000,'max_output_tokens':384000}])
    assert store.set_active_model(p.id,model)
    return store,p


@pytest.mark.parametrize('protocol,kind',[('openai',OpenAICompatClient),('anthropic',AnthropicClient)])
def test_cli_uses_selected_protocol_model_and_capabilities(env,monkeypatch,protocol,kind):
    cfg,kb,_,_=env
    select(kb,cfg,protocol=protocol)
    monkeypatch.setattr(cli,'load_config',lambda _:cfg)
    ctx=cli.Ctx('isolated-config')
    try:
        client=ctx.llm
        assert isinstance(client,kind) and client.cfg.model=='integration-selected'
        assert client.runtime_config.context.window_tokens==1000000
        assert client.runtime_config.llm.model_max_output_tokens==384000
        assert ctx.cfg.context.window_tokens==1000000
    finally:ctx.kb.close()


@pytest.mark.parametrize('selected',[False,True])
def test_cli_missing_selection_or_key_does_not_become_mock(env,monkeypatch,selected):
    cfg,kb,_,_=env
    cfg.llm.provider='custom';cfg.llm.api_key=''
    if selected:select(kb,cfg,url='https://offline.invalid/v1')
    monkeypatch.setattr(cli,'load_config',lambda _:cfg)
    ctx=cli.Ctx('isolated-config')
    try:
        with pytest.raises(ValueError,match='模型|密钥'):ctx.llm
    finally:ctx.kb.close()


def test_cli_status_reports_selected_model_and_protocol_without_exposing_legacy_key(env,capsys):
    cfg,kb,_,_=env
    select(kb,cfg,protocol='anthropic')
    cfg.llm.api_key='legacy-key-never-print'
    cfg.sources.semantic_scholar.api_key='source-key-never-print'
    assert cli.cmd_config(SimpleNamespace(cfg=cfg,kb=kb),SimpleNamespace())==0
    out=capsys.readouterr().out
    assert 'integration-selected' in out and 'anthropic' in out
    assert 'legacy-key-never-print' not in out
    assert 'source-key-never-print' not in out


def test_cli_stdio_borrows_existing_config_and_never_prints_human_text(env,monkeypatch,capsys):
    cfg,kb,_,_=env
    seen=[]
    monkeypatch.setattr(mcp_server,'serve',lambda **kw:seen.append(kw))
    monkeypatch.setattr(mcp_server,'main',lambda:0)
    assert cli.cmd_mcp(SimpleNamespace(cfg=cfg,kb=kb),SimpleNamespace(action='serve'))==0
    assert seen==[{'kb':kb,'cfg':cfg}]
    assert capsys.readouterr().out==''


@requires_sdk
def test_mcp_missing_key_keeps_read_tools_available_and_reports_reason(env):
    cfg,kb,_,_=env
    select(kb,cfg,url='https://offline.invalid/v1')
    server=mcp_server.build_server(kb,cfg)
    assert server._litprism_impl.search_papers()['ok']
    response=server._litprism_impl.ask('alphatopic')
    assert not response['ok'] and '密钥' in response['error']


@requires_sdk
def test_mcp_resolves_new_selection_for_each_ask(env,monkeypatch):
    cfg,kb,_,_=env
    paper(kb)
    store,p=select(kb,cfg,model='model-a')
    seen=[]
    def answer(self,messages,*,task='',**kwargs):
        if task=='expand':return {'terms':['alphatopic']}
        seen.append(self.cfg.model)
        return {'answer':'Grounded response.[P1]'}
    monkeypatch.setattr(OpenAICompatClient,'chat_json',answer)
    server=mcp_server.build_server(kb,cfg)
    assert server._litprism_impl.ask('alphatopic')['ok']
    store.set_models(p.id,[{'model_id':'model-b','enabled':True,'window_tokens':1000000,'max_output_tokens':384000}])
    store.set_active_model(p.id,'model-b')
    assert server._litprism_impl.ask('alphatopic')['ok']
    assert seen==['model-a','model-b']


@requires_sdk
def test_mcp_logical_error_sets_protocol_flag_and_saves_audit_reason(env):
    import anyio
    cfg,kb,_,_=env
    select(kb,cfg,url='https://offline.invalid/v1')
    server=mcp_server.build_server(kb,cfg)
    async def call():return await server.call_tool('ask',{'question':'alphatopic'})
    result=anyio.run(call)
    assert result.is_error and result.structured_content['ok'] is False
    audit=server._litprism_audit.list()[0]
    assert audit['ok']==0 and '密钥' in audit['error']


@pytest.mark.parametrize('owned',[True,False])
def test_mcp_serve_closes_only_owned_database_on_exit(monkeypatch,owned):
    closed=[]
    fake=SimpleNamespace(_litprism_impl=SimpleNamespace(kb=SimpleNamespace(close=lambda:closed.append(True))),
                         _litprism_owns_kb=owned)
    def run(**kw):raise RuntimeError('controlled exit')
    fake.run=run
    monkeypatch.setattr(mcp_server,'build_server',lambda *a,**kw:fake)
    with pytest.raises(RuntimeError,match='controlled exit'):mcp_server.serve()
    assert closed==([True] if owned else [])


def test_direct_mcp_entry_accepts_explicit_config(env,monkeypatch,capsys):
    cfg,_,_,_=env
    seen=[]
    monkeypatch.setattr(mcp_server,'load_config',lambda path:seen.append(path) or cfg)
    monkeypatch.setattr(mcp_server,'serve',lambda **kw:seen.append(kw))
    assert mcp_server.main(['--config','isolated.yaml'])==0
    assert seen==['isolated.yaml',{'cfg':cfg}]
    assert capsys.readouterr().out==''


def test_cli_unknown_paper_cannot_expand_to_global_or_dispatch_model(env,capsys):
    cfg,kb,_,_=env
    class Context:
        @property
        def llm(self):raise AssertionError('Must resolve scope before model access')
    ctx=Context();ctx.cfg=cfg;ctx.kb=kb
    args=SimpleNamespace(paper=['missing-paper'],question='alphatopic',top_k=5)
    assert cli.cmd_ask(ctx,args)==1
    assert '--paper' in capsys.readouterr().err


@pytest.mark.parametrize('via_env',[False,True,'empty'])
def test_missing_explicit_config_never_falls_through_to_other_workspace(tmp_path,monkeypatch,via_env):
    from litprism import config
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config,'APP_ROOT',tmp_path)
    monkeypatch.setattr(config,'examples_config_path',lambda:None)
    monkeypatch.setattr(config,'ensure_user_config',lambda:None)
    monkeypatch.setattr(config,'_from_dict',lambda _:(_ for _ in ()).throw(AssertionError('wrong fallback')))
    missing=tmp_path/'missing.yaml'
    if via_env is True:monkeypatch.setenv('LITPRISM_CONFIG',str(missing))
    else:monkeypatch.delenv('LITPRISM_CONFIG',raising=False)
    with pytest.raises(FileNotFoundError):config.load_config(None if via_env is True else '' if via_env=='empty' else missing)
