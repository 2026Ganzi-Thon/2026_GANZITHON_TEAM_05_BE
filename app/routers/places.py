"""Kakao Local-backed nearby place search."""

from functools import lru_cache
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import User
from app.security import current_user


router = APIRouter(prefix="/places", tags=["장소 선택"])

KAKAO_CATEGORY_CODES = {
    "convenience_store": "CS2",
    "cafe": "CE7",
    # Kakao Local has no park category group, so park searches use keyword search.
    # "other" is temporarily mapped to tourist attractions (AT4); confirm this
    # product mapping with the team before changing the shared API contract.
    "other": "AT4",
}
KAKAO_LOCAL_BASE_URL = "https://dapi.kakao.com/v2/local/search"
MAX_RADIUS_METERS = 20_000
MAX_RESULTS = 15
STRAIGHT_LINE_METERS_PER_WALKING_MINUTE = 67


class _KakaoSettings(BaseSettings):
    """Read the Kakao REST key from the process environment or local .env."""

    kakao_rest_api_key: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache(maxsize=1)
def _get_kakao_api_key() -> str | None:
    return _KakaoSettings().kakao_rest_api_key


class NearbyPlace(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    placeId: str
    name: str
    address: str
    latitude: float
    longitude: float
    distanceMeters: int
    walkingMinutes: int | None


class NearbyPlacesResponse(BaseModel):
    items: list[NearbyPlace]
    searchRadiusMeters: int


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Return straight-line distance, used when Kakao omits `distance`."""
    earth_radius_meters = 6_371_000
    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    return round(2 * earth_radius_meters * math.asin(math.sqrt(a)))


def _search_kakao_local(
    *,
    category: str,
    latitude: float,
    longitude: float,
    radius_meters: int,
    api_key: str,
) -> list[dict]:
    common_params = {
        "x": longitude,
        "y": latitude,
        "radius": radius_meters,
        "sort": "distance",
        "size": MAX_RESULTS,
    }

    if category == "park":
        endpoint = f"{KAKAO_LOCAL_BASE_URL}/keyword.json"
        params = {**common_params, "query": "공원"}
    else:
        endpoint = f"{KAKAO_LOCAL_BASE_URL}/category.json"
        params = {
            **common_params,
            "category_group_code": KAKAO_CATEGORY_CODES[category],
        }

    request = Request(
        f"{endpoint}?{urlencode(params)}",
        headers={"Authorization": f"KakaoAK {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=503, detail="장소 검색 서비스를 사용할 수 없습니다.")

    documents = payload.get("documents")
    if not isinstance(documents, list):
        raise HTTPException(status_code=503, detail="장소 검색 서비스 응답을 처리할 수 없습니다.")
    return documents


@router.get("/nearby", response_model=NearbyPlacesResponse)
def get_nearby_places(
    latitude: float = Query(..., description="현재 위치 위도"),
    longitude: float = Query(..., description="현재 위치 경도"),
    radiusMeters: int = Query(..., description="검색 반경(미터)"),
    category: str = Query(..., description="장소 분류"),
    _user: User = Depends(current_user),
) -> NearbyPlacesResponse:
    """Search nearby places and normalize Kakao Local results to the API contract."""
    allowed_categories = {"convenience_store", "park", "cafe", "other"}
    if category not in allowed_categories:
        raise HTTPException(status_code=400, detail="지원하지 않는 장소 분류입니다.")
    if not (math.isfinite(latitude) and -90 <= latitude <= 90):
        raise HTTPException(status_code=400, detail="위도 범위가 올바르지 않습니다.")
    if not (math.isfinite(longitude) and -180 <= longitude <= 180):
        raise HTTPException(status_code=400, detail="경도 범위가 올바르지 않습니다.")
    if not 0 <= radiusMeters <= MAX_RADIUS_METERS:
        raise HTTPException(status_code=400, detail="검색 반경은 0~20000m 범위여야 합니다.")

    api_key = _get_kakao_api_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="Kakao Local API 키가 설정되지 않았습니다.")

    documents = _search_kakao_local(
        category=category,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radiusMeters,
        api_key=api_key,
    )

    items: list[NearbyPlace] = []
    for document in documents:
        try:
            place_latitude = float(document["y"])
            place_longitude = float(document["x"])
            distance = document.get("distance")
            if distance in (None, ""):
                distance = _distance_meters(
                    latitude, longitude, place_latitude, place_longitude
                )
            distance_meters = int(float(distance))
            items.append(
                NearbyPlace(
                    placeId=str(document["id"]),
                    name=document["place_name"],
                    address=(document.get("road_address_name") or document.get("address_name") or ""),
                    latitude=place_latitude,
                    longitude=place_longitude,
                    distanceMeters=distance_meters,
                    walkingMinutes=math.ceil(
                        distance_meters / STRAIGHT_LINE_METERS_PER_WALKING_MINUTE
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            # Skip malformed individual rows without discarding valid results.
            continue

    return NearbyPlacesResponse(items=items, searchRadiusMeters=radiusMeters)
