import os
from flask import Blueprint, request, jsonify, current_app, send_from_directory
from flask_login import login_required, current_user
from extensions import db
from models import PushSub
import push

bp = Blueprint("push", __name__)


@bp.route("/sw.js")
def service_worker():
    """Served from the site root so the worker can control every page."""
    r = send_from_directory(current_app.static_folder, "sw.js", mimetype="application/javascript")
    r.headers["Service-Worker-Allowed"] = "/"
    r.headers["Cache-Control"] = "no-cache"
    return r


@bp.route("/push/key")
def key():
    return jsonify(key=push.public_key(current_app) if push.OK else "")


@bp.route("/push/subscribe", methods=["POST"])
@login_required
def subscribe():
    d = request.get_json(silent=True) or {}
    k = d.get("keys") or {}
    ep, p256dh, auth = d.get("endpoint", ""), k.get("p256dh", ""), k.get("auth", "")
    if not (ep.startswith("https://") and p256dh and auth) or len(ep) > 600:
        return jsonify(ok=False), 400
    s = PushSub.query.filter_by(endpoint=ep).first() or PushSub(endpoint=ep, p256dh=p256dh, auth=auth, user_id=current_user.id)
    s.user_id, s.p256dh, s.auth = current_user.id, p256dh, auth
    db.session.add(s)
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/push/unsubscribe", methods=["POST"])
@login_required
def unsubscribe():
    ep = (request.get_json(silent=True) or {}).get("endpoint", "")
    PushSub.query.filter_by(endpoint=ep, user_id=current_user.id).delete()
    db.session.commit()
    return jsonify(ok=True)
