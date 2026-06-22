"""
Код Доброчесності — бекенд + API + адмін-панель.
Додано: чат, push-повідомлення, імпорт подій з Excel.
Excel: колонка A = дата, B = подія, C = посилання.
"""
import os
import uuid
import secrets
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, update, String, Integer, Date, DateTime, JSON, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dobrochesnist.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
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
    link: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(String, default="Усі працівники")
    reminders: Mapped[list] = mapped_column(JSON, default=list)
    views: Mapped[int] = mapped_column(Integer, default=0)

class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    token: Mapped[str] = mapped_column(Text, unique=True, index=True)
    platform: Mapped[str] = mapped_column(String, default="android")
    app_version: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(String, index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

class PushLog(Base):
    __tablename__ = "push_logs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    configured: Mapped[int] = mapped_column(Integer, default=0)

Base.metadata.create_all(engine)

def ensure_schema():
    # Просте оновлення старої SQLite-бази без Alembic.
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(events)"))]
        if "link" not in cols:
            conn.execute(text("ALTER TABLE events ADD COLUMN link TEXT DEFAULT ''"))
ensure_schema()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
if not ADMIN_TOKEN:
    ADMIN_TOKEN = secrets.token_urlsafe(24)
    print("\n" + "=" * 64)
    print("ADMIN_TOKEN не задано — тимчасовий токен:")
    print(ADMIN_TOKEN)
    print("Для Render задай ADMIN_TOKEN у Environment Variables")
    print("=" * 64 + "\n")

def require_admin(authorization: str = Header(default="")):
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, "Потрібна авторизація адміністратора", headers={"WWW-Authenticate":"Bearer"})

class EventIn(BaseModel):
    cat: str = "imported"
    title: str
    date: date
    recur: str = ""
    description: str = ""
    instruction: str = ""
    link: str = ""
    audience: str = "Усі працівники"
    reminders: List[int] = []

class EventOut(EventIn):
    id: str
    views: int
    class Config:
        from_attributes = True

class DeviceIn(BaseModel):
    token: str
    platform: str = "android"
    app_version: str = ""

class ChatIn(BaseModel):
    client_id: str
    question: str

class AnswerIn(BaseModel):
    answer: str

class PushIn(BaseModel):
    title: str
    body: str

app = FastAPI(title="Код Доброчесності API", version="0.3.0")
_origins_env = os.getenv("ALLOWED_ORIGINS", "*").strip()
allow_origins = ["*"] if _origins_env == "*" else [o.strip() for o in _origins_env.split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=allow_origins, allow_methods=["*"], allow_headers=["*"])

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
    db.execute(update(Event).where(Event.id == event_id).values(views=Event.views + 1))
    db.commit(); db.refresh(ev)
    return ev

@app.post("/events", response_model=EventOut, status_code=201, dependencies=[Depends(require_admin)])
def create_event(data: EventIn, db: Session = Depends(get_db)):
    ev = Event(id=f"e{uuid.uuid4().hex[:8]}", views=0, **data.model_dump())
    db.add(ev); db.commit(); db.refresh(ev)
    return ev

