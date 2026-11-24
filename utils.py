import math, re, secrets, time
from datetime import datetime, time as dtime, timedelta, timezone
from collections import defaultdict, deque
from functools import wraps
from flask import session, request, abort, g, current_app
from flask_login import current_user
from extensions import db
from models import Notification

CATEGORIES = ["Cleaning", "Moving & Lifting", "Delivery & Errands", "Photography", "Design & Tech", "Handyman",
              "Events & Helpers", "Data Entry", "Tutoring", "Other"]
PAY = {"online": "Online", "cash": "Cash on completion", "other": "Other"}
DURATION = {"hours": "A few hours", "1d": "1 day", "2-3d": "2–3 days", "1w": "About a week", "long": "2+ weeks"}
STATUS = {"draft": "Draft", "open": "Open", "fully_booked": "Fully booked", "in_progress": "In progress",
          "completed": "Completed", "cancelled": "Cancelled", "disputed": "Disputed", "closed": "Closed"}
DURATIONS_MOD = [("1h", "1 hour", 3600), ("6h", "6 hours", 6 * 3600), ("24h", "24 hours", 86400), ("3d", "3 days", 3 * 86400),
                ("7d", "7 days", 7 * 86400), ("30d", "30 days", 30 * 86400), ("forever", "Until staff lifts it", 0)]
SETTING_KEYS = {"pause_signups": "New accounts", "pause_market": "Posting & applying for work", "pause_payouts": "Payout requests",
                "pause_deposits": "Add-money requests", "pause_chat": "Chat"}
REPORT_REASONS = ["Scam", "Fake job", "Incorrect information", "Unsafe work", "Payment issue",
                  "Inappropriate content", "Spam", "Suspicious behavior", "Other"]

_hits = defaultdict(deque)


def rate_limit(name, limit=8, window=60):
    """Tiny in-memory limiter for sensitive POST endpoints. Use Redis-backed limits in production."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            if request.method == "POST":
                q, t = _hits[(name, request.remote_addr)], time.time()
                while q and t - q[0] > window:
                    q.popleft()
                if len(q) >= limit:
                    abort(429)
                q.append(t)
            return f(*a, **k)
        return wrapper
    return deco


def haversine(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def user_loc():
    """Viewer location: session first (never stored), else the approximate point saved on their profile."""
    loc = session.get("loc")
    if loc:
        return tuple(loc)
    if not session.get("loc_off") and current_user.is_authenticated and current_user.lat is not None:
        return (current_user.lat, current_user.lng)
    return None


def user_radius():
    """How far the viewer is willing to travel (km): chosen in the location picker, else saved on their profile."""
    r = session.get("loc_radius")
    if r:
        return int(r)
    if current_user.is_authenticated and current_user.work_radius_km:
        return int(current_user.work_radius_km)
    return 25


def in_worker_area(order, loc=None, radius=None):
    """True if the job is inside BOTH the worker's chosen work area and the maker's worker-radius.
    Coordinates are rounded to ~1 km, so a small tolerance is added. Jobs with no pin are never hidden."""
    loc = loc or user_loc()
    if not loc or order.lat is None:
        return True
    d = haversine(loc, (order.lat, order.lng))
    return d <= (radius or user_radius()) + 1.5 and d <= (order.radius_km or 10) + 1.5


def distance_to(order):
    loc = user_loc()
    if not loc or order.lat is None:
        return None
    return haversine(loc, (order.lat, order.lng))


def fmt_km(d):
    if d is None:
        return None
    if d < 1:
        return "<1 km"
    r = round(d * 2) / 2
    return f"~{r:g} km"


def to_int(v, default=0, lo=0, hi=10**7):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def clean_text(s, limit=1000, multiline=False):
    """Normalise user text: Unicode NFC, drop control/zero-width junk, keep emoji, Hindi etc. and (optionally) newlines."""
    import unicodedata
    s = unicodedata.normalize("NFC", s or "").replace("\r\n", "\n").replace("\r", "\n")
    keep = "\n\t" if multiline else ""
    s = "".join(ch for ch in s if ch in keep or (unicodedata.category(ch) not in ("Cc", "Cf") or ch in "\u200d\u200c\ufe0f"))
    if multiline:
        while "\n\n\n" in s:
            s = s.replace("\n\n\n", "\n\n")
    else:
        s = " ".join(s.split())
    return s.strip()[:limit]


def notify(user_id, text, url=None):
    """In-app notification + a queued phone/browser push (sent after the response, see app.py)."""
    from flask import g
    db.session.add(Notification(user_id=user_id, text=text[:200], url=url))
    g.setdefault("_pushes", []).append((user_id, text[:200], url))


def is_blocked(a, b):
    from models import Block
    return Block.query.filter(((Block.blocker_id == a) & (Block.blocked_id == b)) |
                              ((Block.blocker_id == b) & (Block.blocked_id == a))).first() is not None


def can_message(a, b):
    from models import Order, OrderApplication
    if a == b or is_blocked(a, b):
        return False
    q = OrderApplication.query.join(Order)
    return q.filter(((Order.maker_id == a) & (OrderApplication.worker_id == b)) |
                    ((Order.maker_id == b) & (OrderApplication.worker_id == a))).first() is not None


def admin_required(f):
    """Full admins only (money, platform switches, roles)."""
    @wraps(f)
    def wrapper(*a, **k):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(404)
        return f(*a, **k)
    return wrapper


def staff_required(f):
    """Moderators and admins (users, orders, reports, support)."""
    @wraps(f)
    def wrapper(*a, **k):
        if not current_user.is_authenticated or not current_user.is_staff:
            abort(404)
        return f(*a, **k)
    return wrapper


def audit(action, target="", detail=""):
    from models import AuditLog
    db.session.add(AuditLog(admin_id=current_user.id if current_user.is_authenticated else None, action=action[:60],
                            target=str(target)[:80], detail=str(detail)[:200]))


# ---------------- platform switches ----------------
def settings():
    """All platform settings for this request (one query)."""
    if "_settings" not in g:
        from models import Setting
        g._settings = {s.key: s.value for s in Setting.query.all()}
    return g._settings


def is_paused(key):
    return settings().get(key) == "1"


def set_setting(key, value):
    from models import Setting
    row = db.session.get(Setting, key) or Setting(key=key)
    row.value = value
    db.session.add(row)
    g.pop("_settings", None)


# ---------------- under-18 rules: working hours and a content check ----------------
def local_now():
    """Current time in the platform's time zone (India by default)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(current_app.config.get("LOCAL_TZ", "Asia/Kolkata"))
    except Exception:  # tzdata missing (some Windows installs): fall back to a fixed IST offset
        tz = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(tz)


