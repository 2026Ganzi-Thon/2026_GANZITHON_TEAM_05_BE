from functools import lru_cache
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = 'sqlite:///./letit_local.db'
    jwt_secret_key: str = Field(min_length=32)
    jwt_expire_minutes: int = 1440
    cors_origins: str = 'http://localhost:5173'

    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    @field_validator('jwt_secret_key')
    @classmethod
    def reject_placeholder_jwt_secret(cls, value: str) -> str:
        placeholders = {
            'development-only-change-this-secret-before-deploying',
            'REPLACE_WITH_LONG_RANDOM_SECRET',
        }
        if value in placeholders or len(value.encode('utf-8')) < 32:
            raise ValueError('Set JWT_SECRET_KEY to a private random value of at least 32 bytes.')
        return value

    @property
    def allowed_origins(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(',') if x.strip()]


@lru_cache

@lru_cache
def get_settings() -> Settings:
    return Settings()
