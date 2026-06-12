from dataclasses import dataclass


@dataclass
class DBConfig:
    host: str
    user: str
    password: str
    database: str
    ai: bool = False
    ai_provider: str = "local"
    fallback_provider: str = "nemotron"
    mapping_plugin: str = "default_mapping"
