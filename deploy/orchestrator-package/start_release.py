"""Start the staged program with the installation's existing Python executable."""

import sys
from pathlib import Path

packages = Path(__file__).resolve().parent / "packages"
sys.path.insert(0, str(packages))

if sys.argv[1:] == ["--verify"]:
    import os
    import secrets

    for key in tuple(os.environ):
        if key.startswith("BIS_"):
            del os.environ[key]
    os.environ["BIS_ENV"] = "test"
    os.environ["BIS_SECRET_KEY"] = secrets.token_hex(32)
    from bis.agents.executive_drafts import run
    from bis.main import app

    assert run and any(route.path == "/api/chat/stream" for route in app.routes)
else:
    from bis.cli import main

    raise SystemExit(main(sys.argv[1:]))
