"""Write explicitly requested settings for the local pilot, preserving other values."""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

LOCK = threading.RLock()


def save(values):
    home = os.environ.get('BIS_LOCAL_DIR')
    if not home:
        raise PermissionError('Изменение локальных настроек доступно только в BIS-Pilot')
    path = Path(home) / '.env'
    if not path.is_file():
        raise ValueError('Не найден файл настроек BIS-Pilot')
    with LOCK:
        lines = [line for line in path.read_text(encoding='utf-8-sig').splitlines()
                 if line.split('=', 1)[0].strip() not in values]
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
            output.write('\n'.join([*lines, *(f'{k}={v}' for k, v in values.items())]) + '\n')
            output.flush()
            os.fsync(output.fileno())
            temporary = output.name
        try:
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        os.environ.update(values)
        from bis.config import get_settings

        get_settings.cache_clear()
