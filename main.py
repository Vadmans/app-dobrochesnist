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
LOGIN_STYLE = """<style>
:root{--brand:#13455f;--brand2:#0e3346;--accent:#1f7a96;--text:#16242e;--muted:#6a7d89;--line:#dde7ee}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
 font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--text);
 background:radial-gradient(1200px 600px at 50% -10%,#1b5876 0%,#0e3346 55%,#0a2433 100%)}
.card{width:100%;max-width:380px;background:#fff;padding:34px 30px;border-radius:20px;
 box-shadow:0 30px 70px rgba(7,30,44,.35)}
.logo{width:56px;height:56px;background:linear-gradient(135deg,var(--accent),var(--brand));color:#fff;
 display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:800;border-radius:16px;margin-bottom:18px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
label{display:block;font-size:12px;font-weight:700;color:#3a5161;margin:14px 0 6px}
input{width:100%;padding:12px 13px;border:1px solid var(--line);border-radius:11px;font-size:14px;outline:none;background:#fbfdfe}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(31,122,150,.14)}
button{width:100%;margin-top:22px;padding:13px;background:linear-gradient(135deg,var(--accent),var(--brand));
 color:#fff;border:0;border-radius:11px;font-size:15px;font-weight:700;cursor:pointer;transition:.15s}
button:hover{filter:brightness(1.05);transform:translateY(-1px)}
.err{background:#fbe7e9;color:#9e2f3c;padding:11px 13px;border-radius:11px;font-size:13px;margin-bottom:6px}
.ok{background:#e3f0e9;color:#226a4c;padding:11px 13px;border-radius:11px;font-size:13px;margin-bottom:6px}
</style>"""


@app.get("/")
def root():
    return HTMLResponse('<a href="/admin">Адмін-панель</a>')


@app.get("/setup", response_class=HTMLResponse)
def setup_page(db: Session = Depends(get_db)):
    if admin_exists(db):
        return RedirectResponse(url="/login", status_code=303)
    return HTMLResponse(f"""<html lang="uk"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Створення адміністратора</title>{LOGIN_STYLE}</head><body><div class="card"><div class="logo">Д</div><h1>Створення адміністратора</h1><p class="sub">Перший вхід у систему «Доброчесність»</p><form method="post"><label>Логін</label><input name="username" placeholder="Введіть логін" required><label>Пароль</label><input name="password" type="password" placeholder="Мінімум 8 символів" required><button>Створити обліковий запис</button></form></div></body></html>""")


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
    msg = '<div class="ok">Обліковий запис створено. Увійдіть.</div>' if request.query_params.get("created") else ''
    err = '<div class="err">Невірний логін або пароль</div>' if request.query_params.get("error") else ''
    return HTMLResponse(f"""<html lang="uk"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Вхід | Доброчесність</title>{LOGIN_STYLE}</head><body><div class="card"><div class="logo">Д</div><h1>Вхід в адмін-панель</h1><p class="sub">Система обліку доброчесності</p>{msg}{err}<form method="post"><label>Логін</label><input name="username" placeholder="Введіть логін" required><label>Пароль</label><input name="password" type="password" placeholder="Введіть пароль" required><button>Увійти</button></form></div></body></html>""")


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
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Адмін-панель | Доброчесність</title>
<style>
:root{
 --bg:#eef3f7; --panel:#ffffff;
 --sidebar:#11405a; --sidebar2:#0c2d41;
 --accent:#1f7a96; --accent2:#2a93b2; --accent-soft:#e6f1f6;
 --green:#1f7a5a; --green-soft:#e4f2eb;
 --red:#b23b48; --red-soft:#fbe8ea;
 --orange:#b6740f; --orange-soft:#fbf0dc;
 --text:#16242e; --muted:#6a7d89; --line:#e2eaf0; --line2:#eef3f6;
 --shadow:0 10px 30px rgba(16,58,84,.08);
 --shadow-sm:0 2px 8px rgba(16,58,84,.06);
 --radius:16px;
}
*{box-sizing:border-box}
html,body{margin:0}
body{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);
 font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
.app{display:flex;min-height:100vh}

/* ---------- Sidebar ---------- */
.sidebar{width:262px;background:linear-gradient(180deg,var(--sidebar),var(--sidebar2));color:#fff;
 padding:22px 16px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh;flex-shrink:0}
