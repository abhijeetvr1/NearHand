from datetime import datetime, timedelta
from flask import current_app
from flask_login import UserMixin
from sqlalchemy import func, or_
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db


def now():
    return datetime.utcnow()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.String(12), unique=True, index=True)  # public member ID shown on the profile, e.g. NH-482913
    name = db.Column(db.String(80), nullable=False)
    username = db.Column(db.String(30), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    bio = db.Column(db.String(300), default="")
    work_description = db.Column(db.String(600), default="")
    skills = db.Column(db.String(200), default="")          # comma separated
    experience = db.Column(db.String(60), default="")       # e.g. "2+ years"
    area = db.Column(db.String(80), default="")             # approximate area name only
    lat = db.Column(db.Float)                               # ~1 km precision, never exact
    lng = db.Column(db.Float)
    work_radius_km = db.Column(db.Integer, default=25)      # how far this person is willing to travel for work
    photo = db.Column(db.String(60))                        # file name in static/uploads/avatars
    balance = db.Column(db.Integer, default=0)              # demo wallet, whole rupees
    headline = db.Column(db.String(80), default="")
    languages = db.Column(db.String(120), default="")
    availability = db.Column(db.String(30), default="")
    phone = db.Column(db.String(20), default="")            # private, never shown publicly
    age_group = db.Column(db.String(6))                     # None (not asked yet) | adult | minor
    is_admin = db.Column(db.Boolean, default=False)
    is_mod = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(12), default="active")     # active | suspended | banned
    mute_until = db.Column(db.DateTime)                     # can't chat until then
    timeout_until = db.Column(db.DateTime)                  # read-only: can't post, apply, chat or request money until then
    sanction_note = db.Column(db.String(200), default="")   # last reason, shown to the user
    admin_note = db.Column(db.String(500), default="")      # private staff note
    verified = db.Column(db.Boolean, default=False)         # admin-granted badge
    email_verified = db.Column(db.Boolean, default=False)
    violations = db.Column(db.Integer, default=0)
    created = db.Column(db.DateTime, default=now)
    last_seen = db.Column(db.DateTime, default=now)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_active(self):
        return self.status == "active"

    # ---- roles and age group
    @property
    def is_staff(self):
        return bool(self.is_admin or self.is_mod)

    @property
    def role(self):
        return "admin" if self.is_admin else ("mod" if self.is_mod else "user")

    @property
    def is_minor(self):
        return self.age_group == "minor"

    @property
    def is_adult(self):
        return self.age_group == "adult"

    @property
    def muted(self):
        return bool(self.mute_until and self.mute_until > now())

    @property
    def timed_out(self):
        return bool(self.timeout_until and self.timeout_until > now())

    @property
    def skill_list(self):
        return [s.strip() for s in (self.skills or "").split(",") if s.strip()]

    # ---- reputation
    # A review counts once both sides have reviewed, or REVIEW_REVEAL_DAYS after it was written (whichever is first).
    # That keeps reviews blind (nobody can retaliate) without letting one side hide a rating forever.
    def _rinfo(self):
        c = getattr(self, "_ri", None)
        if c is None:
            n, s = db.session.query(func.count(Review.id), func.coalesce(func.sum(Review.rating), 0)).filter(
                Review.reviewee_id == self.id, Review.visible_clause()).one()
            c = self._ri = (int(n), int(s))
        return c

    @property
    def review_count(self):
        return self._rinfo()[0]

    @property
    def rating(self):
        """Plain average of public reviews (1 decimal). None until the first public review."""
        n, s = self._rinfo()
        return round(s / n, 1) if n else None

    @property
    def rating_score(self):
        """Smoothed score used for levels: a single 5-star review can't instantly make someone 'Top Worker'."""
        n, s = self._rinfo()
        prior_n, prior_mean = 3, 4.0
        return round((prior_n * prior_mean + s) / (prior_n + n), 2)

    def rating_distribution(self):
        rows = dict(db.session.query(Review.rating, func.count(Review.id)).filter(
            Review.reviewee_id == self.id, Review.visible_clause()).group_by(Review.rating).all())
        return {i: int(rows.get(i, 0)) for i in range(5, 0, -1)}

    @property
    def completed_count(self):
        return (OrderApplication.query.join(Order).filter(
            OrderApplication.worker_id == self.id, OrderApplication.status == "accepted",
            Order.status.in_(["completed", "closed"])).count())

    @property
    def created_count(self):
        return Order.query.filter(Order.maker_id == self.id, Order.status != "draft").count()

    @property
    def completion_rate(self):
        done = self.completed_count
        failed = (OrderApplication.query.join(Order).filter(
            OrderApplication.worker_id == self.id, OrderApplication.status.in_(["accepted", "removed"]),
            Order.status.in_(["cancelled", "disputed"])).count()
            + OrderApplication.query.filter_by(worker_id=self.id, status="removed").count())
        return round(100 * done / (done + failed)) if done + failed else None

    @property
    def level(self):
        n = self.completed_count
        r = self.rating_score if self.review_count else 0
        rate = self.completion_rate or 100
        age_days = (now() - self.created).days
        lvl = "New"
        for name, need_n, need_r, need_rate, need_age in [
            ("Beginner", 1, 0, 0, 0), ("Intermediate", 3, 3.5, 0, 14), ("Experienced", 10, 4.0, 80, 60),
            ("Professional", 25, 4.3, 85, 120), ("Top Worker", 50, 4.6, 90, 240)]:
            if n >= need_n and r >= need_r and rate >= need_rate and age_days >= need_age:
                lvl = name
        if self.violations >= 3 and lvl not in ("New", "Beginner"):
            lvl = "Beginner"
        return lvl


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    maker_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(90), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(40), nullable=False, index=True)
    skills = db.Column(db.String(200), default="")
    workers_needed = db.Column(db.Integer, default=1)
    duration = db.Column(db.String(10), default="1d")
    start_date = db.Column(db.Date)
    deadline = db.Column(db.Date)
    pay_per_worker = db.Column(db.Integer, nullable=False)
    bonus = db.Column(db.Integer, default=0)
    pay_method = db.Column(db.String(10), default="cash")
    conditions = db.Column(db.String(400), default="")
    area = db.Column(db.String(80), default="")
    lat = db.Column(db.Float)       # rounded to ~1 km; never exact
    lng = db.Column(db.Float)
    radius_km = db.Column(db.Integer, default=10)          # only workers within this distance of the job can apply
    exact_address = db.Column(db.String(200), default="")  # only shown to maker + accepted workers
    exact_lat = db.Column(db.Float)                        # private meeting point (5 decimals). Only the maker and accepted workers ever see it
    exact_lng = db.Column(db.Float)
    work_from = db.Column(db.Time)                         # daily work hours; required when the job is open to under-18s
    work_to = db.Column(db.Time)
    audio = db.Column(db.String(60))                       # optional voice note file in static/uploads/audio
    audio_seconds = db.Column(db.Integer, default=0)
    adult_only = db.Column(db.Boolean, default=True)       # True: hidden from under-18 accounts
    status = db.Column(db.String(12), default="draft", index=True)
    reviewed_by_admin = db.Column(db.Boolean, default=False)
    featured = db.Column(db.Boolean, default=False)
    created = db.Column(db.DateTime, default=now, index=True)

    maker = db.relationship("User", backref="orders")
    applications = db.relationship("OrderApplication", backref="order", cascade="all, delete-orphan")
    photos = db.relationship("OrderPhoto", backref="order", cascade="all, delete-orphan", order_by="OrderPhoto.position, OrderPhoto.id", lazy="selectin")

    @property
    def accepted(self):
        return sum(1 for a in self.applications if a.status == "accepted")

    @property
    def slots_left(self):
        return max(self.workers_needed - self.accepted, 0)

    @property
    def total_budget(self):
        return self.pay_per_worker * self.workers_needed + (self.bonus or 0)

    @property
    def applicant_count(self):
        return sum(1 for a in self.applications if a.status in ("pending", "accepted"))

    def sync_status(self):
        if self.status in ("open", "fully_booked"):
            self.status = "fully_booked" if self.slots_left == 0 else "open"


