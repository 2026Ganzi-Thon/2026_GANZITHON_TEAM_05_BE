import os
from pathlib import Path

# tests use a file-backed temporary SQLite database (isolated from the developer's DB)
os.environ['DATABASE_URL'] = 'sqlite:///./test_letit.db'
os.environ['JWT_SECRET_KEY'] = 'unit-test-secret-value-not-for-deploy-12345'
from fastapi.testclient import TestClient
from app.main import app as api_app
from app.database import Base, engine
import app.models


def test_health_auth_defaults():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(api_app) as c:
        assert c.get('/health').json() == {'status': 'ok'}
        assert c.get('/health/db').json() == {'database': 'connected'}
        assert len(Base.metadata.tables) == 8
        assert c.get('/api/v1/users/me/defaults').status_code == 401
        r = c.post('/api/v1/auth/signup', json={'username':'newuser','password':'test-password-0123','passwordConfirm':'test-password-0123'})
        assert r.status_code == 201, r.text
        jwt = r.json()['accessToken']
        assert len(r.json()['user']['userId']) == 36
        assert c.post('/api/v1/auth/signup', json={'username':'newuser','password':'test-password-0123','passwordConfirm':'test-password-0123'}).status_code == 409
        assert c.post('/api/v1/auth/login', json={'username':'newuser','password':'bad'}).status_code == 401
        assert c.post('/api/v1/auth/login', json={'username':'newuser','password':'test-password-0123'}).status_code == 200
        headers = {'Authorization':f'Bearer {jwt}'}
        assert c.get('/api/v1/users/me/defaults',headers=headers).json()['onboardingCompleted'] is False
        payload = {'mood':None,'energyLevel':2,'barriers':['crowded'],'distanceLimit':'10min','timeLimit':'30min','budgetLimit':5000}
        assert c.put('/api/v1/users/me/defaults',headers=headers,json=payload).json()['onboardingCompleted'] is True
        assert c.get('/api/v1/users/me/defaults',headers=headers).json()['barriers'] == ['crowded']
    Base.metadata.drop_all(bind=engine)
    Path('test_letit.db').unlink(missing_ok=True)
