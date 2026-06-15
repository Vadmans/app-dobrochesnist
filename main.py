from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import models  # noqa: F401  # SQLAlchemy має побачити всі таблиці перед create_all
from database import Base, engine, ensure_schema
from middleware import admin_guard
from routers import chat, devices, events, pages, push, reference, users

Base.metadata.create_all(engine)
ensure_schema()

app = FastAPI(title="Доброчесність")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(admin_guard)

app.include_router(pages.router)
app.include_router(events.router)
app.include_router(reference.router)
app.include_router(devices.router)
app.include_router(push.router)
app.include_router(chat.router)
app.include_router(users.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
