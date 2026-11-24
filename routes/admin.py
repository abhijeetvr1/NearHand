import csv, io, re, secrets
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response
from flask_login import login_user, current_user
from sqlalchemy import func, or_
from extensions import db
from models import (User, Order, OrderApplication, Report, SupportTicket, Transaction, AuditLog, Review, Notification, WalletTx,
                    PaymentRequest, SavedOrder, Message, Block, PushSub)
from utils import (admin_required, staff_required, audit, notify, rate_limit, STATUS, SETTING_KEYS, DURATIONS_MOD, CATEGORIES,
                   set_setting, settings, wallet_move, clean_text, to_int)
from routes.orders import _drop_audio, _drop_photo_files
from utils import youth_check, clear_tracking

bp = Blueprint("admin", __name__, url_prefix="/admin")
RANGES = [("today", "Today"), ("yesterday", "Yesterday"), ("7d", "7 days"), ("30d", "30 days"), ("month", "This month"),
          ("lastmonth", "Last month"), ("custom", "Custom")]
FOREVER = datetime(2999, 1, 1)


def _range():
    r, t = request.args.get("range", "30d"), date.today()
    if r == "today": s, e = t, t
    elif r == "yesterday": s = e = t - timedelta(days=1)
    elif r == "7d": s, e = t - timedelta(days=6), t
    elif r == "month": s, e = t.replace(day=1), t
    elif r == "lastmonth": e = t.replace(day=1) - timedelta(days=1); s = e.replace(day=1)
    elif r == "custom":
        try: s, e = date.fromisoformat(request.args["from"]), date.fromisoformat(request.args["to"])
        except (KeyError, ValueError): s, e = t - timedelta(days=29), t
    else: r, s, e = "30d", t - timedelta(days=29), t
    return r, datetime.combine(s, datetime.min.time()), datetime.combine(e, datetime.max.time())


def _series(rows, start, end, fn=lambda x: 1):
    days, d = [], start.date()
    while d <= end.date():
        days.append(d); d += timedelta(days=1)
    c = defaultdict(int)
    for row in rows:
        c[row.created.date()] += fn(row)
    return dict(labels=[x.strftime("%d %b") for x in days], data=[c[x] for x in days])


def _dur(code):
    """('24h') -> (datetime when it ends, label). 'forever' lasts until staff lift it."""
    for c, label, secs in DURATIONS_MOD:
        if c == code:
            return (datetime.utcnow() + timedelta(seconds=secs) if secs else FOREVER), label
    return None, None


@bp.context_processor
def counts():
    """Badges in the admin menu."""
    if not (current_user.is_authenticated and current_user.is_staff):
        return {}
    return dict(n_reports=Report.query.filter_by(status="pending").count(),
                n_pay=PaymentRequest.query.filter_by(status="pending").count() if current_user.is_admin else 0,
                n_tickets=SupportTicket.query.filter_by(status="open").count())


# ---------------- sign-in ----------------
@bp.route("/login", methods=["GET", "POST"])
@rate_limit("admin_login", 5, 300)
def login():
    if current_user.is_authenticated and current_user.is_staff:
        return redirect(url_for("admin.overview"))
    if request.method == "POST":
        ident = request.form.get("email", "").strip().lower()
        u = User.query.filter(or_(func.lower(User.email) == ident, User.username == ident)).first()
        if u and u.is_staff and u.status == "active" and u.check_password(request.form.get("password", "")):
            login_user(u)
            audit("staff_login")
            db.session.commit()
            return redirect(url_for("admin.overview"))
        flash("Incorrect staff credentials.", "error")
    return render_template("admin/login.html")


