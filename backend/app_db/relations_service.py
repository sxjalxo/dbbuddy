"""Relation-graph builders — pure functions over stored schema snapshots.

The relation graph is deliberately split from live database access: an ERP schema
is introspected once and persisted as a *snapshot* (see ``SchemaSnapshot``), and
every graph view is then computed from those snapshots with **no live-DB hits**.
That is what lets the overview scale to hundreds of databases — rendering it is a
single app-DB read plus an in-memory build, not hundreds of connections.

Two levels, matching the UI:

* **Overview** (``build_overview_graph``): one node per database, edges *inferred*
  between databases. Real foreign keys never cross a database boundary, so
  cross-DB links can only be inferred — here from shared entity naming
  (``customers`` table in one DB, ``customer_id`` column in another), each edge
  carrying a confidence score and the entities it was inferred from.
* **Detail** (``build_detail_graph``): one node per table within a single
  database, edges from the database's *declared* foreign keys (authoritative),
  falling back to the ``<x>_id -> <x>s`` naming heuristic only when a schema
  declares none — reusing :func:`dbbuddy_core.relationship_graph.build_relationship_graph`.

Everything here operates on plain dicts (the snapshot JSON shape below), so it is
engine-agnostic and unit-testable without a database:

    snapshot = {
        "tables": [
            {"name": "orders",
             "columns": [{"name": "id", "type": "int", "pk": True}, ...],
             "foreign_keys": [
                 {"column": "customer_id", "referenced_table": "customers",
                  "referenced_column": "id"}]},
            ...
        ]
    }
"""

from __future__ import annotations

from collections import defaultdict

from dbbuddy_core.relationship_graph import build_relationship_graph

# Generic tokens that name attributes rather than entities. Left in, a shared
# ``status_id`` / ``type_id`` would wire nearly every database to every other, so
# they are excluded from cross-DB inference (within-DB FK edges are unaffected).
_STOPWORD_ENTITIES = {
    "id", "type", "status", "state", "value", "key", "data", "kind", "code",
    "flag", "level", "category", "group", "class", "item", "record", "detail",
}

# A token's *fan-out* is how many databases share it. Low fan-out is a specific,
# discriminating link; high fan-out is a generic one that would wire the whole
# fleet together. Rather than delete high-fan-out edges, their confidence is
# **tapered** (``_token_weight``) so they fade below stronger links instead of
# vanishing — an earlier hard cutoff produced a cliff where connecting a 9th
# database silently emptied the entire graph (QA finding #12).
#
# Fan-out at or below this is never penalized: a handful of databases sharing an
# entity is real signal, not noise. It also means a fleet this small can never be
# penalized at all, so the old "small fleet exemption" now falls out for free
# rather than being a special case.
_FREE_FANOUT = 8

# ── Compute bound (NOT part of the confidence model) ──────────────────────────
#
# This threshold is an *optimization*, not a statement about relationships. It
# does not mean "these databases are unrelated"; it means "this token is so
# uninformative that generating every pair among its holders isn't worth the
# CPU". Pair emission is quadratic in fan-out, so one token appearing in all 1200
# databases would otherwise materialize ~720k candidate pairs (QA finding #13).
#
# Placed where a token's weight — and therefore its edges' confidence (<= 0.9 *
# 0.05) — is already a barely-visible hairline, so it bounds work at ~160
# databases per token without a discontinuity anywhere users can perceive one.
# Once #13 replaces pair generation with something that doesn't explode on
# high-fan-out tokens, re-evaluate whether this is needed at all: the taper alone
# may be sufficient.
_MIN_COMPUTE_WEIGHT = 0.05

# Hard ceiling on inferred edges returned, kept highest-confidence first. Bounds
# the payload and keeps the overview legible even across hundreds of databases;
# ``stats.truncated`` flags when it kicked in.
_MAX_EDGES = 1000

# Confidence scores for the inference signals, strongest first.
_CONF_DEFINE_REFER = 0.9  # one DB owns the entity table, another references it
_CONF_BOTH_DEFINE = 0.7   # both DBs model the same entity (shared dimension)
_CONF_BOTH_REFER = 0.5    # both DBs merely reference it (no owner seen)


def snapshot_from_rich(schema) -> dict:
    """Serialize a :class:`~dbbuddy_core.dialects.schema_meta.DatabaseSchema` into
    the storable snapshot dict. Kept tiny and dependency-free so the stored shape
    is stable and readable."""
    tables = []
    for name, meta in schema.tables.items():
        tables.append({
            "name": name,
            "columns": [
                {"name": c.name, "type": c.data_type, "pk": bool(c.is_primary_key)}
                for c in meta.columns
            ],
            "foreign_keys": [
                {"column": fk.column, "referenced_table": fk.referenced_table,
                 "referenced_column": fk.referenced_column}
                for fk in meta.foreign_keys
            ],
        })
    return {"tables": tables}


