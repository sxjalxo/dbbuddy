"""Rich schema metadata models.

The rest of the pipeline still uses the lightweight ``{table: [col, ...]}``
dict returned by ``fetch_schema()``. These richer models are produced by
``Dialect.fetch_schema_rich()`` and open the door to smarter join inference,
type-aware query planning, and stronger validation.
"""

from dataclasses import dataclass, field


@dataclass
class ColumnMeta:
    name: str
    data_type: str
    nullable: bool = True
    is_primary_key: bool = False
    is_unique: bool = False
    default: str | None = None


@dataclass
class ForeignKeyMeta:
    column: str
    referenced_table: str
    referenced_column: str


@dataclass
class TableMeta:
    name: str
    columns: list[ColumnMeta] = field(default_factory=list)
    foreign_keys: list[ForeignKeyMeta] = field(default_factory=list)

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def primary_keys(self) -> list[str]:
        return [c.name for c in self.columns if c.is_primary_key]


@dataclass
class DatabaseSchema:
    tables: dict[str, TableMeta] = field(default_factory=dict)

    def to_simple(self) -> dict[str, list[str]]:
        """Convert to the lightweight ``{table: [col, ...]}`` format used by the pipeline."""
        return {name: t.column_names() for name, t in self.tables.items()}

    def table_names(self) -> list[str]:
        return list(self.tables.keys())
