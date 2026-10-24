"""N09 real process/stdio and loopback HTTP acceptance; no external API calls.

Uses synthetic documents and local protocol fixtures, not a scientific quality
sample. All databases/configuration/transcripts stay in a new _selftest root.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def python_command(module,args):
    # Preserve the running interpreter's installed dependencies, including .pth
    # initialization when an older venv's compatible packages were loaded.
    sites=[p for p in sys.path if Path(p).name=='site-packages']
    bootstrap=(f'import site,sys,runpy;[site.addsitedir(p) for p in {sites!r}];'
               f'sys.argv=[{module!r},*sys.argv[1:]];runpy.run_module({module!r},run_name="__main__")')
    return [sys.executable,'-c',bootstrap,*args]


def process_env(home,cfg):
    env=dict(os.environ)
    for key in list(env):
        if key.startswith('LITPRISM_') or key.endswith('_API_KEY') or key=='S2_API_KEY':
            env.pop(key)
    env.update(LITPRISM_HOME=str(home),LITPRISM_DATA_DIR=str(cfg.data_path),
        PYTHONPATH=str(ROOT/'src'),PYTHONIOENCODING='utf-8',PYTHONUTF8='1',
        NO_PROXY='127.0.0.1,localhost,::1',no_proxy='127.0.0.1,localhost,::1')
    return env


@contextmanager
def protocol_fixture(trace):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/v1/search-fail':
                data=b'{"error":"isolated search failure fixture"}'
                self.send_response(503);self.send_header('Content-Length',str(len(data)))
                self.end_headers();self.wfile.write(data);return
            is_qa='【片段】' in str(body['messages'])
            anthropic=self.path.endswith('/messages')
            assert self.path in ('/v1/messages','/v1/chat/completions')
            trace.append({'path':self.path,'model':body['model'],'task':'qa' if is_qa else 'expand',
                'max_tokens':body['max_tokens'],
                'protocol_shape_valid':isinstance(body.get('system'),str) if anthropic else body['messages'][0]['role']=='system',
                'auth_present':bool(self.headers.get('x-api-key') if anthropic else self.headers.get('Authorization'))})
            value={'answer_units':[{'text':'Fixture evidence.[P1]','subject':'Isolated fixture','basis':'direct',
                'sources':['P1'],'quotes':[{'source':'P1','quote':'alphatopic integration evidence.'}]}]} if is_qa else {'terms':['alphatopic']}
            text=json.dumps(value)
            result=({'content':[{'type':'text','text':text}],'stop_reason':'end_turn','usage':{'input_tokens':30,'output_tokens':20}}
                if anthropic else {'choices':[{'message':{'content':text},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}})
            data=json.dumps(result).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
    try:yield f'http://127.0.0.1:{http.server_port}/v1'
    finally:http.shutdown();http.server_close();worker.join(timeout=5)


def data(result):
    value=result.model_dump(by_alias=True,exclude_none=True)
    if 'structuredContent' in value:return value['structuredContent']
    texts=[x['text'] for x in value.get('content',[]) if x.get('type')=='text']
    return json.loads(texts[0]) if texts else value


async def wire(home,cfg,label,command,exercise):
    import anyio
    from mcp.client import stdio
    from mcp.client.session import ClientSession
    transcript,processes=[],[]
    original_parse,original_spawn=stdio._parse_line,stdio._create_platform_compatible_process
    def parse(line):
        transcript.append(json.loads(line))  # Reject any non-protocol stdout.
        return original_parse(line)
    async def spawn(*args,**kwargs):
        proc=await original_spawn(*args,**kwargs);processes.append(proc);return proc
    stdio._parse_line,stdio._create_platform_compatible_process=parse,spawn
    try:
        with (home/(label+'-stderr.txt')).open('w',encoding='utf-8') as errlog:
            params=stdio.StdioServerParameters(command=command[0],args=command[1:],env=process_env(home,cfg),cwd=ROOT)
            with anyio.fail_after(30):
                async with stdio.stdio_client(params,errlog=errlog) as (reader,writer):
                    async with ClientSession(reader,writer,read_timeout_seconds=10) as session:
                        initialized=await session.initialize()
                        assert initialized.server_info.name=='litprism'
                        result=await exercise(session)
        assert len(processes)==1 and processes[0].returncode==0
        result.update(protocol_version=initialized.protocol_version,pid=processes[0].pid,
            exit_code=processes[0].returncode,stdout_json_messages=len(transcript))
        write(home/(label+'-stdout.json'),transcript)
        return result
    finally:
        stdio._parse_line,stdio._create_platform_compatible_process=original_parse,original_spawn


def run(home):
    import anyio
    import yaml
    from fastapi.testclient import TestClient
    from litprism.config import Config,config_to_dict
    from litprism.db import KnowledgeBase
    from litprism.providers import ProviderStore
    from litprism.web import server
    home=home.resolve()
    if ROOT not in home.parents or not home.name.startswith('_selftest.'):
        raise ValueError('Use a new _selftest root inside the workspace')
    home.mkdir(parents=True,exist_ok=False)
    cfg=Config();cfg.app.data_dir=str(home/'data');cfg.export.notes_dir=str(home/'notes')
    cfg.llm.provider='custom'  # No selection must fail, never an implicit mock.
    for name in ('crossref','openalex','arxiv','semantic_scholar','unpaywall'):
        getattr(cfg.sources,name).enabled=False
    config=home/'isolated.yaml'
    config.write_text(yaml.safe_dump(config_to_dict(cfg),allow_unicode=True),encoding='utf-8')
    kb=KnowledgeBase(cfg)
    pid=kb.add_paper(title='N09 isolated protocol fixture',verify_level='verified',material_level='fulltext')
    kb.add_chunks(pid,[{'text':'alphatopic integration evidence.','page':1}])
    bad=kb.add_paper(title='N09 unverified fixture',verify_level='unchecked')
    kb.add_chunks(bad,[{'text':'alphatopic unverified must not be cited.','page':1}])
    original_kb=server.KnowledgeBase
    server.KnowledgeBase=lambda _:kb
    try:app=server.create_app(cfg)
    finally:server.KnowledgeBase=original_kb
    client=TestClient(app);trace=[];summary={'external_api_calls':0,'scientific_quality_sample':False}
    command=lambda module,args:python_command(module,args)
    def cli_call(args,label):
        result=subprocess.run(command('litprism',['--config',str(config),*args]),env=process_env(home,cfg),cwd=ROOT,
            text=True,capture_output=True,encoding='utf-8',timeout=20)
        (home/(label+'-stdout.txt')).write_text(result.stdout,encoding='utf-8')
        (home/(label+'-stderr.txt')).write_text(result.stderr,encoding='utf-8')
        return result
    try:
        before=len(trace)
        missing=client.post('/api/ask',json={'question':'alphatopic'})
        assert missing.status_code==400
        assert cli_call(['ask','alphatopic'],'cli-unconfigured').returncode==1
        assert len(trace)==before
        summary['unconfigured_web_cli_refused']=True
        with protocol_fixture(trace) as url:
            store=ProviderStore(kb,cfg.data_path);providers={}
            for protocol in ('openai','anthropic'):
                p=store.create(name='Isolated '+protocol,base_url=url,protocol=protocol,api_key='isolated-transport-fixture')
                model='fixture-'+protocol
                store.set_models(p.id,[{'model_id':model,'enabled':True,'window_tokens':1000000,'max_output_tokens':384000}])
                providers[protocol]=(p.id,model)
            async def direct(session):
                tools=await session.list_tools();names={t.name for t in tools.tools}
                assert len(names)==8 and 'import_paper' not in names
                found=data(await session.call_tool('search_passages',{'query':'alphatopic'}))
                assert found['ok'] and {x['paper_id'] for x in found['items']}=={pid}
                assert data(await session.call_tool('search_papers',{'query':'N09'}))['count']==2
                library=await session.read_resource('litprism://library')
                assert 'N09 isolated protocol fixture' in str(library)
                matrix=[]
                for protocol,(provider,model) in providers.items():
                    store.set_active_model(provider,model)
                    cid=kb.add_conversation('N09 Web '+protocol,scope={'paper_ids':[pid],'mode':'strict'})
                    posted=client.post('/api/conversations/'+cid+'/chat-runs',json={'question':'alphatopic',
                        'request_id':'n09-'+protocol,'web_search':False});posted.raise_for_status()
                    rid=posted.json()['run_id'];deadline=time.monotonic()+10
                    while True:
                        state=client.get('/api/chat-runs/'+rid).json()
                        if not state['active']:break
                        if time.monotonic()>deadline:raise TimeoutError('Local Web fixture did not finish')
                        await anyio.sleep(.02)
                    answer=client.get('/api/chat-runs/'+rid+'/result');answer.raise_for_status()
                    assert answer.json()['status']=='answered' and model in answer.json()['model']
                    cli_result=cli_call(['ask','alphatopic','--paper',pid],'cli-'+protocol)
                    assert cli_result.returncode==0 and 'Fixture evidence.' in cli_result.stdout
                    start=len(trace);mcp=data(await session.call_tool('ask',{'question':'alphatopic'}))
                    assert mcp['ok'] and mcp['status']=='answered' and model in mcp['model']
                    assert trace[start:] and all(t['model']==model for t in trace[start:])
                    matrix.append({'protocol':protocol,'model':model,'web':'answered','cli_exit':0,'mcp':'answered'})
                corrupt=store.create(name='Missing-key fixture',base_url='https://offline.invalid/v1',chat_model='missing-key')
                store.set_active_model(corrupt.id,'missing-key')
                start=len(trace);error_result=await session.call_tool('ask',{'question':'alphatopic'})
                assert error_result.is_error
                failed=data(error_result)
                assert not failed['ok'] and len(trace)==start
                assert data(await session.call_tool('search_papers',{}))['ok']
                assert cli_call(['ask','alphatopic'],'cli-missing-key').returncode==1
                assert client.post('/api/ask',json={'question':'alphatopic'}).status_code==400
                hidden=await session.call_tool('import_paper',{'title':'Must remain hidden'})
                assert hidden.is_error
                return {'matrix':matrix,'tool_count':len(names),'missing_key_refused':True,'readonly_write_hidden':True}
            summary['direct_stdio']=anyio.run(wire,home,cfg,'direct',
                command('litprism.mcp.server',['--config',str(config)]),direct)
            async def cli_stdio(session):
                found=data(await session.call_tool('search_papers',{'query':'N09 isolated protocol fixture'}))
                assert found['ok'] and found['items'][0]['id']==pid
                return {'explicit_config_preserved':True}
            summary['cli_stdio']=anyio.run(wire,home,cfg,'cli-stdio',
                command('litprism',['--config',str(config),'mcp','serve']),cli_stdio)
            writable=copy.deepcopy(cfg);writable.mcp.readonly=False
            writable_path=home/'writable.yaml'
            writable_path.write_text(yaml.safe_dump(config_to_dict(writable),allow_unicode=True),encoding='utf-8')
            async def write_guard(session):
                names={t.name for t in (await session.list_tools()).tools}
                assert 'import_paper' in names
                refused=await session.call_tool('import_paper',{'title':'Unverified import must fail'})
                assert refused.is_error
                result=data(refused)
                assert not result['ok'] and len(kb.list_papers())==2
                return {'tool_count':len(names),'unverified_import_refused':True}
            summary['write_guard']=anyio.run(wire,home,cfg,'writable',
                command('litprism.mcp.server',['--config',str(writable_path)]),write_guard)
            assert all(t['protocol_shape_valid'] and t['auth_present'] and t['max_tokens']==384000 for t in trace)
            # Exercise the production discovery failure path against a real local
            # HTTP error, without sending even a synthetic credential externally.
            import requests
            post=requests.post
            cfg.websearch.provider='tavily'
            cfg.websearch.api_key_ref=store.secrets.set('search_fixture','isolated-search-fixture')
            count=kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0]
            def local_search(target,*args,**kwargs):
                if target!='https://api.tavily.com/search':
                    raise AssertionError('Unexpected request during search failure control')
                return post(url+'/search-fail',*args,**kwargs)
            requests.post=local_search
            try:
                failed=client.post('/api/evidence/search',json={'query':'isolated search failure'})
                assert failed.status_code==502
            finally:requests.post=post
            assert kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0]==count
            summary['loopback_search_http_503_no_fabrication']=True
        for module,args,label,expected in [
            ('litprism.mcp.server',['--config',str(config)],'direct-eof',0),
            ('litprism',['--config',str(config),'mcp','serve'],'cli-eof',0),
            ('litprism.mcp.server',['--config',str(home/'missing.yaml')],'direct-bad-config',2),
            ('litprism',['--config',str(home/'missing.yaml'),'mcp','serve'],'cli-bad-config',1)]:
            result=subprocess.run(command(module,args),input='',env=process_env(home,cfg),cwd=ROOT,
                text=True,capture_output=True,encoding='utf-8',timeout=15)
            assert result.returncode==expected and result.stdout==''
            summary[label]={'exit_code':result.returncode,'stdout_empty':True}
        start=kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0]
        cfg.websearch.provider='tavily';cfg.websearch.api_key_ref=None
        assert client.post('/api/evidence/search',json={'query':'fixture'}).status_code==503
        assert kb._con.execute('SELECT count(*) FROM evidence_sources').fetchone()[0]==start
        summary.update(search_missing_key_no_fabrication=True,loopback_calls=trace,
            mcp_sdk_version=importlib.metadata.version('mcp'),domain_paper_count=len(kb.list_papers()),
            next_task='N10 usability and management acceptance',daily_database_opened=False)
        write(home/'summary.json',summary)
        return summary
    finally:
        app.state.tasks.pool.shutdown(wait=True);kb.close()


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=run(args.output)
    print(json.dumps({k:v for k,v in result.items() if k!='loopback_calls'},ensure_ascii=True))
