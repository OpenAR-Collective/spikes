import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-evaluation-only")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# wax is the domain-agnostic framework, hivear the AR layer built on it
# (SHARED-0001). No django.contrib apps: there is no admin and no server-side
# session, because the backend is API-only (SHARED-0002).
INSTALLED_APPS = ["django.contrib.staticfiles", "ninja", "wax", "hivear"]

# Only what the API documentation page needs. There are no application views.
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True, "OPTIONS": {}}]

# The documentation page's assets are served from this image, not a CDN, so
# the page works on an installation with no outbound access.
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# The built frontend, copied into the image beside the release it was built
# for. It is served by the color that owns it, so it switches with a cutover
# (SHARED-0009, SHARED-0010). The backend knows nothing else about it.
FRONTEND_DIST = BASE_DIR / "frontend_dist"
WHITENOISE_ROOT = FRONTEND_DIST if FRONTEND_DIST.exists() else None
WHITENOISE_MAX_AGE = 0
# Only names the bundler hashed may be cached for good.
WHITENOISE_IMMUTABLE_FILE_TEST = r"^/assets/[^/]+-[A-Za-z0-9_-]{8}\.(js|css|woff2?|svg|png|jpg)$"

# The application listens twice. Whatever arrives on this second port is
# preview traffic and may not write (SHARED-0010). The proxy sends the idle
# color's preview route here; nothing a client sends can change which
# listener a request came in on.
PREVIEW_PORT = "8001"

# Set when the installation is reached over TLS under a real hostname.
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.ResponseHeaders",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Migrations written before the first release. Everything after them is held
# to the compatibility rules in SHARED-0010 by manage.py check_migrations.
WAX_MIGRATION_BASELINE = {
    ("wax", "0001_core_schema"),
    ("wax", "0002_event_model_state"),
    ("hivear", "0001_ar_schema"),
    ("hivear", "0002_ar_tables"),
    ("hivear", "0003_reason_codes"),
}

# Placeholder for WAX-0017 Identity and Authentication and WAX-0018 Access
# Control: one shared key, one fixed actor.
HIVEAR_DEV_API_KEY = os.environ.get("HIVEAR_DEV_API_KEY", "local-evaluation-only")
HIVEAR_DEV_ACTOR_PK = "00000000-0000-4000-8000-000000000001"
