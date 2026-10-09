# Adapted from the HiveAR prototype by Andy Troiano
# (https://github.com/atroiano/platform-design, commit 361b6aa,
# prototype/config/settings.py), Apache License 2.0.
# Modified by The OpenAR Collective contributors.
# Changes: removed the settings for the HTTP API, the frontend, the preview
# port, and the migration compatibility checker, which the engine bake-off
# does not use, and installed the bake-off's own app, bakeoff
# (Foundation-written). The database, logging, and time settings are unchanged.
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-evaluation-only")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = []

# wax is the domain-agnostic framework, hivear the AR layer built on it
# (SHARED-0001). The bake-off drives the command handlers directly, so there
# is no web layer here: no API, no admin, no sessions, and no URL routes.
INSTALLED_APPS = ["wax", "hivear", "bakeoff"]

MIDDLEWARE = []

_pooled = os.environ.get("DATABASE_POOLED") == "1"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("DATABASE_NAME", "hivear"),
        "USER": os.environ.get("DATABASE_USER", "hivear"),
        "PASSWORD": os.environ.get("DATABASE_PASSWORD", "local-evaluation-only"),
        "HOST": os.environ.get("DATABASE_HOST", "localhost"),
        "PORT": os.environ.get("DATABASE_PORT", "5432"),
        # pgBouncer transaction pooling cannot hold server-side cursors or
        # session state across statements.
        "DISABLE_SERVER_SIDE_CURSORS": _pooled,
        "CONN_MAX_AGE": 0 if _pooled else 60,
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
