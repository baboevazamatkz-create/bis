"""Task memory and an artifact ledger: a draft request survives 'yes' and 'where is the file'."""
import io
import re
import uuid
from zipfile import BadZipFile, ZipFile

from lxml.etree import XMLSyntaxError

from bis.agents.base import ASK_ACT, AgentResult, claims_action, requests_write
from bis.models import Document
from bis.services.library_access import assert_document

DECLINE = re.compile(r'^\s*(?:нет\b|отмена\b|отмени\b|стоп\b|(?:пока\s+)?не\s+(?:создавай|создайте|подготавливай|оформляй|надо|нужно)\b)', re.I)
AFFIRM = re.compile(r'^(?:да|сделай|создай|подтверждаю|согласен|можно)[.! ]*$', re.I)
PARTIAL = re.compile(r'из\s+того,?\s+что\s+есть|(?:оставь|оставьте)\s+.*(?:пуст|пропуск)|(?:допускаю|согласен.{0,15}|создай.{0,25})\s+неполн|сам[аи]?\s+допиш', re.I)
EXECUTIVE = {
    'aosr': re.compile(r'\bаоср\b|акт\w*\s+(?:освидетельствования\s+)?скрытых\s+работ', re.I),
    'aook': re.compile(r'\bаоок\b|акт\w*\s+(?:освидетельствования\s+)?ответственных\s+конструкций', re.I),
}
CONTINUE = re.compile(r'^(да[.! ]*$|сделай[.! ]*$|созда[йт].*(?:из того|что есть)|.*(?:где|покажи|скачай|пришли|дай).*(?:файл|черновик)|(?:продолж|додел|оформ|остальн|используй|по нашей форме))', re.I)
ORGANIZATION_FORM = re.compile(r'\bпо\s+(?:(?:нашей|нашему|утвержд[её]нной|утвержд[её]нному)\s+)*(?:форме|бланку|шаблону)\b', re.I)
CUSTOMER = re.compile(r'\bзаказчик(?:у|а|ом|е|ам|ов|ами|ах)?\b', re.I)


def intent(text):
    if not requests_write(text):
        return None
    from bis.services.document_authoring import TYPES

    task_text = text[ASK_ACT.search(text).end():]
    target = re.split(r'\b(?:на основании|на основе|используя)\b', task_text, maxsplit=1, flags=re.I)[0]
    typed_targets = {**EXECUTIVE, 'scheme': re.compile(r'исполнительн\w+\s+схем', re.I),
                     'remarks': re.compile(r'ведомост\w*\s+замечан', re.I)}
    candidates = [(match.start(), kind) for kind, pattern in typed_targets.items() if (match := pattern.search(target))]
    for kind, words in TYPES.items():
        match = re.search(r'\b(?:' + '|'.join(re.escape(word) for word in words) + ')', target, re.I)
        if match:
            candidates.append((match.start(), kind))
    if candidates:
        return min(candidates)[1]
    return 'administrative' if re.search(r'записк|регламент|положени|инструкц|документ|акт\s+осмотра', task_text, re.I) else None


