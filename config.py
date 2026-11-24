import os
from datetime import timedelta

BASE = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    # Swap DATABASE_URL for postgresql://... to migrate later; no code changes needed.
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASE, "instance", "app.db"))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
    REMEMBER_COOKIE_HTTPONLY = True
    MAX_CONTENT_LENGTH = 4 * 1024 * 1024            # photos (2 MB) and voice notes (see below) both fit
    PLATFORM_FEE = float(os.environ.get("PLATFORM_FEE", "0.05"))
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@nearhand.local")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin12345")
    # Credit workers' wallets for cash jobs too (bookkeeping only; set to 0 once real payments are connected)
    WALLET_CREDIT_CASH_JOBS = os.environ.get("WALLET_CREDIT_CASH_JOBS", "1") == "1"

    # ---- map borders: "india" draws India's official external boundary (Survey of India: all of Jammu & Kashmir and Ladakh) and hides the
    # base map's own border lines; "osm" keeps OpenStreetMap's borders as they are. Different governments depict these borders differently.
    MAP_BOUNDARY = os.environ.get("MAP_BOUNDARY", "india")

    # ---- voice note on a job (optional, 1.5 minutes max)
    MAX_AUDIO_SECONDS = 90
    MAX_AUDIO_BYTES = 3_000_000                      # compressed ~0.5 MB for 90 s; the WAV fallback (12 kHz mono) is ~2.2 MB

    # ---- ratings
    REVIEW_REVEAL_DAYS = 7                           # a review becomes public after this long even if the other side never replies

    # ---- age groups
    YOUTH_CATEGORIES = ("Tutoring", "Data Entry", "Design & Tech")   # the only categories a job may be opened to under-18s in

    # ---- under-18 working hours (local time) and the time zone they are checked in
    LOCAL_TZ = os.environ.get("LOCAL_TZ", "Asia/Kolkata")
    YOUTH_WORK_START = (6, 0)       # 6:00 AM
    YOUTH_WORK_END = (21, 0)        # 9:00 PM

    # ---- job photos
    MAX_PHOTOS = 10

    # ---- sign-up. Turn on once a mail server is connected (see routes/auth.py _dev_link)
    EMAIL_VERIFICATION = os.environ.get("EMAIL_VERIFICATION") == "1"

    # ---- wallet demo
    DEMO_QR_LABEL = "NEARHAND DEMO PAYMENT - NOT REAL MONEY"
