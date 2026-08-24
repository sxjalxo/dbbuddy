"""Behavioral Test Suite — schema-adaptive regression for the query engine.

The old golden suite hard-coded expected table names ("payments", "orders"),
so it only worked for one schema. This suite instead classifies each connected
schema into semantic *roles* (entity, monetary, temporal, event, quantity) and
validates the engine's *intent* — e.g. "monetary aggregate grouped by entity" —
using the schema's own table/column names. It therefore adapts to any database.

Validation drives the deterministic pipeline (intent -> plan) directly, so no
database connection is required.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set

from dbbuddy_core.mapping import map_column
from dbbuddy_core.logger import get_logger

logger = get_logger()

# Semantic roles a table can carry (a table may have several).
ROLE_ENTITY = "entity"      # subject of the data (users, customers)
ROLE_MONETARY = "monetary"  # has a value/amount/price column
ROLE_TEMPORAL = "temporal"  # has a date/time column
ROLE_QUANTITY = "quantity"  # has a count/quantity column
ROLE_EVENT = "event"        # references another table (payments, orders)


def _safe_term(column: str) -> str:
    try:
        return map_column(column)
    except Exception:
        return ""


def _singular(table: str) -> str:
    return table[:-1] if table.endswith("s") and len(table) > 1 else table


def classify_schema(schema: Dict[str, List[str]]) -> Dict[str, Set[str]]:
    """Tag each table with semantic roles inferred from its columns and FKs."""
    referenced: Set[str] = set()   # tables referenced by a foreign key (parents)
    has_fk: Dict[str, bool] = {}   # table -> has an outgoing FK

    for table, columns in schema.items():
        out_fk = False
        for col in columns:
            if col.endswith("_id") and col != "id":
                parent = col[:-3] + "s"  # user_id -> users
                if parent in schema:
                    referenced.add(parent)
                    out_fk = True
        has_fk[table] = out_fk

    roles: Dict[str, Set[str]] = {}
    for table, columns in schema.items():
        terms = {_safe_term(col) for col in columns}
        r: Set[str] = set()
        if "value" in terms:
            r.add(ROLE_MONETARY)
        if "date" in terms:
            r.add(ROLE_TEMPORAL)
        if "quantity" in terms:
            r.add(ROLE_QUANTITY)
        if has_fk[table]:
            r.add(ROLE_EVENT)
        roles[table] = r

    # entity = a table referenced by others (a parent), or a top-level named
    # table with no outgoing FK (the subject the data is "about").
    for table, columns in schema.items():
        is_named = any(_safe_term(col) == "name" for col in columns)
        if table in referenced or (is_named and not has_fk[table]):
            roles[table].add(ROLE_ENTITY)

    return roles


class SchemaProfile:
    """Roles inferred from a schema, with helpers to pick representatives."""

    ENTITY_PREFERENCE = ["users", "customers", "accounts", "clients", "people", "members"]

    def __init__(self, schema: Dict[str, List[str]]):
        self.schema = schema
        self.roles = classify_schema(schema)

    def tables_with_role(self, role: str) -> List[str]:
        return [t for t, r in self.roles.items() if role in r]

    def has_role(self, role: str) -> bool:
        return bool(self.tables_with_role(role))

    def role_of(self, table: str) -> Set[str]:
        return self.roles.get(table, set())

    def entity_table(self) -> Optional[str]:
        candidates = self.tables_with_role(ROLE_ENTITY)
        for pref in self.ENTITY_PREFERENCE:
            if pref in candidates:
                return pref
        return candidates[0] if candidates else None

    def monetary_table(self) -> Optional[str]:
        candidates = self.tables_with_role(ROLE_MONETARY)
        return candidates[0] if candidates else None

    def monetary_column(self, table: Optional[str] = None) -> Optional[str]:
        table = table or self.monetary_table()
        if not table:
            return None
        for col in self.schema.get(table, []):
            if _safe_term(col) == "value":
                return col
        return None

    def event_table(self) -> Optional[str]:
        """An event table, preferring one that references the entity."""
        entity = self.entity_table()
        events = self.tables_with_role(ROLE_EVENT)
        if entity:
            fk = f"{_singular(entity)}_id"
            for t in events:
                if fk in self.schema.get(t, []):
                    return t
        return events[0] if events else None


# --- plan inspection helpers -------------------------------------------------

def _aggregation(plan: dict) -> Optional[dict]:
    agg = plan.get("aggregation")
    return agg if isinstance(agg, dict) else None


def _agg_function(plan: dict) -> Optional[str]:
    agg = _aggregation(plan)
    return agg.get("function") if agg else None


def _agg_table(plan: dict) -> Optional[str]:
    agg = _aggregation(plan)
    col = agg.get("column") if agg else None
    return col.get("table") if isinstance(col, dict) else None


def _group_tables(plan: dict) -> List[str]:
    return [g.get("table") for g in plan.get("group_by", []) if isinstance(g, dict)]


def _order_direction(plan: dict) -> Optional[str]:
    order = plan.get("order_by")
    return order.get("direction") if isinstance(order, dict) else None


def _plan_for(query: str, schema: Dict[str, List[str]]):
    """Run a query through the deterministic pipeline (no DB)."""
    from dbbuddy_core.intent_builder import build_query_intent
    from dbbuddy_core.query_planner import plan_query_execution
    from dbbuddy_core.execution import compile_sql

    intent = build_query_intent(query, [], None, schema)
    plan = plan_query_execution(intent, schema, None, None)
    sql = compile_sql(plan)
    return plan, sql


@dataclass
class Behavior:
    name: str
    requires: List[str]                       # roles the schema must provide
    build: Callable[[SchemaProfile], str]     # profile -> NL query
    check: Callable[[dict, SchemaProfile], tuple]  # (plan, profile) -> (ok, detail)


def _default_behaviors() -> List[Behavior]:
    return [
        Behavior(
            name="monetary aggregate grouped by entity",
            requires=[ROLE_MONETARY, ROLE_ENTITY],
            build=lambda p: f"total {p.monetary_column()} per {_singular(p.entity_table())}",
            check=lambda plan, p: _check(
                plan,
                expect_agg="SUM",
                metric_role=ROLE_MONETARY,
                group_role=ROLE_ENTITY,
                profile=p,
            ),
        ),
        Behavior(
            name="average monetary value",
            requires=[ROLE_MONETARY],
            build=lambda p: f"average {p.monetary_column()}",
            check=lambda plan, p: _check(plan, expect_agg="AVG", metric_role=ROLE_MONETARY, profile=p),
        ),
        Behavior(
            name="count entities",
            requires=[ROLE_ENTITY],
            build=lambda p: f"count {p.entity_table()}",
            check=lambda plan, p: _check(plan, expect_agg="COUNT", profile=p),
        ),
        Behavior(
            name="top entities by monetary",
            requires=[ROLE_MONETARY, ROLE_ENTITY],
            build=lambda p: f"top {p.entity_table()} by total {p.monetary_column()}",
            check=lambda plan, p: _check(
                plan,
                expect_agg="SUM",
                metric_role=ROLE_MONETARY,
                group_role=ROLE_ENTITY,
                order="DESC",
                profile=p,
            ),
        ),
        Behavior(
            name="entities filtered by event count (HAVING)",
            requires=[ROLE_ENTITY, ROLE_EVENT],
            build=lambda p: f"{p.entity_table()} with more than 2 {p.event_table()}",
            check=lambda plan, p: _check(
                plan,
                expect_agg="COUNT",
                group_role=ROLE_ENTITY,
                having=True,
                profile=p,
            ),
        ),
    ]


def _check(plan, profile, expect_agg=None, metric_role=None, group_role=None,
           order=None, having=None) -> tuple:
    """Assert role-based expectations against an execution plan."""
    problems = []

    if expect_agg is not None and _agg_function(plan) != expect_agg:
        problems.append(f"aggregation {_agg_function(plan)} != {expect_agg}")

    if metric_role is not None:
        table = _agg_table(plan)
        if not table or metric_role not in profile.role_of(table):
            problems.append(f"metric table '{table}' is not {metric_role}")

    if group_role is not None:
        group_ok = any(group_role in profile.role_of(t) for t in _group_tables(plan))
        if not group_ok:
            problems.append(f"no group-by table with role {group_role} (got {_group_tables(plan)})")

    if order is not None and _order_direction(plan) != order:
        problems.append(f"order direction {_order_direction(plan)} != {order}")

    if having is not None:
        has_having = isinstance(plan.get("having"), dict)
        if has_having != having:
            problems.append(f"having present={has_having}, expected {having}")

    return (not problems, "; ".join(problems) if problems else "ok")


class BehavioralTestSuite:
    """Schema-adaptive behavioral regression suite."""

    def __init__(self, behaviors: Optional[List[Behavior]] = None):
        self.behaviors = behaviors if behaviors is not None else _default_behaviors()

    def run(self, schema: Dict[str, List[str]]) -> Dict:
        """Run all applicable behaviors against a schema. Behaviors whose
        required roles are absent in the schema are skipped (not failed)."""
        profile = SchemaProfile(schema)
        results = {
            "roles": {t: sorted(r) for t, r in profile.roles.items()},
            "total": 0, "passed": 0, "failed": 0, "skipped": 0, "cases": [],
        }

        for behavior in self.behaviors:
            if not all(profile.has_role(r) for r in behavior.requires):
                results["skipped"] += 1
                results["cases"].append({
                    "name": behavior.name, "status": "skipped",
                    "reason": f"schema lacks role(s): {', '.join(behavior.requires)}",
                })
                continue

            results["total"] += 1
            query = ""
            try:
                query = behavior.build(profile)
                plan, sql = _plan_for(query, schema)
                ok, detail = behavior.check(plan, profile)
            except Exception as exc:
                ok, detail, sql = False, f"exception: {exc}", ""

            results["passed" if ok else "failed"] += 1
            results["cases"].append({
                "name": behavior.name,
                "status": "passed" if ok else "failed",
                "query": query,
                "sql": sql,
                "detail": detail,
            })

        results["pass_rate"] = (
            results["passed"] / results["total"] * 100 if results["total"] else 0.0
        )
        return results
