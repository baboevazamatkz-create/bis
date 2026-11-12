"""Answer explicit contract-price questions from exact text without calculating a guessed total."""
import re
import uuid

from bis.models import Document
from bis.services.document_text import document_chunks, document_text
from bis.services.library_access import assert_document

PRICE = re.compile(r'(?:общая\s+(?:стоимость|сумма)|цена\s+договора|стоимость\s+(?:работ|договора)|сумма\s+договора)', re.I)
MONEY = re.compile(r'\d[\d \u00a0\u202f]*(?:[.,]\d{1,2})?\s*(?:\([^)]{1,160}\)\s*)?(?:руб\w*\.?|₽|тенге|KZT|RUB|USD|доллар\w*|евро)', re.I)


def contract_amount_answer(ctx, question):
    if not re.search(r'сумм|стоимост|цен[аыуе]|сколько.*стоит', question, re.I):
        return None
    # An advance, penalty or subtotal is a different question.
    if re.search(r'аванс|неустой|штраф|пен[яию]|остат|оплачен|доплат|без\s+ндс|размер\s+ндс', question, re.I):
        return None
    ref = ctx.selected_document or ctx.focus.get('document')
    if not ref or ctx.choices:
        return None
    try:
        doc = ctx.db.get(Document, uuid.UUID(ref))
    except (TypeError, ValueError):
        return None
    if doc is None or doc.organization_id != ctx.org_id or doc.kind != 'contract':
        return None
    assert_document(ctx.db, doc)
    chunks = document_chunks(ctx.db, doc)
    full = document_text(ctx.db, doc, ctx.storage)
    found = {}
    for anchor in PRICE.finditer(full):
        window = full[anchor.end():anchor.end() + 220]
        value = MONEY.search(window)
        if not value:
            continue
        # Don't consume a price from the next numbered clause or an advance clause.
        between = window[:value.start()]
        if re.search(r'\n\s*\d+\.\d+\s|аванс|неустой|штраф', between, re.I):
            continue
        start = full.rfind('\n', max(0, anchor.start() - 30), anchor.start()) + 1
        start = max(start, anchor.start() - 30)
        end = anchor.end() + value.end()
        tail = full[end:end + 120].split('\n', 1)[0]
        quote = full[start:end].strip() + tail
        key = re.sub(r'\s+', '', value.group()).casefold()
        found.setdefault(key, (value.group().strip(), quote))
    if not found:
        return None
    lines = []
    for value, quote in list(found.values())[:5]:
        source = next((chunk for chunk in chunks if quote in chunk.content or value in chunk.content), None)
        citation = f' [{ctx.add_citation(source.to_dict())}]' if source else ''
        lines.append(f'{value}{citation}\n«{quote}»')
    if len(found) == 1:
        return 'Общая сумма по договору — ' + lines[0]
    return 'В тексте договора найдены разные значения стоимости. Нужно сверить условия и редакции:\n\n' + '\n\n'.join(lines)
