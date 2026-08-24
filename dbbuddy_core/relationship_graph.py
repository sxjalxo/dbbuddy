"""Relationship Graph Builder for Multi-Table Queries.

This module builds a relationship graph from database schema to enable
graph-based join resolution without hardcoding.

Production-grade multi-table reasoning.
"""

from typing import Dict, List, Optional, Tuple

# {table: [(fk_column, referenced_table, referenced_column), ...]}
ForeignKeyMap = Dict[str, List[Tuple[str, str, str]]]


class JoinKeys(tuple):
    """``(from_col, to_col)`` that also remembers the *other* ways to make the join.

    A plain tuple everywhere it is consumed — ``left, right = keys`` still works,
    and anything that serialises the graph degrades it to a tuple and simply
    loses the alternates, falling back to today's behaviour. The extra state
    exists so role-playing dimensions survive graph construction: one fact table
    reaching one dimension through several foreign keys is the normal shape of a
    star schema, not an edge case.
    """

    # No __slots__: a tuple subclass cannot declare non-empty slots.

    def __new__(cls, from_col: str, to_col: str, alternates=None):
        self = super().__new__(cls, (from_col, to_col))
        self.alternates = list(alternates or [])
        return self


def _add_edge(graph, table: str, ref_table: str, col: str, ref_col: str) -> None:
    existing = graph[table].get(ref_table)
    if existing is None:
        graph[table][ref_table] = JoinKeys(col, ref_col)
        return
    pair = (col, ref_col)
    if tuple(existing) == pair:
        return
    if isinstance(existing, JoinKeys):
        if pair not in existing.alternates:
            existing.alternates.append(pair)
    else:  # a plain tuple survived a round trip through serialisation
        graph[table][ref_table] = JoinKeys(existing[0], existing[1], [pair])


def build_relationship_graph(
    schema: Dict[str, List[str]],
    foreign_keys: Optional[ForeignKeyMap] = None,
    primary_keys: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Dict[str, Tuple[str, str]]]:
    """Build a join graph for multi-table reasoning.

    Prefers **declared foreign keys** (from the database's own catalog) when any
    are provided — the authoritative, schema-agnostic source that works on cryptic
    keys (SAP ``VBELN``), ``_key`` warehouses, and FK names that differ from the
    target table (Odoo ``partner_id`` -> ``res_partner``). Falls back to a naming
    heuristic (``<x>_id`` -> table ``<x>s``) only when the database declares no FKs.

    Format: ``{table: {neighbor_table: (from_col, to_col)}}``. Where a table has
    several FKs to the same neighbor (``from_account_id`` / ``to_account_id`` ->
    ``accounts``; TPC-DS's ``ws_sold_date_sk`` / ``ws_ship_date_sk`` ->
    ``date_dim``), the first declared edge is the default *and the rest are kept
    on it* as :class:`JoinKeys.alternates`, so a caller that knows which role the
    question named can choose. Collapsing them silently answered a question about
    shipping dates with sale dates — a wrong number no validator can see.
    """
    graph: Dict[str, Dict[str, Tuple[str, str]]] = {table: {} for table in schema}

    if foreign_keys:
        for table, fks in foreign_keys.items():
            if table not in graph:
                continue
            for col, ref_table, ref_col in fks:
                if ref_table in graph:
                    _add_edge(graph, table, ref_table, col, ref_col)
                    _add_edge(graph, ref_table, table, ref_col, col)
        # Declared FKs win, but a schema that declares *some* of them still has
        # undeclared relations; fill those from catalog keys rather than leaving
        # the tables unreachable.
        _add_primary_key_edges(graph, schema, primary_keys)
        return graph

    # Heuristic fallback (no declared FKs): "<x>_id" -> a table named <x>.
    for table, columns in schema.items():
        for col in columns:
            ref_table = _resolve_referenced_table(col, table, schema)
            if ref_table is None:
                continue
            ref_col = _primary_key_of(schema[ref_table], ref_table) or col
            graph[table].setdefault(ref_table, (col, ref_col))
            graph[ref_table].setdefault(table, (ref_col, col))

    _add_primary_key_edges(graph, schema, primary_keys)
    return graph


