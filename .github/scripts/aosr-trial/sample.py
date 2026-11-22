"""Generate the synthetic form documented in the DOCX AOSR tests."""
import ast
import io
import sys
from pathlib import Path

from docx import Document

from bis.services import library_forms

source, downloads = map(Path, sys.argv[1:])
module = ast.parse((source / 'backend/tests/test_libraries.py').read_text())
fixture = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == '_aosr_training_doc')
scope = {'Word': Document, 'forms': library_forms, 'io': io}
exec(compile(ast.Module(body=[fixture], type_ignores=[]), str(source), 'exec'), scope)
examples = downloads / 'BIS-AOSR'
examples.mkdir(parents=True)
(examples / 'Учебный-бланк-АОСР.docx').write_bytes(scope[fixture.name](ambiguous=True))
