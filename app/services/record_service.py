"""Date-level activity projection. Call from mission mutation handlers before commit.

Date status is a projection of *all* real missions for that local Seoul date;
'ever_completed' is intentionally sticky for earned rewards.
"""
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import DailyRecord, Mission, UserFlower

KST = ZoneInfo('Asia/Seoul')
FLOWER_LIMIT = 18


def as_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        # PostgreSQL returns aware UTC timestamps; SQLite test datetimes are naive UTC.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST)


def activity_date(mission: Mission) -> date:
    """Completed missions count on their completion date, others on start date."""
    return as_kst(mission.completed_at if mission.status == 'completed' and mission.completed_at else mission.started_at).date()


def update_daily_projection(db: Session, user_id: UUID, day: date) -> str:
    """Refresh calendar status without forgetting historic flower-eligible days."""
    missions = db.scalars(select(Mission).where(Mission.user_id == user_id, Mission.status != 'replaced')).all()
    on_day = [m for m in missions if activity_date(m) == day]
    row = db.get(DailyRecord, (user_id, day))
    if on_day:
        status = 'completed' if all(m.status == 'completed' for m in on_day) else 'partial'
    else:
        status = 'rest' if row is not None and row.status == 'rest' else 'undecided'
    if row is None and status != 'undecided':
        row = DailyRecord(user_id=user_id, record_date=day, status=status, ever_completed=False)
        db.add(row)
    elif row is not None:
        row.status = status
    if row is not None and status == 'completed':
        row.ever_completed = True
    db.flush()
    return status


def grant_earned_flowers(db: Session, user_id: UUID) -> list[int]:
    """Grant automatically on reaching 3, 6, ... distinct eligible calendar days.

    One row per (user_id, flower_number) prevents duplicate rewards.
    Requires same transaction as mission completion.
    """
    completed_days = db.scalar(select(func.count()).select_from(DailyRecord).where(
        DailyRecord.user_id == user_id, DailyRecord.ever_completed.is_(True)
    )) or 0
    target = min(FLOWER_LIMIT, completed_days // 3)
    existing = set(db.scalars(select(UserFlower.flower_number).where(UserFlower.user_id == user_id)).all())
    awards = []
    for num in range(1, target + 1):
        if num not in existing:
            db.add(UserFlower(user_id=user_id, flower_number=num))
            awards.append(num)
    db.flush()
    return awards


def on_mission_changed(db: Session, mission: Mission, *, previous_date: date | None = None) -> dict:
    """Integration hook for B's completion, pause, abandon and replacement handlers.

    Call after assigning mission.status / completed_at, BEFORE db.commit().
    This function does not commit and leaves transaction ownership with caller.
    """
    db.flush()
    dates = {activity_date(mission)}
    if previous_date is not None:
        dates.add(previous_date)
    outcomes = {d.isoformat(): update_daily_projection(db, mission.user_id, d) for d in dates}
    awards = grant_earned_flowers(db, mission.user_id) if mission.status == 'completed' else []
    return {'dailyStatuses': outcomes, 'newFlowerNumbers': awards}
