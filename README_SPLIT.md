# Доброчесність — розділена структура

Запуск локально:

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Render Start Command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Основні файли:

- `main.py` — тільки створює FastAPI та підключає роутери.
- `database.py` — підключення до БД.
- `models.py` — таблиці SQLAlchemy.
- `schemas.py` — Pydantic-схеми API.
- `auth.py` — авторизація, cookie, паролі.
- `firebase_service.py` — Firebase push.
- `utils.py` — rate-limit, IP клієнта.
- `middleware.py` — захист адмінських маршрутів.
- `routers/` — окремі групи API.
