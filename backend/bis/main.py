"""Точка входа сетевого API: uvicorn bis.main:app."""

from bis.app import create_app as create_app

app = create_app()
