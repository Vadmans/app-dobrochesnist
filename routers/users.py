import uuid

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy.orm import Session

from auth import hash_password, require_admin
from database import get_db
from models import AdminUser

router = APIRouter()

@router.get("/users")
def list_admin_users(admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    return [{"id": u.id, "username": u.username, "is_active": u.is_active} for u in db.query(AdminUser).all()]


@router.post("/users")
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


@router.post("/users/{user_id}/password")
def change_admin_password(user_id: str, password: str = Form(...), admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    if len(password) < 8:
        raise HTTPException(400, "Пароль мінімум 8 символів")
    user = db.get(AdminUser, user_id)
    if not user:
        raise HTTPException(404, "Користувача не знайдено")
    user.password_hash, user.salt = hash_password(password)
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/toggle")
def toggle_admin_user(user_id: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.get(AdminUser, user_id)
    if not user:
        raise HTTPException(404, "Користувача не знайдено")
    if user.id == admin.id:
        raise HTTPException(400, "Не можна заблокувати себе")
    user.is_active = not user.is_active
    db.commit()
    return {"ok": True, "is_active": user.is_active}


