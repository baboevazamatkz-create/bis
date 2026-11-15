"""Task memory and an artifact ledger: a draft request survives 'yes' and 'where is the file'."""
import re
import uuid

from bis.agents.base import AgentResult, claims_action, requests_write
from bis.models import Document
from bis.services.library_access import assert_document

CONTINUE = re.compile(r'^(да[.! ]*$|сделай[.! ]*$|созда[йт].*(?:из того|что есть)|.*(?:где|покажи|скачай).*(?:файл|черновик)|(?:продолж|додел|оформ|остальн|используй|по нашей форме))', re.I)
FILE = re.compile(r'(?:где|покажи|скачай|открой).*(?:файл|черновик|письм|приказ)', re.I)
ORGANIZATION_FORM = re.compile(r'\bпо\s+(?:(?:нашей|нашему|утвержд[её]нной|утвержд[её]нному)\s+)*(?:форме|бланку|шаблону)\b', re.I)
CUSTOMER = re.compile(r'\bзаказчик(?:у|а|ом|е|ам|ов|ами|ах)?\b', re.I)


def intent(text):
    if not requests_write(text):
        return None
    from bis.services.document_authoring import TYPES

    for kind, words in TYPES.items():
        if any(word in text.lower() for word in words):
            return kind
    return 'administrative' if re.search(r'записк|регламент|положени|инструкц|документ', text, re.I) else None


def prepare(ctx, user_text, previous, rows):
    state = dict(((previous.meta or {}).get('context') or {}).get('draft_task') or {}) if previous else {}
    scope = ctx.db.info.get('source_context')
    project = str(scope.object_id) if scope and scope.object_id else None
    if state and (state.get('organization_id') != str(ctx.org_id) or state.get('object_id') != project):
        state = {}
    kind = intent(user_text)
    if kind:
        state = {'kind': kind, 'request': user_text, 'status': 'pending', 'document_ids': [],
                 'organization_id': str(ctx.org_id), 'object_id': project}
    elif state and CONTINUE.search(user_text.strip()):
        pass
    elif not state and CONTINUE.search(user_text.strip()):
        # Recover older sessions only across uninterrupted continuation turns, not an arbitrary old instruction.
        for row in rows:
            if row.role != 'user':
                continue
            prior_kind = intent(row.content)
            if prior_kind:
                state = {'kind': prior_kind, 'request': row.content, 'status': 'pending', 'document_ids': [],
                         'organization_id': str(ctx.org_id), 'object_id': project}
                break
            if not CONTINUE.search(row.content.strip()):
                break
    else:
        state = {}
    ctx.draft_task = state
    request = state.get('request', '')
    ctx.db.info['require_organization_letter_form'] = state.get('kind') == 'letter' and bool(
        ORGANIZATION_FORM.search(request) or CUSTOMER.search(request))
    if not state:
        return user_text, None
    if FILE.search(user_text) and state.get('document_ids'):
        refs = []
        for ref in state['document_ids']:
            try:
                doc = ctx.db.get(Document, uuid.UUID(ref))
                if doc is None or doc.organization_id != ctx.org_id or not doc.file_key:
                    continue
                assert_document(ctx.db, doc)
            except (ValueError, LookupError):
                continue
            refs.append({'id': str(doc.id), 'kind': doc.kind, 'title': doc.title, 'status': doc.status,
                         'number': doc.number, 'created': True})
        if refs:
            return user_text, AgentResult(text='Файл черновика доступен ниже: «' + refs[0]['title'] + '».',
                                         agent=ctx.agent_name, documents=refs, provider='artifact_ledger', model='stored-file')
    if state.get('status') != 'created' and not kind:
        return state['request'] + '\nДополнение к этому же поручению: ' + user_text, None
    return user_text, None


def finalize(ctx, result, effective_request):
    from bis.agents.builtin_tools import _doc_ref

    if result.provider == 'library' and result.model == 'form-validation':
        return result

    refs = []
    for ref in ctx.db.info.get('request_created_documents', []):
        doc = ctx.db.get(Document, uuid.UUID(ref))
        if doc is not None and doc.organization_id == ctx.org_id and doc.file_key:
            refs.append(_doc_ref(doc) | {'created': True, 'basis': (doc.data or {}).get('authoring', {}).get('basis')})
    if refs:
        # A read source is not the file just requested by the user.
        result.documents = refs
        if not ctx.draft_task:
            # Tool-confirmed artifacts establish task memory even for a previously unknown document type.
            scope = ctx.db.info.get('source_context')
            doc = ctx.db.get(Document, uuid.UUID(refs[0]['id']))
            ctx.draft_task = {'kind': (doc.data or {}).get('authoring', {}).get('document_type', doc.kind),
                              'request': effective_request, 'organization_id': str(ctx.org_id),
                              'object_id': str(scope.object_id) if scope and scope.object_id else None}
        ctx.draft_task.update(status='created', document_ids=[ref['id'] for ref in refs])
        if not result.text.strip():
            result.text = 'Черновик создан. Файл доступен ниже.'
    elif ctx.draft_task and requests_write(effective_request):
        errors = [e['arguments'] for e in ctx.trace if e.get('error')]
        if claims_action(result.text, effective_request) or re.search(r'готово|создал|подготовил|оформил', result.text, re.I):
            result.text = ('Файл пока не создан: выполнение инструмента не подтверждено. '
                           'Поручение сохранено для продолжения.' + (' Есть ошибка подготовки документа.' if errors else ''))
        elif 'файл' not in result.text.lower():
            result.text = 'Файл пока не создан. ' + result.text
        result.documents = []
        ctx.draft_task['status'] = 'pending'
    return result
