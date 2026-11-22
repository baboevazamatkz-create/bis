import hashlib
import mimetypes
import runpy
from pathlib import Path

import pytest
from sqlalchemy import select

from bis import ocr
from bis.agents.builtin_tools import SearchArgs, search_knowledge
from bis.api.deps import agent_context
from bis.config import get_settings
from bis.models import ConstructionObject, KnowledgeChunk, KnowledgeSource, Organization, User
from bis.models.knowledge import EMBEDDING_DIM, VECTOR
from bis.rag.embeddings import HashingEmbedder, set_embedder
from bis.security import create_access_token
from bis.services.library_access import bind_context

SEARCH = '/api/knowledge/search'


def test_project_search_requires_a_selected_object(client, auth):
    response = client.get(SEARCH, headers=auth, params={'scope': 'project', 'q': 'KLAB471'})
    assert response.status_code == 409


class UnavailableEmbeddings(HashingEmbedder):
    def embed_query(self, text):
        raise ConnectionError('Учебный стенд без эмбеддингов')

    def embed_documents(self, texts):
        raise ConnectionError('Учебный стенд без эмбеддингов')


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), 'library_roots', [tmp_path])
    monkeypatch.setattr(ocr, 'available', lambda: False)
    set_embedder(UnavailableEmbeddings())
    generator = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                                  'docs/tasks/p1-object-sources/make_corpus.py'))
    monkeypatch.setattr('sys.argv', ['make_corpus.py', str(tmp_path / 'corpus')])
    generator['main']()
    return tmp_path / 'corpus', generator


def upload(client, headers, paths, project=None, level='project'):
    form = {'level': level}
    if project:
        form['object_id'] = str(project.id)
    response = client.post('/api/libraries/upload', headers=headers, data=form, files=[
        ('files', (p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0])) for p in paths
    ])
    assert response.status_code == 201, response.text
    return response.json()


