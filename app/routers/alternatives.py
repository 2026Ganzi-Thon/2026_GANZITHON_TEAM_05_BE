"""Alternative mission suggestions and replacing an active mission."""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Mission, MissionStep, Recommendation, User
from app.routers.recommendations import (
    MissionConditions,
    RecommendationOut,
    RecommendationsOut,
    _request_upstage,
)
from app.security import current_user

router = APIRouter(prefix="/missions", tags=["대체 미션"])

Situation = Literal["weather", "closed", "crowded", "low_energy", "cost", "other"]


class AlternativesIn(BaseModel):
    situation: Situation


class ReplaceIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recommendation_id: UUID = Field(alias="recommendationId")
    place_id: str | None = Field(alias="placeId")


class ReplaceOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    old_mission_id: UUID = Field(alias="oldMissionId")
    new_mission_id: UUID = Field(alias="newMissionId")


def _user_mission(db: Session, mission_id: UUID, user_id: UUID) -> Mission:
    mission = db.scalar(select(Mission).where(
        Mission.id == mission_id,
        Mission.user_id == user_id,
    ))
    if mission is None:
        raise HTTPException(status_code=404, detail="미션을 찾을 수 없습니다.")
    return mission


def _same_energy_fallback(conditions: MissionConditions, situation: Situation) -> list[dict]:
    """Return two free, nearby, non-place options if Solar is unavailable."""
    templates = {
        "weather": [
            ("창가에서 바깥 공기 느끼기", "날씨가 좋지 않은 날, 실내에서 잠깐 쉬어요", ["편한 자리를 찾아요", "창가로 가요", "밖을 바라봐요", "천천히 숨을 쉬어요", "원하는 때 마쳐요"]),
            ("가벼운 실내 스트레칭", "실내에서 무리 없이 몸을 풀어요", ["편한 옷을 입어요", "자리 주변을 정리해요", "목과 어깨를 천천히 풀어요", "깊게 숨을 쉬어요", "편한 자세로 마쳐요"]),
        ],
        "closed": [
            ("집 앞 익숙한 길 걷기", "닫힌 장소 대신 가까운 길을 잠깐 걸어요", ["편한 신발을 신어요", "집 밖으로 나와요", "익숙한 길을 골라요", "가능한 만큼 걸어요", "천천히 돌아와요"]),
            ("집 근처에서 잠깐 쉬기", "장소에 들어가지 않고 주변에서 잠깐 쉬어요", ["외투를 챙겨요", "집 밖으로 나와요", "편한 곳에 잠시 서요", "주변 풍경을 살펴봐요", "집으로 돌아와요"]),
        ],
        "crowded": [
            ("조용한 시간 보내기", "사람이 많은 곳을 피해서 집에서 쉬어요", ["조용한 자리를 찾아요", "휴대전화를 잠시 내려놔요", "천천히 숨을 쉬어요", "편한 음악을 들어요", "원하는 때 마쳐요"]),
            ("한적한 길 잠깐 걷기", "붐비는 곳을 피해 익숙한 길을 걸어요", ["편한 신발을 신어요", "한적한 길을 골라요", "천천히 걸어요", "주변을 잠깐 살펴봐요", "집으로 돌아와요"]),
        ],
        "low_energy": [
            ("자리에서 가볍게 몸 풀기", "에너지를 많이 쓰지 않는 작은 움직임을 해요", ["편한 자세로 앉아요", "어깨를 천천히 올렸다 내려요", "손목을 가볍게 돌려요", "숨을 천천히 쉬어요", "편안한 자세로 마쳐요"]),
            ("현관 앞까지 다녀오기", "준비를 최소화해 짧게 바깥 공기를 느껴요", ["겉옷을 걸쳐요", "현관문을 열어요", "문 앞에 잠깐 서요", "바깥 공기를 느껴요", "편하게 안으로 들어와요"]),
        ],
        "cost": [
            ("무료로 동네 산책하기", "돈을 쓰지 않고 가까운 곳을 걸어요", ["편한 신발을 신어요", "집 밖으로 나와요", "익숙한 길을 걸어요", "주변을 둘러봐요", "천천히 돌아와요"]),
            ("집에서 쉬는 시간 만들기", "비용 없이 짧은 휴식 시간을 가져요", ["조용한 자리를 찾아요", "물을 한 잔 준비해요", "편하게 앉아요", "천천히 숨을 쉬어요", "원하는 때 마쳐요"]),
        ],
        "other": [
            ("집 앞 공기 마시기", "부담 없이 잠깐 바깥 공기를 느껴봐요", ["편한 옷을 입어요", "신발을 신어요", "현관문을 열어요", "건물 밖에 잠깐 서 있어요", "천천히 돌아와요"]),
            ("익숙한 길 천천히 걷기", "가까운 익숙한 길을 편한 속도로 걸어요", ["편한 신발을 신어요", "집 밖으로 나와요", "익숙한 길을 골라요", "천천히 걸어요", "편하게 돌아와요"]),
        ],
    }[situation]
    time_cap = {"10min": 10, "30min": 30, "60min": 60}.get(conditions.time_limit)
    items = []
    for title, description, step_texts in templates:
        items.append({
            "title": title,
            "description": description,
            "energyLevel": conditions.energy_level,
            "missionType": "non_place",
            "placeCategory": None,
            "steps": [{"order": i, "text": text} for i, text in enumerate(step_texts, 1)],
            "estimatedDurationMinutes": min(10, time_cap) if time_cap is not None else 10,
            "estimatedCost": 0,
        })
    return items


