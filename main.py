"""
Доброчесність — бекенд (API + база даних).
Один сервіс, який обслуговує і застосунок користувача, і адмін-панель.
За замовчуванням використовує SQLite (нічого не треба встановлювати окремо).
Для продакшену перемикається на PostgreSQL зміною одного рядка DATABASE_URL.
"""
import os
import uuid
import secrets
from datetime import date, timedelta
from typing import List

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, update, String, Integer, Date, JSON, Text
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, Session, sessionmaker,
)

# ── База даних ────────────────────────────────────────────────────────────
# SQLite для розробки. Для PostgreSQL замініть на:
#   "postgresql+psycopg://user:pass@localhost:5432/dobrochesnist"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dobrochesnist.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    cat: Mapped[str] = mapped_column(String, index=True)        # категорія
    title: Mapped[str] = mapped_column(String)                  # назва
    date: Mapped[date] = mapped_column(Date, index=True)        # строк
    recur: Mapped[str] = mapped_column(String, default="")      # повторюваність
    description: Mapped[str] = mapped_column(Text, default="")
    instruction: Mapped[str] = mapped_column(Text, default="")  # «що зробити»
    audience: Mapped[str] = mapped_column(String, default="Усі працівники")
    reminders: Mapped[list] = mapped_column(JSON, default=list) # [30,10,3,0]
    views: Mapped[int] = mapped_column(Integer, default=0)


# Примітка: create_all зручний для SQLite/MVP. Для продакшену з PostgreSQL
# краще використовувати міграції (Alembic), щоб безпечно змінювати схему.
Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Авторизація адміністратора ─────────────────────────────────────────────
# Токен береться зі змінної середовища ADMIN_TOKEN. Якщо її не задано —
# генеруємо тимчасовий і друкуємо в лог (зручно для локальної розробки,
# але для продакшену ОБОВ'ЯЗКОВО задайте власний сталий токен).
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
if not ADMIN_TOKEN:
    ADMIN_TOKEN = secrets.token_urlsafe(24)
    print("\n" + "=" * 64)
    print("  ADMIN_TOKEN не задано — згенеровано тимчасовий токен:")
    print(f"    {ADMIN_TOKEN}")
    print("  Введіть його в адмін-панелі або передавайте у заголовку:")
    print("    Authorization: Bearer <token>")
    print("  Для продакшену задайте сталий: export ADMIN_TOKEN=...")
    print("=" * 64 + "\n")


