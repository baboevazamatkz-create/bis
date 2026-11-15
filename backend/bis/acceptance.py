"""Repeatable HTTP acceptance scenarios; never substitute a scripted model for a live model run."""
from __future__ import annotations

import io
import time
import uuid
from datetime import UTC, datetime
from typing import Literal
from zipfile import ZipFile

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Expectations(StrictModel):
    text_contains: list[str] = Field(default_factory=list)
    text_excludes: list[str] = Field(default_factory=list)
    artifact: Literal['new', 'same', 'none'] | None = None
    file_contains: list[str] = Field(default_factory=list)
    file_excludes: list[str] = Field(default_factory=list)
    citation_documents: list[uuid.UUID] = Field(default_factory=list)
    forbidden_documents: list[uuid.UUID] = Field(default_factory=list)
    require_model: bool = True

    @model_validator(mode='after')
    def check_assertions(self):
        if not any((self.text_contains, self.text_excludes, self.artifact is not None,
                    self.file_contains, self.file_excludes, self.citation_documents, self.forbidden_documents)):
            raise ValueError('Каждый шаг должен проверять результат, а не только HTTP 200')
        if (self.file_contains or self.file_excludes) and self.artifact not in ('new', 'same'):
            raise ValueError('Проверки файла требуют artifact=new или same')
        return self


class Turn(StrictModel):
    message: str = Field(min_length=1)
    expect: Expectations


class Case(StrictModel):
    id: str = Field(min_length=1)
    context: str
    failure_layer: Literal['reading', 'identity', 'retrieval', 'planning', 'execution', 'verification']
    turns: list[Turn] = Field(min_length=1)


class Context(StrictModel):
    organization_id: uuid.UUID
    object_id: uuid.UUID | None = None
    token_env: str | None = None


class Suite(StrictModel):
    name: str = Field(min_length=1)
    archive_revision: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    contexts: dict[str, Context]
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode='after')
    def validate_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError('Идентификаторы сценариев должны быть уникальными')
        for case in self.cases:
            if case.context not in self.contexts:
                raise ValueError(f'Неизвестный контекст: {case.context}')
            if case.turns[0].expect.artifact == 'same':
                raise ValueError('Первый шаг не может ссылаться на ранее созданный файл')
        return self


def normalized(value):
    return ' '.join(value.lower().replace('ё', 'е').split())


def model_observed(payload):
    provider = str(payload.get('provider') or '').lower()
    model = str(payload.get('model') or '').lower()
    deterministic = {'artifact_ledger', 'executive_pipeline', 'project_facts', 'offline', 'test', 'scripted', 'local-offline'}
    return bool(provider and model and provider not in deterministic and
                not any(part in model for part in ('offline', 'scripted', 'stored-file', 'evidence-')))


def docx_text(content):
    """Include tables, headers and footers; reject oversized packages before decompression."""
    from xml.etree import ElementTree as ET

    with ZipFile(io.BytesIO(content)) as package:
        members = [item for item in package.infolist() if item.filename.startswith('word/') and item.filename.endswith('.xml')]
        if sum(item.file_size for item in members) > 20_000_000:
            raise ValueError('Слишком большой DOCX для проверки')
        return '\n'.join(' '.join(ET.fromstring(package.read(item)).itertext()) for item in members)


def _inventory(client, headers):
    ids = set()
    for offset in range(0, 50_000, 500):
        response = client.get('/api/documents', headers=headers, params={'limit': 500, 'offset': offset})
        response.raise_for_status()
        items = response.json()
        ids.update(str(item['id']) for item in items)
        if len(items) < 500:
            return ids
    raise ValueError('Не удалось получить полный исходный реестр')


