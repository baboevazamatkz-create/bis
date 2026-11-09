"""Portable, clearly synthetic PTO archive for acceptance checks; never modifies application data."""
from __future__ import annotations

import json
from pathlib import Path

BASE = {
    'project_doc': ('РД участка 123', 'Рабочая документация. Участок 123, прокладка кабеля.\nШифр РД: УЧ-ЭМ-123, лист 7\nМатериалы: кабель ВВГнг-LS 3х2,5\nНа скрытую прокладку оформить АОСР.\nДокументы качества: паспорт\nИспытания: измерение сопротивления изоляции\nИсполнительная схема: схема прокладки кабеля\nПоследующие работы: обратная засыпка траншеи'),
    'work_journal': ('Общий журнал работ участка 123', '01.11.2026 участок 123 начата прокладка кабеля ВВГнг-LS 3х2,5, партия П-17.\n05.11.2026 участок 123 завершена прокладка кабеля ВВГнг-LS 3х2,5, партия П-17.'),
    'certificate': ('Паспорт П-17', 'Паспорт № ПК-17 на кабель ВВГнг-LS 3х2,5; партия П-17; выдан 01.10.2026; действителен до 31.12.2026.'),
    'scheme': ('Исполнительная схема участка 123', '05.11.2026 участок 123. Исполнительная схема прокладки кабеля ВВГнг-LS 3х2,5.'),
    'test_protocol': ('Протокол испытаний участка 123', '05.11.2026 участок 123. Измерение сопротивления изоляции кабеля ВВГнг-LS 3х2,5. Результаты соответствуют.'),
}
CASES = {
    'complete': ('Полный комплект', []),
    'missing_protocol': ('Нет протокола', ['protocol:']),
    'wrong_batch': ('Паспорт другой партии', ['quality:']),
    'wrong_material': ('Испытана другая марка', ['protocol_material:']),
    'conflicting_dates': ('Второй журнал с другой датой', ['conflict:work_end']),
    'conditional_rule': ('Условное требование библиотеки', ['rule_condition:']),
    'old_rd': ('Новая редакция РД после сборки', ['source_changed']),
}


def generate(output: Path):
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError('Папка должна быть пустой: существующие документы не перезаписываются')
    output.mkdir(parents=True, exist_ok=True)
    for case, (description, expected) in CASES.items():
        folder = output / case
        project = folder / 'project'
        project.mkdir(parents=True)
        docs = []
        for kind, (title, text) in BASE.items():
            if case == 'missing_protocol' and kind == 'test_protocol':
                continue
            if case == 'wrong_batch' and kind == 'certificate':
                text = text.replace('П-17', 'П-99')
            if case == 'wrong_material' and kind == 'test_protocol':
                text = text.replace('3х2,5', '3х25')
            name = f'{kind}.txt'
            (project / name).write_text(text, encoding='utf-8')
            docs.append({'kind': kind, 'title': title, 'file': 'project/' + name})
        if case == 'conflicting_dates':
            (project / 'second_journal.txt').write_text('06.11.2026 участок 123 завершена прокладка кабеля ВВГнг-LS 3х2,5, партия П-17.', encoding='utf-8')
            docs.append({'kind': 'work_journal', 'title': 'Журнал подрядчика участка 123', 'file': 'project/second_journal.txt'})
        knowledge = folder / 'knowledge'
        knowledge.mkdir()
        text = 'УЧЕБНЫЙ РЕГЛАМЕНТ ПТО — СИНТЕТИЧЕСКИЙ ПРИМЕР, НЕ НОРМАТИВНЫЙ АКТ.\nПрокладка кабеля.\nДокументы качества: паспорт\nИспытания: измерение сопротивления изоляции'
        if case == 'conditional_rule':
            text += '\nИспытания: если участок относится к особой зоне, требуется дополнительное измерение сопротивления изоляции кабеля'
        (knowledge / 'Учебный регламент кабеля.txt').write_text(text, encoding='utf-8')
        manifest = {'synthetic': True, 'case': case, 'description': description, 'documents': docs,
            'report': 'Проложен кабель на участке 123.', 'expected_blocker_prefixes': expected,
            'after_build': 'Добавить РД УЧ-ЭМ-123, лист 7, изменение 2 от 04.11.2026 с card.rd_code, card.rd_sheet и card.revision.' if case == 'old_rd' else None}
        (folder / 'scenario.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'README.md').write_text('# Учебный объект ПТО\n\nВсе данные синтетические. Учебный регламент не является действующим нормативом.\n\nКаждый сценарий загружайте в отдельный тестовый проект. `scenario.json` задаёт виды документов, сообщение и ожидаемые препятствия. Папка `knowledge` подключается как отдельная тестовая библиотека; её редакцию нужно подтвердить. Не подключайте учебные требования к рабочим проектам.\n\nВ `old_rd` сначала соберите комплект, затем добавьте новую редакцию по инструкции `after_build` и повторите проверку актуальности.\n\nДля строгого режима дополнительно подключите вашу подтверждённую форму акта. Образцы не заменяют проверку на реальном архиве.\n', encoding='utf-8')
    return {'output': str(output), 'scenarios': list(CASES), 'synthetic': True}
