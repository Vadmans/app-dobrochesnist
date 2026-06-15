from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import require_admin
from database import get_db
from firebase_service import FIREBASE_AVAILABLE, init_firebase, send_to_tokens
from models import AdminUser, Device
from schemas import PushIn

router = APIRouter()

@router.post("/push/send")
def send_push(data: PushIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    if not data.title.strip() or not data.body.strip():
        raise HTTPException(400, "Заголовок і текст обов'язкові")
    if not FIREBASE_AVAILABLE:
        return {"ok": False, "message": "Firebase не встановлено"}
    if not init_firebase():
        return {"ok": False, "message": "Firebase не налаштовано"}
    tokens = [d.token for d in db.query(Device).all() if d.token]
    if not tokens:
        return {"ok": False, "message": "Немає пристроїв"}
    sent, total = send_to_tokens(db, tokens, data.title.strip(), data.body.strip())
    return {"ok": True, "sent": sent, "total": total}