def hits(client, headers, q, scope='project', **kwargs):
    response = client.get(SEARCH, headers=headers, params={'scope': scope, 'q': q, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()['results']


def headers_for(user, project=None):
    headers = {'Authorization': f'Bearer {create_access_token(user.id, user.organization_id, user.role)}'}
    if project:
        headers['X-BIS-Project'] = str(project.id)
    return headers


def test_uploaded_object_sources_and_originals(client, auth, db, demo, corpus, tmp_path):
    root, generator = corpus
    foreign_org = Organization(name='Учебная другая организация')
    db.add(foreign_org)
    db.flush()
    a = ConstructionObject(name='Учебный A', organization_id=demo.id)
    b = ConstructionObject(name='Учебный B', organization_id=demo.id)
    foreign = ConstructionObject(name='Учебный внешний', organization_id=foreign_org.id)
    viewer = User(email='training-viewer@test.invalid', full_name='Читатель', password_hash='unused',
                  organization_id=demo.id, role='viewer')
    foreign_admin = User(email='training-admin@test.invalid', full_name='Администратор', password_hash='unused',
                         organization_id=foreign_org.id, role='admin')
    db.add_all([a, b, foreign, viewer, foreign_admin])
    admin = db.scalar(select(User).where(User.email == 'admin@demo.bis'))
    admin.is_platform_admin = True
    demo.settings = {**(demo.settings or {}), 'libraries_strict': False}
    db.commit()
    admin_a = {**auth, 'X-BIS-Project': str(a.id)}
    admin_b = {**auth, 'X-BIS-Project': str(b.id)}
    viewer_a = headers_for(viewer, a)
    other_headers = headers_for(foreign_admin, foreign)
    library = upload(client, admin_a, sorted((root / 'A').iterdir()), a)
    upload(client, admin_b, list((root / 'B').iterdir()), b)
    upload(client, other_headers, list((root / 'B').iterdir()), foreign)
    org_file = tmp_path / 'org.txt'
    org_file.write_text('KLAB471 ORGONLYMARKER. Согласованный документ организации.', encoding='utf-8')
    global_file = tmp_path / 'norm.txt'
    global_file.write_text('KLAB471 GLOBALONLYMARKER. Общий нормативный документ.', encoding='utf-8')
    upload(client, auth, [org_file], level='organization')
    upload(client, auth, [global_file], level='knowledge')
    files_url = f"/api/libraries/{library['id']}/files"

    def file_rows():
        response = client.get(files_url, headers=viewer_a)
        assert response.status_code == 200, response.text
        return {row['path']: row for row in response.json()}

    files = file_rows()
    coverage = client.get('/api/libraries/project/coverage', headers=viewer_a)
    assert coverage.status_code == 200
    assert coverage.json() == {'files': 4, 'ready': 2, 'partial': 1, 'error': 1, 'unknown': 0}
    assert set(files) == {'RD-TRAIN-17.pdf', 'Specification.xlsx', 'Quality.pdf', 'Scan-only.pdf'}
    for filename in ('RD-TRAIN-17.pdf', 'Specification.xlsx'):
        assert files[filename]['state'] == 'ready'
        assert files[filename]['reading']['with_text'] == files[filename]['reading']['segments'] == 2
    assert files['Quality.pdf']['state'] == 'partial'
    assert files['Quality.pdf']['reading']['empty_pages'] == [2]
    assert files['Quality.pdf']['reading']['with_text'] == 1
    assert files['Scan-only.pdf']['state'] == 'error'
    assert 'OCR' in files['Scan-only.pdf']['error']
    assert files['Scan-only.pdf']['source_id'] is None

    a_hits = {}
    for query, name, marker, page, sheet, reading_status in [
        ('KLAB471', 'RD-TRAIN-17.pdf', 'SHEET 7 | KLAB471 | Cable line K-1: 120 m', 2, None, 'ready'),
        ('SPECLAB082', 'Specification.xlsx', '2 | Кабель силовой | SPECLAB082 | м | 120', None, 'Кабели', 'ready'),
        ('QUALITYLAB309', 'Quality.pdf', 'Batch QUALITYLAB309 was delivered', 1, None, 'partial'),
    ]:
        result = hits(client, viewer_a, query)
        assert result
        hit = result[0]
        assert (hit['file_name'], hit['page'], hit['sheet_name'], hit['reading_status']) == (
            name, page, sheet, reading_status)
        assert marker in hit['content']
        assert {r['source_id'] for r in result} <= {row['source_id'] for row in files.values()}
        expected = (root / 'A' / name).read_bytes()
        assert hit['original_url'] == f"/api/knowledge/project/sources/{files[name]['source_id']}/file"
        response = client.get(hit['original_url'], headers=viewer_a)
        assert response.status_code == 200
        assert hashlib.sha256(response.content).digest() == hashlib.sha256(expected).digest()
        a_hits[query] = hit

    for query in ('SCANNEDLAB701', 'UNREADLAB884', 'DRAWINGOBJECTB', 'ORGONLYMARKER', 'GLOBALONLYMARKER'):
        assert not hits(client, viewer_a, query), query
    assert not hits(client, admin_b, 'DRAWINGOBJECTA')
    b_hit = hits(client, admin_b, 'KLAB471')[0]
    assert b_hit['page'] == 2 and '375 m' in b_hit['content']
    foreign_hit = hits(client, other_headers, 'KLAB471')[0]
    assert hits(client, auth, 'ORGONLYMARKER', 'organization')
    assert hits(client, auth, 'GLOBALONLYMARKER', 'norms')
    assert client.get(SEARCH, headers={**auth, 'X-BIS-Project': str(foreign.id)},
                      params={'q': 'KLAB471', 'scope': 'project'}).status_code == 400

    for strict in (False, True):
        demo.settings = {**demo.settings, 'libraries_strict': strict}
        db.commit()
        bind_context(db, demo.id, a.id)
        ctx = agent_context(db, viewer, query_only=True)
        result = search_knowledge.handler(ctx, SearchArgs(scope='project', query='KLAB471'))
        assert result['results'] and '120 m' in result['results'][0]['content']
        assert all(c['source_id'] == a_hits['KLAB471']['source_id'] for c in ctx.citations)
        for unavailable in (b_hit, foreign_hit):
            assert client.get(unavailable['original_url'], headers=viewer_a).status_code == 404
        assert client.get(a_hits['KLAB471']['original_url'], headers=viewer_a).status_code == 200
        assert hits(client, viewer_a, 'ORGONLYMARKER', 'organization')
        assert hits(client, viewer_a, 'GLOBALONLYMARKER', 'norms')
        db.info.pop('source_context')

    if VECTOR and get_settings().vector_enabled:
        embedder = HashingEmbedder(EMBEDDING_DIM)
        vector_query = 'VECTORLABUNEQUAL'
        for chunk in db.scalars(select(KnowledgeChunk).join(KnowledgeSource).where(KnowledgeSource.file_name.is_not(None))):
            chunk.embedding = embedder.embed_query(vector_query)
        db.commit()
        set_embedder(embedder)
        for strict in (False, True):
            demo.settings = {**demo.settings, 'libraries_strict': strict}
            db.commit()
            for headers, expected_sources in [
                (viewer_a, {row['source_id'] for row in files.values()} - {None}),
                (admin_b, {b_hit['source_id']}),
            ]:
                assert not hits(client, headers, vector_query, fragments=True)
                assert {hit['source_id'] for hit in hits(client, headers, vector_query)} == expected_sources
        set_embedder(UnavailableEmbeddings())

    passport = db.get(KnowledgeSource, files['Quality.pdf']['source_id'])
    passport.meta = {**passport.meta, 'reading': {'segments': 2, 'with_text': 2, 'complete': False, 'sparse_pages': [2]}}
    db.commit()
    assert file_rows()['Quality.pdf']['state'] == 'partial'
    passport.meta = {key: value for key, value in passport.meta.items() if key != 'reading'}
    db.commit()
    assert file_rows()['Quality.pdf']['reading'] == {}
    assert hits(client, viewer_a, 'QUALITYLAB309')[0]['reading_status'] == 'unknown'

    source_path = Path(library['path']) / 'RD-TRAIN-17.pdf'
    generator['write_drawing'](source_path, 121, 'DRAWINGREPLACEMENT')
    response = client.post(f"/api/libraries/{library['id']}/sync", headers=admin_a)
    assert response.status_code == 202
    assert not hits(client, viewer_a, 'DRAWINGOBJECTA')
    assert '121 m' in hits(client, viewer_a, 'KLAB471')[0]['content']
    assert client.get(a_hits['KLAB471']['original_url'], headers=viewer_a).status_code == 404
    response = client.patch(f"/api/libraries/{library['id']}", headers=admin_a, params={'enabled': False})
    assert response.status_code == 200
    assert not hits(client, viewer_a, 'KLAB471')
    assert client.get('/api/libraries/project/coverage', headers=viewer_a).json()['files'] == 0

    bind_context(db, demo.id)
    ctx = agent_context(db, viewer, query_only=True)
    assert 'объект' in search_knowledge.handler(ctx, SearchArgs(scope='project', query='KLAB471'))['error'].lower()
