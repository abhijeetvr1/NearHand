import re, secrets
from flask import session, Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user, login_required, current_user
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import func
from extensions import db
from models import User
from utils import rate_limit, new_uid, is_paused

bp = Blueprint("auth", __name__)


def _ser():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"])


def _safe_next(n):
    return n if n and n.startswith("/") and not n.startswith("//") else None


def _dev_link(label, endpoint, payload, salt):
    """No mail server yet: the link is logged and shown in flash. Replace with email sending later."""
    link = url_for(endpoint, token=_ser().dumps(payload, salt=salt), _external=True)
    current_app.logger.info("%s: %s", label, link)
    if current_app.debug:
        flash(f"Dev mode — {label}: {link}", "info")


@bp.route("/login", methods=["GET", "POST"])
@rate_limit("login", 10, 60)
def login():
    if current_user.is_authenticated:
        return redirect(url_for("market.index"))
    nxt = _safe_next(request.values.get("next"))
    error = None
    if request.method == "POST":
        ident = request.form.get("email", "").strip().lower()
        user = User.query.filter(func.lower(User.email) == ident).first() or User.query.filter_by(username=ident).first()
        if user and user.check_password(request.form.get("password", "")):
            if user.status != "active":
                error = ("This account is banned. Contact support if you think this is a mistake." if user.status == "banned"
                         else "This account is suspended. Contact support if you think this is a mistake.")
            else:
                login_user(user, remember=bool(request.form.get("remember")))
                if not user.age_group:  # older accounts are asked once, right after logging in
                    return redirect(url_for("auth.age", next=nxt or url_for("market.index")))
                return redirect(nxt or url_for("market.index"))
        else:
            error = "Email or password is incorrect."
    return render_template("auth/login.html", next=nxt, error=error, ident=request.form.get("email", ""))


def _make_username(name, email):
    base = re.sub(r"[^a-z0-9]", "", name.lower())[:14] or re.sub(r"[^a-z0-9]", "", email.split("@")[0].lower())[:14] or "user"
    if len(base) < 3:
        base += "user"
    cand = base
    while User.query.filter_by(username=cand).first():
        cand = f"{base}{secrets.randbelow(9000) + 1000}"
    return cand


@bp.route("/signup", methods=["GET", "POST"])
@rate_limit("signup", 6, 60)
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("market.index"))
    f, e = request.form, {}
    closed = is_paused("pause_signups")
    if request.method == "POST" and closed:
        flash("New accounts are paused for a short while. Please try again later.", "error")
        return render_template("auth/signup.html", errors=e, form=f, closed=closed)
    if request.method == "POST":
        name, email, pw = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("password", "")
        if not 2 <= len(name) <= 80: e["name"] = "Enter your name (2–80 characters)."
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 120: e["email"] = "Enter a valid email address."
        elif User.query.filter(func.lower(User.email) == email).first(): e["email"] = "An account with this email already exists."
        if len(pw) < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw): e["password"] = "Use 8+ characters with a letter and a number."
        if not f.get("terms"): e["terms"] = "Please accept the Terms and Privacy Policy."
        if f.get("age") not in ("adult", "minor"): e["age"] = "Tell us if you are 18 or older."
        if not e:
            u = User(name=name, username=_make_username(name, email), email=email, age_group=f["age"], uid=new_uid(),
                     email_verified=not current_app.config["EMAIL_VERIFICATION"])   # no mail server yet: nobody has to verify (flip EMAIL_VERIFICATION=1 later)
            u.set_password(pw)
            db.session.add(u)
            db.session.commit()
            login_user(u)
            if current_app.config["EMAIL_VERIFICATION"]:
                _dev_link("verify your email", "auth.verify", u.id, "verify")
            session["welcome"] = True      # shows the "allow location" pop-up on the next page
            flash("Welcome to NearHand! Add a photo and your work area so people can find you." if u.is_adult else
                  "Welcome to NearHand! You'll see work that is suitable for under-18s. Add your work area to find jobs near you.", "success")
            return redirect(url_for("profile.edit"))
    return render_template("auth/signup.html", errors=e, form=f, closed=closed)


@bp.route("/age", methods=["GET", "POST"])
@login_required
def age():
    """One-time question for accounts created before the age rule: are you 18 or older?"""
    nxt = _safe_next(request.values.get("next")) or url_for("market.index")
    if current_user.age_group:
        return redirect(nxt)
    if request.method == "POST":
        choice = request.form.get("age")
        if choice in ("adult", "minor"):
            current_user.age_group = choice
            db.session.commit()
            flash("Thanks. You're all set." if choice == "adult" else "Thanks. You'll only see work that is suitable for under-18s.", "success")
            return redirect(nxt)
        flash("Please choose one option.", "error")
    return render_template("auth/age.html", next=nxt)


@bp.route("/verify/<token>")
def verify(token):
    try:
        uid = _ser().loads(token, salt="verify", max_age=86400 * 3)
        u = db.session.get(User, uid)
        u.email_verified = True
        db.session.commit()
        flash("Email verified.", "success")
    except (BadSignature, SignatureExpired, AttributeError):
        flash("This verification link is invalid or has expired.", "error")
    return redirect(url_for("market.index"))


@bp.route("/forgot", methods=["GET", "POST"])
@rate_limit("forgot", 5, 300)
def forgot():
    if request.method == "POST":
        u = User.query.filter(func.lower(User.email) == request.form.get("email", "").strip().lower()).first()
        if u and not u.is_admin:
            _dev_link("reset your password", "auth.reset", u.id, "reset")
        flash("If that email has an account, a reset link is on its way.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
@rate_limit("reset", 8, 300)
def reset(token):
    try:
        uid = _ser().loads(token, salt="reset", max_age=3600)
    except (BadSignature, SignatureExpired):
        flash("This reset link is invalid or has expired. Request a new one.", "error")
        return redirect(url_for("auth.forgot"))
    if request.method == "POST":
        pw = request.form.get("password", "")
        if len(pw) < 8 or not any(c.isdigit() for c in pw) or not any(c.isalpha() for c in pw):
            flash("Use 8+ characters with a letter and a number.", "error")
        else:
            u = db.session.get(User, uid)
            u.set_password(pw)
            db.session.commit()
            flash("Password updated. You can log in now.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You're logged out.", "success")
    return redirect(url_for("market.landing"))
