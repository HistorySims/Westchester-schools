from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    supabase_db_url: str = Field(default="", description="Postgres connection string")
    supabase_url: str = Field(default="", description="Supabase project URL")
    supabase_service_key: str = Field(default="", description="Supabase service-role key")
    anthropic_api_key: str = Field(default="", description="Anthropic API key for synthesis")
    voyage_api_key: str = Field(default="", description="Voyage AI API key for embeddings/rerank")

    loc_user_agent: str = Field(
        default=(
            "Herald/0.1 "
            "(mailto:timhartnett29@gmail.com; "
            "+https://github.com/HistorySims/herald)"
        ),
        description=(
            "User-Agent sent to LOC / Chronicling America. LOC asks "
            "computational users to identify themselves; the polite form "
            "is `AppName/version (mailto:contact; +project-url)`."
        ),
    )


def load() -> Settings:
    return Settings()
