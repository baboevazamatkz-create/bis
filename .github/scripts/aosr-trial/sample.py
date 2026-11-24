"""Generate the synthetic form documented in the DOCX AOSR tests."""
import ast
import io
import sys
from pathlib import Path

from docx import Document

from bis.services import library_forms

source, downloads = map(Path, sys.argv[1:])
module = ast.parse((source / 'backend/tests/test_libraries.py').read_text())
names = {'_aosr_precision_doc', '_filled_aosr_example'}
fixtures = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in names]
assert {node.name for node in fixtures} == names
scope = {'Word': Document, 'forms': library_forms, 'io': io}
exec(compile(ast.Module(body=fixtures, type_ignores=[]), str(source), 'exec'), scope)
examples = downloads / 'BIS-AOSR'
examples.mkdir(parents=True)
(examples / 'АОСР — две даты в строке.docx').write_bytes(scope['_aosr_precision_doc']('inline'))
(examples / 'АОСР — пустая ячейка.docx').write_bytes(scope['_aosr_precision_doc']('table'))
(examples / 'АОСР — заполненный пример.docx').write_bytes(scope['_filled_aosr_example']())
