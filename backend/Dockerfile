FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIS_STORAGE_DIR=/data/files
WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY bis ./bis
RUN pip install --no-cache-dir ".[anthropic]" && useradd --create-home bis && mkdir -p /data/files && chown -R bis /data

USER bis
EXPOSE 8000
CMD ["sh", "-c", "bis migrate && uvicorn bis.main:app --host 0.0.0.0 --port 8000 --workers 2"]
