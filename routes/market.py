from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from flask_login import current_user
from sqlalchemy import or_
from models import Order, User, Transaction, Notification, Message
from utils import CATEGORIES, PAY, DURATION, user_loc, user_radius, distance_to, in_worker_area, to_int, age_filter, is_paused
from extensions import db
from sqlalchemy import func

bp = Blueprint("market", __name__)

SORTS = [("newest", "Newest first"), ("nearest", "Nearest first"), ("price_desc", "Price: high to low"),
         ("price_asc", "Price: low to high"), ("top_paying", "Highest total pay"), ("applicants", "Most applicants"),
         ("oldest", "Oldest first")]
PUBLIC = ("open", "fully_booked", "in_progress")


def visible_orders():
    """Base query for everything shown in the marketplace. Under-18 accounts never get adult-only work."""
    return age_filter(Order.query.filter(Order.status.in_(PUBLIC)))


def area_mode(args):
    """'mine' = only work inside the worker's chosen area (default once a location is set); 'all' = everything."""
    return args.get("area") if args.get("area") in ("mine", "all") else ("mine" if user_loc() else "all")


@bp.route("/")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("market.index"))
    return render_template("landing.html")


@bp.route("/market")
def index():
    a = request.args
    q = visible_orders()
    status = a.get("status", "open")
    if status in PUBLIC:
        q = q.filter(Order.status == status)
    if a.get("q", "").strip():
        t = f"%{a['q'].strip()[:60]}%"
        q = q.filter(or_(Order.title.ilike(t), Order.description.ilike(t), Order.skills.ilike(t), Order.category.ilike(t)))
    if a.get("category") in CATEGORIES:
        q = q.filter(Order.category == a["category"])
    if a.get("skill", "").strip():
        q = q.filter(Order.skills.ilike(f"%{a['skill'].strip()[:40]}%"))
    if a.get("min_price"):
        q = q.filter(Order.pay_per_worker >= to_int(a["min_price"]))
    if a.get("max_price"):
        q = q.filter(Order.pay_per_worker <= to_int(a["max_price"]))
    if a.get("workers") in ("1", "2", "3"):
        n = int(a["workers"])
        q = q.filter(Order.workers_needed == n) if n < 3 else q.filter(Order.workers_needed >= 3)
    if a.get("pay") in PAY:
        q = q.filter(Order.pay_method == a["pay"])
    if a.get("duration") in DURATION:
        q = q.filter(Order.duration == a["duration"])
    if a.get("voice") == "1":
        q = q.filter(Order.audio.isnot(None))
    days = {"1": 1, "7": 7, "30": 30}.get(a.get("posted"))
    if days:
        q = q.filter(Order.created >= datetime.utcnow() - timedelta(days=days))

    orders = q.all()
    loc, radius = user_loc(), user_radius()
    mode = area_mode(a)
    total_before_area = len(orders)
    for o in orders:
        o.dist = distance_to(o)
    if loc and mode == "mine":   # inside the worker's work area AND inside the maker's worker-radius
        orders = [o for o in orders if in_worker_area(o, loc, radius)]
    if a.get("max_dist") and loc:
        md = to_int(a["max_dist"], 0)
        orders = [o for o in orders if o.dist is not None and o.dist <= md]
    outside = total_before_area - len(orders) if loc and mode == "mine" else 0

    sort = a.get("sort", "nearest" if loc else "newest")
    keys = {"newest": (lambda o: o.created, True), "oldest": (lambda o: o.created, False),
            "price_desc": (lambda o: o.pay_per_worker, True), "price_asc": (lambda o: o.pay_per_worker, False),
            "top_paying": (lambda o: o.total_budget, True), "applicants": (lambda o: o.applicant_count, True),
            "nearest": (lambda o: o.dist if o.dist is not None else 1e9, False)}
    k, rev = keys.get(sort, keys["newest"])
    orders.sort(key=k, reverse=rev)
    orders.sort(key=lambda o: not o.featured)   # stable: featured work stays on top of whatever sort is chosen

    ctx = dict(orders=orders, sort=sort, mode=mode, radius=radius, outside=outside)
    if a.get("partial"):
        return render_template("market/_grid.html", **ctx)
    open_n = visible_orders().filter(Order.status == "open").count()
    hour_n = visible_orders().filter(Order.created >= datetime.utcnow() - timedelta(hours=1)).count()
    return render_template("market/index.html", categories=CATEGORIES, sorts=SORTS, args=a, open_n=open_n, hour_n=hour_n,
                           market_paused=is_paused("pause_market"), **ctx)


