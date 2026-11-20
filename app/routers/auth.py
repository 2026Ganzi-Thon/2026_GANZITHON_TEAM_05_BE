from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.schemas import SignupIn, LoginIn, TokenOut, UserOut
from app.security import hash_password, verify_password, create_token
from app.rate_limit import limiter

router = APIRouter(prefix='/auth', tags=['인증'])


@router.post('/signup', response_model=TokenOut, status_code=201)
@limiter.limit("10/hour")
def signup(request: Request, payload: SignupIn, db: Session = Depends(get_db)):
    if payload.password != payload.passwordConfirm:
        raise HTTPException(status_code=400, detail='비밀번호 확인이 일치하지 않습니다.')
    user = User(username=payload.username, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail='이미 존재하는 아이디입니다.')
    db.refresh(user)
    return TokenOut(accessToken=create_token(user.id), user=UserOut(userId=user.id, username=user.username))


@router.post('/login', response_model=TokenOut)
@limiter.limit("10/minute")
def login(request: Request, payload: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail='아이디 또는 비밀번호가 올바르지 않습니다.')
    return TokenOut(accessToken=create_token(user.id), user=UserOut(userId=user.id, username=user.username))