def _validated_items(candidates: list[dict], conditions: MissionConditions) -> list[RecommendationOut]:
    items = [RecommendationOut.model_validate({
        **candidate,
        "recommendationId": uuid4(),
        "steps": [{**step, "stepId": uuid4()} for step in candidate["steps"]],
    }) for candidate in candidates]
    if len(items) != 2:
        raise ValueError("추천은 정확히 2개여야 합니다.")
    for item in items:
        if item.energy_level != conditions.energy_level:
            raise ValueError("에너지 단계가 기존 미션과 다릅니다.")
        if (item.mission_type == "place") != (item.place_category is not None):
            raise ValueError("미션 종류와 장소 분류가 맞지 않습니다.")
        if item.estimated_duration_minutes <= 0 or item.estimated_cost < 0:
            raise ValueError("예상 시간 또는 금액이 올바르지 않습니다.")
        if conditions.time_limit != "any":
            cap = {"10min": 10, "30min": 30, "60min": 60}[conditions.time_limit]
            if item.estimated_duration_minutes > cap:
                raise ValueError("예상 시간이 기존 조건을 초과합니다.")
        if conditions.budget_limit is not None and item.estimated_cost > conditions.budget_limit:
            raise ValueError("예상 금액이 기존 조건을 초과합니다.")
    return items