@bp.route("/api/location", methods=["POST"])
def set_location():
    """Viewer location + work radius are kept in the session, rounded to ~1 km. Saved on the profile only if `save` is set."""
    d = request.get_json(silent=True) or {}
    try:
        lat, lng = float(d["lat"]), float(d["lng"])
        assert -90 <= lat <= 90 and -180 <= lng <= 180
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(ok=False), 400
    lat, lng = round(lat, 2), round(lng, 2)
    name = str(d.get("name") or "")[:80].strip()
    radius = to_int(d.get("radius"), user_radius(), 1, 500)
    session["loc"], session["loc_name"], session["loc_radius"] = [lat, lng], name, radius
    session.pop("loc_off", None)
    if d.get("save") and current_user.is_authenticated:
        current_user.lat, current_user.lng, current_user.work_radius_km = lat, lng, radius
        if name:
            current_user.area = name
        db.session.commit()
    return jsonify(ok=True)


@bp.route("/api/location/clear", methods=["POST"])
def clear_location():
    session.pop("loc", None)
    session.pop("loc_name", None)
    session.pop("loc_radius", None)
    session["loc_off"] = True
    return jsonify(ok=True)


@bp.route("/api/activity")
def activity():
    """Anonymous, coarse activity for the landing globe: no names, only ~1 km points and area labels."""
    pts, feed = [], []
    for o in Order.query.filter(Order.status != "draft", Order.lat.isnot(None)).order_by(Order.created.desc()).limit(60):
        pts.append({"lat": o.lat, "lng": o.lng, "k": "job"})
        feed.append((o.created, f"New job posted in {o.area or 'a nearby area'}"))
    for u in User.query.filter(User.is_admin == False, User.lat.isnot(None)).order_by(User.created.desc()).limit(60):  # noqa: E712
        pts.append({"lat": u.lat, "lng": u.lng, "k": "member"})
        feed.append((u.created, f"New member joined from {u.area or 'a nearby area'}"))
    feed.sort(key=lambda x: x[0], reverse=True)
    stats = dict(
        members=User.query.filter_by(is_admin=False).count(),
        jobs=Order.query.filter(Order.status != "draft").count(),
        completed=Order.query.filter(Order.status.in_(["completed", "closed"])).count(),
        paid=db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).scalar())
    return jsonify(points=pts, stats=stats, feed=[{"text": t, "ts": d.isoformat() + "Z"} for d, t in feed[:5]])


@bp.route("/api/live")
def live():
    """Polled by the browser every few seconds: new jobs, notifications, chat messages and wallet balance."""
    oid = request.args.get("oid", 0, type=int)
    pub = visible_orders()
    loc, radius = user_loc(), user_radius()
    fresh = pub.filter(Order.id > oid, Order.status == "open").all()
    if loc and request.args.get("area", "mine") == "mine":  # only announce jobs that are inside this worker's area
        fresh = [o for o in fresh if in_worker_area(o, loc, radius)]
    max_id = age_filter(db.session.query(func.coalesce(func.max(Order.id), 0)).filter(Order.status != "draft")).scalar()
    out = dict(
        orders=dict(max=max_id, new=len(fresh), open=pub.filter(Order.status == "open").count(),
                    hour=pub.filter(Order.created >= datetime.utcnow() - timedelta(hours=1)).count()))
    if current_user.is_authenticated:
        nid, mid = request.args.get("nid", 0, type=int), request.args.get("mid", 0, type=int)
        notifs = Notification.query.filter(Notification.user_id == current_user.id, Notification.id > nid).order_by(Notification.id).limit(5).all()
        msgs = Message.query.filter(Message.recipient_id == current_user.id, Message.id > mid).order_by(Message.id).limit(5).all()
        senders = {u.id: u for u in User.query.filter(User.id.in_([x.sender_id for x in msgs]))} if msgs else {}
        out.update(
            unread_n=Notification.query.filter_by(user_id=current_user.id, read=False).count(),
            unread_m=Message.query.filter_by(recipient_id=current_user.id, read=False).count(),
            balance=current_user.balance or 0,
            notifs=[dict(id=n.id, text=n.text, url=n.url) for n in notifs],
            msgs=[dict(id=x.id, body=x.body[:80], user=senders[x.sender_id].username, name=senders[x.sender_id].name) for x in msgs if x.sender_id in senders])
    return jsonify(out)
