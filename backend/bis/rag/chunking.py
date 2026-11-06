"""Разбиение текста на фрагменты.

NormativeChunker — для СП, ГОСТ, СНиП, приказов: фрагмент = пункт документа (5.16.3, А.2),
с иерархией разделов. Это позволяет цитировать точно: «СП 70.13330.2012, п. 5.16.3».
GenericChunker — для писем, договоров, прочих документов: абзацы ~1000 символов с перекрытием.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bis.rag.parsers import Page

DESIGNATION_PATTERNS = [
    r"СП\s+\d{1,3}(?:\.\d{1,6}){1,2}(?:\.\d{4})?",
    r"ГОСТ(?:\s+Р)?(?:\s+(?:ИСО|ISO|МЭК|IEC|EN))?\s+\d+(?:\.\d+)*\s*[-–—]\s*\d{2,4}",
    r"СНиП\s+\d+(?:[.\-–]\d+)+",
    r"СанПиН\s+\d+(?:[.\-–]\d+)+",
    r"ВСН\s+\d+(?:[.\-–]\d+)+",
    r"РД[-\s]\d+(?:[.\-–]\d+)+",
    r"№\s*\d+-ФЗ",
    r"(?:Приказ|Постановление)[^\n№]{0,80}№\s*\d+(?:/\w+)?",
]
_DESIGNATION_RE = re.compile("|".join(f"(?:{p})" for p in DESIGNATION_PATTERNS))

_CLAUSE_RE = re.compile(r"^\s*(?P<num>[1-9]\d?(?:\.\d{1,3}){1,5})\.?\s+(?P<rest>\S.*)$")
_SECTION_RE = re.compile(r"^\s*(?P<num>[1-9]\d?)\.?\s+(?P<rest>[А-ЯЁA-Z][^\n]{2,150})$")
_APPENDIX_RE = re.compile(r"^\s*Приложение\s+(?P<num>[А-ЯЁ])\b\.?\s*(?P<rest>.*)$")
_APP_CLAUSE_RE = re.compile(r"^\s*(?P<num>[А-ЯЁ]\.\d{1,3}(?:\.\d{1,3}){0,4})\.?\s+(?P<rest>\S.*)$")
_REF_CLAUSE_RE = re.compile(r"(?:п\.|пункт\w*|подпункт\w*|пп\.)\s*(?P<num>(?:[А-ЯЁ]\.)?\d+(?:\.\d+)*)", re.IGNORECASE)


def normalize_designation(text: str) -> str:
    text = re.sub(r"\s*[–—]\s*", "-", text)
    text = re.sub(r"\s*-\s*(?=\d)", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def detect_designation(text: str, filename: str | None = None) -> str | None:
    head = text[:3000]
    m = _DESIGNATION_RE.search(head)
    if m:
        return normalize_designation(m.group(0))
    if filename:
        m = _DESIGNATION_RE.search(filename.replace("_", " "))
        if m:
            return normalize_designation(m.group(0))
    return None


def parse_reference(query: str) -> tuple[str | None, str | None]:
    """Ссылка вида «СП 70.13330.2012 п. 5.16.3» → (обозначение, пункт)."""
    m = _DESIGNATION_RE.search(query)
    designation = normalize_designation(m.group(0)) if m else None
    c = _REF_CLAUSE_RE.search(query)
    return designation, (c.group("num") if c else None)


@dataclass
class ChunkData:
    ord: int
    content: str
    clause: str | None = None
    section_path: str | None = None
    heading: str | None = None
    page: int | None = None


@dataclass
class _Unit:
    num: str | None
    kind: str  # clause | section | appendix | preamble
    lines: list[str]
    page: int | None

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


def _num_key(num: str) -> tuple[int, ...]:
    return tuple(int(p) for p in num.split("."))


def _looks_like_heading(text: str) -> bool:
    return len(text) < 160 and not text.rstrip().endswith((".", ";", ":", ",")) and "\n" not in text.strip()


class NormativeChunker:
    def __init__(self, max_chars: int = 1800, part_chars: int = 1200):
        self.max_chars = max_chars
        self.part_chars = part_chars

    def _units(self, pages: list[Page]) -> list[_Unit]:
        units: list[_Unit] = [_Unit(None, "preamble", [], pages[0].number if pages else None)]
        last_clause: tuple[int, ...] = ()
        last_section = 0
        in_appendix = False
        for page in pages:
            for raw in page.text.splitlines():
                line = raw.rstrip()
                if not line.strip():
                    continue
                m = _APPENDIX_RE.match(line)
                if m and len(line) < 200:
                    in_appendix = True
                    units.append(_Unit(m.group("num"), "appendix", [line.strip()], page.number))
                    continue
                if in_appendix:
                    m = _APP_CLAUSE_RE.match(line)
                    if m and len(m.group("rest")) >= 3:
                        units.append(_Unit(m.group("num"), "clause", [line.strip()], page.number))
                        continue
                else:
                    m = _CLAUSE_RE.match(line)
                    if m and len(m.group("rest")) >= 8 and m.group("rest")[0].isalpha():
                        key = _num_key(m.group("num"))
                        limit = 40 if last_section == 0 else last_section + 3  # фрагмент может начинаться не с раздела 1
                        if key > last_clause and key[0] <= limit:
                            last_clause = key
                            last_section = max(last_section, key[0])
                            units.append(_Unit(m.group("num"), "clause", [line.strip()], page.number))
                            continue
                    m = _SECTION_RE.match(line)
                    if m:
                        n = int(m.group("num"))
                        if last_section < n <= (40 if last_section == 0 else last_section + 3):
                            last_section = n
                            last_clause = (n,)
                            units.append(_Unit(m.group("num"), "section", [line.strip()], page.number))
                            continue
                units[-1].lines.append(line.strip())
        return [u for u in units if u.text]

    def chunk(self, pages: list[Page], designation: str | None = None) -> list[ChunkData]:
        units = self._units(pages)
        titles: dict[str, str] = {}
        heading_units: set[int] = set()
        for i, u in enumerate(units):
            if u.kind in ("section", "appendix") and _looks_like_heading(u.text):
                titles[u.num or ""] = u.text
                heading_units.add(i)
            elif u.kind == "clause" and u.num:
                nxt = units[i + 1] if i + 1 < len(units) else None
                if nxt and nxt.num and nxt.num.startswith(u.num + ".") and _looks_like_heading(u.text):
                    titles[u.num] = u.text
                    heading_units.add(i)

        chunks: list[ChunkData] = []
        for i, u in enumerate(units):
            if i in heading_units:
                continue
            path = self._path(u.num, titles) if u.num else None
            heading = titles.get(u.num or "") if u.kind != "clause" else None
            if u.kind == "preamble":
                for part in GenericChunker(self.part_chars).chunk([Page(u.page, u.text)]):
                    part.ord = len(chunks)
                    part.heading = "Вводная часть"
                    chunks.append(part)
                continue
            for j, part in enumerate(self._split(u.text)):
                chunks.append(
                    ChunkData(
                        ord=len(chunks),
                        content=part,
                        clause=u.num if u.kind == "clause" else None,
                        section_path=path,
                        heading=heading if j == 0 else (f"п. {u.num} (продолжение)" if u.num else None),
                        page=u.page,
                    )
                )
        return chunks

    @staticmethod
    def _path(num: str, titles: dict[str, str]) -> str | None:
        parts = num.split(".")
        chain = []
        for k in range(1, len(parts)):
            prefix = ".".join(parts[:k])
            if prefix in titles:
                chain.append(titles[prefix])
        return " › ".join(chain) or None

    def _split(self, text: str) -> list[str]:
        if len(text) <= self.max_chars:
            return [text]
        parts, cur = [], ""
        for line in text.split("\n"):
            if cur and len(cur) + len(line) > self.part_chars:
                parts.append(cur.strip())
                cur = ""
            cur += line + "\n"
        if cur.strip():
            parts.append(cur.strip())
        return parts


class GenericChunker:
    def __init__(self, max_chars: int = 1000, overlap_paragraphs: int = 1):
        self.max_chars = max_chars
        self.overlap = overlap_paragraphs

    def chunk(self, pages: list[Page]) -> list[ChunkData]:
        chunks: list[ChunkData] = []
        for page in pages:
            paras = [p.strip() for p in re.split(r"\n\s*\n|\n", page.text) if p.strip()]
            buf: list[str] = []
            size = 0
            for p in paras:
                if buf and size + len(p) > self.max_chars:
                    chunks.append(ChunkData(ord=len(chunks), content="\n".join(buf), page=page.number))
                    buf = buf[-self.overlap :] if self.overlap else []
                    size = sum(len(x) for x in buf)
                while len(p) > self.max_chars * 1.5:  # очень длинный абзац
                    chunks.append(ChunkData(ord=len(chunks), content=p[: self.max_chars], page=page.number))
                    p = p[self.max_chars :]
                buf.append(p)
                size += len(p)
            if buf:
                chunks.append(ChunkData(ord=len(chunks), content="\n".join(buf), page=page.number))
        return chunks