def require_admin(authorization: str = Header(default="")):
    """Залежність: пускає далі лише за правильного Bearer-токена."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="Потрібна авторизація адміністратора",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Схеми запитів/відповідей ──────────────────────────────────────────────
class EventIn(BaseModel):
    cat: str
    title: str
    date: date
    recur: str = ""
    description: str = ""
    instruction: str = ""
    audience: str = "Усі працівники"
    reminders: List[int] = []


class EventOut(EventIn):
    id: str
    views: int

    class Config:
        from_attributes = True


# ── Застосунок ────────────────────────────────────────────────────────────
app = FastAPI(title="Доброчесність API", version="0.2.0")

# CORS. За замовчуванням дозволяємо всі джерела (зручно для розробки).
# Для продакшену задайте ALLOWED_ORIGINS через кому, наприклад:
#   export ALLOWED_ORIGINS="https://app.example.com,https://admin.example.com"
_origins_env = os.getenv("ALLOWED_ORIGINS", "*").strip()
allow_origins = (
    ["*"] if _origins_env == "*"
    else [o.strip() for o in _origins_env.split(",") if o.strip()]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Публічні ендпоінти (застосунок користувача) ───────────────────────────
@app.get("/events", response_model=List[EventOut])
def list_events(db: Session = Depends(get_db)):
    """Усі події, відсортовані за строком."""
    return db.query(Event).order_by(Event.date).all()


@app.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    return ev


@app.post("/events/{event_id}/view", response_model=EventOut)
def register_view(event_id: str, db: Session = Depends(get_db)):
    """Лічильник переглядів для статистики адмінки (атомарний інкремент)."""
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    # Атомарне збільшення на рівні БД — без гонок при одночасних запитах.
    db.execute(update(Event).where(Event.id == event_id).values(views=Event.views + 1))
    db.commit()
    db.refresh(ev)
    return ev


# ── Адмінські ендпоінти (адмін-панель) ────────────────────────────────────
# Усі захищені залежністю require_admin: потрібен Bearer-токен.
@app.post("/events", response_model=EventOut, status_code=201,
          dependencies=[Depends(require_admin)])
def create_event(data: EventIn, db: Session = Depends(get_db)):
    ev = Event(id=f"e{uuid.uuid4().hex[:8]}", views=0, **data.model_dump())
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


@app.put("/events/{event_id}", response_model=EventOut,
         dependencies=[Depends(require_admin)])
def update_event(event_id: str, data: EventIn, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    for k, v in data.model_dump().items():
        setattr(ev, k, v)
    db.commit()
    db.refresh(ev)
    return ev


@app.delete("/events/{event_id}", status_code=204,
            dependencies=[Depends(require_admin)])
def delete_event(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    db.delete(ev)
    db.commit()


@app.get("/stats", dependencies=[Depends(require_admin)])
def stats(db: Session = Depends(get_db)):
    events = db.query(Event).all()
    return {
        "events": len(events),
        "views": sum(e.views for e in events),
        "categories": len({e.cat for e in events}),
        "by_event": [
            {"id": e.id, "title": e.title, "cat": e.cat, "views": e.views}
            for e in sorted(events, key=lambda e: e.views, reverse=True)
        ],
    }


# ── Демо-наповнення (лише якщо база порожня) ──────────────────────────────
def seed():
    db = SessionLocal()
    try:
        if db.query(Event).count() > 0:
            return
        today = date.today()
        demo = [
            Event(id="e1", cat="declaration", title="Подання щорічної декларації",
                  date=today + timedelta(days=12), recur="Щороку, до 1 квітня",
                  description="Подати щорічну декларацію через Реєстр декларацій.",
                  instruction="Перевірте: доходи, нерухомість, транспорт, корпоративні права, рахунки.",
                  audience="Усі працівники", reminders=[30, 10, 3, 0], views=184),
            Event(id="e2", cat="training", title="Щорічне навчання з доброчесності",
                  date=today + timedelta(days=3), recur="Щороку",
                  description="Пройти онлайн-курс із запобігання конфлікту інтересів.",
                  instruction="Курс триває ~40 хв. Сертифікат завантажується у профіль.",
                  audience="Усі працівники", reminders=[10, 3, 0], views=97),
            Event(id="e3", cat="notice", title="Повідомлення про суттєві зміни майнового стану",
                  date=today + timedelta(days=28), recur="За потреби, протягом 10 днів",
                  description="Подати повідомлення про суттєву зміну майнового стану.",
                  instruction="Скористайтесь помічником, щоб перевірити, чи зміна суттєва.",
                  audience="За індивідуальною ситуацією", reminders=[3, 0], views=41),
            Event(id="e4", cat="gifts", title="Декларування отриманих подарунків",
                  date=today + timedelta(days=46), recur="За потреби",
                  description="Зафіксувати подарунки, отримані у зв'язку зі службою.",
                  instruction="Подарунки понад межу передаються органу. Зберігайте документи.",
                  audience="Усі працівники", reminders=[0], views=23),
        ]
        db.add_all(demo)
        db.commit()
    finally:
        db.close()


# Наповнюємо базу при запуску. Викликаємо напряму, а не через app.on_event,
# бо в нових версіях Starlette подію "startup" прибрано. Функція ідемпотентна:
# якщо події вже є — нічого не робить.
seed()


@app.get("/", response_class=HTMLResponse)
def root():
    return """
