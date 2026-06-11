"""
Доброчесність — бекенд (API + база даних).

Один сервіс, який обслуговує і застосунок користувача, і адмін-панель.
За замовчуванням використовує SQLite (нічого не треба встановлювати окремо).
Для продакшену перемикається на PostgreSQL зміною одного рядка DATABASE_URL.
"""

import os
import uuid
from datetime import date, timedelta
from typing import List

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, String, Integer, Date, JSON, Text
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, Session, sessionmaker,
)

# ── База даних ────────────────────────────────────────────────────────────
# SQLite для розробки. Для PostgreSQL замініть на:
# "postgresql+psycopg://user:pass@localhost:5432/dobrochesnist"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dobrochesnist.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    cat: Mapped[str] = mapped_column(String, index=True)            # категорія
    title: Mapped[str] = mapped_column(String)                     # назва
    date: Mapped[date] = mapped_column(Date, index=True)           # строк
    recur: Mapped[str] = mapped_column(String, default="")         # повторюваність
    description: Mapped[str] = mapped_column(Text, default="")
    instruction: Mapped[str] = mapped_column(Text, default="")     # «що зробити»
    audience: Mapped[str] = mapped_column(String, default="Усі працівники")
    reminders: Mapped[list] = mapped_column(JSON, default=list)    # [30,10,3,0]
    views: Mapped[int] = mapped_column(Integer, default=0)


Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
app = FastAPI(title="Доброчесність API", version="0.1.0")

# Дозволяємо звертатися з застосунку користувача та адмін-панелі.
# Для продакшену звузьте allow_origins до реальних адрес ваших клієнтів.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    """Лічильник переглядів для статистики адмінки."""
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    ev.views += 1
    db.commit()
    db.refresh(ev)
    return ev


# ── Адмінські ендпоінти (адмін-панель) ────────────────────────────────────
# TODO (етап 2): захистити автентифікацією — наприклад, залежністю,
# яка перевіряє токен адміністратора, перш ніж пускати до цих маршрутів.
@app.post("/events", response_model=EventOut, status_code=201)
def create_event(data: EventIn, db: Session = Depends(get_db)):
    ev = Event(id=f"e{uuid.uuid4().hex[:8]}", views=0, **data.model_dump())
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


@app.put("/events/{event_id}", response_model=EventOut)
def update_event(event_id: str, data: EventIn, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    for k, v in data.model_dump().items():
        setattr(ev, k, v)
    db.commit()
    db.refresh(ev)
    return ev


@app.delete("/events/{event_id}", status_code=204)
def delete_event(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    db.delete(ev)
    db.commit()


@app.get("/stats")
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


# ── Запуск ────────────────────────────────────────────────────────────────
# Дозволяє стартувати і командою `python main.py`, і `uvicorn main:app`.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
