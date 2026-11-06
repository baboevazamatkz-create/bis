from pathlib import Path

from bis.rag.chunking import GenericChunker, NormativeChunker, detect_designation, parse_reference
from bis.rag.parsers import Page

DEMO = (Path(__file__).parents[1] / "bis" / "demo" / "demo_sp.txt").read_text(encoding="utf-8")


def test_normative_chunker_clauses_and_paths():
    chunks = NormativeChunker().chunk([Page(1, DEMO)])
    by_clause = {c.clause: c for c in chunks if c.clause}
    assert "3.1.2" in by_clause
    assert by_clause["3.1.2"].section_path == "3 Земляные работы › 3.1 Обратная засыпка"
    assert by_clause["3.1.2"].content.startswith("3.1.2 Засыпку пазух")
    # заголовки разделов не становятся отдельными фрагментами
    assert "3.1" not in by_clause and "4" not in by_clause
    assert {"А.1", "А.4"} <= set(by_clause)
    assert by_clause["А.3"].section_path.startswith("Приложение А")
    assert chunks[0].heading == "Вводная часть"


def test_numbers_inside_text_are_not_clauses():
    text = "4 Бетонные работы\n4.1 Требования к бетону и смесям\nДопуск составляет\n1.5 м при длине пролёта\n4.2 Следующий пункт про арматуру"
    chunks = NormativeChunker().chunk([Page(None, text)])
    clauses = [c.clause for c in chunks if c.clause]
    assert "1.5" not in clauses
    assert clauses == ["4.1", "4.2"]


def test_long_clause_is_split():
    long = "5.1 " + "\n".join([f"Очень длинное требование к производству работ номер {i}." for i in range(80)])
    chunks = NormativeChunker(max_chars=800, part_chars=500).chunk([Page(None, "5 Раздел\n" + long)])
    parts = [c for c in chunks if c.clause == "5.1"]
    assert len(parts) > 1
    assert parts[1].heading == "п. 5.1 (продолжение)"


def test_designation_detection():
    assert detect_designation("СВОД ПРАВИЛ СП 70.13330.2012 Несущие") == "СП 70.13330.2012"
    assert detect_designation("ГОСТ Р 21.101 – 2020 Система проектной документации") == "ГОСТ Р 21.101-2020"
    assert detect_designation("Текст", "ГОСТ_10180-2012.pdf") == "ГОСТ 10180-2012"
    assert parse_reference("что в СП 48.13330.2019 п. 7.1.3?") == ("СП 48.13330.2019", "7.1.3")
    assert parse_reference("требования к засыпке") == (None, None)


def test_generic_chunker_overlap():
    text = "\n".join(f"Абзац {i}: " + "текст " * 40 for i in range(10))
    chunks = GenericChunker(max_chars=600).chunk([Page(3, text)])
    assert len(chunks) > 2
    assert all(c.page == 3 for c in chunks)
    first_last = chunks[0].content.splitlines()[-1]
    assert chunks[1].content.startswith(first_last)
