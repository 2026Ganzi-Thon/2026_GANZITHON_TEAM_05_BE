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


# LETIT_KAKAO_DIVERSIFIED_V2
# Keep the /nearby request and response contract unchanged.
# A Kakao request yields <=15 results; fetch more candidates and return <=15.
# Radii at 2000m and 5000m use directional searches to avoid nearest-only saturation.
def _offset_coordinates(latitude: float, longitude: float, north_m: float, east_m: float) -> tuple[float, float]:
    lat = latitude + north_m / 111_320
    longitude_scale = max(abs(math.cos(math.radians(latitude))), 0.01)
    lon = longitude + east_m / (111_320 * longitude_scale)
    return lat, lon


def _fetch_kakao_page(
    *, category: str, latitude: float, longitude: float,
    radius_meters: int, api_key: str, page: int,
) -> tuple[list[dict], bool]:
    common_params = {
        "x": longitude,
        "y": latitude,
        "radius": radius_meters,
        "sort": "distance",
        "size": MAX_RESULTS,
        "page": page,
    }
    if category == "park":
        endpoint = f"{KAKAO_LOCAL_BASE_URL}/keyword.json"
        params = {**common_params, "query": "공원"}
    else:
        endpoint = f"{KAKAO_LOCAL_BASE_URL}/category.json"
        params = {**common_params, "category_group_code": KAKAO_CATEGORY_CODES[category]}
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
    return documents, bool(payload.get("meta", {}).get("is_end", True))


def _search_kakao_local(
    *, category: str, latitude: float, longitude: float,
    radius_meters: int, api_key: str,
) -> list[dict]:
    if radius_meters == 0:
        return []

    # Always test the original origin first. If it fails, preserve the 503 contract.
    first, is_end = _fetch_kakao_page(
        category=category, latitude=latitude, longitude=longitude,
        radius_meters=radius_meters, api_key=api_key, page=1,
    )
    candidates = list(first)
    jobs = []
    if not is_end:
        jobs.extend((latitude, longitude, radius_meters, p) for p in (2, 3))

    # A wider search from the user's location alone may return only nearby shops.
    # Search four outward sectors, then filter by distance from the ORIGINAL user.
    if radius_meters >= 2_000:
        shift = round(radius_meters * 0.58)
        sector_radius = max(100, round(radius_meters * 0.53))
        for north, east in ((shift, 0), (-shift, 0), (0, shift), (0, -shift)):
            lat, lon = _offset_coordinates(latitude, longitude, north, east)
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                jobs.append((lat, lon, sector_radius, 1))

    # Extra result pages and sectors are best-effort; first page is mandatory.
    if jobs:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(
                _fetch_kakao_page, category=category, latitude=lat,
                longitude=lon, radius_meters=radius, api_key=api_key, page=page,
            ) for lat, lon, radius, page in jobs]
            for future in as_completed(futures):
                try:
                    documents, _ = future.result()
                    candidates.extend(documents)
                except HTTPException:
                    continue
    return candidates


def _select_places(
    documents: list[dict], latitude: float, longitude: float,
    radius_meters: int,
) -> list[NearbyPlace]:
    by_id: dict[str, NearbyPlace] = {}
    for doc in documents:
        try:
            lat = float(doc["y"])
            lon = float(doc["x"])
            if not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            # Always measure from the actual user, not shifted query origins.
            distance = _distance_meters(latitude, longitude, lat, lon)
            if distance > radius_meters:
                continue
            identifier = str(doc["id"])
            if identifier not in by_id:
                by_id[identifier] = NearbyPlace(
                    placeId=identifier,
                    name=doc["place_name"],
                    address=doc.get("road_address_name") or doc.get("address_name") or "",
                    latitude=lat, longitude=lon, distanceMeters=distance,
                    walkingMinutes=math.ceil(distance / STRAIGHT_LINE_METERS_PER_WALKING_MINUTE),
                )
        except (KeyError, TypeError, ValueError):
            continue

    candidates = sorted(by_id.values(), key=lambda item: item.distanceMeters)
    # 3–4 stages should contain farther options, rather than just the nearest 15.
    min_preferred = {2_000: 800, 5_000: 2_000}.get(radius_meters, 0)
    if min_preferred:
        preferred = [p for p in candidates if p.distanceMeters >= min_preferred]
        other = [p for p in candidates if p.distanceMeters < min_preferred]
        return (preferred + other)[:MAX_RESULTS]
    return candidates[:MAX_RESULTS]


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
    items = _select_places(documents, latitude, longitude, radiusMeters)
    return NearbyPlacesResponse(items=items, searchRadiusMeters=radiusMeters)