def prepare(ctx, user_text, previous, rows, form_version_id=None):
    state = dict(((previous.meta or {}).get('context') or {}).get('draft_task') or {}) if previous else {}
    scope = ctx.db.info.get('source_context')
    project = str(scope.object_id) if scope and scope.object_id else None
    incompatible_state = state and (state.get('organization_id') != str(ctx.org_id) or state.get('object_id') != project)
    if incompatible_state:
        state = {}
    kind = intent(user_text)
    if DECLINE.search(user_text) and (kind or state):
        ctx.draft_task = {}
        ctx.suspended_task = {}
        return user_text, AgentResult(text='Подготовка остановлена.', agent=ctx.agent_name,
                                     provider='artifact_ledger', model='cancelled-task')
    meta = (previous.meta or {}) if previous else {}
    if not state and meta.get('model') == 'cancelled-task' and AFFIRM.fullmatch(user_text.strip()):
        return user_text, AgentResult(text='Сейчас нет поручения, ожидающего подтверждения.', agent=ctx.agent_name,
                                     provider='artifact_ledger', model='cancelled-task')
    allow_affirmation = bool(meta.get('awaits_draft_permission'))
    same_request = bool(state and ' '.join(user_text.casefold().split()) == ' '.join(state['request'].casefold().split()))
    followup = bool(same_request or AFFIRM.fullmatch(user_text.strip()) or user_text.strip(' .!?').casefold() == 'пришли'
                    or PARTIAL.search(user_text) or CONTINUE.search(user_text.strip()))
    if kind and not same_request:
        state = {'kind': kind, 'request': user_text, 'status': 'pending', 'document_ids': [],
                 'organization_id': str(ctx.org_id), 'object_id': project,
                 'expected': 'downloadable_document', 'allow_incomplete': bool(PARTIAL.search(user_text))}
    elif state and (followup or state.get('status') == 'needs_scope'):
        if PARTIAL.search(user_text) or (AFFIRM.fullmatch(user_text.strip()) and state.get('status') == 'awaiting_permission' and allow_affirmation):
            state['allow_incomplete'] = True
    elif not state and followup and not incompatible_state:
        # Older histories can only resume uninterrupted directives in the same chat.
        for row in rows:
            if row.role != 'user':
                continue
            prior_kind = intent(row.content)
            if prior_kind:
                state = {'kind': prior_kind, 'request': row.content, 'status': 'pending', 'document_ids': [],
                         'organization_id': str(ctx.org_id), 'object_id': project}
                break
            if not (CONTINUE.search(row.content.strip()) or AFFIRM.fullmatch(row.content.strip())):
                break
    else:
        ctx.suspended_task = state
        state = {}
    ctx.draft_task = state
    request = state.get('request', '')
    ctx.db.info['require_organization_letter_form'] = state.get('kind') == 'letter' and bool(
        ORGANIZATION_FORM.search(request) or CUSTOMER.search(request))
    if not state:
        return user_text, None
    if state.get('status') in ('created', 'artifact_unavailable') and followup:
        from bis.agents.builtin_tools import _doc_ref

        refs = [_doc_ref(doc) | {'created': True} for ref in state.get('document_ids', [])
                if (doc := verified_document(ctx, ref, state))]
        if refs:
            return user_text, AgentResult(text='Файл черновика доступен ниже: «' + refs[0]['title'] + '».',
                                         agent=ctx.agent_name, documents=refs, provider='artifact_ledger', model='stored-file')
        return user_text, AgentResult(text='Ранее сохранённый файл сейчас недоступен. Для создания нового черновика поставьте новое поручение.',
                                     agent=ctx.agent_name, provider='artifact_ledger', model='unavailable')
    if state['kind'] in EXECUTIVE and (getattr(ctx, 'workspace', None) == 'executive' or state.get('allow_incomplete')):
        chosen = re.search(r'(?i)форма?\w*\s+([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b', user_text)
        if form_version_id or chosen:
            state['form_version_id'] = str(form_version_id or uuid.UUID(chosen[1]))
        from bis.agents.executive_drafts import run

        return state['request'], run(ctx, user_text)
    if state.get('status') != 'created' and not kind:
        return state['request'] + '\nДополнение к этому же поручению: ' + user_text, None
    return user_text, None


DOCUMENT_TOOLS = {
    'create_letter_draft': 'letter', 'create_aosr_draft': 'aosr', 'create_aook_draft': 'aook',
    'create_executive_scheme': 'scheme', 'create_remarks_sheet': 'remarks',
}


def incompatible_tool(task, name, arguments):
    expected = task.get('kind')
    if not expected:
        return None
    actual = DOCUMENT_TOOLS.get(name)
    if name == 'create_document_draft':
        from bis.services.document_authoring import canonical_type

        try:
            actual = canonical_type(arguments['document_type'])
        except (KeyError, ValueError):
            return None
        if expected == 'administrative':
            return None
    if name == 'prepare_executive_package' and expected not in ('administrative', 'package'):
        return 'Комплект работ формируется по отдельному поручению о выполненной работе'
    if actual and actual != expected:
        return f'Ожидается документ вида {expected}. Инструмент {name} создаёт другой вид ({actual})'
    return None


