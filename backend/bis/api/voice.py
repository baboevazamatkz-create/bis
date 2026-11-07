"""Голосовое управление: распознавание, голосовая команда (→ агенты), озвучивание ответа."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
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


async def _read(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_AUDIO:
        raise HTTPException(413, "Аудио больше 25 МБ")
    return data


@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...), user: User = Depends(require("knowledge:read"))):
    try:
        text = voice.transcribe(await _read(file), file.filename or "audio.webm", file.content_type)
    except voice.VoiceError as e:
        raise HTTPException(503, str(e)) from e
    return {"text": text}


@router.post("/command")
async def command(file: UploadFile = File(...), session_id: str | None = Form(None), agent: str | None = Form(None),
                  db: Session = Depends(get_db), user: User = Depends(require("knowledge:read"))):
    """Голосовая команда: распознать и сразу передать агентам."""
    try:
        text = voice.transcribe(await _read(file), file.filename or "audio.webm", file.content_type)
    except voice.VoiceError as e:
        raise HTTPException(503, str(e)) from e
    if not text.strip():
        raise HTTPException(422, "Речь не распознана")
    reply = chat(ChatIn(message=text, session_id=session_id or None, agent=agent or None), db=db, user=user)
    return {"transcript": text, **reply}


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/speak")
def speak(body: SpeakIn, user: User = Depends(require("knowledge:read"))):
    try:
        audio, mime = voice.speak(body.text)
    except voice.VoiceError as e:
        raise HTTPException(503, str(e)) from e
    return Response(content=audio, media_type=mime)
