"""Explicit local development settings; no ambient SSH or cluster configuration."""

import os
import secrets
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BROKER_DATA_DIR", Path.cwd() / "local")).resolve()
DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
PROFILE = os.environ.get("BROKER_PROFILE", "development")
if PROFILE != "development":
    raise ImproperlyConfigured("This alpha currently supports the development/test profile only; protected installation acceptance is not complete.")

secret_file = Path(os.environ.get("BROKER_SECRET_KEY_FILE", DATA_DIR / "django-secret"))
try:
    with secret_file.open("x", encoding="utf-8") as handle:
        secret_file.chmod(0o600)
        handle.write(secrets.token_urlsafe(64))
except FileExistsError:
    pass
SECRET_KEY = secret_file.read_text(encoding="utf-8").strip()
if len(SECRET_KEY) < 32:
    raise ImproperlyConfigured("Django secret must contain at least 32 characters")

DEBUG = False
ALLOWED_HOSTS = os.environ.get("BROKER_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver,control").split(",")
ROOT_URLCONF = "compute_broker.control.urls"
AUTH_USER_MODEL = "broker.User"
from .identity.providers import load_provider_configuration
BROKER_IDENTITY_PROVIDERS = load_provider_configuration(os.environ.get("BROKER_IDENTITY_CONFIG_FILE"))
AUTHENTICATION_BACKENDS = ["compute_broker.control.identity.backends.IdentityRoutingBackend"]
from compute_broker.extensions import configured_names, application_configs
try:
    BROKER_EXTENSIONS = configured_names()
except ValueError as exc:
    raise ImproperlyConfigured(str(exc)) from exc
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "compute_broker.control.apps.BrokerConfig", *application_configs(BROKER_EXTENSIONS)]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True, "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth"]}}]
from .database import database_config
DATABASES = {"default": database_config(DATA_DIR)}
USE_TZ = True
TIME_ZONE = os.environ.get("BROKER_TIME_ZONE", "Europe/Berlin")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = []
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024 * 1024  # Public requests have a separate 2 MiB boundary.
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
ARTIFACT_ROOT = Path(os.environ.get("BROKER_ARTIFACT_ROOT", DATA_DIR / "objects")).resolve()
MAX_UPLOAD_BYTES = 512 * 1024
PROJECT_STORAGE_LIMIT = 64 * 1024 * 1024
GLOBAL_STORAGE_LIMIT = 256 * 1024 * 1024
MAX_OBJECTS = 1000
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
LOGIN_URL = "/login/"