def verified_document(ctx, reference, task):
    try:
        doc = ctx.db.get(Document, uuid.UUID(reference))
        if doc is None or doc.organization_id != ctx.org_id or not doc.file_key:
            return None
        if task:
            project = task.get('object_id')
            if project and project != (str(doc.object_id) if doc.object_id else None):
                return None
            kind = task.get('kind')
            if (kind and kind not in ('administrative', doc.kind)
                    and (doc.data or {}).get('authoring', {}).get('document_type') != kind):
                return None
        scope = ctx.db.info.get('source_context')
        if scope and scope.object_id is not None and doc.object_id is not None and doc.object_id != scope.object_id:
            return None
        assert_document(ctx.db, doc)
        content = ctx.storage.get(doc.file_key)
        if not content:
            return None
        if (doc.file_name or '').lower().endswith('.docx'):
            from docx import Document as Word

            with ZipFile(io.BytesIO(content)) as package:
                if package.testzip() or 'word/document.xml' not in package.namelist():
                    return None
            if Word(io.BytesIO(content)).element.body is None:
                return None
        return doc
    except (AttributeError, ValueError, TypeError, LookupError, OSError, BadZipFile, XMLSyntaxError):
        return None


def finalize(ctx, result, effective_request):
    from bis.agents.builtin_tools import _doc_ref

    if result.provider == 'artifact_ledger' or (result.provider == 'library' and result.model == 'form-validation'):
        return result

    refs = []
    attempted = ctx.db.info.get('request_created_documents', [])
    for ref in attempted:
        doc = verified_document(ctx, ref, ctx.draft_task)
        if doc is not None:
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
        projects = {ctx.db.get(Document, uuid.UUID(ref['id'])).object_id for ref in refs}
        if not ctx.draft_task.get('object_id') and len(projects) == 1:
            project = next(iter(projects))
            ctx.draft_task['object_id'] = str(project) if project else None
        ctx.draft_task.update(status='created', document_ids=[ref['id'] for ref in refs])
        uncertain = [ref for ref in refs if (ref.get('basis') or {}).get('identity_review_required')]
        if uncertain:
            result.text = 'Создан черновик: ' + '; '.join(ref['title'] for ref in refs) + '. Файл доступен ниже.'
            for ref in uncertain:
                sample = (ref['basis'].get('sample') or {}).get('title', 'исходящее письмо организации')
                result.text += ('\nБланк автоматически взят из «' + sample + '». Подписант не подтверждён '
                                'источниками: в файле оставлены поля для уточнения, реквизиты из карточки не подставлены.')
        if not result.text.strip():
            result.text = 'Черновик создан. Файл доступен ниже.'
    elif attempted:
        ctx.draft_task.update(status='artifact_unavailable', document_ids=list(attempted))
        result.text = 'Файл пока недоступен. Тип, область доступа или содержимое файла ещё не подтверждены.'
        result.documents = []
    elif ctx.draft_task and (requests_write(effective_request) or result.provider == 'executive_draft'):
        errors = [e['arguments'] for e in ctx.trace if e.get('error')]
        if result.provider == 'executive_draft' and ctx.draft_task['status'] in ('awaiting_permission', 'needs_scope', 'blocked'):
            return result
        if claims_action(result.text, effective_request) or re.search(r'готово|создал|подготовил|оформил', result.text, re.I):
            result.text = ('Файл пока не создан: выполнение инструмента не подтверждено. '
                           'Поручение сохранено для продолжения.' + (' Есть ошибка подготовки документа.' if errors else ''))
        elif 'файл' not in result.text.lower():
            result.text = 'Файл пока не создан. ' + result.text
        result.documents = []
        ctx.draft_task['status'] = 'pending'
    return result