def _add_primary_key_edges(
    graph: Dict[str, Dict[str, Tuple[str, str]]],
    schema: Dict[str, List[str]],
    primary_keys: Optional[Dict[str, List[str]]],
) -> None:
    """Relate a child column to the parent whose *declared* key it repeats.

    The ``<x>_id`` heuristic assumes a naming convention. Warehouse and legacy
    schemas routinely break it: ``"line-item"."key"`` points at ``"order"."key"``
    with no suffix anywhere, so the graph had no edge, the planner still selected
    the parent's column, and the compiler rejected the plan outright.

    Catalog primary keys are used rather than guessed ones, and only when the key
    name is **unambiguous** — declared as the primary key of exactly one table.
    A name like ``id``, which is the key of every table in a conventional schema,
    is therefore skipped rather than wiring every table to an arbitrary one.
    Single-column keys only: a composite key is not a join this can infer safely.
    """
    if not primary_keys:
        return

    owners: Dict[str, List[str]] = {}
    for table, keys in primary_keys.items():
        if table in graph and len(keys) == 1:
            owners.setdefault(keys[0].lower(), []).append(table)

    unique = {name: tables[0] for name, tables in owners.items() if len(tables) == 1}
    if not unique:
        return

    for table, columns in schema.items():
        own_keys = {k.lower() for k in (primary_keys.get(table) or [])}
        for col in columns:
            parent = unique.get(col.lower())
            if parent is None or parent == table or col.lower() in own_keys:
                continue
            parent_col = primary_keys[parent][0]
            graph[table].setdefault(parent, (col, parent_col))
            graph[parent].setdefault(table, (parent_col, col))


# Table-name forms a "<x>_id" column may refer to. Checked case-insensitively and
# in this order, so the exact match wins before either pluralization guess.
def _name_candidates(stem: str) -> List[str]:
    cands = [stem, stem + "s"]
    if stem.endswith("y"):
        cands.append(stem[:-1] + "ies")    # category_id -> categories
    if stem.endswith(("s", "x", "z", "ch", "sh")):
        cands.append(stem + "es")          # address_id -> addresses
    return cands


def _singular_forms(stem: str) -> list[str]:
    """Singular candidates for a plural-looking stem (``orders_id`` -> ``order``)."""
    out = []
    if stem.endswith("ies"):
        out.append(stem[:-3] + "y")
    if stem.endswith("es"):
        out.append(stem[:-2])
    if stem.endswith("s"):
        out.append(stem[:-1])
    return out


def _resolve_referenced_table(col: str, table: str, schema: Dict[str, List[str]]) -> Optional[str]:
    """The table a ``<x>_id`` column most likely points at, or None.

    Matches singular and plural spellings in both directions — real schemas use
    ``user_id -> user`` as readily as ``user_id -> users``, and Rails/Django-style
    ``category_id -> categories`` is common enough that dropping it loses real
    joins. A table's own key (``shipment_id`` inside ``shipments``) is never a
    self-edge.
    """
    lower = col.lower()
    if not lower.endswith("_id") or lower == "_id":
        return None
    stem = lower[:-3]
    if not stem:
        return None

    by_lower = {t.lower(): t for t in schema}
    for cand in _name_candidates(stem) + _singular_forms(stem):
        hit = by_lower.get(cand)
        if hit is not None and hit != table:
            return hit
    return None


def _primary_key_of(columns: List[str], table: str = "") -> Optional[str]:
    """The column a FK most likely targets.

    Order: a literal ``id``/``pk``, then the table's own ``<table>_id`` /
    ``<singular>_id``. That second form is the norm in clinical, warehouse and
    legacy schemas (``patient.patient_id``), and defaulting to ``id`` there
    produced joins onto a column that does not exist — SQL that compiles and then
    fails at the database, with the join silently wrong rather than absent.

    Returns None when nothing plausible is present, so the caller decides rather
    than having a guess forced on it.
    """
    lower = {c.lower(): c for c in columns}
    for cand in ("id", "pk"):
        if cand in lower:
            return lower[cand]

    stem = table.lower().rstrip("s")
    for cand in (f"{table.lower()}_id", f"{stem}_id"):
        if cand and cand in lower:
            return lower[cand]

    # Exactly one ``*_id`` column: unambiguous, so it is the key.
    id_cols = [orig for low, orig in lower.items() if low.endswith("_id")]
    return id_cols[0] if len(id_cols) == 1 else None
