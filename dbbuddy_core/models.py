from dataclasses import dataclass
from typing import TYPE_CHECKING

from dbbuddy_core.dialects.engine import DatabaseEngine

if TYPE_CHECKING:
    from dbbuddy_core.ai_providers import ProviderRuntimeConfig


@dataclass
class DBConfig:
    host: str
    user: str
    password: str
    database: str
    port: int | None = None
    # A namespace within the database, for engines that have one. PostgreSQL
    # tables outside ``public`` are invisible without it; MySQL has no such level
    # and ignores it. None means "whatever the connection's own search path says",
    # which is the previous behaviour.
    db_schema: str | None = None
    engine: DatabaseEngine = DatabaseEngine.MYSQL
    ai: bool = False
    ai_provider: str = "local"
    fallback_provider: str = "nemotron"
    mapping_plugin: str = "default_mapping"
    # Resolved, self-contained provider chain (active provider first, then its
    # fallbacks). When set, the AI labeling path uses the provider registry with
    # these instead of the legacy ``ai_provider`` string. The backend builds it
    # from the caller's org provider records; the engine never touches the app DB.
    ai_provider_chain: "list[ProviderRuntimeConfig] | None" = None
