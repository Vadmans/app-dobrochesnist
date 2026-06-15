from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from auth import require_admin
from database import get_db
from models import AdminUser, Device
from schemas import DeviceIn
from utils import allow_rate, client_ip

router = APIRouter()

@router.post("/devices/register")
def register_device(data: DeviceIn, request: Request, db: Session = Depends(get_db)):
    if not allow_rate(f"devreg:{client_ip(request)}", 60, 60):
        raise HTTPException(429, "Забагато запитів, спробуйте трохи згодом")
    token = (data.token or "").strip()
    if not token:
        raise HTTPException(400, "Token обов'язковий")
    cid = (data.client_id or "").strip()
    now = datetime.now(timezone.utc)
    dev = db.get(Device, token)
    if dev:
        dev.updated_at = now
        dev.last_seen_at = now
        if data.platform:
            dev.platform = data.platform
        if data.app_version:
            dev.app_version = data.app_version
        if cid:
            dev.client_id = cid
    else:
        dev = Device(token=token, platform=data.platform, app_version=data.app_version, client_id=cid, created_at=now, updated_at=now, last_seen_at=now)
        db.add(dev)
    db.commit()
    return {"ok": True}


@router.get("/devices")
def list_devices(admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    return db.query(Device).order_by(Device.updated_at.desc()).all()


@router.delete("/devices/{token}", status_code=204)
def delete_device(token: str, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    dev = db.get(Device, token)
    if not dev:
        raise HTTPException(404, "Пристрій не знайдено")
    db.delete(dev)
    db.commit()


