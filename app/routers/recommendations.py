"""Mission recommendation endpoint backed by Upstage Solar Pro 4."""

from __future__ import annotations

import json
import logging
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
logger = logging.getLogger(__name__)

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


# ENERGY_SCOPE_POLICY_V2: the energy level alone selects the travel radius.
# Radii are straight-line search bounds, not actual walking/transit travel times.
# 0=indoors, 1=within 200m, 2=within 800m, 3=within 2km, 4=within 5km.
# Mood, barriers and limits choose activities within the selected scope.
ENERGY_SCOPE_RULES = {
    0: "집 안에서만 수행. 현관문 밖으로 나가는 활동은 금지. 베란다나 테라스가 필요한 미션은 두 개 중 최대 하나만 허용.",
    1: "건물 밖으로 반드시 나가기. 집 바로 앞이나 근처에서 직선거리 200m 이내 외부 활동. 장소 방문도 가능.",
    2: "건물 밖으로 반드시 나가기. 동네 주변 직선거리 800m 이내에서 활동. 카페·공원·편의점 방문도 가능.",
    3: "집 밖에서 가까운 외부 장소에 가서 활동. 검색 반경 직선거리 2km 이내. 실내에서 끝나는 활동 금지.",
    4: "집 밖에서 더 먼 외부 장소를 방문해 활동. 검색 반경 직선거리 5km 이내. 실내에서 끝나는 활동 금지.",
}


# These checks catch clear indoor missions and incompatible structured fields.
# Semantic location constraints require integration tests with real AI responses.
def _scope_violation(item: RecommendationOut, energy: int) -> bool:
    main = f"{item.title} {item.description}"
    body = " ".join([main, *(step.text for step in item.steps)])
    indoor_only = ("집 안에서", "집안에서", "방 안에서", "거실에서", "침대에서",
                   "실내에서", "베란다에서", "창가에서", "창문 앞에서",
                   "집에서 스트레칭", "집에서 독서", "집에서 음악", "집에서 쉬기")
    outdoor = ("건물 밖", "현관 밖", "집 밖", "집 앞", "바깥", "산책", "공원",
               "편의점", "카페", "골목", "동네", "거리 걷", "외출", "방문", "걷기")
    if energy == 0:
        if item.mission_type != "non_place":
            return True
        # A few outdoor terms can appear as parts of indoor tasks ("창밖 풍경").
        outdoor_actions = ("건물 밖으로", "현관 밖으로", "집 밖으로", "밖으로 나가",
                           "산책하", "공원에 가", "편의점에 가", "카페에 가", "동네를 걷")
        return any(term in body for term in outdoor_actions) or not any(
            term in body for term in ("집 안", "집안", "거실", "방 안", "방에서", "실내", "집에서", "창가")
        )
    # Indoors as the mission itself is disallowed; preparation indoors is fine.
    if any(term in main for term in indoor_only):
        return True
    if energy in (3, 4) and item.mission_type != "place":
        return True
    if energy in (1, 2) and not any(term in body for term in outdoor):
        return True
    if energy == 1 and not any(term in body for term in ("집 앞", "건물 앞", "현관 밖", "집 바로", "가까운", "근처", "주변")):
        return True
    if energy == 2 and not any(term in body for term in ("집 근처", "동네", "주변", "근처", "가까운", "골목")):
        return True
    return False


def _special_facility_mission(item: RecommendationOut) -> bool:
    main = f"{item.title} {item.description}"
    return any(term in main for term in ("베란다", "발코니", "테라스", "마당", "옥상"))