# ---------------- overview ----------------
@bp.route("/")
@staff_required
def overview():
    r, s, e = _range()
    now, td = datetime.utcnow(), date.today()
    uq = User.query
    live = ["open", "fully_booked", "in_progress"]
    fee = lambda a, b: db.session.query(func.coalesce(func.sum(Transaction.fee), 0)).filter(Transaction.created >= a, Transaction.created <= b).scalar()
    stats = [
        ("Total users", uq.count()), ("Active (30d)", uq.filter(User.last_seen >= now - timedelta(days=30)).count()),
        ("New today", uq.filter(User.created >= datetime.combine(td, datetime.min.time())).count()),
        ("New this week", uq.filter(User.created >= now - timedelta(days=7)).count()),
        ("Adults / under 18", f"{uq.filter_by(age_group='adult').count()} / {uq.filter_by(age_group='minor').count()}"),
        ("Muted / timed out", f"{uq.filter(User.mute_until > now).count()} / {uq.filter(User.timeout_until > now).count()}"),
        ("Suspended / banned", f"{uq.filter_by(status='suspended').count()} / {uq.filter_by(status='banned').count()}"),
        ("Total orders", Order.query.filter(Order.status != "draft").count()), ("Active orders", Order.query.filter(Order.status.in_(live)).count()),
        ("Completed", Order.query.filter(Order.status.in_(["completed", "closed"])).count()), ("Cancelled", Order.query.filter_by(status="cancelled").count()),
        ("Reported orders", db.session.query(Report.order_id).filter(Report.order_id.isnot(None)).distinct().count()),
        ("Revenue (range)", "₹{:,}".format(fee(s, e))), ("Revenue today", "₹{:,}".format(fee(datetime.combine(td, datetime.min.time()), now))),
        ("Wallet money held", "₹{:,}".format(db.session.query(func.coalesce(func.sum(User.balance), 0)).scalar())),
        ("Avg rating", round(db.session.query(func.coalesce(func.avg(Review.rating), 0)).filter(Review.visible_clause()).scalar(), 2)),
        ("Applications (range)", OrderApplication.query.filter(OrderApplication.created.between(s, e)).count()),
        ("Orders posted (range)", Order.query.filter(Order.created.between(s, e), Order.status != "draft").count()),
    ]
    charts = dict(
        users=_series(uq.filter(User.created.between(s, e)).all(), s, e),
        orders=_series(Order.query.filter(Order.created.between(s, e), Order.status != "draft").all(), s, e),
        revenue=_series(Transaction.query.filter(Transaction.created.between(s, e)).all(), s, e, lambda t: t.fee),
        completed=_series(Transaction.query.filter(Transaction.created.between(s, e)).all(), s, e),
        cats=dict(Counter(o.category for o in Order.query.filter(Order.status != "draft")).most_common(8)),
        pay=dict(Counter(o.pay_method for o in Order.query.filter(Order.status != "draft"))))
    return render_template("admin/overview.html", stats=stats, charts=charts, ranges=RANGES, r=r, args=request.args,
                           pending_reports=Report.query.filter_by(status="pending").count(),
                           pending_pay=PaymentRequest.query.filter_by(status="pending").count())


# ---------------- users ----------------
def _user_query():
    q = User.query
    t = request.args.get("q", "").strip()
    if t:
        like = f"%{t}%"
        q = q.filter(or_(User.name.ilike(like), User.username.ilike(like), User.email.ilike(like), User.uid.ilike(like), User.phone.ilike(like)))
    if request.args.get("status") in ("active", "suspended", "banned"):
        q = q.filter(User.status == request.args["status"])
    role = request.args.get("role")
    if role == "admin": q = q.filter(User.is_admin == True)  # noqa: E712
    elif role == "mod": q = q.filter(User.is_mod == True)  # noqa: E712
    elif role == "user": q = q.filter(User.is_admin == False, User.is_mod == False)  # noqa: E712
    if request.args.get("age") in ("adult", "minor"):
        q = q.filter(User.age_group == request.args["age"])
    if request.args.get("flag") == "muted": q = q.filter(User.mute_until > datetime.utcnow())
    elif request.args.get("flag") == "timeout": q = q.filter(User.timeout_until > datetime.utcnow())
    elif request.args.get("flag") == "warned": q = q.filter(User.violations > 0)
    sort = request.args.get("sort", "new")
    order = {"new": User.created.desc(), "old": User.created.asc(), "seen": User.last_seen.desc(), "wallet": User.balance.desc(),
             "name": User.name.asc(), "warned": User.violations.desc()}.get(sort, User.created.desc())
    return q.order_by(order)


@bp.route("/users")
@staff_required
def users():
    return render_template("admin/users.html", users=_user_query().limit(300).all(), total=_user_query().count())


@bp.route("/users.csv")
@admin_required
def users_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["uid", "name", "username", "email", "phone", "role", "age_group", "status", "balance", "violations", "area", "joined", "last_seen"])
    for u in _user_query().all():
        w.writerow([u.uid, u.name, u.username, u.email, u.phone, u.role, u.age_group or "", u.status, u.balance or 0, u.violations or 0,
                    u.area or "", u.created.isoformat(), u.last_seen.isoformat() if u.last_seen else ""])
    audit("users_export")
    db.session.commit()
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=nearhand-users.csv"})


