from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SignupIn(APIModel):
    username: str = Field(min_length=3, max_length=80, pattern=r'^[A-Za-z0-9_]+$')
    password: str = Field(min_length=8, max_length=128)
    passwordConfirm: str


class LoginIn(APIModel):
    username: str
    password: str


class UserOut(APIModel):
    userId: UUID
    username: str


class TokenOut(APIModel):
    accessToken: str
    tokenType: str = 'bearer'
    user: UserOut


class DefaultsIn(APIModel):
    mood: Literal['lethargic','depressed','anxious','refresh','unsure'] | None = None
    energyLevel: int = Field(ge=0, le=4)
    barriers: list[Literal['tired','preparation','crowded','alone','cost','no_reason']] = Field(default_factory=list)
    distanceLimit: Literal['5min','10min','20min','any']
    timeLimit: Literal['10min','30min','60min','any']
    budgetLimit: Literal[0,5000,10000] | None


class DefaultsOut(DefaultsIn):
    onboardingCompleted: bool
