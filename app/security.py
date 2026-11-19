from datetime import datetime, timedelta, timezone
from uuid import UUID
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database import get_db
from app.models import User

_hasher = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError):
        return False


def create_token(user_id: UUID) -> str:
    s = get_settings()
    payload = {'sub': str(user_id), 'exp': datetime.now(timezone.utc) + timedelta(minutes=s.jwt_expire_minutes)}
    return jwt.encode(payload, s.jwt_secret_key, algorithm='HS256')


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    error = HTTPException(status_code=401, detail='다시 로그인해 주세요.')
    if credentials is None:
        raise error
    try:
        payload = jwt.decode(credentials.credentials, get_settings().jwt_secret_key, algorithms=['HS256'])
        user_id = UUID(payload['sub'])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise error
    user = db.get(User, user_id)
    if user is None:
        raise error
    return user
