"""Unit tests for the relation-graph builders (pure, no database).

Covers snapshot serialization, the table-level detail graph (declared FK vs
heuristic fallback), and the database-level overview with cross-DB inference
(confidence tiers, generic-token stopwords, and the IDF-style guard that stops a
common token from wiring the whole fleet together).
"""

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKEND = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app_db.relations_service import (  # noqa: E402
    build_detail_graph,
    build_overview_graph,
    snapshot_from_rich,
)
from dbbuddy_core.dialects.schema_meta import (  # noqa: E402
    ColumnMeta,
    DatabaseSchema,
    ForeignKeyMeta,
    TableMeta,
)


def _table(name, cols, fks=None):
    return {
        "name": name,
        "columns": [{"name": c, "type": "int", "pk": c == "id"} for c in cols],
        "foreign_keys": fks or [],
    }


def _fk(column, ref_table, ref_col="id"):
    return {"column": column, "referenced_table": ref_table, "referenced_column": ref_col}


# ── snapshot_from_rich ────────────────────────────────────────────────────────

def test_snapshot_from_rich_serializes_tables_columns_fks():
    schema = DatabaseSchema(tables={
        "orders": TableMeta(
            name="orders",
            columns=[ColumnMeta("id", "int", is_primary_key=True), ColumnMeta("customer_id", "int")],
            foreign_keys=[ForeignKeyMeta("customer_id", "customers", "id")],
        ),
        "customers": TableMeta(name="customers", columns=[ColumnMeta("id", "int", is_primary_key=True)]),
    })
    snap = snapshot_from_rich(schema)
    assert {t["name"] for t in snap["tables"]} == {"orders", "customers"}
    orders = next(t for t in snap["tables"] if t["name"] == "orders")
    assert orders["foreign_keys"] == [{"column": "customer_id", "referenced_table": "customers", "referenced_column": "id"}]
    assert {c["name"]: c["pk"] for c in orders["columns"]} == {"id": True, "customer_id": False}


# ── detail graph ──────────────────────────────────────────────────────────────

def test_detail_graph_uses_declared_foreign_keys():
    snap = {"tables": [
        _table("orders", ["id", "customer_id"], [_fk("customer_id", "customers")]),
        _table("customers", ["id", "name"]),
    ]}
    g = build_detail_graph("c1", "Shop", snap)
    assert g["level"] == "detail" and g["connection_id"] == "c1"
    assert {n["id"] for n in g["nodes"]} == {"orders", "customers"}
    assert g["edges"] == [
        {"source": "orders", "target": "customers", "from_col": "customer_id", "to_col": "id", "kind": "fk"}
    ]
    assert g["stats"] == {"tables": 2, "relationships": 1}


def test_detail_graph_falls_back_to_naming_heuristic_without_fks():
    # No declared FKs, but the <x>_id -> <x>s naming convention is present.
    snap = {"tables": [
        _table("orders", ["id", "user_id"]),
        _table("users", ["id", "name"]),
    ]}
    g = build_detail_graph("c1", "Shop", snap)
    assert len(g["edges"]) == 1
    edge = g["edges"][0]
    assert edge["kind"] == "heuristic"
    assert {edge["source"], edge["target"]} == {"orders", "users"}


def test_detail_graph_ignores_fk_to_unknown_table():
    snap = {"tables": [_table("orders", ["id", "vendor_id"], [_fk("vendor_id", "vendors")])]}
    g = build_detail_graph("c1", "Shop", snap)
    assert g["edges"] == []  # referenced table not in this DB


# ── overview graph ────────────────────────────────────────────────────────────

def _db(id, label, snapshot):
    return {"id": id, "label": label, "engine": "mysql", "database": label, "snapshot": snapshot}


def test_overview_lists_databases_without_snapshots_as_edgeless_nodes():
    g = build_overview_graph([
        _db("a", "A", None),
        _db("b", "B", None),
    ])
    assert g["stats"] == {
        "databases": 2, "snapshotted": 0, "links": 0, "total_links": 0, "truncated": False,
    }
    assert all(n["has_snapshot"] is False for n in g["nodes"])
    assert g["edges"] == []


def test_overview_infers_define_refer_link_with_high_confidence():
    # DB "crm" owns the customers table; DB "billing" references customer_id.
    crm = {"tables": [_table("customers", ["id", "name"])]}
    billing = {"tables": [_table("invoices", ["id", "customer_id"])]}
    g = build_overview_graph([_db("crm", "CRM", crm), _db("billing", "Billing", billing)])
    assert len(g["edges"]) == 1
    e = g["edges"][0]
    assert {e["source"], e["target"]} == {"crm", "billing"}
    assert e["confidence"] == 0.9  # define + refer is the strongest signal
    assert "customer" in e["shared"]


def test_overview_both_define_is_medium_confidence():
    a = {"tables": [_table("products", ["id", "name"])]}
    b = {"tables": [_table("products", ["id", "sku"])]}
    g = build_overview_graph([_db("a", "A", a), _db("b", "B", b)])
    assert len(g["edges"]) == 1
    assert g["edges"][0]["confidence"] == 0.7


def test_overview_generic_tokens_do_not_create_edges():
    # Both DBs share only a stopword entity ("status") — not a real link.
    a = {"tables": [_table("a_thing", ["id", "status_id"])]}
    b = {"tables": [_table("b_thing", ["id", "status_id"])]}
    g = build_overview_graph([_db("a", "A", a), _db("b", "B", b)])
    assert g["edges"] == []