.brand{display:flex;align-items:center;gap:12px;padding:4px 8px 22px;border-bottom:1px solid rgba(255,255,255,.10);margin-bottom:18px}
.brand-icon{width:44px;height:44px;border-radius:13px;background:linear-gradient(135deg,var(--accent2),var(--accent));
 display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:800;flex-shrink:0;box-shadow:0 6px 16px rgba(0,0,0,.18)}
.brand b{font-size:17px;font-weight:800;letter-spacing:.2px}
.brand small{display:block;font-size:11px;font-weight:500;opacity:.7;margin-top:2px;letter-spacing:.3px}
.nav-label{font-size:11px;text-transform:uppercase;letter-spacing:1px;opacity:.55;padding:0 10px;margin:6px 0 8px}
.nav{display:flex;flex-direction:column;gap:3px}
.nav button{display:flex;align-items:center;gap:11px;width:100%;padding:11px 13px;background:transparent;
 border:0;color:rgba(255,255,255,.82);font-size:14px;font-weight:600;cursor:pointer;border-radius:11px;
 text-align:left;transition:background .15s,color .15s;position:relative}
.nav button .ic{font-size:16px;width:20px;text-align:center;flex-shrink:0}
.nav button:hover{background:rgba(255,255,255,.08);color:#fff}
.nav button.active{background:rgba(255,255,255,.14);color:#fff}
.nav button.active::before{content:"";position:absolute;left:-16px;top:9px;bottom:9px;width:3px;border-radius:0 3px 3px 0;background:var(--accent2)}
.sidebar-footer{margin-top:auto;padding-top:16px}
.logout-btn{display:flex;align-items:center;justify-content:center;gap:9px;width:100%;padding:12px;
 background:rgba(178,59,72,.16);border:1px solid rgba(255,255,255,.10);color:#fff;font-weight:700;
 text-decoration:none;border-radius:11px;font-size:13px;transition:.15s}
.logout-btn:hover{background:#b23b48;border-color:transparent}

/* ---------- Main ---------- */
.main{flex:1;padding:30px 34px;min-width:0}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:22px}
.topbar h2{margin:0;font-size:25px;font-weight:800;letter-spacing:-.3px}
.topbar .sub{margin:3px 0 0;color:var(--muted);font-size:13px}

/* ---------- Cards ---------- */
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
 padding:22px;margin-bottom:18px;box-shadow:var(--shadow-sm)}
.card h3{margin:0 0 18px;font-size:15px;font-weight:700;display:flex;align-items:center;gap:8px;
 padding-bottom:14px;border-bottom:1px solid var(--line2)}
.card h3 .ic{font-size:16px}

.grid-2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}

