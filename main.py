"""
Доброчесність — бекенд (API + база даних + адмін-панель)

Render:
- Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
- Environment Variables:
  DATABASE_URL=postgresql://...neon.tech/...?...sslmode=require
  SECRET_KEY=<секретний_рядок>
  FIREBASE_SERVICE_ACCOUNT_JSON={...} (опціонально)
"""

import base64
import hashlib
import hmac
import os
import secrets
import uuid
import json
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import Boolean, Date, DateTime, Integer, JSON, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# Firebase
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False

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
    return db.get(AdminUser, user_id)


def require_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    user = get_session_user(request, db)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Потрібна авторизація")
    return user


def admin_exists(db: Session) -> bool:
    return db.query(AdminUser).count() > 0


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
    except Exception:
        return None


# ==================== Pydantic Models ====================
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
    class Config: from_attributes = True


class ReferenceIn(BaseModel):
    title: str
    description: str = ""
    link: str = ""


class ReferenceOut(ReferenceIn):
    id: str
    class Config: from_attributes = True


class DeviceIn(BaseModel):
    token: str
    platform: str = "android"
    app_version: str = ""


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
    class Config: from_attributes = True


# ==================== FastAPI App ====================
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def admin_guard(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()
    protected = (
        path.startswith("/admin") or path.startswith("/users") or path.startswith("/push")
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
                return Response(status_code=401)
        finally:
            db.close()
    return await call_next(request)


# ==================== Events ====================
@app.get("/events", response_model=List[EventOut])
def list_events(db: Session = Depends(get_db)):
    return db.query(Event).order_by(Event.date).all()


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


# ==================== Reference ====================
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


# ==================== Devices ====================
@app.post("/devices/register")
def register_device(data: DeviceIn, db: Session = Depends(get_db)):
    token = (data.token or "").strip()
    if not token:
        raise HTTPException(400, "Token обов'язковий")
    now = datetime.now(timezone.utc)
    dev = db.get(Device, token)
    if dev:
        dev.updated_at = now
        dev.last_seen_at = now
    else:
        dev = Device(token=token, platform=data.platform, app_version=data.app_version, created_at=now, updated_at=now, last_seen_at=now)
        db.add(dev)
    db.commit()
    return {"ok": True}


@app.get("/devices")
def list_devices(admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(Device).order_by(Device.updated_at.desc()).all()


@app.delete("/devices/{token}", status_code=204)
def delete_device(token: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    dev = db.get(Device, token)
    if not dev:
        raise HTTPException(404, "Пристрій не знайдено")
    db.delete(dev)
    db.commit()


# ==================== Push Notifications ====================
@app.post("/push/send")
def send_push(data: PushIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    if not data.title.strip() or not data.body.strip():
        raise HTTPException(400, "Заголовок і текст обов'язкові")
    if not FIREBASE_AVAILABLE:
        return {"ok": False, "message": "Firebase не встановлено"}
    fb = init_firebase()
    if not fb:
        return {"ok": False, "message": "Firebase не налаштовано"}
    devices = db.query(Device).all()
    tokens = [d.token for d in devices if d.token]
    if not tokens:
        return {"ok": False, "message": "Немає пристроїв"}
    sent = 0
    for token in tokens:
        try:
            msg = messaging.Message(notification=messaging.Notification(title=data.title.strip(), body=data.body.strip()), token=token)
            messaging.send(msg)
            sent += 1
        except Exception:
            pass
    return {"ok": True, "sent": sent, "total": len(tokens)}


# ==================== Chat ====================
@app.post("/chat/question", response_model=ChatMessageOut, status_code=201)
def create_chat_question(data: ChatQuestionIn, db: Session = Depends(get_db)):
    client_id = (data.client_id or "").strip()
    question = (data.question or "").strip()
    if not client_id or not question:
        raise HTTPException(400, "client_id та питання обов'язкові")
    msg = ChatMessage(id=f"q{uuid.uuid4().hex[:12]}", client_id=client_id, question=question)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


@app.get("/chat/messages", response_model=List[ChatMessageOut])
def list_my_chat_messages(client_id: str, db: Session = Depends(get_db)):
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
    # Push-сповіщення про відповідь
    if FIREBASE_AVAILABLE and init_firebase():
        for device in db.query(Device).all():
            try:
                notification = messaging.Notification(title="Відповідь у чаті", body="На ваше питання надано відповідь")
                fcm_msg = messaging.Message(notification=notification, data={"type": "chat_answer"}, token=device.token)
                messaging.send(fcm_msg)
            except Exception:
                pass
    return msg


@app.delete("/chat/{message_id}", status_code=204)
def delete_chat_message(message_id: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    msg = db.get(ChatMessage, message_id)
    if not msg:
        raise HTTPException(404, "Повідомлення не знайдено")
    db.delete(msg)
    db.commit()
    return Response(status_code=204)


# ==================== Admin Users ====================
@app.get("/users")
def list_admin_users(admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    return [{"id": u.id, "username": u.username, "is_active": u.is_active} for u in db.query(AdminUser).all()]


@app.post("/users")
def create_admin_user(username: str = Form(...), password: str = Form(...), admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    username = username.strip()
    if not username or len(password) < 8:
        raise HTTPException(400, "Логін обов'язковий, пароль мінімум 8 символів")
    if db.query(AdminUser).filter(AdminUser.username == username).first():
        raise HTTPException(400, "Такий логін уже існує")
    password_hash, salt = hash_password(password)
    user = AdminUser(id=f"u{uuid.uuid4().hex[:12]}", username=username, password_hash=password_hash, salt=salt)
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
        raise HTTPException(400, "Не можна заблокувати себе")
    user.is_active = not user.is_active
    db.commit()
    return {"ok": True, "is_active": user.is_active}


# ==================== Auth Pages ====================
LOGIN_STYLE = """<style>.card{max-width:400px;margin:auto;background:#fff;padding:28px;border-radius:20px}.logo{width:54px;height:54px;background:#1F5673;color:#fff;display:flex;align-items:center;justify-content:center;font-size:28px;border-radius:16px;margin-bottom:18px}input{width:100%;padding:12px;margin:10px 0;border:1px solid #ddd;border-radius:12px}button{width:100%;padding:12px;background:#1F5673;color:#fff;border:0;border-radius:12px;cursor:pointer}.err{background:#F5E2E4;color:#9E2F3C;padding:10px;border-radius:10px}.ok{background:#E1EEE8;color:#2C6A4E;padding:10px;border-radius:10px}</style>"""


@app.get("/")
def root():
    return HTMLResponse('<a href="/admin">Адмін-панель</a>')


@app.get("/setup", response_class=HTMLResponse)
def setup_page(db: Session = Depends(get_db)):
    if admin_exists(db):
        return RedirectResponse(url="/login", status_code=303)
    return HTMLResponse(f"""<html><body><div class="card"><div class="logo">Д</div><h1>Створення адміністратора</h1><form method="post"><input name="username" placeholder="Логін" required><input name="password" type="password" placeholder="Пароль" required><button>Створити</button></form></div>{LOGIN_STYLE}</body></html>""")


@app.post("/setup")
def setup_create_admin(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if admin_exists(db):
        return RedirectResponse(url="/login", status_code=303)
    username = username.strip()
    if not username or len(password) < 8:
        return HTMLResponse('<div class="err">Помилка</div>', status_code=400)
    password_hash, salt = hash_password(password)
    db.add(AdminUser(id=f"u{uuid.uuid4().hex[:12]}", username=username, password_hash=password_hash, salt=salt))
    db.commit()
    return RedirectResponse(url="/login?created=1", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if not admin_exists(db):
        return RedirectResponse(url="/setup", status_code=303)
    msg = '<div class="ok">Створено! Увійдіть</div>' if request.query_params.get("created") else ''
    err = '<div class="err">Невірний логін або пароль</div>' if request.query_params.get("error") else ''
    return HTMLResponse(f"""<html><body><div class="card"><div class="logo">Д</div><h1>Вхід</h1>{msg}{err}<form method="post"><input name="username" placeholder="Логін" required><input name="password" type="password" placeholder="Пароль" required><button>Увійти</button></form></div>{LOGIN_STYLE}</body></html>""")


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(AdminUser).filter(AdminUser.username == username.strip(), AdminUser.is_active == True).first()
    if not user or not verify_password(password, user.password_hash, user.salt):
        return RedirectResponse(url="/login?error=1", status_code=303)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(key=SESSION_COOKIE, value=create_session_cookie(user.id), httponly=True)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login?logout=1", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ==================== Admin Panel ====================
ADMIN_HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Адмін-панель | Доброчесність</title>
<style>
:root{--bg:#eef4f8;--panel:#ffffff;--sidebar:#123f59;--sidebar2:#0d3147;--accent:#1f6f8b;--accent2:#2c8aa8;--green:#2c6a4e;--red:#a93542;--orange:#b56a12;--text:#172b3a;--muted:#6b7c88;--line:#dbe6ec;--shadow:0 14px 35px rgba(18,63,89,.10)}
*{box-sizing:border-box}body{margin:0;font-family:Arial,Helvetica,sans-serif;background:linear-gradient(135deg,#eaf2f7 0%,#f7fafc 100%);color:var(--text)}.app{display:flex;min-height:100vh}.sidebar{width:280px;background:linear-gradient(180deg,var(--sidebar),var(--sidebar2));color:#fff;padding:20px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}.brand{display:flex;align-items:center;gap:12px;font-size:20px;font-weight:800;margin-bottom:26px}.brand-icon{width:46px;height:46px;border-radius:16px;background:rgba(255,255,255,.14);display:flex;align-items:center;justify-content:center;font-size:24px;box-shadow:inset 0 0 0 1px rgba(255,255,255,.12)}.brand small{display:block;font-size:12px;font-weight:400;opacity:.75;margin-top:2px}.nav{display:flex;flex-direction:column;gap:7px}.nav button,.logout-btn{display:flex;align-items:center;gap:10px;width:100%;padding:13px 14px;background:transparent;border:1px solid transparent;color:#fff;font-size:14px;cursor:pointer;border-radius:14px;text-align:left;transition:.15s}.nav button.active,.nav button:hover{background:rgba(255,255,255,.14);border-color:rgba(255,255,255,.12)}.sidebar-footer{margin-top:auto;padding-top:16px}.logout-btn{justify-content:center;text-decoration:none;background:rgba(169,53,66,.18);border-color:rgba(255,255,255,.12)}.logout-btn:hover{background:#a93542}.main{flex:1;padding:28px;min-width:0}.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}.topbar h2{margin:0;font-size:28px}.card{background:rgba(255,255,255,.96);border:1px solid rgba(219,230,236,.9);border-radius:22px;padding:22px;margin-bottom:20px;box-shadow:var(--shadow)}.card h3{margin:0 0 18px;font-size:18px}.grid-2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.form-group{margin-bottom:14px}label{display:block;margin-bottom:7px;font-weight:700;font-size:13px;color:#2d4656}input,textarea,select{width:100%;padding:12px 13px;border:1px solid var(--line);border-radius:12px;background:#fbfdfe;color:var(--text);outline:none}input:focus,textarea:focus,select:focus{border-color:var(--accent2);box-shadow:0 0 0 3px rgba(44,138,168,.12)}button{padding:10px 16px;border:0;border-radius:12px;cursor:pointer;font-weight:700;transition:.15s}.btn-main{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff}.btn-main:hover,.btn-green:hover,.btn-red:hover,.btn-edit:hover{filter:brightness(.96);transform:translateY(-1px)}.btn-green{background:var(--green);color:#fff}.btn-red{background:var(--red);color:#fff}.btn-edit{background:var(--orange);color:#fff}.btn-light{background:#e8f1f6;color:#174c68}.actions{display:flex;gap:8px;flex-wrap:wrap}.table-wrap{width:100%;overflow:auto;border-radius:16px;border:1px solid var(--line)}table{width:100%;border-collapse:collapse;background:#fff;min-width:760px}th,td{padding:12px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{background:#f4f8fb;color:#415767;font-size:13px}tr:hover td{background:#fbfdff}.section{display:none}.section.active{display:block}.status{padding:12px 14px;margin-bottom:15px;border-radius:14px;display:none;font-weight:700}.status.ok{background:#e1eee8;color:#2c6a4e;display:block}.status.err{background:#f8e1e4;color:#a93542;display:block}.badge{display:inline-block;background:#e6f0f5;color:#174c68;padding:5px 9px;border-radius:999px;font-size:12px}.muted{color:var(--muted);font-size:12px}.mt-2{margin-top:10px}@media(max-width:900px){.app{display:block}.sidebar{position:relative;width:100%;height:auto}.main{padding:18px}.grid-2{grid-template-columns:1fr}.topbar{align-items:flex-start;flex-direction:column}table{min-width:680px}}
</style>
</head>
<body>
<div class="app">
<aside class="sidebar">
<div class="brand"><div class="brand-icon">Д</div><div>Доброчесність<small>адмін-панель</small></div></div>
<nav class="nav">
<button class="active" data-tab="events">📅 Події</button>
<button data-tab="reference">📚 Довідка</button>
<button data-tab="admin">👥 Адміни</button>
<button data-tab="push">🔔 Push</button>
<button data-tab="devices">📱 Пристрої</button>
<button data-tab="chat">💬 Чат</button>
</nav>
<div class="sidebar-footer"><a class="logout-btn" href="/logout">↩️ Вийти</a></div>
</aside>
<main class="main">
<div class="topbar"><h2 id="pageTitle">Події</h2><button class="btn-light" onclick="refreshCurrent()">🔄 Оновити</button></div>
<div id="status" class="status"></div>

<section id="tab-events" class="section active">
<div class="card"><h3>➕ Нова подія</h3><input type="hidden" id="eventId"><div class="form-group"><label>Назва</label><input id="title" placeholder="Назва події"></div><div class="grid-2"><div><label>Дата</label><input id="date" type="date"></div><div><label>Категорія</label><select id="cat"><option value="declaration">Декларування</option><option value="conflict">Конфлікт інтересів</option><option value="gifts">Подарунки</option><option value="notice">Повідомлення</option><option value="training">Навчання</option></select></div></div><div class="form-group mt-2"><label>Опис</label><textarea id="description" rows="2"></textarea></div><div class="form-group"><label>Інструкція</label><textarea id="instruction" rows="2"></textarea></div><div class="grid-2"><div><label>Посилання</label><input id="link" placeholder="https://"></div><div><label>Нагадування, днів</label><input id="reminders" value="30,10,3,0"></div></div><div class="actions mt-2"><button class="btn-main" onclick="saveEvent()">💾 Зберегти</button><button class="btn-light" onclick="clearForm()">🗑️ Очистити</button></div></div>
<div class="card"><h3>📋 Список подій</h3><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Назва</th><th>Категорія</th><th>Дії</th></tr></thead><tbody id="events"></tbody></table></div></div>
</section>

<section id="tab-reference" class="section">
<div class="card"><h3>➕ Новий запис довідки</h3><input type="hidden" id="refId"><div class="form-group"><label>Назва</label><input id="refTitle"></div><div class="form-group"><label>Опис</label><textarea id="refDescription" rows="3"></textarea></div><div class="form-group"><label>Посилання</label><input id="refLink"></div><button class="btn-main" onclick="saveRef()">💾 Зберегти</button></div>
<div class="card"><h3>📚 Список довідки</h3><div class="table-wrap"><table><thead><tr><th>Назва</th><th>Опис</th><th>Дії</th></tr></thead><tbody id="refs"></tbody></table></div></div>
</section>

<section id="tab-admin" class="section">
<div class="card"><h3>➕ Новий адмін</h3><div class="grid-2"><div><label>Логін</label><input id="newUser"></div><div><label>Пароль</label><input id="newPass" type="password"></div></div><button class="btn-main mt-2" onclick="createUser()">Створити</button></div>
<div class="card"><h3>👥 Список адмінів</h3><div class="table-wrap"><table><thead><tr><th>Логін</th><th>Статус</th><th>Дії</th></tr></thead><tbody id="users"></tbody></table></div></div>
</section>

<section id="tab-push" class="section">
<div class="card"><h3>🔔 Надіслати push-повідомлення</h3><label>Заголовок</label><input id="pushTitle" placeholder="Заголовок"><label class="mt-2">Текст</label><textarea id="pushBody" rows="3" placeholder="Текст повідомлення"></textarea><button onclick="sendPush()" class="btn-green mt-2">📨 Надіслати всім</button></div>
</section>

<section id="tab-devices" class="section">
<div class="card"><h3>📱 Зареєстровані пристрої</h3><button onclick="loadDevices()" class="btn-green">🔄 Оновити</button><div class="table-wrap mt-2"><table><thead><tr><th>Token</th><th>Платформа</th><th>Версія</th><th>Дії</th></tr></thead><tbody id="devices"></tbody></table></div></div>
</section>

<section id="tab-chat" class="section">
<div class="card"><h3>💬 Питання користувачів</h3><button onclick="loadChat()" class="btn-green">🔄 Оновити</button><div class="table-wrap mt-2"><table><thead><tr><th>Дата</th><th>Питання</th><th>Відповідь</th><th>Статус</th><th>Дії</th></tr></thead><tbody id="chatMessages"></tbody></table></div></div>
</section>
</main>
</div>

<script>
let currentTab='events';
const titles={events:'Події',reference:'Довідка',admin:'Адміни',push:'Push-повідомлення',devices:'Пристрої',chat:'Чат'};

document.querySelectorAll('.nav button').forEach(btn=>btn.addEventListener('click',()=>{
 currentTab=btn.dataset.tab;
 document.querySelectorAll('.nav button').forEach(b=>b.classList.remove('active'));
 btn.classList.add('active');
 document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
 document.getElementById('tab-'+currentTab).classList.add('active');
 document.getElementById('pageTitle').textContent=titles[currentTab];
 refreshCurrent();
}));

function refreshCurrent(){
 if(currentTab==='events') loadEvents();
 if(currentTab==='reference') loadRefs();
 if(currentTab==='admin') loadAdmins();
 if(currentTab==='devices') loadDevices();
 if(currentTab==='chat') loadChat();
}

function showStatus(t,ok=true){const e=document.getElementById('status');e.className='status '+(ok?'ok':'err');e.textContent=t;e.style.display='block';setTimeout(()=>e.style.display='none',3000);}
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,function(m){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m];});}
function jsArg(v){return String(v??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,'\\n').replace(/\r/g,'');}
async function req(u,o={}){const r=await fetch(u,o);if(r.redirected&&r.url.includes('/login'))location.href='/login';return r;}
function fmtDate(v){if(!v)return'';const[y,m,d]=String(v).split('-');return `${d}.${m}.${y}`;}

async function loadEvents(){try{const r=await req('/events');const d=await r.json(),tb=document.getElementById('events');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="4">Подій поки немає</td></tr>';return;}d.forEach(ev=>{tb.innerHTML+=`<tr><td>${fmtDate(ev.date)}</td><td><b>${escapeHtml(ev.title)}</b><br><span class="muted">${escapeHtml(ev.description)}</span></td><td><span class="badge">${escapeHtml(ev.cat)}</span></td><td><div class="actions"><button class="btn-edit" onclick='editEvent(${JSON.stringify(ev).replace(/'/g,"&#39;")})'>✏️</button><button class="btn-red" onclick="deleteEvent('${ev.id}')">🗑️</button></div></td></tr>`});}catch(e){showStatus(e.message,false);}}
function editEvent(ev){document.getElementById('eventId').value=ev.id;document.getElementById('title').value=ev.title;document.getElementById('date').value=ev.date;document.getElementById('cat').value=ev.cat;document.getElementById('description').value=ev.description||'';document.getElementById('instruction').value=ev.instruction||'';document.getElementById('link').value=ev.link||'';document.getElementById('reminders').value=(ev.reminders||[]).join(',');window.scrollTo({top:0,behavior:'smooth'});showStatus('Відкрито редагування');}
function clearForm(){document.getElementById('eventId').value='';document.getElementById('title').value='';document.getElementById('date').value='';document.getElementById('description').value='';document.getElementById('instruction').value='';document.getElementById('link').value='';document.getElementById('reminders').value='30,10,3,0';}
async function saveEvent(){const id=document.getElementById('eventId').value;const p={title:document.getElementById('title').value.trim(),date:document.getElementById('date').value,cat:document.getElementById('cat').value,description:document.getElementById('description').value,instruction:document.getElementById('instruction').value,link:document.getElementById('link').value,reminders:document.getElementById('reminders').value.split(',').map(x=>Number(x.trim())).filter(x=>!isNaN(x))};if(!p.title||!p.date){showStatus('Заповніть назву та дату',false);return;}try{const r=await req(id?`/events/${id}`:'/events',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw new Error('Не вдалося зберегти');clearForm();await loadEvents();showStatus(id?'Оновлено':'Створено');}catch(e){showStatus(e.message||'Помилка',false);}}
async function deleteEvent(id){if(!confirm('Видалити подію?'))return;await req(`/events/${id}`,{method:'DELETE'});await loadEvents();showStatus('Видалено');}

async function loadRefs(){try{const r=await req('/reference');const d=await r.json();const tb=document.getElementById('refs');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="3">Записів довідки поки немає</td></tr>';return;}d.forEach(x=>{tb.innerHTML+=`<tr><td><b>${escapeHtml(x.title)}</b>${x.link?`<br><span class="muted">${escapeHtml(x.link)}</span>`:''}</td><td>${escapeHtml(x.description)}</td><td><div class="actions"><button class="btn-edit" onclick='editRef(${JSON.stringify(x).replace(/'/g,"&#39;")})'>✏️</button><button class="btn-red" onclick="deleteRef('${x.id}')">🗑️</button></div></td></tr>`});}catch(e){showStatus('Не вдалося завантажити довідку',false);}}
function editRef(r){document.getElementById('refId').value=r.id;document.getElementById('refTitle').value=r.title;document.getElementById('refDescription').value=r.description||'';document.getElementById('refLink').value=r.link||'';window.scrollTo({top:0,behavior:'smooth'});}
async function saveRef(){const id=document.getElementById('refId').value;const p={title:document.getElementById('refTitle').value.trim(),description:document.getElementById('refDescription').value,link:document.getElementById('refLink').value};if(!p.title){showStatus('Заповніть назву',false);return;}try{const r=await req(id?`/reference/${id}`:'/reference',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw new Error();document.getElementById('refId').value='';document.getElementById('refTitle').value='';document.getElementById('refDescription').value='';document.getElementById('refLink').value='';await loadRefs();showStatus(id?'Оновлено':'Створено');}catch(e){showStatus('Помилка',false);}}
async function deleteRef(id){if(!confirm('Видалити запис?'))return;await req(`/reference/${id}`,{method:'DELETE'});await loadRefs();showStatus('Видалено');}

async function loadAdmins(){try{const r=await req('/users');const d=await r.json();const tb=document.getElementById('users');tb.innerHTML='';d.forEach(u=>{tb.innerHTML+=`<tr><td>${escapeHtml(u.username)}</td><td>${u.is_active?'✅ Активний':'❌ Заблокований'}</td><td><div class="actions"><button class="btn-edit" onclick="changePass('${u.id}')">🔑 Пароль</button><button class="btn-red" onclick="toggleUser('${u.id}',${u.is_active})">${u.is_active?'🔒 Блокувати':'🔓 Активувати'}</button></div></td></tr>`});}catch(e){showStatus('Не вдалося завантажити адмінів',false);}}
async function createUser(){const username=document.getElementById('newUser').value.trim();const password=document.getElementById('newPass').value;if(!username||password.length<8){showStatus('Пароль мін. 8 символів',false);return;}try{const r=await req('/users',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({username,password})});if(!r.ok)throw new Error();document.getElementById('newUser').value='';document.getElementById('newPass').value='';await loadAdmins();showStatus('Створено');}catch(e){showStatus('Помилка створення',false);}}
async function changePass(id){const p=prompt('Новий пароль (мін. 8 символів):');if(!p||p.length<8){showStatus('Пароль має бути не менше 8 символів',false);return;}await req(`/users/${id}/password`,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({password:p})});showStatus('Пароль змінено');}
async function toggleUser(id,isActive){if(!confirm(isActive?'Заблокувати адміністратора?':'Активувати адміністратора?'))return;await req(`/users/${id}/toggle`,{method:'POST'});await loadAdmins();showStatus('Статус змінено');}

async function sendPush(){const title=document.getElementById('pushTitle').value.trim();const body=document.getElementById('pushBody').value.trim();if(!title||!body){showStatus('Заповніть заголовок і текст',false);return;}try{const r=await req('/push/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,body})});const d=await r.json();showStatus(d.ok?`Надіслано ${d.sent}/${d.total}`:'Помилка: '+d.message,d.ok);}catch(e){showStatus('Помилка',false);}}

async function loadDevices(){try{const r=await req('/devices');const d=await r.json();const tb=document.getElementById('devices');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="4">Пристроїв поки немає</td></tr>';return;}d.forEach(x=>{tb.innerHTML+=`<tr><td style="font-family:monospace;font-size:11px">${escapeHtml(x.token)}</td><td>${escapeHtml(x.platform)}</td><td>${escapeHtml(x.app_version)}</td><td><button class="btn-red" onclick="deleteDevice('${jsArg(x.token)}')">🗑️</button></td></tr>`});}catch(e){showStatus('Не вдалося завантажити пристрої',false);}}
async function deleteDevice(token){if(!confirm('Видалити пристрій?'))return;await req(`/devices/${encodeURIComponent(token)}`,{method:'DELETE'});await loadDevices();showStatus('Видалено');}

async function loadChat(){try{const r=await req('/chat/admin');const d=await r.json();const tb=document.getElementById('chatMessages');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="5">Немає питань</td></tr>';return;}d.forEach(x=>{const answered=!!x.answer;const dateStr=x.created_at?new Date(x.created_at).toLocaleString('uk-UA'):'';tb.innerHTML+=`<tr><td>${escapeHtml(dateStr)}</td><td><b>${escapeHtml(x.question)}</b><br><span class="muted">${escapeHtml(x.client_id)}</span></td><td>${answered?escapeHtml(x.answer):'<textarea id="a_'+x.id+'" rows="2" style="width:100%"></textarea>'}</td><td>${answered?'✅ Відповідь надано':'⏳ Очікує'}</td><td><div class="actions">${answered?'<button class="btn-edit" onclick="editAnswer(\''+x.id+'\',\''+jsArg(x.answer)+'\')">✏️</button>':''}<button class="btn-green" onclick="answerChat(\''+x.id+'\',document.getElementById(\'a_'+x.id+'\')?.value)">📝</button><button class="btn-red" onclick="deleteMessage(\''+x.id+'\')">🗑️</button></div></td></tr>`});}catch(e){showStatus('Не вдалося завантажити чат',false);}}
async function editAnswer(id,current){const newA=prompt('Редагувати відповідь:',current);if(newA)await answerChat(id,newA);}
async function answerChat(id,answer){if(!answer||!answer.trim()){showStatus('Введіть відповідь',false);return;}try{const r=await req(`/chat/${id}/answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer:answer.trim()})});if(!r.ok)throw new Error();await loadChat();showStatus('Відповідь збережено');}catch(e){showStatus('Помилка',false);}}
async function deleteMessage(id){if(!confirm('Видалити повідомлення?'))return;await req(`/chat/${id}`,{method:'DELETE'});await loadChat();showStatus('Видалено');}

loadEvents();
</script>
</body>
</html>"""


@app.get("/admin", response_class=HTMLResponse)
def admin_panel(admin: AdminUser = Depends(require_admin)):
    return HTMLResponse(ADMIN_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