def _fallback_recommendations(conditions: MissionConditions) -> list[dict]:
    """Two zero-cost, scope-compatible backup missions for each level."""
    energy = conditions.energy_level
    cases = {
        0: [
            ("집 안에서 가볍게 몸 풀기", "집 안의 편안한 공간에서 부담 없이 움직여요", "non_place", None, 5,
             ["집 안에서 편하게 서거나 앉아요", "어깨를 천천히 돌려요", "목을 부드럽게 움직여요", "양팔을 가볍게 늘려요", "편하게 쉬어요"]),
            ("집 안에서 마음 환기하기", "집 안에서 창밖을 바라보며 잠시 쉬어요", "non_place", None, 5,
             ["집 안의 편한 자리를 찾아요", "창문 너머를 천천히 바라봐요", "눈에 들어오는 색 세 가지를 찾아요", "깊게 숨을 쉬어요", "편안히 마무리해요"]),
        ],
        1: [
            ("건물 앞에서 바깥 공기 느끼기", "건물 밖으로 나가 집 바로 앞에서 잠깐 쉬어요", "non_place", None, 5,
             ["신발을 신어요", "건물 밖으로 나가요", "집 앞 안전한 곳에 서요", "바깥 공기를 느껴요", "집으로 돌아와요"]),
            ("집 앞에서 주변 풍경 살펴보기", "건물 밖 집 앞에서 새로운 풍경을 찾아봐요", "non_place", None, 5,
             ["가볍게 준비해요", "집 앞 건물 밖으로 나가요", "주변 풍경을 둘러봐요", "좋아 보이는 풍경 하나를 찾아요", "집으로 돌아와요"]),
        ],
        2: [
            ("동네 골목 짧게 산책하기", "집 근처 동네 골목에서 짧게 걸어요", "non_place", None, 10,
             ["신발을 신어요", "건물 밖으로 나가요", "집 근처 골목을 걸어요", "동네 풍경을 둘러봐요", "집으로 돌아와요"]),
            ("동네 가까운 공원 방문하기", "집 근처의 가까운 공원에 잠시 다녀와요", "place", "park", 10,
             ["외출 준비를 해요", "가까운 동네 공원을 찾아요", "공원에 걸어가요", "공원에서 잠시 쉬어요", "집으로 돌아와요"]),
        ],
        3: [
            ("가까운 공원 방문하기", "가까운 외부 장소인 공원에 다녀와요", "place", "park", 20,
             ["공원 위치를 확인해요", "외출 준비를 해요", "공원으로 이동해요", "공원에서 잠깐 쉬어요", "집으로 돌아와요"]),
            ("가까운 동네 카페 들러보기", "가까운 외부 장소인 카페에 들러보세요. 주문은 선택이에요", "place", "cafe", 20,
             ["카페를 선택해요", "외출 준비를 해요", "카페까지 이동해요", "카페 근처에서 잠시 쉬어요", "집으로 돌아와요"]),
        ],
        4: [
            ("조금 먼 공원 방문하기", "평소보다 조금 먼 외부 장소의 공원으로 나가요", "place", "park", 30,
             ["가볼 공원을 골라요", "외출 준비를 해요", "공원으로 이동해요", "공원에서 풍경을 바라봐요", "집으로 돌아와요"]),
            ("조금 먼 동네 카페 방문하기", "조금 떨어진 동네의 외부 장소인 카페에 가봐요. 주문은 선택이에요", "place", "cafe", 30,
             ["카페를 골라요", "외출 준비를 해요", "카페로 이동해요", "주변을 잠시 둘러봐요", "집으로 돌아와요"]),
        ],
    }
    time_cap = {"10min": 10, "30min": 30, "60min": 60}.get(conditions.time_limit)
    results = []
    for title, description, mission_type, category, minutes, texts in cases[energy]:
        results.append({
            "title": title,
            "description": description,
            "energyLevel": energy,
            "missionType": mission_type,
            "placeCategory": category,
            "steps": [{"order": idx, "text": step} for idx, step in enumerate(texts, 1)],
            "estimatedDurationMinutes": min(minutes, time_cap) if time_cap else minutes,
            "estimatedCost": 0,
        })
    return results


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
        "절대 규칙: 외출 장소와 이동 범위는 energyLevel 하나로만 결정합니다. "
        f"energyLevel={conditions.energy_level}: {ENERGY_SCOPE_RULES[conditions.energy_level]} "
        "0단계는 missionType=non_place이며 1~2단계는 place와 non_place 모두 허용됩니다. 3~4단계는 place입니다. "
        "1~4단계에서는 반드시 건물 밖에서 활동해야 합니다. 집 안 활동을 절대로 추천하지 마세요. "
        "0단계는 반드시 집 안 활동만 추천하고 두 추천 모두 베란다·테라스처럼 특정 시설을 요구하면 안 됩니다. "
        "mood, barriers, distanceLimit, timeLimit, budgetLimit는 정해진 이동 범위 안에서 "
        "활동의 종류와 난이도, 시간, 비용만 조정합니다. "
        "distanceLimit가 에너지 단계와 충돌하면 반드시 에너지 단계를 우선합니다. "
        "카카오 장소 검색 반경은 1단계 200m, 2단계 800m, 3단계 2km, 4단계 5km입니다. "
        "실제 교통·도보 소요시간을 보장하거나 특정 장소명을 지어내지 마세요. "
        "집 안·집 밖·집 앞·가까운 장소 등 위치를 제목과 설명, 단계에도 명확히 적으세요. "
        "한국어로 작성하고, 선택한 예산과 시간 범위를 넘지 마세요. "
        "장소를 특정해야 하는 미션은 missionType=place와 placeCategory를 지정하고, "
        "그 외에는 missionType=non_place, placeCategory=null로 작성하세요. "
        "각 미션의 steps는 실행 가능한 순서로 정확히 5개 작성하세요. "
        "제외할 기존 미션과 제목이 겹치지 않게 하세요. 설명이나 마크다운 없이 JSON 객체만 반환하세요.\n"
        '형식: {"recommendations":[{"title":"...","description":"...",'
        '"energyLevel":0,"missionType":"non_place","placeCategory":null,'
        '"steps":[{"order":1,"text":"준비해요"},{"order":2,"text":"이동해요"},'
        '{"order":3,"text":"활동해요"},{"order":4,"text":"주변을 살펴요"},'
        '{"order":5,"text":"마무리해요"}],'
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
        if body.conditions.energy_level == 0 and all(_special_facility_mission(item) for item in validated):
            raise ValueError("Both indoor missions need special housing facilities")
        for item in validated:
            if item.energy_level != body.conditions.energy_level:
                raise ValueError("AI recommendation energy level does not match input")
            if (item.mission_type == "place") != (item.place_category is not None):
                raise ValueError("AI recommendation place type/category mismatch")
            if _scope_violation(item, body.conditions.energy_level):
                raise ValueError("AI recommendation violates energy-level location scope")
            if item.estimated_duration_minutes <= 0 or item.estimated_cost < 0:
                raise ValueError("AI recommendation estimates are invalid")
            if body.conditions.time_limit != "any":
                time_cap = {"10min": 10, "30min": 30, "60min": 60}[body.conditions.time_limit]
                if item.estimated_duration_minutes > time_cap:
                    raise ValueError("AI recommendation exceeds time limit")
            if body.conditions.budget_limit is not None and item.estimated_cost > body.conditions.budget_limit:
                raise ValueError("AI recommendation exceeds budget")
    except (RuntimeError, ValueError, KeyError, TypeError, ValidationError, json.JSONDecodeError) as exc:
        # Safe operational diagnostics: no user conditions, token, API key or raw response.
        logger.warning(
            "AI recommendation fallback: %s %s",
            type(exc).__name__,
            str(exc)[:200] if type(exc) is ValueError else "(details omitted)",
        )
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
                steps=[
                    {**step.model_dump(by_alias=True), "stepId": str(step.step_id)}
                    for step in item.steps
                ],
                conditions=body.conditions.model_dump(by_alias=True),
                estimated_duration_minutes=item.estimated_duration_minutes,
                estimated_cost=item.estimated_cost,
            ))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="추천을 저장할 수 없습니다.") from exc

    return RecommendationsOut(fallbackUsed=fallback_used, recommendations=validated)