class OrderPhoto(db.Model):
    """Up to MAX_PHOTOS pictures on a job. `name` is the full-size JPEG, `t_<name>` the small one used on cards."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)
    position = db.Column(db.Integer, default=0)
    created = db.Column(db.DateTime, default=now)


class LiveLocation(db.Model):
    """Latest position a worker is sharing for ONE accepted job. Replaced on every update, deleted when the job ends."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    lat = db.Column(db.Float, nullable=False)
    lng = db.Column(db.Float, nullable=False)
    accuracy = db.Column(db.Integer, default=0)
    updated = db.Column(db.DateTime, default=now)
    user = db.relationship("User")
    __table_args__ = (db.UniqueConstraint("order_id", "user_id"),)


class OrderApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    worker_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    message = db.Column(db.String(500), nullable=False)
    experience = db.Column(db.String(300), default="")
    portfolio = db.Column(db.String(200), default="")
    availability = db.Column(db.String(120), default="")
    status = db.Column(db.String(10), default="pending")  # pending|accepted|rejected|removed|withdrawn
    created = db.Column(db.DateTime, default=now)
    worker = db.relationship("User")
    __table_args__ = (db.UniqueConstraint("order_id", "worker_id"),)


class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    reviewee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    rating = db.Column(db.Integer, nullable=False)
    text = db.Column(db.String(500), default="")
    revealed = db.Column(db.Boolean, default=False)  # set once both sides have reviewed
    created = db.Column(db.DateTime, default=now)
    order = db.relationship("Order")
    reviewer = db.relationship("User", foreign_keys=[reviewer_id])
    __table_args__ = (db.UniqueConstraint("order_id", "reviewer_id", "reviewee_id"),)

    @staticmethod
    def visible_clause():
        days = current_app.config.get("REVIEW_REVEAL_DAYS", 7)
        return or_(Review.revealed == True, Review.created <= now() - timedelta(days=days))  # noqa: E712

    @property
    def is_public(self):
        return bool(self.revealed or self.created <= now() - timedelta(days=current_app.config.get("REVIEW_REVEAL_DAYS", 7)))


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    text = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(200))
    read = db.Column(db.Boolean, default=False)
    created = db.Column(db.DateTime, default=now)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    body = db.Column(db.String(1000), nullable=False)
    read = db.Column(db.Boolean, default=False)
    created = db.Column(db.DateTime, default=now)


