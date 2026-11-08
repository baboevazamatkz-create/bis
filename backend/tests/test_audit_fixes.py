"""Аудит: агенты не переспрашивают то, что есть в данных; колонки реквизитов; вид и суть договора."""

import pytest

from bis.agents.base import claims_action, deflects, text_tool_call
from bis.brain.card import rules_card
from bis.rag.columns import split_columns
from bis.services.archive import contract_kind

REQ = """13. РЕКВИЗИТЫ СТОРОН
Заказчик:                                   Подрядчик:
АО «Сахаэнерго»                             ООО «Пилот»
ИНН 1435045660 КПП 143501001               ИНН 1435000000 КПП 143501001
р/с 40702810776000100123                   р/с 40702810076000000001
Генеральный директор                       Директор"""


def test_split_columns_keeps_each_party_whole():
    out = split_columns(REQ)
    left, right = out.split("\n\n", 1)
    assert "Сахаэнерго" in left and "1435045660" in left and "1435000000" not in left
    assert "Пилот" in right and "1435000000" in right and "Сахаэнерго" not in right
    assert split_columns("Пункт 1.   Цена   100 руб.") == "Пункт 1.   Цена   100 руб."  # одна строка — не колонки
    table = "1   Бетон В25   м3   10\n2   Арматура А500   т   2\n3   Опалубка   м2   40"
    assert split_columns(table) == table  # таблица остаётся строками


def test_contract_kind_by_title():
    assert contract_kind("ДОГОВОР ПОДРЯДА № 1\nпо проектной документации") == "general"
    assert contract_kind("Договор поставки № 5") == "supply"
    assert contract_kind("ДОГОВОР № 12 на выполнение проектных и изыскательских работ") == "design"
    assert contract_kind("Договор субподряда № 3") == "subcontract"


def test_contract_card_has_parties_from_requisites():
    text = "ДОГОВОР ПОДРЯДА № 7 от 01.02.2024\nг. Якутск\n1. Подрядчик обязуется выполнить работы по ремонту кровли.\n" \
           + "текст " * 200 + "\n" + split_columns(REQ)
    c = rules_card(text, "contract")
    assert {"АО «Сахаэнерго»", "ООО «Пилот»"} <= set(c.parties)


def test_deflection_and_text_tool_call():
    assert deflects("Для получения реквизитов необходимо уточнить номер договора")
    assert deflects("Предоставьте, пожалуйста, договор")
    assert not deflects("ИНН заказчика — 1435045660 [1]")
    call = text_tool_call('create_aosr_draft {"work": "бетонирование"}', {"create_aosr_draft": object()})
    assert call is not None and call.arguments == {"work": "бетонирование"}
    assert text_tool_call("просто текст {без json}", {"x": object()}) is None


def test_resolver_single_and_ambiguous(db, demo):
    from bis.agents.resolve import obj_row
    from bis.api.deps import agent_context
    from bis.models import ConstructionObject, User

    user = db.query(User).filter(User.organization_id == demo.id).first()
    ctx = agent_context(db, user)
    objs = db.query(ConstructionObject).filter(ConstructionObject.organization_id == demo.id).all()
    if len(objs) == 1:
        assert obj_row(ctx, None).id == objs[0].id
    else:
        with pytest.raises(LookupError, match="Несколько"):
            obj_row(ctx, None)
    assert obj_row(ctx, objs[0].name).id == objs[0].id


def test_claimed_action_only_for_requests():
    assert claims_action("Черновик АОСР создан.", "сделай АОСР на армирование")
    assert not claims_action("Письмо № 51 отправлено 05.05.2026.", "отправлено ли письмо 51?")
    assert deflects("р/с [не указан]")


def test_contract_parties_link_counterparty_and_object(db, demo):
    from bis.brain.parties import link_contract, parse_parties
    from bis.models import ConstructionObject, Contract

    parties = parse_parties(split_columns(REQ))
    assert [p["inn"] for p in parties] == ["1435045660", "1435000000"]
    obj = ConstructionObject(organization_id=demo.id, name="ПС Хандыга")
    db.add(obj)
    db.flush()
    c = Contract(organization_id=demo.id, kind="general", our_role="contractor", number="1", object_id=obj.id)
    db.add(c)
    db.flush()
    out = link_contract(db, c, split_columns(REQ))
    assert out["counterparty"] in ("АО «Сахаэнерго»", "ООО «Пилот»") and c.counterparty_id is not None
    assert obj.developer_id == c.counterparty_id or out["counterparty"] == "ООО «Пилот»"
