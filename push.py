"""Web Push (VAPID). Works on Android Chrome/Edge/Firefox and desktop; iOS needs the site added to the Home Screen.
Requires HTTPS (localhost is allowed). Keys are generated once and stored in instance/."""
import base64, json, os, threading
from flask import current_app

try:
    from pywebpush import webpush, WebPushException
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    OK = True
except Exception:  # library missing: push is simply disabled
    OK = False


def _paths(app):
    return os.path.join(app.instance_path, "vapid_private.pem")


def _vapid(app):
    path = _paths(app)
    if not os.path.exists(path):
        os.makedirs(app.instance_path, exist_ok=True)
        v = Vapid()
        v.generate_keys()
        v.save_key(path)
    return Vapid.from_file(path)


def public_key(app):
    """Application server key in the base64url format the browser expects."""
    if not OK:
        return ""
    raw = _vapid(app).public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _send_all(app, user_id, payload):
    from extensions import db
    from models import PushSub
    with app.app_context():
        vapid = _vapid(app)
        for s in PushSub.query.filter_by(user_id=user_id).all():
            try:
                webpush(subscription_info={"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}},
                        data=json.dumps(payload, ensure_ascii=False), vapid_private_key=vapid,
                        vapid_claims={"sub": "mailto:" + app.config.get("ADMIN_EMAIL", "admin@nearhand.local")}, ttl=3600)
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code in (404, 410):  # subscription is gone
                    db.session.delete(s)
                    db.session.commit()
            except Exception:
                pass


def send_async(user_id, payload):
    """Fire-and-forget so a slow push service never delays a page."""
    if not OK:
        return
    app = current_app._get_current_object()
    threading.Thread(target=_send_all, args=(app, user_id, payload), daemon=True).start()