def snapshot_table_count(snapshot: dict | None) -> int:
    if not snapshot:
        return 0
    return len(snapshot.get("tables", []))


def _normalize_entity(name: str) -> str:
    """Normalize a table or reference name to a comparable entity token.

    Lowercases, drops a schema/owner prefix (``dbo.orders`` -> ``orders``), and
    singularizes so ``customers`` and ``customer`` match. The singular rules are
    deliberately small but avoid the classic over-stripping traps: ``categories``
    -> ``category`` and ``statuses`` -> ``status``, while words ending in ``us`` /
    ``ss`` / ``is`` (``status``, ``address``, ``analysis``) are left alone. It is
    only a matching key across schemas, so it need not be linguistically perfect —
    just consistent.
    """
    token = (name or "").strip().lower()
    if "." in token:
        token = token.rsplit(".", 1)[-1]
    if len(token) <= 3 or not token.endswith("s"):
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith(("ses", "xes", "zes", "ches", "shes")):
        return token[:-2]
    if token.endswith(("ss", "us", "is", "os")):
        return token
    return token[:-1]


def _entity_signals(snapshot: dict) -> tuple[set[str], set[str]]:
    """Return ``(defines, refers)`` entity tokens for one database.

    * ``defines`` — entities the DB *models*: its table names.
    * ``refers`` — entities the DB *references*: the target of each declared FK,
      plus the ``<x>`` of any ``<x>_id`` column (the common convention when FKs
      are not declared).
    """
    defines: set[str] = set()
    refers: set[str] = set()
    for table in snapshot.get("tables", []):
        defines.add(_normalize_entity(table.get("name", "")))
        for fk in table.get("foreign_keys", []):
            refers.add(_normalize_entity(fk.get("referenced_table", "")))
        for col in table.get("columns", []):
            cname = (col.get("name") or "").lower()
            if cname.endswith("_id") and len(cname) > 3:
                refers.add(_normalize_entity(cname[:-3]))
    defines.discard("")
    refers.discard("")
    return defines, refers


def build_overview_graph(databases: list[dict]) -> dict:
    """Build the database-level overview graph.

    ``databases`` is a list of ``{"id", "label", "engine", "database",
    "snapshot": dict | None, "captured_at": str | None}``. Databases without a
    snapshot appear as nodes (so the user sees them and can snapshot them) but
    contribute no inferred edges.

    An inverted index maps each entity token to the databases that define or
    reference it, and edges are emitted per shared token, scored by signal
    strength (``_pair_confidence``) tapered by how generic the token is
    (``_token_weight``). A pair keeps the best score any single shared token
    earns it.

    Building the index is linear in the total number of tables/columns, but
    emitting edges is **quadratic in the number of databases sharing a token**
    (every pair among them). ``_MAX_EDGES`` caps only the result, so the work is
    bounded instead by ``_MIN_COMPUTE_WEIGHT``, a pure optimization that stops
    pair generation once a token's contribution is imperceptible (~160
    databases). See finding #13 in docs/QA_CHECKLIST.md.
    """
    nodes = []
    signals: dict[str, tuple[set[str], set[str]]] = {}
    for db in databases:
        snap = db.get("snapshot")
        nodes.append({
            "id": db["id"],
            "label": db.get("label") or db["id"],
            "engine": db.get("engine"),
            "database": db.get("database"),
            "table_count": snapshot_table_count(snap),
            "has_snapshot": snap is not None,
            "captured_at": db.get("captured_at"),
        })
        if snap is not None:
            signals[db["id"]] = _entity_signals(snap)

    # Inverted index: token -> {"defines": {db_id}, "refers": {db_id}}.
    index: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"defines": set(), "refers": set()})
    for db_id, (defines, refers) in signals.items():
        for tok in defines:
            if tok not in _STOPWORD_ENTITIES:
                index[tok]["defines"].add(db_id)
        for tok in refers:
            if tok not in _STOPWORD_ENTITIES:
                index[tok]["refers"].add(db_id)

    snapshotted = len(signals)

    # pair (a<b) -> {"confidence": float, "shared": {token}}
    pairs: dict[tuple[str, str], dict] = defaultdict(lambda: {"confidence": 0.0, "shared": set()})
    for tok, hit in index.items():
        involved = hit["defines"] | hit["refers"]
        if len(involved) < 2:
            continue  # unique to one database — nothing to link
        weight = _token_weight(len(involved))
        if weight < _MIN_COMPUTE_WEIGHT:
            continue  # compute bound, not a confidence judgement — see the constant
        members = sorted(involved)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                conf = _pair_confidence(tok, a, b, hit) * weight
                edge = pairs[(a, b)]
                # Best single token wins: a pair linked by one specific entity keeps
                # its strong score even if it also happens to share generic ones.
                edge["confidence"] = max(edge["confidence"], conf)
                edge["shared"].add(tok)

    edges = [
        {
            "source": a, "target": b,
            "confidence": round(data["confidence"], 2),
            "shared": sorted(data["shared"])[:8],
            "shared_count": len(data["shared"]),
            "kind": "inferred",
        }
        for (a, b), data in sorted(pairs.items())
    ]
    # Highest-confidence first, then cap so the payload/render stays bounded even
    # on very large fleets. Sort is stable on the already-sorted (a, b) order, so
    # ties break deterministically.
    edges.sort(key=lambda e: e["confidence"], reverse=True)
    # Record the true total *before* capping. Without it a caller cannot tell a
    # fleet with exactly 1000 links from one with 20 000 showing its strongest
    # 1000 — and since the cap keeps the top of a confidence sort, truncation
    # silently removes whole weaker tiers rather than thinning evenly (#16).
    total_links = len(edges)
    truncated = total_links > _MAX_EDGES
    if truncated:
        edges = edges[:_MAX_EDGES]

    return {
        "level": "overview",
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "databases": len(nodes),
            "snapshotted": snapshotted,
            "links": len(edges),          # how many are in this payload
            "total_links": total_links,   # how many exist; == links unless truncated
            "truncated": truncated,
        },
    }


