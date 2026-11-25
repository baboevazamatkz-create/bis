"""Review, learn and fill unfamiliar Word forms using the library workflow."""
import io

from docx import Document as Word

from bis.config import get_settings
from bis.models import Organization


def _word_form(*labels):
    doc = Word()
    for label in labels:
        doc.add_paragraph(label)
    dest = io.BytesIO()
    doc.save(dest)
    return dest.getvalue()


def _upload(client, auth, name, raw):
    upload = client.post('/api/libraries/upload', headers=auth, data={'level': 'organization'},
                         files={'files': (name, raw)})
    assert upload.status_code == 201, upload.text
    connection = upload.json()['id']
    return client.get(f'/api/libraries/{connection}/files', headers=auth).json()[0]['active_version_id']


def test_unfamiliar_letter_is_reviewed_saved_generated_and_learned(client, auth, db, demo, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), 'library_roots', [tmp_path])
    db.get(Organization, demo.id).inn = '7701234567'
    db.commit()
    original = _word_form('Тема письма: ______', 'Текст письма: ______', 'ИНН нашей организации: ______')
    first = _upload(client, auth, 'Новое письмо.docx', original)
    path = f'/api/libraries/versions/{first}'
    prepared = client.post(path + '/prepare?kind=letter', headers=auth)
    assert prepared.status_code == 200, prepared.text
    prepared = prepared.json()
    assert prepared['layout'][0]['text'] == 'Тема письма: ______'
    assert {entry['field'] for entry in prepared['entries']} >= {'body', 'subject'}
    mapping = {key: prepared[key] for key in ('format', 'entries', 'profile')}
    body = {'kind': 'letter', 'mapping': mapping}
    assert client.post(path + '/approve', headers=auth, json=body).status_code == 400
    preview = client.post(path + '/preview', headers=auth, json=body)
    assert preview.status_code == 200, preview.text[:300]
    body['preview_token'] = preview.headers['X-BIS-Form-Preview']
    assert client.post(path + '/approve', headers=auth, json=body).status_code == 200
    assert client.get(path + '/review', headers=auth).json()['kind'] == 'letter'
    blank = client.post(path + '/generate', headers=auth, json={})
    assert blank.status_code == 200, blank.text[:300]
    blank_id = blank.headers['X-BIS-Document-Id']
    rejected = client.post(f'/api/documents/{blank_id}/approve', headers=auth)
    assert rejected.status_code == 400 and 'Нет данных' in rejected.text
    values = {entry['target']: 'Учебное сообщение' for entry in mapping['entries'] if entry['field'] == 'body'}
    result = client.post(path + '/generate', headers=auth, json={'values': values})
    assert result.status_code == 200, result.text[:300]
    paragraphs = [p.text for p in Word(io.BytesIO(result.content)).paragraphs]
    assert 'Текст письма: Учебное сообщение' in paragraphs
    assert 'ИНН нашей организации: 7701234567' in paragraphs
    fresh = client.post(path + '/generate', headers=auth, json={'values': values})
    assert fresh.status_code == 200
    fresh_id = fresh.headers['X-BIS-Document-Id']
    approved = client.post(f'/api/documents/{fresh_id}/approve', headers=auth)
    assert approved.status_code == 200, approved.text
    db.get(Organization, demo.id).inn = '7701234568'
    db.commit()
    stale_id = result.headers['X-BIS-Document-Id']
    stale = client.post(f'/api/documents/{stale_id}/approve', headers=auth)
    assert stale.status_code == 400 and 'Источники заполнения изменились' in stale.text
    second = _upload(client, auth, 'Похожее письмо.docx',
                     _word_form('Тема письма: __________', 'Текст письма: ______', 'ИНН нашей организации: ______'))
    learned = client.post(f'/api/libraries/versions/{second}/prepare?kind=letter', headers=auth).json()
    assert 'learned' in learned['origins'].values()
    third = _upload(client, auth, 'Изменённое письмо.docx', _word_form('Тема письма: ______', 'Текст письма: ______',
                                                                        'Другая структура документа'))
    changed = client.post(f'/api/libraries/versions/{third}/prepare?kind=letter', headers=auth).json()
    assert 'learned' not in changed['origins'].values()


def test_aook_preserves_merged_cells_and_shows_missing_fields(client, auth, tmp_path, monkeypatch):
    from bis.services import library_forms

    monkeypatch.setattr(get_settings(), 'library_roots', [tmp_path])
    doc = Word()
    doc.add_heading('Акт ответственных конструкций', level=1)
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = 'Разрешение на нагружение: ______'
    for name in sorted(library_forms.REQUIRED['aook'] - {'permit'}):
        doc.add_paragraph(library_forms.FIELDS[name] + ': ______')
    stream = io.BytesIO()
    doc.save(stream)
    version = _upload(client, auth, 'Учебный АООК.docx', stream.getvalue())
    path = f'/api/libraries/versions/{version}'
    prepared = client.post(path + '/prepare?kind=aook', headers=auth)
    assert prepared.status_code == 200, prepared.text
    payload = prepared.json()
    assert payload['layout'][1]['type'] == 'table'
    assert {entry['field'] for entry in payload['entries']} == library_forms.REQUIRED['aook']
    body = {'kind': 'aook', 'mapping': {key: payload[key] for key in ('format', 'entries', 'profile')}}
    preview = client.post(path + '/preview', headers=auth, json=body)
    assert preview.status_code == 200, preview.text[:300]
    expect = Word(io.BytesIO(preview.content)).tables[0]
    assert expect.cell(0, 0)._tc is expect.cell(0, 1)._tc
    body['preview_token'] = preview.headers['X-BIS-Form-Preview']
    assert client.post(path + '/approve', headers=auth, json=body).status_code == 200
    source_values = client.post(path + '/resolve', headers=auth, json=body)
    assert source_values.status_code == 200, source_values.text
    fields = source_values.json()['fields']
    assert any(row['field'] == 'permit' and row['state'] == 'missing' for row in fields.values())