@bp.route("/users/<int:uid>")
@staff_required
def user_detail(uid):
    u = db.get_or_404(User, uid)
    my_orders = Order.query.filter_by(maker_id=uid).order_by(Order.created.desc()).limit(15).all()
    order_ids = [o.id for o in Order.query.filter_by(maker_id=uid).with_entities(Order.id)]
    ctx = dict(
        u=u, orders=my_orders,
        apps=OrderApplication.query.filter_by(worker_id=uid).order_by(OrderApplication.created.desc()).limit(15).all(),
        txs=WalletTx.query.filter_by(user_id=uid).order_by(WalletTx.created.desc()).limit(20).all(),
        reqs=PaymentRequest.query.filter_by(user_id=uid).order_by(PaymentRequest.created.desc()).limit(10).all(),
        against=Report.query.filter(or_(Report.user_id == uid, Report.order_id.in_(order_ids or [0]))).order_by(Report.created.desc()).limit(15).all(),
        by_me=Report.query.filter_by(reporter_id=uid).order_by(Report.created.desc()).limit(10).all(),
        reviews=Review.query.filter_by(reviewee_id=uid).order_by(Review.created.desc()).limit(10).all(),
        log=AuditLog.query.filter(AuditLog.target.in_([f"@{u.username}", u.uid])).order_by(AuditLog.created.desc()).limit(12).all(),
        durations=DURATIONS_MOD, earned=db.session.query(func.coalesce(func.sum(Transaction.amount - Transaction.fee), 0)).filter_by(worker_id=uid).scalar(),
        spent=db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter_by(maker_id=uid).scalar(),
        devices=PushSub.query.filter_by(user_id=uid).count(), msgs_sent=Message.query.filter_by(sender_id=uid).count())
    return render_template("admin/user.html", **ctx)


ADMIN_ONLY = {"ban", "delete", "verify", "unverify", "make_mod", "remove_mod", "make_admin", "remove_admin", "set_age", "resetpw", "wallet", "edit"}


def _sanction(u, kind, code, reason):
    """Mute or time out a user. kind: mute | timeout. Returns the label, or None if the duration is unknown."""
    until, label = _dur(code)
    if not until:
        return None
    reason = clean_text(reason, 150)
    if kind == "mute":
        u.mute_until = until
    else:
        u.timeout_until = until
    u.sanction_note = reason
    what = "muted (no chat)" if kind == "mute" else "put in timeout (read-only)"
    notify(u.id, f"You were {what} for {label.lower()}." + (f" Reason: {reason}" if reason else ""))
    return f"{kind.capitalize()} {label}"


