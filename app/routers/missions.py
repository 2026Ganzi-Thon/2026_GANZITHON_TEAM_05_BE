"""Mission start and progress routes using the shared SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Mission, MissionReview, MissionStep, Recommendation, User
from app.security import current_user

router = APIRouter(prefix="/missions", tags=["미션 수행"])
RESUMABLE_DAYS = 7


class MissionStartIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recommendation_id: UUID = Field(alias="recommendationId")
    place_id: str | None = Field(alias="placeId")


class MissionStartedOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mission_id: UUID = Field(alias="missionId")
    title: str
    status: Literal["in_progress"]
    completed_step_ids: list[UUID] = Field(alias="completedStepIds")
    completed_count: int = Field(alias="completedCount")
    remaining_count: int = Field(alias="remainingCount")


class MissionStepOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    step_id: UUID = Field(alias="stepId")
    order: int
    text: str
    completed: bool


class PlaceInfoOut(BaseModel):
    """Place details saved with the mission; Kakao enrichment is not wired yet."""
    model_config = ConfigDict(populate_by_name=True)

    place_id: str = Field(alias="placeId")
    name: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    distance_meters: int | None = Field(default=None, alias="distanceMeters")
    walking_minutes: int | None = Field(default=None, alias="walkingMinutes")


class ReviewOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mood_after: str | None = Field(alias="moodAfter")
    one_line: str | None = Field(alias="oneLine")
    retry_activity: str | None = Field(alias="retryActivity")
    good_point: str | None = Field(alias="goodPoint")


class MissionDetailOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mission_id: UUID = Field(alias="missionId")
    title: str
    description: str
    energy_level: int = Field(alias="energyLevel")
    mission_type: Literal["place", "non_place"] = Field(alias="missionType")
    place_category: str | None = Field(alias="placeCategory")
    steps: list[MissionStepOut]
    place: PlaceInfoOut | None
    estimated_duration_minutes: int = Field(alias="estimatedDurationMinutes")
    estimated_cost: int = Field(alias="estimatedCost")
    status: Literal["in_progress", "paused", "completed", "abandoned", "replaced"]
    started_at: datetime = Field(alias="startedAt")
    completed_at: datetime | None = Field(alias="completedAt")
    resumable_until: datetime | None = Field(alias="resumableUntil")
    completed_step_ids: list[UUID] = Field(alias="completedStepIds")
    review: ReviewOut | None


class StepIdsIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    completed_step_ids: list[UUID] = Field(alias="completedStepIds", max_length=5)


class StepIdsOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mission_id: UUID = Field(alias="missionId")
    completed_step_ids: list[UUID] = Field(alias="completedStepIds")
    status: Literal["in_progress"]


class SavedProgressOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mission_id: UUID = Field(alias="missionId")
    status: Literal["paused"]
    completed_step_ids: list[UUID] = Field(alias="completedStepIds")
    completed_count: int = Field(alias="completedCount")
    remaining_count: int = Field(alias="remainingCount")
    resumable_until: datetime = Field(alias="resumableUntil")


class CompletedOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mission_id: UUID = Field(alias="missionId")
    status: Literal["completed"]
    completed_at: datetime = Field(alias="completedAt")
    completed_count: int = Field(alias="completedCount")


class ResumableMissionOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mission_id: UUID = Field(alias="missionId")
    title: str
    energy_level: int = Field(alias="energyLevel")
    status: Literal["paused"]
    completed_step_ids: list[UUID] = Field(alias="completedStepIds")
    resumable_until: datetime = Field(alias="resumableUntil")


class ResumableListOut(BaseModel):
    items: list[ResumableMissionOut]


class ResumedOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mission_id: UUID = Field(alias="missionId")
    status: Literal["in_progress"]
    completed_step_ids: list[UUID] = Field(alias="completedStepIds")


class AbandonedOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mission_id: UUID = Field(alias="missionId")
    status: Literal["abandoned"]
    removed_from_resumable: Literal[True] = Field(alias="removedFromResumable")


def _get_user_mission(db: Session, mission_id: UUID, user_id: UUID) -> Mission:
    mission = db.scalar(
        select(Mission).where(Mission.id == mission_id, Mission.user_id == user_id)
    )
    if mission is None:
        raise HTTPException(status_code=404, detail="미션을 찾을 수 없습니다.")
    return mission


def _mission_steps(db: Session, mission_id: UUID) -> list[MissionStep]:
    return list(db.scalars(
        select(MissionStep)
        .where(MissionStep.mission_id == mission_id)
        .order_by(MissionStep.step_order)
    ).all())


def _completed_ids(steps: list[MissionStep]) -> list[UUID]:
    return [step.id for step in steps if step.is_completed]


def _started_at_utc(mission: Mission) -> datetime:
    value = mission.started_at or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _resumable_until(mission: Mission) -> datetime:
    return _started_at_utc(mission) + timedelta(days=RESUMABLE_DAYS)


def _set_completed_steps(steps: list[MissionStep], completed_ids: list[UUID]) -> None:
    requested = set(completed_ids)
    actual = {step.id for step in steps}
    if len(requested) != len(completed_ids):
        raise HTTPException(status_code=400, detail="완료 단계 ID가 중복되었습니다.")
    if not requested.issubset(actual):
        raise HTTPException(status_code=400, detail="해당 미션에 속하지 않는 단계 ID입니다.")
    for step in steps:
        step.is_completed = step.id in requested


@router.post("", response_model=MissionStartedOut, status_code=status.HTTP_201_CREATED)
def start_mission(
    body: MissionStartIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> MissionStartedOut:
    recommendation = db.scalar(select(Recommendation).where(
        Recommendation.id == body.recommendation_id,
        Recommendation.user_id == user.id,
    ))
    if recommendation is None:
        raise HTTPException(status_code=404, detail="추천을 찾을 수 없습니다.")
    if recommendation.mission_type == "place" and not body.place_id:
        raise HTTPException(status_code=400, detail="장소 미션에는 장소를 선택해야 합니다.")
    if recommendation.mission_type == "non_place" and body.place_id is not None:
        raise HTTPException(status_code=400, detail="장소가 없는 미션에는 placeId를 보낼 수 없습니다.")

    mission = Mission(
        id=uuid4(),
        user_id=user.id,
        recommendation_id=recommendation.id,
        title=recommendation.title,
        description=recommendation.description,
        energy_level=recommendation.energy_level,
        mission_type=recommendation.mission_type,
        place_category=recommendation.place_category,
        status="in_progress",
        estimated_duration_minutes=recommendation.estimated_duration_minutes,
        estimated_cost=recommendation.estimated_cost,
        # Current request contract contains only placeId. Full Kakao details require
        # the agreed Kakao integration and are intentionally not fabricated here.
        place_info={"placeId": body.place_id} if body.place_id else None,
    )
    db.add(mission)
    try:
        db.flush()
        source_steps = recommendation.steps or []
        if len(source_steps) != 5:
            raise HTTPException(status_code=400, detail="추천 단계가 5개가 아니어서 미션을 시작할 수 없습니다.")
        for index, source in enumerate(source_steps, start=1):
            db.add(MissionStep(
                id=uuid4(),
                mission_id=mission.id,
                step_order=index,
                text=source["text"],
                is_completed=False,
            ))
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except (KeyError, TypeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="추천 단계 정보를 처리할 수 없습니다.") from exc
    db.refresh(mission)
    return MissionStartedOut(
        missionId=mission.id,
        title=mission.title,
        status="in_progress",
        completedStepIds=[],
        completedCount=0,
        remainingCount=5,
    )


@router.get("/resumable", response_model=ResumableListOut)
def list_resumable_missions(
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ResumableListOut:
    missions = db.scalars(
        select(Mission)
        .where(Mission.user_id == user.id, Mission.status == "paused")
        .order_by(Mission.started_at.desc())
    ).all()
    now = datetime.now(timezone.utc)
    items = []
    for mission in missions:
        until = _resumable_until(mission)
        if until <= now:
            continue
        items.append(ResumableMissionOut(
            missionId=mission.id,
            title=mission.title,
            energyLevel=mission.energy_level,
            status="paused",
            completedStepIds=_completed_ids(_mission_steps(db, mission.id)),
            resumableUntil=until,
        ))
    return ResumableListOut(items=items)


@router.get("/{mission_id}", response_model=MissionDetailOut)
def get_mission(
    mission_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> MissionDetailOut:
    mission = _get_user_mission(db, mission_id, user.id)
    steps = _mission_steps(db, mission.id)
    review = db.get(MissionReview, mission.id)
    place = PlaceInfoOut.model_validate(mission.place_info) if mission.place_info else None
    review_out = ReviewOut.model_validate({
        "moodAfter": review.mood_after,
        "oneLine": review.one_line,
        "retryActivity": review.retry_activity,
        "goodPoint": review.good_point,
    }) if review else None
    return MissionDetailOut(
        missionId=mission.id,
        title=mission.title,
        description=mission.description,
        energyLevel=mission.energy_level,
        missionType=mission.mission_type,
        placeCategory=mission.place_category,
        steps=[MissionStepOut(stepId=s.id, order=s.step_order, text=s.text, completed=s.is_completed) for s in steps],
        place=place,
        estimatedDurationMinutes=mission.estimated_duration_minutes,
        estimatedCost=mission.estimated_cost,
        status=mission.status,
        startedAt=_started_at_utc(mission),
        completedAt=mission.completed_at,
        resumableUntil=_resumable_until(mission) if mission.status in {"in_progress", "paused"} else None,
        completedStepIds=_completed_ids(steps),
        review=review_out,
    )


@router.patch("/{mission_id}/steps", response_model=StepIdsOut)
def update_mission_steps(
    mission_id: UUID,
    body: StepIdsIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> StepIdsOut:
    mission = _get_user_mission(db, mission_id, user.id)
    if mission.status != "in_progress":
        raise HTTPException(status_code=400, detail="진행 중인 미션의 단계만 수정할 수 있습니다.")
    steps = _mission_steps(db, mission.id)
    _set_completed_steps(steps, body.completed_step_ids)
    db.commit()
    return StepIdsOut(missionId=mission.id, completedStepIds=_completed_ids(steps), status="in_progress")


@router.post("/{mission_id}/save-progress", response_model=SavedProgressOut)
def save_mission_progress(
    mission_id: UUID,
    body: StepIdsIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SavedProgressOut:
    mission = _get_user_mission(db, mission_id, user.id)
    if mission.status != "in_progress":
        raise HTTPException(status_code=400, detail="진행 중인 미션만 저장할 수 있습니다.")
    steps = _mission_steps(db, mission.id)
    _set_completed_steps(steps, body.completed_step_ids)
    completed = len(_completed_ids(steps))
    if completed == len(steps):
        raise HTTPException(status_code=400, detail="모든 단계를 완료한 미션은 진행 저장할 수 없습니다.")
    mission.status = "paused"
    db.commit()
    return SavedProgressOut(
        missionId=mission.id,
        status="paused",
        completedStepIds=_completed_ids(steps),
        completedCount=completed,
        remainingCount=len(steps) - completed,
        resumableUntil=_resumable_until(mission),
    )


@router.post("/{mission_id}/complete", response_model=CompletedOut)
def complete_mission(
    mission_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> CompletedOut:
    mission = _get_user_mission(db, mission_id, user.id)
    if mission.status != "in_progress":
        raise HTTPException(status_code=400, detail="진행 중인 미션만 완료할 수 있습니다.")
    steps = _mission_steps(db, mission.id)
    completed = len(_completed_ids(steps))
    if completed != len(steps) or not steps:
        raise HTTPException(status_code=400, detail="완료하지 않은 단계가 있습니다.")
    mission.status = "completed"
    mission.completed_at = datetime.now(timezone.utc)
    db.commit()
    return CompletedOut(
        missionId=mission.id,
        status="completed",
        completedAt=mission.completed_at,
        completedCount=completed,
    )


@router.post("/{mission_id}/resume", response_model=ResumedOut)
def resume_mission(
    mission_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ResumedOut:
    mission = _get_user_mission(db, mission_id, user.id)
    if mission.status != "paused":
        raise HTTPException(status_code=400, detail="이어하기 가능한 미션이 아닙니다.")
    if _resumable_until(mission) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="미션 시작 후 7일이 지나 이어할 수 없습니다.")
    mission.status = "in_progress"
    steps = _mission_steps(db, mission.id)
    db.commit()
    return ResumedOut(missionId=mission.id, status="in_progress", completedStepIds=_completed_ids(steps))


@router.post("/{mission_id}/abandon", response_model=AbandonedOut)
def abandon_mission(
    mission_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> AbandonedOut:
    mission = _get_user_mission(db, mission_id, user.id)
    if mission.status not in {"in_progress", "paused"}:
        raise HTTPException(status_code=400, detail="진행 중이거나 저장된 미션만 그만둘 수 있습니다.")
    mission.status = "abandoned"
    db.commit()
    return AbandonedOut(missionId=mission.id, status="abandoned", removedFromResumable=True)
