import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, session, redirect, url_for
from flask_login import current_user
from config import Config, BASE
from extensions import db, login_manager, csrf
from utils import PAY, DURATION, STATUS, SETTING_KEYS, fmt_km, user_loc, user_radius, settings, age_filter, youth_hours_open


def _add_missing_columns():
    """Tiny auto-migration so an older SQLite file keeps working after new columns are added."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    want = {
        "user": {"lat": "FLOAT", "lng": "FLOAT", "photo": "VARCHAR(60)", "balance": "INTEGER DEFAULT 0",
                 "headline": "VARCHAR(80)", "languages": "VARCHAR(120)", "availability": "VARCHAR(30)", "phone": "VARCHAR(20)",
                 "uid": "VARCHAR(12)", "age_group": "VARCHAR(6)", "is_mod": "BOOLEAN DEFAULT FALSE",
                 "work_radius_km": "INTEGER DEFAULT 25", "mute_until": "TIMESTAMP", "timeout_until": "TIMESTAMP",
                 "sanction_note": "VARCHAR(200)", "admin_note": "VARCHAR(500)"},
        "order": {"featured": "BOOLEAN DEFAULT FALSE", "audio": "VARCHAR(60)", "audio_seconds": "INTEGER DEFAULT 0",
                  "adult_only": "BOOLEAN DEFAULT TRUE", "work_from": "TIME", "work_to": "TIME",
                  "exact_lat": "FLOAT", "exact_lng": "FLOAT"},
        "audit_log": {"detail": "VARCHAR(200)"},
        "report": {"resolution": "VARCHAR(200)", "handler_id": "INTEGER", "handled_at": "TIMESTAMP"},
    }
    for table, cols in want.items():
        have = {c["name"] for c in insp.get_columns(table)}
        for name, typ in cols.items():
            if name not in have:
                db.session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {name} {typ}'))
    db.session.commit()


def _backfill():
    """Give every older account a UID, a work radius and (for staff) an age group."""
    from sqlalchemy import text
    from models import User
    from utils import new_uid
    for u in User.query.filter(User.uid.is_(None)).all():
        u.uid = new_uid()
        db.session.commit()
    db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_user_uid ON "user" (uid)'))
    db.session.execute(text('UPDATE "user" SET work_radius_km = 25 WHERE work_radius_km IS NULL'))
    db.session.execute(text('UPDATE "user" SET age_group = \'adult\' WHERE is_admin = TRUE AND age_group IS NULL'))
    db.session.commit()


def _purge_drafts():
    """Jobs are no longer saved before they are published, so drop any leftover drafts from older versions."""
    from models import Order
    if Order.query.filter_by(status="draft").delete(synchronize_session=False):
        db.session.commit()


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    os.makedirs(os.path.join(BASE, "instance"), exist_ok=True)
    for sub in ("avatars", "audio"):
        os.makedirs(os.path.join(app.static_folder, "uploads", sub), exist_ok=True)
    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Log in or sign up to continue."

    from models import User, Notification, Message, Order

    @login_manager.user_loader
    def load_user(uid):
        u = db.session.get(User, int(uid))
        return u if u and u.status == "active" else None

    import push as pushlib
    from routes import auth, market, orders, profile, support, admin, push as push_routes
    for m in (auth, market, orders, profile, support, admin, push_routes):
        app.register_blueprint(m.bp)

    @app.before_request
    def touch_user():
        if current_user.is_authenticated and (datetime.utcnow() - (current_user.last_seen or datetime(2000, 1, 1))) > timedelta(minutes=5):
            current_user.last_seen = datetime.utcnow()
            db.session.commit()

    @app.before_request
    def age_gate():
        """Everyone must say whether they are 18+ once. Older accounts are asked the next time they open the app."""
        if not current_user.is_authenticated or current_user.age_group:
            return
        if request.endpoint in ("auth.age", "auth.logout", "static", "push.service_worker", "support.page", None):
            return
        return redirect(url_for("auth.age", next=request.full_path.rstrip("?")))

    app.json.ensure_ascii = False  # send real UTF-8 (Hindi, emoji) in JSON instead of \\u escapes

    @app.after_request
    def flush_push(r):
        """Send queued phone/browser pushes once the request has succeeded (a rollback can't leak one)."""
        from flask import g
        items = g.pop("_pushes", None)
        if items and r.status_code < 400:
            for uid, text, url in items:
                pushlib.send_async(uid, {"title": "NearHand", "body": text, "url": url or "/dashboard", "tag": url or "nearhand"})
        return r

    @app.after_request
    def headers(r):
        r.headers.setdefault("X-Content-Type-Options", "nosniff")
        r.headers.setdefault("X-Frame-Options", "DENY")
        r.headers.setdefault("Referrer-Policy", "same-origin")
        r.headers.setdefault("Permissions-Policy", "geolocation=(self), notifications=(self), microphone=(self)")
        if r.mimetype == "text/html":
            r.headers.setdefault("Cache-Control", "no-store")
        return r

    @app.context_processor
    def inject():
        unread_n = unread_m = 0
        oq = db.session.query(db.func.coalesce(db.func.max(Order.id), 0)).filter(Order.status != "draft")
        live = dict(nid=0, mid=0, oid=age_filter(oq).scalar())
        if current_user.is_authenticated:
            unread_n = Notification.query.filter_by(user_id=current_user.id, read=False).count()
            unread_m = Message.query.filter_by(recipient_id=current_user.id, read=False).count()
            live["nid"] = db.session.query(db.func.coalesce(db.func.max(Notification.id), 0)).filter_by(user_id=current_user.id).scalar()
            live["mid"] = db.session.query(db.func.coalesce(db.func.max(Message.id), 0)).filter_by(recipient_id=current_user.id).scalar()
        import os.path
        has_hero = os.path.exists(os.path.join(app.static_folder, "images", "hero.jpg"))
        st = settings()
        paused = [label for k, label in SETTING_KEYS.items() if st.get(k) == "1"]
        return dict(unread_n=unread_n, unread_m=unread_m, PAY=PAY, DURATION=DURATION, STATUS=STATUS, fmt_km=fmt_km,
                    loc_name=session.get("loc_name") or (current_user.area if current_user.is_authenticated and not session.get("loc_off") else None), has_loc=bool(user_loc()),
                    work_radius=user_radius(), loc_xy=user_loc(), has_hero=has_hero, live=live, site=st, paused_labels=paused, SETTING_KEYS=SETTING_KEYS,
                    max_audio=app.config["MAX_AUDIO_SECONDS"], map_boundary=app.config["MAP_BOUNDARY"], max_photos=app.config["MAX_PHOTOS"],
                    welcome=session.pop("welcome", False),
                    minor_off_hours=bool(current_user.is_authenticated and current_user.is_minor and not youth_hours_open()))

    @app.template_filter("money")
    def money(v):
        return "₹{:,}".format(int(v or 0))

    @app.template_filter("ago")
    def ago(d):
        s = int((datetime.utcnow() - d).total_seconds())
        for n, u in ((86400, "d"), (3600, "h"), (60, "m")):
            if s >= n:
                return f"{s // n}{u} ago"
        return "just now"

    @app.template_filter("until")
    def until(d):
        """Time left until `d`, e.g. '5h 20m'."""
        s = int((d - datetime.utcnow()).total_seconds())
        if s <= 0:
            return "ended"
        if s >= 86400 * 365:
            return "until lifted"
        parts = []
        for n, u in ((86400, "d"), (3600, "h"), (60, "m")):
            if s >= n and len(parts) < 2:
                parts.append(f"{s // n}{u}")
                s %= n
        return " ".join(parts) or "<1m"

    @app.template_filter("tm")
    def tm(t):
        """time(18, 30) -> '6:30 PM'"""
        return t.strftime("%I:%M %p").lstrip("0") if t else ""

    @app.template_filter("mmss")
    def mmss(sec):
        sec = int(sec or 0)
        return f"{sec // 60}:{sec % 60:02d}"

    @app.template_filter("catslug")
    def catslug(c):
        import re
        return re.sub(r"[^a-z]+", "-", (c or "other").lower().replace("&", "and")).strip("-")

    @app.template_filter("dt")
    def dt(d, f="%d %b %Y"):
        return d.strftime(f) if d else "—"

    for code, msg in ((403, "You don't have access to this."), (404, "We couldn't find that page."),
                      (429, "Too many attempts. Please wait a minute and try again."),
                      (413, "That file is too large.")):
        app.register_error_handler(code, lambda e, c=code, m=msg: (render_template("pages/error.html", code=c, msg=m), c))

    with app.app_context():
        db.create_all()
        _add_missing_columns()
        _backfill()
        _purge_drafts()
        from seed import seed
        seed(app)
    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1", port=int(os.environ.get("PORT", 5000)))
