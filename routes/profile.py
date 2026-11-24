import os, re, uuid
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, jsonify, Response
from flask_login import login_required, current_user
from sqlalchemy import func, or_
from extensions import db
from models import (User, Order, OrderApplication, Review, Notification, Message, Block, Report, Transaction, WalletTx, PaymentRequest)
from utils import (can_message, is_blocked, notify, REPORT_REASONS, rate_limit, to_int, wallet_move, clean_text, restriction, qr_svg, is_paused)

bp = Blueprint("profile", __name__)
DONE = ["completed", "closed"]


# ---------------- profile (public view + owner hub) ----------------
def _ctx(u, owner):
    q = Order.query.filter(Order.maker_id == u.id)
    if not owner:
        q = q.filter(Order.status != "draft")
    posted = q.order_by(Order.created.desc()).limit(10).all()
    done = (OrderApplication.query.join(Order).filter(OrderApplication.worker_id == u.id, OrderApplication.status == "accepted",
                                                      Order.status.in_(DONE)).order_by(Order.created.desc()).limit(10).all())
    by_order = {r.order_id: r for r in Review.query.filter(Review.reviewee_id == u.id, Review.visible_clause(),
                                                           Review.order_id.in_([a.order_id for a in done]))} if done else {}
    revs = Review.query.filter(Review.reviewee_id == u.id, Review.visible_clause()).order_by(Review.created.desc()).all()
    dist = u.rating_distribution()
    ctx = dict(posted=posted, done=done, by_order=by_order, revs=revs[:6], dist=dist, reasons=REPORT_REASONS,
               can_msg=current_user.is_authenticated and can_message(current_user.id, u.id))
    if owner:  # private numbers: only ever sent to the account owner
        ctx["pending_reviews"] = Review.query.filter(Review.reviewee_id == u.id, ~Review.visible_clause()).count()
        ctx["earned"] = db.session.query(func.coalesce(func.sum(Transaction.amount - Transaction.fee), 0)).filter_by(worker_id=u.id).scalar()
        ctx["spent"] = db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter_by(maker_id=u.id).scalar()
    return ctx


@bp.route("/u/<username>")
def view(username):
    u = User.query.filter_by(username=username).first_or_404()
    owner = current_user.is_authenticated and current_user.id == u.id
    if u.status == "banned" and not (current_user.is_authenticated and current_user.is_admin) and not owner:
        abort(404)
    tab = request.args.get("tab", "overview")
    tab = tab if owner and tab in ("overview", "edit", "account") else "overview"
    return render_template("profile/me.html" if owner else "profile/view.html", u=u, tab=tab, errors={}, **_ctx(u, owner))


@bp.route("/profile")
@login_required
def me():
    return redirect(url_for("profile.view", username=current_user.username))


def _save_photo(file):
    """Re-encode to a 320px JPEG. This validates it is a real image and strips EXIF (incl. GPS)."""
    from PIL import Image, ImageOps
    try:
        img = Image.open(file.stream)
        img.verify()
        file.stream.seek(0)
        img = ImageOps.exif_transpose(Image.open(file.stream)).convert("RGB")
    except Exception:
        return None
    img = ImageOps.fit(img, (320, 320))
    folder = os.path.join(current_app.static_folder, "uploads", "avatars")
    os.makedirs(folder, exist_ok=True)
    name = uuid.uuid4().hex[:16] + ".jpg"
    img.save(os.path.join(folder, name), "JPEG", quality=85)
    return name


def _drop_photo(name):
    if name:
        try:
            os.remove(os.path.join(current_app.static_folder, "uploads", "avatars", os.path.basename(name)))
        except OSError:
            pass


