"""A endpoints on isolated local SQLite. Never writes to Supabase."""
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from uuid import uuid4

os.environ['DATABASE_URL'] = 'sqlite:///./test_letit_a.db'
os.environ['JWT_SECRET_KEY'] = 'unit-test-secret-value-not-for-deploy-12345'

from fastapi.testclient import TestClient
from app.main import app as api_app
from app.database import engine, SessionLocal, Base
from app.models import User, Mission, MissionStep, DailyRecord
from app.services.record_service import on_mission_changed, KST
import app.models


def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def teardown_function():
    Base.metadata.drop_all(engine)
    engine.dispose()


def signup(c, username):
    r=c.post('/api/v1/auth/signup',json={'username':username,'password':'TestPass01234!', 'passwordConfirm':'TestPass01234!'})
    assert r.status_code==201,r.text
    return {'Authorization':f"Bearer {r.json()['accessToken']}"},r.json()['user']['userId']


def create_mission(user_id, status='paused', days_ago=0, energy=2, place=True):
    from uuid import UUID
    from app.models import Mission
    with SessionLocal() as db:
        now=datetime.now(timezone.utc)-timedelta(days=days_ago)
        m=Mission(user_id=UUID(user_id),title='테스트 산책',description='외출하기',energy_level=energy,
                  mission_type='place' if place else 'non_place',place_category='park' if place else None,
                  status=status,started_at=now,completed_at=now if status=='completed' else None,
                  estimated_duration_minutes=15,estimated_cost=0,
                  place_info={'placeId':'12345','name':'도산공원','address':'서울특별시','latitude':37.5,
                              'longitude':127.0,'distanceMeters':350,'walkingMinutes':5} if place else None,
                  archived_hidden=False)
        db.add(m);db.flush()
        for n in range(1,6):
            db.add(MissionStep(mission_id=m.id,step_order=n,text=f'{n}단계',is_completed=(n<=2 if status!='completed' else True)))
        on_mission_changed(db,m)
        db.commit()
        return str(m.id)


def test_review_archive_ownership():
    with TestClient(api_app) as c:
        ha,ua=signup(c,'review_user1'); hb,ub=signup(c,'review_user2')
        mid=create_mission(ua)
        assert c.put(f'/api/v1/missions/{mid}/review',headers=hb,json={'moodAfter':'better'}).status_code==404
        r=c.put(f'/api/v1/missions/{mid}/review',headers=ha,json={'moodAfter':'better','oneLine':'좋았어요','retryActivity':None,'goodPoint':None})
        assert r.status_code==200,r.text
        assert r.json()['review']['oneLine']=='좋았어요'
        assert c.put(f'/api/v1/missions/{mid}/review',headers=ha,json={}).status_code==409
        assert c.get('/api/v1/archive',headers=ha).json()['total']==1
        detail=c.get(f'/api/v1/archive/{mid}',headers=ha)
        assert detail.status_code==200,detail.text
        assert len(detail.json()['steps'])==5
        assert detail.json()['review']['moodAfter']=='better'
        assert c.delete(f'/api/v1/archive/{mid}',headers=hb).status_code==404
        assert c.delete(f'/api/v1/archive/{mid}',headers=ha).status_code==200
        assert c.get(f'/api/v1/archive/{mid}',headers=ha).status_code==404
        assert c.get('/api/v1/archive',headers=ha).json()['total']==0
        # Hiding a record does not erase statistics or flowers.
        month=datetime.now(KST).strftime('%Y-%m')
        assert c.get('/api/v1/reflection/summary',headers=ha,params={'month':month}).json()['counts']['partial']>=1


def test_rest_today_and_calendar():
    with TestClient(api_app) as c:
        h,u=signup(c,'rest_user')
        today=datetime.now(KST).date().isoformat()
        assert c.put('/api/v1/days/2001-01-01/rest',headers=h).status_code==400
        assert c.put(f'/api/v1/days/{today}/rest',headers=h).json()['status']=='rest'
        month=today[:7]
        days=c.get('/api/v1/reflection/calendar',headers=h,params={'month':month}).json()['days']
        assert {'date':today,'status':'rest'} in days
        assert c.get('/api/v1/reflection/summary',headers=h,params={'month':month}).json()['counts']['notCompleted']==1


def test_flower_grant_and_claim_without_regrant():
    with TestClient(api_app) as c:
        h,u=signup(c,'flower_user')
        for d in (3,2,1):
            create_mission(u,status='completed',days_ago=d)
        f=c.get('/api/v1/collection/flowers',headers=h).json()
        assert f['cumulativeCompletedDays']==3,f
        assert f['pendingClaimCount']==1,f
        assert f['flowers'][0]['owned'] is True
        assert f['flowers'][1]['owned'] is False
        claimed=c.post('/api/v1/collection/flowers/claim',headers=h)
        assert claimed.status_code==200,claimed.text
        assert len(claimed.json()['claimedFlowers'])==1
        assert c.post('/api/v1/collection/flowers/claim',headers=h).json()['claimedFlowers']==[]
        # Completing another mission on a previously successful day never adds flower.
        create_mission(u,status='completed',days_ago=1)
        assert c.get('/api/v1/collection/flowers',headers=h).json()['pendingClaimCount']==0
        progress=c.get('/api/v1/reflection/progress',headers=h,params={'from':'2000-01-01','to':'2099-12-31'})
        assert progress.status_code==200,progress.text
        assert progress.json()['completedByEnergy'][2]['count']==4


def test_incomplete_day_and_user_isolation():
    with TestClient(api_app) as c:
        h,u=signup(c,'calendar_one'); h2,u2=signup(c,'calendar_two')
        create_mission(u,status='completed',days_ago=1)
        create_mission(u,status='paused',days_ago=1)
        monthly=(datetime.now(KST)-timedelta(days=1)).strftime('%Y-%m')
        days=c.get('/api/v1/reflection/calendar',headers=h,params={'month':monthly}).json()['days']
        assert any(d['status']=='partial' for d in days), days
        assert c.get('/api/v1/archive',headers=h2).json()['total']==0
        assert c.get('/api/v1/collection/flowers',headers=h2).json()['cumulativeCompletedDays']==0