@bp.route("/users/<int:uid>/<act>", methods=["POST"])
@staff_required
def user_action(uid, act):
    u = db.get_or_404(User, uid)
    me, f = current_user, request.form
    back = redirect(url_for("admin.user_detail", uid=uid))
    if act in ADMIN_ONLY and not me.is_admin:
        flash("Only admins can do that.", "error")
        return back
    # Moderators can never act on staff; admins can act on moderators but not on other admins (demote them first).
    if u.id == me.id and act in ("suspend", "ban", "delete", "mute", "timeout", "make_mod", "set_age"):
        flash("You can't do that to your own account.", "error")
        return back
    protected = u.is_admin and u.id != me.id or (u.is_mod and not me.is_admin)
    if protected and act not in ("note", "remove_admin"):
        flash("You can't do that to this account.", "error")
        return back
    reason = clean_text(f.get("reason", ""), 150)

    if act in ("suspend", "ban"):
        u.status, u.sanction_note = ("suspended" if act == "suspend" else "banned"), reason
        done = u.status
    elif act == "restore":
        if u.status == "banned" and not me.is_admin:
            flash("Only admins can lift a ban.", "error")
            return back
        u.status, u.sanction_note, done = "active", "", "restored"
    elif act in ("verify", "unverify"):
        u.verified, done = act == "verify", act
    elif act in ("mute", "timeout"):
        done = _sanction(u, act, f.get("duration", "24h"), reason)
        if not done:
            flash("Choose how long.", "error")
            return back
    elif act in ("unmute", "untimeout"):
        setattr(u, "mute_until" if act == "unmute" else "timeout_until", None)
        notify(u.id, "Your mute was lifted." if act == "unmute" else "Your timeout was lifted. You can use NearHand fully again.")
        done = act
    elif act == "warn":
        u.violations = (u.violations or 0) + 1
        notify(u.id, "Warning from moderators." + (f" Reason: {reason}" if reason else "") + f" ({u.violations} on record)")
        done = "warned"
    elif act == "clear_warnings":
        u.violations, done = 0, "warnings cleared"
    elif act == "note":
        u.admin_note, done = clean_text(f.get("note", ""), 500, multiline=True), "note saved"
    elif act == "set_age":
        if f.get("age") in ("adult", "minor"):
            u.age_group, done = f["age"], f"age group {f['age']}"
        else:
            u.age_group, done = None, "age group reset (will be asked again)"
    elif act in ("make_mod", "remove_mod"):
        u.is_mod, done = act == "make_mod", ("moderator added" if act == "make_mod" else "moderator removed")
        notify(u.id, "You are now a NearHand moderator." if u.is_mod else "Your moderator access was removed.", url_for("admin.overview") if u.is_mod else None)
    elif act in ("make_admin", "remove_admin"):
        if act == "remove_admin" and User.query.filter_by(is_admin=True, status="active").count() <= 1:
            flash("There must always be at least one active admin.", "error")
            return back
        u.is_admin, done = act == "make_admin", ("admin added" if act == "make_admin" else "admin removed")
        if act == "make_admin":
            u.is_mod = False
        notify(u.id, "You are now a NearHand admin." if u.is_admin else "Your admin access was removed.")
    elif act == "wallet":
        amt = to_int(f.get("amount"), 0, -10**6, 10**6)
        note = clean_text(f.get("note", ""), 90)
        if not amt or not note:
            flash("Enter a non-zero amount and a short note.", "error")
            return back
        if (u.balance or 0) + amt < 0:
            flash("That would take the wallet below ₹0.", "error")
            return back
        wallet_move(u, "deposit" if amt > 0 else "adjust", amt, f"Admin: {note}")
        notify(u.id, f"{'₹{:,}'.format(abs(amt))} was {'added to' if amt > 0 else 'taken from'} your wallet by an admin.", url_for("profile.wallet"))
        done = f"wallet {amt:+d}"
    elif act == "resetpw":
        tmp = secrets.token_urlsafe(7) + "7a"
        u.set_password(tmp)
        flash(f"Temporary password for @{u.username}: {tmp}  (shown once. Ask them to change it.)", "info")
        done = "password reset"
    elif act == "edit":
        e = {}
        name, username, email = f.get("name", "").strip(), f.get("username", "").strip().lower(), f.get("email", "").strip().lower()
        phone = re.sub(r"[^\d+]", "", f.get("phone", ""))[:20]
        if not 2 <= len(name) <= 80: e["name"] = "Name must be 2–80 characters."
        if not re.fullmatch(r"[a-z0-9_]{3,30}", username): e["username"] = "Username: 3–30 letters, numbers or underscores."
        elif username != u.username and User.query.filter_by(username=username).first(): e["username"] = "That username is taken."
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email): e["email"] = "Enter a valid email."
        elif email != u.email and User.query.filter(func.lower(User.email) == email).first(): e["email"] = "That email is used by another account."
        if e:
            flash(" ".join(e.values()), "error")
            return back
        u.name, u.username, u.email, u.phone = name, username, email, phone
        for k, n in (("headline", 80), ("bio", 300), ("work_description", 600), ("experience", 60), ("area", 80), ("languages", 120)):
            setattr(u, k, clean_text(f.get(k, ""), n, multiline=k == "work_description"))
        u.skills = ", ".join([x.strip()[:30] for x in f.get("skills", "").split(",") if x.strip()][:8])
        u.availability = f.get("availability") if f.get("availability") in ("Weekdays", "Weekends", "Evenings", "Anytime") else ""
        u.work_radius_km = to_int(f.get("work_radius_km"), u.work_radius_km or 25, 1, 500)
        u.email_verified = bool(f.get("email_verified"))
        done = "profile edited"
    elif act == "delete":
        if Order.query.filter_by(maker_id=uid).count() or OrderApplication.query.filter_by(worker_id=uid).count():
            flash("This user has order history. Ban the account instead of deleting it.", "error")
            return back
        for model, col in ((Notification, Notification.user_id), (PushSub, PushSub.user_id), (SavedOrder, SavedOrder.user_id), (WalletTx, WalletTx.user_id),
                           (PaymentRequest, PaymentRequest.user_id)):
            model.query.filter(col == uid).delete()
        Block.query.filter(or_(Block.blocker_id == uid, Block.blocked_id == uid)).delete()
        audit("user_delete", f"@{u.username}", u.uid)
        db.session.delete(u)
        db.session.commit()
        flash(f"Deleted @{u.username}.", "success")
        return redirect(url_for("admin.users"))
    else:
        abort(404)
    if act in ("suspend", "ban", "restore"):
        notify(uid, f"Your account status changed to {u.status}." + (f" Reason: {reason}" if reason and act != "restore" else ""))
    audit(f"user_{act}", f"@{u.username}", f"{done}" + (f" · {reason}" if reason else ""))
    db.session.commit()
    flash(f"Done: {done} for @{u.username}.", "success")
    return back


