import json
import os
from typing import List, Optional

from sqlalchemy.orm import Session

from config import logger
from models import Device

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    FIREBASE_AVAILABLE = True
except ImportError:
    firebase_admin = None
    credentials = None
    messaging = None
    FIREBASE_AVAILABLE = False

# Firebase
firebase_app = None

def init_firebase():
    global firebase_app
    if not FIREBASE_AVAILABLE:
        return None
    if firebase_app:
        return firebase_app
    if firebase_admin._apps:
        firebase_app = firebase_admin.get_app()
        return firebase_app
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    try:
        cred = credentials.Certificate(json.loads(raw))
        firebase_app = firebase_admin.initialize_app(cred)
        return firebase_app
    except Exception as e:
        logger.warning("Firebase init не вдалося: %s", e)
        return None


def _firebase_ready() -> bool:
    return FIREBASE_AVAILABLE and init_firebase() is not None


def send_to_tokens(db: Session, tokens: List[str], title: str, body: str, data: Optional[dict] = None):
    """Надсилає push пачками (до 500) і прибирає недійсні токени. Повертає (надіслано, всього)."""
    tokens = [t for t in dict.fromkeys(tokens) if t]  # унікальні, без порожніх
    if not _firebase_ready() or not tokens:
        return 0, len(tokens)
    payload = {k: str(v) for k, v in (data or {}).items()}
    notif = messaging.Notification(title=title, body=body)
    sent = 0
    dead: list = []
    multicast_fn = getattr(messaging, "send_each_for_multicast", None)
    for i in range(0, len(tokens), 500):
        chunk = tokens[i:i + 500]
        if multicast_fn:
            try:
                resp = multicast_fn(messaging.MulticastMessage(notification=notif, data=payload, tokens=chunk))
            except Exception as e:
                logger.warning("multicast помилка: %s", e)
                continue
            for idx, sr in enumerate(resp.responses):
                if sr.success:
                    sent += 1
                else:
                    name = type(sr.exception).__name__ if sr.exception else ""
                    if "Unregistered" in name or "SenderIdMismatch" in name or "InvalidArgument" in name:
                        dead.append(chunk[idx])
        else:
            for tok in chunk:
                try:
                    messaging.send(messaging.Message(notification=notif, data=payload, token=tok))
                    sent += 1
                except Exception as e:
                    if "Unregistered" in type(e).__name__ or "SenderIdMismatch" in type(e).__name__:
                        dead.append(tok)
    if dead:
        for tok in dead:
            obj = db.get(Device, tok)
            if obj:
                db.delete(obj)
        db.commit()
        logger.info("Прибрано недійсних токенів: %d", len(dead))
    return sent, len(tokens)


