"""
Доброчесність — бекенд (API + база даних + адмін-панель з авторизацією через БД).

Render:
- Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
- Environment Variables:
  DATABASE_URL=postgresql://...neon.tech/...?...sslmode=require
  SECRET_KEY=<будь-який_довгий_секретний_рядок>  (бажано, але не обов'язково)

Перший запуск:
- Відкрий /setup
- Створи першого адміністратора
- Після створення першого адміністратора /setup автоматично закривається
"""

import base64
import hashlib
import hmac
import os
import secrets
import uuid
import json
import firebase_admin
from firebase_admin import credentials, messaging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import Boolean, Date, DateTime, Integer, JSON, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dobrochesnist.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    cat: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    date: Mapped[date] = mapped_column(Date, index=True)
    recur: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    instruction: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(String, default="Усі працівники")
    link: Mapped[str] = mapped_column(String, default="")
    reminders: Mapped[list] = mapped_column(JSON, default=list)
    views: Mapped[int] = mapped_column(Integer, default=0)

class Reference(Base):
    __tablename__ = "reference"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String, default="")

class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    salt: Mapped[str] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

class Device(Base):
    __tablename__ = "devices"
    token: Mapped[str] = mapped_column(String, primary_key=True)
    platform: Mapped[str] = mapped_column(String, default="android")
    app_version: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(String, index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


Base.metadata.create_all(engine)

def ensure_schema():
    stmts = [
        "ALTER TABLE events ADD COLUMN link VARCHAR DEFAULT ''",
        "ALTER TABLE devices ADD COLUMN platform VARCHAR DEFAULT 'android'",
        "ALTER TABLE devices ADD COLUMN app_version VARCHAR DEFAULT ''",
        "ALTER TABLE devices ADD COLUMN created_at TIMESTAMP",
        "ALTER TABLE devices ADD COLUMN updated_at TIMESTAMP",
        "ALTER TABLE devices ADD COLUMN last_seen_at TIMESTAMP",
        "ALTER TABLE chat_messages ADD COLUMN answer TEXT DEFAULT ''",
        "ALTER TABLE chat_messages ADD COLUMN status VARCHAR DEFAULT 'new'",
        "ALTER TABLE chat_messages ADD COLUMN answered_at TIMESTAMP",
    ]
    for stmt in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception:
            pass
ensure_schema()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

SESSION_COOKIE = "dobro_admin_session"
SESSION_TTL_SECONDS = 60 * 60 * 12
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("DATABASE_URL", "local-dev-secret")

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return base64.b64encode(digest).decode("utf-8"), salt

def verify_password(password: str, password_hash: str, salt: str) -> bool:
    calculated, _ = hash_password(password, salt)
    return secrets.compare_digest(calculated, password_hash)

def sign_value(value: str) -> str:
    sig = hmac.new(SECRET_KEY.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"

def unsign_value(signed: str) -> Optional[str]:
    if not signed or "." not in signed:
        return None
    value, sig = signed.rsplit(".", 1)
    expected = hmac.new(SECRET_KEY.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
    return value if secrets.compare_digest(sig, expected) else None

def create_session_cookie(user_id: str) -> str:
    expires = int(datetime.now(timezone.utc).timestamp()) + SESSION_TTL_SECONDS
    return sign_value(f"{user_id}:{expires}")

def get_session_user(request: Request, db: Session) -> Optional[AdminUser]:
    raw = unsign_value(request.cookies.get(SESSION_COOKIE) or "")
    if not raw:
        return None
    try:
        user_id, expires_s = raw.split(":", 1)
        if int(expires_s) < int(datetime.now(timezone.utc).timestamp()):
            return None
    except Exception:
        return None
    user = db.get(AdminUser, user_id)
    return user if user and user.is_active else None

def require_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    user = get_session_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Потрібна авторизація адміністратора")
    return user

def admin_exists(db: Session) -> bool:
    return db.query(AdminUser).count() > 0

class EventIn(BaseModel):
    cat: str
    title: str
    date: date
    recur: str = ""
    description: str = ""
    instruction: str = ""
    audience: str = "Усі працівники"
    link: str = ""
    reminders: List[int] = []

class EventOut(EventIn):
    id: str
    views: int
    class Config:
        from_attributes = True

class ReferenceIn(BaseModel):
    title: str
    description: str = ""
    link: str = ""

class ReferenceOut(ReferenceIn):
    id: str
    class Config:
        from_attributes = True

class DeviceIn(BaseModel):
    token: str
    platform: str = "android"
    app_version: str = ""

class DeviceOut(DeviceIn):
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    class Config:
        from_attributes = True

class PushIn(BaseModel):
    title: str
    body: str

class ChatQuestionIn(BaseModel):
    client_id: str
    question: str

class ChatAnswerIn(BaseModel):
    answer: str

class ChatMessageOut(BaseModel):
    id: str
    client_id: str
    question: str
    answer: str = ""
    status: str = "new"
    created_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None
    class Config:
        from_attributes = True

app = FastAPI(title="Доброчесність API", version="0.3.0", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    # Для мобільного додатку на Capacitor/Android.
    # CORS НЕ є захистом від зміни БД. Зміни захищає admin_guard нижче.
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

firebase_app = None

def init_firebase():
    global firebase_app
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
        print("Firebase init error:", e)
        return None

@app.middleware("http")
async def admin_guard(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()
    protected = (
        path.startswith("/admin") or path.startswith("/stats") or path.startswith("/users") or path.startswith("/push")
        or path.startswith("/chat/admin")
        or (path.startswith("/chat/") and method in {"POST", "PUT", "DELETE", "PATCH"} and not path.startswith("/chat/question"))
        or (path.startswith("/devices") and not path.startswith("/devices/register"))
        or (path.startswith("/events") and method in {"POST", "PUT", "DELETE", "PATCH"} and not path.endswith("/view"))
        or (path.startswith("/reference") and method in {"POST", "PUT", "DELETE", "PATCH"})
    )
    if protected:
        db = SessionLocal()
        try:
            if not get_session_user(request, db):
                if path.startswith("/admin"):
                    return RedirectResponse(url="/login", status_code=303)
                return Response(status_code=401, content="Потрібна авторизація адміністратора")
        finally:
            db.close()
    return await call_next(request)

@app.get("/events", response_model=List[EventOut])
def list_events(db: Session = Depends(get_db)):
    return db.query(Event).order_by(Event.date).all()

@app.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    return ev

@app.post("/events/{event_id}/view", response_model=EventOut)
def register_view(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    ev.views += 1
    db.commit()
    db.refresh(ev)
    return ev

@app.post("/events", response_model=EventOut, status_code=201)
def create_event(data: EventIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    ev = Event(id=f"e{uuid.uuid4().hex[:8]}", views=0, **data.model_dump())
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev

@app.put("/events/{event_id}", response_model=EventOut)
def update_event(event_id: str, data: EventIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    for key, value in data.model_dump().items():
        setattr(ev, key, value)
    db.commit()
    db.refresh(ev)
    return ev

@app.delete("/events/{event_id}", status_code=204)
def delete_event(event_id: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    db.delete(ev)
    db.commit()

@app.get("/reference", response_model=List[ReferenceOut])
def list_reference(db: Session = Depends(get_db)):
    return db.query(Reference).order_by(Reference.title).all()

@app.post("/reference", response_model=ReferenceOut, status_code=201)
def create_reference(data: ReferenceIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    r = Reference(id=f"r{uuid.uuid4().hex[:8]}", **data.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r

@app.put("/reference/{ref_id}", response_model=ReferenceOut)
def update_reference(ref_id: str, data: ReferenceIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(Reference, ref_id)
    if not r:
        raise HTTPException(404, "Запис не знайдено")
    for key, value in data.model_dump().items():
        setattr(r, key, value)
    db.commit()
    db.refresh(r)
    return r

@app.delete("/reference/{ref_id}", status_code=204)
def delete_reference(ref_id: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(Reference, ref_id)
    if not r:
        raise HTTPException(404, "Запис не знайдено")
    db.delete(r)
    db.commit()

@app.get("/stats")
def stats(admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    events = db.query(Event).all()
    return {"events": len(events), "views": sum(e.views for e in events), "categories": len({e.cat for e in events}), "by_event": [{"id": e.id, "title": e.title, "cat": e.cat, "views": e.views} for e in sorted(events, key=lambda e: e.views, reverse=True)]}

@app.post("/devices/register")
def register_device(data: DeviceIn, db: Session = Depends(get_db)):
    token = (data.token or "").strip()
    if not token:
        raise HTTPException(400, "FCM token обов'язковий")
    now = datetime.now(timezone.utc)
    dev = db.get(Device, token)
    if dev:
        dev.platform = data.platform or dev.platform or "android"
        dev.app_version = data.app_version or dev.app_version or ""
        dev.updated_at = now
        dev.last_seen_at = now
    else:
        dev = Device(
            token=token,
            platform=data.platform or "android",
            app_version=data.app_version or "",
            created_at=now,
            updated_at=now,
            last_seen_at=now,
        )
        db.add(dev)
    db.commit()
    return {"ok": True}

@app.get("/devices", response_model=List[DeviceOut])
def list_devices(admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(Device).order_by(Device.updated_at.desc()).all()

@app.delete("/devices/{token}", status_code=204)
def delete_device(token: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    dev = db.get(Device, token)
    if not dev:
        raise HTTPException(404, "Пристрій не знайдено")
    db.delete(dev)
    db.commit()

@app.post("/push/send")
def send_push(data: PushIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    if not data.title.strip() or not data.body.strip():
        raise HTTPException(400, "Заголовок і текст повідомлення обов'язкові")

    fb = init_firebase()
    if not fb:
        raise HTTPException(500, "Firebase не налаштовано. Перевір змінну FIREBASE_SERVICE_ACCOUNT_JSON на Render.")

    devices = db.query(Device).all()
    tokens = [d.token for d in devices if d.token]

    if not tokens:
        return {"ok": False, "sent": 0, "failed": 0, "message": "Немає зареєстрованих пристроїв"}

    sent = 0
    failed = 0
    errors = []

    for token in tokens:
        try:
            message = messaging.Message(
                notification=messaging.Notification(
                    title=data.title.strip(),
                    body=data.body.strip(),
                ),
                data={
                    "source": "dobrochesnist",
                    "type": "admin_push",
                },
                token=token,
            )
            messaging.send(message)
            sent += 1
        except Exception as e:
            failed += 1
            errors.append(str(e)[:300])

    return {"ok": True, "sent": sent, "failed": failed, "errors": errors[:5]}

@app.post("/chat/question", response_model=ChatMessageOut, status_code=201)
def create_chat_question(data: ChatQuestionIn, db: Session = Depends(get_db)):
    client_id = (data.client_id or "").strip()
    question = (data.question or "").strip()
    if not client_id:
        raise HTTPException(400, "client_id обов'язковий")
    if not question:
        raise HTTPException(400, "Питання обов'язкове")
    msg = ChatMessage(
        id=f"q{uuid.uuid4().hex[:12]}",
        client_id=client_id,
        question=question,
        answer="",
        status="new",
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg

@app.get("/chat/messages", response_model=List[ChatMessageOut])
def list_my_chat_messages(client_id: str, db: Session = Depends(get_db)):
    client_id = (client_id or "").strip()
    if not client_id:
        return []
    return db.query(ChatMessage).filter(ChatMessage.client_id == client_id).order_by(ChatMessage.created_at.desc()).all()

@app.get("/chat/admin", response_model=List[ChatMessageOut])
def list_chat_admin(admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(ChatMessage).order_by(ChatMessage.created_at.desc()).all()

@app.post("/chat/{message_id}/answer", response_model=ChatMessageOut)
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

    # Сповіщення про відповідь. Оскільки пристрої поки не прив'язані до користувача,
    # повідомлення надсилається всім зареєстрованим пристроям.
    try:
        init_firebase()
        for device in db.query(Device).all():
            try:
                notification = messaging.Notification(title="Відповідь у чаті", body="На ваше питання надано відповідь у додатку Доброчесність")
                fcm_msg = messaging.Message(notification=notification, data={"type": "chat_answer", "question_id": msg.id}, token=device.token)
                messaging.send(fcm_msg)
            except Exception as e:
                print("CHAT PUSH ERROR:", str(e))
    except Exception as e:
        print("CHAT PUSH INIT ERROR:", str(e))
    return msg

@app.get("/users")
def list_admin_users(admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.query(AdminUser).order_by(AdminUser.username).all()
    return [{"id": u.id, "username": u.username, "is_active": u.is_active, "created_at": u.created_at.isoformat() if u.created_at else None, "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None} for u in users]

@app.post("/users")
def create_admin_user(username: str = Form(...), password: str = Form(...), admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    username = username.strip()
    if not username or len(password) < 8:
        raise HTTPException(400, "Логін обов'язковий, пароль мінімум 8 символів")
    if db.query(AdminUser).filter(AdminUser.username == username).first():
        raise HTTPException(400, "Такий логін уже існує")
    password_hash, salt = hash_password(password)
    user = AdminUser(id=f"u{uuid.uuid4().hex[:12]}", username=username, password_hash=password_hash, salt=salt, is_active=True)
    db.add(user)
    db.commit()
    return {"ok": True}

@app.post("/users/{user_id}/password")
def change_admin_password(user_id: str, password: str = Form(...), admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    if len(password) < 8:
        raise HTTPException(400, "Пароль мінімум 8 символів")
    user = db.get(AdminUser, user_id)
    if not user:
        raise HTTPException(404, "Користувача не знайдено")
    user.password_hash, user.salt = hash_password(password)
    db.commit()
    return {"ok": True}

@app.post("/users/{user_id}/toggle")
def toggle_admin_user(user_id: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.get(AdminUser, user_id)
    if not user:
        raise HTTPException(404, "Користувача не знайдено")
    if user.id == admin.id:
        raise HTTPException(400, "Не можна заблокувати самого себе")
    user.is_active = not user.is_active
    db.commit()
    return {"ok": True, "is_active": user.is_active}

def seed():
    # Порожній перший запуск: створюються тільки таблиці.
    # Демо-події та довідник автоматично НЕ додаються.
    pass


# seed() навмисно не викликається, щоб при першому запуску база була порожня.


LOGIN_STYLE = """
<style>
:root { --bg:#EBEEF0; --surface:#FFFFFF; --ink:#1B2430; --soft:#5A6577; --line:#DCE1E6; --accent:#1F5673; --red:#9E2F3C; --green:#2C6A4E; }
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; font-family:Arial, sans-serif; background:linear-gradient(135deg,#E6EEF2,#F7F9FA); color:var(--ink); display:flex; align-items:center; justify-content:center; padding:20px; }
.card { width:100%; max-width:420px; background:white; border-radius:20px; padding:28px; box-shadow:0 18px 50px rgba(31,86,115,.14); border:1px solid rgba(31,86,115,.08); }
.logo { width:54px; height:54px; border-radius:16px; background:#1F5673; color:#fff; display:flex; align-items:center; justify-content:center; font-size:28px; font-weight:800; margin-bottom:18px; }
h1 { margin:0; color:#1F5673; font-size:26px; } p { color:var(--soft); line-height:1.45; }
label { display:block; margin:16px 0 6px; color:var(--soft); font-size:13px; font-weight:700; }
input { width:100%; padding:13px 14px; border:1px solid #cfd6dd; border-radius:12px; font-size:15px; outline:none; }
button { width:100%; margin-top:20px; padding:13px 16px; border:0; border-radius:12px; background:#1F5673; color:white; font-weight:800; cursor:pointer; font-size:15px; }
.err { background:#F5E2E4; color:#9E2F3C; padding:10px 12px; border-radius:10px; margin-top:14px; font-size:14px; }
.ok { background:#E1EEE8; color:#2C6A4E; padding:10px 12px; border-radius:10px; margin-top:14px; font-size:14px; }
small { color:var(--soft); display:block; margin-top:16px; }
</style>
"""

@app.get("/", response_class=HTMLResponse)
def root():
    return """<!DOCTYPE html><html lang='uk'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Доброчесність API</title><style>body{font-family:Arial;background:#eef2f5;margin:0;padding:40px;color:#1B2430}.box{max-width:800px;margin:auto;background:white;padding:30px;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.06)}h1{color:#1F5673;margin-top:0}a{display:inline-block;margin:8px 8px 0 0;padding:10px 14px;background:#1F5673;color:white;text-decoration:none;border-radius:8px}</style></head><body><div class='box'><h1>Доброчесність API працює</h1><p>Сервіс успішно запущений.</p><a href='/admin'>Адмін-панель</a><a href='/events'>Події JSON</a><a href='/reference'>Довідник JSON</a></div></body></html>"""

@app.get("/setup", response_class=HTMLResponse)
def setup_page(db: Session = Depends(get_db)):
    if admin_exists(db):
        return RedirectResponse(url="/login", status_code=303)
    return f"""<!DOCTYPE html><html lang='uk'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Перший адміністратор</title>{LOGIN_STYLE}</head><body><form class='card' method='post' action='/setup'><div class='logo'>Д</div><h1>Створення адміністратора</h1><p>Адміністраторів ще немає. Створи перший обліковий запис. Після цього ця сторінка закриється.</p><label>Логін</label><input name='username' autocomplete='username' required><label>Пароль</label><input name='password' type='password' autocomplete='new-password' minlength='8' required><button type='submit'>Створити адміністратора</button><small>Пароль збережеться у Neon у вигляді хешу.</small></form></body></html>"""

@app.post("/setup")
def setup_create_admin(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if admin_exists(db):
        return RedirectResponse(url="/login", status_code=303)
    username = username.strip()
    if not username or len(password) < 8:
        return HTMLResponse(f"<!DOCTYPE html><html><head>{LOGIN_STYLE}</head><body><div class='card'><h1>Помилка</h1><div class='err'>Логін обов'язковий, пароль мінімум 8 символів.</div></div></body></html>", status_code=400)
    password_hash, salt = hash_password(password)
    db.add(AdminUser(id=f"u{uuid.uuid4().hex[:12]}", username=username, password_hash=password_hash, salt=salt, is_active=True))
    db.commit()
    return RedirectResponse(url="/login?created=1", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if not admin_exists(db):
        return RedirectResponse(url="/setup", status_code=303)
    msg = ""
    if request.query_params.get("created") == "1": msg = '<div class="ok">Адміністратора створено. Тепер увійди.</div>'
    if request.query_params.get("error") == "1": msg = '<div class="err">Невірний логін або пароль.</div>'
    if request.query_params.get("logout") == "1": msg = '<div class="ok">Вихід виконано.</div>'
    return f"""<!DOCTYPE html><html lang='uk'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Вхід адміністратора</title>{LOGIN_STYLE}</head><body><form class='card' method='post' action='/login'><div class='logo'>Д</div><h1>Вхід адміністратора</h1><p>Адмін-панель системи “Доброчесність”.</p>{msg}<label>Логін</label><input name='username' autocomplete='username' required><label>Пароль</label><input name='password' type='password' autocomplete='current-password' required><button type='submit'>Увійти</button><small>Доступ до редагування захищений.</small></form></body></html>"""

@app.post("/login")
def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(AdminUser).filter(AdminUser.username == username.strip(), AdminUser.is_active == True).first()
    if not user or not verify_password(password, user.password_hash, user.salt):
        return RedirectResponse(url="/login?error=1", status_code=303)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(key=SESSION_COOKIE, value=create_session_cookie(user.id), httponly=True, secure=True, samesite="lax", max_age=SESSION_TTL_SECONDS)
    return response

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login?logout=1", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response

@app.get("/admin", response_class=HTMLResponse)
def admin_panel(admin: AdminUser = Depends(require_admin)):
    return """
<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Адмін-панель | Доброчесність</title>
<style>
:root{
  --bg:#eef3f7;
  --bg2:#f7fafc;
  --panel:#ffffff;
  --ink:#17212f;
  --muted:#667085;
  --line:#d9e2ea;
  --brand:#174c68;
  --brand2:#0f3448;
  --accent:#2f7ea4;
  --green:#24724f;
  --red:#a93542;
  --amber:#b56a12;
  --shadow:0 18px 50px rgba(23,76,104,.13);
  --radius:20px;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;
  min-height:100vh;
  font-family:Arial, sans-serif;
  color:var(--ink);
  background:
    radial-gradient(circle at top left, rgba(47,126,164,.18), transparent 34%),
    linear-gradient(135deg,var(--bg),var(--bg2));
}
.app{display:grid;grid-template-columns:290px 1fr;min-height:100vh}
.sidebar{
  position:sticky;top:0;height:100vh;padding:22px;
  background:linear-gradient(180deg,var(--brand2),var(--brand));
  color:#fff;box-shadow:12px 0 35px rgba(15,52,72,.20);
}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:28px}
.logo{width:48px;height:48px;border-radius:16px;background:rgba(255,255,255,.14);display:flex;align-items:center;justify-content:center;font-size:25px;font-weight:900;border:1px solid rgba(255,255,255,.20)}
.brand h1{font-size:20px;line-height:1.15;margin:0}.brand small{display:block;color:rgba(255,255,255,.72);margin-top:4px}
.nav{display:grid;gap:10px}.nav button{width:100%;text-align:left;border:0;border-radius:16px;padding:14px 14px;color:rgba(255,255,255,.78);background:transparent;cursor:pointer;font-weight:800;font-size:15px;display:flex;align-items:center;gap:10px}.nav button:hover,.nav button.active{background:rgba(255,255,255,.14);color:#fff}.nav .ico{width:30px;height:30px;border-radius:10px;background:rgba(255,255,255,.12);display:inline-flex;align-items:center;justify-content:center}.side-footer{position:absolute;left:22px;right:22px;bottom:22px;border-top:1px solid rgba(255,255,255,.18);padding-top:16px}.side-footer a{display:block;color:rgba(255,255,255,.82);text-decoration:none;margin-top:9px;font-weight:700;font-size:13px}
.main{padding:26px;min-width:0}.topbar{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:18px}.title h2{margin:0;font-size:28px;color:var(--brand2)}.title p{margin:6px 0 0;color:var(--muted)}.quick{display:flex;gap:10px;flex-wrap:wrap}.quick a,.quick button{border:1px solid var(--line);background:#fff;color:var(--brand);border-radius:999px;padding:10px 13px;text-decoration:none;font-weight:800;cursor:pointer;box-shadow:0 6px 20px rgba(23,76,104,.06)}
.status{padding:12px 14px;border-radius:14px;margin:0 0 16px;display:none;font-weight:700}.ok{background:#e2f1ea;color:var(--green);display:block}.err{background:#f8e1e4;color:var(--red);display:block}
.section{display:none;animation:fade .18s ease}.section.active{display:block}@keyframes fade{from{opacity:.4;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
.grid{display:grid;grid-template-columns:1.05fr .95fr;gap:18px}.card{background:rgba(255,255,255,.92);border:1px solid rgba(217,226,234,.9);border-radius:var(--radius);box-shadow:var(--shadow);padding:20px;margin-bottom:18px}.card h3{margin:0 0 14px;color:var(--brand2);font-size:20px}.muted{color:var(--muted);font-size:13px;line-height:1.45}.form-grid{display:grid;grid-template-columns:1fr 180px 210px;gap:14px}.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}label{display:block;margin:0 0 6px;color:#536172;font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.03em}input,textarea,select{width:100%;padding:12px 13px;margin:0 0 14px;border:1px solid #cad6df;border-radius:14px;font-size:14px;background:#fff;color:var(--ink);outline:none}input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 4px rgba(47,126,164,.12)}textarea{resize:vertical}button{padding:11px 14px;border:0;border-radius:13px;cursor:pointer;background:var(--brand);color:white;font-weight:900}.btns{display:flex;gap:9px;flex-wrap:wrap}.gray{background:#5c6676}.green{background:var(--green)}.del{background:var(--red)}.edit{background:var(--amber)}.ghost{background:#eef4f8;color:var(--brand)}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:18px}.kpi{background:#fff;border:1px solid var(--line);border-radius:18px;padding:17px;box-shadow:0 12px 35px rgba(23,76,104,.08)}.kpi b{font-size:28px;color:var(--brand2)}.kpi span{display:block;color:var(--muted);font-size:12px;font-weight:900;text-transform:uppercase;margin-top:6px}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:18px;background:#fff}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:14px}th{background:#f3f7fa;color:#667085;font-size:12px;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}tr:last-child td{border-bottom:0}.pill{display:inline-block;padding:5px 9px;border-radius:999px;background:#e6f0f5;color:var(--brand);font-size:12px;font-weight:900}.table-actions{display:flex;gap:7px;flex-wrap:wrap}.table-actions button{padding:8px 10px;font-size:12px}.lnk{color:var(--brand);font-size:12px;word-break:break-all;font-weight:700}
.empty{padding:18px;color:var(--muted)}
@media(max-width:980px){.app{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.side-footer{position:static;margin-top:20px}.grid,.form-grid,.two,.kpis{grid-template-columns:1fr}.main{padding:14px}.topbar{align-items:flex-start;flex-direction:column}}
@media(max-width:700px){.card{padding:15px}.title h2{font-size:23px}table,thead,tbody,th,td,tr{display:block}thead{display:none}tr{border-bottom:1px solid var(--line);padding:10px}td{border:0;padding:6px 2px}.sidebar{padding:16px}.nav{grid-template-columns:1fr 1fr}.nav button{font-size:13px;padding:11px}.brand h1{font-size:18px}}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><div class="logo">Д</div><div><h1>Доброчесність</h1><small>Адмін-панель системи</small></div></div>
    <nav class="nav">
      <button class="active" data-tab="events"><span class="ico">📅</span>Події</button>
      <button data-tab="reference"><span class="ico">📚</span>Довідка</button>
      <button data-tab="admin"><span class="ico">🛡️</span>Адміністрування</button>
      <button data-tab="devices"><span class="ico">📱</span>Пристрої</button>
      <button data-tab="push"><span class="ico">🔔</span>Push</button>
      <button data-tab="chat"><span class="ico">💬</span>Чат</button>
    </nav>
    <div class="side-footer">
      <div class="muted" style="color:rgba(255,255,255,.7)">Швидкі переходи</div>
      <a href="/events" target="_blank">JSON подій</a>
      <a href="/reference" target="_blank">JSON довідника</a>
      <a href="/stats" target="_blank">Статистика</a>
      <a href="/logout">Вийти</a>
    </div>
  </aside>
  <main class="main">
    <div class="topbar">
      <div class="title"><h2 id="pageTitle">Події та нагадування</h2><p id="pageSub">Створення, редагування та контроль календарних подій.</p></div>
      <div class="quick"><button class="ghost" onclick="refreshCurrent()">Оновити</button><a href="/logout">Вийти</a></div>
    </div>
    <div id="status" class="status"></div>

    <section id="tab-events" class="section active">
      <div class="kpis"><div class="kpi"><b id="kpiEvents">0</b><span>Подій</span></div><div class="kpi"><b id="kpiViews">0</b><span>Переглядів</span></div><div class="kpi"><b id="kpiCats">0</b><span>Категорій</span></div></div>
      <div class="grid">
        <div class="card"><h3>Нова / редагування події</h3><input type="hidden" id="eventId"><div class="form-grid"><div><label>Назва події</label><input id="title" placeholder="Наприклад: Подання щорічної декларації"></div><div><label>Дата</label><input id="date" type="date"></div><div><label>Категорія</label><select id="cat"><option value="declaration">Декларування</option><option value="conflict">Конфлікт інтересів</option><option value="gifts">Подарунки</option><option value="notice">Повідомлення</option><option value="training">Навчання</option><option value="restriction">Обмеження</option></select></div></div><div class="two"><div><label>Повторюваність</label><input id="recur" placeholder="Щороку / щомісяця / одноразово"></div><div><label>Аудиторія</label><input id="audience" value="Усі працівники"></div></div><label>Опис</label><textarea id="description" rows="3" placeholder="Короткий зміст події"></textarea><label>Інструкція для користувача</label><textarea id="instruction" rows="4" placeholder="Що потрібно зробити користувачу"></textarea><div class="two"><div><label>Посилання</label><input id="link" placeholder="https://..."></div><div><label>Нагадування, днів до події</label><input id="reminders" value="30,10,3,0"></div></div><div class="btns"><button onclick="saveEvent()">Зберегти подію</button><button class="gray" onclick="clearForm()">Очистити</button></div></div>
        <div class="card"><h3>Підказка</h3><p class="muted">У цій вкладці додаються події для календаря мобільного додатку. Для зручності заповнюй назву, дату, категорію, інструкцію та кількість днів для нагадувань через кому.</p><p class="muted">Після натискання “Редагувати” форма автоматично заповнюється вибраною подією.</p></div>
      </div>
      <div class="card"><h3>Список подій</h3><div class="table-wrap"><table><thead><tr><th>ID</th><th>Дата</th><th>Назва</th><th>Категорія</th><th>Посилання</th><th>Перегляди</th><th>Дії</th></tr></thead><tbody id="events"></tbody></table></div></div>
    </section>

    <section id="tab-reference" class="section">
      <div class="grid">
        <div class="card"><h3>Запис довідника</h3><input type="hidden" id="refId"><label>Назва</label><input id="refTitle" placeholder="Наприклад: Конфлікт інтересів"><label>Опис</label><textarea id="refDescription" rows="5" placeholder="Коротке пояснення для користувача"></textarea><label>Посилання</label><input id="refLink" placeholder="https://..."><div class="btns"><button onclick="saveRef()">Зберегти запис</button><button class="gray" onclick="clearRefForm()">Очистити</button></div></div>
        <div class="card"><h3>Для чого ця вкладка</h3><p class="muted">Тут зберігаються довідкові матеріали: конфлікт інтересів, подарунки, обмеження, декларування, посилання на нормативні джерела.</p></div>
      </div>
      <div class="card"><h3>Матеріали довідки</h3><div class="table-wrap"><table><thead><tr><th>Назва</th><th>Опис</th><th>Посилання</th><th>Дії</th></tr></thead><tbody id="refs"></tbody></table></div></div>
    </section>

    <section id="tab-admin" class="section">
      <div class="grid">
        <div class="card"><h3>Створити адміністратора</h3><label>Новий логін</label><input id="newUser" placeholder="Логін адміністратора"><label>Пароль нового адміністратора</label><input id="newPass" type="password" placeholder="Мінімум 8 символів"><div class="btns"><button onclick="createUser()">Створити адміністратора</button></div></div>
        <div class="card"><h3>Безпека</h3><p class="muted">Редагування подій, довідника та адміністраторів захищене авторизацією. Мобільний додаток може читати події та довідку, але зміни доступні тільки після входу в адмін-панель.</p></div>
      </div>
      <div class="card"><h3>Адміністратори</h3><div class="table-wrap"><table><thead><tr><th>Логін</th><th>Статус</th><th>Останній вхід</th><th>Дії</th></tr></thead><tbody id="users"></tbody></table></div></div>
    </section>

    <section id="tab-devices" class="section">
      <div class="card">
        <h3>Зареєстровані пристрої</h3>
        <p class="muted">Тут мають з’являтися FCM-токени Android-пристроїв після запуску мобільного додатку.</p>
        <div class="btns" style="margin:12px 0"><button class="green" onclick="loadDevices(true)">Оновити пристрої</button></div>
        <div class="table-wrap"><table><thead><tr><th>Платформа</th><th>Token</th><th>Версія</th><th>Оновлено</th><th>Дії</th></tr></thead><tbody id="devices"></tbody></table></div>
      </div>
    </section>

    <section id="tab-push" class="section">
      <div class="grid">
        <div class="card">
          <h3>Надіслати push-повідомлення</h3>
          <label>Заголовок</label>
          <input id="pushTitle" placeholder="Наприклад: Нагадування з доброчесності">
          <label>Текст повідомлення</label>
          <textarea id="pushBody" rows="5" placeholder="Введіть текст, який отримають користувачі на Android"></textarea>
          <div class="btns">
            <button class="green" onclick="sendPush()">Надіслати всім пристроям</button>
            <button class="gray" onclick="clearPushForm()">Очистити</button>
          </div>
        </div>
        <div class="card">
          <h3>Як це працює</h3>
          <p class="muted">Повідомлення буде надіслано через Firebase Cloud Messaging усім пристроям, які зареєстровані у вкладці “Пристрої”.</p>
          <p class="muted">Якщо повідомлення не приходить, перевір: чи є токени у таблиці devices, чи додано FIREBASE_SERVICE_ACCOUNT_JSON на Render, і чи встановлено firebase-admin у requirements.txt.</p>
        </div>
      </div>
    </section>

    <section id="tab-chat" class="section">
      <div class="card">
        <h3>Питання користувачів</h3>
        <p class="muted">У цій вкладці відображаються питання, які користувачі надсилають із мобільного додатку. Адміністратор може надати відповідь, після чого користувачу буде надіслано push-повідомлення.</p>
        <div class="btns" style="margin:12px 0">
          <button class="green" onclick="loadChat(true)">Оновити чат</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Дата</th>
                <th>Питання</th>
                <th>Відповідь</th>
                <th>Статус</th>
                <th>Дії</th>
              </tr>
            </thead>
            <tbody id="chatMessages"></tbody>
          </table>
        </div>
      </div>
    </section>
  </main>
</div>
<script>
const titles={events:['Події та нагадування','Створення, редагування та контроль календарних подій.'],reference:['Довідка','Керування довідковими матеріалами для користувачів.'],admin:['Адміністрування','Керування адміністраторами та доступом.'],devices:['Пристрої','Зареєстровані Android-пристрої для push-повідомлень.'],push:['Push-повідомлення','Надсилання повідомлень на всі зареєстровані Android-пристрої.'],chat:['Чат','Відповіді на питання користувачів мобільного додатку.']};
let currentTab='events';
document.querySelectorAll('.nav button').forEach(btn=>btn.addEventListener('click',()=>switchTab(btn.dataset.tab)));
function switchTab(tab){currentTab=tab;document.querySelectorAll('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));const el=document.getElementById('tab-'+tab); if(!el){showStatus('Вкладку не знайдено: '+tab,false);return;} el.classList.add('active');document.getElementById('pageTitle').textContent=titles[tab][0];document.getElementById('pageSub').textContent=titles[tab][1];location.hash=tab;refreshCurrent(false)}
function refreshCurrent(show=true){if(currentTab==='events')loadEvents(show);if(currentTab==='reference')loadRefs(show);if(currentTab==='admin')loadUsers(show);if(currentTab==='devices')loadDevices(show);if(currentTab==='push')loadDevices(false);if(currentTab==='chat')loadChat(show)}
function showStatus(t,ok=true){const e=document.getElementById('status');e.className='status '+(ok?'ok':'err');e.textContent=t;e.style.display='block';window.scrollTo({top:0,behavior:'smooth'});setTimeout(()=>e.style.display='none',5000)}
function escapeHtml(v){return String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;')}
async function req(u,o={}){const r=await fetch(u,o);if(r.redirected&&r.url.includes('/login'))location.href='/login';return r}
function fmtDate(v){if(!v)return '—';const [y,m,d]=String(v).split('-');return d&&m&&y?`${d}.${m}.${y}`:v}
async function loadStats(){try{const r=await req('/stats');if(!r.ok)return;const s=await r.json();document.getElementById('kpiEvents').textContent=s.events??0;document.getElementById('kpiViews').textContent=s.views??0;document.getElementById('kpiCats').textContent=s.categories??0}catch(e){}}
async function loadEvents(show=false){try{const r=await req('/events');if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json(),tb=document.getElementById('events');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="7"><div class="empty">Подій поки немає</div></td></tr>';return}d.forEach(ev=>{const tr=document.createElement('tr'),lc=ev.link?`<a class="lnk" href="${escapeHtml(ev.link)}" target="_blank">${escapeHtml(ev.link)}</a>`:'<span class="muted">—</span>';tr.innerHTML=`<td><span class="muted">${escapeHtml(ev.id)}</span></td><td><b>${fmtDate(ev.date)}</b><div class="muted">${escapeHtml(ev.recur||'')}</div></td><td><b>${escapeHtml(ev.title)}</b><div class="muted">${escapeHtml(ev.description)}</div></td><td><span class="pill">${escapeHtml(ev.cat)}</span></td><td>${lc}</td><td>${escapeHtml(ev.views??0)}</td><td><div class="table-actions"><button class="edit">Редагувати</button><button class="del">Видалити</button></div></td>`;tr.querySelector('.edit').onclick=()=>editEvent(ev);tr.querySelector('.del').onclick=()=>deleteEvent(ev.id);tb.appendChild(tr)});loadStats();if(show)showStatus('Список подій оновлено')}catch(e){showStatus('Не вдалося завантажити події: '+e.message,false)}}
function editEvent(ev){document.getElementById('eventId').value=ev.id||'';document.getElementById('title').value=ev.title||'';document.getElementById('date').value=ev.date||'';document.getElementById('cat').value=ev.cat||'declaration';document.getElementById('recur').value=ev.recur||'';document.getElementById('description').value=ev.description||'';document.getElementById('instruction').value=ev.instruction||'';document.getElementById('link').value=ev.link||'';document.getElementById('audience').value=ev.audience||'Усі працівники';document.getElementById('reminders').value=(ev.reminders||[30,10,3,0]).join(',');switchTab('events');showStatus('Подію відкрито для редагування')}
function clearForm(){['eventId','title','date','recur','description','instruction','link'].forEach(id=>document.getElementById(id).value='');document.getElementById('cat').value='declaration';document.getElementById('audience').value='Усі працівники';document.getElementById('reminders').value='30,10,3,0'}
async function saveEvent(){const id=document.getElementById('eventId').value.trim(),p={title:document.getElementById('title').value.trim(),date:document.getElementById('date').value,cat:document.getElementById('cat').value,recur:document.getElementById('recur').value.trim(),description:document.getElementById('description').value.trim(),instruction:document.getElementById('instruction').value.trim(),link:document.getElementById('link').value.trim(),audience:document.getElementById('audience').value.trim()||'Усі працівники',reminders:document.getElementById('reminders').value.split(',').map(x=>Number(x.trim())).filter(x=>!isNaN(x))};if(!p.title||!p.date||!p.cat){showStatus('Заповни назву, дату та категорію',false);return}try{const r=await req(id?`/events/${id}`:'/events',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());clearForm();await loadEvents();showStatus(id?'Подію оновлено':'Подію створено')}catch(e){showStatus('Помилка збереження: '+e.message,false)}}
async function deleteEvent(id){if(!confirm('Видалити цю подію?'))return;try{const r=await req(`/events/${id}`,{method:'DELETE'});if(!r.ok&&r.status!==204)throw new Error('HTTP '+r.status+' '+await r.text());await loadEvents();showStatus('Подію видалено')}catch(e){showStatus('Помилка видалення: '+e.message,false)}}
async function loadRefs(show=false){try{const r=await req('/reference');if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json(),tb=document.getElementById('refs');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="4"><div class="empty">Записів поки немає</div></td></tr>';return}d.forEach(x=>{const tr=document.createElement('tr'),lc=x.link?`<a class="lnk" href="${escapeHtml(x.link)}" target="_blank">${escapeHtml(x.link)}</a>`:'<span class="muted">—</span>';tr.innerHTML=`<td><b>${escapeHtml(x.title)}</b></td><td><div class="muted">${escapeHtml(x.description)}</div></td><td>${lc}</td><td><div class="table-actions"><button class="edit">Редагувати</button><button class="del">Видалити</button></div></td>`;tr.querySelector('.edit').onclick=()=>editRef(x);tr.querySelector('.del').onclick=()=>deleteRef(x.id);tb.appendChild(tr)});if(show)showStatus('Довідку оновлено')}catch(e){showStatus('Не вдалося завантажити довідник: '+e.message,false)}}
function editRef(r){document.getElementById('refId').value=r.id||'';document.getElementById('refTitle').value=r.title||'';document.getElementById('refDescription').value=r.description||'';document.getElementById('refLink').value=r.link||'';switchTab('reference');showStatus('Запис відкрито для редагування')}
function clearRefForm(){['refId','refTitle','refDescription','refLink'].forEach(id=>document.getElementById(id).value='')}
async function saveRef(){const id=document.getElementById('refId').value.trim(),p={title:document.getElementById('refTitle').value.trim(),description:document.getElementById('refDescription').value.trim(),link:document.getElementById('refLink').value.trim()};if(!p.title){showStatus('Заповни назву запису довідника',false);return}try{const r=await req(id?`/reference/${id}`:'/reference',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());clearRefForm();await loadRefs();showStatus(id?'Запис оновлено':'Запис створено')}catch(e){showStatus('Помилка збереження: '+e.message,false)}}
async function deleteRef(id){if(!confirm('Видалити цей запис довідника?'))return;try{const r=await req(`/reference/${id}`,{method:'DELETE'});if(!r.ok&&r.status!==204)throw new Error('HTTP '+r.status+' '+await r.text());await loadRefs();showStatus('Запис видалено')}catch(e){showStatus('Помилка видалення: '+e.message,false)}}
async function loadUsers(show=false){try{const r=await req('/users');if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json(),tb=document.getElementById('users');tb.innerHTML='';d.forEach(u=>{const tr=document.createElement('tr');tr.innerHTML=`<td><b>${escapeHtml(u.username)}</b><div class="muted">${escapeHtml(u.id)}</div></td><td>${u.is_active?'<span class="pill">Активний</span>':'<span class="muted">Заблокований</span>'}</td><td>${escapeHtml(u.last_login_at||'—')}</td><td><div class="table-actions"><button class="edit">Змінити пароль</button><button class="del">${u.is_active?'Заблокувати':'Активувати'}</button></div></td>`;tr.querySelector('.edit').onclick=()=>changeUserPassword(u.id);tr.querySelector('.del').onclick=()=>toggleUser(u.id);tb.appendChild(tr)});if(show)showStatus('Список адміністраторів оновлено')}catch(e){showStatus('Не вдалося завантажити користувачів: '+e.message,false)}}
async function createUser(){const username=document.getElementById('newUser').value.trim(),password=document.getElementById('newPass').value;if(!username||password.length<8){showStatus('Логін обов’язковий, пароль мінімум 8 символів',false);return}try{const r=await req('/users',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({username,password})});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());document.getElementById('newUser').value='';document.getElementById('newPass').value='';await loadUsers();showStatus('Адміністратора створено')}catch(e){showStatus('Помилка створення адміністратора: '+e.message,false)}}
async function changeUserPassword(id){const password=prompt('Новий пароль мінімум 8 символів:');if(!password)return;if(password.length<8){showStatus('Пароль мінімум 8 символів',false);return}try{const r=await req(`/users/${id}/password`,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({password})});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());showStatus('Пароль змінено')}catch(e){showStatus('Помилка зміни пароля: '+e.message,false)}}
async function toggleUser(id){if(!confirm('Змінити статус адміністратора?'))return;try{const r=await req(`/users/${id}/toggle`,{method:'POST'});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());await loadUsers();showStatus('Статус змінено')}catch(e){showStatus('Помилка зміни статусу: '+e.message,false)}}
async function loadDevices(show=false){try{const r=await req('/devices');if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json(),tb=document.getElementById('devices');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="5"><div class="empty">Пристроїв поки немає. Відкрий мобільний додаток на телефоні та дозволь сповіщення.</div></td></tr>';return}d.forEach(x=>{const tr=document.createElement('tr');const shortToken=String(x.token||'');tr.innerHTML=`<td><span class="pill">${escapeHtml(x.platform||'android')}</span></td><td><div class="muted" style="max-width:520px;word-break:break-all">${escapeHtml(shortToken)}</div></td><td>${escapeHtml(x.app_version||'—')}</td><td>${escapeHtml(x.updated_at||x.last_seen_at||'—')}</td><td><div class="table-actions"><button class="del">Видалити</button></div></td>`;tr.querySelector('.del').onclick=()=>deleteDevice(x.token);tb.appendChild(tr)});if(show)showStatus('Список пристроїв оновлено')}catch(e){showStatus('Не вдалося завантажити пристрої: '+e.message,false)}}
async function deleteDevice(token){if(!confirm('Видалити цей пристрій?'))return;try{const r=await req(`/devices/${encodeURIComponent(token)}`,{method:'DELETE'});if(!r.ok&&r.status!==204)throw new Error('HTTP '+r.status+' '+await r.text());await loadDevices();showStatus('Пристрій видалено')}catch(e){showStatus('Помилка видалення пристрою: '+e.message,false)}}
function clearPushForm(){document.getElementById('pushTitle').value='';document.getElementById('pushBody').value=''}
async function sendPush(){const title=document.getElementById('pushTitle').value.trim(),body=document.getElementById('pushBody').value.trim();if(!title||!body){showStatus('Заповни заголовок і текст push-повідомлення',false);return}if(!confirm('Надіслати push усім зареєстрованим пристроям?'))return;try{const r=await req('/push/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,body})});const txt=await r.text();let d={};try{d=JSON.parse(txt)}catch{}if(!r.ok)throw new Error(txt||('HTTP '+r.status));showStatus(`Push надіслано. Успішно: ${d.sent??0}, помилок: ${d.failed??0}`,true)}catch(e){showStatus('Помилка надсилання push: '+e.message,false)}}

async function loadChat(show=false){try{const r=await req('/chat/admin');if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());const d=await r.json(),tb=document.getElementById('chatMessages');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="5"><div class="empty">Питань поки немає.</div></td></tr>';return}d.forEach(x=>{const tr=document.createElement('tr');const answered=Boolean(x.answer);tr.innerHTML=`<td><span class="muted">${escapeHtml(x.created_at||'—')}</span></td><td><b>${escapeHtml(x.question||'')}</b><div class="muted">${escapeHtml(x.client_id||'')}</div></td><td>${answered?`<div class="muted">${escapeHtml(x.answer)}</div>`:`<textarea id="answer_${escapeHtml(x.id)}" rows="3" placeholder="Введіть відповідь"></textarea>`}</td><td>${answered?'<span class="pill">Відповідь надано</span>':'<span class="muted">Очікує відповіді</span>'}</td><td><div class="table-actions">${answered?'<button class="edit">Редагувати</button>':'<button class="green">Відповісти</button>'}</div></td>`;const btn=tr.querySelector('button');if(btn){btn.onclick=()=>{if(answered){const a=prompt('Нова відповідь:',x.answer||'');if(a!==null)answerChat(x.id,a)}else{answerChat(x.id,document.getElementById('answer_'+x.id).value)}}}tb.appendChild(tr)});if(show)showStatus('Чат оновлено')}catch(e){showStatus('Не вдалося завантажити чат: '+e.message,false)}}
async function answerChat(id,answer){answer=String(answer||'').trim();if(!answer){showStatus('Введи відповідь',false);return}try{const r=await req(`/chat/${encodeURIComponent(id)}/answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer})});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());await loadChat();showStatus('Відповідь збережено та надіслано повідомлення')}catch(e){showStatus('Помилка відповіді: '+e.message,false)}}
loadEvents();loadRefs();loadUsers();loadDevices();loadChat();loadStats();
if(location.hash){const tab=location.hash.replace('#','');if(['events','reference','admin','devices','push','chat'].includes(tab))switchTab(tab)}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
