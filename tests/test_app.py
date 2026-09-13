"""Scaffold smoke tests: app boots, DB schema creates, health endpoint answers."""
import pytest

from app import create_app, db


@pytest.fixture()
def app():
    app = create_app("config.TestConfig")
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def test_healthz(app):
    client = app.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_models_create_and_query(app):
    from app.models import College, Settings, User

    with app.app_context():
        college = College(name="SVKM")
        db.session.add(college)
        user = User(email="test@example.com", username="tester", college=college,
                    google_sub="sub-1")
        db.session.add(user)
        db.session.flush()
        db.session.add(Settings(user_id=user.id))
        db.session.commit()

        fetched = db.session.query(User).filter_by(email="test@example.com").one()
        assert fetched.google_sub == "sub-1"
        assert fetched.college.default_subject_limit == 70
        assert db.session.get(Settings, fetched.id).overall_limit == 75
