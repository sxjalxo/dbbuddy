"""Schema-portability regression corpus.

Loads realistic ERP / domain schemas from ``tests/schema_portability/*.json`` and
asserts the deterministic engine extracts the right typed WHERE filters against
them — so every planner change is validated on real-world schema shapes (cryptic
SAP keys, ERPNext string PKs, boolean flags, junction tables), not just the demo.

Add a schema by dropping a JSON file in that folder; add a regression by adding a
case to CASES. Each fix we make should arrive with a case here.
"""

import json
import pathlib
from datetime import date

import pytest

import dbbuddy_core.type_handlers as type_handlers
from dbbuddy_core.relationship_graph import build_relationship_graph
from dbbuddy_core.type_handlers import InvalidComparisonError, extract_comparisons

_DIR = pathlib.Path(__file__).parent / "schema_portability"


def _load():
    out = {}
    for path in sorted(_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        tables = data["tables"]
        out[path.stem] = {
            "schema": {t: list(cols) for t, cols in tables.items()},
            "types": {t: dict(cols) for t, cols in tables.items()},
            "foreign_keys": {
                t: [tuple(fk) for fk in fks]
                for t, fks in data.get("foreign_keys", {}).items()
            },
        }
    return out


SCHEMAS = _load()


@pytest.fixture
def pinned_today(monkeypatch):
    monkeypatch.setattr(type_handlers, "_today", lambda: date(2026, 7, 15))
    return date(2026, 7, 15)


def where(schema_name, query):
    s = SCHEMAS[schema_name]
    return extract_comparisons(query, s["schema"], s["types"])


def test_corpus_loaded():
    # Guardrail so a bad path doesn't silently skip every case.
    assert {"sap", "odoo", "erpnext", "healthcare", "ecommerce"} <= set(SCHEMAS)


# (schema, query, expected filter spec that must be present)
CASES = [
    # Type-aware numeric comparisons across naming conventions.
    ("sap", "sales orders with NETWR over 1000", {"column": "VBAK.NETWR", "operator": ">", "value": 1000}),
    ("odoo", "sale orders with amount_total over 5000", {"column": "sale_order.amount_total", "operator": ">", "value": 5000}),
    ("erpnext", "invoices with grand_total over 10000", {"column": "tabSalesInvoice.grand_total", "operator": ">", "value": 10000}),
    ("healthcare", "observations with value_num > 140", {"column": "observations.value_num", "operator": ">", "value": 140}),
    ("ecommerce", "orders with total over 100", {"column": "orders.total", "operator": ">", "value": 100}),
    # Thousands separators must not silently truncate ("1,000" -> 1).
    ("ecommerce", "orders with total over 1,000", {"column": "orders.total", "operator": ">", "value": 1000}),
    # Equality across conventions.
    ("odoo", "sale_order where state = done", {"column": "sale_order.state", "operator": "=", "value": "done"}),
    ("healthcare", "encounters where department = Cardiology", {"column": "encounters.department", "operator": "=", "value": "Cardiology"}),
    # Grammar-aware predicates (Phase B).
    ("ecommerce", "orders where status in (paid, pending)", {"column": "orders.status", "operator": "IN", "value": ["paid", "pending"]}),
    ("ecommerce", "orders where status is not cancelled", {"column": "orders.status", "operator": "!=", "value": "cancelled"}),
    ("ecommerce", "customers with no email", {"column": "customers.email", "operator": "IS NULL", "value": None}),
    ("ecommerce", "customers where email contains gmail", {"column": "customers.email", "operator": "LIKE", "value": "%gmail%"}),
    # Phase D: boolean adjective phrasing, currency symbol.
    ("ecommerce", "paid orders", {"column": "orders.is_paid", "operator": "=", "value": True}),
    ("ecommerce", "unpaid orders", {"column": "orders.is_paid", "operator": "=", "value": False}),
    ("ecommerce", "in stock products", {"column": "products.in_stock", "operator": "=", "value": True}),
    ("ecommerce", "orders with total over $500", {"column": "orders.total", "operator": ">", "value": 500}),
]


@pytest.mark.parametrize("schema, query, expected", CASES)
def test_extraction_cases(schema, query, expected):
    assert expected in where(schema, query)


def test_determinism_boolean_column_does_not_suppress_value(pinned_today):
    # Determinism (Phase C): ecommerce.orders has an `is_paid` column. The value
    # "paid" in `status = paid` must NOT be dropped just because "paid" is a
    # fragment of an unrelated column name.
    got = where("ecommerce", "orders where status = paid and total over 500")
    assert {"column": "orders.status", "operator": "=", "value": "paid"} in got
    assert {"column": "orders.total", "operator": ">", "value": 500} in got


def test_boolean_adjective_not_fired_in_value_position():
    # ecommerce has both `status` (varchar) and `is_paid` (boolean). "paid" used
    # as a status value must NOT also flip the is_paid flag.
    for q in ("orders where status = paid", "orders where status in (paid, pending)",
              "orders where status is not paid"):
        got = where("ecommerce", q)
        assert all(f["column"] != "orders.is_paid" for f in got), (q, got)


def test_marker_word_not_captured_as_value():
    # "customer named X": ERPNext has a `customer` column — "named" must not become
    # the customer value.
    got = where("erpnext", "customer named Acme")
    assert all(f.get("value") != "named" for f in got)


def test_ordering_on_text_column_fails_closed():
    with pytest.raises(InvalidComparisonError):
        where("healthcare", "patients where first_name > 5")


@pytest.mark.parametrize("schema", list(SCHEMAS))
def test_no_spurious_filters_on_plain_listing(schema):
    # "show all <rows>" style queries must not fabricate a filter on any schema.
    for q in ("show all records", "list everything"):
        assert where(schema, q) == []


# ── Phase A: join graph from declared FK metadata ─────────────────────────────

def _graph(schema_name):
    s = SCHEMAS[schema_name]
    return build_relationship_graph(s["schema"], s["foreign_keys"] or None)


def test_fk_metadata_joins_cryptic_keys():
    # SAP: joins on non-_id cryptic columns the heuristic could never find.
    g = _graph("sap")
    assert g["VBAP"]["VBAK"] == ("VBELN", "VBELN")
    assert g["VBAK"]["KNA1"] == ("KUNNR", "KUNNR")


def test_fk_metadata_joins_when_fk_name_differs_from_table():
    # Odoo: partner_id -> res_partner (not "partner").
    g = _graph("odoo")
    assert g["sale_order"]["res_partner"] == ("partner_id", "id")
    assert g["res_partner"]["res_country"] == ("country_id", "id")


def test_fk_metadata_picks_a_real_column_for_multi_fk():
    # Banking: two FKs to accounts — keep a real column (first), never a guess.
    g = _graph("banking")
    assert g["transfers"]["accounts"][0] in ("from_account_id", "to_account_id")
    assert g["accounts"]["customers"] == ("owner_id", "customer_id")


def test_heuristic_fallback_has_no_garbage_self_edges():
    # No declared FKs → heuristic, but a table's own PK must not self-join.
    schema = {"shipments": ["shipment_id", "carrier_id"], "carriers": ["carrier_id"]}
    g = build_relationship_graph(schema)  # no FKs
    assert "shipments" not in g["shipments"]
    # `carriers` has no `id` column, so the target is its own `carrier_id`. This
    # previously asserted ("carrier_id", "id") — a join onto a column that does
    # not exist, which the database rejects. The expectation encoded the bug.
    assert g["shipments"].get("carriers") == ("carrier_id", "carrier_id")


def test_heuristic_targets_a_real_key_when_there_is_no_id_column():
    # Clinical/warehouse convention: `patient.patient_id`, never a bare `id`.
    schema = {"encounter": ["encounter_id", "patient_id"],
              "patient": ["patient_id", "name"]}
    g = build_relationship_graph(schema)
    assert g["encounter"]["patient"] == ("patient_id", "patient_id")
    assert g["patient"]["encounter"] == ("patient_id", "patient_id")


def test_heuristic_still_prefers_a_literal_id_when_present():
    schema = {"orders": ["id", "user_id"], "users": ["id", "user_id", "name"]}
    g = build_relationship_graph(schema)
    assert g["orders"]["users"] == ("user_id", "id")