@bp.route("/profile/edit", methods=["GET", "POST"])
@login_required
def edit():
    if request.method == "GET":
        return redirect(url_for("profile.view", username=current_user.username, tab="edit"))
    f, u, errors = request.form, current_user, {}
    name, username = f.get("name", "").strip(), f.get("username", "").strip().lower()
    phone = re.sub(r"[^\d+]", "", f.get("phone", ""))[:20]
    if not 2 <= len(name) <= 80: errors["name"] = "Enter your name (2–80 characters)."
    if not re.fullmatch(r"[a-z0-9_]{3,30}", username): errors["username"] = "3–30 letters, numbers or underscores."
    elif username != u.username and User.query.filter_by(username=username).first(): errors["username"] = "That username is taken."
    if phone and not re.fullmatch(r"\+?\d{7,15}", phone): errors["phone"] = "Enter a valid phone number."
    file, new_photo = request.files.get("photo"), None
    if file and file.filename:
        if file.mimetype not in ("image/jpeg", "image/png", "image/webp"):
            errors["photo"] = "Use a JPG, PNG or WebP image."
        else:
            new_photo = _save_photo(file)
            if not new_photo: errors["photo"] = "That file isn't a valid image."
    if errors:
        return render_template("profile/me.html", u=u, tab="edit", errors=errors, **_ctx(u, True))
    u.name, u.username = name, username
    u.headline, u.bio = f.get("headline", "").strip()[:80], f.get("bio", "").strip()[:300]
    u.work_description = f.get("work_description", "").strip()[:600]
    u.skills = ", ".join([s.strip()[:30] for s in f.get("skills", "").split(",") if s.strip()][:8])
    u.languages = ", ".join([s.strip()[:20] for s in f.get("languages", "").split(",") if s.strip()][:6])
    u.availability = f.get("availability") if f.get("availability") in ("Weekdays", "Weekends", "Evenings", "Anytime") else ""
    u.experience, u.area, u.phone = f.get("experience", "").strip()[:60], f.get("area", "").strip()[:80], phone
    u.work_radius_km = to_int(f.get("work_radius_km"), u.work_radius_km or 25, 1, 500)
    try:  # approximate point only (~1 km)
        u.lat, u.lng = (round(float(f["lat"]), 2), round(float(f["lng"]), 2)) if f.get("lat") and f.get("lng") else (None, None)
    except ValueError:
        pass
    if new_photo or f.get("remove_photo"):
        _drop_photo(u.photo)
        u.photo = new_photo
    db.session.commit()
    flash("Profile saved.", "success")
    return redirect(url_for("profile.view", username=u.username))


@bp.route("/profile/password", methods=["POST"])
@login_required
@rate_limit("pw", 6, 300)
def password():
    f = request.form
    if not current_user.check_password(f.get("current", "")):
        flash("Your current password is incorrect.", "error")
    elif len(f.get("new", "")) < 8 or not re.search(r"[A-Za-z]", f["new"]) or not re.search(r"\d", f["new"]):
        flash("Use 8+ characters with a letter and a number.", "error")
    elif f.get("new") != f.get("confirm"):
        flash("The new passwords don't match.", "error")
    else:
        current_user.set_password(f["new"])
        db.session.commit()
        flash("Password updated.", "success")
    return redirect(url_for("profile.view", username=current_user.username, tab="account"))


# ---------------- demo wallet ----------------
# Nothing here moves real money. A person "pays" the demo QR and asks for a top-up; an admin adds the money by hand.
# A payout request holds the amount straight away and an admin marks it paid (also by hand) or rejects it (refund).
def _tell_admins(text):
    for (aid,) in db.session.query(User.id).filter(User.is_admin == True, User.status == "active"):  # noqa: E712
        notify(aid, text, url_for("admin.payments"))


@bp.route("/wallet")
@login_required
def wallet():
    txs = WalletTx.query.filter_by(user_id=current_user.id).order_by(WalletTx.created.desc()).limit(50).all()
    reqs = PaymentRequest.query.filter_by(user_id=current_user.id).order_by(PaymentRequest.created.desc()).limit(15).all()
    return render_template("profile/wallet.html", txs=txs, reqs=reqs, tab=request.args.get("tab", "add"),
                           qr_label=current_app.config["DEMO_QR_LABEL"])


