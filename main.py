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

Base.metadata.create_all(engine)

def ensure_schema():
    for stmt in ["ALTER TABLE events ADD COLUMN link VARCHAR DEFAULT ''"]:
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

app = FastAPI(title="Доброчесність API", version="0.3.0", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["capacitor://localhost", "http://localhost", "https://app-dobrochesnist.onrender.com"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def admin_guard(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()
    protected = (
        path.startswith("/admin") or path.startswith("/stats") or path.startswith("/users")
        or (path.startswith("/events") and method in {"POST", "PUT", "DELETE", "PATCH"} and not path.endswith("/view"))
        or (path.startswith("/reference") and method in {"POST", "PUT", "DELETE", "PATCH"})
    )
    if protected:
        db = SessionLocal()
        try:
            if not get_session_user(request, db):
                if path.startswith("/admin") or path.startswith("/stats") or path.startswith("/users"):
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
    db = SessionLocal()
    try:
        if db.query(Event).count() == 0:
            today = date.today()
            db.add_all([
                Event(id="e1", cat="declaration", title="Подання щорічної декларації", date=today + timedelta(days=12), recur="Щороку, до 1 квітня", description="Подати щорічну декларацію через Реєстр декларацій.", instruction="Перевірте: доходи, нерухомість, транспорт, корпоративні права, рахунки.", audience="Усі працівники", link="", reminders=[30,10,3,0], views=184),
                Event(id="e2", cat="training", title="Щорічне навчання з доброчесності", date=today + timedelta(days=3), recur="Щороку", description="Пройти онлайн-курс із запобігання конфлікту інтересів.", instruction="Курс триває ~40 хв. Сертифікат завантажується у профіль.", audience="Усі працівники", link="", reminders=[10,3,0], views=97),
            ])
            db.commit()
        if db.query(Reference).count() == 0:
            db.add_all([
                Reference(id="r1", title="Конфлікт інтересів", description="Якщо приватний інтерес впливає на службові рішення — повідомте керівника та утримайтесь від дій до врегулювання.", link=""),
                Reference(id="r2", title="Подарунки", description="Подарунки у зв'язку зі службою обмежені за вартістю. Те, що перевищує межу, передається органу.", link=""),
            ])
            db.commit()
    finally:
        db.close()
seed()

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
<!DOCTYPE html><html lang="uk"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Адмін-панель | Доброчесність</title>
<style>:root{--bg:#EBEEF0;--surface:#FFFFFF;--ink:#1B2430;--soft:#5A6577;--line:#DCE1E6;--accent:#1F5673;--red:#9E2F3C;--amber:#A9690A;--green:#2C6A4E}*{box-sizing:border-box}body{font-family:Arial,sans-serif;background:var(--bg);margin:0;padding:24px;color:var(--ink)}.wrap{max-width:1200px;margin:auto}.box{background:var(--surface);padding:24px;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.06);margin-bottom:18px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:18px}h1{margin:0;color:var(--accent);font-size:26px}h2{margin:0 0 14px;color:var(--accent);font-size:20px}.links a{color:var(--accent);margin-left:12px;text-decoration:none;font-weight:600}label{display:block;margin-bottom:5px;color:var(--soft);font-size:13px;font-weight:700}input,textarea,select{width:100%;padding:10px 12px;margin:0 0 14px;border:1px solid #cfd6dd;border-radius:10px;font-size:14px}button{padding:10px 14px;border:0;border-radius:10px;cursor:pointer;background:var(--accent);color:white;font-weight:700}button.del{background:var(--red)}button.edit{background:var(--amber)}button.gray{background:#5A6577}button.green{background:var(--green)}.row{display:grid;grid-template-columns:1fr 180px 220px;gap:14px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}.status{padding:10px 12px;border-radius:10px;margin:12px 0;display:none}.ok{background:#E1EEE8;color:var(--green);display:block}.err{background:#F5E2E4;color:var(--red);display:block}table{width:100%;border-collapse:collapse;margin-top:18px}th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:14px}th{background:#f1f5f7;color:var(--soft);font-size:12px;text-transform:uppercase}.muted{color:var(--soft);font-size:12px}.pill{display:inline-block;padding:4px 8px;border-radius:999px;background:#E6EEF2;color:var(--accent);font-size:12px;font-weight:700}.table-actions{display:flex;gap:6px;flex-wrap:wrap}a.lnk{color:var(--accent);font-size:12px;word-break:break-all}@media(max-width:800px){body{padding:10px}.box{padding:16px}.row{grid-template-columns:1fr;gap:0}table,thead,tbody,th,td,tr{display:block}thead{display:none}tr{border:1px solid var(--line);border-radius:12px;margin-bottom:10px;padding:8px}td{border-bottom:0;padding:6px}}</style></head>
<body><div class="wrap"><div class="box"><div class="top"><div><h1>Адмін-панель “Доброчесність”</h1><div class="muted">Керування подіями, довідником і адміністраторами</div></div><div class="links"><a href="/events" target="_blank">/events</a><a href="/reference" target="_blank">/reference</a><a href="/stats" target="_blank">/stats</a><a href="/logout">Вийти</a></div></div><div id="status" class="status"></div><input type="hidden" id="eventId"><div class="row"><div><label>Назва події</label><input id="title"></div><div><label>Дата</label><input id="date" type="date"></div><div><label>Категорія</label><select id="cat"><option value="declaration">Декларування</option><option value="conflict">Конфлікт інтересів</option><option value="gifts">Подарунки</option><option value="notice">Повідомлення</option><option value="training">Навчання</option><option value="restriction">Обмеження</option></select></div></div><label>Повторюваність</label><input id="recur"><label>Опис</label><textarea id="description" rows="3"></textarea><label>Інструкція для користувача</label><textarea id="instruction" rows="3"></textarea><label>Посилання</label><input id="link"><label>Аудиторія</label><input id="audience" value="Усі працівники"><label>Нагадування, днів до події</label><input id="reminders" value="30,10,3,0"><div class="actions"><button onclick="saveEvent()">Зберегти подію</button><button class="gray" onclick="clearForm()">Очистити</button><button class="green" onclick="loadEvents()">Оновити список</button></div><table><thead><tr><th>ID</th><th>Дата</th><th>Назва</th><th>Категорія</th><th>Посилання</th><th>Перегляди</th><th>Дії</th></tr></thead><tbody id="events"></tbody></table></div><div class="box"><h2>Довідник</h2><input type="hidden" id="refId"><label>Назва</label><input id="refTitle"><label>Опис</label><textarea id="refDescription" rows="3"></textarea><label>Посилання</label><input id="refLink"><div class="actions"><button onclick="saveRef()">Зберегти запис</button><button class="gray" onclick="clearRefForm()">Очистити</button><button class="green" onclick="loadRefs()">Оновити список</button></div><table><thead><tr><th>Назва</th><th>Опис</th><th>Посилання</th><th>Дії</th></tr></thead><tbody id="refs"></tbody></table></div><div class="box"><h2>Адміністратори</h2><label>Новий логін</label><input id="newUser"><label>Пароль нового адміністратора</label><input id="newPass" type="password"><div class="actions"><button onclick="createUser()">Створити адміністратора</button><button class="green" onclick="loadUsers()">Оновити список</button></div><table><thead><tr><th>Логін</th><th>Статус</th><th>Останній вхід</th><th>Дії</th></tr></thead><tbody id="users"></tbody></table></div></div>
<script>
function showStatus(t,ok=true){const e=document.getElementById('status');e.className='status '+(ok?'ok':'err');e.textContent=t;e.style.display='block';window.scrollTo({top:0,behavior:'smooth'});setTimeout(()=>e.style.display='none',5000)}
function escapeHtml(v){return String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;')}
async function req(u,o={}){const r=await fetch(u,o);if(r.redirected&&r.url.includes('/login'))location.href='/login';return r}
async function loadEvents(){try{const r=await req('/events');if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json(),tb=document.getElementById('events');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="7">Подій поки немає</td></tr>';return}d.forEach(ev=>{const tr=document.createElement('tr'),lc=ev.link?`<a class="lnk" href="${escapeHtml(ev.link)}" target="_blank">${escapeHtml(ev.link)}</a>`:'<span class="muted">—</span>';tr.innerHTML=`<td><span class="muted">${escapeHtml(ev.id)}</span></td><td>${escapeHtml(ev.date)}</td><td><b>${escapeHtml(ev.title)}</b><div class="muted">${escapeHtml(ev.description)}</div></td><td><span class="pill">${escapeHtml(ev.cat)}</span></td><td>${lc}</td><td>${escapeHtml(ev.views??0)}</td><td><div class="table-actions"><button class="edit">Редагувати</button><button class="del">Видалити</button></div></td>`;tr.querySelector('.edit').onclick=()=>editEvent(ev);tr.querySelector('.del').onclick=()=>deleteEvent(ev.id);tb.appendChild(tr)})}catch(e){showStatus('Не вдалося завантажити події: '+e.message,false)}}
function editEvent(ev){['eventId','title','date','cat','recur','description','instruction','link','audience'].forEach(id=>{document.getElementById(id).value=ev[id]||''});document.getElementById('cat').value=ev.cat||'declaration';document.getElementById('audience').value=ev.audience||'Усі працівники';document.getElementById('reminders').value=(ev.reminders||[30,10,3,0]).join(',');window.scrollTo({top:0,behavior:'smooth'})}
function clearForm(){['eventId','title','date','recur','description','instruction','link'].forEach(id=>document.getElementById(id).value='');document.getElementById('cat').value='declaration';document.getElementById('audience').value='Усі працівники';document.getElementById('reminders').value='30,10,3,0'}
async function saveEvent(){const id=document.getElementById('eventId').value.trim(),p={title:document.getElementById('title').value.trim(),date:document.getElementById('date').value,cat:document.getElementById('cat').value,recur:document.getElementById('recur').value.trim(),description:document.getElementById('description').value.trim(),instruction:document.getElementById('instruction').value.trim(),link:document.getElementById('link').value.trim(),audience:document.getElementById('audience').value.trim()||'Усі працівники',reminders:document.getElementById('reminders').value.split(',').map(x=>Number(x.trim())).filter(x=>!isNaN(x))};if(!p.title||!p.date||!p.cat){showStatus('Заповни назву, дату та категорію',false);return}try{const r=await req(id?`/events/${id}`:'/events',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());clearForm();await loadEvents();showStatus(id?'Подію оновлено':'Подію створено')}catch(e){showStatus('Помилка збереження: '+e.message,false)}}
async function deleteEvent(id){if(!confirm('Видалити цю подію?'))return;try{const r=await req(`/events/${id}`,{method:'DELETE'});if(!r.ok&&r.status!==204)throw new Error('HTTP '+r.status+' '+await r.text());await loadEvents();showStatus('Подію видалено')}catch(e){showStatus('Помилка видалення: '+e.message,false)}}
async function loadRefs(){try{const r=await req('/reference');if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json(),tb=document.getElementById('refs');tb.innerHTML='';if(!d.length){tb.innerHTML='<tr><td colspan="4">Записів поки немає</td></tr>';return}d.forEach(x=>{const tr=document.createElement('tr'),lc=x.link?`<a class="lnk" href="${escapeHtml(x.link)}" target="_blank">${escapeHtml(x.link)}</a>`:'<span class="muted">—</span>';tr.innerHTML=`<td><b>${escapeHtml(x.title)}</b></td><td><div class="muted">${escapeHtml(x.description)}</div></td><td>${lc}</td><td><div class="table-actions"><button class="edit">Редагувати</button><button class="del">Видалити</button></div></td>`;tr.querySelector('.edit').onclick=()=>editRef(x);tr.querySelector('.del').onclick=()=>deleteRef(x.id);tb.appendChild(tr)})}catch(e){showStatus('Не вдалося завантажити довідник: '+e.message,false)}}
function editRef(r){document.getElementById('refId').value=r.id||'';document.getElementById('refTitle').value=r.title||'';document.getElementById('refDescription').value=r.description||'';document.getElementById('refLink').value=r.link||''}
function clearRefForm(){['refId','refTitle','refDescription','refLink'].forEach(id=>document.getElementById(id).value='')}
async function saveRef(){const id=document.getElementById('refId').value.trim(),p={title:document.getElementById('refTitle').value.trim(),description:document.getElementById('refDescription').value.trim(),link:document.getElementById('refLink').value.trim()};if(!p.title){showStatus('Заповни назву запису довідника',false);return}try{const r=await req(id?`/reference/${id}`:'/reference',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());clearRefForm();await loadRefs();showStatus(id?'Запис оновлено':'Запис створено')}catch(e){showStatus('Помилка збереження: '+e.message,false)}}
async function deleteRef(id){if(!confirm('Видалити цей запис довідника?'))return;try{const r=await req(`/reference/${id}`,{method:'DELETE'});if(!r.ok&&r.status!==204)throw new Error('HTTP '+r.status+' '+await r.text());await loadRefs();showStatus('Запис видалено')}catch(e){showStatus('Помилка видалення: '+e.message,false)}}
async function loadUsers(){try{const r=await req('/users');if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json(),tb=document.getElementById('users');tb.innerHTML='';d.forEach(u=>{const tr=document.createElement('tr');tr.innerHTML=`<td><b>${escapeHtml(u.username)}</b><div class="muted">${escapeHtml(u.id)}</div></td><td>${u.is_active?'<span class="pill">Активний</span>':'<span class="muted">Заблокований</span>'}</td><td>${escapeHtml(u.last_login_at||'—')}</td><td><div class="table-actions"><button class="edit">Змінити пароль</button><button class="del">${u.is_active?'Заблокувати':'Активувати'}</button></div></td>`;tr.querySelector('.edit').onclick=()=>changeUserPassword(u.id);tr.querySelector('.del').onclick=()=>toggleUser(u.id);tb.appendChild(tr)})}catch(e){showStatus('Не вдалося завантажити користувачів: '+e.message,false)}}
async function createUser(){const username=document.getElementById('newUser').value.trim(),password=document.getElementById('newPass').value;if(!username||password.length<8){showStatus('Логін обов’язковий, пароль мінімум 8 символів',false);return}try{const r=await req('/users',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({username,password})});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());document.getElementById('newUser').value='';document.getElementById('newPass').value='';await loadUsers();showStatus('Адміністратора створено')}catch(e){showStatus('Помилка створення адміністратора: '+e.message,false)}}
async function changeUserPassword(id){const password=prompt('Новий пароль мінімум 8 символів:');if(!password)return;if(password.length<8){showStatus('Пароль мінімум 8 символів',false);return}try{const r=await req(`/users/${id}/password`,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({password})});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());showStatus('Пароль змінено')}catch(e){showStatus('Помилка зміни пароля: '+e.message,false)}}
async function toggleUser(id){if(!confirm('Змінити статус адміністратора?'))return;try{const r=await req(`/users/${id}/toggle`,{method:'POST'});if(!r.ok)throw new Error('HTTP '+r.status+' '+await r.text());await loadUsers();showStatus('Статус змінено')}catch(e){showStatus('Помилка зміни статусу: '+e.message,false)}}
loadEvents();loadRefs();loadUsers();
</script></body></html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