# ---------------- orders and market ----------------
@bp.route("/orders")
@staff_required
def orders():
    q = Order.query.filter(Order.status != "draft")
    t = request.args.get("q", "").strip()
    if t:
        q = q.filter(or_(Order.title.ilike(f"%{t}%"), Order.category.ilike(f"%{t}%"), Order.area.ilike(f"%{t}%")))
    if request.args.get("status") in STATUS:
        q = q.filter_by(status=request.args["status"])
    if request.args.get("age") == "adult": q = q.filter(Order.adult_only == True)  # noqa: E712
    elif request.args.get("age") == "youth": q = q.filter(Order.adult_only == False)  # noqa: E712
    if request.args.get("voice") == "1": q = q.filter(Order.audio.isnot(None))
    reported = {r[0] for r in db.session.query(Report.order_id).filter(Report.order_id.isnot(None)).distinct()}
    return render_template("admin/orders.html", orders=q.order_by(Order.created.desc()).limit(200).all(), reported=reported)


@bp.route("/orders/<int:oid>/<act>", methods=["POST"])
@staff_required
def order_action(oid, act):
    o = db.get_or_404(Order, oid)
    if act == "cancel": o.status = "cancelled"
    elif act == "suspend": o.status = "closed"
    elif act == "reviewed": o.reviewed_by_admin = True
    elif act == "feature": o.featured = not o.featured
    elif act == "toggle_adult":
        if o.adult_only:   # opening a job to under-18s runs the same safety check the poster gets
            issues = youth_check({"title": o.title, "description": o.description, "category": o.category, "skills": o.skills,
                                  "conditions": o.conditions, "work_from": o.work_from.strftime("%H:%M") if o.work_from else "",
                                  "work_to": o.work_to.strftime("%H:%M") if o.work_to else ""})
            if issues:
                flash("Can't open this to under-18s: " + " ".join(issues.values()), "error")
                return redirect(request.referrer or url_for("admin.orders"))
        o.adult_only = not o.adult_only
    elif act == "remove_photos":
        for p in list(o.photos):
            _drop_photo_files(p.name)
            db.session.delete(p)
    elif act == "remove_audio":
        _drop_audio(o.audio)
        o.audio, o.audio_seconds = None, 0
    elif act == "delete":
        if not current_user.is_admin:
            flash("Only admins can delete orders.", "error")
            return redirect(request.referrer or url_for("admin.orders"))
        SavedOrder.query.filter_by(order_id=oid).delete()
        Report.query.filter_by(order_id=oid).update({"order_id": None})
        Transaction.query.filter_by(order_id=oid).delete()
        Review.query.filter_by(order_id=oid).delete()
        _drop_audio(o.audio)
        for p in o.photos:
            _drop_photo_files(p.name)
        clear_tracking(oid)
        db.session.delete(o)
    else: abort(404)
    if act in ("cancel", "suspend"):
        clear_tracking(oid)
    if act in ("cancel", "suspend", "remove_audio", "remove_photos"):
        what = {"cancel": "cancelled", "suspend": "suspended", "remove_audio": "had its voice note removed", "remove_photos": "had its photos removed"}[act]
        notify(o.maker_id, f"Your order “{o.title}” was {what} by moderators.", url_for("orders.detail", oid=oid) if act != "delete" else None)
    audit(f"order_{act}", f"#{oid}", o.title[:60] if act != "delete" else "")
    db.session.commit()
    flash(f"Order #{oid}: {act.replace('_', ' ')} done.", "success")
    return redirect(request.referrer or url_for("admin.orders"))