def youth_window():
    c = current_app.config
    return dtime(*c["YOUTH_WORK_START"]), dtime(*c["YOUTH_WORK_END"])


def youth_hours_open():
    """True while under-18s are allowed to work (6 AM to 9 PM local time)."""
    a, b = youth_window()
    return a <= local_now().time().replace(tzinfo=None) < b


def parse_time(v):
    try:
        return datetime.strptime((v or "").strip()[:5], "%H:%M").time()
    except ValueError:
        return None


UNSAFE_TERMS = [
    ("alcohol, smoking or tobacco", r"alcohol|liquor|beer|wine|whisk?e?y|vodka|bartender|bar tender|pub|night ?club|hookah|shisha|cigarettes?|cigar|tobacco|vape|vaping|gutkha|gutka|bidi|sharab|daru"),
    ("gambling", r"casino|gambl\w*|betting|satta|lottery|poker|rummy|teen patti|juaa"),
    ("adult or sexual content", r"adults? only|18\+|escort|massage|strip\w*|erotic|porn\w*|sexy|sex|nude|dating|girlfriend|boyfriend|intimate|companionship"),
    ("drugs", r"drugs?|weed|marijuana|ganja|cannabis|cocaine|charas|heroin|opium"),
    ("weapons or violence", r"weapons?|guns?|pistol|firearms?|explosives?|knife|knives|firecrackers?|bodyguard|fight|beat up"),
    ("night or late work", r"night|midnight|overnight|late[- ]night|after 9|after dark|till late"),
    ("secretive or risky requests", r"come alone|meet alone|alone with|don'?t tell|keep (?:it )?secret|private photos?|send (?:me )?photos?|video call|otp|upi pin|bank account|send money|advance (?:fee|payment)|registration fee|deposit first"),
    ("heavy, dangerous or height work", r"construction|demolition|roof(?:top)?|scaffold\w*|ladder|climb\w*|chemicals?|acid|welding|heavy (?:lifting|loads?)|high voltage|wiring|driving|cash collection"),
]
_UNSAFE_RE = [(label, re.compile(r"(?<!\w)(?:" + pat + r")(?!\w)", re.I)) for label, pat in UNSAFE_TERMS]


