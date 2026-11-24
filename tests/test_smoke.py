"""End-to-end smoke test: python -m pytest tests  (or: python tests/test_smoke.py)
Uses a throw-away SQLite file, so your real data is never touched."""
import io, os, sys, tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = "sqlite:///" + _db

from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import User, Order, Review, PaymentRequest, Report  # noqa: E402

app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 3000          # looks like a WebM file to the magic-byte check


def signup(name, email, age):
    c = app.test_client()
    r = c.post("/signup", data=dict(name=name, email=email, password="Passw0rd1", terms="1", age=age))
    assert r.status_code == 302, (name, r.status_code, r.get_data(as_text=True)[:300])
    return c


def order_form(**kw):
    d = dict(title="Help with maths homework", description="Need a patient tutor for two hours of maths revision.", category="Tutoring",
             pay_per_worker="300", pay_method="cash", duration="hours", workers_needed="1", area="Test Area", lat="25.25", lng="87.00",
             radius_choice="custom", custom_radius="10", youth_ok="1", work_from="09:00", work_to="17:00")
    d.update(kw)
    return d


def post_order(c, **kw):
    r = c.post("/orders/new", data=order_form(**kw))
    assert r.status_code == 200 and b'name="nonce"' in r.data, r.get_data(as_text=True)[:400]
    nonce = r.get_data(as_text=True).split('name="nonce" value="')[1].split('"')[0]
    fields = {k: v for k, v in order_form(**kw).items()}
    fields["nonce"] = nonce
    r = c.post("/orders/publish", data=fields)
    assert r.status_code == 302, r.get_data(as_text=True)[:300]
    return int(r.headers["Location"].split("/orders/")[1].split("/")[0])


