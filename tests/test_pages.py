"""Page-level checks: every screen renders, and editing a job's voice note works."""
import io, os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.dirname(__file__))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "pages.db")
import test_smoke as t  # noqa: E402  (also boots the app)
from models import Order, User  # noqa: E402


def run():
    app = t.app
    anon = app.test_client()
    for url in ["/", "/login", "/signup", "/forgot", "/page/about", "/page/terms", "/market", "/market?area=all&voice=1&sort=price_desc", "/static/js/audio.js", "/static/js/map.js", "/sw.js"]:
        assert anon.get(url).status_code == 200, url
    maker = t.signup("Page Maker", "pm@x.com", "adult")
    teen = t.signup("Page Teen", "pt@x.com", "minor")
    maker.post("/api/location", json=dict(lat=25.25, lng=87.0, name="Area", radius=30, save=True))
    teen.post("/api/location", json=dict(lat=25.25, lng=87.0, name="Area", radius=30, save=True))
    a = maker.post("/orders/audio", data=dict(audio=(io.BytesIO(t.WEBM), "a.webm"), seconds="30"), content_type="multipart/form-data").get_json()
    oid = t.post_order(maker, audio_name=a["name"], audio_seconds="30")
    assert maker.get("/orders/new").status_code == 200 and b"data-rec" in maker.get("/orders/new").data
    html = maker.get(f"/orders/{oid}/edit").get_data(as_text=True)
    assert "data-rec" in html and a["name"] in html                       # edit form shows the saved voice note
    # replace the voice note on edit
    b = maker.post("/orders/audio", data=dict(audio=(io.BytesIO(t.WEBM + b"1"), "b.webm"), seconds="20"), content_type="multipart/form-data").get_json()
    r = maker.post(f"/orders/{oid}/edit", data=t.order_form(audio_name=b["name"], audio_seconds="20"))
    assert r.status_code == 302
    with app.app_context():
        o = t.db.session.get(Order, oid); assert o.audio == b["name"] and o.audio_seconds == 20
    assert not os.path.exists(os.path.join(app.static_folder, "uploads", "audio", a["name"]))     # old file removed
    # remove it
    maker.post(f"/orders/{oid}/edit", data=t.order_form(remove_audio="1"))
    with app.app_context():
        assert t.db.session.get(Order, oid).audio is None
    # someone else's file name can't be attached
    r = maker.post(f"/orders/{oid}/edit", data=t.order_form(audio_name="deadbeefdeadbeef.webm"))
    assert r.status_code == 200 and b"expired" in r.data
    # pages for a teen, a profile, chat
    assert teen.get(f"/orders/{oid}").status_code == 200
    with app.app_context():
        mname = User.query.filter_by(email="pm@x.com").first().username
    assert teen.get(f"/u/{mname}").status_code == 200
    assert maker.get("/u/%s?tab=edit" % mname).status_code == 200
    assert b"work_radius_km" in maker.get(f"/u/{mname}?tab=edit").data
    assert teen.get("/orders/new").status_code == 302
    for url in ["/wallet", "/wallet?tab=withdraw", "/dashboard?tab=working", "/history?tab=created", "/page/safety"]:
        assert teen.get(url).status_code == 200, url
    check_new_features(app, maker, teen, oid)
    print("PAGES OK")


def photo_bytes(color=(200, 30, 30), size=(2400, 1800)):
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", size, color).save(buf, "JPEG"); return buf.getvalue()


