"""A-part records: review, rest, archive, reflection and progress (8 endpoints)."""
from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DailyRecord, Mission, MissionReview, MissionStep, User
from app.security import current_user
from app.services.record_service import as_kst, activity_date, KST

router = APIRouter(tags=['성찰·기록'])


class ReviewIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    mood_after: str | None = Field(default=None, alias='moodAfter')
    one_line: str | None = Field(default=None, alias='oneLine', max_length=1500)
    retry_activity: str | None = Field(default=None, alias='retryActivity', max_length=1500)
    good_point: str | None = Field(default=None, alias='goodPoint', max_length=1500)


def get_owned_mission(db: Session, mission_id: UUID, user_id: UUID, *, allow_hidden=False) -> Mission:
    mission = db.get(Mission, mission_id)
    if mission is None or mission.user_id != user_id or mission.status == 'replaced' or (mission.archived_hidden and not allow_hidden):
        raise HTTPException(status_code=404, detail='기록을 찾을 수 없습니다.')
    return mission


def readable_review(review: MissionReview | None) -> dict | None:
    if review is None:
        return None
    return {'moodAfter': review.mood_after, 'oneLine': review.one_line,
            'retryActivity': review.retry_activity, 'goodPoint': review.good_point}


def mission_steps(db: Session, mission_id: UUID) -> list[MissionStep]:
    return list(db.scalars(select(MissionStep).where(MissionStep.mission_id == mission_id).order_by(MissionStep.step_order)).all())


def mission_detail(db: Session, mission: Mission) -> dict:
    steps = mission_steps(db, mission.id)
    return {
        'missionId': mission.id, 'title': mission.title, 'description': mission.description,
        'energyLevel': mission.energy_level, 'missionType': mission.mission_type,
        'placeCategory': mission.place_category, 'status': mission.status,
        'startedAt': mission.started_at, 'completedAt': mission.completed_at,
        'resumableUntil': mission.started_at + timedelta(days=7) if mission.status in ('paused','in_progress') else None,
        'steps': [{'stepId': s.id, 'order': s.step_order, 'text': s.text, 'completed': s.is_completed} for s in steps],
        'completedStepIds': [s.id for s in steps if s.is_completed],
        'estimatedDurationMinutes': mission.estimated_duration_minutes, 'estimatedCost': mission.estimated_cost,
        'place': mission.place_info, 'review': readable_review(db.get(MissionReview, mission.id))
    }


