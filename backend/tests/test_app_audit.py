"""Regressions from the whole-app workflow audit."""
import io

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import select

from bis.config import get_settings
from bis.models import ChatSession, ConstructionObject, Document, User


def test_unknown_api_and_missing_file_return_json_and_shell_refreshes(tmp_path, monkeypatch):
    from bis.main import create_app

    (tmp_path / 'assets').mkdir()
    (tmp_path / 'index.html').write_text('<html>pilot</html>')
    monkeypatch.setattr(get_settings(), 'web_dir', tmp_path)
    app = create_app()

    @app.get('/api/missing-file')
    def missing():
        raise FileNotFoundError('private storage location')

    with TestClient(app) as client:
        response = client.get('/api/does-not-exist')
        assert response.status_code == 404 and response.headers['content-type'].startswith('application/json')
        missing = client.get('/api/missing-file')
        assert missing.status_code == 404 and 'private' not in missing.text
        shell = client.get('/')
        assert shell.status_code == 200 and shell.headers['cache-control'] == 'no-cache'


def test_read_upload_does_not_read_unbounded_data():
    from bis.api.uploads import read_upload

    class Stream(io.BytesIO):
        def read(self, size=-1):
            assert size == 5
            return super().read(size)

    with pytest.raises(HTTPException) as error:
        read_upload(UploadFile(file=Stream(b'123456789')), limit=4)
    assert error.value.status_code == 413


def test_chat_history_respects_selected_project_without_strict_sources(client, auth, db, demo):
    first = db.scalar(select(ConstructionObject).where(ConstructionObject.organization_id == demo.id))
    other = ConstructionObject(organization_id=demo.id, name='Другой проект для истории')
    db.add(other)
    db.flush()
    user = db.scalar(select(User).where(User.email == 'admin@demo.bis'))
    session = ChatSession(organization_id=demo.id, user_id=user.id, object_id=other.id, title='Other')
    db.add(session)
    db.commit()
    headers = {**auth, 'X-BIS-Project': str(first.id)}
    assert not any(r['id'] == str(session.id) for r in client.get('/api/chat/sessions', headers=headers).json())
    assert client.get(f'/api/chat/sessions/{session.id}', headers=headers).status_code == 404


def test_missing_document_file_is_recoverable_error(client, auth, db, demo):
    doc = Document(organization_id=demo.id, kind='other', title='Missing original', data={},
                   file_key='nonexistent/audit-file.txt', file_name='audit.txt')
    db.add(doc)
    db.commit()
    response = client.get(f'/api/documents/{doc.id}/file', headers=auth)
    assert response.status_code == 404 and 'Исходный файл недоступен' in response.json()['detail']


def test_voice_status_requires_key_and_folder(monkeypatch):
    from bis import voice

    settings = get_settings()
    monkeypatch.setattr(settings, 'stt_provider', 'yandex')
    monkeypatch.setattr(type(settings), 'yandex', lambda self: ('folder', None))
    monkeypatch.setattr(voice, 'ffmpeg_exe', lambda: 'ffmpeg')
    assert voice.status()['stt'] is False


def test_long_voice_recording_is_not_silently_truncated(monkeypatch):
    from bis import voice

    monkeypatch.setattr(type(get_settings()), 'yandex', lambda self: ('folder', 'fixture-key'))
    monkeypatch.setattr(voice, '_ogg_chunks', lambda audio: [b'chunk'] * 41)
    with pytest.raises(voice.VoiceError, match='слишком длинная'):
        voice._transcribe_yandex(b'voice', None)