def check_new_features(app, maker, teen, oid):
    from models import OrderPhoto, LiveLocation
    # ---- under-18 safety check: words, category, hours
    chk = lambda **k: maker.post("/orders/youth-check", json=dict(dict(title="Help with maths", description="Two hours of maths revision for a school student.", category="Tutoring", work_from="09:00", work_to="17:00"), **k)).get_json()
    assert chk()["ok"] is True
    assert chk(title="Serve beer at a pub")["ok"] is False
    assert chk(description="Come alone and keep it secret from your parents, send photos")["ok"] is False
    assert chk(category="Moving & Lifting")["ok"] is False
    assert chk(work_from="05:00")["ok"] is False and chk(work_to="21:30")["ok"] is False and chk(work_to="")["ok"] is False
    assert chk(work_from="18:00", work_to="22:00")["ok"] is False
    # ticking the box on a bad job is refused on save, naming what is wrong
    r = maker.post("/orders/new", data=t.order_form(title="Serve beer at a pub", description="Serve drinks at night in a busy pub for tips."))
    assert b"isn&#39;t suitable for under-18s" in r.data or b"isn't suitable for under-18s" in r.data
    r = maker.post("/orders/new", data=t.order_form(work_to="22:00"))
    assert b"Under-18s may only work between" in r.data
    # an adult-only job needs no hours and no check
    r = maker.post("/orders/new", data=t.order_form(category="Moving & Lifting", youth_ok="", work_from="", work_to="", title="Carry boxes", description="Carry twenty boxes into a new flat on the first floor."))
    assert b'name="nonce"' in r.data
    # half-filled hours are refused
    assert b"both a start and an end" in maker.post("/orders/new", data=t.order_form(work_to="", youth_ok="")).data

    # ---- photos: upload, EXIF stripped/resized, limit of 10, attach, view, remove
    up = lambda: maker.post("/orders/photo", data=dict(photo=(io.BytesIO(photo_bytes()), "p.jpg")), content_type="multipart/form-data")
    names = []
    for _ in range(10):
        j = up().get_json(); assert j["ok"], j; names.append(j["name"])
    from PIL import Image
    im = Image.open(os.path.join(app.static_folder, "uploads", "photos", names[0])); assert max(im.size) <= 1600
    assert maker.post("/orders/photo", data=dict(photo=(io.BytesIO(b"not an image"), "x.jpg")), content_type="multipart/form-data").status_code == 400
    r = maker.post(f"/orders/{oid}/edit", data=t.order_form(photo_names=",".join(names)))
    assert r.status_code == 302
    with app.app_context():
        assert OrderPhoto.query.filter_by(order_id=oid).count() == 10
    extra = up().get_json()["name"]
    assert b"up to 10 photos" in maker.post(f"/orders/{oid}/edit", data=t.order_form(photo_names=extra)).data     # 11th is refused
    assert b'data-lightbox' in teen.get(f"/orders/{oid}").data                                                     # others can view them
    mk_html = maker.get("/market?area=all").get_data(as_text=True)
    assert "cardart photo" in mk_html and "pcount" in mk_html                      # Find Work card shows a real photo band
    det = teen.get(f"/orders/{oid}").get_data(as_text=True)
    assert 'id="lightbox"' in det and "<dialog id=\"lightbox\"" not in det and "gph hero" in det   # viewer is an overlay, not a bottom-sheet <dialog>
    with app.app_context():
        pid = OrderPhoto.query.filter_by(order_id=oid).first().id
    maker.post(f"/orders/{oid}/edit", data=t.order_form(remove_photos=str(pid)))
    with app.app_context():
        assert OrderPhoto.query.filter_by(order_id=oid).count() == 9
    assert teen.post("/orders/photo", data=dict(photo=(io.BytesIO(photo_bytes()), "p.jpg")), content_type="multipart/form-data").status_code == 403   # minors can't post

    # ---- live route: only the maker and accepted workers; the worker shares, the maker sees
    w = t.signup("Route Worker", "rw@x.com", "adult"); w.post("/api/location", json=dict(lat=25.25, lng=87.0, name="Area", radius=30))
    other = t.signup("Random Person", "rp@x.com", "adult")
    w.post(f"/orders/{oid}/apply", data=dict(message="I would like to do this job please"))
    assert w.get(f"/orders/{oid}/live").status_code == 404                          # not accepted yet
    with app.app_context():
        aid = [a for a in t.db.session.get(Order, oid).applications if a.worker.email == "rw@x.com"][0].id
    r = maker.post(f"/orders/{oid}/app/{aid}/accept")
    with app.app_context():
        from models import Notification
        assert any("/live" in (n.url or "") for n in Notification.query.all())          # the acceptance notification opens the route
    assert w.get(f"/orders/{oid}/live").status_code == 200 and maker.get(f"/orders/{oid}/live").status_code == 200
    assert other.get(f"/orders/{oid}/live").status_code == 404 and teen.get(f"/orders/{oid}/live").status_code == 404
    assert b"Open my route" in w.get(f"/orders/{oid}").data and b"Track workers live" in maker.get(f"/orders/{oid}").data
    assert maker.post(f"/orders/{oid}/track", json=dict(lat=25.3, lng=87.1)).status_code == 403     # makers can't post positions
    assert w.post(f"/orders/{oid}/track", json=dict(lat="x", lng=1)).status_code == 400
    assert w.post(f"/orders/{oid}/track", json=dict(lat=25.31, lng=87.11, acc=12)).get_json()["ok"]
    j = maker.get(f"/orders/{oid}/track.json").get_json()
    assert j["active"] and j["workers"][0]["lat"] == 25.31 and j["dest"]["approx"] is False and abs(j["dest"]["lat"] - 25.25) < 0.01
    assert w.get(f"/orders/{oid}/track.json").get_json()["workers"] == []                              # a worker never sees other positions
    w.post(f"/orders/{oid}/track", json=dict(lat=25.32, lng=87.12))
    with app.app_context():
        assert LiveLocation.query.filter_by(order_id=oid).count() == 1                                  # latest only, not a trail
    # under-18 hours: outside 6 AM-9 PM a minor can't share a route
    import utils
    real = utils.youth_hours_open
    # ---- the route ends with the job: positions are deleted
    maker.post(f"/orders/{oid}/start"); maker.post(f"/orders/{oid}/complete")
    with app.app_context():
        assert LiveLocation.query.filter_by(order_id=oid).count() == 0
    assert w.post(f"/orders/{oid}/track", json=dict(lat=25.3, lng=87.1)).status_code == 410

    # ---- no email verification, and the welcome pop-up appears once after registering
    import utils; utils._hits.clear()          # the test created many accounts from one address; reset the sign-up rate limiter
    c = app.test_client()
    c.post("/signup", data=dict(name="Welcome New", email="wn@x.com", password="Passw0rd1", terms="1", age="adult"))
    with app.app_context():
        assert User.query.filter_by(email="wn@x.com").first().email_verified is True
    page = c.get("/market").get_data(as_text=True)
    assert 'id="welcome"' in page and 'id="welcome"' not in c.get("/market").get_data(as_text=True)


