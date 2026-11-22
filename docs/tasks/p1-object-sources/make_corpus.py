"""Build the synthetic BIS P1 source files in a fresh output directory."""
from argparse import ArgumentParser
from pathlib import Path

from openpyxl import Workbook
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def write_pdf(path: Path, pages: list[list[str] | Image.Image]) -> None:
    pdf = canvas.Canvas(str(path), invariant=True)
    for page in pages:
        if isinstance(page, Image.Image):
            pdf.drawImage(ImageReader(page), 40, 700, width=500, height=90)
        else:
            for index, line in enumerate(page):
                pdf.drawString(40, 790 - 18 * index, line)
        pdf.showPage()
    pdf.save()


def scan(text: str) -> Image.Image:
    image = Image.new("RGB", (550, 100), "white")
    ImageDraw.Draw(image).text((20, 40), text, fill="black")
    return image


def write_drawing(path: Path, quantity: int, marker: str) -> None:
    write_pdf(path, [
        ["Working drawings for synthetic construction training, electrical discipline.",
         "Physical page 1: cover sheet and purposes of the training file."],
        [f"RD-TRAIN-17 | SHEET 7 | KLAB471 | Cable line K-1: {quantity} m.",
         f"Installation in grid A-2. Evidence marker: {marker}."],
    ])


def write_specification(path: Path) -> None:
    book = Workbook()
    book.active.title = "Ведомость"
    book.active.append(["Поз.", "Наименование", "Марка", "Ед.", "Кол-во"])
    book.active.append([1, "Гофра монтажная", "TRAINCONDUIT", "м", 120])
    sheet = book.create_sheet("Кабели")
    sheet.append(["Поз.", "Наименование", "Марка", "Ед.", "Кол-во"])
    sheet.append([2, "Кабель силовой", "SPECLAB082", "м", 120])
    book.save(path)
    book.close()


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New directory for objects A and B")
    output = parser.parse_args().output
    output.mkdir()
    object_a = output / "A"
    object_b = output / "B"
    object_a.mkdir()
    object_b.mkdir()
    write_drawing(object_a / "RD-TRAIN-17.pdf", 120, "DRAWINGOBJECTA")
    write_drawing(object_b / "RD-TRAIN-17.pdf", 375, "DRAWINGOBJECTB")
    write_specification(object_a / "Specification.xlsx")
    write_pdf(object_a / "Quality.pdf", [
        ["Training quality certificate for incoming cable material.",
         "Batch QUALITYLAB309 was delivered for line K-1 in grid A-2."],
        scan("SCANNEDLAB701: image-only annex with a training inspection value."),
    ])
    write_pdf(object_a / "Scan-only.pdf", [
        scan("UNREADLAB884: image-only training document."),
    ])


if __name__ == "__main__":
    main()
