import pytest
from litprism.qa_guard import answer_risks
from test_workflow_guards import env as workflow_env, paper

env = workflow_env


@pytest.mark.parametrize('answer,source,blocked', [
    ('3.57%属于单模型。[P1 p.6]', '【表格行】邻近表题：Table 5. Error rates of ensembles. {"Model":"ResNet", "Error":"3.57"}', True),
    ('3.57%属于集成。[P1 p.6]', '【表格行】邻近表题：Table 5. Error rates of ensembles. {"Model":"ResNet", "Error":"3.57"}', False),
    ('3.57%属于单模型。[P1 p.6]', 'Table 4. Single-model results\nResNet\n3.57\nTable 5. Ensembles.', True),
    ('在 TriviaQA 上按开发集选择阈值。[P1 p.6]', 'We did not use TriviaQA data for this model.', True),
    ('该设置未使用 TriviaQA。[P1 p.6]', 'We did not use TriviaQA data for this model.', False),
    ('该设置使用 TriviaQA。[P1 p.6]', 'We use TriviaQA data for this model.', False),
])
def test_explicit_attribution_counterexamples_and_positive_controls(answer, source, blocked):
    issues = answer_risks(answer, [{'tag': 'P1', 'text': source}])
    assert bool(issues) == blocked


def test_uncited_negative_source_cannot_override_the_actual_positive_source():
    assert not answer_risks('使用 TriviaQA。[P1 p.6]', [
        {'tag': 'P1', 'text': 'We use TriviaQA.'},
        {'tag': 'P2', 'text': 'We did not use TriviaQA.'}])


def test_legacy_nearest_caption_is_not_trusted_in_a_mixed_table_chunk():
    assert answer_risks('7.32%为单模型。[P1 p.6]', [{'tag': 'P1', 'text':
        '【表格行】邻近表题：Table 4. single-model results {"Error":"7.32"}\nTable 5. ensembles.'}])


def test_service_failure_diagnostics_never_copy_exception_body():
    from litprism.llm import LLMError, safe_failure
    details = safe_failure(LLMError('SECRET_RESPONSE_BODY', http_status=429))
    assert details['http_status'] == 429 and 'SECRET' not in str(details)


def test_section_number_in_cited_prose_is_not_a_table_measurement():
    source = ('Table 2. Results\nTable 3. Configuration\n6.1\nMachine Translation\n'
              'Training took 3.5 days on 8 P100 GPUs. All published single models.')
    assert not answer_risks('第 6.1 节报告，big 模型训练3.5天，并超过先前单模型。[P1 p.8]',
                            [{'tag': 'P1', 'text': source}])
    # Removing locator numbers must not hide an actual unbound numeric row.
    source += '\n3.57\n'
    assert answer_risks('第6.1节的单模型错误率为3.57%。[P1 p.8]', [{'tag': 'P1', 'text': source}])


def test_deep_timeout_cause_cannot_erase_connect_phase():
    from litprism.llm import LLMError, safe_failure
    import requests
    low = TimeoutError('SECRET_SOCKET')
    connect = requests.exceptions.ConnectTimeout('SECRET_REQUEST')
    connect.__cause__ = low
    wrapped = LLMError('SECRET_WRAPPER')
    wrapped.__cause__ = connect
    details = safe_failure(wrapped)
    assert details['kind'] == 'connect_timeout' and 'SECRET' not in str(details)


def test_refusal_with_wrong_explanation_is_held_with_auditable_sources(env):
    cfg, kb, model, client = env
    pid = paper(kb, text='alphatopic. We did not use TriviaQA data for this model.')
    def respond(messages, task='', **kwargs):
        return {'answer': '证据不足。在 TriviaQA 上按开发集选择阈值。[P1 p.1]',
                'insufficient_evidence': True} if task == 'qa' else {}
    model.chat_json = respond
    cid = kb.add_conversation('Negation', scope={'paper_ids': [pid]})
    result = client.post('/api/chat/'+cid, json={'question': 'alphatopic'}).json()
    assert result['status'] == 'needs_review'
    assert '选择阈值' not in result['assistant']['content']
    assert result['assistant']['source_links']
    reopened = client.get(f'/api/conversations/{cid}/messages').json()['items'][-1]
    assert reopened['status'] == 'needs_review' and reopened['guard_issues']
    from litprism.context.dialogue import build_dialogue
    assert not any(m['role'] == 'assistant' for m in build_dialogue(kb, cfg, cid, None).history)