def test_wav_fallback_recording_is_accepted():
    """The browser's Web Audio fallback uploads a WAV (12 kHz mono). It must be accepted, up to the 90 s size."""
    import struct
    c = t.signup("Wav Maker", "wav@x.com", "adult")
    def wav(seconds, rate=12000):
        n = seconds * rate; data = b"\x01\x02" * n
        return b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data
    ok = c.post("/orders/audio", data=dict(audio=(io.BytesIO(wav(90)), "note.wav"), seconds="90"), content_type="multipart/form-data")
    assert ok.status_code == 200 and ok.get_json()["name"].endswith(".wav"), ok.get_data(as_text=True)[:200]
    assert c.post("/orders/audio", data=dict(audio=(io.BytesIO(wav(130)), "note.wav"), seconds="90"), content_type="multipart/form-data").status_code == 400   # too big = too long


def test_youth_hours_window():
    import datetime as dt
    import utils
    with t.app.test_request_context():
        from unittest import mock
        real_now = utils.local_now
        for hh, mm, expect in [(5, 59, False), (6, 0, True), (12, 0, True), (20, 59, True), (21, 0, False), (23, 30, False)]:
            fake = dt.datetime(2026, 11, 6, hh, mm, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
            with mock.patch.object(utils, "local_now", return_value=fake):
                assert utils.youth_hours_open() is expect, (hh, mm)


def test_minor_cannot_work_at_night():
    import datetime as dt
    from unittest import mock
    import utils
    app = t.app
    import utils; utils._hits.clear()
    teen = t.signup("Night Teen", "nt@x.com", "minor"); maker = t.signup("Night Maker", "nm@x.com", "adult")
    maker.post("/api/location", json=dict(lat=25.25, lng=87.0, name="Area", radius=25, save=True))
    oid = t.post_order(maker, title="Tutor my brother", description="Two hours of maths tutoring for a school student.")
    teen.post("/api/location", json=dict(lat=25.25, lng=87.0, name="Area", radius=25))
    teen.post(f"/orders/{oid}/apply", data=dict(message="I can help with maths tutoring"))
    with app.app_context():
        aid = t.db.session.get(Order, oid).applications[0].id
    maker.post(f"/orders/{oid}/app/{aid}/accept")
    night = dt.datetime(2026, 11, 6, 22, 30, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    day = dt.datetime(2026, 11, 6, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    with mock.patch.object(utils, "local_now", return_value=night):
        page = teen.get(f"/orders/{oid}/live").get_data(as_text=True)
        assert "can&#39;t work more right now" in page or "can't work more right now" in page
        r = teen.post(f"/orders/{oid}/track", json=dict(lat=25.3, lng=87.1))
        assert r.status_code == 403 and "work more" in r.get_json()["error"]
        assert "can&#39;t work more right now" in teen.get("/market").get_data(as_text=True) or "can't work more right now" in teen.get("/market").get_data(as_text=True)
    with mock.patch.object(utils, "local_now", return_value=day):
        assert teen.post(f"/orders/{oid}/track", json=dict(lat=25.3, lng=87.1)).status_code == 200
        assert "work more right now" not in teen.get("/market").get_data(as_text=True)
    # an adult is never blocked by the night rule
    with mock.patch.object(utils, "local_now", return_value=night):
        assert "work more right now" not in maker.get("/market").get_data(as_text=True)


if __name__ == "__main__":
    run()


def test_pages():
    run()
