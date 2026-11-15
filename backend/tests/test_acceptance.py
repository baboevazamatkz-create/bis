"""Tests of the acceptance runner, not evidence of model intelligence."""
import io
import uuid

import httpx
import pytest
from docx import Document
from pydantic import ValidationError

from bis.acceptance import Suite, run_suite

ORG, PROJECT, OLD, NEW = (str(uuid.uuid4()) for _ in range(4))


def suite(turns=None):
    return Suite.model_validate({'name': 'Контроль', 'archive_revision': 'fixture-v1', 'source_revision': 'test',
        'contexts': {'alpha': {'organization_id': ORG, 'object_id': PROJECT}},
        'cases': [{'id': 'letter', 'context': 'alpha', 'failure_layer': 'execution', 'turns': turns or [
            {'message': 'Подготовь письмо', 'expect': {'artifact': 'new', 'file_contains': ['Новый текст'], 'file_excludes': ['Старая подпись']}},
            {'message': 'Где файл?', 'expect': {'artifact': 'same', 'require_model': False}}]}]})


def file_bytes(text='Новый текст'):
    word = Document()
    word.add_table(rows=1, cols=1).cell(0, 0).text = text
    word.sections[0].header.paragraphs[0].text = 'Компания Альфа'
    out = io.BytesIO()
    word.save(out)
    return out.getvalue()


def transport(*, artifact=NEW, model='gpt-test', text='Черновик создан', file=None, organization=ORG, timeout=False):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers.get('X-BIS-Project') == PROJECT
        path = request.url.path
        if path == '/api/auth/me':
            return httpx.Response(200, json={'user': {'organization_id': organization}})
        if path == '/api/documents':
            return httpx.Response(200, json=[{'id': OLD}])
        if path == '/api/chat':
            if timeout:
                raise httpx.ReadTimeout('provider may still be running', request=request)
            return httpx.Response(200, json={'session_id': 'session', 'provider': 'artifact_ledger' if len([r for r in requests if r.method == 'POST']) > 1 else 'openai',
                'model': model, 'text': text, 'documents': [{'id': artifact, 'created': True}],
                'citations': [{'document_id': OLD, 'used': False}]})
        if path == f'/api/documents/{artifact}/file':
            return httpx.Response(200, content=file if file is not None else file_bytes())
        raise AssertionError(path)

    return httpx.MockTransport(handle), requests


def run(scenario=None, **options):
    mock, requests = transport(**options)
    with httpx.Client(base_url='http://bis.test', transport=mock) as client:
        report = run_suite(client, scenario or suite())
    return report, requests


def test_checks_real_file_content_tables_and_dialogue_session():
    import json
    report, requests = run()
    assert report['passed'] and report['cases_passed'] == 1
    assert report['human_review_required'] and report['claim_scope'] == 'explicit_acceptance_assertions_only'
    posts = [json.loads(request.content) for request in requests if request.method == 'POST']
    assert posts[0]['session_id'] is None and posts[1]['session_id'] == 'session'
    assert report['results'][0]['turns'][1]['document_ids'] == [NEW]


def test_old_document_cannot_pass_as_new_even_with_created_flag():
    report, _ = run(artifact=OLD)
    assert not report['passed'] and not report['results'][0]['turns'][0]['checks']['artifact']


@pytest.mark.parametrize('model', ['offline', 'scripted'])
def test_no_model_fallback_cannot_be_counted_as_live_model_success(model):
    report, _ = run(model=model)
    assert not report['passed'] and not report['model_observed']


@pytest.mark.parametrize('content', [file_bytes('Неправильный документ'), file_bytes('Новый текст. Старая подпись'), b'not a docx'])
def test_download_200_is_not_enough(content):
    report, _ = run(file=content)
    assert not report['passed']


def test_identity_checked_before_any_task_is_submitted():
    report, requests = run(organization=str(uuid.uuid4()))
    assert not report['passed'] and report['results'][0]['error'] == 'organization_mismatch'
    assert not any(request.method == 'POST' for request in requests)


def test_timeout_stops_continuation_without_repeating_mutation():
    scenario = suite()
    following = scenario.cases[0].model_copy(deep=True)
    following.id = 'following-task'
    scenario.cases.append(following)
    report, requests = run(scenario, timeout=True)
    assert not report['passed'] and report['results'][0]['result_unknown']
    assert sum(request.method == 'POST' for request in requests) == 1
    assert report['results'][1]['error'] == 'not_run_after_timeout'


def test_unreferenced_retrieval_does_not_prove_answer_is_grounded():
    scenario = suite([{'message': 'Какова цена?', 'expect': {'citation_documents': [OLD]}}])
    report, _ = run(scenario)
    assert not report['passed'] and not report['results'][0]['turns'][0]['checks']['citations']


def test_schema_rejects_empty_checks_and_unknown_fields():
    with pytest.raises(ValidationError):
        suite([{'message': 'Вопрос', 'expect': {}}])
    with pytest.raises(ValidationError):
        suite([{'message': 'Вопрос', 'expect': {'text_contain': ['Ответ']}}])


def test_context_requires_explicit_matching_credentials_when_configured():
    scenario = suite()
    scenario.contexts['alpha'].token_env = 'BIS_ACCEPT_ALPHA_TOKEN'
    report, requests = run(scenario)
    assert not report['passed'] and not requests
    assert report['results'][0]['error'] == 'missing_context_token'


def test_runner_uses_actual_bis_chat_and_download_endpoints(client, auth, demo):
    from bis.llm.gateway import LLMGateway, ProviderEntry, set_gateway
    from bis.llm.providers.offline import ScriptedProvider
    from bis.llm.types import Message, ToolCall

    llm = ScriptedProvider([Message('assistant', tool_calls=[ToolCall('1', 'create_letter_draft', {
        'subject': 'О направлении ИД', 'body': ['Направляем исполнительную документацию.']})]), 'Черновик создан.'])
    set_gateway(LLMGateway(entries={'scripted': ProviderEntry(llm, 'confidential')}))
    scenario = suite()
    scenario.contexts['alpha'].organization_id = demo.id
    scenario.contexts['alpha'].object_id = None
    scenario.contexts['alpha'].token_env = 'TEST_BIS_TOKEN'
    scenario.cases[0].turns[0].expect.require_model = False
    scenario.cases[0].turns[0].expect.file_contains = ['Направляем исполнительную документацию.']
    report = run_suite(client, scenario, tokens={'TEST_BIS_TOKEN': auth['Authorization'].removeprefix('Bearer ')})
    assert report['passed'], report
    assert not report['model_observed']  # successful mechanism check must not become proof of a live model
    assert len(report['results'][0]['turns']) == 2


def test_cli_writes_failure_report_without_credentials(tmp_path, monkeypatch):
    from bis.cli import main

    scenario = suite()
    scenario.contexts['alpha'].token_env = 'BIS_ACCEPT_TOKEN'
    monkeypatch.setenv('BIS_ACCEPT_TOKEN', 'private-fixture-token')
    manifest = tmp_path / 'suite.json'
    manifest.write_text(scenario.model_dump_json(), encoding='utf-8')
    mock, _ = transport(artifact=OLD)
    client = httpx.Client(base_url='http://bis.test', transport=mock)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client)
    output = tmp_path / 'reports' / 'acceptance.json'
    assert main(['acceptance', str(manifest), '--url', 'http://bis.test', '--report', str(output)]) == 1
    assert 'private-fixture-token' not in output.read_text(encoding='utf-8')
    assert '"passed": false' in output.read_text(encoding='utf-8')
