from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, UserDefaults
from app.schemas import DefaultsIn, DefaultsOut
from app.security import current_user

router = APIRouter(prefix='/users', tags=['사용자 기본값'])


def to_schema(row: UserDefaults) -> DefaultsOut:
    return DefaultsOut(mood=row.mood, energyLevel=row.energy_level, barriers=row.barriers,
                       distanceLimit=row.distance_limit, timeLimit=row.time_limit,
                       budgetLimit=row.budget_limit, onboardingCompleted=row.onboarding_completed)


@router.put('/me/defaults', response_model=DefaultsOut)
def update_defaults(body: DefaultsIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(UserDefaults, user.id)
    if row is None:
        row = UserDefaults(user_id=user.id)
        db.add(row)
    row.mood = body.mood
    row.energy_level = body.energyLevel
    row.barriers = list(body.barriers)
    row.distance_limit = body.distanceLimit
    row.time_limit = body.timeLimit
    row.budget_limit = body.budgetLimit
    row.onboarding_completed = True
    db.commit()
    db.refresh(row)
    return to_schema(row)


@router.get('/me/defaults', response_model=DefaultsOut)
def get_defaults(user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(UserDefaults, user.id)
    if row is None:
        return DefaultsOut(mood=None, energyLevel=1, barriers=[], distanceLimit='10min',
                           timeLimit='30min', budgetLimit=5000, onboardingCompleted=False)
    return to_schema(row)
