"""A-part flower collection: grants happen on mission completion, never on claim."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DailyRecord, User, UserFlower
from app.security import current_user
from app.services.flower_service import FLOWERS

router = APIRouter(prefix='/collection/flowers', tags=['도감'])


def readable_flower(num: int, row: UserFlower | None) -> dict:
    name, description = FLOWERS[num - 1]
    return {'flowerNumber': num, 'name': name, 'description': description,
            'owned': row is not None, 'acquiredAt': row.acquired_at if row else None}


@router.get('')
def get_flowers(user: User = Depends(current_user), db: Session = Depends(get_db)):
    days = db.scalar(select(func.count()).select_from(DailyRecord).where(
        DailyRecord.user_id == user.id, DailyRecord.ever_completed.is_(True))) or 0
    owned = {f.flower_number: f for f in db.scalars(select(UserFlower).where(UserFlower.user_id == user.id)).all()}
    return {'cumulativeCompletedDays': days,
            'flowers': [readable_flower(i, owned.get(i)) for i in range(1, 19)],
            'pendingClaimCount': sum(f.claimed_at is None for f in owned.values())}


@router.post('/claim')
def claim_flowers(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(UserFlower).where(UserFlower.user_id == user.id, UserFlower.claimed_at.is_(None)).order_by(UserFlower.flower_number)).all()
    now = datetime.now(timezone.utc)
    for row in rows:
        row.claimed_at = now
    db.commit()
    return {'claimedFlowers': [{'flowerNumber': f.flower_number,
                                'name': FLOWERS[f.flower_number - 1][0],
                                'acquiredAt': f.acquired_at} for f in rows],
            'pendingClaimCount': 0}
