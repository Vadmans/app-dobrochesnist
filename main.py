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
  <title>Доброчесність API</title>
  <style>
    body { font-family: Arial, sans-serif; background:#eef2f5; margin:0; padding:40px; color:#1B2430; }
    .box { max-width:800px; margin:auto; background:white; padding:30px; border-radius:16px; box-shadow:0 10px 30px rgba(0,0,0,.06); }
    h1 { color:#1F5673; margin-top:0; }
    a { display:inline-block; margin:8px 8px 0 0; padding:10px 14px; background:#1F5673; color:white; text-decoration:none; border-radius:8px; }
  </style>
</head>
<body>
  <div class="box">
    <h1>Доброчесність API працює</h1>
    <p>Сервіс успішно запущений. Мобільний додаток може отримувати події через API.</p>
    <a href="/docs">Swagger /docs</a>
    <a href="/admin">Адмін-панель</a>
    <a href="/events">Події JSON</a>
  </div>
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
  <title>Адмін-панель | Доброчесність</title>
  <style>
    :root {
      --bg:#EBEEF0;
      --surface:#FFFFFF;
      --ink:#1B2430;
      --soft:#5A6577;
      --line:#DCE1E6;
      --accent:#1F5673;
      --red:#9E2F3C;
      --amber:#A9690A;
      --green:#2C6A4E;
    }
    * { box-sizing:border-box; }
    body { font-family: Arial, sans-serif; background:var(--bg); margin:0; padding:24px; color:var(--ink); }
    .box { max-width:1200px; margin:auto; background:var(--surface); padding:24px; border-radius:16px; box-shadow:0 10px 30px rgba(0,0,0,.06); }
    .top { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:18px; flex-wrap:wrap; }
    h1 { margin:0; color:var(--accent); font-size:26px; }
    .links a { color:var(--accent); margin-left:12px; text-decoration:none; font-weight:600; }
    label { display:block; margin-bottom:5px; color:var(--soft); font-size:13px; font-weight:700; }
    input, textarea, select {
      width:100%; padding:10px 12px; margin:0 0 14px;
      border:1px solid #cfd6dd; border-radius:10px; font-size:14px;
    }
    textarea { resize:vertical; }
    button {
      padding:10px 14px; border:0; border-radius:10px; cursor:pointer;
      background:var(--accent); color:white; font-weight:700;
    }
    button:hover { opacity:.92; }
    button.del { background:var(--red); }
    button.edit { background:var(--amber); }
    button.gray { background:#5A6577; }
    button.green { background:var(--green); }
    .row { display:grid; grid-template-columns:1fr 180px 220px; gap:14px; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:18px; }
    .status { padding:10px 12px; border-radius:10px; margin:12px 0; display:none; }
    .ok { background:#E1EEE8; color:var(--green); display:block; }
    .err { background:#F5E2E4; color:var(--red); display:block; }
    table { width:100%; border-collapse:collapse; margin-top:18px; }
    th, td { padding:10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:14px; }
    th { background:#f1f5f7; color:var(--soft); font-size:12px; text-transform:uppercase; }
    .muted { color:var(--soft); font-size:12px; }
    .pill { display:inline-block; padding:4px 8px; border-radius:999px; background:#E6EEF2; color:var(--accent); font-size:12px; font-weight:700; }
    .table-actions { display:flex; gap:6px; flex-wrap:wrap; }
    .auth { display:flex; gap:8px; align-items:flex-end; flex-wrap:wrap; margin-bottom:8px; }
    .auth > div { flex:1; min-width:220px; }
    .auth input { margin-bottom:0; }
    @media (max-width:800px) {
      body { padding:10px; }
      .box { padding:16px; }
      .row { grid-template-columns:1fr; gap:0; }
      table, thead, tbody, th, td, tr { display:block; }
      thead { display:none; }
      tr { border:1px solid var(--line); border-radius:12px; margin-bottom:10px; padding:8px; }
      td { border-bottom:0; padding:6px; }
    }
  </style>
</head>
<body>
  <div class="box">
    <div class="top">
      <div>
        <h1>Адмін-панель “Доброчесність”</h1>
        <div class="muted">Керування подіями для мобільного додатку</div>
      </div>
      <div class="links">
        <a href="/docs" target="_blank">/docs</a>
        <a href="/events" target="_blank">/events</a>
      </div>
    </div>

    <div class="auth">
      <div>
        <label>Токен адміністратора</label>
        <input id="token" type="password" placeholder="Вставте ADMIN_TOKEN">
      </div>
      <button class="gray" id="btnToken">Запам'ятати токен</button>
    </div>

    <div id="status" class="status"></div>

    <input type="hidden" id="eventId">

    <div class="row">
      <div>
        <label>Назва події</label>
        <input id="title" placeholder="Наприклад: Подання щорічної декларації">
      </div>
      <div>
        <label>Дата</label>
        <input id="date" type="date">
      </div>
      <div>
        <label>Категорія</label>
        <select id="cat">
          <option value="declaration">Декларування</option>
          <option value="conflict">Конфлікт інтересів</option>
          <option value="gifts">Подарунки</option>
          <option value="notice">Повідомлення</option>
          <option value="training">Навчання</option>
          <option value="restriction">Обмеження</option>
        </select>
      </div>
    </div>

    <label>Повторюваність</label>
    <input id="recur" placeholder="Наприклад: Щороку, до 1 квітня">

    <label>Опис</label>
    <textarea id="description" rows="3" placeholder="Короткий опис події"></textarea>

    <label>Інструкція для користувача</label>
    <textarea id="instruction" rows="3" placeholder="Що потрібно зробити користувачу"></textarea>

    <label>Аудиторія</label>
    <input id="audience" value="Усі працівники">

    <label>Нагадування, днів до події</label>
    <input id="reminders" value="30,10,3,0">

    <div class="actions">
      <button id="btnSave">Зберегти подію</button>
      <button class="gray" id="btnClear">Очистити</button>
      <button class="green" id="btnReload">Оновити список</button>
    </div>

    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Дата</th>
          <th>Назва</th>
          <th>Категорія</th>
          <th>Перегляди</th>
          <th>Дії</th>
        </tr>
      </thead>
      <tbody id="events"></tbody>
    </table>
  </div>

  <script>
    // Токен зберігаємо лише в межах вкладки (sessionStorage), не в коді.
    const tokenInput = document.getElementById("token");
    tokenInput.value = sessionStorage.getItem("adminToken") || "";

    function getToken() { return tokenInput.value.trim(); }
    function authHeaders(extra = {}) {
      const t = getToken();
      return t ? { ...extra, "Authorization": "Bearer " + t } : extra;
    }

    function showStatus(text, ok = true) {
      const el = document.getElementById("status");
      el.className = "status " + (ok ? "ok" : "err");
      el.textContent = text;            // textContent — без ризику XSS
      el.style.display = "block";
      clearTimeout(el._t);
      el._t = setTimeout(() => { el.style.display = "none"; }, 4000);
    }

    // Безпечно створити клітинку з текстом (екранування за рахунок textContent).
    function td(text) {
      const cell = document.createElement("td");
      cell.textContent = text == null ? "" : String(text);
      return cell;
    }

    async function loadEvents() {
      try {
        const res = await fetch("/events");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        const tbody = document.getElementById("events");
        tbody.innerHTML = "";
        if (!data.length) {
          const tr = document.createElement("tr");
          const cell = document.createElement("td");
          cell.colSpan = 6;
          cell.textContent = "Подій поки немає";
          tr.appendChild(cell);
          tbody.appendChild(tr);
          return;
        }
        data.forEach(ev => {
          const tr = document.createElement("tr");

          const idCell = document.createElement("td");
          const idSpan = document.createElement("span");
          idSpan.className = "muted";
          idSpan.textContent = ev.id || "";
          idCell.appendChild(idSpan);
          tr.appendChild(idCell);

          tr.appendChild(td(ev.date));

          // Назва + опис + інструкція — усе через textContent.
          const titleCell = document.createElement("td");
          const b = document.createElement("b");
          b.textContent = ev.title || "";
          titleCell.appendChild(b);
          [ev.description, ev.instruction].forEach(t => {
            const d = document.createElement("div");
            d.className = "muted";
            d.textContent = t || "";
            titleCell.appendChild(d);
          });
          tr.appendChild(titleCell);

          const catCell = document.createElement("td");
          const pill = document.createElement("span");
          pill.className = "pill";
          pill.textContent = ev.cat || "";
          catCell.appendChild(pill);
          tr.appendChild(catCell);

          tr.appendChild(td(ev.views ?? 0));

          const actCell = document.createElement("td");
          const wrap = document.createElement("div");
          wrap.className = "table-actions";

          const editBtn = document.createElement("button");
          editBtn.className = "edit";
          editBtn.textContent = "Редагувати";
          editBtn.addEventListener("click", () => editEvent(ev));   // замикання, без inline-JSON

          const delBtn = document.createElement("button");
          delBtn.className = "del";
          delBtn.textContent = "Видалити";
          delBtn.addEventListener("click", () => deleteEvent(ev.id));

          wrap.appendChild(editBtn);
          wrap.appendChild(delBtn);
          actCell.appendChild(wrap);
          tr.appendChild(actCell);

          tbody.appendChild(tr);
        });
      } catch (e) {
        showStatus("Не вдалося завантажити події: " + e.message, false);
      }
    }

    function editEvent(ev) {
      document.getElementById("eventId").value = ev.id || "";
      document.getElementById("title").value = ev.title || "";
      document.getElementById("date").value = ev.date || "";
      document.getElementById("cat").value = ev.cat || "declaration";
      document.getElementById("recur").value = ev.recur || "";
      document.getElementById("description").value = ev.description || "";
      document.getElementById("instruction").value = ev.instruction || "";
      document.getElementById("audience").value = ev.audience || "Усі працівники";
      document.getElementById("reminders").value = (ev.reminders || [30, 10, 3, 0]).join(",");
      window.scrollTo({ top: 0, behavior: "smooth" });
    }

    function clearForm() {
      document.getElementById("eventId").value = "";
      document.getElementById("title").value = "";
      document.getElementById("date").value = "";
      document.getElementById("cat").value = "declaration";
      document.getElementById("recur").value = "";
      document.getElementById("description").value = "";
      document.getElementById("instruction").value = "";
      document.getElementById("audience").value = "Усі працівники";
      document.getElementById("reminders").value = "30,10,3,0";
    }

    async function saveEvent() {
      if (!getToken()) { showStatus("Спершу вкажіть токен адміністратора", false); return; }
      const id = document.getElementById("eventId").value.trim();
      const payload = {
        title: document.getElementById("title").value.trim(),
        date: document.getElementById("date").value,
        cat: document.getElementById("cat").value,
        recur: document.getElementById("recur").value.trim(),
        description: document.getElementById("description").value.trim(),
        instruction: document.getElementById("instruction").value.trim(),
        audience: document.getElementById("audience").value.trim() || "Усі працівники",
        reminders: document.getElementById("reminders").value
          .split(",")
          .map(x => Number(x.trim()))
          .filter(x => !isNaN(x))
      };
      if (!payload.title || !payload.date || !payload.cat) {
        showStatus("Заповни назву, дату та категорію", false);
        return;
      }
      const url = id ? `/events/${id}` : "/events";
      const method = id ? "PUT" : "POST";
      try {
        const res = await fetch(url, {
          method,
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify(payload)
        });
        if (res.status === 401) { showStatus("Невірний токен адміністратора", false); return; }
        if (!res.ok) {
          const txt = await res.text();
          throw new Error("HTTP " + res.status + " " + txt);
        }
        clearForm();
        await loadEvents();
        showStatus(id ? "Подію оновлено" : "Подію створено");
      } catch (e) {
        showStatus("Помилка збереження: " + e.message, false);
      }
    }

    async function deleteEvent(id) {
      if (!getToken()) { showStatus("Спершу вкажіть токен адміністратора", false); return; }
      if (!confirm("Видалити цю подію?")) return;
      try {
        const res = await fetch(`/events/${id}`, { method: "DELETE", headers: authHeaders() });
        if (res.status === 401) { showStatus("Невірний токен адміністратора", false); return; }
        if (!res.ok && res.status !== 204) {
          const txt = await res.text();
          throw new Error("HTTP " + res.status + " " + txt);
        }
        await loadEvents();
        showStatus("Подію видалено");
      } catch (e) {
        showStatus("Помилка видалення: " + e.message, false);
      }
    }

    document.getElementById("btnToken").addEventListener("click", () => {
      sessionStorage.setItem("adminToken", getToken());
      showStatus("Токен збережено в цій вкладці");
    });
    document.getElementById("btnSave").addEventListener("click", saveEvent);
    document.getElementById("btnClear").addEventListener("click", clearForm);
    document.getElementById("btnReload").addEventListener("click", loadEvents);

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