@bp.route("/wallet/qr.svg")
@login_required
def wallet_qr():
    """Demo QR. It encodes plain text (not a UPI link), so no payment app can ever send real money to it."""
    amt = to_int(request.args.get("amount"), 0, 0, 100000)
    text = f"{current_app.config['DEMO_QR_LABEL']} | UID {current_user.uid} | INR {amt}"
    return Response(qr_svg(text), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


@bp.route("/wallet/deposit", methods=["POST"])
@login_required
@rate_limit("wallet", 15, 60)
def wallet_deposit():
    amt = to_int(request.form.get("amount"), 0, 0, 10**7)
    ref = clean_text(request.form.get("ref", ""), 40)
    why = restriction("deposit")
    if why:
        flash(why, "error")
    elif not 10 <= amt <= 100000:
        flash("Enter an amount between ₹10 and ₹1,00,000.", "error")
    elif PaymentRequest.query.filter_by(user_id=current_user.id, kind="deposit", status="pending").count() >= 3:
        flash("You already have 3 add-money requests waiting. Please wait for an admin to review them.", "error")
    else:
        db.session.add(PaymentRequest(user_id=current_user.id, kind="deposit", amount=amt, ref=ref))
        _tell_admins(f"Add-money request: {current_user.name} ({current_user.uid}) asked for ₹{amt:,}")
        db.session.commit()
        flash(f"Request sent for ₹{amt:,}. An admin will add it to your wallet (demo: no real money moves).", "success")
    return redirect(url_for("profile.wallet", tab="add"))


@bp.route("/wallet/withdraw", methods=["POST"])
@login_required
@rate_limit("wallet", 15, 60)
def wallet_withdraw():
    amt = to_int(request.form.get("amount"), 0, 0, 10**7)
    dest = clean_text(request.form.get("dest", ""), 60)
    why = restriction("payout")
    if why:
        flash(why, "error")
    elif amt < 10:
        flash("Minimum payout is ₹10.", "error")
    elif amt > (current_user.balance or 0):
        flash("You don't have enough balance for that.", "error")
    elif len(dest) < 4:
        flash("Enter a UPI ID or bank account for the payout.", "error")
    elif PaymentRequest.query.filter_by(user_id=current_user.id, kind="payout", status="pending").count() >= 3:
        flash("You already have 3 payout requests waiting. Please wait for an admin to review them.", "error")
    else:
        wallet_move(current_user, "withdraw", -amt, f"Payout requested to {dest}")   # held now, refunded if rejected
        db.session.add(PaymentRequest(user_id=current_user.id, kind="payout", amount=amt, dest=dest))
        _tell_admins(f"Payout request: {current_user.name} ({current_user.uid}) asked for ₹{amt:,}")
        db.session.commit()
        flash(f"Payout of ₹{amt:,} requested. An admin will review it (demo: no real money moves).", "success")
    return redirect(url_for("profile.wallet", tab="withdraw"))


@bp.route("/wallet/request/<int:rid>/cancel", methods=["POST"])
@login_required
def wallet_cancel(rid):
    r = PaymentRequest.query.filter_by(id=rid, user_id=current_user.id, status="pending").first_or_404()
    r.status, r.admin_note, r.handled_at = "rejected", "Cancelled by you", datetime.utcnow()
    if r.kind == "payout":
        wallet_move(current_user, "refund", r.amount, f"Payout #{r.id} cancelled")
    db.session.commit()
    flash("Request cancelled." + (" The money is back in your wallet." if r.kind == "payout" else ""), "success")
    return redirect(url_for("profile.wallet", tab="add" if r.kind == "deposit" else "withdraw"))


# ---------------- home / lists ----------------
@bp.route("/dashboard")
@login_required
def dashboard():
    uid = current_user.id
    tab = request.args.get("tab", "posted")
    tab = tab if tab in ("posted", "working") else "posted"
    # Jobs are now created in one step. Remove any abandoned drafts left over from the old two-step flow.
    if Order.query.filter_by(maker_id=uid, status="draft").delete():
        db.session.commit()
    posted = Order.query.filter(Order.maker_id == uid, Order.status != "draft").order_by(Order.created.desc()).limit(60).all()
    working = (OrderApplication.query.join(Order).filter(OrderApplication.worker_id == uid, OrderApplication.status.in_(["pending", "accepted"]))
               .order_by(OrderApplication.created.desc()).limit(60).all())
    waiting = (OrderApplication.query.join(Order).filter(Order.maker_id == uid, OrderApplication.status == "pending",
                                                         Order.status.in_(["open", "fully_booked"])).count())
    return render_template("profile/dashboard.html", tab=tab, posted=posted, working=working, waiting=waiting)


@bp.route("/my/orders")
@login_required
def my_orders():
    return redirect(url_for("profile.dashboard", tab="posted"))


@bp.route("/my/applications")
@login_required
def my_apps():
    return redirect(url_for("profile.dashboard", tab="working"))


@bp.route("/history")
@login_required
def history():
    """History & earnings: Jobs done (money earned) and Jobs created (money spent), with the amount after each job name."""
    uid = current_user.id
    tab = request.args.get("tab", "done")
    tab = tab if tab in ("done", "created") else "done"
    finished = ["completed", "closed", "cancelled", "disputed"]
    created = Order.query.filter(Order.maker_id == uid, Order.status.in_(finished)).order_by(Order.created.desc()).all()
    done = (OrderApplication.query.join(Order).filter(OrderApplication.worker_id == uid, OrderApplication.status.in_(["accepted", "removed"]),
                                                      Order.status.in_(finished)).order_by(Order.created.desc()).all())
    mine_as_worker = {t.order_id: t for t in Transaction.query.filter_by(worker_id=uid)}
    in_wallet = {w.order_id: w.amount for w in WalletTx.query.filter_by(user_id=uid, kind="earning") if w.order_id}
    spent = {}
    for t in Transaction.query.filter_by(maker_id=uid):
        s = spent.setdefault(t.order_id, [0, 0])
        s[0] += t.amount
        s[1] += 1
    done_rows = []
    for a in done:
        t = mine_as_worker.get(a.order_id)
        net = (t.amount - t.fee) if t else 0
        done_rows.append(dict(order=a.order, amount=net, in_wallet=a.order_id in in_wallet, method=a.order.pay_method))
    created_rows = [dict(order=o, amount=spent.get(o.id, [0, 0])[0], people=spent.get(o.id, [0, 0])[1]) for o in created]
    return render_template("profile/history.html", tab=tab, done_rows=done_rows, created_rows=created_rows,
                           earned=sum(r["amount"] for r in done_rows), spent_total=sum(r["amount"] for r in created_rows))


@bp.route("/notifications", methods=["GET", "POST"])
@login_required
def notifications():
    q = Notification.query.filter_by(user_id=current_user.id)
    if request.method == "POST":
        q.filter_by(read=False).update({"read": True})
        db.session.commit()
        return redirect(url_for("profile.notifications"))
    items = q.order_by(Notification.created.desc()).limit(60).all()
    return render_template("profile/notifications.html", items=items, unread_ids=[n.id for n in items if not n.read])


@bp.route("/notifications/<int:nid>")
@login_required
def open_notification(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.read = True
    db.session.commit()
    return redirect(n.url if n.url and n.url.startswith("/") else url_for("profile.notifications"))


# ---------------- chat ----------------
def _convos(me):
    msgs = Message.query.filter(or_(Message.sender_id == me, Message.recipient_id == me)).order_by(Message.created.desc()).all()
    convos = {}
    for m in msgs:
        other = m.recipient_id if m.sender_id == me else m.sender_id
        c = convos.setdefault(other, dict(last=m, unread=0))
        if m.recipient_id == me and not m.read:
            c["unread"] += 1
    users = {u.id: u for u in User.query.filter(User.id.in_(convos.keys()))} if convos else {}
    return [(users[k], v) for k, v in convos.items() if k in users]


def _mj(m, me):
    return dict(id=m.id, mine=m.sender_id == me, body=m.body, ts=m.created.isoformat() + "Z", read=bool(m.read))


@bp.route("/chat")
@login_required
def messages():
    return render_template("messages/chat.html", convos=_convos(current_user.id), active=None, data=None)


@bp.route("/chat/<username>", methods=["GET", "POST"])
@login_required
@rate_limit("msg", 40, 60)
def thread(username):
    other = User.query.filter_by(username=username).first_or_404()
    me = current_user.id
    pair = or_((Message.sender_id == me) & (Message.recipient_id == other.id), (Message.sender_id == other.id) & (Message.recipient_id == me))
    allowed = can_message(me, other.id)
    if not allowed and not Message.query.filter(pair).count():
        flash("You can chat once you've applied to, or received an application from, each other.", "error")
        return redirect(url_for("profile.messages"))
    blocked = is_blocked(me, other.id)
    if request.method == "POST":  # no-JS fallback
        body = clean_text(request.form.get("body", ""), 1000, multiline=True)
        why = restriction("chat")
        if why:
            flash(why, "error")
        elif body and allowed and not blocked:
            db.session.add(Message(sender_id=me, recipient_id=other.id, body=body))
            notify(other.id, f"New message from {current_user.name}", url_for("profile.thread", username=current_user.username))
            db.session.commit()
        return redirect(url_for("profile.thread", username=username))
    Message.query.filter_by(sender_id=other.id, recipient_id=me, read=False).update({"read": True})
    db.session.commit()
    msgs = Message.query.filter(pair).order_by(Message.created).all()
    i_blocked = Block.query.filter_by(blocker_id=me, blocked_id=other.id).first() is not None
    return render_template("messages/chat.html", convos=_convos(me), active=other, data=dict(me=me, user=other.username, msgs=[_mj(m, me) for m in msgs]),
                           can_send=allowed and not blocked and not restriction("chat"), i_blocked=i_blocked, reasons=REPORT_REASONS)


@bp.route("/chat/<username>/send", methods=["POST"])
@login_required
@rate_limit("msg", 40, 60)
def chat_send(username):
    other = User.query.filter_by(username=username).first_or_404()
    body = clean_text((request.get_json(silent=True) or {}).get("body") or "", 1000, multiline=True)
    why = restriction("chat")
    if why:
        return jsonify(ok=False, error=why), 403
    if not body or not can_message(current_user.id, other.id):
        return jsonify(ok=False, error="You can't send messages here."), 400
    m = Message(sender_id=current_user.id, recipient_id=other.id, body=body)
    db.session.add(m)
    notify(other.id, f"{current_user.name}: {body[:90]}", url_for("profile.thread", username=current_user.username))
    db.session.commit()
    return jsonify(ok=True, msg=_mj(m, current_user.id))


@bp.route("/chat/<username>/poll")
@login_required
def chat_poll(username):
    other = User.query.filter_by(username=username).first_or_404()
    me, after = current_user.id, request.args.get("after", 0, type=int)
    new = Message.query.filter(Message.sender_id == other.id, Message.recipient_id == me, Message.id > after).order_by(Message.id).all()
    for m in new:
        m.read = True
    db.session.commit()
    seen = [m.id for m in Message.query.filter_by(sender_id=me, recipient_id=other.id, read=True).order_by(Message.id.desc()).limit(40)]
    return jsonify(msgs=[_mj(m, me) for m in new], seen=seen)


@bp.route("/chat/<username>/block", methods=["POST"])
@login_required
def block(username):
    other = User.query.filter_by(username=username).first_or_404()
    b = Block.query.filter_by(blocker_id=current_user.id, blocked_id=other.id).first()
    if b:
        db.session.delete(b)
        flash(f"{other.name} unblocked.", "success")
    elif other.id != current_user.id:
        db.session.add(Block(blocker_id=current_user.id, blocked_id=other.id))
        flash(f"{other.name} blocked. They can't message you.", "success")
    db.session.commit()
    return redirect(url_for("profile.thread", username=username))


@bp.route("/report/user/<username>", methods=["POST"])
@login_required
@rate_limit("report_user", 10, 300)
def report_user(username):
    other = User.query.filter_by(username=username).first_or_404()
    reason = request.form.get("reason")
    if other.id == current_user.id or reason not in REPORT_REASONS:
        flash("Pick a reason for the report.", "error")
    else:
        db.session.add(Report(reporter_id=current_user.id, user_id=other.id, reason=reason, details=request.form.get("details", "").strip()[:500]))
        db.session.commit()
        flash("Report sent to our moderation team.", "success")
    return redirect(request.referrer or url_for("profile.view", username=username))
