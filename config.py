import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dobrochesnist")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dobrochesnist.db")
IS_PRODUCTION = not DATABASE_URL.startswith("sqlite")

SESSION_COOKIE = "dobro_admin_session"
SESSION_TTL_SECONDS = 60 * 60 * 12

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError(
            "Не задано змінну середовища SECRET_KEY. "
            "Додайте її у налаштуваннях Render (Environment) — це секретний рядок "
            "для підпису сесій."
        )
    SECRET_KEY = "local-dev-secret-change-me"
    logger.warning("SECRET_KEY не задано — використовується тимчасовий ключ для локальної розробки.")

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1" if IS_PRODUCTION else "0") == "1"
