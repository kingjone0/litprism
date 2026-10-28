"""运行真实后端与无窗口浏览器的研究流程验收；仅使用临时库和离线模型桩。"""
from __future__ import annotations

from fastapi import Request

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('LITPRISM_HOME', 'E:/codex-litprism-data')
sys.path.insert(0, str(ROOT / 'src'))

from litprism.config import Config, config_to_dict  # noqa: E402
from litprism.db import KnowledgeBase  # noqa: E402
from litprism.sidecar import harden  # noqa: E402
from litprism.web import server as web  # noqa: E402


class AcceptanceModel:
    name = 'offline:acceptance-fixture'
    supports_semantic_review = True
    durable_failed = False
    reading_failed = False

    def chat_json(self, messages, *, task=None):
        if task == 'extract' and 'readingretrymarker' in str(messages) and not AcceptanceModel.reading_failed:
            AcceptanceModel.reading_failed = True
            raise TimeoutError('离线解读入口验收：一次故意未知结果')
        question = messages[-1]['content'].split('【问题】', 1)[-1] if task == 'qa' else ''
        if task == 'qa' and ('anchortrialmarker' in question or 'unboundmappingmarker' in question or 'unitreviewmarker' in question or 'unitknowledgefollowmarker' in question):
            anchors = json.loads(messages[-1]['content'].split('【试验定位锚点（程序生成，不是语义结论）】\n')[1])
            a = next((a for a in anchors if 'unitknowledgefollowmarker' in a['preview']), anchors[0]) if 'unitknowledgefollowmarker' in question else anchors[0]
            text = f'隔离试验中选定原文的转述。[{a["source"]}]'
            mapped = {'subject': '离线流程试验', 'basis': 'direct', 'sources': [a['source']],
                      'quotes': [{'source': a['source'], 'anchor_id': a['anchor_id']}]}
            if 'unboundmappingmarker' in question:
                return {'answer': text, 'evidence_map': [dict(mapped, claim='模型另外概括的句子。['+a['source']+']')]}
            if 'unitknowledgefollowmarker' in question:
                return {'answer_units': [dict(mapped, text='已重新检索单元人工知识。['+a['source']+']')]}
            if 'unitreviewmarker' in question:
                return {'answer_units': [dict(mapped, text='originalmarker，并使用八块GPU。['+a['source']+']'),
                    dict(mapped, text='UNREVIEWED_UI_UNIT_MARKER ['+a['source']+']')]}
            return {'answer_units': [dict(mapped, text=text)]}
        if task == 'qa' and 'metadatamarker' in question:
            import re
            tag, stamp = re.search(r'\[(P\d+)\] 本轮搜索摘要[^\n]*获取时间：([^\s]+)', messages[-1]['content']).groups()
            text = f'本轮网页摘要的获取时间为 {stamp}。[{tag}]'
            return {'answer': text, 'evidence_map': [{'claim': text, 'subject': '来源获取记录',
                'basis': 'metadata', 'sources': [tag], 'quotes': [{'source': tag, 'quote': '获取时间：'+stamp}]}]}
        if task == 'qa' and ('mixedwebmarker' in question or 'mixedcorrectedmarker' in question):
            import re
            content = messages[-1]['content'].split('【片段】',1)[-1].split('【程序送达记录】')[0]
            phrase = 'mixedwebfixture' if 'mixedwebmarker' in question else 'mixedcorrectedmarker'
            blocks = re.findall(r'\[(P\d+)\] [^\n]+\n(.*?)(?=\n\n---|$)',content,re.S)
            tag = next(tag for tag,text in blocks if phrase in text)
            return {'answer':('据本轮搜索摘要（未审核）提供的描述。' if phrase=='mixedwebfixture' else '依据新确认的知识回答。')+'['+tag+']'}
        self.last_reasoning = '<script>thinking_ui_marker</script>' if task == 'qa' else ''
        self.last_request_settings = {'max_tokens': 384000, 'timeout': 300, 'output_mode': 'automatic'} if task == 'qa' else {}
        if task == 'qa' and 'durablereloadmarker' in question:
            time.sleep(1)
        if task == 'qa' and 'durableretrymarker' in question and not AcceptanceModel.durable_failed:
            AcceptanceModel.durable_failed = True
            raise TimeoutError('离线持久请求验收：一次故意失败')
        if task == 'semantic_cross_review':
            return {'differences': []}
        if task == 'semantic_review':
            request = json.loads(messages[-1]['content'])
            return {'reviews': [{'id': c['id'], 'verdict': 'needs_review' if '待核查' in c['text'] else 'supported',
                                 'reason': '离线流程桩：仅验收审核交互，不验证学术质量。',
                                 'quotes': [{'tag': s['tag'], 'quote': s['text']} for s in request['sources']], 'facts': []}
                                for c in request['claims']]}
        if task == 'qa' and 'delaymarker' in question:
            from litprism.tasks import checkpoint
            for _ in range(60):
                checkpoint('取消操作验收：等待测试取消')
                time.sleep(.05)
        if task == 'expand':
            return {'terms': ['alphatopic']}
        if task == 'qa' and 'invalidcitationmarker' in question:
            return {'answer': 'BLOCKED_UI_DRAFT [P99 p.999]'}
        if task == 'qa' and 'refusalcitationmarker' in question:
            return {'answer': '证据不足，已知背景 [P1 p.1]', 'insufficient_evidence': True}
        if task == 'qa' and 'attributionmarker' in question:
            return {'answer': '使用 TriviaQA 训练该模型 [P1 p.1]'}
        if task == 'qa' and 'conditionalmarker' in question:
            return {'answer': '2024索引对2020年领导人得67%。[P1 p.1]'}
        if task == 'qa' and 'tagcitationmarker' in question:
            return {'answer': '稳定片段编号的来源验收。[P1]'}
        if task == 'qa' and 'claimmappingmarker' in question:
            return {'answer': '原文有明确否定。[P1]', 'evidence_map': [{
                'claim': '原文有明确否定。[P1]', 'subject': '<script>claim_subject_marker</script>',
                'basis': 'direct', 'sources': ['P1'], 'quotes': [{
                    'source': 'P1', 'quote': 'We did not use TriviaQA data for this model.'}]}]}
        if task == 'extract':
            return {'research_question': '待核查陈述：用于离线验收的原文描述 [P1 p.1]',
                    'field_evidence': {'research_question': ['P1 p.1']}}
        if task == 'concepts':
            return {'concepts': [{'term': 'alphatopic', 'definition': '用于离线验收的原文描述 [P1 p.1]', 'category': '方法'}]}
        if task == 'compare':
            return {'summary': '用于离线验收的原文描述 [P1 p.1]', 'dimensions': [],
                    'convergence': ['共同描述 [P1 p.1]'], 'divergence': [],
                    'evidence_map': [{'claim': '共同描述', 'sources': ['P1 p.1']}]}
        if task == 'qa' and 'reviewmarker' in question:
            return {'answer': '知识快照的测试内容 [P1]'}
        return {'answer': '用于离线验收的原文描述 [P1 p.1]'}

    def chat(self, messages, **kwargs):
        if 'delaymarker' in str(messages):
            from litprism.tasks import checkpoint
            for _ in range(60):
                checkpoint('Skill 取消操作验收')
                time.sleep(.05)
        return '用于离线验收的原文描述 [P1 p.1]'


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('workflows', 'n10'), default='workflows')
    args = parser.parse_args()
    import uvicorn
    home = Path(os.environ['LITPRISM_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workflow-acceptance-', dir=home) as folder:
        cfg = Config()
        cfg.app.data_dir = str(Path(folder) / 'data')
        for name in ('crossref', 'openalex', 'arxiv', 'semantic_scholar', 'unpaywall'):
            getattr(cfg.sources, name).enabled = False
        import yaml
        cfg.config_path = str(Path(folder) / 'config.yaml')
        Path(cfg.config_path).write_text(yaml.safe_dump(config_to_dict(cfg), allow_unicode=True), encoding='utf-8')
        kb = KnowledgeBase(cfg)
        seed = kb.add_paper(title='验收文献 A', verify_level='verified')
        kb.add_chunks(seed, [{'text': 'We did not use TriviaQA data for this model.\n'
            +'Accuracy is 67% using the 2024 index for 2024 leaders.\n'
            +'alphatopic 用于离线验收的原文描述。' * 20, 'page': 1}])
        web.KnowledgeBase = lambda _: kb
        web.build_llm = lambda _: AcceptanceModel()
        app = web.create_app(cfg)
        from litprism.web import evidence_routes
        from litprism.knowledge import create_source
        def offline_search(_kb, _cfg, query):
            if not any(marker in query for marker in ('mixedwebmarker', 'metadatamarker')):
                return {'items': []}
            return {'items':[create_source(kb,kind='web',title='混合取证外部快照',
                content='alphatopic mixedwebfixture 本轮实际取得的搜索摘要。',url='https://example.org/mixed-evidence')]}
        evidence_routes.discover_web = offline_search

        # Only this isolated acceptance app exposes mutation fixtures; never production.
        @app.post('/api/acceptance/material')
        def material_fixture(req: dict):
            pid = req.get('paper_id')
            if not pid:
                pid = kb.add_paper(title='未解读质量验收' if req.get('unanalysed') else '材料版本验收',
                                   verify_level='verified', material_level='fulltext')
                kb.add_chunks(pid, [{'text': '旧版原文：originalmarker。训练使用八块 GPU。', 'page': 1}], level='fulltext')
            elif req.get('replace'):
                kb.add_chunks(pid, [{'text': '新版原文：replacementmarker。训练使用十六块 GPU。', 'page': 2}], level='fulltext')
            if not req.get('replace') and not req.get('unanalysed'):
                chunk = kb.get_chunks(pid)[0]
                kb.save_analysis(pid, {'research_question': chunk['text'] + ' [P1]', '_model': 'offline:material-fixture',
                    '_citations': [{'tag': 'P1', 'chunk_id': chunk['id'], 'paper_id': pid, 'page': chunk['page']}],
                    '_semantic_review': {'items': [{'id': 'materialclaim', 'field': 'research_question',
                        'text': chunk['text'], 'candidate_tags': ['P1'], 'status': 'needs_review'}]}})
            return {'paper_id': pid, 'chunks': kb.get_chunks(pid), 'materials': kb.list_materials(pid)}

        from fastapi.responses import JSONResponse, StreamingResponse
        import asyncio
        import uuid
        stream_cases = {}

        @app.post('/api/acceptance/stream')
        def stream_fixture(req: dict, request: Request):
            from litprism.providers import ProviderStore
            key = uuid.uuid4().hex[:12]
            title = '流式验收论文 ' + key
            text = 'streamingfixture 提出了有依据的实验描述。'
            pid = kb.add_paper(title=title, verify_level='verified')
            kb.add_chunks(pid, [{'text': (text+'\n')*25, 'page':1}], level='fulltext')
            cid = kb.add_conversation('流式验收 '+key, scope={'paper_ids':[pid], 'mode':'strict'})
            providers = ProviderStore(kb, cfg.app.data_dir)
            provider = providers.create(name='本机流式验收 '+key,
                base_url=str(request.base_url).rstrip('/')+'/acceptance-provider',
                api_key='local-stream-acceptance', chat_model=key)
            providers.set_models(provider.id, [{'model_id':key}])
            providers.set_active_model(provider.id,key)
            stream_cases[key] = dict(key=key, paper_id=pid, conversation_id=cid, provider_id=provider.id,
                release=threading.Event(), qa_calls=0, expansions=0, mode=req.get('mode','normal'), title=title, quote=text)
            return {k:stream_cases[key][k] for k in ('key','paper_id','conversation_id','provider_id')}

        @app.get('/api/acceptance/stream/{key}')
        def stream_stats(key: str):
            case=stream_cases[key]
            return {k:case[k] for k in ('qa_calls','expansions')}

        @app.post('/api/acceptance/stream/{key}/release')
        def release_stream(key: str):
            stream_cases[key]['release'].set()
            return {'ok':True}

        @app.get('/acceptance-provider/models')
        def stream_models():
            return {'data':[{'id':key,'context_length':1_000_000,'max_output_tokens':384_000} for key in stream_cases]}

        @app.post('/acceptance-provider/chat/completions')
        def stream_provider(req: dict):
            case=stream_cases[req['model']]
            if not req.get('stream'):
                case['expansions']+=1
                return JSONResponse({'choices':[{'message':{'content':'{"terms":["streamingfixture"]}'}}],
                                     'usage':{'prompt_tokens':3,'completion_tokens':2}})
            case['qa_calls']+=1
            tag='P99' if case['mode']=='bad_citation' else 'P1'
            data={'answer_units':[{'text':f'streamingfixture 提出了有依据的实验描述 [{tag}]。',
                  'subject':case['title'],'basis':'direct','sources':[tag],
                  'quotes':[{'source':tag,'quote':case['quote']}]}]}
            content=json.dumps(data,ensure_ascii=False)
            split=content.index('实验')
            def packet(value):return 'data: '+(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False))+'\n\n'
            async def generate():
                yield packet({'choices':[{'index':0,'delta':{'reasoning_content':'服务实际返回的思考片段。'}}]})
                yield packet({'choices':[{'index':0,'delta':{'content':content[:split]}}]})
                while not case['release'].is_set():
                    yield ': keepalive\n\n'
                    await asyncio.sleep(.05)
                if case['mode']=='disconnect':return
                yield packet({'choices':[{'index':0,'delta':{'content':content[split:]},'finish_reason':'stop'}]})
                yield packet({'choices':[],'usage':{'prompt_tokens':50,'completion_tokens':25}})
                yield packet('[DONE]')
            return StreamingResponse(generate(),media_type='text/event-stream')

        @app.post('/api/acceptance/reading')
        def reading_fixture(req: dict):
            import uuid
            # This case tests the offline reading client, independent of earlier provider-selector cases.
            with kb.tx() as con:
                con.execute("DELETE FROM meta WHERE key='active_provider'")
            level = req.get('level', 'unknown')
            if level not in ('metadata', 'abstract', 'partial', 'fulltext', 'unknown'):
                raise ValueError('Unsupported fixture material')
            pid = kb.add_paper(title='G02阅读 ' + level + ' ' + uuid.uuid4().hex[:6],
                               verify_level='verified', material_level='metadata' if level == 'metadata' else 'fulltext')
            if level != 'metadata':
                text = '用于离线验收的原文描述。 ' * 30
                if req.get('retry'):
                    text += ' readingretrymarker'
                kb.add_chunks(pid, [{'text': text, 'page': 1}], level=level)
            if req.get('previous'):
                chunk = kb.get_chunks(pid)[0]
                kb.save_analysis(pid, {'key_conclusions': 'PREVIOUS_READING_VISIBLE [P1 p.1]',
                    '_model': 'offline:previous-reading', '_citation_check': {'passed': True},
                    '_citations': [{'tag': 'P1', 'chunk_id': chunk['id'], 'paper_id': pid, 'page': 1}]})
            return {'paper_id': pid}

        capacity = {}

        @app.post('/api/acceptance/library-capacity')
        def library_capacity_fixture():
            if capacity:
                return capacity
            from datetime import datetime
            import uuid
            parent, child = ['col_' + uuid.uuid4().hex[:12] for _ in range(2)]
            with kb.tx() as con:
                for cid, name, pid in [(parent, 'G01容量父组', None), (child, 'G01容量子组', parent)]:
                    con.execute('INSERT INTO collections (id,name,parent_id,sort_order,created_at) VALUES (?,?,?,0,?)',
                                (cid, name, pid, datetime.now().isoformat()))
            papers = [kb.add_paper(title='G01容量 最早论文', authors=['G01独有作者'], verify_level='verified')]
            papers += [kb.add_paper(title=f'G01容量 论文 {i}', verify_level='verified') for i in range(300)]
            conversations = [kb.add_conversation(f'G01会话 {i}') for i in range(101)]
            with kb.tx() as con:
                con.execute('UPDATE papers SET created_at=?,collection_id=? WHERE id=?', ('2000-01-01', child, papers[0]))
            capacity.update(papers=papers, conversations=conversations, parent=parent, child=child)
            return capacity

        @app.delete('/api/acceptance/library-capacity')
        def clear_library_capacity_fixture():
            for pid in capacity.get('papers', []):
                kb.delete_paper(pid)
            for cid in capacity.get('conversations', []):
                kb.delete_conversation(cid)
            with kb.tx() as con:
                for key in ('child', 'parent'):
                    if capacity.get(key):
                        con.execute('DELETE FROM collections WHERE id=?', (capacity[key],))
            capacity.clear()
            return {'ok': True}

        @app.post('/api/acceptance/anchor-trial')
        def anchor_trial_fixture():
            cfg.qa.evidence_anchor_trial = True
            return {'trial': True}

        @app.post('/api/acceptance/compact-history')
        def compact_history_fixture():
            from litprism.context.meter import Meter
            from litprism.context.policy import SelectionResult
            from litprism.context.seam import Seam
            from litprism.context.summarizer import Summarizer, ExtractiveSummarizer
            cid = kb.add_conversation('离线压缩显示验收')
            kb.append_message(cid,'user','LONG_ORIGINAL_VISIBLE 用户研究问题。 '*250)
            kb.append_message(cid,'assistant','LONG_REPLY_VISIBLE 原回答仍可阅读。')
            messages = kb.get_messages(cid)
            tokens = sum(Meter(cfg).estimate_message(m) for m in messages)
            sel = SelectionResult(ok=True,start_seq=1,end_seq=2,
                shadowed_refs=[m['ref'] for m in messages],shadowed_tokens=tokens)
            seam = Seam(kb,cfg)
            tx,_ = seam.begin(cid,'offline-ui-controlled',sel)
            summary = Summarizer(cfg,backend=ExtractiveSummarizer()).summarize(messages,tokens)
            assert tx and summary.ok
            result = seam.commit(tx,summary)
            assert result.ok
            return {'conversation_id':cid,'block_id':result.block_id}

        harden(app, 'acceptance-only')
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        backend = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        worker = threading.Thread(target=backend.run, kwargs={'sockets': [sock]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while not backend.started:
                if time.monotonic() > deadline:
                    raise RuntimeError('验收后端启动超时')
                time.sleep(.05)
            env = dict(os.environ, LITPRISM_ACCEPTANCE_URL=f'http://127.0.0.1:{port}', LITPRISM_ACCEPTANCE_SEED=seed)
            script = 'check_n10.cjs' if args.suite == 'n10' else 'check_workflows.cjs'
            result = subprocess.run(['node', str(ROOT / 'tools' / script)], env=env,
                                    capture_output=True, text=True, encoding='utf-8',
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            print(result.stdout, end='')
            if result.stderr:
                print(result.stderr, file=sys.stderr, end='')
            return result.returncode
        finally:
            backend.should_exit = True
            worker.join(timeout=10)
            sock.close()
            kb.close()


if __name__ == '__main__':
    raise SystemExit(main())
