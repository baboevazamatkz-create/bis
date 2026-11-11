"""Read Word 97–2003 piece tables without Microsoft Office or executing macros."""
import io
import re
import struct

import olefile
from striprtf.striprtf import rtf_to_text


def read_word(data: bytes) -> str:
    if data.lstrip().startswith(b'{\\rtf'):
        return rtf_to_text(data.decode('cp1251'), encoding='cp1251')
    if not olefile.isOleFile(io.BytesIO(data)):
        raise ValueError('Файл DOC не является документом Word 97–2003 или RTF. Сохраните его в DOCX.')
    with olefile.OleFileIO(io.BytesIO(data)) as package:
        word = package.openstream('WordDocument').read()
        return read_pieces(word, package)


def read_pieces(word, package):
    def u16(offset):
        return struct.unpack_from('<H', word, offset)[0]

    def u32(offset):
        return struct.unpack_from('<I', word, offset)[0]

    if u16(0) != 0xA5EC or u16(2) < 0xC1:
        raise ValueError('Версия DOC старше Word 97; сохраните документ в DOCX.')
    flags = u16(10)
    if flags & 0x8100:
        raise ValueError('DOC защищён паролем; загрузите незашифрованную копию.')
    table = package.openstream('1Table' if flags & 0x200 else '0Table').read()
    # FibRgFcLcb follows variable-length FibRgW and FibRgLw; fcClx is pair 33.
    pos = 32
    pos += 2 + u16(pos) * 2
    pos += 2 + u16(pos) * 4
    if u16(pos) <= 33:
        raise ValueError('В DOC отсутствует таблица текста.')
    offset = pos + 2 + 33 * 8
    start, length = u32(offset), u32(offset + 4)
    clx = table[start:start + length]
    i = 0
    while i < len(clx) and clx[i] == 1:
        i += 3 + struct.unpack_from('<H', clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 2:
        raise ValueError('Повреждена таблица фрагментов DOC.')
    size = struct.unpack_from('<I', clx, i + 1)[0]
    plc = clx[i + 5:i + 5 + size]
    if len(plc) != size or size < 4 or (size - 4) % 12:
        raise ValueError('Неполная таблица текста DOC.')
    count = (size - 4) // 12
    cp = struct.unpack_from('<' + 'I' * (count + 1), plc)
    parts = []
    for j in range(count):
        chars = cp[j + 1] - cp[j]
        if chars < 0:
            raise ValueError('Повреждён порядок фрагментов DOC.')
        fc = struct.unpack_from('<I', plc, (count + 1) * 4 + j * 8 + 2)[0]
        compressed = bool(fc & 0x40000000)
        start = fc & 0x3fffffff
        if compressed:
            start //= 2
        size = chars * (1 if compressed else 2)
        raw = word[start:start + size]
        if len(raw) != size:
            raise ValueError('Обнаружен обрезанный фрагмент DOC.')
        parts.append(raw.decode('cp1252' if compressed else 'utf-16-le', errors='replace'))
    text = ''.join(parts).replace('\r', '\n').replace('\x07', ' | ').replace('\x0b', '\n').replace('\x0c', '\n')
    return re.sub(r'[\x00-\x08\x0e-\x1f]', '', text).strip()