class Block(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    blocker_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    blocked_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    reason = db.Column(db.String(40), nullable=False)
    details = db.Column(db.String(500), default="")
    status = db.Column(db.String(12), default="pending")  # pending|investigating|dismissed|actioned
    resolution = db.Column(db.String(200), default="")    # what staff did, e.g. "Timeout 24h"
    handler_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    handled_at = db.Column(db.DateTime)
    created = db.Column(db.DateTime, default=now)
    reporter = db.relationship("User", foreign_keys=[reporter_id])
    reported_user = db.relationship("User", foreign_keys=[user_id])
    handler = db.relationship("User", foreign_keys=[handler_id])
    order = db.relationship("Order")

    @property
    def target(self):
        """The person a report is really about: the named user, or the maker of the reported order."""
        return self.reported_user or (self.order.maker if self.order else None)


class SupportTicket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    email = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(120), nullable=False)
    body = db.Column(db.String(1500), nullable=False)
    status = db.Column(db.String(10), default="open")
    created = db.Column(db.DateTime, default=now)


class SavedOrder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "order_id"),)


class Transaction(db.Model):
    """Ledger row per paid worker. A real gateway can later attach to `gateway_ref`."""
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False)
    maker_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    worker_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    fee = db.Column(db.Integer, default=0)
    method = db.Column(db.String(10))
    category = db.Column(db.String(40))
    status = db.Column(db.String(12), default="recorded")
    gateway_ref = db.Column(db.String(80))
    created = db.Column(db.DateTime, default=now, index=True)
    order = db.relationship("Order")


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(60), nullable=False)
    target = db.Column(db.String(80))
    detail = db.Column(db.String(200), default="")
    created = db.Column(db.DateTime, default=now)
    admin = db.relationship("User")


class WalletTx(db.Model):
    """Demo wallet ledger. kind: deposit | withdraw | payment | earning | adjust | refund."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)
    amount = db.Column(db.Integer, nullable=False)         # signed: + credit, - debit
    balance_after = db.Column(db.Integer, default=0)
    note = db.Column(db.String(120), default="")
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"))
    created = db.Column(db.DateTime, default=now, index=True)


class PaymentRequest(db.Model):
    """Demo money flow. A user pays the demo QR and asks for a top-up (deposit), or asks for a payout.
    Staff approve or reject it by hand. No real money moves anywhere."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    kind = db.Column(db.String(8), nullable=False)          # deposit | payout
    amount = db.Column(db.Integer, nullable=False)
    dest = db.Column(db.String(60), default="")             # payout: UPI id or bank account
    ref = db.Column(db.String(40), default="")              # deposit: the reference the user typed after "paying"
    status = db.Column(db.String(10), default="pending", index=True)  # pending | approved | rejected
    admin_note = db.Column(db.String(200), default="")
    handler_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created = db.Column(db.DateTime, default=now, index=True)
    handled_at = db.Column(db.DateTime)
    user = db.relationship("User", foreign_keys=[user_id])
    handler = db.relationship("User", foreign_keys=[handler_id])


class Setting(db.Model):
    """Platform switches: pause_signups, pause_market, pause_payouts, pause_deposits, pause_chat, notice."""
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.String(300), default="")


class PushSub(db.Model):
    """Browser/phone push subscription (Web Push). One user can have several devices."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    endpoint = db.Column(db.String(600), unique=True, nullable=False)
    p256dh = db.Column(db.String(200), nullable=False)
    auth = db.Column(db.String(100), nullable=False)
    created = db.Column(db.DateTime, default=now)
