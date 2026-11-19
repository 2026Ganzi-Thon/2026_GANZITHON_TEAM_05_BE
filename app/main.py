from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from app.config import get_settings
from app.database import engine
from app.routers import auth, users, records, collection

app = FastAPI(title='Letit Backend API', version='0.1.0', description='26개 중 인증 2개/기본값 2개 구현; 나머지는 개발 예정')
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
app.include_router(records.router, prefix="/api/v1")
app.include_router(collection.router, prefix="/api/v1")