@bp.route("/market")
@staff_required
def market():
    """Marketplace health: supply, demand, where work is and who is posting it."""
    posted = Order.query.filter(Order.status != "draft")
    now = datetime.utcnow()
    n_orders = posted.count()
    apps = OrderApplication.query
    by_status = {k: posted.filter(Order.status == k).count() for k in ("open", "fully_booked", "in_progress", "completed", "closed", "cancelled")}
    stats = [
        ("Open now", by_status["open"]), ("Fully booked", by_status["fully_booked"]), ("In progress", by_status["in_progress"]),
        ("Completed", by_status["completed"] + by_status["closed"]), ("Cancelled", by_status["cancelled"]),
        ("Posted (24h)", posted.filter(Order.created >= now - timedelta(days=1)).count()),
        ("Posted (7d)", posted.filter(Order.created >= now - timedelta(days=7)).count()),
        ("Avg pay per person", "₹{:,}".format(int(db.session.query(func.coalesce(func.avg(Order.pay_per_worker), 0)).filter(Order.status != "draft").scalar()))),
        ("Open budget", "₹{:,}".format(sum(o.total_budget for o in posted.filter(Order.status.in_(["open", "fully_booked", "in_progress"])).all()))),
        ("Applications", apps.count()), ("Pending applications", apps.filter_by(status="pending").count()),
        ("Avg applicants / job", round(apps.count() / n_orders, 1) if n_orders else 0),
        ("Jobs with voice note", posted.filter(Order.audio.isnot(None)).count()),
        ("Open to under-18s", posted.filter(Order.adult_only == False).count()),  # noqa: E712
        ("Adult-only jobs", posted.filter(Order.adult_only == True).count()),  # noqa: E712
        ("Open with no applicants", sum(1 for o in posted.filter(Order.status == "open").all() if o.applicant_count == 0)),
        ("Reports on jobs", db.session.query(Report.order_id).filter(Report.order_id.isnot(None)).distinct().count()),
    ]
    s, e = now - timedelta(days=29), now
    charts = dict(
        orders=_series(posted.filter(Order.created.between(s, e)).all(), s, e),
        apps=_series(apps.filter(OrderApplication.created.between(s, e)).all(), s, e),
        cats=dict(Counter(o.category for o in posted).most_common(8)),
        status={STATUS[k]: v for k, v in by_status.items() if v})
    areas = Counter(o.area for o in posted if o.area).most_common(8)
    makers = (db.session.query(User, func.count(Order.id)).join(Order, Order.maker_id == User.id).filter(Order.status != "draft")
              .group_by(User.id).order_by(func.count(Order.id).desc()).limit(8).all())
    hot = sorted(posted.filter(Order.status == "open").all(), key=lambda o: -o.applicant_count)[:8]
    return render_template("admin/market.html", stats=stats, charts=charts, areas=areas, makers=makers, hot=hot)


# ---------------- reports ----------------
@bp.route("/reports")
@staff_required
def reports():
    st = request.args.get("status", "pending")
    q = Report.query
    if st != "all":
        q = q.filter_by(status=st)
    return render_template("admin/reports.html", reports=q.order_by(Report.created.desc()).limit(200).all(), st=st,
                           durations=DURATIONS_MOD)


def _apply_report(do, reason, r):
    """Carry out the action chosen on a report. Returns a short description for the record, or None if it isn't allowed."""
    tgt, me = r.target, current_user
    if do in ("", "none"):
        return "Reviewed, no further action"
    if do == "remove_audio":
        if not (r.order and r.order.audio):
            return None
        _drop_audio(r.order.audio)
        r.order.audio, r.order.audio_seconds = None, 0
        notify(r.order.maker_id, "A voice note on your order was removed by moderators.", url_for("orders.detail", oid=r.order_id))
        return "Voice note removed"
    if do == "remove_photos":
        if not (r.order and r.order.photos):
            return None
        for p in list(r.order.photos):
            _drop_photo_files(p.name)
            db.session.delete(p)
        notify(r.order.maker_id, "Photos on your order were removed by moderators.", url_for("orders.detail", oid=r.order_id))
        return "Photos removed"
    if do == "suspend_order":
        if not r.order:
            return None
        clear_tracking(r.order_id)
        r.order.status = "closed"
        r.order.maker.violations = (r.order.maker.violations or 0) + 1
        notify(r.order.maker_id, f"Your order “{r.order.title}” was suspended by moderators." + (f" Reason: {reason}" if reason else ""))
        return "Order suspended + violation"
    if not tgt:
        return None
    if tgt.is_admin or (tgt.is_mod and not me.is_admin):
        return None
    if do == "warn":
        tgt.violations = (tgt.violations or 0) + 1
        notify(tgt.id, "Warning from moderators." + (f" Reason: {reason}" if reason else ""))
        return "Warning"
    if do == "suspend_user":
        tgt.status, tgt.sanction_note = "suspended", reason
        notify(tgt.id, "Your account was suspended." + (f" Reason: {reason}" if reason else ""))
        return "User suspended"
    if do == "ban":
        if not me.is_admin:
            return None
        tgt.status, tgt.sanction_note = "banned", reason
        return "User banned"
    if ":" in do:
        kind, code = do.split(":", 1)
        if kind in ("mute", "timeout"):
            return _sanction(tgt, kind, code, reason or r.reason)
    return None


