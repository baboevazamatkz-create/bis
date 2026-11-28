"""Use the existing fact investigator and document writers for a requested draft.

A request for an act is not a production report and never publishes work facts.
"""
import re
from datetime import date

from sqlalchemy import select

from bis.agents.base import AgentResult
from bis.agents.resolve import obj_row
from bis.documents.aook import AOOKData
from bis.documents.aosr import AOSRData
from bis.documents.common import Party
from bis.models import Document
from bis.models.facts import FactDecision
from bis.security import has_scope
from bis.services import documents, exec_docs, project_facts
from bis.services.executive_pipeline import _LOC, norm
from bis.services.library_access import bind_context

KINDS = {'aosr': 'АОСР', 'aook': 'АООК'}
PREDICATES = ('project_ref', 'work_start', 'work_end', 'material')
WORK_PREFIX = re.compile(
    r'^.*?(?:аоср|аоок|акт\w*\s+(?:освидетельствования\s+)?(?:скрытых\s+работ|ответственных\s+конструкций))'
    r'(?:\s+по\s+(?:(?:загруженным|имеющимся|исходным)\s+)?документам)?'
    r'[\s,:–—-]*(?:(?:на|для|по)\s+)?', re.I | re.S)


def _signature(ctx, project_id):
    docs = project_facts.source_documents(ctx, project_id).with_only_columns(
        Document.id, Document.file_key, Document.updated_at, Document.status)
    decisions = select(FactDecision.id).where(FactDecision.organization_id == ctx.org_id,
                                               FactDecision.object_id == project_id)
    return project_facts.digest([ctx.db.execute(docs.order_by(Document.id)).all(),
                                 ctx.db.scalars(decisions.order_by(FactDecision.id)).all()])


def _matches(works, text):
    words = [word[:max(4, len(word) - 2)] for word in re.findall(r'[а-яёa-z0-9-]{4,}', norm(works))]
    return bool(words) and all(re.search(r'\b' + re.escape(word), norm(text)) for word in words)


def _collect(ctx, obj, works, location):
    from bis.brain.orchestrator import investigate

    criteria = {'predicates': PREDICATES, 'category': None, 'location': location, 'work_key': '',
                'source_kinds': ['project_doc', 'work_journal'],
                'steps': ['Проверить документы проекта', 'Сверить основания и подготовить черновик']}
    result = investigate(ctx, works, obj.id, plan=criteria)
    ids = {fact['document_id'] for fact in result['facts'] if fact['document_id']}
    kinds = {str(identifier): kind for identifier, kind in ctx.db.execute(
        select(Document.id, Document.kind).where(Document.organization_id == ctx.org_id,
                                                  Document.object_id == obj.id, Document.id.in_(ids)))}
    conflicting_ids = {fact['id'] for conflict in result['conflicts'] if conflict['state'] == 'open'
                       for fact in conflict['facts']}
    rejected, evidence = set(), {}
    for fact in result['facts']:
        field = fact['predicate']
        if field not in PREDICATES or not fact['document_id']:
            continue
        if field == 'project_ref':
            if kinds.get(fact['document_id']) != 'project_doc' or not _matches(works, fact['source_title'] + ' ' + fact['quote']):
                continue
        elif not (location and kinds.get(fact['document_id']) == 'work_journal' and fact['category'] == 'actual'
                  and _matches(works, fact['quote'])):
            continue
        if field in ('work_start', 'work_end') and not date.min < date.fromisoformat(fact['value']) <= date.today():
            continue
        if fact['id'] in conflicting_ids or fact['state'] != 'active':
            rejected.add(field)
        else:
            evidence.setdefault(field, []).append(fact)
    plan, used, gaps = {}, [], []
    for field in PREDICATES:
        candidates = evidence.get(field, [])
        values = sorted({fact['value'] for fact in candidates})
        if field in rejected:
            gaps.append((field, 'Основание содержит конфликт или ещё не подтверждено: ' + project_facts.LABELS[field]))
        elif len(values) == 1:
            plan[field] = values[0]
            used.extend(candidates)
        elif len(values) > 1:
            gaps.append((field, 'Источники расходятся: ' + ', '.join(values[:5])))
    if plan.get('work_start') and plan.get('work_end') and plan['work_start'] > plan['work_end']:
        gaps.append(('period', 'Подтверждённые источники содержат несовместимые даты работ'))
        plan.pop('work_start')
        plan.pop('work_end')
        used = [fact for fact in used if fact['predicate'] not in ('work_start', 'work_end')]
    if any(result['coverage'][key] for key in ('remaining', 'failed', 'truncated_facts')):
        gaps.append(('coverage', 'Часть подходящих источников ещё не проверена'))
    return {'fields': plan, 'evidence': used, 'gaps': gaps, 'remaining': result['coverage']['remaining']}