def run_suite(client: httpx.Client, suite: Suite, *, tokens=None):
    """Sequential, no POST retries: a timeout must not silently repeat a document creation."""
    started_at = datetime.now(UTC).isoformat()
    tokens = tokens or {}
    results = []
    mutation_outcome_unknown = False
    for case in suite.cases:
        if mutation_outcome_unknown:
            results.append({'id': case.id, 'passed': False, 'failure_layer': case.failure_layer,
                            'error': 'not_run_after_timeout'})
            continue
        context = suite.contexts[case.context]
        headers = {}
        if context.token_env:
            if not tokens.get(context.token_env):
                results.append({'id': case.id, 'passed': False, 'failure_layer': 'identity', 'error': 'missing_context_token'})
                continue
            headers['Authorization'] = 'Bearer ' + tokens[context.token_env]
        if context.object_id:
            headers['X-BIS-Project'] = str(context.object_id)
        row = {'id': case.id, 'failure_layer': case.failure_layer, 'turns': [], 'passed': False}
        results.append(row)
        try:
            identity = client.get('/api/auth/me', headers=headers)
            identity.raise_for_status()
            if identity.json()['user']['organization_id'] != str(context.organization_id):
                row.update(failure_layer='identity', error='organization_mismatch')
                continue
            before = _inventory(client, headers) if any(t.expect.artifact == 'new' for t in case.turns) else set()
            session = None
            previous_artifacts = set()
            for turn in case.turns:
                started = time.monotonic()
                response = client.post('/api/chat', headers=headers, json={
                    'message': turn.message, 'session_id': session, 'workspace': 'executive',
                    'object_id': str(context.object_id) if context.object_id else None})
                response.raise_for_status()
                payload = response.json()
                session = payload['session_id']
                observed = model_observed(payload)
                checks = {}
                expected = turn.expect
                answer = normalized(payload['text'])
                checks['text_contains'] = all(normalized(value) in answer for value in expected.text_contains)
                checks['text_excludes'] = all(normalized(value) not in answer for value in expected.text_excludes)
                checks['model'] = not expected.require_model or observed
                refs = payload.get('documents') or []
                created = {str(item['id']) for item in refs if item.get('created')}
                if expected.artifact == 'new':
                    checks['artifact'] = bool(created) and created.isdisjoint(before)
                elif expected.artifact == 'same':
                    checks['artifact'] = bool(previous_artifacts) and created == previous_artifacts
                elif expected.artifact == 'none':
                    checks['artifact'] = not created
                sources = payload.get('citations') or []
                citations = {str(item['document_id']) for item in sources if item.get('document_id')}
                used = {str(item['document_id']) for item in sources if item.get('document_id') and item.get('used')}
                checks['citations'] = {str(value) for value in expected.citation_documents} <= used
                exposed = citations | {str(item['id']) for item in refs}
                checks['scope'] = not (exposed & {str(value) for value in expected.forbidden_documents})
                files = []
                if expected.artifact in ('new', 'same'):
                    for document_id in sorted(created):
                        file = client.get(f'/api/documents/{document_id}/file', headers=headers)
                        file.raise_for_status()
                        files.append(normalized(docx_text(file.content)))
                    checks['downloadable_docx'] = bool(files)
                if expected.file_contains:
                    # One complete result must satisfy the task; splitting required fields among unrelated files fails.
                    checks['file_contains'] = any(all(normalized(value) in text for value in expected.file_contains) for text in files)
                if expected.file_excludes:
                    checks['file_excludes'] = bool(files) and all(normalized(value) not in text for text in files for value in expected.file_excludes)
                row['turns'].append({'message': turn.message, 'checks': checks, 'passed': all(checks.values()),
                    'duration_seconds': round(time.monotonic() - started, 3), 'provider': payload.get('provider'),
                    'model': payload.get('model'), 'model_observed': observed, 'document_ids': sorted(created),
                    'answer': payload['text']})
                if not all(checks.values()):
                    break
                if created:
                    previous_artifacts = created
                    before.update(created)
            row['passed'] = len(row['turns']) == len(case.turns) and all(t['passed'] for t in row['turns'])
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError) as error:
            # No response bodies/headers in diagnostics: may contain credentials. Never retry a failed mutation.
            row['error'] = type(error).__name__
            if isinstance(error, httpx.TimeoutException):
                row['result_unknown'] = True
                mutation_outcome_unknown = True
            if isinstance(error, httpx.HTTPStatusError):
                row['http_status'] = error.response.status_code
        except Exception as error:  # noqa: BLE001 — malformed DOCX must be a failed case, not a lost report
            row['error'] = type(error).__name__
    passed = sum(row['passed'] for row in results)
    return {'suite': suite.name, 'archive_revision': suite.archive_revision, 'source_revision': suite.source_revision,
            'started_at': started_at, 'passed': passed == len(results),
            'cases_passed': passed, 'cases_total': len(results), 'results': results,
            'claim_scope': 'explicit_acceptance_assertions_only', 'human_review_required': True,
            'model_observed': any(turn['model_observed'] for row in results for turn in row.get('turns', []))}