@bp.route("/reports/<int:rid>/<act>", methods=["POST"])
@staff_required
def report_action(rid, act):
    r = db.get_or_404(Report, rid)
    new = {"investigate": "investigating", "dismiss": "dismissed", "action": "actioned"}.get(act) or abort(404)
    resolution = ""
    if act == "action":
        resolution = _apply_report(request.form.get("do", "none"), clean_text(request.form.get("reason", ""), 150), r)
        if resolution is None:
            flash("That action isn't available for this report (or needs an admin).", "error")
            return redirect(url_for("admin.reports", status=request.args.get("status", "pending")))
    elif act == "dismiss":
        resolution = "Dismissed"
    r.status, r.resolution, r.handler_id, r.handled_at = new, resolution, current_user.id, datetime.utcnow()
    notify(r.reporter_id, f"Your report #{r.id} is now {new}. Thank you for helping keep NearHand safe.")
    audit(f"report_{act}", f"#{r.id}", resolution)
    db.session.commit()
    flash(f"Report #{r.id} {new}" + (f": {resolution}." if resolution else "."), "success")
    return redirect(url_for("admin.reports", status=request.args.get("status", "pending")))


# ---------------- support ----------------
@bp.route("/tickets", methods=["GET", "POST"])
@staff_required
def tickets():
    if request.method == "POST":
        t = db.get_or_404(SupportTicket, request.form.get("id", type=int))
        t.status = "closed" if t.status == "open" else "open"
        audit("ticket_toggle", f"#{t.id}")
        db.session.commit()
        return redirect(url_for("admin.tickets"))
    return render_template("admin/tickets.html", tickets=SupportTicket.query.order_by(SupportTicket.status.desc(), SupportTicket.created.desc()).limit(200).all())


# ---------------- money (admins only) ----------------
def _find_user(ident):
    ident = (ident or "").strip()
    if not ident:
        return None
    return (User.query.filter(User.uid == ident.upper()).first() or User.query.filter(func.lower(User.email) == ident.lower()).first()
            or User.query.filter(User.username == ident.lower().lstrip("@")).first())


@bp.route("/payments")
@admin_required
def payments():
    pend = PaymentRequest.query.filter_by(status="pending").order_by(PaymentRequest.created).all()
    done = PaymentRequest.query.filter(PaymentRequest.status != "pending").order_by(PaymentRequest.handled_at.desc()).limit(40).all()
    recent = WalletTx.query.order_by(WalletTx.created.desc()).limit(25).all()
    users = {u.id: u for u in User.query.filter(User.id.in_({t.user_id for t in recent}))} if recent else {}
    return render_template("admin/payments.html", pend=pend, done=done, recent=recent, users=users)


@bp.route("/payments/add", methods=["POST"])
@admin_required
def payments_add():
    u = _find_user(request.form.get("who"))
    amt = to_int(request.form.get("amount"), 0, 1, 10**6)
    note = clean_text(request.form.get("note", ""), 90) or "Top-up"
    if not u:
        flash("No user found with that UID, username or email.", "error")
    elif amt < 1:
        flash("Enter an amount of at least ₹1.", "error")
    else:
        wallet_move(u, "deposit", amt, f"Added by admin: {note}")
        notify(u.id, f"₹{amt:,} was added to your wallet.", url_for("profile.wallet"))
        audit("wallet_add", u.uid, f"₹{amt} · {note}")
        db.session.commit()
        flash(f"Added ₹{amt:,} to {u.name} ({u.uid}).", "success")
    return redirect(url_for("admin.payments"))


