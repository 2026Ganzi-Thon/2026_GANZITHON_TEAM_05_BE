from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from app.config import get_settings
from app.database import engine
from app.rate_limit import limiter
from app.routers import (
    auth,
    users,
    records,
    collection,
    alternatives,
    missions,
    places,
    recommendations,
)

app = FastAPI(title='Letit Backend API', version='0.1.0', description='26개 중 인증 2개/기본값 2개 구현; 나머지는 개발 예정')
app.state.limiter = limiter


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            'detail': '요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.',
            'code': 'RATE_LIMITED',
        },
    )


app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().allowed_origins,
                   allow_credentials=True, allow_methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS'],
                   allow_headers=['Authorization','Content-Type'])


@app.exception_handler(HTTPException)
async def api_error(request: Request, exc: HTTPException):
    code = {400: 'BAD_REQUEST', 401: 'UNAUTHORIZED', 403: 'FORBIDDEN',
            404: 'NOT_FOUND', 409: 'CONFLICT'}.get(exc.status_code, 'HTTP_ERROR')
    return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail, 'code': code}, headers=exc.headers)


@app.get('/health', tags=['시스템'])
def health():
    return {'status': 'ok'}


@app.get('/health/db', tags=['시스템'])
def health_db():
    try:
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
        return {'database': 'connected'}
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={'database':'unavailable'})


app.include_router(auth.router, prefix='/api/v1')
app.include_router(users.router, prefix='/api/v1')

# B 담당: 미션 추천·수행·장소
app.include_router(recommendations.router, prefix="/api/v1")
app.include_router(missions.router, prefix="/api/v1")
app.include_router(alternatives.router, prefix="/api/v1")
app.include_router(places.router, prefix="/api/v1")

# A 담당: 성찰·기록·도감
app.include_router(records.router, prefix="/api/v1")
app.include_router(collection.router, prefix="/api/v1")
