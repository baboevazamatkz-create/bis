"""Reusable archive-example discovery and administrative drafting, independent of the organization."""
from datetime import date

from pydantic import BaseModel, Field
from sqlalchemy import select

from bis.agents.base import AgentContext, tool
from bis.agents.builtin_tools import _doc_ref
from bis.agents.resolve import object_ref
from bis.documents.aosr import REPRESENTATIVE_ROLES
from bis.documents.templates import default_template, render_template
from bis.models import DocumentTemplate, Organization
from bis.services import document_authoring as authoring
from bis.services import documents


class ExamplesArgs(BaseModel):
    document_type: str = Field(description='Вид документа: letter, order или название нового административного документа')
    purpose: str = Field(default='', description='Назначение документа, чтобы найти подходящие образцы')


@tool('find_document_examples', 'Найти образцы и формы в разрешённых базах текущей организации/проекта/знаний. '
      'Работает с архивными документами без заполнителей; пути и названия организации задавать не нужно. '
      'Образец — оформление/структура, не источник новых дат, адресатов или подписей.', scopes=('documents:read',))
def find_document_examples(ctx: AgentContext, args: ExamplesArgs):
    choices = authoring.examples(ctx.db, ctx.org_id, args.document_type, args.purpose)
    templates = ctx.db.scalars(select(DocumentTemplate).where(
        DocumentTemplate.organization_id == ctx.org_id, DocumentTemplate.kind == authoring.canonical_type(args.document_type),
        DocumentTemplate.is_default.is_(True))).all()
    return {'registered_templates': [{'id': str(template.id), 'name': template.name} for template in templates],
            'document_type': args.document_type, 'examples': [authoring.public_example(item) for item in choices],
            'policy': 'Сначала утверждённая форма, затем подходящий архивный образец. Черновик и его ограничения показываются явно.'}


class DraftArgs(BaseModel):
    document_type: str = Field(description='Вид административного документа, например order/приказ, memo/служебная записка. Для ИД используй специализированный конвейер.')
    subject: str = Field(min_length=1, max_length=500)
    paragraphs: list[str] = Field(min_length=1, max_length=100, description='Готовый текст по заданию и найденным фактам; неизвестные значения явно [уточнить]')
    fields: dict[str, str] = Field(default_factory=dict, description='Дополнительные реквизиты любого вида документа, без выдуманных значений')
    sample_id: str | None = Field(default=None, description='Образец из find_document_examples; пусто — подобрать автоматически')
    object_id: str | None = None
    document_date: date | None = Field(None, description='Дата документа для подстановки действующих назначений')
    signer_position: str | None = None
    signer_name: str | None = None


@tool('create_document_draft', 'Создать реальный редактируемый DOCX административного документа произвольного вида '
      '(приказ, записка, инструкция и т.д.). Сам подбирает образец в подключённых базах текущей организации. '
      'Старые факты образца не переносятся. Отсутствующие реквизиты отмечаются для проверки, а не блокируют черновик. '
      'Не заменяет специализированные формы АОСР/АООК/схем. Возвращает созданный файл и использованный образец.', scopes=('documents:write',))
def create_document_draft(ctx: AgentContext, args: DraftArgs):
    kind = authoring.canonical_type(args.document_type)
    if kind in authoring.REGULATED or any(word in args.document_type.lower() for word in ('аоср', 'аоок', 'кс-2', 'кс-3', 'исполнительн')):
        raise ValueError('Этот документ создаётся специализированным конвейером ИД с проверкой формы и оснований')
    if kind == 'letter':
        raise ValueError('Для письма вызови create_letter_draft: он также автоматически подбирает архивный образец')
    org = ctx.db.get(Organization, ctx.org_id)
    project_ref = args.object_id or ctx.focus.get('object')
    project_id = object_ref(ctx, project_ref) if project_ref else None
    doc_date = args.document_date or date.today()
    fields = dict(args.fields)
    if kind == 'акт осмотра' and project_id:
        participants = documents.object_participants(ctx.db, ctx.org_id, project_id, doc_date)
        customer = participants['developer']
        fields['Объект'] = participants['object_name']
        fields['Заказчик'] = ', '.join(filter(None, [
            customer.format() if customer else '[Заказчик: уточнить]',
            f'КПП {customer.kpp}' if customer and customer.kpp else None,
            customer.email if customer else None,
        ]))
        signers = [
            f"{REPRESENTATIVE_ROLES[role]['title']} — {rep.format()}"
            for role, rep in participants['representatives'].items()
        ]
        signers.extend(f"{REPRESENTATIVE_ROLES[role]['title']} — [уточнить представителя]"
                       for role in participants['ambiguous_representative_roles'])
        fields['Подписанты по назначениям'] = '\n'.join(signers) or '[Подписанты: уточнить]'
    signer = (org.settings or {}).get('default_signer') or {}
    position, name = args.signer_position or signer.get('position'), args.signer_name or signer.get('name')
    fresh = authoring.render_administrative(org, args.document_type, args.subject, args.paragraphs, position, name, fields, doc_date=doc_date)
    template = default_template(ctx.db, ctx.org_id, kind) if not args.sample_id else None
    if template is not None:
        content = render_template(ctx.storage.get(template.file_key), {**fields, 'fields': fields,
            'subject': args.subject, 'body': args.paragraphs, 'body_text': '\n'.join(args.paragraphs),
            'sender_text': org.name, 'signer_name': name or '[Подписант: уточнить]',
            'signer_position': position or '[Должность: уточнить]', 'date': doc_date})
        basis = {'mode': 'registered_template', 'template_id': str(template.id), 'review_required': True,
                 'note': 'Использован зарегистрированный шаблон организации. Проверьте заполненные реквизиты.'}
    else:
        content, basis = authoring.adapt_layout(ctx.db, ctx.storage, ctx.org_id, kind, fresh, args.subject, args.sample_id)
    doc = documents.save_document(ctx.db, ctx.storage, org_id=ctx.org_id,
        kind=kind if kind in authoring.TYPES else 'administrative', title=f'{args.document_type}: {args.subject}',
        data={'subject': args.subject, 'body': args.paragraphs, 'fields': fields,
              'authoring': {'document_type': args.document_type, 'basis': basis}}, file_bytes=content,
        file_name=f'Черновик_{kind if kind in authoring.TYPES else "документ"}.docx', created_by=f'agent:{ctx.agent_name}',
        object_id=project_id, doc_date=doc_date, issues=[{'level': 'warning', 'message': basis['note']},
            *([{'level': 'warning', 'message': 'Подписант не подтверждён'}] if not name else [])])
    ref = _doc_ref(doc) | {'created': True, 'basis': basis}
    ctx.documents.append(ref)
    ctx.created_document_ids.append(str(doc.id))
    return ref


AUTHORING_TOOLS = [find_document_examples, create_document_draft]