@router.put('/days/{day}/rest')
def mark_rest(day: date, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if day != datetime.now(KST).date():
        raise HTTPException(status_code=400, detail='오늘 날짜에만 쉬기 기록을 남길 수 있습니다.')
    exists = db.scalars(select(Mission).where(Mission.user_id == user.id, Mission.status != 'replaced')).all()
    if any(activity_date(m) == day for m in exists):
        raise HTTPException(status_code=409, detail='이미 미션 수행 기록이 있는 날짜입니다.')
    row = db.get(DailyRecord, (user.id, day))
    if row is None:
        db.add(DailyRecord(user_id=user.id, record_date=day, status='rest', ever_completed=False))
    else:
        row.status = 'rest'
    db.commit()
    return {'date': day, 'status': 'rest'}


@router.put('/missions/{mission_id}/review')
def save_review(mission_id: UUID, body: ReviewIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    mission = get_owned_mission(db, mission_id, user.id)
    if mission.status not in ('paused','completed','abandoned'):
        raise HTTPException(status_code=409, detail='미션을 저장하거나 완료한 후 후기를 작성할 수 있습니다.')
    if db.get(MissionReview, mission_id):
        raise HTTPException(status_code=409, detail='이미 저장한 후기는 수정할 수 없습니다.')
    row = MissionReview(mission_id=mission_id, mood_after=body.mood_after,
                        one_line=body.one_line, retry_activity=body.retry_activity, good_point=body.good_point)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {'missionId': mission.id, 'review': readable_review(row), 'savedAt': row.created_at}


@router.get('/reflection/summary')
def reflection_summary(month: str = Query(pattern=r'^\d{4}-(0[1-9]|1[0-2])$'), user: User = Depends(current_user), db: Session = Depends(get_db)):
    # Counts are mission records (completed/partial) and rest DAYS (notCompleted).
    missions = db.scalars(select(Mission).where(Mission.user_id == user.id, Mission.status != 'replaced')).all()
    monthly = [m for m in missions if activity_date(m).strftime('%Y-%m') == month]
    rows = db.scalars(select(DailyRecord).where(DailyRecord.user_id == user.id)).all()
    def mini(m):
        return {'missionId': m.id, 'title': m.title, 'date': activity_date(m).isoformat()} if m else None
    earliest = min(missions, key=lambda m: as_kst(m.started_at)) if missions else None
    latest = max(missions, key=lambda m: as_kst(m.started_at)) if missions else None
    return {'month': month,
            'counts': {'completed': sum(m.status == 'completed' for m in monthly),
                       'partial': sum(m.status != 'completed' for m in monthly),
                       'notCompleted': sum(r.status == 'rest' and r.record_date.strftime('%Y-%m') == month for r in rows)},
            'firstMission': mini(earliest), 'latestMission': mini(latest)}


@router.get('/reflection/calendar')
def reflection_calendar(month: str = Query(pattern=r'^\d{4}-(0[1-9]|1[0-2])$'), user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(DailyRecord).where(DailyRecord.user_id == user.id)).all()
    days = [{'date': r.record_date, 'status': r.status} for r in rows if r.record_date.strftime('%Y-%m') == month]
    days.sort(key=lambda d:d['date'])
    return {'month': month, 'days': days}


def archive_mini(db: Session, m: Mission) -> dict:
    steps = mission_steps(db, m.id)
    done = sum(s.is_completed for s in steps)
    return {'missionId': m.id, 'title': m.title, 'energyLevel': m.energy_level,
            'startedAt': m.started_at, 'endedAt': m.completed_at,
            'completedCount': done, 'remainingCount': len(steps) - done, 'status': m.status}


@router.get('/archive')
def archive(page: int = Query(default=1, ge=1), pageSize: int = Query(default=20, ge=1, le=100),
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    condition = (Mission.user_id == user.id, Mission.status != 'replaced', Mission.archived_hidden.is_(False))
    total = db.scalar(select(func.count()).select_from(Mission).where(*condition)) or 0
    items = db.scalars(select(Mission).where(*condition).order_by(Mission.started_at.desc()).offset((page-1)*pageSize).limit(pageSize)).all()
    return {'items': [archive_mini(db, m) for m in items], 'page': page, 'pageSize': pageSize,
            'total': total, 'hasNext': page*pageSize < total}


@router.get('/archive/{mission_id}')
def archive_detail(mission_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return mission_detail(db, get_owned_mission(db, mission_id, user.id))


@router.delete('/archive/{mission_id}')
def delete_archive(mission_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    mission = get_owned_mission(db, mission_id, user.id)
    mission.archived_hidden = True
    db.commit()
    return {'success': True, 'missionId': mission_id, 'archivedHidden': True}


@router.get('/reflection/progress')
def reflection_progress(start: date = Query(alias='from'), end: date = Query(alias='to'),
                        user: User = Depends(current_user), db: Session = Depends(get_db)):
    if end < start:
        raise HTTPException(status_code=400, detail='조회 종료일은 시작일 이후여야 합니다.')
    missions = db.scalars(select(Mission).where(Mission.user_id == user.id, Mission.status != 'replaced')).all()
    ordered = sorted(missions, key=lambda m: as_kst(m.started_at))
    def mini(m):
        return {'missionId': m.id, 'title': m.title, 'date': activity_date(m)} if m else None
    filtered = [m for m in missions if start <= activity_date(m) <= end]
    counts = [{'energyLevel': e, 'count':sum(m.status == 'completed' and m.energy_level == e for m in filtered)} for e in range(5)]
    distance = sorted([{'date': activity_date(m), 'distanceMeters': m.place_info.get('distanceMeters')}
                       for m in filtered if m.place_info and isinstance(m.place_info.get('distanceMeters'), (int,float))],
                      key=lambda item: item['date'])
    return {'firstMission': mini(ordered[0]) if ordered else None, 'latestMission': mini(ordered[-1]) if ordered else None,
            'completedByEnergy': counts, 'distanceHistory': distance}
