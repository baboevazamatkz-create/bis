"""Regression cases for silent extraction loss; no external OCR or language model calls."""
import io
from types import SimpleNamespace

from docx import Document as Word
from docx.oxml import OxmlElement
from PIL import Image

from bis.rag.parsers import Page, parse_docx
from bis.rag.reading import reading_report
from bis.services import archive


def word_bytes(word):
    buffer = io.BytesIO()
    word.save(buffer)
    return buffer.getvalue()


def mixed_word():
    word = Word()
    word.add_paragraph('Рабочая документация. Шифр ЭМ-123. Участок 123. Общие указания по прокладке кабеля.')
    image = io.BytesIO()
    Image.new('RGB', (200, 100), 'white').save(image, format='PNG')
    word.add_picture(io.BytesIO(image.getvalue()))
    return word_bytes(word)


def test_content_controls_and_inserted_text_are_read_in_document_order():
    word = Word()
    word.add_paragraph('Начало договора')
    paragraph = word.add_paragraph('3. Цена договора: ')
    inserted = OxmlElement('w:ins')
    run, value = OxmlElement('w:r'), OxmlElement('w:t')
    value.text = '40 485 945 рублей'
    run.append(value)
    inserted.append(run)
    paragraph._p.append(inserted)
    control, content = OxmlElement('w:sdt'), OxmlElement('w:sdtContent')
    nested = word.add_paragraph('4. Аванс составляет 30 процентов.')
    content.append(nested._p)
    control.append(content)
    word.element.body.insert(2, control)
    parsed = parse_docx(word_bytes(word))
    assert '3. Цена договора: 40 485 945 рублей' in parsed.text
    assert '4. Аванс составляет 30 процентов.' in parsed.text
    assert parsed.text.index('Цена договора') < parsed.text.index('Аванс')
    assert parsed.meta['tracked_changes'] and not reading_report(parsed.pages, 'parsed')['complete']


def test_deleted_and_hidden_amounts_are_not_current_contract_facts():
    word = Word()
    paragraph = word.add_paragraph('Цена договора: 200 рублей')
    deleted, run, value = OxmlElement('w:del'), OxmlElement('w:r'), OxmlElement('w:t')
    value.text = 'Старая цена: 100 рублей'
    run.append(value)
    deleted.append(run)
    paragraph._p.append(deleted)
    paragraph.add_run('Скрытая цена: 999 рублей').font.hidden = True
    parsed = parse_docx(word_bytes(word))
    assert '200 рублей' in parsed.text and '100 рублей' not in parsed.text and '999 рублей' not in parsed.text


def test_merged_cells_do_not_multiply_amounts():
    word = Word()
    table = word.add_table(rows=2, cols=3)
    table.cell(0, 0).text = 'Итого'
    table.cell(0, 1).merge(table.cell(0, 2)).text = '40 485 945'
    table.cell(1, 0).text = 'Аванс'
    table.cell(1, 1).text = '30 %'
    parsed = parse_docx(word_bytes(word))
    assert parsed.text.count('40 485 945') == 1
    assert 'Итого | 40 485 945 | ' in parsed.text


def test_mixed_docx_recognizes_images_even_when_body_is_readable(monkeypatch):
    seen = []

    def recognize(data, mime):
        seen.append(mime)
        return ['Спецификация: Кабель ВВГнг-LS 3х2,5 | м | 125']

    monkeypatch.setattr(archive, '_ocr_pages', recognize)
    pages, method = archive._pages(mixed_word(), 'project.docx', None)
    text = '\n'.join(page.text for page in pages)
    assert 'Общие указания' in text and '125' in text
    assert seen == ['image/png'] and method == 'parsed+ocr_docx'
    assert reading_report(pages, method)['complete']


def test_mixed_docx_without_ocr_keeps_text_and_reports_missing_content(monkeypatch):
    monkeypatch.setattr(archive, '_ocr_pages', lambda *args: [])
    pages, method = archive._pages(mixed_word(), 'project.docx', None)
    report = reading_report(pages, method)
    assert 'Общие указания' in '\n'.join(page.text for page in pages)
    assert not report['complete'] and report['unread_segments'] and report['warnings']


def test_native_docx_parser_does_not_claim_images_have_been_read():
    parsed = parse_docx(mixed_word())
    assert len(parsed.meta['embedded_images']) == 1
    assert not reading_report(parsed.pages, 'parsed')['complete']


def test_blank_unnumbered_segments_and_garbled_text_are_explicitly_incomplete():
    assert not reading_report([Page(None, 'Читаемое содержание'), Page(None, '')], 'parsed')['complete']
    result = reading_report([Page(None, '\ufffd' * 100)], 'parsed')
    assert not result['complete'] and result['low_quality_segments'] == [1]


def test_project_native_source_preserves_extraction_warning():
    from bis.services.project_reading import page_source
    count, get_page = page_source(mixed_word(), SimpleNamespace(file_name='project.docx', mime=None))
    assert count == 1 and get_page(0)['extraction_warnings']


def test_one_broken_embedded_image_does_not_erase_typed_text(monkeypatch):
    def broken(*args):
        raise OSError('unsupported image encoding')
    monkeypatch.setattr(archive, '_ocr_pages', broken)
    pages, method = archive._pages(mixed_word(), 'project.docx', None)
    report = reading_report(pages, method)
    assert 'Общие указания' in '\n'.join(page.text for page in pages)
    assert not report['complete'] and 'OSError' in ' '.join(report['warnings'])


def test_empty_excel_sheet_is_incomplete_even_without_a_physical_page_number():
    from openpyxl import Workbook

    from bis.rag.parsers import parse_xlsx
    book = Workbook()
    buffer = io.BytesIO()
    book.save(buffer)
    parsed = parse_xlsx(buffer.getvalue())
    assert not reading_report(parsed.pages, 'parsed')['complete']


def test_explicitly_visible_word_run_is_not_mistaken_for_hidden_text():
    word = Word()
    paragraph = word.add_paragraph('Цена: ')
    paragraph.add_run('40 485 945 рублей').font.hidden = False
    assert '40 485 945 рублей' in parse_docx(word_bytes(word)).text


def test_project_pdf_falls_back_when_layout_extractor_fails(monkeypatch):
    from pypdf._page import PageObject
    from test_project_reading import pdf_bytes

    from bis.services.project_reading import page_source
    original = PageObject.extract_text

    def extract(self, *args, **kwargs):
        if kwargs.get('extraction_mode') == 'layout':
            raise ValueError('layout extraction unavailable')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PageObject, 'extract_text', extract)
    total, get_page = page_source(pdf_bytes(['Шифр РД: ЭМ-123. Участок 123. Прокладка кабеля по проекту.']),
                                 SimpleNamespace(file_name='project.pdf', mime='application/pdf'))
    assert total == 1 and 'ЭМ-123' in get_page(0)['text']
    assert get_page(0)['physical_page'] == 1 and get_page(0)['origin'] == 'pdf_native'