@router.post("/{mission_id}/alternatives", response_model=RecommendationsOut)
def suggest_alternatives(
    mission_id: UUID,
    body: AlternativesIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> RecommendationsOut:
    mission = _user_mission(db, mission_id, user.id)
    if mission.status not in {"in_progress", "paused"}:
        raise HTTPException(status_code=400, detail="진행 중이거나 저장된 미션만 대체 추천을 받을 수 있습니다.")
    if mission.recommendation_id is None:
        raise HTTPException(status_code=400, detail="기존 추천 조건을 찾을 수 없어 대체 추천을 만들 수 없습니다.")
    original = db.scalar(select(Recommendation).where(
        Recommendation.id == mission.recommendation_id,
        Recommendation.user_id == user.id,
    ))
    if original is None:
        raise HTTPException(status_code=404, detail="기존 추천을 찾을 수 없습니다.")
    try:
        conditions = MissionConditions.model_validate(original.conditions)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="기존 미션 조건을 읽을 수 없습니다.") from exc

    situation_text = {
        "weather": "날씨 때문에 기존 장소를 이용할 수 없음. 실내 또는 날씨 영향을 적게 받는 선택지",
        "closed": "장소가 닫힘. 해당 장소에 의존하지 않는 대안",
        "crowded": "장소가 붐빔. 한적하고 자극이 적은 대안",
        "low_energy": "에너지가 부족함. 준비와 움직임이 적은 대안",
        "cost": "비용이 부담됨. 예산 범위 안에서 비용이 낮은 대안",
        "other": "기존 미션을 수행하기 어려운 다른 사정. 더 간단한 대안",
    }[body.situation]

    fallback_used = False
    try:
        candidates = _request_upstage(
            conditions,
            [original.title, mission.title],
            additional_instructions=situation_text,
        )
        recommendations = _validated_items(candidates, conditions)
    except (RuntimeError, ValueError, KeyError, TypeError, ValidationError, json.JSONDecodeError):
        fallback_used = True
        recommendations = _validated_items(_same_energy_fallback(conditions, body.situation), conditions)

    try:
        for item in recommendations:
            saved_conditions = conditions.model_dump(by_alias=True)
            saved_conditions.update({"situation": body.situation, "missionId": str(mission.id)})
            db.add(Recommendation(
                id=item.recommendation_id,
                user_id=user.id,
                title=item.title,
                description=item.description,
                energy_level=item.energy_level,
                mission_type=item.mission_type,
                place_category=item.place_category,
                steps=[step.model_dump(by_alias=True) for step in item.steps],
                conditions=saved_conditions,
                estimated_duration_minutes=item.estimated_duration_minutes,
                estimated_cost=item.estimated_cost,
            ))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="대체 추천을 저장할 수 없습니다.") from exc
    return RecommendationsOut(fallbackUsed=fallback_used, recommendations=recommendations)


@router.post("/{mission_id}/replace", response_model=ReplaceOut, status_code=status.HTTP_201_CREATED)
def replace_mission(
    mission_id: UUID,
    body: ReplaceIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> ReplaceOut:
    old_mission = _user_mission(db, mission_id, user.id)
    if old_mission.status not in {"in_progress", "paused"}:
        raise HTTPException(status_code=400, detail="진행 중이거나 저장된 미션만 변경할 수 있습니다.")
    recommendation = db.scalar(select(Recommendation).where(
        Recommendation.id == body.recommendation_id,
        Recommendation.user_id == user.id,
    ))
    if recommendation is None:
        raise HTTPException(status_code=404, detail="대체 추천을 찾을 수 없습니다.")
    if str(recommendation.conditions.get("missionId")) != str(old_mission.id):
        raise HTTPException(status_code=400, detail="해당 미션에서 생성한 대체 추천이 아닙니다.")
    if recommendation.mission_type == "place" and not body.place_id:
        raise HTTPException(status_code=400, detail="장소 미션에는 장소를 선택해야 합니다.")
    if recommendation.mission_type == "non_place" and body.place_id is not None:
        raise HTTPException(status_code=400, detail="장소가 없는 미션에는 placeId를 보낼 수 없습니다.")
    if not isinstance(recommendation.steps, list) or len(recommendation.steps) != 5:
        raise HTTPException(status_code=400, detail="대체 추천 단계 정보가 올바르지 않습니다.")

    new_mission = Mission(
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
        place_info={"placeId": body.place_id} if body.place_id else None,
    )
    old_mission.status = "replaced"
    db.add(new_mission)
    try:
        db.flush()
        for index, step in enumerate(recommendation.steps, start=1):
            db.add(MissionStep(
                id=uuid4(),
                mission_id=new_mission.id,
                step_order=index,
                text=step["text"],
                is_completed=False,
            ))
        db.commit()
    except (KeyError, TypeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="대체 추천 단계 정보를 처리할 수 없습니다.") from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="대체 미션을 저장할 수 없습니다.") from exc

    return ReplaceOut(oldMissionId=old_mission.id, newMissionId=new_mission.id)
