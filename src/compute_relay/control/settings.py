"""Personal broker settings; local SQLite and no external identity providers."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent
from .private_state import private_directory, signing_secret
DATA_DIR = private_directory(os.environ.get("BROKER_DATA_DIR", Path.cwd() / "local"))
PROFILE = os.environ.get("BROKER_PROFILE", "local")
if PROFILE != "local":
    raise ImproperlyConfigured("This package supports the local profile only")

secret_file = Path(os.environ.get("BROKER_SECRET_KEY_FILE", DATA_DIR / "django-secret"))
SECRET_KEY = signing_secret(secret_file)

DEBUG = False
ALLOWED_HOSTS = os.environ.get("BROKER_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",")
ROOT_URLCONF = "compute_relay.control.urls"
AUTH_USER_MODEL = "broker.User"
from .local_auth_config import configured_owner, local_auth_configuration
BROKER_LOCAL_OWNER_ID, BROKER_LOCAL_AUTH_MODE, BROKER_LOCAL_ORIGIN = local_auth_configuration(os.environ)
BROKER_LOCAL_RUNTIME = os.environ.get("BROKER_LOCAL_RUNTIME", "")
BROKER_INSTALLATION_ID = configured_owner(os.environ.get("BROKER_INSTALLATION_ID"), name="BROKER_INSTALLATION_ID")
BROKER_INSTANCE_ID = configured_owner(os.environ.get("BROKER_INSTANCE_ID"), name="BROKER_INSTANCE_ID")
AUTHENTICATION_BACKENDS = ["compute_relay.control.local_auth.LocalOwnerBackend"]
from compute_relay.extensions import configured_names, application_configs
try:
    BROKER_EXTENSIONS = configured_names()
except ValueError as exc:
    raise ImproperlyConfigured(str(exc)) from exc
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "compute_relay.control.apps.BrokerConfig", *application_configs(BROKER_EXTENSIONS)]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "compute_relay.control.local_auth.LocalOwnerSessionMiddleware",
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
MAX_TRANSFER_BYTES = int(os.environ.get("BROKER_MAX_TRANSFER_BYTES", 8 * 1024**3))
PROJECT_STORAGE_LIMIT = int(os.environ.get("BROKER_PROJECT_STORAGE_LIMIT", 64 * 1024**3))
GLOBAL_STORAGE_LIMIT = int(os.environ.get("BROKER_GLOBAL_STORAGE_LIMIT", 256 * 1024**3))
if not 1 <= MAX_TRANSFER_BYTES <= 8 * 1024**3 or min(PROJECT_STORAGE_LIMIT, GLOBAL_STORAGE_LIMIT) < 1:
    raise ImproperlyConfigured("Storage quotas must be positive and the per-file transfer limit must not exceed 8 GiB")
MAX_OBJECTS = 10000
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
LOGIN_URL = "/login/"
