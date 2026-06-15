from typing import List
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import require_admin
from database import get_db
from models import AdminUser, Event
from schemas import EventIn, EventOut

router = APIRouter()

@router.get("/events", response_model=List[EventOut])
def list_events(db: Session = Depends(get_db)):
    return db.query(Event).order_by(Event.date).all()


@router.post("/events/{event_id}/view", response_model=EventOut)
def register_view(event_id: str, db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    ev.views += 1
    db.commit()
    db.refresh(ev)
    return ev


@router.post("/events", response_model=EventOut, status_code=201)
def create_event(data: EventIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    ev = Event(id=f"e{uuid.uuid4().hex[:8]}", views=0, **data.model_dump())
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


@router.put("/events/{event_id}", response_model=EventOut)
def update_event(event_id: str, data: EventIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    for key, value in data.model_dump().items():
        setattr(ev, key, value)
    db.commit()
    db.refresh(ev)
    return ev


@router.delete("/events/{event_id}", status_code=204)
def delete_event(event_id: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    ev = db.get(Event, event_id)
    if not ev:
        raise HTTPException(404, "Подію не знайдено")
    db.delete(ev)
    db.commit()