def test_overview_small_fleet_keeps_shared_entity_links():
    # With a handful of DBs, an entity common to all of them is a real link, not
    # noise — the IDF guard must NOT drop it (regression: it once did at small N).
    dbs = [_db(f"d{i}", f"D{i}", {"tables": [_table("customers", ["id"])]}) for i in range(4)]
    g = build_overview_graph(dbs)
    assert g["stats"]["snapshotted"] == 4
    assert len(g["edges"]) == 6  # complete graph among 4 (all share "customer")


def _ubiquitous_fleet(n):
    """n databases that all define the same single entity."""
    return build_overview_graph(
        [_db(f"d{i}", f"D{i}", {"tables": [_table("customers", ["id"])]}) for i in range(n)]
    )


def test_overview_downweights_ubiquitous_token_instead_of_dropping_it():
    # On a larger fleet a token present in every DB is too generic to be a strong
    # link — but it is still a link. It is tapered, not deleted.
    g = _ubiquitous_fleet(12)
    assert g["stats"]["snapshotted"] == 12
    assert len(g["edges"]) == 12 * 11 // 2  # every pair still present
    # Both-define is 0.7 at full weight; at fan-out 12 it is tapered to 8/12.
    assert all(e["confidence"] == round(0.7 * (8 / 12), 2) for e in g["edges"])


def test_overview_taper_is_continuous_across_the_old_cliff():
    # Regression for the cliff: a hard cutoff meant 8 databases sharing an entity
    # produced a full graph and 9 produced *nothing*, so connecting one more
    # database silently blanked the view. Confidence must now degrade smoothly.
    conf = {}
    for n in (7, 8, 9, 10, 16):
        g = _ubiquitous_fleet(n)
        assert len(g["edges"]) == n * (n - 1) // 2, f"edges vanished at n={n}"
        conf[n] = g["edges"][0]["confidence"]
    assert conf[7] == conf[8]  # at or below _FREE_FANOUT: no penalty
    assert conf[8] > conf[9] > conf[10] > conf[16]  # then monotonically weaker
    assert conf[9] / conf[8] > 0.8  # and the step across the old cliff is gentle


def test_overview_specific_link_outranks_generic_one():
    # The taper's purpose: a link backed by an entity only two DBs share must sort
    # above one backed by an entity the whole fleet shares.
    dbs = [_db(f"d{i}", f"D{i}", {"tables": [_table("customers", ["id"])]}) for i in range(20)]
    # d0 and d1 additionally share a narrow entity.
    dbs[0]["snapshot"]["tables"].append(_table("warranties", ["id"]))
    dbs[1]["snapshot"]["tables"].append(_table("warranties", ["id", "name"]))
    g = build_overview_graph(dbs)
    top = g["edges"][0]
    assert {top["source"], top["target"]} == {"d0", "d1"}
    assert "warranty" in top["shared"]
    assert top["confidence"] > g["edges"][-1]["confidence"]


def test_overview_skips_tokens_too_faint_to_perceive():
    # Past ~160 sharers a token's weight falls under _MIN_COMPUTE_WEIGHT. This is a
    # compute bound, not a claim that the databases are unrelated: generating every
    # pair is quadratic in fan-out, and these edges would be invisible hairlines.
    # It applies only where confidence is already negligible, so no visible step.
    g = _ubiquitous_fleet(200)
    assert g["stats"]["snapshotted"] == 200
    assert g["edges"] == []


def test_overview_reports_true_link_total_when_truncated():
    # The cap keeps the strongest 1000 links, so `links` alone is a floor: a fleet
    # with 20 000 links is indistinguishable from one with exactly 1000. Callers
    # need the real total to say "showing 1,000 of N" instead (#16).
    dbs = [_db(f"d{i}", f"D{i}", {"tables": [_table("customers", ["id"])]}) for i in range(60)]
    g = build_overview_graph(dbs)
    stats = g["stats"]
    assert stats["truncated"] is True
    assert stats["links"] == 1000 == len(g["edges"])  # what the payload carries
    assert stats["total_links"] == 60 * 59 // 2       # what actually exists
    assert stats["total_links"] > stats["links"]


def test_overview_total_links_equals_links_when_not_truncated():
    crm = {"tables": [_table("customers", ["id", "name"])]}
    billing = {"tables": [_table("invoices", ["id", "customer_id"])]}
    stats = build_overview_graph([_db("crm", "CRM", crm), _db("billing", "B", billing)])["stats"]
    assert stats["truncated"] is False
    assert stats["total_links"] == stats["links"] == 1


def test_overview_confidence_sorted_descending():
    # Strong link (crm defines customer, billing refers) and a weaker both-define
    # link (crm & analytics both define "product").
    crm = {"tables": [_table("customers", ["id"]), _table("products", ["id"])]}
    billing = {"tables": [_table("invoices", ["id", "customer_id"])]}
    analytics = {"tables": [_table("products", ["id", "name"])]}
    g = build_overview_graph([
        _db("crm", "CRM", crm), _db("billing", "Billing", billing), _db("analytics", "Analytics", analytics),
    ])
    confs = [e["confidence"] for e in g["edges"]]
    assert confs == sorted(confs, reverse=True)
    assert confs[0] == 0.9
