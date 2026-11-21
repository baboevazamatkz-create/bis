"""An archived outgoing letter is usable stationery without manual registration."""
import io
from types import SimpleNamespace
from zipfile import ZipFile

from docx import Document
from PIL import Image

from bis.services import document_authoring as authoring


def test_result_reports_actual_unknown_signer_instead_of_model_claim(monkeypatch):
    import uuid

    from bis.agents import builtin_tools
    from bis.agents.authoring_tasks import finalize
    from bis.agents.base import AgentResult
    doc = SimpleNamespace(id=uuid.uuid4(), organization_id='org', file_key='saved', kind='letter',
        data={'authoring': {'basis': {'identity_review_required': True,
                                    'sample': {'title': 'Архивное исходящее письмо'}}}})
    monkeypatch.setattr(builtin_tools, '_doc_ref', lambda d: {'id': str(d.id), 'title': 'Письмо', 'kind': 'letter'})
    ctx = SimpleNamespace(org_id='org', draft_task={'kind': 'letter'},
        db=SimpleNamespace(info={'request_created_documents': [str(doc.id)]}, get=lambda *a: doc))
    result = finalize(ctx, AgentResult(text='Подписант — Seed Person.', agent='assistant'), 'Напиши письмо')
    assert 'Seed Person' not in result.text
    assert 'не подтверждён' in result.text and 'Архивное исходящее письмо' in result.text


def test_archive_letter_does_not_inherit_unverified_card_signer():
    import pytest

    from bis.documents import LetterData
    from bis.documents.common import Party
    from bis.services.document_authoring import reconcile_archive_identity
    data = LetterData(sender=Party(name='Pilot Company'), subject='New', body=['New text'],
                      addressee=['Customer'], signer_name='Seed Person', signer_position='Director', executor='Seed User')
    basis = {'selection_reason': 'outgoing_organization_archive', 'sender_confirmed': False}
    revised = reconcile_archive_identity(data, basis)
    assert revised.signer_name == '[Подписант: уточнить]' and revised.executor is None
    assert data.signer_name == 'Seed Person'
    with pytest.raises(ValueError, match='карточки'):
        reconcile_archive_identity(data.model_copy(update={'body': ['С уважением, Pilot Company.']}), basis)


def test_outgoing_image_stationery_survives_without_matching_card(monkeypatch):
    logo = io.BytesIO()
    Image.new('RGB', (220, 55), 'red').save(logo, format='PNG')
    old = Document()
    old.add_paragraph().add_run().add_picture(io.BytesIO(logo.getvalue()))
    old.add_paragraph('01.01.2000 № 777')
    old.add_paragraph('OLD RECIPIENT AND BODY')
    signature = io.BytesIO()
    Image.new('RGB', (40, 20), 'blue').save(signature, format='PNG')
    old.add_paragraph().add_run().add_picture(io.BytesIO(signature.getvalue()))
    buf = io.BytesIO()
    old.save(buf)
    org = SimpleNamespace(name='Pilot organization', inn=None, address=None, phone=None, email=None)
    fresh = authoring.render_administrative(org, 'Письмо', 'Новая тема', ['Новый текст'], '', '', {})
    choice = dict(id='source', title='Исходящие/Письмо.docx', file_name='Письмо.docx',
                  level='organization', outgoing=True, _file_key='source')
    monkeypatch.setattr(authoring, 'examples', lambda *a, **kw: [choice])
    result, basis = authoring.adapt_layout(SimpleNamespace(get=lambda *a: org),
        SimpleNamespace(get=lambda *a: buf.getvalue()), 'org', 'letter', fresh)
    assert basis['mode'] == 'sender_brand_and_styles'
    with ZipFile(io.BytesIO(result)) as package:
        media = [package.read(n) for n in package.namelist() if n.startswith('word/media/')]
        xml = '\n'.join(package.read(n).decode() for n in package.namelist() if n.endswith('.xml'))
    assert logo.getvalue() in media
    assert signature.getvalue() not in media
    assert 'OLD RECIPIENT' not in xml and '777' not in xml and 'Новый текст' in xml


def test_required_letter_uses_archive_without_registered_form(db, demo):
    from test_adaptive_authoring import context, sample

    from bis.agents.builtin_tools import LetterArgs, create_letter_draft
    original = sample(db, demo, brand='ARCHIVE LETTERHEAD')
    ctx = context(db, demo)
    ctx.db.info['require_organization_letter_form'] = True
    result = create_letter_draft.handler(ctx, LetterArgs(subject='О проверке', body=['Новый текст']))
    assert result['created'] and result['basis']['sample']['id'] == str(original.id)
    assert result['basis']['mode'] == 'sender_brand_and_styles'


def test_organization_library_overrides_stale_project_binding(db, demo, tmp_path):
    from test_adaptive_authoring import context, sample

    from bis.models import ConstructionObject
    from bis.models.libraries import LibraryConnection, LibraryFile, LibraryVersion
    project = ConstructionObject(organization_id=demo.id, name='Archived pilot', status='archived')
    db.add(project)
    db.flush()
    original = sample(db, demo)
    original.object_id = project.id
    original.kind = 'other'
    original.title = '123'
    base = LibraryConnection(organization_id=demo.id, name='Company', path=str(tmp_path),
                             level='organization', enabled=True)
    db.add(base)
    db.flush()
    item = LibraryFile(connection_id=base.id, relative_path='Исходящие/123.docx', state='ready')
    db.add(item)
    db.flush()
    version = LibraryVersion(file_id=item.id, document_id=original.id, revision=1, state='ready',
                             file_key=original.file_key, sha256='0' * 64, mapping={})
    db.add(version)
    db.flush()
    item.active_version_id = version.id
    original.data = {**original.data, 'library_version_id': str(version.id)}
    demo.settings = {**demo.settings, 'libraries_strict': True}
    db.flush()
    context(db, demo)
    found = authoring.examples(db, demo.id, 'letter')
    assert str(original.id) in {i['id'] for i in found}
    base.enabled = False
    db.flush()
    assert str(original.id) not in {i['id'] for i in authoring.examples(db, demo.id, 'letter')}
