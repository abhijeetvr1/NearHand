import io, os, secrets, shutil, subprocess, uuid
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, jsonify, session
from flask_login import login_required, current_user
from extensions import db
from models import Order, OrderApplication, Review, SavedOrder, Report, Transaction, OrderPhoto, LiveLocation
from utils import (cap_title, CATEGORIES, PAY, DURATION, REPORT_REASONS, to_int, notify, distance_to, can_message, rate_limit, wallet_move,
                   restriction, order_hidden_from_viewer, in_worker_area, user_loc, user_radius, haversine, audit, youth_check, parse_time,
                   clear_tracking, youth_hours_open, youth_window)
from models import User

bp = Blueprint("orders", __name__, url_prefix="/orders")
EDITABLE = ("open", "fully_booked")


def _get(oid):
    return db.get_or_404(Order, oid)


def _owner(oid):
    o = _get(oid)
    if o.maker_id != current_user.id:
        abort(403)
    return o


def _date(v):
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        return None


# ---------------- voice note (optional, 1.5 minutes max) ----------------
AUDIO_MAGIC = ((b"\x1aE\xdf\xa3", 0, "webm"), (b"OggS", 0, "ogg"), (b"ftyp", 4, "m4a"), (b"RIFF", 0, "wav"), (b"ID3", 0, "mp3"), (b"\xff\xfb", 0, "mp3"))


def _audio_dir():
    d = os.path.join(current_app.static_folder, "uploads", "audio")
    os.makedirs(d, exist_ok=True)
    return d


def _audio_ext(head):
    for sig, off, ext in AUDIO_MAGIC:
        if head[off:off + len(sig)] == sig:
            return ext
    return None


