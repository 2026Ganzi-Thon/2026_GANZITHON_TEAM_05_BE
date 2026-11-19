from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = 'sqlite:///./letit_local.db'
    jwt_secret_key: str = 'development-only-change-this-secret-before-deploying'
    jwt_expire_minutes: int = 1440
    cors_origins: str = 'http://localhost:5173'

    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    @property
    def allowed_origins(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(',') if x.strip()]


@lru_cache

@lru_cache
def get_settings() -> Settings:
    return Settings()