def youth_check(f):
    """Is this job suitable for under-18s? `f` has title, description, category, skills, conditions, work_from, work_to.
    Returns {field: message}; an empty dict means it passes. Checks the words used, the category and the daily hours."""
    cats = current_app.config["YOUTH_CATEGORIES"]
    out = {}
    for field, name in (("title", "The title"), ("description", "The description"), ("skills", "The skills"), ("conditions", "The terms")):
        text = f.get(field) or ""
        for label, rx in _UNSAFE_RE:
            hit = rx.search(text)
            if hit:
                out[field] = f"{name} mentions “{hit.group(0)}” ({label}). That isn't suitable for under-18s."
                break
    if f.get("category") not in cats:
        out["category"] = "Only these categories can be opened to under-18s: " + ", ".join(cats) + "."
    a, b = youth_window()
    wf, wt = parse_time(f.get("work_from")), parse_time(f.get("work_to"))
    if not wf or not wt:
        out["work_hours"] = "Add the daily start and end time. It is required for work open to under-18s."
    elif wf < a or wt > b or wt <= wf:
        out["work_hours"] = f"Under-18s may only work between {a.strftime('%I:%M %p').lstrip('0')} and {b.strftime('%I:%M %p').lstrip('0')}. Pick hours inside that window."
    return out


def clear_tracking(order_id):
    """Forget shared worker positions for a job (called when it is completed, cancelled, closed or deleted)."""
    from models import LiveLocation
    LiveLocation.query.filter_by(order_id=order_id).delete()


def restriction(kind):
    """Why the signed-in person can't do `kind` right now (post | apply | chat | deposit | payout), or None.
    Staff are never blocked by platform pauses so they can still test and fix things."""
    u = current_user
    if kind == "work":   # going to a job / sharing a route: under-18s only between 6 AM and 9 PM
        if u.is_minor and not youth_hours_open():
            a, b = youth_window()
            return f"You can't work more right now. Under-18 accounts can work between {a.strftime('%I:%M %p').lstrip('0')} and {b.strftime('%I:%M %p').lstrip('0')}."
        return None
    if u.timed_out:
        return f"Your account is in timeout until {u.timeout_until:%d %b, %H:%M} UTC. You can browse but not do this." + (f" Reason: {u.sanction_note}" if u.sanction_note else "")
    if kind == "chat" and u.muted:
        return f"You are muted until {u.mute_until:%d %b, %H:%M} UTC." + (f" Reason: {u.sanction_note}" if u.sanction_note else "")
    if not u.is_staff:
        key = {"post": "pause_market", "apply": "pause_market", "chat": "pause_chat", "deposit": "pause_deposits", "payout": "pause_payouts"}.get(kind)
        if key and is_paused(key):
            return "This feature is paused for a short while. Please try again later."
    return None


def new_uid():
    from models import User
    while True:
        uid = "NH-%06d" % (100000 + secrets.randbelow(900000))
        if not User.query.filter_by(uid=uid).first():
            return uid


def age_filter(q):
    """Under-18 accounts never see work flagged adult-only. Everyone else sees everything."""
    from models import Order
    if current_user.is_authenticated and current_user.is_minor:
        return q.filter(Order.adult_only == False, Order.work_from.isnot(None))  # noqa: E712
    return q


def order_hidden_from_viewer(o):
    return bool(current_user.is_authenticated and current_user.is_minor and (o.adult_only or o.work_from is None) and o.maker_id != current_user.id)


def qr_svg(text):
    """Demo QR as SVG bytes (generated locally, nothing is sent anywhere)."""
    import io, qrcode, qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def wallet_move(user, kind, amount, note="", order_id=None):
    """Apply a signed amount to a user's demo wallet and write a ledger row (caller commits)."""
    from models import WalletTx
    user.balance = (user.balance or 0) + amount
    db.session.add(WalletTx(user_id=user.id, kind=kind, amount=amount, balance_after=user.balance, note=note[:120], order_id=order_id))


def cap_title(s):
    """Capitalise the first letter of every word, leaving the rest alone (keeps HTML, iPhone, 2BHK)."""
    return " ".join(w[:1].upper() + w[1:] for w in s.split())
