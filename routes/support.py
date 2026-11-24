from flask import Blueprint, request, jsonify, render_template, abort
from flask_login import current_user
from extensions import db
from models import SupportTicket
from utils import rate_limit
import re

bp = Blueprint("support", __name__)

PAGES = {"about": "About NearHand", "terms": "Terms of Service", "privacy": "Privacy Policy",
         "safety": "Trust & Safety", "contact": "Contact"}


@bp.route("/page/<name>")
def page(name):
    if name not in PAGES:
        abort(404)
    return render_template("pages/info.html", name=name, title=PAGES[name])


@bp.route("/support/ticket", methods=["POST"])
@rate_limit("ticket", 5, 300)
def ticket():
    d = request.get_json(silent=True) or request.form
    email = (d.get("email") or (current_user.email if current_user.is_authenticated else "")).strip()
    subject, body = (d.get("subject") or "").strip(), (d.get("body") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(ok=False, error="Enter a valid email so we can reply."), 400
    if not 3 <= len(subject) <= 120 or not 10 <= len(body) <= 1500:
        return jsonify(ok=False, error="Add a subject and describe the problem (10+ characters)."), 400
    db.session.add(SupportTicket(user_id=current_user.id if current_user.is_authenticated else None,
                                 email=email, subject=subject, body=body))
    db.session.commit()
    return jsonify(ok=True)