@app.put("/events/{event_id}", response_model=EventOut, dependencies=[Depends(require_admin)])
def update_event(event_id: str, data: EventIn, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    for k, v in data.model_dump().items():
        setattr(ev, k, v)
    db.commit(); db.refresh(ev)
    return ev

@app.delete("/events/{event_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_event(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    db.delete(ev); db.commit()


def parse_excel_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            from openpyxl.utils.datetime import from_excel
            return from_excel(value).date()
        except Exception:
            return None
    s = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None

@app.post("/events/import-excel", dependencies=[Depends(require_admin)])
async def import_events_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Завантажте файл Excel .xlsx")
    try:
        from openpyxl import load_workbook
    except Exception:
        raise HTTPException(500, "На сервері не встановлено openpyxl. Додайте openpyxl у requirements.txt")
    try:
        wb = load_workbook(file.file, data_only=True)
        ws = wb.active
    except Exception as e:
        raise HTTPException(400, f"Не вдалося прочитати Excel: {e}")
    created, skipped = 0, []
    for idx, row in enumerate(ws.iter_rows(min_row=1, values_only=True), start=1):
        raw_date = row[0] if len(row) > 0 else None
        raw_title = row[1] if len(row) > 1 else None
        raw_link = row[2] if len(row) > 2 else ""
        ev_date = parse_excel_date(raw_date)
        title = str(raw_title or "").strip()
        link = str(raw_link or "").strip()
        # Автоматично пропускаємо заголовок або пусті рядки.
        if not ev_date or not title:
            if raw_date or raw_title or raw_link:
                skipped.append({"row": idx, "reason": "немає дати або назви"})
            continue
        ev = Event(
            id=f"e{uuid.uuid4().hex[:8]}", cat="imported", title=title, date=ev_date,
            recur="", description=(f"Посилання: {link}" if link else ""), instruction=link,
            link=link, audience="Усі працівники", reminders=[30,10,3,0], views=0
        )
        db.add(ev); created += 1
    db.commit()
    return {"created": created, "skipped": skipped[:50], "skipped_count": len(skipped)}

@app.post("/devices/register")
def register_device(data: DeviceIn, db: Session = Depends(get_db)):
    token = data.token.strip()
    if not token:
        raise HTTPException(400, "Порожній token")
    dev = db.query(Device).filter(Device.token == token).first()
    if not dev:
        dev = Device(id=f"d{uuid.uuid4().hex[:10]}", token=token)
        db.add(dev)
    dev.platform = data.platform
    dev.app_version = data.app_version
    dev.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "device_id": dev.id}


def send_push_to_tokens(tokens: List[str], title: str, body: str):
    # Працює, якщо встановлено firebase-admin і задано FIREBASE_SERVICE_ACCOUNT_JSON.
    service_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if not service_json:
        return {"configured": False, "sent": 0, "failed": 0, "error": "FIREBASE_SERVICE_ACCOUNT_JSON не задано"}
    try:
        import json
        import firebase_admin
        from firebase_admin import credentials, messaging
        if not firebase_admin._apps:
            cred = credentials.Certificate(json.loads(service_json))
            firebase_admin.initialize_app(cred)
        sent = failed = 0
        for token in tokens:
            try:
                messaging.send(messaging.Message(notification=messaging.Notification(title=title, body=body), token=token))
                sent += 1
            except Exception:
                failed += 1
        return {"configured": True, "sent": sent, "failed": failed}
    except Exception as e:
        return {"configured": False, "sent": 0, "failed": len(tokens), "error": str(e)}

@app.post("/push/send", dependencies=[Depends(require_admin)])
def push_send(data: PushIn, db: Session = Depends(get_db)):
    devices = db.query(Device).all()
    tokens = [d.token for d in devices if d.token]
    result = send_push_to_tokens(tokens, data.title, data.body)
    log = PushLog(id=f"p{uuid.uuid4().hex[:8]}", title=data.title, body=data.body,
                  sent=result.get("sent",0), failed=result.get("failed",0), configured=1 if result.get("configured") else 0)
    db.add(log); db.commit()
    result["devices"] = len(tokens)
    return result

@app.post("/chat")
def chat_create(data: ChatIn, db: Session = Depends(get_db)):
    question = data.question.strip()
    if not question:
        raise HTTPException(400, "Порожнє питання")
    msg = ChatMessage(id=f"c{uuid.uuid4().hex[:10]}", client_id=data.client_id.strip() or "unknown", question=question)
    db.add(msg); db.commit()
    return {"ok": True, "id": msg.id}

@app.get("/chat/{client_id}")
def chat_for_client(client_id: str, db: Session = Depends(get_db)):
    items = db.query(ChatMessage).filter(ChatMessage.client_id == client_id).order_by(ChatMessage.created_at.desc()).limit(50).all()
    return [{"id":m.id,"question":m.question,"answer":m.answer,"status":m.status,"created_at":m.created_at.isoformat(),"answered_at":m.answered_at.isoformat() if m.answered_at else None} for m in items]

@app.get("/admin/chat", dependencies=[Depends(require_admin)])
def admin_chat(db: Session = Depends(get_db)):
    items = db.query(ChatMessage).order_by(ChatMessage.created_at.desc()).limit(200).all()
    return [{"id":m.id,"client_id":m.client_id,"question":m.question,"answer":m.answer,"status":m.status,"created_at":m.created_at.isoformat(),"answered_at":m.answered_at.isoformat() if m.answered_at else None} for m in items]

@app.post("/admin/chat/{message_id}/answer", dependencies=[Depends(require_admin)])
def answer_chat(message_id: str, data: AnswerIn, db: Session = Depends(get_db)):
    msg = db.get(ChatMessage, message_id)
    if not msg:
        raise HTTPException(404, "Повідомлення не знайдено")
    msg.answer = data.answer.strip()
    msg.status = "answered" if msg.answer else "new"
    msg.answered_at = datetime.utcnow() if msg.answer else None
    db.commit()
    return {"ok": True}

@app.delete("/admin/chat/{message_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_chat(message_id: str, db: Session = Depends(get_db)):
    msg = db.get(ChatMessage, message_id)
    if not msg:
        raise HTTPException(404, "Повідомлення не знайдено")
    db.delete(msg); db.commit()

@app.get("/stats", dependencies=[Depends(require_admin)])
def stats(db: Session = Depends(get_db)):
    events = db.query(Event).all()
    chat_new = db.query(ChatMessage).filter(ChatMessage.status == "new").count()
    devices = db.query(Device).count()
    return {"events":len(events),"views":sum(e.views for e in events),"categories":len({e.cat for e in events}),"devices":devices,"chat_new":chat_new,
            "by_event":[{"id":e.id,"title":e.title,"cat":e.cat,"views":e.views} for e in sorted(events, key=lambda e:e.views, reverse=True)]}

def seed():
    db = SessionLocal()
    try:
        if db.query(Event).count() > 0:
            return
        today = date.today()
        db.add_all([
            Event(id="e1", cat="declaration", title="Подання щорічної декларації", date=today+timedelta(days=12), recur="Щороку, до 1 квітня", description="Подати щорічну декларацію через Реєстр декларацій.", instruction="Перевірте доходи, майно, транспорт та корпоративні права.", link="", audience="Усі працівники", reminders=[30,10,3,0], views=184),
            Event(id="e2", cat="training", title="Щорічне навчання з доброчесності", date=today+timedelta(days=3), recur="Щороку", description="Пройти онлайн-курс із запобігання конфлікту інтересів.", instruction="Після проходження збережіть сертифікат.", link="", audience="Усі працівники", reminders=[10,3,0], views=97),
        ])
        db.commit()
    finally:
        db.close()
seed()

@app.get("/", response_class=HTMLResponse)
def root():
    return """
<!doctype html><html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Код Доброчесності</title>
<style>:root{--n:#102f44;--b:#1f5673;--g:#f2b134;--bg:#f4f7fb;--i:#17212f;--m:#667085}*{box-sizing:border-box}body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:radial-gradient(circle at top left,#deedf5,#f7f9fc 45%,#edf3f8);color:var(--i)}.hero{max-width:1120px;margin:auto;min-height:100vh;display:grid;grid-template-columns:1.1fr .9fr;gap:22px;align-items:center;padding:28px}.card{background:rgba(255,255,255,.84);border:1px solid #fff;border-radius:30px;box-shadow:0 24px 70px rgba(16,47,68,.14);backdrop-filter:blur(14px)}.intro{padding:42px}.brand{display:flex;gap:14px;align-items:center;margin-bottom:30px}.logo{width:58px;height:58px;border-radius:18px;background:linear-gradient(145deg,var(--n),var(--b));display:grid;place-items:center;color:#fff;font-weight:950;font-size:22px}.brand b{font-size:18px}.brand span{display:block;color:var(--m);font-size:13px}h1{font-size:48px;line-height:1.04;margin:0 0 16px;color:var(--n);letter-spacing:-1.4px}.lead{font-size:17px;line-height:1.7;color:#475467;margin:0 0 26px}.actions{display:flex;gap:12px;flex-wrap:wrap}.btn{padding:13px 18px;border-radius:15px;text-decoration:none;font-weight:850}.primary{background:var(--b);color:white}.light{background:white;color:var(--b);border:1px solid #e6ebf1}.preview{padding:20px}.phone{background:#0e2d42;color:#fff;border-radius:28px;padding:20px;min-height:410px}.mini{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.12);border-radius:18px;padding:16px;margin-bottom:12px}.num{font-size:32px;font-weight:950;color:var(--g)}@media(max-width:850px){.hero{grid-template-columns:1fr}.preview{display:none}h1{font-size:36px}.intro{padding:26px}}</style></head>
<body><main class="hero"><section class="intro card"><div class="brand"><div class="logo">КД</div><div><b>Код Доброчесності</b><span>Твій цифровий орієнтир</span></div></div><h1>API, адмін-панель, чат і push-повідомлення</h1><p class="lead">Сервіс працює. Адміністратор керує подіями, імпортує Excel, відповідає в чаті та надсилає push-повідомлення на мобільний додаток.</p><div class="actions"><a class="btn primary" href="/admin">Відкрити адмін-панель</a><a class="btn light" href="/docs">Swagger /docs</a><a class="btn light" href="/events">Події JSON</a></div></section><aside class="preview card"><div class="phone"><div class="mini"><b>Найближча подія</b><p>Подання декларації</p></div><div class="mini"><div class="num">Excel</div><p>A дата • B подія • C посилання</p></div><div class="mini"><b>Модулі</b><p>Події • Чат • Push • Статистика</p></div></div></aside></main></body></html>
"""

@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    return r"""
<!doctype html><html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Адмін-панель | Код Доброчесності</title>
<style>
:root{--bg:#f4f7fb;--side:#102f44;--side2:#1f5673;--card:#fff;--ink:#17212f;--mut:#667085;--line:#e7edf3;--gold:#f2b134;--green:#1f8a5b;--red:#b42335;--orange:#b86a00;--shadow:0 18px 45px rgba(16,47,68,.11);--r:22px}*{box-sizing:border-box}body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink)}.app{display:grid;grid-template-columns:280px 1fr;min-height:100vh}.sidebar{background:linear-gradient(180deg,var(--side),#0b2536);color:#fff;padding:24px;position:sticky;top:0;height:100vh;overflow:auto}.brand{display:flex;align-items:center;gap:13px;margin-bottom:28px}.logo{width:54px;height:54px;border-radius:17px;background:linear-gradient(145deg,var(--gold),#ffd77a);display:grid;place-items:center;color:#102f44;font-weight:950;font-size:20px}.brand span{display:block;color:rgba(255,255,255,.65);font-size:12px;margin-top:3px}.nav{display:grid;gap:8px}.nav button{width:100%;border:0;border-radius:15px;padding:13px 14px;text-align:left;background:transparent;color:rgba(255,255,255,.72);font-weight:850;cursor:pointer}.nav button.active,.nav button:hover{background:rgba(255,255,255,.11);color:#fff}.note{margin-top:24px;padding:15px;border-radius:18px;background:rgba(255,255,255,.08);font-size:13px;color:rgba(255,255,255,.72);line-height:1.45}.main{padding:28px;min-width:0}.top{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:20px}.top h1{margin:0;color:#102f44;font-size:30px}.top p{margin:6px 0 0;color:var(--mut)}.quick{display:flex;gap:10px;flex-wrap:wrap}.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}.grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:14px;margin-bottom:18px}.stat{padding:17px}.label{color:var(--mut);font-size:12px;font-weight:900}.value{font-size:30px;font-weight:950;margin-top:8px;color:#102f44}.panel{padding:18px;margin-bottom:18px}.head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}.head h2{margin:0;font-size:18px}.toolbar{display:grid;grid-template-columns:1fr 210px 160px;gap:10px;margin-bottom:14px}input,textarea,select{width:100%;border:1px solid #d7dee7;background:#fff;border-radius:14px;padding:12px 13px;font-size:14px;color:var(--ink);outline:none}textarea{resize:vertical}label{display:block;color:#475467;font-size:12px;font-weight:900;margin:0 0 7px}.btn{border:0;border-radius:14px;padding:12px 15px;font-weight:850;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:8px;background:var(--side2);color:#fff}.gray{background:#eef3f7;color:#344054}.gold{background:var(--gold);color:#102f44}.red{background:var(--red);color:#fff}.green{background:var(--green);color:#fff}.orange{background:var(--orange);color:#fff}.status{display:none;padding:12px 14px;border-radius:14px;margin-bottom:14px;font-weight:800}.ok{display:block;background:#e7f6ef;color:var(--green)}.err{display:block;background:#fde7ea;color:var(--red)}.table{overflow:auto;border:1px solid var(--line);border-radius:18px}table{width:100%;border-collapse:collapse;background:#fff}th,td{padding:13px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:14px}th{background:#f8fafc;color:#667085;font-size:12px;text-transform:uppercase}.muted{color:var(--mut);font-size:12px;margin-top:4px}.pill{display:inline-flex;border-radius:999px;padding:5px 9px;background:#eaf2f6;color:var(--side2);font-size:12px;font-weight:900}.soon{background:#fff4db;color:#9a6400}.today{background:#ffe8ea;color:var(--red)}.actions{display:flex;gap:8px;flex-wrap:wrap}.hidden{display:none!important}.modal{position:fixed;inset:0;background:rgba(16,47,68,.48);display:none;align-items:center;justify-content:center;padding:18px;z-index:10}.modal.open{display:flex}.modalbox{width:min(780px,100%);max-height:92vh;overflow:auto;background:#fff;border-radius:26px;padding:20px;box-shadow:0 35px 80px rgba(0,0,0,.28)}.formgrid{display:grid;grid-template-columns:1fr 180px 210px;gap:12px}.chatitem{border:1px solid var(--line);border-radius:18px;padding:14px;margin-bottom:10px;background:#fff}.chatq{font-weight:900}.chata{margin-top:8px;color:#344054;background:#f8fafc;border-radius:14px;padding:10px}.excelbox{border:1px dashed #b8c4cf;background:#fbfdff;border-radius:18px;padding:16px}.bar{height:12px;background:#eef3f7;border-radius:999px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--side2),var(--gold))}@media(max-width:980px){.app{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.grid{grid-template-columns:repeat(2,1fr)}.toolbar,.formgrid{grid-template-columns:1fr}}@media(max-width:560px){.main{padding:14px}.grid{grid-template-columns:1fr}th:nth-child(1),td:nth-child(1){white-space:nowrap}}
</style></head><body><div class="app"><aside class="sidebar"><div class="brand"><div class="logo">КД</div><div><b>Код Доброчесності</b><span>Твій цифровий орієнтир</span></div></div><nav class="nav"><button class="active" data-tab="dashboard">📊 Огляд</button><button data-tab="events">📅 Події</button><button data-tab="excel">📥 Excel імпорт</button><button data-tab="chat">💬 Чат</button><button data-tab="push">🔔 Push</button><button data-tab="stats">📈 Статистика</button><button data-tab="settings">⚙️ Налаштування</button></nav><div class="note">Excel: A — дата, B — подія, C — посилання. Формат дати: 22.06.2026 або 2026-06-22.</div></aside><main class="main"><div class="top"><div><h1>Адмін-панель</h1><p>Події, чат, push-повідомлення та завантаження з Excel.</p></div><div class="quick"><button class="btn gold" id="btnCreateTop">+ Нова подія</button><a class="btn gray" href="/docs" target="_blank">/docs</a></div></div><div id="status" class="status"></div>
<section id="dashboard" class="tab"><div class="grid"><div class="stat card"><div class="label">Події</div><div class="value" id="stEvents">0</div></div><div class="stat card"><div class="label">Перегляди</div><div class="value" id="stViews">0</div></div><div class="stat card"><div class="label">Категорії</div><div class="value" id="stCats">0</div></div><div class="stat card"><div class="label">Нові чати</div><div class="value" id="stChat">0</div></div><div class="stat card"><div class="label">Пристрої</div><div class="value" id="stDevices">0</div></div></div><div class="panel card"><div class="head"><h2>Найближчі події</h2><button class="btn gray" id="reload1">Оновити</button></div><div class="table"><table><thead><tr><th>Дата</th><th>Подія</th><th>Категорія</th><th>Статус</th></tr></thead><tbody id="nearestBody"></tbody></table></div></div></section>
<section id="events" class="tab hidden"><div class="panel card"><div class="head"><h2>Події</h2><div class="actions"><button class="btn gold" id="btnCreate">+ Створити</button><button class="btn gray" id="reload2">Оновити</button></div></div><div class="toolbar"><input id="search" placeholder="Пошук подій"><select id="filterCat"><option value="">Усі категорії</option><option value="declaration">Декларування</option><option value="conflict">Конфлікт інтересів</option><option value="gifts">Подарунки</option><option value="notice">Повідомлення</option><option value="training">Навчання</option><option value="restriction">Обмеження</option><option value="imported">Excel</option></select><select id="sort"><option value="date">За датою</option><option value="views">За переглядами</option><option value="title">За назвою</option></select></div><div class="table"><table><thead><tr><th>Дата</th><th>Подія</th><th>Категорія</th><th>Перегляди</th><th>Дії</th></tr></thead><tbody id="eventsBody"></tbody></table></div></div></section>
<section id="excel" class="tab hidden"><div class="panel card"><div class="head"><h2>Завантаження подій з Excel</h2></div><div class="excelbox"><p><b>Структура файлу:</b> A — дата, B — подія, C — посилання.</p><input type="file" id="excelFile" accept=".xlsx,.xlsm"><br><br><button class="btn green" id="btnExcel">Завантажити Excel у події</button></div><div class="muted" id="excelResult"></div></div></section>
<section id="chat" class="tab hidden"><div class="panel card"><div class="head"><h2>Чат користувачів</h2><button class="btn gray" id="reloadChat">Оновити чат</button></div><div id="chatList"></div></div></section>
<section id="push" class="tab hidden"><div class="panel card"><div class="head"><h2>Push-повідомлення</h2></div><label>Заголовок</label><input id="pushTitle" placeholder="Наприклад: Нагадування"><label>Текст повідомлення</label><textarea id="pushBody" rows="4" placeholder="Текст push-повідомлення"></textarea><button class="btn green" id="btnPush">Надіслати всім пристроям</button><div class="muted" id="pushResult"></div></div></section>
<section id="stats" class="tab hidden"><div class="panel card"><div class="head"><h2>Популярність подій</h2></div><div class="table"><table><thead><tr><th>Подія</th><th>Категорія</th><th>Перегляди</th><th>Графік</th></tr></thead><tbody id="statsBody"></tbody></table></div></div></section>
<section id="settings" class="tab hidden"><div class="panel card"><div class="head"><h2>Налаштування</h2></div><label>ADMIN_TOKEN</label><input id="token" type="password" placeholder="Встав ADMIN_TOKEN"><br><br><button class="btn green" id="saveToken">Запамʼятати токен</button> <button class="btn red" id="clearToken">Очистити</button><p class="muted">Токен зберігається тільки в цій вкладці браузера.</p></div></section>
</main></div>
<div class="modal" id="modal"><div class="modalbox"><div class="head"><h2 id="modalTitle">Нова подія</h2><button class="btn gray" id="closeModal">Закрити</button></div><input type="hidden" id="eventId"><div class="formgrid"><div><label>Назва</label><input id="title"></div><div><label>Дата</label><input id="date" type="date"></div><div><label>Категорія</label><select id="cat"><option value="declaration">Декларування</option><option value="conflict">Конфлікт інтересів</option><option value="gifts">Подарунки</option><option value="notice">Повідомлення</option><option value="training">Навчання</option><option value="restriction">Обмеження</option><option value="imported">Excel</option></select></div></div><label>Повторюваність</label><input id="recur"><label>Опис</label><textarea id="description" rows="3"></textarea><label>Інструкція</label><textarea id="instruction" rows="3"></textarea><label>Посилання</label><input id="link"><div class="formgrid"><div><label>Аудиторія</label><input id="audience" value="Усі працівники"></div><div><label>Нагадування</label><input id="reminders" value="30,10,3,0"></div><div style="display:flex;align-items:end"><button class="btn green" id="saveEvent" style="width:100%">Зберегти</button></div></div></div></div>
<script>
let allEvents=[], allChat=[]; const $=id=>document.getElementById(id); const catNames={declaration:"Декларування",conflict:"Конфлікт інтересів",gifts:"Подарунки",notice:"Повідомлення",training:"Навчання",restriction:"Обмеження",imported:"Excel"};
$("token").value=sessionStorage.getItem("adminToken")||""; function tok(){return $("token").value.trim()} function h(extra={}){return tok()?{...extra,Authorization:"Bearer "+tok()}:extra} function msg(t,ok=true){const e=$("status");e.className="status "+(ok?"ok":"err");e.textContent=t;e.style.display="block";clearTimeout(e._t);e._t=setTimeout(()=>e.style.display="none",4500)} function fmt(d){if(!d)return"";return new Date(d+"T00:00:00").toLocaleDateString("uk-UA")} function days(d){const a=new Date();a.setHours(0,0,0,0);return Math.ceil((new Date(d+"T00:00:00")-a)/86400000)} function td(t){const c=document.createElement("td");c.textContent=t==null?"":String(t);return c}
document.querySelectorAll(".nav button").forEach(b=>b.onclick=()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.add("hidden"));$(b.dataset.tab).classList.remove("hidden");document.querySelectorAll(".nav button").forEach(x=>x.classList.toggle("active",x===b)); if(b.dataset.tab==="chat") loadChat(); if(b.dataset.tab==="dashboard") loadStats();});
function openM(edit=false){$("modalTitle").textContent=edit?"Редагування події":"Нова подія";$("modal").classList.add("open")} function closeM(){$("modal").classList.remove("open")} function clearForm(){["eventId","title","date","recur","description","instruction","link"].forEach(id=>$(id).value="");$("cat").value="declaration";$("audience").value="Усі працівники";$("reminders").value="30,10,3,0"}
function edit(ev){$("eventId").value=ev.id;$("title").value=ev.title||"";$("date").value=ev.date||"";$("cat").value=ev.cat||"declaration";$("recur").value=ev.recur||"";$("description").value=ev.description||"";$("instruction").value=ev.instruction||"";$("link").value=ev.link||"";$("audience").value=ev.audience||"Усі працівники";$("reminders").value=(ev.reminders||[30,10,3,0]).join(",");openM(true)}
function filtered(){let d=[...allEvents],q=$("search").value.toLowerCase().trim(),c=$("filterCat").value;if(c)d=d.filter(e=>e.cat===c);if(q)d=d.filter(e=>[e.title,e.description,e.instruction,e.link,catNames[e.cat]].join(" ").toLowerCase().includes(q));let s=$("sort").value;d.sort((a,b)=>s==="views"?(b.views||0)-(a.views||0):s==="title"?(a.title||"").localeCompare(b.title||"","uk"):(a.date||"").localeCompare(b.date||""));return d}
function renderDash(){let views=allEvents.reduce((n,e)=>n+(e.views||0),0), cats=new Set(allEvents.map(e=>e.cat)).size;$("stEvents").textContent=allEvents.length;$("stViews").textContent=views;$("stCats").textContent=cats;let rows=allEvents.map(e=>({...e,d:days(e.date)})).filter(e=>e.d>=0).sort((a,b)=>a.d-b.d).slice(0,8), body=$("nearestBody");body.innerHTML="";if(!rows.length){body.innerHTML='<tr><td colspan="4">Майбутніх подій немає</td></tr>';return} rows.forEach(e=>{let tr=document.createElement("tr");tr.append(td(fmt(e.date)));let name=td(e.title);if(e.link){let m=document.createElement("div");m.className="muted";m.textContent=e.link;name.append(m)}tr.append(name);let cat=document.createElement("td"),p=document.createElement("span");p.className="pill";p.textContent=catNames[e.cat]||e.cat;cat.append(p);tr.append(cat);let st=document.createElement("td"),s=document.createElement("span");s.className="pill "+(e.d===0?"today":e.d<=7?"soon":"");s.textContent=e.d===0?"Сьогодні":"через "+e.d+" дн.";st.append(s);tr.append(st);body.append(tr)})}
function renderEvents(){let body=$("eventsBody");body.innerHTML="";let data=filtered();if(!data.length){body.innerHTML='<tr><td colspan="5">Подій не знайдено</td></tr>';return}data.forEach(e=>{let tr=document.createElement("tr");tr.append(td(fmt(e.date)));let name=td(e.title);[e.description,e.link].filter(Boolean).forEach(t=>{let m=document.createElement("div");m.className="muted";m.textContent=t;name.append(m)});tr.append(name);let cat=document.createElement("td"),p=document.createElement("span");p.className="pill";p.textContent=catNames[e.cat]||e.cat;cat.append(p);tr.append(cat);tr.append(td(e.views||0));let a=document.createElement("td"),w=document.createElement("div");w.className="actions";let eb=document.createElement("button");eb.className="btn orange";eb.textContent="Редагувати";eb.onclick=()=>edit(e);let db=document.createElement("button");db.className="btn red";db.textContent="Видалити";db.onclick=()=>del(e.id);w.append(eb,db);a.append(w);tr.append(a);body.append(tr)})}
function renderStats(){let body=$("statsBody");body.innerHTML="";let max=Math.max(1,...allEvents.map(e=>e.views||0));[...allEvents].sort((a,b)=>(b.views||0)-(a.views||0)).forEach(e=>{let tr=document.createElement("tr");tr.append(td(e.title));tr.append(td(catNames[e.cat]||e.cat));tr.append(td(e.views||0));let c=document.createElement("td"),bar=document.createElement("div"),i=document.createElement("i");bar.className="bar";i.style.width=Math.max(4,Math.round(((e.views||0)/max)*100))+"%";bar.append(i);c.append(bar);tr.append(c);body.append(tr)})}
async function loadEvents(){try{let r=await fetch("/events");allEvents=await r.json();renderDash();renderEvents();renderStats()}catch(e){msg("Помилка подій: "+e.message,false)}} async function loadStats(){if(!tok())return;try{let r=await fetch("/stats",{headers:h()});if(r.ok){let s=await r.json();$("stChat").textContent=s.chat_new;$("stDevices").textContent=s.devices}}catch(e){}}
async function save(){if(!tok()){msg("Спершу вкажи ADMIN_TOKEN",false);return}let id=$("eventId").value.trim(),payload={title:$("title").value.trim(),date:$("date").value,cat:$("cat").value,recur:$("recur").value.trim(),description:$("description").value.trim(),instruction:$("instruction").value.trim(),link:$("link").value.trim(),audience:$("audience").value.trim()||"Усі працівники",reminders:$("reminders").value.split(",").map(x=>Number(x.trim())).filter(x=>!isNaN(x))};if(!payload.title||!payload.date){msg("Заповни назву і дату",false);return}try{let r=await fetch(id?`/events/${id}`:"/events",{method:id?"PUT":"POST",headers:h({"Content-Type":"application/json"}),body:JSON.stringify(payload)});if(!r.ok)throw new Error(await r.text());closeM();clearForm();await loadEvents();msg("Збережено")}catch(e){msg("Помилка: "+e.message,false)}}
async function del(id){if(!tok()){msg("Спершу вкажи ADMIN_TOKEN",false);return}if(!confirm("Видалити подію?"))return;let r=await fetch(`/events/${id}`,{method:"DELETE",headers:h()});if(r.ok||r.status===204){await loadEvents();msg("Видалено")}else msg("Помилка видалення",false)}
async function uploadExcel(){if(!tok()){msg("Спершу вкажи ADMIN_TOKEN",false);return}let f=$("excelFile").files[0];if(!f){msg("Вибери Excel файл",false);return}let fd=new FormData();fd.append("file",f);try{let r=await fetch("/events/import-excel",{method:"POST",headers:h(),body:fd});let j=await r.json();if(!r.ok)throw new Error(j.detail||JSON.stringify(j));$("excelResult").textContent=`Завантажено: ${j.created}. Пропущено: ${j.skipped_count}.`;await loadEvents();msg("Excel завантажено")}catch(e){msg("Помилка Excel: "+e.message,false)}}
async function loadChat(){if(!tok()){msg("Спершу вкажи ADMIN_TOKEN",false);return}try{let r=await fetch("/admin/chat",{headers:h()});allChat=await r.json();let box=$("chatList");box.innerHTML="";if(!allChat.length){box.innerHTML='<div class="muted">Повідомлень поки немає</div>';return}allChat.forEach(m=>{let d=document.createElement("div");d.className="chatitem";d.innerHTML=`<div class="muted">${m.client_id} • ${new Date(m.created_at).toLocaleString("uk-UA")}</div><div class="chatq"></div><textarea rows="3" placeholder="Відповідь"></textarea><div class="actions"><button class="btn green">Відповісти</button><button class="btn red">Видалити</button></div>`;d.querySelector(".chatq").textContent=m.question;if(m.answer){let a=document.createElement("div");a.className="chata";a.textContent="Відповідь: "+m.answer;d.insertBefore(a,d.querySelector("textarea"));d.querySelector("textarea").value=m.answer}d.querySelector(".green").onclick=()=>answerChat(m.id,d.querySelector("textarea").value);d.querySelector(".red").onclick=()=>deleteChat(m.id);box.append(d)})}catch(e){msg("Помилка чату: "+e.message,false)}} async function answerChat(id,answer){let r=await fetch(`/admin/chat/${id}/answer`,{method:"POST",headers:h({"Content-Type":"application/json"}),body:JSON.stringify({answer})});if(r.ok){msg("Відповідь збережено");loadChat();loadStats()}else msg("Помилка відповіді",false)} async function deleteChat(id){if(!confirm("Видалити повідомлення?"))return;let r=await fetch(`/admin/chat/${id}`,{method:"DELETE",headers:h()});if(r.ok||r.status===204){msg("Чат видалено");loadChat();loadStats()}else msg("Помилка видалення",false)}
async function sendPush(){if(!tok()){msg("Спершу вкажи ADMIN_TOKEN",false);return}let title=$("pushTitle").value.trim(),body=$("pushBody").value.trim();if(!title||!body){msg("Заповни заголовок і текст",false);return}try{let r=await fetch("/push/send",{method:"POST",headers:h({"Content-Type":"application/json"}),body:JSON.stringify({title,body})});let j=await r.json();$("pushResult").textContent=j.configured?`Надіслано: ${j.sent}, помилок: ${j.failed}, пристроїв: ${j.devices}`:`Push не налаштовано: ${j.error||"немає Firebase"}. Пристроїв: ${j.devices}`;msg(j.configured?"Push відправлено":"Push не налаштовано",j.configured)}catch(e){msg("Помилка push: "+e.message,false)}}
$("btnCreate").onclick=$("btnCreateTop").onclick=()=>{clearForm();openM(false)};$("closeModal").onclick=closeM;$("modal").onclick=e=>{if(e.target.id==="modal")closeM()};$("saveEvent").onclick=save;$("reload1").onclick=$("reload2").onclick=()=>{loadEvents();loadStats()};["search","filterCat","sort"].forEach(id=>$(id).oninput=renderEvents);$("saveToken").onclick=()=>{sessionStorage.setItem("adminToken",tok());msg("Токен збережено");loadStats()};$("clearToken").onclick=()=>{sessionStorage.removeItem("adminToken");$("token").value="";msg("Токен очищено")};$("btnExcel").onclick=uploadExcel;$("reloadChat").onclick=loadChat;$("btnPush").onclick=sendPush;loadEvents();loadStats();
</script></body></html>
"""

@app.post("/chat/question")
def chat_question(data: ChatIn, db: Session = Depends(get_db)):
    return chat_create(data, db)

@app.get("/chat/messages")
def chat_messages(client_id: str, db: Session = Depends(get_db)):
    return chat_for_client(client_id, db)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_with_chat_push_excel:app", host="0.0.0.0", port=8000, reload=True)
