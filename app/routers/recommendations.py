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
from app.models import Mission, Recommendation, User
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
    budget_limit: Literal[0, 10000, 50000] | None = Field(alias="budgetLimit")


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
    """Budget-aware backup missions, preserving the user's energy scope.

    Estimated costs are illustrative and not verified venue prices. Low-energy
    levels remain free if a paid option would require leaving the allowed area.
    """
    energy = conditions.energy_level
    budget = conditions.budget_limit

    # Each option: title, description, missionType, placeCategory,
    # duration in minutes, estimated cost, five concrete steps.
    free = {
        0: [
            ("집 안에서 가볍게 몸 풀기", "집 안에서 편안하게 몸을 움직여 보세요.", "non_place", None, 5, 0,
             ["집 안의 편한 자리를 찾아요", "어깨를 천천히 돌려요", "목을 부드럽게 움직여요", "양팔을 가볍게 뻗어요", "잠시 쉬며 마무리해요"]),
            ("집 안에서 창밖 감상하기", "창밖 풍경을 보며 마음을 환기해 보세요.", "non_place", None, 5, 0,
             ["집 안에서 창가를 찾아요", "창밖의 풍경을 바라봐요", "보이는 색 세 가지를 찾아요", "깊게 숨을 쉬어요", "편안히 마무리해요"]),
        ],
        1: [
            ("집 앞에서 바람 쐬기", "건물 밖 집 앞에서 바깥 공기를 느껴 보세요.", "non_place", None, 5, 0,
             ["편한 신발을 신어요", "건물 밖으로 나가요", "집 앞 안전한 곳에 서요", "바람과 주변 소리에 집중해요", "집으로 돌아와요"]),
            ("집 앞에서 풍경 찾기", "집 바로 앞에서 작은 변화를 찾아보세요.", "non_place", None, 5, 0,
             ["가볍게 외출 준비를 해요", "집 앞 건물 밖으로 나가요", "주변을 천천히 살펴봐요", "마음에 드는 풍경을 하나 찾아요", "집으로 돌아와요"]),
        ],
        2: [
            ("동네 골목 산책하기", "집 근처 동네를 천천히 걸어보세요.", "non_place", None, 10, 0,
             ["신발을 신어요", "건물 밖으로 나가요", "집 근처 골목을 걸어요", "주변 풍경을 살펴봐요", "집으로 돌아와요"]),
            ("동네 공원 둘러보기", "집 근처 공원에서 잠깐 쉬어 보세요.", "place", "park", 15, 0,
             ["가까운 동네 공원을 찾아요", "외출 준비를 해요", "공원에 걸어가요", "공원에서 잠시 쉬어요", "집으로 돌아와요"]),
        ],
        3: [
            ("가까운 공원 산책하기", "가까운 공원에서 여유롭게 걸어보세요.", "place", "park", 20, 0,
             ["근처 공원을 선택해요", "외출 준비를 해요", "공원까지 이동해요", "산책로를 잠깐 걸어요", "집으로 돌아와요"]),
            ("가까운 카페 주변 걷기", "가까운 카페 근처 풍경을 즐겨 보세요.", "place", "cafe", 20, 0,
             ["가까운 카페를 찾아요", "외출 준비를 해요", "카페까지 이동해요", "카페 주변에서 잠시 쉬어요", "집으로 돌아와요"]),
        ],
        4: [
            ("조금 먼 공원 탐방하기", "조금 떨어진 공원에서 풍경을 감상해요.", "place", "park", 30, 0,
             ["조금 먼 공원을 골라요", "외출 준비를 해요", "공원까지 이동해요", "공원에서 천천히 걸어요", "집으로 돌아와요"]),
            ("새로운 동네 카페 찾아가기", "평소와 다른 동네를 가볍게 탐방해요.", "place", "cafe", 30, 0,
             ["조금 먼 카페를 찾아요", "외출 준비를 해요", "카페까지 이동해요", "카페 주변을 둘러봐요", "집으로 돌아와요"]),
        ],
    }

    # Never force an expensive activity that violates the mobility scope.
    # The listed amounts are estimates, not verified menu or admission prices.
    priced = {
        10_000: {
            2: [
                ("동네 카페에서 음료 즐기기", "동네 카페에서 좋아하는 음료로 쉬어가요.", "place", "cafe", 20, 6_000,
                 ["집 근처 카페를 찾아요", "집 밖으로 나가요", "카페까지 걸어가요", "예산 내에서 음료를 골라요", "잠시 쉬고 돌아와요"]),
                ("동네 편의점에서 간식 고르기", "동네 편의점에서 작은 간식을 골라봐요.", "place", "convenience_store", 15, 4_000,
                 ["가까운 편의점을 찾아요", "집 밖으로 나가요", "편의점까지 걸어가요", "가격을 보고 간식을 골라요", "집으로 돌아와요"]),
            ],
            3: [
                ("가까운 카페에서 쉬기", "카페에서 좋아하는 음료와 잠시 쉬어가요.", "place", "cafe", 25, 7_000,
                 ["가까운 카페를 골라요", "외출 준비를 해요", "카페까지 이동해요", "가격을 확인하고 음료를 골라요", "쉬다가 집으로 돌아와요"]),
                ("가까운 편의점에서 간식 찾기", "가까운 편의점에서 간단한 간식을 골라요.", "place", "convenience_store", 20, 5_000,
                 ["가까운 편의점을 찾아요", "외출 준비를 해요", "편의점으로 이동해요", "가격을 확인하고 간식을 골라요", "집으로 돌아와요"]),
            ],
            4: [
                ("새로운 동네 카페 방문하기", "조금 먼 카페에서 분위기를 바꿔 보세요.", "place", "cafe", 35, 8_000,
                 ["조금 먼 카페를 찾아요", "외출 준비를 해요", "카페까지 이동해요", "가격을 보고 음료를 골라요", "집으로 돌아와요"]),
                ("조금 먼 공원과 간식 즐기기", "새로운 공원에서 간식과 함께 쉬어가요.", "place", "park", 35, 6_000,
                 ["조금 먼 공원을 골라요", "간식을 준비해요", "공원까지 이동해요", "간식을 먹으며 풍경을 즐겨요", "주변을 정리하고 돌아와요"]),
            ],
        },
        50_000: {
            2: [
                ("동네 카페에서 디저트 즐기기", "동네 카페에서 음료와 디저트를 즐겨요.", "place", "cafe", 30, 15_000,
                 ["집 근처 카페를 찾아요", "외출 준비를 해요", "카페까지 걸어가요", "예산 내에서 음료와 디저트를 골라요", "쉬다가 집으로 돌아와요"]),
                ("동네 공원 피크닉하기", "집 근처 공원에서 간단한 피크닉을 해요.", "place", "park", 30, 18_000,
                 ["집 근처 공원을 찾아요", "예산 내에서 간식을 준비해요", "공원까지 걸어가요", "간식을 즐기며 쉬어요", "정리하고 집으로 돌아와요"]),
            ],
            3: [
                ("가까운 카페에서 디저트 타임", "카페에서 취향에 맞는 디저트를 즐겨요.", "place", "cafe", 40, 25_000,
                 ["가까운 카페를 찾아요", "외출 준비를 해요", "카페까지 이동해요", "메뉴 가격을 보고 디저트를 골라요", "쉬고 집으로 돌아와요"]),
                ("가까운 공원에서 피크닉", "가까운 공원에서 간식을 즐기며 쉬어가요.", "place", "park", 40, 20_000,
                 ["가까운 공원을 찾아요", "간식을 미리 준비해요", "공원까지 이동해요", "간식과 함께 쉬어요", "정리한 뒤 돌아와요"]),
            ],
            4: [
                ("새로운 동네 카페 탐방하기", "조금 먼 카페에서 음료와 디저트를 즐겨요.", "place", "cafe", 45, 30_000,
                 ["조금 먼 카페를 찾아요", "외출 준비를 해요", "카페까지 이동해요", "예산 내에서 메뉴를 골라요", "쉬다가 집으로 돌아와요"]),
                ("조금 먼 공원 피크닉하기", "낯선 공원에서 여유롭게 쉬어가요.", "place", "park", 45, 25_000,
                 ["조금 먼 공원을 찾아요", "예산 내에서 간식을 준비해요", "공원까지 이동해요", "피크닉하며 풍경을 즐겨요", "정리하고 돌아와요"]),
            ],
        },
        None: {
            2: [
                ("동네 카페에서 디저트 즐기기", "동네 카페에서 작은 여유를 즐겨 보세요.", "place", "cafe", 30, 15_000,
                 ["집 근처 카페를 찾아요", "외출 준비를 해요", "카페까지 걸어가요", "메뉴 가격을 확인해요", "음료와 디저트를 즐겨요"]),
                ("동네 공원 피크닉하기", "집 근처 공원에서 가볍게 쉬어가요.", "place", "park", 30, 18_000,
                 ["집 근처 공원을 골라요", "간식을 준비해요", "공원까지 걸어가요", "간식을 즐겨요", "정리하고 집으로 돌아와요"]),
            ],
            3: [
                ("가까운 카페 디저트 코스", "가까운 카페에서 여러 디저트를 골라봐요.", "place", "cafe", 45, 35_000,
                 ["가까운 카페를 골라요", "외출 준비를 해요", "카페까지 이동해요", "메뉴 가격을 보고 골라요", "쉬다가 돌아와요"]),
                ("가까운 공원 피크닉하기", "공원에서 간식과 풍경을 함께 즐겨요.", "place", "park", 40, 22_000,
                 ["가까운 공원을 선택해요", "원하는 간식을 준비해요", "공원까지 이동해요", "간식을 먹으며 쉬어요", "정리하고 돌아와요"]),
            ],
            4: [
                ("조금 먼 카페에서 여유 즐기기", "새로운 동네 카페에서 느긋하게 쉬어가요.", "place", "cafe", 50, 45_000,
                 ["조금 먼 카페를 찾아요", "외출 준비를 해요", "카페로 이동해요", "메뉴 가격을 확인하고 골라요", "쉬다가 집으로 돌아와요"]),
                ("조금 먼 공원 피크닉하기", "새로운 공원에서 작은 피크닉을 즐겨요.", "place", "park", 50, 32_000,
                 ["조금 먼 공원을 찾아요", "원하는 간식을 준비해요", "공원까지 이동해요", "간식과 풍경을 즐겨요", "정리하고 돌아와요"]),
            ],
        },
    }
    # For indoor / doorstep missions, purchased supplies may be used indoors.
    # Estimates are illustrative, not confirmed shop prices.
    if energy in (0, 1) and budget != 0:
        candidates = (
            [
                ("집 안에서 색연필로 그림 그리기", "간단한 미술 재료로 집 안에서 그림을 그려요.", 4000,
                 ["집 안에서 색연필을 준비해요", "종이를 펼쳐요", "좋아하는 풍경을 그려요", "색을 칠해요", "정리해요"]),
                ("집 안에서 작은 만들기", "간단한 만들기 재료로 집 안에서 쉬어가요.", 7000,
                 ["집 안에서 재료를 준비해요", "설명서를 읽어요", "조립을 시작해요", "완성해요", "정리해요"]),
            ] if energy == 0 else [
                ("집 앞에서 음료 즐기기", "준비한 음료를 들고 건물 밖에서 쉬어요.", 5000,
                 ["예산 내 음료를 준비해요", "건물 밖으로 나가요", "집 앞에서 마셔요", "잠깐 쉬어요", "집으로 돌아와요"]),
                ("집 앞에서 간식 먹기", "간식을 준비해 집 앞에서 바람을 쐬어요.", 8000,
                 ["간식을 준비해요", "건물 밖으로 나가요", "집 앞에서 간식을 즐겨요", "주변을 둘러봐요", "집으로 돌아와요"]),
            ]
        )
        if budget in (50_000, None):
            if energy == 0:
                candidates = [("집 안에서 취미 키트 만들기", "집 안에서 취미 키트를 활용해 만들기를 해요.", 18_000,
                               ["취미 키트를 준비해요", "집 안에서 작업 공간을 정해요", "설명서를 읽어요", "만들기를 진행해요", "재료를 정리해요"])]
            else:
                candidates = [("집 앞에서 도시락 즐기기", "준비한 도시락과 음료를 집 앞에서 즐겨요.", 15_000,
                               ["도시락과 음료를 준비해요", "건물 밖으로 나가요", "집 앞 안전한 곳에 앉아요", "식사를 즐겨요", "주변을 정리하고 돌아와요"])]
        choices = [
            (title, desc, "non_place", None, 15, cost, steps)
            for title, desc, cost, steps in candidates
        ]
        if budget in (50_000, None):
            choices.append(free[energy][0])
    else:
        choices = priced.get(budget, {}).get(energy, free[energy])
    time_cap = {"10min": 10, "30min": 30, "60min": 60}.get(conditions.time_limit)
    result = []
    for title, description, mission_type, category, minutes, cost, steps in choices:
        result.append({
            "title": title,
            "description": description,
            "energyLevel": energy,
            "missionType": mission_type,
            "placeCategory": category,
            "steps": [{"order": n, "text": step} for n, step in enumerate(steps, start=1)],
            "estimatedDurationMinutes": min(minutes, time_cap) if time_cap else minutes,
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
    # LETIT_STRICT_BUDGET_V3: concise, non-redundant AI prompt.
    prompt = (
        "한국어 일상 미션 2개를 서로 다르게 추천하세요. JSON 객체만 반환하세요. "
        "최근 수행 활동과 제목뿐 아니라 핵심 행동이 같은 추천은 피하세요. "
        "두 미션의 활동 목적과 행동을 명확히 다르게 구성하세요. "
        f"이동 범위는 energyLevel만 따릅니다: {ENERGY_SCOPE_RULES[conditions.energy_level]} "
        "0단계는 집 안 전용(non_place), 1~2단계는 외출(place 또는 non_place), "
        "3~4단계는 외부 장소 방문(place)입니다. "
        "0단계에서 두 미션 모두 베란다·마당 등 특수 시설을 요구하지 마세요. "
        "기분·장벽·시간은 범위 내 활동 선택에 활용하고 거리 선택보다 에너지 범위를 우선하세요. "
        "예산 규칙(estimatedCost, 원): 0은 둘 다 0원, 10000은 둘 다 1~10000원, "
        "50000은 둘 다 0~50000원이되 하나 이상은 10000~50000원, "
        "null은 둘 다 0~100000원이되 하나 이상은 10000~100000원입니다. "
        "비용은 활동에 실제로 필요한 구매·이용료의 합리적인 추정치여야 하며 무료 활동의 가격을 임의로 높이지 마세요. "
        "이동 범위와 예산이 충돌하면 이동 범위를 우선하되 집 안 취미 재료처럼 수행 가능한 유료 활동을 고려하세요. "
        "실제 업장 가격·교통 시간을 확인한 것처럼 말하거나 특정 상호명을 지어내지 마세요. "
        "선택한 timeLimit와 예산 상한을 넘기지 마세요. "
        "title은 20자 이내, description은 45자 이내 한 문장으로 쓰고 거리 수치·정책·개발 용어를 노출하지 마세요. "
        "각 미션의 steps는 순서대로 정확히 5개이며 실제 수행 가능한 내용이어야 합니다. "
        "placeCategory는 convenience_store, park, cafe, other 중 하나만 사용하고 "
        "missionType=non_place일 때는 null로 쓰세요. "
        "기존 제목을 반복하지 마세요. 다음 스키마의 필드를 모두 포함하세요.\n"
        '{"recommendations":[{"title":"...","description":"...",'
        '"energyLevel":3,"missionType":"place","placeCategory":"cafe",'
        '"steps":[{"order":1,"text":"..."},{"order":2,"text":"..."},'
        '{"order":3,"text":"..."},{"order":4,"text":"..."},'
        '{"order":5,"text":"..."}],"estimatedDurationMinutes":30,"estimatedCost":15000}]}\n'
        f"조건: {json.dumps(prompt_data, ensure_ascii=False)}\n"
        f"추가 요청: {additional_instructions or '없음'}\n"
        f"최근 수행 활동과 제외할 추천: {json.dumps(excluded_titles, ensure_ascii=False)}"
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


def _normalize_ai_place_category(candidate: dict) -> dict:
    """Convert only unambiguous AI category aliases to contract values."""
    value = candidate.get("placeCategory")
    if not isinstance(value, str):
        return candidate
    aliases = {
        "cafe": "cafe",
        "coffee_shop": "cafe",
        "coffee shop": "cafe",
        "coffeehouse": "cafe",
        "카페": "cafe",
        "park": "park",
        "public_park": "park",
        "공원": "park",
        "convenience_store": "convenience_store",
        "convenience-store": "convenience_store",
        "convenience store": "convenience_store",
        "편의점": "convenience_store",
        "other": "other",
    }
    normalized = aliases.get(value.strip().lower())
    if normalized is None or normalized == value:
        return candidate
    return {**candidate, "placeCategory": normalized}


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

    # LETIT_RECENT_MISSIONS_V1: user's 10 most recent actually started missions.
    # Use titles + brief descriptions to avoid rephrasing the same activity.
    recent_missions = (
        db.query(Mission)
        .filter(Mission.user_id == user.id)
        .order_by(Mission.started_at.desc(), Mission.id.desc())
        .limit(10)
        .all()
    )
    recent_context = [
        f"{m.title} - {m.description[:100]}" for m in recent_missions
    ]
    exclusion_context = excluded_titles + recent_context

    fallback_used = False
    try:
        candidates = _request_upstage(body.conditions, exclusion_context)
        candidates = [_normalize_ai_place_category(item) for item in candidates]
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
        costs = [item.estimated_cost for item in validated]
        budget = body.conditions.budget_limit
        if budget == 0 and any(cost != 0 for cost in costs):
            raise ValueError("budget 0 must be free")
        if budget == 10_000 and any(not 1 <= cost <= 10_000 for cost in costs):
            raise ValueError("budget 10000 requires paid activities")
        if budget == 50_000 and not any(10_000 <= cost <= 50_000 for cost in costs):
            raise ValueError("budget 50000 requires one mid-priced activity")
        if budget is None and (any(cost > 100_000 for cost in costs)
                               or not any(10_000 <= cost <= 100_000 for cost in costs)):
            raise ValueError("unlimited budget requires one 10000~100000 activity")
    except (RuntimeError, ValueError, KeyError, TypeError, ValidationError, json.JSONDecodeError) as exc:
        # Safe operational diagnostics: no user conditions, token, API key or raw response.
        if isinstance(exc, ValidationError):
            error_fields = [
                {
                    "field": ".".join(map(str, e["loc"])),
                    "type": e["type"],
                }
                for e in exc.errors()
            ]
            logger.warning("AI recommendation validation failed: %s", error_fields)

            if "candidates" in locals():
                categories = [
                    str(item.get("placeCategory"))[:50]
                    for item in candidates
                    if isinstance(item, dict)
                ]
                logger.warning("AI placeCategory values: %s", categories)
        else:
            logger.warning("AI recommendation fallback: %s", type(exc).__name__)
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