def run(ctx, user_text):
    task = ctx.draft_task
    kind = task['kind']
    label = KINDS[kind]

    def result(text):
        return AgentResult(text=text, agent=ctx.agent_name, citations=ctx.citations, trace=ctx.trace,
                           provider='executive_draft', model='scoped-facts')

    if not has_scope(ctx.user.role, 'documents:write'):
        task['status'] = 'blocked'
        return result('Недостаточно прав для создания черновика.')
    scope = ctx.db.info.get('source_context')
    if not scope or not scope.object_id:
        task['status'] = 'blocked'
        return result('Выберите проект в верхней панели и откройте поручение в выбранном проекте.')
    from bis.agents.authoring_tasks import AFFIRM, PARTIAL

    request = task['request']
    raw_work = WORK_PREFIX.sub('', request)
    consent = PARTIAL.search(raw_work)
    if consent:
        raw_work = raw_work[:consent.start()]
    works = task.get('works') or raw_work.strip(' ,:;.?!')
    if not works or works == request:
        if (user_text != request and not PARTIAL.search(user_text)
                and not AFFIRM.fullmatch(user_text.strip()) and '?' not in user_text):
            works = user_text.strip(' .!?')
        else:
            task['status'] = 'needs_scope'
            return result(f'Укажите работы для {label}.')
    task['works'] = works
    obj = obj_row(ctx, str(scope.object_id))
    place = _LOC.search(works)
    location = place[0] if place else None
    signature = _signature(ctx, obj.id)
    if (task.get('source_signature') != signature
            or (task.get('allow_incomplete') and task.get('plan', {}).get('remaining'))):
        task['plan'] = _collect(ctx, obj, works, location)
        task['source_signature'] = _signature(ctx, obj.id)
    prepared = task['plan']
    from bis.brain.orchestrator import cite

    refs = [cite(ctx, fact) for fact in prepared['evidence']]
    for citation in ctx.citations:
        citation['used'] = True
    if not task.get('allow_incomplete'):
        task['status'] = 'awaiting_permission'
        sources = (' Найдены основания ' + ' '.join(dict.fromkeys(refs)) + '.') if refs else ''
        return result(f'Факт выполнения работ требует проверки.{sources} Создать редактируемый черновик {label} с пропусками?')
    fields = prepared['fields']
    if ctx.progress:
        ctx.progress({'label': f'Сохраняю черновик {label}'})
    p = documents.object_participants(ctx.db, ctx.org_id, obj.id)
    data = (AOSRData if kind == 'aosr' else AOOKData)(
        **({'permit': None} if kind == 'aook' else {}),
        object_name=p['object_name'], developer=p['developer'] or Party(name=''), builder=p['builder'],
        designer=p['designer'], performer=p['performer'], representatives=p['representatives'],
        other_representatives=p['other_representatives'], works=works, project_docs=fields.get('project_ref', ''),
        work_start=fields.get('work_start'), work_end=fields.get('work_end'),
        materials=[fields['material']] if fields.get('material') else [],
        additional_info='Факт выполнения работ требует проверки. Неустановленные сведения оставлены для заполнения.')
    # Keep the source binding and the existing template policy through the real writer.
    bind_context(ctx.db, ctx.org_id, obj.id)
    writer = documents.create_aosr if kind == 'aosr' else exec_docs.create_aook
    try:
        ctx.db.info['library_form_format'] = 'docx'
        if task.get('form_version_id'):
            ctx.db.info['library_form_id'] = task['form_version_id']
        with ctx.db.begin_nested():
            doc = writer(ctx.db, ctx.storage, org_id=ctx.org_id, data=data,
                         created_by=f'agent:{ctx.agent_name}', object_id=obj.id)
    except (LookupError, ValueError, OSError) as exc:
        task['status'] = 'blocked'
        task['last_error'] = str(exc)
        return result('Файл пока не создан. ' + str(exc))
    finally:
        ctx.db.info.pop('library_form_id', None)
        ctx.db.info.pop('library_form_format', None)
    doc.issues = [*(doc.issues or []), {'field': 'completion', 'level': 'error',
                 'message': 'Факт выполнения работ требует проверки.'},
                 *({'field': field, 'level': 'error', 'message': message} for field, message in prepared['gaps'])]
    doc.data = {**doc.data, 'authoring_facts': prepared['evidence']}
    task['missing'] = list(dict.fromkeys(issue.get('field') for issue in doc.issues if issue.get('field')))
    task.pop('last_error', None)
    gaps = list(dict.fromkeys(issue['message'] for issue in doc.issues))
    return result(f'Черновик {label} сохранён, файл доступен ниже.\n\n' +
                  '\n'.join('• ' + item for item in gaps[:10]))
