"""Letit security checks. Uses temporary SQLite only; never connects to Supabase."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Mission, User
from app.security import create_token, hash_password



@pytest.fixture()
def security_env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def temporary_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = temporary_db
    try:
        with session_factory() as db:
            user_a = User(id=uuid4(), username="security_test_a", password_hash=hash_password("TestOnlyPassword_A_123!"))
            user_b = User(id=uuid4(), username="security_test_b", password_hash=hash_password("TestOnlyPassword_B_123!"))
            db.add_all([user_a, user_b])
            db.flush()
            mission = Mission(
                id=uuid4(),
                user_id=user_b.id,
                title="보안 테스트용 미션",
                description="다른 계정의 미션",
                energy_level=0,
                mission_type="non_place",
                place_category=None,
                status="in_progress",
                estimated_duration_minutes=10,
                estimated_cost=0,
            )
            db.add(mission)
            db.commit()
            a_id, b_id, mission_id = user_a.id, user_b.id, mission.id

        with TestClient(app) as client:
            yield client, create_token(a_id), create_token(b_id), mission_id
    finally:
        app.dependency_overrides.pop(get_db, None)
        Base.metadata.drop_all(engine)
        engine.dispose()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_unauthenticated_mission_access_denied(security_env):
    client, _, _, mission_id = security_env
    response = client.get(f"/api/v1/missions/{mission_id}")
    assert response.status_code == 401


def test_other_users_mission_read_and_write_denied(security_env):
    client, a_token, b_token, mission_id = security_env
    # Owner can access, but a different account cannot.
    assert client.get(f"/api/v1/missions/{mission_id}", headers=_auth(b_token)).status_code == 200
    assert client.get(f"/api/v1/missions/{mission_id}", headers=_auth(a_token)).status_code in (403, 404)
    response = client.patch(
        f"/api/v1/missions/{mission_id}/steps",
        headers=_auth(a_token),
        json={"completedStepIds": []},
    )
    assert response.status_code in (403, 404)


def test_invalid_user_defaults_rejected(security_env):
    client, a_token, _, _ = security_env
    response = client.put(
        "/api/v1/users/me/defaults",
        headers=_auth(a_token),
        json={
            "mood": "refresh",
            "energyLevel": 9,
            "barriers": [],
            "distanceLimit": "any",
            "timeLimit": "any",
            "budgetLimit": 50000,
        },
    )
    assert response.status_code == 422


def test_invalid_token_denied(security_env):
    client, _, _, mission_id = security_env
    response = client.get(
        f"/api/v1/missions/{mission_id}",
        headers={"Authorization": "Bearer invalid.security.token"},
    )
    assert response.status_code == 401


def test_login_rate_limit(security_env):
    client, _, _, _ = security_env

    for _ in range(10):
        response = client.post(
            "/api/v1/auth/login",
            json={
                "username": "security_test_a",
                "password": "wrong_password"
            }
        )
        assert response.status_code == 401

    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": "security_test_a",
            "password": "wrong_password"
        }
    )
    assert response.status_code == 429