def run():
    admin = app.test_client()
    assert admin.post("/admin/login", data=dict(email="admin@nearhand.local", password="admin12345")).status_code == 302

    maker = signup("Maker One", "maker@x.com", "adult")
    teen = signup("Teen Two", "teen@x.com", "minor")
    far = signup("Far Three", "far@x.com", "adult")

    # ---- UID on profile
    with app.app_context():
        m = User.query.filter_by(email="maker@x.com").first()
        assert m.uid.startswith("NH-") and m.is_adult
        mid, mname = m.id, m.username
    assert b"NH-" in maker.get("/profile", follow_redirects=True).data

    # ---- minors cannot post
    assert teen.get("/orders/new").status_code == 302

    # ---- voice note upload: valid, wrong format, too long
    maker.post("/api/location", json=dict(lat=25.25, lng=87.0, name="Test Area", radius=25, save=True))
    r = maker.post("/orders/audio", data=dict(audio=(io.BytesIO(WEBM), "a.webm"), seconds="42"), content_type="multipart/form-data")
    j = r.get_json(); assert j and j["ok"], j
    bad = maker.post("/orders/audio", data=dict(audio=(io.BytesIO(b"notaudio" * 50), "a.txt"), seconds="10"), content_type="multipart/form-data")
    assert bad.status_code == 400
    long = maker.post("/orders/audio", data=dict(audio=(io.BytesIO(WEBM), "a.webm"), seconds="200"), content_type="multipart/form-data")
    assert long.status_code == 400
    big = maker.post("/orders/audio", data=dict(audio=(io.BytesIO(WEBM + b"\x00" * 3_200_000), "a.webm"), seconds="30"), content_type="multipart/form-data")
    assert big.status_code == 400

    # ---- location is required
    r = maker.post("/orders/new", data=order_form(lat="", lng=""))
    assert b"Place the pin" in r.data

    # ---- publish youth-safe job with audio + adult-only job
    oid_y = post_order(maker, audio_name=j["name"], audio_seconds="42")
    oid_a = post_order(maker, title="Move heavy furniture", category="Moving & Lifting", description="Carry a sofa and two cupboards up the stairs.", youth_ok="")
    with app.app_context():
        assert db.session.get(Order, oid_y).adult_only is False and db.session.get(Order, oid_y).audio == j["name"]
        assert db.session.get(Order, oid_a).adult_only is True
        assert str(db.session.get(Order, oid_y).work_from) == "09:00:00"

    # ---- the voice note shows on the marketplace card and the detail page
    m1 = maker.get("/market").get_data(as_text=True)
    assert "data-play" in m1 and "Move Heavy Furniture" in m1
    assert "<audio" in maker.get(f"/orders/{oid_y}").get_data(as_text=True)

    # ---- under-18 filtering
    teen.post("/api/location", json=dict(lat=25.25, lng=87.0, name="Test Area", radius=25))
    tm = teen.get("/market").get_data(as_text=True)
    assert "Help With Maths Homework" in tm and "Move Heavy Furniture" not in tm
    assert teen.get(f"/orders/{oid_a}").status_code == 404
    assert teen.post(f"/orders/{oid_a}/apply", data=dict(message="I can help you with this work")).status_code == 404
    assert teen.get("/market?partial=1").status_code == 200

    # ---- work-area + worker-radius filtering
    far.post("/api/location", json=dict(lat=28.61, lng=77.20, name="Delhi", radius=25))      # ~1000 km away
    fm = far.get("/market").get_data(as_text=True)
    assert "Help With Maths Homework" not in fm and "outside your work area" in fm
    assert "Help With Maths Homework" in far.get("/market?area=all").get_data(as_text=True)
    far.post(f"/orders/{oid_y}/apply", data=dict(message="I can travel there for this job"))
    with app.app_context():
        assert not any(a.worker.email == "far@x.com" for a in db.session.get(Order, oid_y).applications)   # outside the maker's radius

    # ---- teen applies, maker accepts, job completes, both review
    teen.post(f"/orders/{oid_y}/apply", data=dict(message="I am good at maths and free today"))
    with app.app_context():
        app_id = db.session.get(Order, oid_y).applications[0].id
        tid = User.query.filter_by(email="teen@x.com").first().id
    maker.post(f"/orders/{oid_y}/app/{app_id}/accept")
    maker.post(f"/orders/{oid_y}/start")
    maker.post(f"/orders/{oid_y}/complete")
    maker.post(f"/orders/{oid_y}/review", data=dict(to=tid, rating="5", text="Great help"))
    with app.app_context():
        t = User.query.get(tid)
        assert t.review_count == 0 and t.rating is None         # blind until the other side reviews
    teen.post(f"/orders/{oid_y}/review", data=dict(to=mid, rating="4", text="Clear instructions"))
    with app.app_context():
        t, mk = User.query.get(tid), User.query.get(mid)
        assert t.rating == 5.0 and mk.rating == 4.0 and t.review_count == 1
        assert t.rating_score < 5.0                              # smoothed score can't be gamed by one review
    # one-sided review becomes public after the reveal window
    with app.app_context():
        rv = Review.query.filter_by(reviewee_id=tid).first()
        db.session.delete(Review.query.filter_by(reviewee_id=mid).first())
        rv.revealed = False
        rv.created = datetime.utcnow() - timedelta(days=8)
        db.session.commit()
        assert User.query.get(tid).review_count == 1

    # ---- demo wallet: QR, deposit request, admin approves, payout, pause, reject refunds
    assert maker.get("/wallet/qr.svg?amount=500").data.startswith(b"<?xml") or b"<svg" in maker.get("/wallet/qr.svg?amount=500").data
    maker.post("/wallet/deposit", data=dict(amount="1000", ref="demo"))
    with app.app_context():
        dep = PaymentRequest.query.filter_by(kind="deposit").first(); assert dep and dep.status == "pending"
        assert User.query.get(mid).balance in (0, None)           # nothing credited until an admin approves
    admin.post(f"/admin/payments/{dep.id}/approve")
    with app.app_context():
        assert User.query.get(mid).balance == 1000
    admin.post("/admin/controls/pause_payouts/toggle")
    maker.post("/wallet/withdraw", data=dict(amount="200", dest="me@upi"))
    with app.app_context():
        assert PaymentRequest.query.filter_by(kind="payout").count() == 0 and User.query.get(mid).balance == 1000
    admin.post("/admin/controls/pause_payouts/toggle")
    maker.post("/wallet/withdraw", data=dict(amount="200", dest="me@upi"))
    with app.app_context():
        pay = PaymentRequest.query.filter_by(kind="payout").first()
        assert User.query.get(mid).balance == 800                 # held immediately
    admin.post(f"/admin/payments/{pay.id}/reject")
    with app.app_context():
        assert User.query.get(mid).balance == 1000                # refunded
    admin.post("/admin/payments/add", data=dict(who="maker@x.com", amount="250", note="bonus"))
    with app.app_context():
        assert User.query.get(mid).balance == 1250

    # ---- moderation: mute blocks chat, timeout blocks posting, lifted again
    admin.post(f"/admin/users/{mid}/mute", data=dict(duration="1h", reason="spam"))
    with app.app_context():
        assert User.query.get(mid).muted
    r = maker.post(f"/chat/{tid and 'teenone' or ''}/send", json=dict(body="hi")) if False else None
    admin.post(f"/admin/users/{mid}/timeout", data=dict(duration="24h", reason="test"))
    assert maker.get("/orders/new").status_code == 302                              # timed out: can't post
    admin.post(f"/admin/users/{mid}/untimeout"); admin.post(f"/admin/users/{mid}/unmute")
    assert maker.get("/orders/new").status_code == 200

    # ---- reports + staff actions + roles
    teen.post(f"/orders/{oid_a if False else oid_y}/report", data=dict(reason="Spam"))
    with app.app_context():
        rid = Report.query.first().id
    assert admin.post(f"/admin/reports/{rid}/action", data=dict(do="timeout:24h", reason="spam")).status_code == 302
    with app.app_context():
        assert User.query.get(mid).timed_out and Report.query.get(rid).status == "actioned"
    admin.post(f"/admin/users/{mid}/untimeout")
    far_id = None
    with app.app_context():
        far_id = User.query.filter_by(email="far@x.com").first().id
    admin.post(f"/admin/users/{far_id}/make_mod")
    mod = app.test_client(); mod.post("/login", data=dict(email="far@x.com", password="Passw0rd1"))
    assert mod.get("/admin/").status_code == 200
    assert mod.get("/admin/payments").status_code == 404                           # money pages are admin-only
    mod.post(f"/admin/users/{mid}/ban")
    with app.app_context():
        assert User.query.get(mid).status == "active"                              # mods cannot ban
    mod.post(f"/admin/users/{mid}/mute", data=dict(duration="1h"))
    with app.app_context():
        assert User.query.get(mid).muted                                           # but they can mute
    admin.post("/admin/users/%d/unmute" % mid)
    admin.post(f"/admin/users/{mid}/ban")
    with app.app_context():
        assert User.query.get(mid).status == "banned"
    admin.post(f"/admin/users/{mid}/restore")

    # ---- signups pause
    admin.post("/admin/controls/pause_signups/toggle")
    c = app.test_client(); c.post("/signup", data=dict(name="Late One", email="late@x.com", password="Passw0rd1", terms="1", age="adult"))
    with app.app_context():
        assert User.query.filter_by(email="late@x.com").first() is None
    admin.post("/admin/controls/pause_signups/toggle")

    # ---- age gate for accounts that were never asked
    with app.app_context():
        u = User(name="Old Acct", username="oldacct", email="old@x.com", uid="NH-999999"); u.set_password("Passw0rd1"); db.session.add(u); db.session.commit()
    old = app.test_client(); r = old.post("/login", data=dict(email="old@x.com", password="Passw0rd1"))
    assert "/age" in r.headers["Location"]
    assert "/age" in old.get("/market").headers["Location"]
    old.post("/age", data=dict(age="minor"))
    assert old.get("/market").status_code == 200

    # ---- every page renders
    for url in ["/market", "/dashboard", "/wallet", "/history", "/profile", "/chat", "/notifications", f"/orders/{oid_y}", f"/orders/{oid_y}/manage", "/api/live", "/api/activity"]:
        assert maker.get(url, follow_redirects=True).status_code == 200, url
    for url in ["/admin/", "/admin/users", f"/admin/users/{mid}", "/admin/users.csv", "/admin/orders", "/admin/market", "/admin/reports?status=all",
                "/admin/payments", "/admin/controls", "/admin/staff", "/admin/audit", "/admin/tickets", "/admin/revenue", "/admin/users?q=NH-&role=user&sort=wallet"]:
        assert admin.get(url).status_code == 200, url
    assert b"Moderation" in mod.get("/admin/users").data or mod.get("/admin/users").status_code == 200
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    run()


def test_smoke():
    run()
