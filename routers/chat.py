import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from auth import require_admin
from database import get_db
from firebase_service import send_to_tokens
from models import AdminUser, ChatMessage, Device
from schemas import ChatAnswerIn, ChatMessageOut, ChatQuestionIn
from utils import allow_rate, client_ip
from config import logger

router = APIRouter()

@router.post("/chat/question", response_model=ChatMessageOut, status_code=201)
def create_chat_question(data: ChatQuestionIn, request: Request, db: Session = Depends(get_db)):
    if not allow_rate(f"chatq:{client_ip(request)}", 10, 60):
        raise HTTPException(429, "Забагато запитань поспіль, зачекайте хвилину")
    client_id = (data.client_id or "").strip()
    question = (data.question or "").strip()
    if not client_id or not question:
        raise HTTPException(400, "client_id та питання обов'язкові")
    if len(question) > 4000:
        raise HTTPException(400, "Питання задовге")
    msg = ChatMessage(id=f"q{uuid.uuid4().hex[:12]}", client_id=client_id, question=question)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


@router.get("/chat/messages", response_model=List[ChatMessageOut])
def list_my_chat_messages(client_id: str, db: Session = Depends(get_db)):
    if not client_id:
        return []
    return db.query(ChatMessage).filter(ChatMessage.client_id == client_id).order_by(ChatMessage.created_at.desc()).all()


@router.get("/chat/admin", response_model=List[ChatMessageOut])
def list_chat_admin(admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(ChatMessage).order_by(ChatMessage.created_at.desc()).all()


@router.post("/chat/{message_id}/answer", response_model=ChatMessageOut)
def answer_chat_message(message_id: str, data: ChatAnswerIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    msg = db.get(ChatMessage, message_id)
    if not msg:
        raise HTTPException(404, "Питання не знайдено")
    answer = (data.answer or "").strip()
    if not answer:
        raise HTTPException(400, "Відповідь обов'язкова")
    msg.answer = answer
    msg.status = "answered"
    msg.answered_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    # Push-сповіщення лише на пристрої автора питання (за client_id).
    # Будь-який збій тут (немає колонки/Firebase/мережі) не повинен ламати збереження відповіді.
    try:
        asker_tokens = [d.token for d in db.query(Device).filter(Device.client_id == msg.client_id).all() if d.token]
        if asker_tokens:
            send_to_tokens(db, asker_tokens, "Відповідь у чаті", "На ваше питання надано відповідь", {"type": "chat_answer", "message_id": msg.id})
    except Exception as e:
        db.rollback()
        logger.warning("Push про відповідь не надіслано: %s", e)
    return msg


@router.delete("/chat/{message_id}", status_code=204)
def delete_chat_message(message_id: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    msg = db.get(ChatMessage, message_id)
    if not msg:
        raise HTTPException(404, "Повідомлення не знайдено")
    db.delete(msg)
    db.commit()
    return Response(status_code=204)


