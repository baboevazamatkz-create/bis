"""Голосовое управление: распознавание, голосовая команда (→ агенты), озвучивание ответа."""

import os
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.orm import Session

from bis import voice
from bis.api.chat import ChatIn, chat
from bis.api.deps import require
from bis.db import get_db
from bis.models import User

router = APIRouter(prefix="/voice", tags=["voice"])
MAX_AUDIO = 25 * 1024 * 1024


@router.get("/status")
def status(user: User = Depends(require("knowledge:read"))):
    return voice.status()


class VoiceSetup(BaseModel):
    provider: Literal["openai", "yandex"]
    api_key: SecretStr
    folder_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$", max_length=100)


@router.post("/setup")
def setup(body: VoiceSetup, user: User = Depends(require("settings:write")), db: Session = Depends(get_db)):
    from bis.services.local_config import LOCK, save

    db.close()
    home = os.environ.get("BIS_LOCAL_DIR")
    if not home or not (Path(home) / ".env").is_file():
        raise HTTPException(409, "Настройка распознавания через интерфейс доступна в локальном BIS-Pilot")
    key = body.api_key.get_secret_value().strip()
    if not re.fullmatch(r"[A-Za-z0-9_.~+/-]{1,512}={0,2}", key):
        raise HTTPException(422, "Проверьте формат API-ключа для распознавания")
    if body.provider == "yandex" and not body.folder_id:
        raise HTTPException(422, "Укажите Folder ID для SpeechKit")
    values = {"BIS_STT_PROVIDER": body.provider}
    if body.provider == "openai":
        values.update(BIS_STT_URL=voice.OPENAI_STT_URL, BIS_STT_MODEL=voice.OPENAI_STT_MODEL, BIS_STT_API_KEY=key)
    else:
        values.update(BIS_STT_YANDEX_FOLDER_ID=body.folder_id, BIS_STT_YANDEX_API_KEY=key)
    with LOCK:
        save(values)
        result = voice.status()
    return {**result, "message": "Настройки распознавания сохранены." if result["stt"]
            else "Настройки сохранены. Для запуска SpeechKit установите FFmpeg."}


def _read(file: UploadFile) -> bytes:
    data = file.file.read(MAX_AUDIO + 1)
    if len(data) > MAX_AUDIO:
        raise HTTPException(413, "Аудио больше 25 МБ")
    return data


@router.post("/transcribe")
def transcribe(file: UploadFile = File(...), user: User = Depends(require("knowledge:read")),
               db: Session = Depends(get_db)):
    db.close()
    try:
        text = voice.transcribe(_read(file), file.filename or "audio.webm", file.content_type)
    except voice.VoiceError as e:
        raise HTTPException(503, str(e)) from e
    return {"text": text}


@router.post("/command")
def command(file: UploadFile = File(...), session_id: str | None = Form(None), agent: str | None = Form(None),
                  db: Session = Depends(get_db), user: User = Depends(require("knowledge:read"))):
    """Голосовая команда: распознать и сразу передать агентам."""
    db.close()  # release authentication connection during external STT; the session can reopen for chat
    try:
        text = voice.transcribe(_read(file), file.filename or "audio.webm", file.content_type)
    except voice.VoiceError as e:
        raise HTTPException(503, str(e)) from e
    if not text.strip():
        raise HTTPException(422, "Речь не распознана")
    reply = chat(ChatIn(message=text, session_id=session_id or None, agent=agent or None), db=db, user=user)
    return {"transcript": text, **reply}


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/speak")
def speak(body: SpeakIn, user: User = Depends(require("knowledge:read")), db: Session = Depends(get_db)):
    db.close()
    try:
        audio, mime = voice.speak(body.text)
    except voice.VoiceError as e:
        raise HTTPException(503, str(e)) from e
    return Response(content=audio, media_type=mime)
