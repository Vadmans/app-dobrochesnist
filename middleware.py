from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from database import SessionLocal
from auth import get_session_user


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

