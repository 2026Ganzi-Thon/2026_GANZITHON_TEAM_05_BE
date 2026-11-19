"""Mission recommendation endpoint backed by Upstage Solar Pro 4."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Recommendation, User
from app.security import current_user

router = APIRouter(prefix="/missions", tags=["미션 추천"])

Mood = Literal["lethargic", "depressed", "anxious", "refresh", "unsure"]
Barrier = Literal["tired", "preparation", "crowded", "alone", "cost", "no_reason"]
DistanceLimit = Literal["5min", "10min", "20min", "any"]
TimeLimit = Literal["10min", "30min", "60min", "any"]
PlaceCategory = Literal["convenience_store", "park", "cafe", "other"]


class MissionConditions(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mood: Mood | None = None
    energy_level: int = Field(alias="energyLevel", ge=0, le=4)
    barriers: list[Barrier] = Field(default_factory=list)
    distance_limit: DistanceLimit = Field(alias="distanceLimit")
    time_limit: TimeLimit = Field(alias="timeLimit")
    budget_limit: Literal[0, 5000, 10000] | None = Field(alias="budgetLimit")


class RecommendationsIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    conditions: MissionConditions
    exclude_recommendation_ids: list[UUID] = Field(
        alias="excludeRecommendationIds", max_length=50
    )


class RecommendationStepOut(BaseModel):
    step_id: UUID = Field(alias="stepId")
    order: int
    text: str


class RecommendationOut(BaseModel):
    recommendation_id: UUID = Field(alias="recommendationId")
    title: str
    description: str
    energy_level: int = Field(alias="energyLevel")
    mission_type: Literal["place", "non_place"] = Field(alias="missionType")
    place_category: PlaceCategory | None = Field(alias="placeCategory")
    steps: list[RecommendationStepOut] = Field(min_length=5, max_length=5)
    estimated_duration_minutes: int = Field(alias="estimatedDurationMinutes")
    estimated_cost: int = Field(alias="estimatedCost")


class RecommendationsOut(BaseModel):
    fallback_used: bool = Field(alias="fallbackUsed")
    recommendations: list[RecommendationOut] = Field(min_length=2, max_length=2)


class _UpstageSettings(BaseSettings):
    upstage_api_key: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache(maxsize=1)
def _get_upstage_api_key() -> str | None:
    return _UpstageSettings().upstage_api_key


def _fallback_recommendations(conditions: MissionConditions) -> list[dict]:
    """Small deterministic demo set used when AI is unavailable or malformed."""
    energy = conditions.energy_level
    if conditions.budget_limit == 0:
        ideas = [
            ("집 앞 공기 마시기", "현관 밖에서 짧게 바깥 공기를 느껴봐요", "non_place", None, 5, 0,
             ["편한 옷을 입어요", "신발을 신어요", "현관문을 열어요", "건물 밖에 잠깐 서 있어요", "천천히 돌아와요"]),
            ("익숙한 길 짧게 걷기", "집 근처 익숙한 길을 천천히 걸어요", "non_place", None, 10, 0,
             ["편한 신발을 신어요", "집 밖으로 나와요", "익숙한 길을 골라요", "가능한 만큼 천천히 걸어요", "편하게 돌아와요"]),
        ]
    elif energy <= 1:
        ideas = [
            ("집 앞 공기 마시기", "현관 밖에서 짧게 바깥 공기를 느껴봐요", "non_place", None, 5, 0,
             ["편한 옷을 입어요", "신발을 신어요", "현관문을 열어요", "건물 밖에 잠깐 서 있어요", "천천히 돌아와요"]),
            ("가까운 편의점 다녀오기", "부담 없는 거리의 편의점에 다녀와요", "place", "convenience_store", 10, 0,
             ["외출 준비를 해요", "가까운 편의점을 찾아요", "천천히 걸어가요", "필요하면 물 한 병을 사요", "편한 길로 돌아와요"]),
        ]
    else:
        ideas = [
            ("동네 한 바퀴 걷기", "무리하지 않는 속도로 주변을 걸어요", "non_place", None, 15, 0,
             ["걷기 편한 신발을 신어요", "집 밖으로 나와요", "익숙한 길을 걸어요", "잠깐 멈춰 주변을 둘러봐요", "천천히 돌아와요"]),
            ("조용한 카페에서 쉬기", "가까운 카페에서 잠깐 쉬어가요", "place", "cafe", 25, 5000,
             ["외출 준비를 해요", "가까운 카페를 찾아요", "편한 길로 이동해요", "음료를 주문하거나 자리에 앉아요", "쉬고 나서 돌아와요"]),
        ]

    time_cap = {"10min": 10, "30min": 30, "60min": 60}.get(conditions.time_limit)
    result = []
    for title, description, mission_type, category, minutes, cost, step_texts in ideas:
        if time_cap is not None:
            minutes = min(minutes, time_cap)
        if conditions.budget_limit is not None:
            cost = min(cost, conditions.budget_limit)
        result.append({
            "title": title,
            "description": description,
            "energyLevel": energy,
            "missionType": mission_type,
            "placeCategory": category,
            "steps": [{"order": i, "text": text} for i, text in enumerate(step_texts, 1)],
            "estimatedDurationMinutes": minutes,
            "estimatedCost": cost,
        })
    return result


def _request_upstage(
    conditions: MissionConditions,
    excluded_titles: list[str],
    additional_instructions: str | None = None,
) -> list[dict]:
    """Call Upstage's OpenAI-compatible Chat Completions API without extra packages."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    api_key = _get_upstage_api_key()
    if not api_key:
        raise RuntimeError("UPSTAGE_API_KEY is not configured")

    prompt_data = conditions.model_dump(by_alias=True)
    prompt = (
        "다음 조건에 맞는 부담이 적고 안전한 일상 미션을 정확히 2개 만들어 주세요. "
        "한국어로 작성하고, 선택한 예산과 시간 범위를 넘지 마세요. "
        "장소를 특정해야 하는 미션은 missionType=place와 placeCategory를 지정하고, "
        "그 외에는 missionType=non_place, placeCategory=null로 작성하세요. "
        "각 미션의 steps는 실행 가능한 순서로 정확히 5개 작성하세요. "
        "제외할 기존 미션과 제목이 겹치지 않게 하세요. 설명이나 마크다운 없이 JSON 객체만 반환하세요.\n"
        '형식: {"recommendations":[{"title":"...","description":"...",'
        '"energyLevel":0,"missionType":"non_place","placeCategory":null,'
        '"steps":[{"order":1,"text":"..."}],'
        '"estimatedDurationMinutes":10,"estimatedCost":0}]}\n'
        f"조건: {json.dumps(prompt_data, ensure_ascii=False)}\n"
        f"추가 요청: {additional_instructions or '없음'}\n"
        f"제외할 미션 제목: {json.dumps(excluded_titles, ensure_ascii=False)}"
    )
    payload = json.dumps({
        "model": "solar-pro4",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 1800,
    }).encode("utf-8")
    request = Request(
        "https://api.upstage.ai/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Upstage request failed") from exc

    content = response_data["choices"][0]["message"]["content"]
    content = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content.strip(), flags=re.IGNORECASE)
    parsed = json.loads(content)
    recommendations = parsed.get("recommendations")
    if not isinstance(recommendations, list) or len(recommendations) != 2:
        raise ValueError("Upstage response must contain two recommendations")
    return recommendations


