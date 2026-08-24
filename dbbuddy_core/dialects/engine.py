"""Database engine identifier enum.

Using ``str, Enum`` means enum members compare equal to their string values,
serialize transparently to JSON, and are accepted wherever a plain string was
used before — so callers that haven't been updated yet still work.
"""

from enum import Enum


class DatabaseEngine(str, Enum):
    MYSQL = "mysql"
    POSTGRES = "postgresql"
    SQLSERVER = "sqlserver"

    # Make str(engine) / f"{engine}" yield the value ("postgresql"), not the
    # member name ("DatabaseEngine.POSTGRES"). Without this, an enum member and
    # the equivalent plain string stringify differently, which would fragment
    # any string-keyed cache (e.g. the per-database context/pool key).
    def __str__(self) -> str:
        return self.value

    def __format__(self, format_spec: str) -> str:
        return str.__format__(self.value, format_spec)

    @classmethod
    def normalize(cls, value: str) -> "DatabaseEngine":
        """Accept common shorthands ("postgres", "mssql") and return the canonical member."""
        _aliases = {
            "postgres": cls.POSTGRES,
            "mssql": cls.SQLSERVER,
            "sql server": cls.SQLSERVER,
            "sql_server": cls.SQLSERVER,
            "sqlserver": cls.SQLSERVER,
            "microsoft sql server": cls.SQLSERVER,
        }
        try:
            return cls(value.lower().strip())
        except ValueError:
            normalized = _aliases.get(value.lower().strip())
            if normalized is None:
                supported = [e.value for e in cls]
                raise ValueError(
                    f"Unsupported database engine: {value!r}. "
                    f"Supported: {supported}"
                ) from None
            return normalized
