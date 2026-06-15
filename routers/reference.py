import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import require_admin
from database import get_db
from models import AdminUser, Reference
from schemas import ReferenceIn, ReferenceOut

router = APIRouter()

@router.get("/reference", response_model=List[ReferenceOut])
def list_reference(db: Session = Depends(get_db)):
    return db.query(Reference).order_by(Reference.title).all()


@router.post("/reference", response_model=ReferenceOut, status_code=201)
def create_reference(data: ReferenceIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    r = Reference(id=f"r{uuid.uuid4().hex[:8]}", **data.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.put("/reference/{ref_id}", response_model=ReferenceOut)
def update_reference(ref_id: str, data: ReferenceIn, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(Reference, ref_id)
    if not r:
        raise HTTPException(404, "Запис не знайдено")
    for key, value in data.model_dump().items():
        setattr(r, key, value)
    db.commit()
    db.refresh(r)
    return r


@router.delete("/reference/{ref_id}", status_code=204)
def delete_reference(ref_id: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    r = db.get(Reference, ref_id)
    if not r:
        raise HTTPException(404, "Запис не знайдено")
    db.delete(r)
    db.commit()