def _probe_seconds(path):
    """Real length via ffprobe when it is installed; None when it isn't (the size cap and the recorder's own timer still apply)."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        return float(out)
    except Exception:
        return None


def _drop_audio(name):
    if name:
        try:
            os.remove(os.path.join(_audio_dir(), os.path.basename(name)))
        except OSError:
            pass


def _audio_from_form(f, current=None):
    """Return (file_name_or_None, seconds, error). Only a file this session just uploaded (or the order's own) can be attached."""
    if f.get("remove_audio"):
        return None, 0, None
    name = (f.get("audio_name") or "").strip()
    if not name:
        return current.audio if current else None, (current.audio_seconds if current else 0), None
    if name != (current.audio if current else None) and name not in session.get("audio_pending", []):
        return None, 0, "That voice note expired. Please record it again."
    if not os.path.exists(os.path.join(_audio_dir(), os.path.basename(name))):
        return None, 0, "That voice note could not be found. Please record it again."
    return name, to_int(f.get("audio_seconds"), 0, 0, current_app.config["MAX_AUDIO_SECONDS"]), None


@bp.route("/audio", methods=["POST"])
@login_required
@rate_limit("audio", 12, 300)
def upload_audio():
    """Receives the recorded voice note as soon as recording stops, so the job form only carries a file name."""
    why = restriction("post")
    if why:
        return jsonify(ok=False, error=why), 403
    if current_user.is_minor:
        return jsonify(ok=False, error="Posting work is for accounts aged 18 or older."), 403
    cfg = current_app.config
    file = request.files.get("audio")
    if not file:
        return jsonify(ok=False, error="No recording received."), 400
    data = file.read(cfg["MAX_AUDIO_BYTES"] + 1)
    if not data or len(data) > cfg["MAX_AUDIO_BYTES"]:
        return jsonify(ok=False, error="That recording is too long. The limit is 1 minute 30 seconds."), 400
    ext = _audio_ext(data[:12])
    if not ext:
        return jsonify(ok=False, error="That audio format isn't supported."), 400
    secs = to_int(request.form.get("seconds"), 0, 0, 3600)
    if secs > cfg["MAX_AUDIO_SECONDS"] + 2:
        return jsonify(ok=False, error="Voice notes can be at most 1 minute 30 seconds."), 400
    name = uuid.uuid4().hex[:16] + "." + ext
    path = os.path.join(_audio_dir(), name)
    with open(path, "wb") as fh:
        fh.write(data)
    real = _probe_seconds(path)
    if real is not None and real > cfg["MAX_AUDIO_SECONDS"] + 3:
        _drop_audio(name)
        return jsonify(ok=False, error="Voice notes can be at most 1 minute 30 seconds."), 400
    pend = (session.get("audio_pending") or [])[-4:] + [name]
    session["audio_pending"] = pend
    return jsonify(ok=True, name=name, url=url_for("static", filename="uploads/audio/" + name), seconds=min(secs, cfg["MAX_AUDIO_SECONDS"]))


# ---------------- photos (optional, up to MAX_PHOTOS per job) ----------------
def _photo_dir():
    d = os.path.join(current_app.static_folder, "uploads", "photos")
    os.makedirs(d, exist_ok=True)
    return d


def _drop_photo_files(name):
    for n in (name, "t_" + name):
        try:
            os.remove(os.path.join(_photo_dir(), os.path.basename(n)))
        except OSError:
            pass


def _save_photo(file):
    """Verify it is really an image, remove location data (EXIF), shrink it, store a full size and a small copy. Returns the file name."""
    from PIL import Image, ImageOps
    data = file.read(4 * 1024 * 1024 + 1)
    if not data or len(data) > 4 * 1024 * 1024:
        raise ValueError("That photo is too large (4 MB max).")
    try:
        probe = Image.open(io.BytesIO(data)); probe.verify()
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > 40_000_000:
            raise ValueError("That photo is too large.")
        img = ImageOps.exif_transpose(img).convert("RGB")
    except ValueError:
        raise
    except Exception:
        raise ValueError("That file isn't a photo we can use. Try a JPG or PNG.")
    name = uuid.uuid4().hex[:16] + ".jpg"
    full = img.copy(); full.thumbnail((1600, 1600))
    full.save(os.path.join(_photo_dir(), name), "JPEG", quality=82, optimize=True)
    small = img.copy(); small.thumbnail((520, 520))
    small.save(os.path.join(_photo_dir(), "t_" + name), "JPEG", quality=78, optimize=True)
    return name


@bp.route("/photo", methods=["POST"])
@login_required
@rate_limit("photo", 30, 300)
def upload_photo():
    """One photo per request (the browser sends them one by one). Attached to the job when the form is saved."""
    why = restriction("post")
    if why:
        return jsonify(ok=False, error=why), 403
    if current_user.is_minor:
        return jsonify(ok=False, error="Posting work is for accounts aged 18 or older."), 403
    file = request.files.get("photo")
    if not file:
        return jsonify(ok=False, error="No photo received."), 400
    pend = session.get("photo_pending", [])
    if len(pend) >= current_app.config["MAX_PHOTOS"] * 3:
        return jsonify(ok=False, error="Too many uploads. Save your job first."), 429
    try:
        name = _save_photo(file)
    except ValueError as ex:
        return jsonify(ok=False, error=str(ex)), 400
    session["photo_pending"] = pend + [name]
    return jsonify(ok=True, name=name, url=url_for("static", filename="uploads/photos/" + name), thumb=url_for("static", filename="uploads/photos/t_" + name))


def _photos_from_form(f, order=None):
    """-> (new_names, remove_ids, error). New names must have been uploaded by this session; the total can't pass MAX_PHOTOS."""
    names = []
    for n in (f.get("photo_names") or "").split(","):
        n = os.path.basename(n.strip())
        if n and n not in names:
            if n not in session.get("photo_pending", []) or not os.path.exists(os.path.join(_photo_dir(), n)):
                return [], [], "A photo expired. Please add it again."
            names.append(n)
    remove = {int(x) for x in (f.get("remove_photos") or "").split(",") if x.strip().isdigit()}
    keep = [p for p in (order.photos if order else []) if p.id not in remove]
    if len(keep) + len(names) > current_app.config["MAX_PHOTOS"]:
        return [], [], f"You can add up to {current_app.config['MAX_PHOTOS']} photos."
    return names, remove, None


def _apply_photos(o, names, remove):
    for p in list(o.photos):
        if p.id in remove:
            _drop_photo_files(p.name)
            db.session.delete(p)
    pos = max([p.position or 0 for p in o.photos] + [0])
    for n in names:
        pos += 1
        o.photos.append(OrderPhoto(name=n, position=pos))
    session["photo_pending"] = [n for n in session.get("photo_pending", []) if n not in names]


@bp.route("/youth-check", methods=["POST"])
@login_required
@rate_limit("youthcheck", 40, 60)
def youth_check_api():
    """Live safety check behind the 'Suitable for under 18' box on the Post work form."""
    f = request.get_json(silent=True) or {}
    issues = youth_check({k: str(f.get(k) or "")[:2000] for k in ("title", "description", "category", "skills", "conditions", "work_from", "work_to")})
    return jsonify(ok=not issues, issues=list(issues.values()), fields=list(issues))


def _guard_poster(edit=False):
    """Shared checks for posting work. Returns a redirect response when the person can't post, else None."""
    if current_user.is_minor:
        flash("Posting work is for accounts aged 18 or older. You can still find and apply for work that is suitable for under-18s.", "error")
        return redirect(url_for("market.index"))
    why = restriction("post")
    if why:
        flash(why, "error")
        return redirect(url_for("profile.dashboard"))
    return None


def _parse(f):
    e = {}
    title, desc = cap_title(" ".join(f.get("title", "").split())), f.get("description", "").strip()
    if not 5 <= len(title) <= 60 or len(title.split()) > 10: e["title"] = "Use 5–60 characters and at most 10 words."
    if not 20 <= len(desc) <= 2000: e["description"] = "Describe the work in 20–2000 characters."
    if f.get("category") not in CATEGORIES: e["category"] = "Pick a category."
    pay = to_int(f.get("pay_per_worker"), 0, 0)
    if pay < 1: e["pay_per_worker"] = "Enter the amount each person will be paid."
    if f.get("pay_method") not in PAY: e["pay_method"] = "Choose how you will pay."
    if f.get("duration") not in DURATION: e["duration"] = "Choose an estimate."
    area = f.get("area", "").strip()
    if not 2 <= len(area) <= 80: e["area"] = "Enter the nearby area or locality."
    start, dl = _date(f.get("start_date")), _date(f.get("deadline"))
    if start and dl and dl < start: e["deadline"] = "Deadline can't be before the start date."
    radius = to_int(f.get("custom_radius"), 10, 1, 500) if f.get("radius_choice") == "custom" else to_int(f.get("radius_choice"), 10, 1, 500)
    skills = ", ".join([s.strip()[:30] for s in f.get("skills", "").split(",") if s.strip()][:6])
    lat = lng = None
    try:  # round to 2 decimals (~1 km). Exact GPS is never stored.
        if f.get("lat") and f.get("lng"):
            lat, lng = round(float(f["lat"]), 2), round(float(f["lng"]), 2)
            assert -90 <= lat <= 90 and -180 <= lng <= 180
    except (ValueError, AssertionError):
        lat = lng = None
    exact_lat = exact_lng = None
    if lat is None:
        e["location"] = "Place the pin where the work happens: tap Detect my location or tap the map."
    else:  # the exact pin is private: only the maker and accepted workers ever see it (the public point above is rounded to ~1 km)
        exact_lat, exact_lng = round(float(f["lat"]), 5), round(float(f["lng"]), 5)
    wf, wt = parse_time(f.get("work_from")), parse_time(f.get("work_to"))
    if bool(f.get("work_from")) != bool(f.get("work_to")) or (f.get("work_from") and not (wf and wt)):
        e["work_hours"] = "Enter both a start and an end time, or leave both empty."
        wf = wt = None
    youth_ok = False
    if f.get("youth_ok"):   # the poster says it's suitable for under-18s: check the words, the category and the hours
        issues = youth_check({"title": title, "description": desc, "category": f.get("category"), "skills": skills,
                              "conditions": f.get("conditions", ""), "work_from": f.get("work_from"), "work_to": f.get("work_to")})
        if issues:
            for k in ("title", "description", "category", "work_hours"):
                if k in issues and k not in e:
                    e[k] = issues[k]
            e["youth"] = "This job can't be opened to under-18s yet. Fix these, or untick the box: " + " ".join(issues.values())
        else:
            youth_ok = True
    d = dict(title=title, description=desc, category=f.get("category"), skills=skills,
             workers_needed=to_int(f.get("workers_needed"), 1, 1, 100), duration=f.get("duration"),
             start_date=start, deadline=dl, pay_per_worker=pay, bonus=to_int(f.get("bonus"), 0, 0),
             pay_method=f.get("pay_method"), conditions=f.get("conditions", "").strip()[:400], area=area,
             radius_km=radius, exact_address=f.get("exact_address", "").strip()[:200], lat=lat, lng=lng,
             exact_lat=exact_lat, exact_lng=exact_lng, work_from=wf, work_to=wt, adult_only=not youth_ok)
    return d, e


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    """Step 1 (form) and step 2 (review). Nothing is saved until the user presses Confirm & publish."""
    blocked = _guard_poster()
    if blocked:
        return blocked
    errors, form = {}, {}
    if request.method == "POST":
        form = request.form
        if not form.get("back"):  # "Edit order" on the review screen only re-opens the form, filled in
            data, errors = _parse(form)
            audio, secs, aerr = _audio_from_form(form)
            if aerr:
                errors["audio"] = aerr
            pnames, _, perr = _photos_from_form(form)
            if perr:
                errors["photos"] = perr
            if not errors:
                session["order_nonce"] = nonce = secrets.token_hex(8)
                fields = {k: v for k, v in form.items() if k not in ("csrf_token", "back")}
                return render_template("orders/summary.html", o=Order(audio=audio, audio_seconds=secs, **data), fields=fields, nonce=nonce, pnames=pnames)
    topup = (current_user.balance or 0) <= 0 and not session.get("topup_skipped") and request.method == "GET"
    return render_template("orders/form.html", order=None, errors=errors, form=form, categories=CATEGORIES, topup=topup)


@bp.route("/publish", methods=["POST"])
@login_required
@rate_limit("publish", 10, 60)
def publish():
    """Creates the order as Open in one step. The one-time nonce makes a double tap or a refresh harmless."""
    blocked = _guard_poster()
    if blocked:
        return blocked
    if not request.form.get("nonce") or request.form["nonce"] != session.pop("order_nonce", None):
        flash("That job was already published.", "success")
        return redirect(url_for("profile.dashboard"))
    data, errors = _parse(request.form)
    audio, secs, aerr = _audio_from_form(request.form)
    if aerr:
        errors["audio"] = aerr
    pnames, _, perr = _photos_from_form(request.form)
    if perr:
        errors["photos"] = perr
    if errors:
        flash("Please check the details and try again.", "error")
        return render_template("orders/form.html", order=None, errors=errors, form=request.form, categories=CATEGORIES, topup=False)
    o = Order(maker_id=current_user.id, status="open", audio=audio, audio_seconds=secs, **data)
    db.session.add(o)
    session["audio_pending"] = [n for n in session.get("audio_pending", []) if n != audio]
    _apply_photos(o, pnames, set())
    db.session.commit()
    return redirect(url_for("orders.published", oid=o.id))


@bp.route("/<int:oid>/published")
@login_required
def published(oid):
    return render_template("orders/published.html", o=_owner(oid))


@bp.route("/topup-skip", methods=["POST"])
@login_required
def topup_skip():
    session["topup_skipped"] = True
    return jsonify(ok=True)


@bp.route("/<int:oid>/edit", methods=["GET", "POST"])
@login_required
def edit(oid):
    o = _owner(oid)
    if o.status not in EDITABLE:
        flash("This order can't be edited in its current state.", "error")
        return redirect(url_for("orders.detail", oid=oid))
    why = restriction("post")
    if why:
        flash(why, "error")
        return redirect(url_for("orders.detail", oid=oid))
    errors = {}
    if request.method == "POST":
        data, errors = _parse(request.form)
        if data["workers_needed"] < o.accepted:
            errors["workers_needed"] = f"{o.accepted} people are already accepted."
        audio, secs, aerr = _audio_from_form(request.form, current=o)
        if aerr:
            errors["audio"] = aerr
        pnames, premove, perr = _photos_from_form(request.form, order=o)
        if perr:
            errors["photos"] = perr
        if not errors:
            _apply_photos(o, pnames, premove)
            if o.audio and audio != o.audio:
                _drop_audio(o.audio)
            for k, v in data.items():
                setattr(o, k, v)
            o.audio, o.audio_seconds = audio, secs
            o.sync_status()
            db.session.commit()
            flash("Changes saved.", "success")
            return redirect(url_for("orders.detail", oid=o.id))
    return render_template("orders/form.html", order=o, errors=errors, form=request.form if request.method == "POST" else {}, categories=CATEGORIES, topup=False)


@bp.route("/<int:oid>")
def detail(oid):
    o = _get(oid)
    mine = current_user.is_authenticated and o.maker_id == current_user.id
    if (o.status == "draft" and not mine) or order_hidden_from_viewer(o):
        abort(404)
    my_app = None
    reviews_todo = []
    if current_user.is_authenticated:
        my_app = OrderApplication.query.filter_by(order_id=oid, worker_id=current_user.id).first()
        if o.status in ("completed", "closed"):
            done = {(r.reviewee_id) for r in Review.query.filter_by(order_id=oid, reviewer_id=current_user.id)}
            if mine:
                reviews_todo = [a.worker for a in o.applications if a.status == "accepted" and a.worker_id not in done]
            elif my_app and my_app.status == "accepted" and o.maker_id not in done:
                reviews_todo = [o.maker]
    show_address = mine or bool(my_app and my_app.status == "accepted")
    pending = sum(1 for a in o.applications if a.status == "pending")
    dist = distance_to(o)
    out_of_range = bool(dist is not None and dist > (o.radius_km or 10) + 1.5)
    blocked_why = restriction("apply") if current_user.is_authenticated and not mine else None
    return render_template("orders/detail.html", o=o, mine=mine, my_app=my_app, dist=dist, out_of_range=out_of_range,
                           blocked_why=blocked_why, no_loc=not user_loc(),
                           reviews_todo=reviews_todo, show_address=show_address, pending=pending,
                           reasons=REPORT_REASONS, can_msg=current_user.is_authenticated and can_message(current_user.id, o.maker_id))


@bp.route("/<int:oid>/apply", methods=["POST"])
@login_required
@rate_limit("apply", 15, 60)
def apply(oid):
    o = _get(oid)
    if order_hidden_from_viewer(o):
        abort(404)
    msg = request.form.get("message", "").strip()
    why = restriction("apply")
    loc = user_loc()
    if why:
        flash(why, "error")
    elif o.maker_id == current_user.id:
        flash("You can't apply to your own order.", "error")
    elif o.status != "open":
        flash("This order isn't accepting applications.", "error")
    elif not loc and o.lat is not None:
        flash("Set your work area first so we can check you are inside this job's worker radius.", "error")
    elif loc and o.lat is not None and haversine(loc, (o.lat, o.lng)) > (o.radius_km or 10) + 1.5:
        flash(f"This job only takes workers within {o.radius_km} km. You are outside that radius.", "error")
    elif not 10 <= len(msg) <= 500:
        flash("Tell the maker why you're a good fit (10–500 characters).", "error")
    else:
        a = OrderApplication.query.filter_by(order_id=oid, worker_id=current_user.id).first()
        if a and a.status != "withdrawn":
            flash("You've already applied.", "error")
            return redirect(url_for("orders.detail", oid=oid))
        a = a or OrderApplication(order_id=oid, worker_id=current_user.id)
        a.message, a.status = msg, "pending"
        a.experience = request.form.get("experience", "").strip()[:300]
        a.portfolio = request.form.get("portfolio", "").strip()[:200]
        a.availability = request.form.get("availability", "").strip()[:120]
        db.session.add(a)
        notify(o.maker_id, f"{current_user.name} applied to “{o.title}”", url_for("orders.manage", oid=oid))
        db.session.commit()
        flash("Application sent. You'll be notified when the maker responds.", "success")
    return redirect(url_for("orders.detail", oid=oid))


@bp.route("/<int:oid>/withdraw", methods=["POST"])
@login_required
def withdraw(oid):
    a = OrderApplication.query.filter_by(order_id=oid, worker_id=current_user.id).first_or_404()
    if a.status in ("pending", "accepted") and a.order.status in ("open", "fully_booked"):
        a.status = "withdrawn"
        a.order.sync_status()
        notify(a.order.maker_id, f"{current_user.name} withdrew from “{a.order.title}”", url_for("orders.manage", oid=oid))
        db.session.commit()
        flash("Application withdrawn.", "success")
    return redirect(url_for("orders.detail", oid=oid))


@bp.route("/<int:oid>/manage")
@login_required
def manage(oid):
    o = _owner(oid)
    apps = sorted(o.applications, key=lambda a: ({"pending": 0, "accepted": 1}.get(a.status, 2), a.created))
    return render_template("orders/manage.html", o=o, apps=apps, reasons=REPORT_REASONS)


@bp.route("/<int:oid>/app/<int:aid>/<act>", methods=["POST"])
@login_required
def app_action(oid, aid, act):
    o = _owner(oid)
    a = OrderApplication.query.filter_by(id=aid, order_id=oid).first_or_404()
    if act == "accept":
        if o.status != "open" or o.slots_left < 1 or a.status != "pending":
            flash("This person can't be accepted right now.", "error")
        else:
            a.status = "accepted"
            db.session.flush()
            o.sync_status()
            notify(a.worker_id, f"You're in! “{o.title}” accepted you. Tap to see your route.", url_for("orders.live", oid=oid))
            flash(f"{a.worker.name} accepted." + (" Order is now fully booked." if o.status == "fully_booked" else ""), "success")
    elif act == "reject" and a.status == "pending":
        a.status = "rejected"
        notify(a.worker_id, f"Your application for “{o.title}” was declined", url_for("orders.detail", oid=oid))
        flash("Application declined.", "success")
    elif act == "remove" and a.status == "accepted" and o.status in ("open", "fully_booked", "in_progress"):
        a.status = "removed"
        LiveLocation.query.filter_by(order_id=oid, user_id=a.worker_id).delete()
        db.session.flush()
        o.sync_status()
        notify(a.worker_id, f"You were removed from “{o.title}”", url_for("orders.detail", oid=oid))
        flash(f"{a.worker.name} removed.", "success")
    else:
        flash("That action isn't available.", "error")
    db.session.commit()
    return redirect(url_for("orders.manage", oid=oid))


@bp.route("/<int:oid>/<act>", methods=["POST"])
@login_required
def action(oid, act):
    o = _owner(oid)
    accepted = [a for a in o.applications if a.status == "accepted"]
    if act == "start" and o.status in ("open", "fully_booked") and accepted:
        o.status = "in_progress"
        for a in accepted:
            notify(a.worker_id, f"“{o.title}” is starting. Open your live route.", url_for("orders.live", oid=oid))
        flash("Work started. New applications are closed.", "success")
    elif act == "complete" and o.status == "in_progress":
        clear_tracking(o.id)
        fee_rate = current_app.config["PLATFORM_FEE"]
        share = (o.bonus or 0) // max(len(accepted), 1)
        amounts = {a.worker_id: o.pay_per_worker + share for a in accepted}
        if o.pay_method == "online" and (current_user.balance or 0) < sum(amounts.values()):
            flash(f"Add {sum(amounts.values()) - (current_user.balance or 0):,} more rupees to your wallet to release online payment.", "error")
            return redirect(url_for("profile.wallet"))
        o.status = "completed"
        credit_cash = current_app.config.get("WALLET_CREDIT_CASH_JOBS", True)
        for a in accepted:
            amt = amounts[a.worker_id]
            fee = round(amt * fee_rate)
            db.session.add(Transaction(order_id=o.id, maker_id=o.maker_id, worker_id=a.worker_id, amount=amt, fee=fee,
                                       method=o.pay_method, category=o.category))
            worker = db.session.get(User, a.worker_id)
            if o.pay_method == "online":  # wallet-to-wallet, platform fee kept from the worker's side
                wallet_move(current_user, "payment", -amt, f"Paid for “{o.title[:50]}”", o.id)
                wallet_move(worker, "earning", amt - fee, f"Earned from “{o.title[:50]}”", o.id)
            elif credit_cash:  # cash settles outside the app, but the worker's earnings are still recorded in their wallet
                wallet_move(worker, "earning", amt - fee, f"Earned from “{o.title[:50]}” (cash job)", o.id)
            notify(a.worker_id, f"“{o.title}” is complete. {'₹{:,}'.format(amt - fee)} was added to your wallet.", url_for("orders.detail", oid=oid))
        flash("Marked as completed" + (" and payment released from your wallet." if o.pay_method == "online" else ".") + " You can now rate each other.", "success")
    elif act == "cancel" and o.status in EDITABLE + ("in_progress",):
        o.status = "cancelled"
        clear_tracking(o.id)
        for a in o.applications:
            if a.status in ("pending", "accepted"):
                notify(a.worker_id, f"“{o.title}” was cancelled", url_for("orders.detail", oid=oid))
        flash("Order cancelled.", "success")
    elif act == "close" and o.status == "completed":
        o.status = "closed"
        flash("Order closed.", "success")
    else:
        flash("That action isn't available for this order.", "error")
    db.session.commit()
    return redirect(url_for("orders.detail", oid=oid))


@bp.route("/<int:oid>/delete", methods=["POST"])
@login_required
def delete(oid):
    o = _owner(oid)
    if o.status in ("completed", "closed", "in_progress") or o.accepted:
        flash("Orders with accepted workers can't be deleted. Cancel it instead.", "error")
        return redirect(url_for("orders.detail", oid=oid))
    SavedOrder.query.filter_by(order_id=oid).delete()
    Report.query.filter_by(order_id=oid).update({"order_id": None})
    _drop_audio(o.audio)
    for p in o.photos:
        _drop_photo_files(p.name)
    clear_tracking(o.id)
    db.session.delete(o)
    db.session.commit()
    flash("Order deleted.", "success")
    return redirect(url_for("profile.my_orders"))


# ---------------- live route: accepted worker -> job, visible to the maker ----------------
LIVE_STATUSES = ("open", "fully_booked", "in_progress")


def _live_access(oid):
    """(order, 'maker'|'worker') for the maker or an accepted worker of an active job; 404 for anyone else."""
    o = _get(oid)
    if o.maker_id == current_user.id:
        return o, "maker"
    acc = OrderApplication.query.filter_by(order_id=oid, worker_id=current_user.id, status="accepted").first()
    if not acc:
        abort(404)
    return o, "worker"


def _dest(o):
    """Where the worker is heading. The maker's exact pin if there is one, else the ~1 km public point (flagged approximate)."""
    if o.exact_lat is not None:
        return dict(lat=o.exact_lat, lng=o.exact_lng, approx=False)
    return dict(lat=o.lat, lng=o.lng, approx=True) if o.lat is not None else None


@bp.route("/<int:oid>/live")
@login_required
def live(oid):
    o, role = _live_access(oid)
    active = o.status in LIVE_STATUSES
    workers = [a.worker for a in o.applications if a.status == "accepted"]
    why = restriction("work") if role == "worker" else None
    return render_template("orders/live.html", o=o, role=role, active=active, workers=workers, dest=_dest(o), blocked_why=why)


@bp.route("/<int:oid>/track", methods=["POST"])
@login_required
@rate_limit("track", 40, 60)
def track(oid):
    """A worker's phone reports where it is (every few seconds while the live page is open and sharing is on)."""
    o, role = _live_access(oid)
    if role != "worker":
        return jsonify(ok=False), 403
    if o.status not in LIVE_STATUSES:
        clear_tracking(oid); db.session.commit()
        return jsonify(ok=False, error="This job is no longer active."), 410
    why = restriction("work")
    if why:
        return jsonify(ok=False, error=why), 403
    d = request.get_json(silent=True) or {}
    try:
        lat, lng = round(float(d["lat"]), 5), round(float(d["lng"]), 5)
        assert -90 <= lat <= 90 and -180 <= lng <= 180
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(ok=False), 400
    row = LiveLocation.query.filter_by(order_id=oid, user_id=current_user.id).first() or LiveLocation(order_id=oid, user_id=current_user.id)
    row.lat, row.lng, row.accuracy, row.updated = lat, lng, to_int(d.get("acc"), 0, 0, 100000), datetime.utcnow()
    db.session.add(row)
    LiveLocation.query.filter(LiveLocation.updated < datetime.utcnow() - timedelta(days=1)).delete()   # housekeeping
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/<int:oid>/track.json")
@login_required
def track_json(oid):
    """What the maker's live map polls: each accepted worker's latest shared position."""
    o, role = _live_access(oid)
    out = dict(active=o.status in LIVE_STATUSES, dest=_dest(o), workers=[])
    if role == "maker" and out["active"]:
        now = datetime.utcnow()
        rows = {r.user_id: r for r in LiveLocation.query.filter_by(order_id=oid)}
        for a in o.applications:
            if a.status != "accepted":
                continue
            r = rows.get(a.worker_id)
            out["workers"].append(dict(id=a.worker_id, name=a.worker.name, lat=r.lat if r else None, lng=r.lng if r else None,
                                       acc=r.accuracy if r else 0, age=int((now - r.updated).total_seconds()) if r else None))
    return jsonify(out)


@bp.route("/<int:oid>/report", methods=["POST"])
@login_required
@rate_limit("report", 10, 300)
def report(oid):
    o = _get(oid)
    reason = request.form.get("reason")
    if reason not in REPORT_REASONS:
        flash("Pick a reason for the report.", "error")
    elif Report.query.filter_by(reporter_id=current_user.id, order_id=oid, status="pending").first():
        flash("You've already reported this order. Our team is reviewing it.", "error")
    else:
        uid = request.form.get("user_id", type=int)
        target = uid if uid and o.maker_id == current_user.id and any(a.worker_id == uid for a in o.applications) else None
        db.session.add(Report(reporter_id=current_user.id, order_id=oid, user_id=target, reason=reason,
                              details=request.form.get("details", "").strip()[:500]))
        db.session.commit()
        flash("Report sent to our moderation team. Thank you.", "success")
    return redirect(request.referrer or url_for("orders.detail", oid=oid))


@bp.route("/<int:oid>/review", methods=["POST"])
@login_required
def review(oid):
    o = _get(oid)
    to = request.form.get("to", type=int)
    rating = request.form.get("rating", type=int)
    accepted = {a.worker_id for a in o.applications if a.status == "accepted"}
    valid = (o.status in ("completed", "closed") and rating in range(1, 6) and
             ((current_user.id == o.maker_id and to in accepted) or (current_user.id in accepted and to == o.maker_id)))
    if not valid:
        flash("You can't leave that review.", "error")
    elif Review.query.filter_by(order_id=oid, reviewer_id=current_user.id, reviewee_id=to).first():
        flash("You've already reviewed this person for this order.", "error")
    else:
        r = Review(order_id=oid, reviewer_id=current_user.id, reviewee_id=to, rating=rating,
                   text=request.form.get("text", "").strip()[:500])
        db.session.add(r)
        other = Review.query.filter_by(order_id=oid, reviewer_id=to, reviewee_id=current_user.id).first()
        if other:  # blind reviews: both become visible together
            r.revealed = other.revealed = True
        notify(to, f"{current_user.name} rated you for “{o.title}”", url_for("profile.view", username=current_user.username))
        db.session.commit()
        flash("Review submitted. It becomes public once both of you have reviewed." if not other else "Review submitted.", "success")
    return redirect(url_for("orders.detail", oid=oid))
