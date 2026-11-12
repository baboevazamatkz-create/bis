"""Bound upload reads before parsers allocate memory for an entire document."""
from fastapi import HTTPException, UploadFile


def read_upload(file: UploadFile, limit: int = 100 * 1024 * 1024) -> bytes:
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f'Файл превышает ограничение {limit // (1024 * 1024)} МБ')
    return data