@router.post("/recommendations", response_model=RecommendationsOut)
def create_recommendations(
    body: RecommendationsIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> RecommendationsOut:
    """Generate two personalized recommendations and persist them for this user."""
    excluded_ids = set(body.exclude_recommendation_ids)
    excluded_rows = (
        db.query(Recommendation)
        .filter(Recommendation.user_id == user.id, Recommendation.id.in_(excluded_ids))
        .all()
        if excluded_ids else []
    )
    excluded_titles = [row.title for row in excluded_rows]

    fallback_used = False
    try:
        candidates = _request_upstage(body.conditions, excluded_titles)
        # Validate model-controlled fields before they are persisted.
        validated = [RecommendationOut.model_validate({
            **candidate,
            "recommendationId": uuid4(),
            "steps": [
                {**step, "stepId": uuid4()}
                for step in candidate["steps"]
            ],
        }) for candidate in candidates]
        for item in validated:
            if item.energy_level != body.conditions.energy_level:
                raise ValueError("AI recommendation energy level does not match input")
            if (item.mission_type == "place") != (item.place_category is not None):
                raise ValueError("AI recommendation place type/category mismatch")
            if item.estimated_duration_minutes <= 0 or item.estimated_cost < 0:
                raise ValueError("AI recommendation estimates are invalid")
            if body.conditions.time_limit != "any":
                time_cap = {"10min": 10, "30min": 30, "60min": 60}[body.conditions.time_limit]
                if item.estimated_duration_minutes > time_cap:
                    raise ValueError("AI recommendation exceeds time limit")
            if body.conditions.budget_limit is not None and item.estimated_cost > body.conditions.budget_limit:
                raise ValueError("AI recommendation exceeds budget")
    except (RuntimeError, ValueError, KeyError, TypeError, ValidationError, json.JSONDecodeError):
        fallback_used = True
        validated = [RecommendationOut.model_validate({
            **candidate,
            "recommendationId": uuid4(),
            "steps": [
                {**step, "stepId": uuid4()}
                for step in candidate["steps"]
            ],
        }) for candidate in _fallback_recommendations(body.conditions)]

    # Persist only to the signed-in user's rows; shared models/schema are not modified.
    try:
        for item in validated:
            db.add(Recommendation(
                id=item.recommendation_id,
                user_id=user.id,
                title=item.title,
                description=item.description,
                energy_level=item.energy_level,
                mission_type=item.mission_type,
                place_category=item.place_category,
                steps=[step.model_dump(by_alias=True) for step in item.steps],
                conditions=body.conditions.model_dump(by_alias=True),
                estimated_duration_minutes=item.estimated_duration_minutes,
                estimated_cost=item.estimated_cost,
            ))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="추천을 저장할 수 없습니다.") from exc

    return RecommendationsOut(fallbackUsed=fallback_used, recommendations=validated)