@bp.route("/payments/<int:rid>/<act>", methods=["POST"])
@admin_required
def payment_action(rid, act):
    r = db.get_or_404(PaymentRequest, rid)
    note = clean_text(request.form.get("note", ""), 150)
    if r.status != "pending" or act not in ("approve", "reject"):
        flash("That request was already handled.", "error")
        return redirect(url_for("admin.payments"))
    u = r.user
    if act == "approve":
        if r.kind == "deposit":
            wallet_move(u, "deposit", r.amount, f"Top-up approved (#{r.id})")
            notify(u.id, f"₹{r.amount:,} was added to your wallet.", url_for("profile.wallet"))
        else:
            notify(u.id, f"Your payout of ₹{r.amount:,} to {r.dest} was marked as paid (demo).", url_for("profile.wallet", tab="withdraw"))
        r.status = "approved"
    else:
        if r.kind == "payout":
            wallet_move(u, "refund", r.amount, f"Payout #{r.id} rejected")
        notify(u.id, f"Your {r.kind} request of ₹{r.amount:,} was rejected." + (f" {note}" if note else ""), url_for("profile.wallet"))
        r.status = "rejected"
    r.admin_note, r.handler_id, r.handled_at = note, current_user.id, datetime.utcnow()
    audit(f"{r.kind}_{act}", u.uid, f"#{r.id} ₹{r.amount}")
    db.session.commit()
    flash(f"{r.kind.capitalize()} #{r.id} {r.status}.", "success")
    return redirect(url_for("admin.payments"))


@bp.route("/revenue")
@admin_required
def revenue():
    r, s, e = _range()
    tx = Transaction.query.filter(Transaction.created.between(s, e)).all()
    gross, fees = sum(t.amount for t in tx), sum(t.fee for t in tx)
    by = lambda key: sorted(((k, sum(t.fee for t in tx if key(t) == k), sum(t.amount for t in tx if key(t) == k)) for k in {key(t) for t in tx}), key=lambda x: -x[1])
    return render_template("admin/revenue.html", r=r, ranges=RANGES, args=request.args, gross=gross, fees=fees, n=len(tx),
                           avg=int(gross / len(tx)) if tx else 0, by_cat=by(lambda t: t.category), by_pay=by(lambda t: t.method),
                           chart=_series(tx, s, e, lambda t: t.fee), recent=sorted(tx, key=lambda t: t.created, reverse=True)[:25])


# ---------------- platform switches and staff (admins only) ----------------
@bp.route("/controls", methods=["GET", "POST"])
@admin_required
def controls():
    if request.method == "POST":
        notice = clean_text(request.form.get("notice", ""), 200)
        set_setting("notice", notice)
        audit("notice_set", "", notice)
        db.session.commit()
        flash("Notice saved." if notice else "Notice removed.", "success")
        return redirect(url_for("admin.controls"))
    return render_template("admin/controls.html")


@bp.route("/controls/<key>/toggle", methods=["POST"])
@admin_required
def control_toggle(key):
    if key not in SETTING_KEYS:
        abort(404)
    new = "0" if settings().get(key) == "1" else "1"
    set_setting(key, new)
    audit("pause_on" if new == "1" else "pause_off", key, SETTING_KEYS[key])
    db.session.commit()
    flash(f"{SETTING_KEYS[key]} {'paused' if new == '1' else 'resumed'}.", "success")
    return redirect(request.referrer or url_for("admin.controls"))


@bp.route("/staff", methods=["GET", "POST"])
@admin_required
def staff():
    if request.method == "POST":
        u = _find_user(request.form.get("who"))
        role = request.form.get("role")
        if not u or role not in ("mod", "admin"):
            flash("No user found with that UID, username or email.", "error")
        elif u.status != "active":
            flash("That account is not active.", "error")
        elif u.is_admin and role != "admin" and User.query.filter_by(is_admin=True, status="active").count() <= 1:
            flash("There must always be at least one active admin.", "error")
        else:
            u.is_admin, u.is_mod = role == "admin", role == "mod"
            notify(u.id, f"You are now a NearHand {'admin' if role == 'admin' else 'moderator'}.", url_for("admin.overview"))
            audit(f"make_{role}", u.uid, u.name)
            db.session.commit()
            flash(f"{u.name} ({u.uid}) is now {'an admin' if role == 'admin' else 'a moderator'}.", "success")
        return redirect(url_for("admin.staff"))
    team = User.query.filter(or_(User.is_admin == True, User.is_mod == True)).order_by(User.is_admin.desc(), User.created).all()  # noqa: E712
    return render_template("admin/staff.html", team=team)


@bp.route("/audit")
@admin_required
def audit_log():
    return render_template("admin/audit.html", logs=AuditLog.query.order_by(AuditLog.created.desc()).limit(300).all())


@bp.route("/announce", methods=["POST"])
@admin_required
def announce():
    text = request.form.get("text", "").strip()[:200]
    if text:
        for (uid,) in db.session.query(User.id).filter_by(status="active"):
            notify(uid, "Announcement: " + text)
        audit("announce", text[:60])
        db.session.commit()
        flash("Announcement sent to all active users.", "success")
    return redirect(url_for("admin.overview"))