def _token_weight(fanout: int) -> float:
    """How much an entity token shared by ``fanout`` databases is worth, in (0, 1].

    An IDF-style taper: full weight up to ``_FREE_FANOUT``, then decaying as
    ``_FREE_FANOUT / fanout`` — so a token common to the whole fleet becomes weak
    rather than disappearing. Monotonic and continuous, which is the point: the
    previous hard cutoff meant a token shared by 8 databases scored full strength
    and the same token shared by 9 produced *no edges at all*, so connecting one
    more database blanked the graph (QA finding #12). Now 9 databases simply score
    8/9 of 8's confidence, and the ranking pushes generic links below specific
    ones on their own.
    """
    if fanout <= _FREE_FANOUT:
        return 1.0
    return _FREE_FANOUT / fanout


def _pair_confidence(token: str, a: str, b: str, hit: dict) -> float:
    """Confidence that databases ``a`` and ``b`` are linked through ``token``."""
    a_def, b_def = a in hit["defines"], b in hit["defines"]
    a_ref, b_ref = a in hit["refers"], b in hit["refers"]
    # One side owns the entity table, the other references it: the strongest cue.
    if (a_def and b_ref) or (b_def and a_ref):
        return _CONF_DEFINE_REFER
    if a_def and b_def:
        return _CONF_BOTH_DEFINE
    return _CONF_BOTH_REFER


def build_detail_graph(connection_id: str, label: str, snapshot: dict) -> dict:
    """Build the table-level graph for a single database from its snapshot.

    Edges come from *declared* foreign keys when the schema has any (authoritative,
    directed); otherwise from the shared ``<x>_id -> <x>s`` naming heuristic. The
    two are tagged (``kind``) so the UI can distinguish a real FK from a guess.
    """
    tables = snapshot.get("tables", [])
    table_names = {t["name"] for t in tables}

    nodes = [
        {
            "id": t["name"],
            "label": t["name"],
            "column_count": len(t.get("columns", [])),
            "primary_keys": [c["name"] for c in t.get("columns", []) if c.get("pk")],
        }
        for t in sorted(tables, key=lambda t: t["name"])
    ]

    declared = [
        (t["name"], fk)
        for t in tables
        for fk in t.get("foreign_keys", [])
        if fk.get("referenced_table") in table_names
    ]

    edges: list[dict] = []
    if declared:
        for table_name, fk in declared:
            edges.append({
                "source": table_name,
                "target": fk["referenced_table"],
                "from_col": fk["column"],
                "to_col": fk["referenced_column"],
                "kind": "fk",
            })
    else:
        # No declared FKs: fall back to the naming heuristic, reusing the engine's
        # graph builder. It is bidirectional, so dedupe to one edge per pair.
        simple = {t["name"]: [c["name"] for c in t.get("columns", [])] for t in tables}
        graph = build_relationship_graph(simple)
        seen: set[tuple[str, str]] = set()
        for table_name, neighbors in graph.items():
            for neighbor, (from_col, to_col) in neighbors.items():
                key = tuple(sorted((table_name, neighbor)))
                if key in seen:
                    continue
                seen.add(key)
                edges.append({
                    "source": table_name,
                    "target": neighbor,
                    "from_col": from_col,
                    "to_col": to_col,
                    "kind": "heuristic",
                })

    edges.sort(key=lambda e: (e["source"], e["target"]))
    return {
        "level": "detail",
        "connection_id": connection_id,
        "label": label,
        "nodes": nodes,
        "edges": edges,
        "stats": {"tables": len(nodes), "relationships": len(edges)},
    }