<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Код Доброчесності</title>
  <style>
    :root{
      --navy:#12384f; --navy2:#1f5673; --gold:#f2b134; --bg:#f4f7fb;
      --card:#ffffff; --ink:#17212f; --muted:#667085; --line:#e6ebf1;
      --shadow:0 24px 60px rgba(18,56,79,.14); --radius:28px;
    }
    *{box-sizing:border-box} body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:radial-gradient(circle at top left,#dcecf5 0,#f7f9fc 38%,#eef3f8 100%);color:var(--ink);min-height:100vh}
    .hero{max-width:1180px;margin:0 auto;padding:46px 22px 34px;display:grid;grid-template-columns:1.1fr .9fr;gap:24px;align-items:center}
    .card{background:rgba(255,255,255,.82);border:1px solid rgba(255,255,255,.72);box-shadow:var(--shadow);border-radius:var(--radius);backdrop-filter:blur(14px)}
    .intro{padding:42px;position:relative;overflow:hidden}.intro:after{content:"";position:absolute;right:-120px;top:-120px;width:260px;height:260px;border-radius:50%;background:rgba(242,177,52,.16)}
    .brand{display:flex;gap:14px;align-items:center;margin-bottom:34px}.logo{width:58px;height:58px;border-radius:18px;background:linear-gradient(145deg,var(--navy),var(--navy2));display:grid;place-items:center;color:white;font-weight:900;font-size:22px;box-shadow:0 14px 32px rgba(31,86,115,.28)}
    .brand b{font-size:18px}.brand span{display:block;color:var(--muted);font-size:13px;margin-top:2px}
    h1{font-size:48px;line-height:1.03;margin:0 0 16px;color:#102f44;letter-spacing:-1.4px}.lead{font-size:17px;line-height:1.7;color:#475467;max-width:670px;margin:0 0 28px}
    .actions{display:flex;gap:12px;flex-wrap:wrap}.btn{display:inline-flex;align-items:center;gap:9px;padding:13px 18px;border-radius:15px;text-decoration:none;font-weight:800;border:1px solid transparent}.btn.primary{background:var(--navy2);color:#fff}.btn.light{background:#fff;color:var(--navy2);border-color:var(--line)}
    .preview{padding:22px}.window{background:#0f2f44;border-radius:24px;padding:18px;min-height:420px;color:white;position:relative;overflow:hidden}.window:before{content:"";position:absolute;inset:auto -80px -110px auto;width:260px;height:260px;background:rgba(242,177,52,.2);border-radius:50%}
    .dots{display:flex;gap:7px;margin-bottom:18px}.dots i{width:10px;height:10px;border-radius:50%;background:rgba(255,255,255,.36)}.dash{display:grid;gap:13px}.mini{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.12);border-radius:18px;padding:16px}.mini h3{margin:0 0 6px;font-size:15px}.mini p{margin:0;color:rgba(255,255,255,.74);font-size:13px}.statline{display:grid;grid-template-columns:1fr 1fr;gap:12px}.num{font-size:30px;font-weight:900;color:var(--gold)}
    @media(max-width:850px){.hero{grid-template-columns:1fr;padding-top:18px}.intro{padding:26px}h1{font-size:36px}.preview{display:none}}
  </style>
</head>
<body>
  <main class="hero">
    <section class="intro card">
      <div class="brand"><div class="logo">КД</div><div><b>Код Доброчесності</b><span>Твій цифровий орієнтир</span></div></div>
      <h1>Цифровий сервіс для контролю строків та подій</h1>
      <p class="lead">API успішно працює. Адміністратор може керувати подіями, а мобільний додаток отримує актуальні строки через захищені ендпоінти.</p>
      <div class="actions">
        <a class="btn primary" href="/admin">Відкрити адмін-панель</a>
        <a class="btn light" href="/docs">Swagger /docs</a>
        <a class="btn light" href="/events">Події JSON</a>
      </div>
    </section>
    <aside class="preview card">
      <div class="window">
        <div class="dots"><i></i><i></i><i></i></div>
        <div class="dash">
          <div class="mini"><h3>Найближча подія</h3><p>Подання декларації • залишилось 12 днів</p></div>
          <div class="statline"><div class="mini"><div class="num">24</div><p>Активні події</p></div><div class="mini"><div class="num">1.4k</div><p>Перегляди</p></div></div>
          <div class="mini"><h3>Статус системи</h3><p>Мобільний додаток підключено до API</p></div>
        </div>
      </div>
    </aside>
  </main>
</body>
</html>
"""


@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    return """
<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Адмін-панель | Код Доброчесності</title>
  <style>
    :root{
      --bg:#f4f7fb; --sidebar:#102f44; --sidebar2:#1f5673; --card:#fff; --ink:#17212f;
      --muted:#667085; --line:#e7edf3; --gold:#f2b134; --green:#1f8a5b; --red:#b42335; --orange:#b86a00;
      --shadow:0 18px 45px rgba(16,47,68,.11); --radius:22px;
    }
    *{box-sizing:border-box} body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--ink)}
    .app{display:grid;grid-template-columns:280px 1fr;min-height:100vh}.sidebar{background:linear-gradient(180deg,var(--sidebar),#0b2536);color:#fff;padding:24px;position:sticky;top:0;height:100vh;overflow:auto}.brand{display:flex;align-items:center;gap:13px;margin-bottom:28px}.logo{width:54px;height:54px;border-radius:17px;background:linear-gradient(145deg,var(--gold),#ffd77a);display:grid;place-items:center;color:#102f44;font-weight:950;font-size:20px;box-shadow:0 14px 30px rgba(242,177,52,.22)}.brand b{font-size:17px}.brand span{display:block;color:rgba(255,255,255,.65);font-size:12px;margin-top:3px}
    .nav{display:grid;gap:8px}.nav button{width:100%;border:0;border-radius:15px;padding:13px 14px;text-align:left;background:transparent;color:rgba(255,255,255,.72);font-weight:800;cursor:pointer;display:flex;gap:10px;align-items:center}.nav button.active,.nav button:hover{background:rgba(255,255,255,.11);color:#fff}.side-note{margin-top:26px;padding:16px;border-radius:18px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.1);font-size:13px;color:rgba(255,255,255,.72);line-height:1.45}.main{padding:28px;min-width:0}.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:22px;flex-wrap:wrap}.topbar h1{margin:0;font-size:30px;letter-spacing:-.7px;color:#102f44}.topbar p{margin:6px 0 0;color:var(--muted)}.quick{display:flex;gap:10px;flex-wrap:wrap}.linkbtn,.btn{border:0;border-radius:14px;padding:12px 15px;font-weight:850;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:8px}.linkbtn{background:#fff;color:var(--sidebar2);border:1px solid var(--line)}.btn{background:var(--sidebar2);color:#fff}.btn.gold{background:var(--gold);color:#102f44}.btn.gray{background:#eef3f7;color:#344054}.btn.red{background:var(--red);color:#fff}.btn.green{background:var(--green);color:#fff}.btn.orange{background:var(--orange);color:#fff}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px;margin-bottom:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}.stat{padding:18px}.stat .label{color:var(--muted);font-size:13px;font-weight:800}.stat .value{font-size:32px;font-weight:950;margin-top:8px;color:#102f44}.stat .hint{color:var(--muted);font-size:12px;margin-top:6px}.panel{padding:18px;margin-bottom:18px}.panel-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}.panel h2{margin:0;font-size:18px}.toolbar{display:grid;grid-template-columns:1fr 210px 150px;gap:10px;margin-bottom:14px}input,textarea,select{width:100%;border:1px solid #d7dee7;background:#fff;border-radius:14px;padding:12px 13px;font-size:14px;color:var(--ink);outline:none}input:focus,textarea:focus,select:focus{border-color:var(--sidebar2);box-shadow:0 0 0 4px rgba(31,86,115,.1)}label{display:block;color:#475467;font-size:12px;font-weight:900;margin:0 0 7px}.formgrid{display:grid;grid-template-columns:1fr 180px 210px;gap:12px}.status{display:none;padding:12px 14px;border-radius:14px;margin-bottom:14px;font-weight:750}.ok{display:block;background:#e7f6ef;color:var(--green)}.err{display:block;background:#fde7ea;color:var(--red)}.empty{padding:22px;color:var(--muted);text-align:center}.table-wrap{overflow:auto;border-radius:18px;border:1px solid var(--line)}table{width:100%;border-collapse:collapse;background:#fff}th,td{padding:13px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:14px}th{background:#f8fafc;color:#667085;font-size:12px;text-transform:uppercase;letter-spacing:.04em}tr:last-child td{border-bottom:0}.muted{color:var(--muted);font-size:12px;line-height:1.45}.pill{display:inline-flex;align-items:center;border-radius:999px;padding:5px 10px;background:#e8f1f6;color:var(--sidebar2);font-size:12px;font-weight:900}.pill.soon{background:#fff4d6;color:#8a5200}.pill.today{background:#ffe7e9;color:#a0192a}.actions{display:flex;gap:7px;flex-wrap:wrap}.hidden{display:none!important}.modal{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;align-items:center;justify-content:center;padding:18px;z-index:20}.modal.open{display:flex}.modal-box{width:min(860px,100%);max-height:92vh;overflow:auto;background:#fff;border-radius:26px;padding:20px;box-shadow:0 24px 80px rgba(15,23,42,.28)}.modal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.modal-head h2{margin:0}.close{background:#f1f5f9;color:#102f44;border:0;border-radius:12px;padding:10px 12px;cursor:pointer;font-weight:900}.chartbar{height:10px;background:#eef3f7;border-radius:999px;overflow:hidden;min-width:160px}.chartbar i{display:block;height:100%;background:linear-gradient(90deg,var(--sidebar2),var(--gold));border-radius:999px}.authbox{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}.authbox div{min-width:240px;flex:1}.authbox input{margin:0}.mobile-menu{display:none}
    @media(max-width:980px){.app{grid-template-columns:1fr}.sidebar{position:relative;height:auto}.nav{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:repeat(2,1fr)}.toolbar,.formgrid{grid-template-columns:1fr}.main{padding:16px}.mobile-menu{display:inline-flex}}
    @media(max-width:560px){.grid{grid-template-columns:1fr}.sidebar{padding:18px}.topbar h1{font-size:25px}th{display:none}table,tr,td{display:block;width:100%}tr{border-bottom:1px solid var(--line);padding:8px}td{border:0;padding:7px 10px}.actions .btn{width:100%;justify-content:center}}
  </style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand"><div class="logo">КД</div><div><b>Код Доброчесності</b><span>Твій цифровий орієнтир</span></div></div>
    <div class="nav">
      <button class="active" data-tab="dashboard">📊 Dashboard</button>
      <button data-tab="events">📅 Події</button>
      <button data-tab="stats">📈 Статистика</button>
      <button data-tab="settings">⚙️ Налаштування</button>
    </div>
    <div class="side-note"><b>Порада:</b><br>ADMIN_TOKEN зберігається тільки в цій вкладці браузера. Після закриття вкладки його треба ввести повторно.</div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div><h1>Адмін-панель</h1><p>Керування подіями, строками та переглядами мобільного додатку</p></div>
      <div class="quick"><a class="linkbtn" href="/" target="_blank">🏠 Головна</a><a class="linkbtn" href="/docs" target="_blank">🧩 API</a><button class="btn gold" id="btnOpenCreate">＋ Створити подію</button></div>
    </div>

    <div id="status" class="status"></div>

    <section id="dashboard" class="tab">
      <div class="grid">
        <div class="card stat"><div class="label">Усього подій</div><div class="value" id="statEvents">0</div><div class="hint">активні записи в базі</div></div>
        <div class="card stat"><div class="label">Перегляди</div><div class="value" id="statViews">0</div><div class="hint">загальна активність</div></div>
        <div class="card stat"><div class="label">Категорії</div><div class="value" id="statCats">0</div><div class="hint">типи подій</div></div>
        <div class="card stat"><div class="label">Найближча</div><div class="value" id="statDays">—</div><div class="hint" id="statNearest">немає подій</div></div>
      </div>
      <div class="card panel"><div class="panel-head"><h2>Найближчі події</h2><button class="btn gray" id="btnReloadA">Оновити</button></div><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Подія</th><th>Категорія</th><th>Строк</th></tr></thead><tbody id="nearestBody"></tbody></table></div></div>
    </section>

    <section id="events" class="tab hidden">
      <div class="card panel">
        <div class="panel-head"><h2>Список подій</h2><button class="btn" id="btnOpenCreate2">＋ Додати</button></div>
        <div class="toolbar"><input id="search" placeholder="Пошук за назвою, описом, категорією"><select id="filterCat"><option value="">Усі категорії</option><option value="declaration">Декларування</option><option value="conflict">Конфлікт інтересів</option><option value="gifts">Подарунки</option><option value="notice">Повідомлення</option><option value="training">Навчання</option><option value="restriction">Обмеження</option></select><select id="sort"><option value="date">За датою</option><option value="views">За переглядами</option><option value="title">За назвою</option></select></div>
        <div class="table-wrap"><table><thead><tr><th>Дата</th><th>Назва</th><th>Категорія</th><th>Перегляди</th><th>Дії</th></tr></thead><tbody id="eventsBody"></tbody></table></div>
      </div>
    </section>

    <section id="stats" class="tab hidden">
      <div class="card panel"><div class="panel-head"><h2>Популярність подій</h2><button class="btn gray" id="btnReloadB">Оновити</button></div><div class="table-wrap"><table><thead><tr><th>Подія</th><th>Категорія</th><th>Перегляди</th><th>Графік</th></tr></thead><tbody id="statsBody"></tbody></table></div></div>
    </section>

    <section id="settings" class="tab hidden">
      <div class="card panel"><div class="panel-head"><h2>Авторизація адміністратора</h2></div><div class="authbox"><div><label>ADMIN_TOKEN</label><input id="token" type="password" placeholder="Вставте токен адміністратора"></div><button class="btn" id="btnToken">Запамʼятати</button><button class="btn gray" id="btnForgetToken">Очистити</button></div><p class="muted" style="margin-top:12px">Токен потрібен для створення, редагування та видалення подій.</p></div>
    </section>
  </main>
</div>

<div class="modal" id="modal">
  <div class="modal-box">
    <div class="modal-head"><h2 id="modalTitle">Нова подія</h2><button class="close" id="btnClose">✕</button></div>
    <input type="hidden" id="eventId">
    <div class="formgrid"><div><label>Назва події</label><input id="title" placeholder="Наприклад: Подання щорічної декларації"></div><div><label>Дата</label><input id="date" type="date"></div><div><label>Категорія</label><select id="cat"><option value="declaration">Декларування</option><option value="conflict">Конфлікт інтересів</option><option value="gifts">Подарунки</option><option value="notice">Повідомлення</option><option value="training">Навчання</option><option value="restriction">Обмеження</option></select></div></div>
    <label>Повторюваність</label><input id="recur" placeholder="Наприклад: Щороку, до 1 квітня">
    <label>Опис</label><textarea id="description" rows="3" placeholder="Короткий опис події"></textarea>
    <label>Інструкція для користувача</label><textarea id="instruction" rows="3" placeholder="Що потрібно зробити користувачу"></textarea>
    <div class="formgrid"><div><label>Аудиторія</label><input id="audience" value="Усі працівники"></div><div><label>Нагадування</label><input id="reminders" value="30,10,3,0"></div><div style="display:flex;align-items:end"><button class="btn green" id="btnSave" style="width:100%;justify-content:center">Зберегти</button></div></div>
  </div>
</div>

<script>
  let allEvents = [];
  const $ = id => document.getElementById(id);
  const tokenInput = $("token"); tokenInput.value = sessionStorage.getItem("adminToken") || "";
  const catNames = {declaration:"Декларування", conflict:"Конфлікт інтересів", gifts:"Подарунки", notice:"Повідомлення", training:"Навчання", restriction:"Обмеження"};
  function getToken(){ return tokenInput.value.trim(); }
  function authHeaders(extra={}){ const t=getToken(); return t ? {...extra, Authorization:"Bearer "+t} : extra; }
  function showStatus(text, ok=true){ const el=$("status"); el.className="status "+(ok?"ok":"err"); el.textContent=text; el.style.display="block"; clearTimeout(el._t); el._t=setTimeout(()=>el.style.display="none",4200); }
  function fmtDate(d){ if(!d) return ""; const x=new Date(d+"T00:00:00"); return x.toLocaleDateString("uk-UA",{day:"2-digit",month:"2-digit",year:"numeric"}); }
  function daysLeft(d){ const today=new Date(); today.setHours(0,0,0,0); const x=new Date(d+"T00:00:00"); return Math.ceil((x-today)/86400000); }
  function td(text){ const c=document.createElement("td"); c.textContent=text==null?"":String(text); return c; }
  function setTab(name){ document.querySelectorAll(".tab").forEach(x=>x.classList.add("hidden")); $(name).classList.remove("hidden"); document.querySelectorAll(".nav button").forEach(b=>b.classList.toggle("active",b.dataset.tab===name)); }
  document.querySelectorAll(".nav button").forEach(b=>b.addEventListener("click",()=>setTab(b.dataset.tab)));
  function openModal(edit=false){ $("modalTitle").textContent=edit?"Редагування події":"Нова подія"; $("modal").classList.add("open"); }
  function closeModal(){ $("modal").classList.remove("open"); }
  function clearForm(){ ["eventId","title","date","recur","description","instruction"].forEach(id=>$(id).value=""); $("cat").value="declaration"; $("audience").value="Усі працівники"; $("reminders").value="30,10,3,0"; }
  function editEvent(ev){ $("eventId").value=ev.id||""; $("title").value=ev.title||""; $("date").value=ev.date||""; $("cat").value=ev.cat||"declaration"; $("recur").value=ev.recur||""; $("description").value=ev.description||""; $("instruction").value=ev.instruction||""; $("audience").value=ev.audience||"Усі працівники"; $("reminders").value=(ev.reminders||[30,10,3,0]).join(","); openModal(true); }
  function filteredEvents(){ const q=$("search").value.toLowerCase().trim(); const fc=$("filterCat").value; let d=[...allEvents]; if(fc) d=d.filter(e=>e.cat===fc); if(q) d=d.filter(e=>[e.title,e.description,e.instruction,e.cat,catNames[e.cat]].join(" ").toLowerCase().includes(q)); const s=$("sort").value; d.sort((a,b)=>s==="views"?(b.views||0)-(a.views||0):s==="title"?(a.title||"").localeCompare(b.title||"","uk"):(a.date||"").localeCompare(b.date||"")); return d; }
  function renderDashboard(){ const views=allEvents.reduce((n,e)=>n+(e.views||0),0); const cats=new Set(allEvents.map(e=>e.cat)).size; const future=allEvents.map(e=>({...e,days:daysLeft(e.date)})).sort((a,b)=>a.days-b.days); const nearest=future.find(e=>e.days>=0)||future[0]; $("statEvents").textContent=allEvents.length; $("statViews").textContent=views; $("statCats").textContent=cats; $("statDays").textContent=nearest ? (nearest.days>=0?nearest.days:"—") : "—"; $("statNearest").textContent=nearest ? nearest.title : "немає подій"; const body=$("nearestBody"); body.innerHTML=""; const rows=future.filter(e=>e.days>=0).slice(0,6); if(!rows.length){ body.innerHTML='<tr><td colspan="4" class="empty">Майбутніх подій немає</td></tr>'; return; } rows.forEach(ev=>{ const tr=document.createElement("tr"); tr.appendChild(td(fmtDate(ev.date))); const name=td(ev.title); const m=document.createElement("div"); m.className="muted"; m.textContent=ev.audience||""; name.appendChild(m); tr.appendChild(name); const cat=document.createElement("td"); const p=document.createElement("span"); p.className="pill"; p.textContent=catNames[ev.cat]||ev.cat; cat.appendChild(p); tr.appendChild(cat); const st=document.createElement("td"); const s=document.createElement("span"); s.className="pill "+(ev.days===0?"today":ev.days<=7?"soon":""); s.textContent=ev.days===0?"Сьогодні":"через "+ev.days+" дн."; st.appendChild(s); tr.appendChild(st); body.appendChild(tr); }); }
  function renderEvents(){ const body=$("eventsBody"); body.innerHTML=""; const data=filteredEvents(); if(!data.length){ body.innerHTML='<tr><td colspan="5" class="empty">Подій не знайдено</td></tr>'; return; } data.forEach(ev=>{ const tr=document.createElement("tr"); tr.appendChild(td(fmtDate(ev.date))); const name=td(ev.title); [ev.description,ev.instruction].filter(Boolean).forEach(t=>{const d=document.createElement("div"); d.className="muted"; d.textContent=t; name.appendChild(d)}); tr.appendChild(name); const cat=document.createElement("td"); const p=document.createElement("span"); p.className="pill"; p.textContent=catNames[ev.cat]||ev.cat; cat.appendChild(p); tr.appendChild(cat); tr.appendChild(td(ev.views??0)); const a=document.createElement("td"); const w=document.createElement("div"); w.className="actions"; const eb=document.createElement("button"); eb.className="btn orange"; eb.textContent="Редагувати"; eb.onclick=()=>editEvent(ev); const db=document.createElement("button"); db.className="btn red"; db.textContent="Видалити"; db.onclick=()=>deleteEvent(ev.id); w.append(eb,db); a.appendChild(w); tr.appendChild(a); body.appendChild(tr); }); }
  function renderStats(){ const body=$("statsBody"); body.innerHTML=""; const max=Math.max(1,...allEvents.map(e=>e.views||0)); [...allEvents].sort((a,b)=>(b.views||0)-(a.views||0)).forEach(ev=>{ const tr=document.createElement("tr"); tr.appendChild(td(ev.title)); tr.appendChild(td(catNames[ev.cat]||ev.cat)); tr.appendChild(td(ev.views||0)); const c=document.createElement("td"); const bar=document.createElement("div"); bar.className="chartbar"; const i=document.createElement("i"); i.style.width=Math.max(4,Math.round(((ev.views||0)/max)*100))+"%"; bar.appendChild(i); c.appendChild(bar); tr.appendChild(c); body.appendChild(tr); }); if(!allEvents.length) body.innerHTML='<tr><td colspan="4" class="empty">Статистики поки немає</td></tr>'; }
  function renderAll(){ renderDashboard(); renderEvents(); renderStats(); }
  async function loadEvents(){ try{ const res=await fetch("/events"); if(!res.ok) throw new Error("HTTP "+res.status); allEvents=await res.json(); renderAll(); }catch(e){ showStatus("Не вдалося завантажити події: "+e.message,false); } }
  async function saveEvent(){ if(!getToken()){ showStatus("Спершу вкажіть ADMIN_TOKEN у налаштуваннях",false); setTab("settings"); return; } const id=$("eventId").value.trim(); const payload={title:$("title").value.trim(),date:$("date").value,cat:$("cat").value,recur:$("recur").value.trim(),description:$("description").value.trim(),instruction:$("instruction").value.trim(),audience:$("audience").value.trim()||"Усі працівники",reminders:$("reminders").value.split(",").map(x=>Number(x.trim())).filter(x=>!isNaN(x))}; if(!payload.title||!payload.date||!payload.cat){ showStatus("Заповни назву, дату та категорію",false); return; } try{ const res=await fetch(id?`/events/${id}`:"/events",{method:id?"PUT":"POST",headers:authHeaders({"Content-Type":"application/json"}),body:JSON.stringify(payload)}); if(res.status===401){ showStatus("Невірний ADMIN_TOKEN",false); return; } if(!res.ok) throw new Error("HTTP "+res.status+" "+await res.text()); clearForm(); closeModal(); await loadEvents(); showStatus(id?"Подію оновлено":"Подію створено"); }catch(e){ showStatus("Помилка збереження: "+e.message,false); } }
  async function deleteEvent(id){ if(!getToken()){ showStatus("Спершу вкажіть ADMIN_TOKEN у налаштуваннях",false); setTab("settings"); return; } if(!confirm("Видалити цю подію?")) return; try{ const res=await fetch(`/events/${id}`,{method:"DELETE",headers:authHeaders()}); if(res.status===401){ showStatus("Невірний ADMIN_TOKEN",false); return; } if(!res.ok&&res.status!==204) throw new Error("HTTP "+res.status+" "+await res.text()); await loadEvents(); showStatus("Подію видалено"); }catch(e){ showStatus("Помилка видалення: "+e.message,false); } }
  $("btnOpenCreate").onclick=()=>{clearForm();openModal(false)}; $("btnOpenCreate2").onclick=()=>{clearForm();openModal(false)}; $("btnClose").onclick=closeModal; $("modal").addEventListener("click",e=>{if(e.target.id==="modal")closeModal()}); $("btnSave").onclick=saveEvent; $("btnReloadA").onclick=loadEvents; $("btnReloadB").onclick=loadEvents; ["search","filterCat","sort"].forEach(id=>$(id).addEventListener("input",renderEvents)); $("btnToken").onclick=()=>{sessionStorage.setItem("adminToken",getToken());showStatus("Токен збережено в цій вкладці")}; $("btnForgetToken").onclick=()=>{sessionStorage.removeItem("adminToken");tokenInput.value="";showStatus("Токен очищено")};
  loadEvents();
</script>
</body>
</html>
"""


# ── Запуск ────────────────────────────────────────────────────────────────
# Дозволяє стартувати і командою `python main.py`, і `uvicorn main:app`.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