/* ---------- Forms ---------- */
.form-group{margin-bottom:14px}
label{display:block;margin-bottom:6px;font-weight:600;font-size:12.5px;color:#43596a}
input,textarea,select{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;
 background:#fbfdfe;color:var(--text);outline:none;font-size:14px;font-family:inherit;transition:.15s}
input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(31,122,150,.13);background:#fff}
textarea{resize:vertical;min-height:54px}
select{appearance:none;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%236a7d89' stroke-width='3'><path d='M6 9l6 6 6-6'/></svg>");
 background-repeat:no-repeat;background-position:right 12px center;padding-right:34px}

/* ---------- Buttons ---------- */
button{padding:10px 16px;border:0;border-radius:10px;cursor:pointer;font-weight:700;font-size:13.5px;
 font-family:inherit;transition:transform .12s,filter .15s,box-shadow .15s}
.btn-main{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;box-shadow:0 4px 12px rgba(31,122,150,.25)}
.btn-green{background:var(--green);color:#fff}
.btn-red{background:var(--red);color:#fff}
.btn-edit{background:var(--orange);color:#fff}
.btn-light{background:#fff;color:#14516e;border:1px solid var(--line)}
.btn-main:hover,.btn-green:hover,.btn-red:hover,.btn-edit:hover{filter:brightness(1.06);transform:translateY(-1px)}
.btn-light:hover{background:var(--accent-soft);border-color:var(--accent)}
.actions{display:flex;gap:7px;flex-wrap:wrap}
.actions button{padding:7px 11px;font-size:13px}

/* ---------- Tables ---------- */
.table-wrap{width:100%;overflow:auto;border-radius:12px;border:1px solid var(--line)}
table{width:100%;border-collapse:collapse;background:#fff;min-width:720px}
th,td{padding:12px 14px;text-align:left;border-bottom:1px solid var(--line2);vertical-align:top}
th{background:#f7fafc;color:#566c7a;font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
 position:sticky;top:0;z-index:1}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:#f9fcfe}
td b{font-weight:700;color:var(--text)}

/* ---------- Sections ---------- */
.section{display:none;animation:fade .22s ease}
.section.active{display:block}
@keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}

/* ---------- Toast / status ---------- */
.status{position:fixed;top:20px;right:20px;z-index:50;min-width:220px;max-width:340px;padding:13px 16px;
 border-radius:12px;font-weight:600;font-size:13.5px;display:none;box-shadow:0 12px 30px rgba(16,58,84,.18);
 border-left:4px solid transparent}
.status.ok{background:#fff;color:#1c6248;border-left-color:var(--green);display:block}
.status.err{background:#fff;color:#9e2f3c;border-left-color:var(--red);display:block}

/* ---------- Badges ---------- */
.badge{display:inline-block;background:var(--accent-soft);color:#14516e;padding:4px 10px;border-radius:999px;
 font-size:12px;font-weight:700;white-space:nowrap}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;font-weight:700;padding:4px 10px;border-radius:999px}
.pill-ok{background:var(--green-soft);color:#1c6248}
.pill-wait{background:var(--orange-soft);color:#8a560a}
.pill-off{background:var(--red-soft);color:#9e2f3c}
.muted{color:var(--muted);font-size:12px}
.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:11px;color:#54707f;word-break:break-all}
.mt-2{margin-top:10px}
.empty{text-align:center;color:var(--muted);padding:26px 14px !important}

@media(max-width:900px){
 .app{flex-direction:column}
 .sidebar{position:relative;width:100%;height:auto;flex-direction:column}
 .nav{flex-direction:row;flex-wrap:wrap;gap:6px}
 .nav button{width:auto}
 .nav button.active::before{display:none}
 .nav-label{display:none}
 .main{padding:18px}
 .grid-2{grid-template-columns:1fr}
 .topbar{flex-direction:column;align-items:flex-start}
}
</style>
</head>
<body>
<div class="app">
<aside class="sidebar">
<div class="brand"><div class="brand-icon">Д</div><div><b>Доброчесність</b><small>Адміністративна панель</small></div></div>
<div class="nav-label">Керування</div>
<nav class="nav">
<button class="active" data-tab="events"><span class="ic">📅</span> Події</button>
<button data-tab="reference"><span class="ic">📚</span> Довідка</button>
<button data-tab="chat"><span class="ic">💬</span> Чат із користувачами</button>
</nav>
<div class="nav-label" style="margin-top:18px">Розсилка та доступ</div>
<nav class="nav">
<button data-tab="push"><span class="ic">🔔</span> Push-повідомлення</button>
<button data-tab="devices"><span class="ic">📱</span> Пристрої</button>
<button data-tab="admin"><span class="ic">👥</span> Адміністратори</button>
</nav>
<div class="sidebar-footer"><a class="logout-btn" href="/logout">↩ Вийти з панелі</a></div>
</aside>

<main class="main">
<div class="topbar">
<div><h2 id="pageTitle">Події</h2><p class="sub" id="pageSub">Календар подій із комплаєнсу та доброчесності</p></div>
<button class="btn-light" onclick="refreshCurrent()">↻ Оновити</button>
</div>
<div id="status" class="status"></div>

<section id="tab-events" class="section active">
<div class="card"><h3><span class="ic">➕</span> Нова подія</h3><input type="hidden" id="eventId"><div class="form-group"><label>Назва</label><input id="title" placeholder="Наприклад: Подання щорічної декларації"></div><div class="grid-2"><div><label>Дата</label><input id="date" type="date"></div><div><label>Категорія</label><select id="cat"><option value="declaration">Декларування</option><option value="conflict">Конфлікт інтересів</option><option value="gifts">Подарунки</option><option value="notice">Повідомлення</option><option value="training">Навчання</option></select></div></div><div class="form-group mt-2"><label>Опис</label><textarea id="description" rows="2" placeholder="Короткий опис події"></textarea></div><div class="form-group"><label>Інструкція</label><textarea id="instruction" rows="2" placeholder="Що потрібно зробити працівнику"></textarea></div><div class="grid-2"><div><label>Посилання</label><input id="link" placeholder="https://"></div><div><label>Нагадування, днів до події</label><input id="reminders" value="30,10,3,0"></div></div><div class="actions mt-2"><button class="btn-main" onclick="saveEvent()">Зберегти подію</button><button class="btn-light" onclick="clearForm()">Очистити форму</button></div></div>
<div class="card"><h3><span class="ic">📋</span> Список подій</h3><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Назва</th><th>Категорія</th><th>Дії</th></tr></thead><tbody id="events"></tbody></table></div></div>
</section>

<section id="tab-reference" class="section">
<div class="card"><h3><span class="ic">➕</span> Новий запис довідки</h3><input type="hidden" id="refId"><div class="form-group"><label>Назва</label><input id="refTitle" placeholder="Назва матеріалу"></div><div class="form-group"><label>Опис</label><textarea id="refDescription" rows="3" placeholder="Опис матеріалу"></textarea></div><div class="form-group"><label>Посилання</label><input id="refLink" placeholder="https://"></div><button class="btn-main" onclick="saveRef()">Зберегти запис</button></div>
<div class="card"><h3><span class="ic">📚</span> Список довідки</h3><div class="table-wrap"><table><thead><tr><th>Назва</th><th>Опис</th><th>Дії</th></tr></thead><tbody id="refs"></tbody></table></div></div>
</section>

<section id="tab-admin" class="section">
<div class="card"><h3><span class="ic">➕</span> Новий адміністратор</h3><div class="grid-2"><div><label>Логін</label><input id="newUser" placeholder="Логін"></div><div><label>Пароль</label><input id="newPass" type="password" placeholder="Мінімум 8 символів"></div></div><button class="btn-main mt-2" onclick="createUser()">Створити адміністратора</button></div>
<div class="card"><h3><span class="ic">👥</span> Список адміністраторів</h3><div class="table-wrap"><table><thead><tr><th>Логін</th><th>Статус</th><th>Дії</th></tr></thead><tbody id="users"></tbody></table></div></div>
</section>

<section id="tab-push" class="section">
<div class="card"><h3><span class="ic">🔔</span> Надіслати push-повідомлення</h3><div class="form-group"><label>Заголовок</label><input id="pushTitle" placeholder="Заголовок повідомлення"></div><div class="form-group"><label>Текст</label><textarea id="pushBody" rows="3" placeholder="Текст повідомлення для всіх пристроїв"></textarea></div><button onclick="sendPush()" class="btn-green">Надіслати всім</button></div>
</section>

<section id="tab-devices" class="section">
<div class="card"><h3><span class="ic">📱</span> Зареєстровані пристрої</h3><div class="table-wrap"><table><thead><tr><th>Token</th><th>Платформа</th><th>Версія</th><th>Дії</th></tr></thead><tbody id="devices"></tbody></table></div></div>
</section>

<section id="tab-chat" class="section">
<div class="card"><h3><span class="ic">💬</span> Питання користувачів</h3><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Питання</th><th>Відповідь</th><th>Статус</th><th>Дії</th></tr></thead><tbody id="chatMessages"></tbody></table></div></div>
</section>
</main>
</div>

<script>
let currentTab='events';
const titles={events:'Події',reference:'Довідка',admin:'Адміністратори',push:'Push-повідомлення',devices:'Пристрої',chat:'Чат із користувачами'};
const subs={events:'Календар подій із комплаєнсу та доброчесності',reference:'Матеріали та корисні посилання для працівників',admin:'Облікові записи з доступом до панелі',push:'Миттєві сповіщення на всі пристрої',devices:'Пристрої, що отримують сповіщення',chat:'Запитання працівників та відповіді на них'};

document.querySelectorAll('.nav button').forEach(btn=>btn.addEventListener('click',()=>{
 currentTab=btn.dataset.tab;
 document.querySelectorAll('.nav button').forEach(b=>b.classList.remove('active'));
 btn.classList.add('active');
 document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
 document.getElementById('tab-'+currentTab).classList.add('active');
 document.getElementById('pageTitle').textContent=titles[currentTab];
 document.getElementById('pageSub').textContent=subs[currentTab];
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

async function loadEvents(){try{const r=await req('/events');const d=await r.json(),tb=document.getElementById('events');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="4" class="empty">Подій поки немає. Створіть першу подію вище.</td></tr>';return;}d.forEach(ev=>{tb.innerHTML+=`<tr><td>${fmtDate(ev.date)}</td><td><b>${escapeHtml(ev.title)}</b><br><span class="muted">${escapeHtml(ev.description)}</span></td><td><span class="badge">${escapeHtml(ev.cat)}</span></td><td><div class="actions"><button class="btn-edit" onclick='editEvent(${JSON.stringify(ev).replace(/'/g,"&#39;")})'>Редагувати</button><button class="btn-red" onclick="deleteEvent('${ev.id}')">Видалити</button></div></td></tr>`});}catch(e){showStatus('Не вдалося завантажити події',false);}}
function editEvent(ev){document.getElementById('eventId').value=ev.id;document.getElementById('title').value=ev.title;document.getElementById('date').value=ev.date;document.getElementById('cat').value=ev.cat;document.getElementById('description').value=ev.description||'';document.getElementById('instruction').value=ev.instruction||'';document.getElementById('link').value=ev.link||'';document.getElementById('reminders').value=(ev.reminders||[]).join(',');window.scrollTo({top:0,behavior:'smooth'});showStatus('Відкрито редагування події');}
function clearForm(){document.getElementById('eventId').value='';document.getElementById('title').value='';document.getElementById('date').value='';document.getElementById('description').value='';document.getElementById('instruction').value='';document.getElementById('link').value='';document.getElementById('reminders').value='30,10,3,0';}
async function saveEvent(){const id=document.getElementById('eventId').value;const p={title:document.getElementById('title').value.trim(),date:document.getElementById('date').value,cat:document.getElementById('cat').value,description:document.getElementById('description').value,instruction:document.getElementById('instruction').value,link:document.getElementById('link').value,reminders:document.getElementById('reminders').value.split(',').map(x=>Number(x.trim())).filter(x=>!isNaN(x))};if(!p.title||!p.date){showStatus('Заповніть назву та дату',false);return;}try{const r=await req(id?`/events/${id}`:'/events',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw new Error('Не вдалося зберегти');clearForm();await loadEvents();showStatus(id?'Подію оновлено':'Подію створено');}catch(e){showStatus(e.message||'Помилка',false);}}
async function deleteEvent(id){if(!confirm('Видалити подію?'))return;await req(`/events/${id}`,{method:'DELETE'});await loadEvents();showStatus('Подію видалено');}

async function loadRefs(){try{const r=await req('/reference');const d=await r.json();const tb=document.getElementById('refs');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="3" class="empty">Записів довідки поки немає.</td></tr>';return;}d.forEach(x=>{tb.innerHTML+=`<tr><td><b>${escapeHtml(x.title)}</b>${x.link?`<br><span class="muted">${escapeHtml(x.link)}</span>`:''}</td><td>${escapeHtml(x.description)}</td><td><div class="actions"><button class="btn-edit" onclick='editRef(${JSON.stringify(x).replace(/'/g,"&#39;")})'>Редагувати</button><button class="btn-red" onclick="deleteRef('${x.id}')">Видалити</button></div></td></tr>`});}catch(e){showStatus('Не вдалося завантажити довідку',false);}}
function editRef(r){document.getElementById('refId').value=r.id;document.getElementById('refTitle').value=r.title;document.getElementById('refDescription').value=r.description||'';document.getElementById('refLink').value=r.link||'';window.scrollTo({top:0,behavior:'smooth'});}
async function saveRef(){const id=document.getElementById('refId').value;const p={title:document.getElementById('refTitle').value.trim(),description:document.getElementById('refDescription').value,link:document.getElementById('refLink').value};if(!p.title){showStatus('Заповніть назву',false);return;}try{const r=await req(id?`/reference/${id}`:'/reference',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw new Error();document.getElementById('refId').value='';document.getElementById('refTitle').value='';document.getElementById('refDescription').value='';document.getElementById('refLink').value='';await loadRefs();showStatus(id?'Запис оновлено':'Запис створено');}catch(e){showStatus('Помилка',false);}}
async function deleteRef(id){if(!confirm('Видалити запис?'))return;await req(`/reference/${id}`,{method:'DELETE'});await loadRefs();showStatus('Запис видалено');}

async function loadAdmins(){try{const r=await req('/users');const d=await r.json();const tb=document.getElementById('users');tb.innerHTML='';d.forEach(u=>{tb.innerHTML+=`<tr><td><b>${escapeHtml(u.username)}</b></td><td>${u.is_active?'<span class="pill pill-ok">Активний</span>':'<span class="pill pill-off">Заблокований</span>'}</td><td><div class="actions"><button class="btn-edit" onclick="changePass('${u.id}')">Змінити пароль</button><button class="btn-red" onclick="toggleUser('${u.id}',${u.is_active})">${u.is_active?'Заблокувати':'Активувати'}</button></div></td></tr>`});}catch(e){showStatus('Не вдалося завантажити адміністраторів',false);}}
async function createUser(){const username=document.getElementById('newUser').value.trim();const password=document.getElementById('newPass').value;if(!username||password.length<8){showStatus('Логін обов’язковий, пароль мін. 8 символів',false);return;}try{const r=await req('/users',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({username,password})});if(!r.ok)throw new Error();document.getElementById('newUser').value='';document.getElementById('newPass').value='';await loadAdmins();showStatus('Адміністратора створено');}catch(e){showStatus('Помилка створення',false);}}
async function changePass(id){const p=prompt('Новий пароль (мінімум 8 символів):');if(!p||p.length<8){showStatus('Пароль має бути не менше 8 символів',false);return;}await req(`/users/${id}/password`,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({password:p})});showStatus('Пароль змінено');}
async function toggleUser(id,isActive){if(!confirm(isActive?'Заблокувати адміністратора?':'Активувати адміністратора?'))return;await req(`/users/${id}/toggle`,{method:'POST'});await loadAdmins();showStatus('Статус змінено');}

async function sendPush(){const title=document.getElementById('pushTitle').value.trim();const body=document.getElementById('pushBody').value.trim();if(!title||!body){showStatus('Заповніть заголовок і текст',false);return;}try{const r=await req('/push/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,body})});const d=await r.json();showStatus(d.ok?`Надіслано ${d.sent} із ${d.total}`:'Помилка: '+d.message,d.ok);}catch(e){showStatus('Помилка',false);}}

async function loadDevices(){try{const r=await req('/devices');const d=await r.json();const tb=document.getElementById('devices');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="4" class="empty">Пристроїв поки немає.</td></tr>';return;}d.forEach(x=>{tb.innerHTML+=`<tr><td><span class="mono">${escapeHtml(x.token)}</span></td><td>${escapeHtml(x.platform)}</td><td>${escapeHtml(x.app_version)||'—'}</td><td><button class="btn-red" onclick="deleteDevice('${jsArg(x.token)}')">Видалити</button></td></tr>`});}catch(e){showStatus('Не вдалося завантажити пристрої',false);}}
async function deleteDevice(token){if(!confirm('Видалити пристрій?'))return;await req(`/devices/${encodeURIComponent(token)}`,{method:'DELETE'});await loadDevices();showStatus('Пристрій видалено');}

async function loadChat(){try{const r=await req('/chat/admin');const d=await r.json();const tb=document.getElementById('chatMessages');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="5" class="empty">Запитань поки немає.</td></tr>';return;}d.forEach(x=>{const answered=!!x.answer;const dateStr=x.created_at?new Date(x.created_at).toLocaleString('uk-UA'):'';tb.innerHTML+=`<tr><td>${escapeHtml(dateStr)}</td><td><b>${escapeHtml(x.question)}</b><br><span class="muted">${escapeHtml(x.client_id)}</span></td><td>${answered?escapeHtml(x.answer):'<textarea id="a_'+x.id+'" rows="2" placeholder="Введіть відповідь"></textarea>'}</td><td>${answered?'<span class="pill pill-ok">Відповідь надано</span>':'<span class="pill pill-wait">Очікує</span>'}</td><td><div class="actions">${answered?'<button class="btn-edit" onclick="editAnswer(\''+x.id+'\',\''+jsArg(x.answer)+'\')">Редагувати</button>':''}<button class="btn-green" onclick="answerChat(\''+x.id+'\',document.getElementById(\'a_'+x.id+'\')?.value)">Відповісти</button><button class="btn-red" onclick="deleteMessage(\''+x.id+'\')">Видалити</button></div></td></tr>`});}catch(e){showStatus('Не вдалося завантажити чат',false);}}
async function editAnswer(id,current){const newA=prompt('Редагувати відповідь:',current);if(newA)await answerChat(id,newA);}
async function answerChat(id,answer){if(!answer||!answer.trim()){showStatus('Введіть відповідь',false);return;}try{const r=await req(`/chat/${id}/answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answer:answer.trim()})});if(!r.ok)throw new Error();await loadChat();showStatus('Відповідь збережено');}catch(e){showStatus('Помилка',false);}}
async function deleteMessage(id){if(!confirm('Видалити повідомлення?'))return;await req(`/chat/${id}`,{method:'DELETE'});await loadChat();showStatus('Повідомлення видалено');}

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
