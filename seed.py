from extensions import db
from models import User
from utils import new_uid


def seed(app):
    """Only creates the admin account. No demo data."""
    if not User.query.filter_by(is_admin=True).first():
        cfg = app.config
        a = User(name="Admin", username="admin", email=cfg["ADMIN_EMAIL"], is_admin=True, email_verified=True, verified=True,
                 age_group="adult", uid=new_uid())
        a.set_password(cfg["ADMIN_PASSWORD"])
        db.session.add(a)
        db.session.commit()
