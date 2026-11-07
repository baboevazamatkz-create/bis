"""Генераторы документов: встроенные формы и шаблоны организаций."""

from bis.documents.aosr import AOSRData, render_aosr, validate_aosr
from bis.documents.letter import LetterData, render_letter

DOCUMENT_KINDS = {
    "aosr": "Акт освидетельствования скрытых работ",
    "aook": "Акт освидетельствования ответственных конструкций",
    "id_registry": "Реестр исполнительной документации",
    "work_journal": "Журнал выполнения работ",
    "test_protocol": "Протокол испытаний",
    "certificate": "Документ о качестве",
    "letter": "Исходящее письмо",
    "incoming_letter": "Входящее письмо",
    "order": "Приказ",
    "contract": "Договор",
    "ks2": "Акт о приёмке выполненных работ (КС-2)",
    "ks3": "Справка о стоимости работ (КС-3)",
    "m29": "Отчёт о расходе материалов (М-29)",
    "writeoff": "Акт списания материалов",
    "scheme": "Исполнительная схема",
    "project_doc": "Проектная / рабочая документация",
    "remarks": "Ведомость замечаний",
    "photo": "Фото с объекта",
    "survey": "Геодезическая съёмка",
    "supply_doc": "Накладная (УПД, ТОРГ-12, ТТН)",
    "other": "Прочее",
}

__all__ = ["DOCUMENT_KINDS", "AOSRData", "LetterData", "render_aosr", "render_letter", "validate_aosr"]
