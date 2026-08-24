"""Exercise the deterministic engine against realistic ERP / domain schemas.

Runs the schema-adaptive layers that don't need a live DB — type classification,
FK/relationship inference, and typed filter / name-literal extraction — against
schemas modelled on real products (SAP, Dynamics, Odoo, ERPNext) and domains
(healthcare, HR, logistics). The point is to *learn where the heuristics break*
on real-world structures (cryptic keys, string PKs, FK-name ≠ table-name), not to
assert pass/fail. Run:  python scripts/exercise_schemas.py
"""

import sys
import pathlib

# This harness prints Unicode (≠, em-dashes) in its labels. On Windows the console
# defaults to cp1252 and crashes with UnicodeEncodeError, so force UTF-8 output.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from dbbuddy_core.intent_builder import (  # noqa: E402
    extract_filters, extract_value_filters, _find_identifier_column,
)
from dbbuddy_core.relationship_graph import build_relationship_graph  # noqa: E402
from dbbuddy_core.type_handlers import classify_sql_type  # noqa: E402


# Each schema: {table: [(column, sql_type), ...]}, plus the joins a human expects
# and a few natural-language probe queries.
SCHEMAS = {
    "SAP (ERP, cryptic keys)": {
        "tables": {
            "VBAK": [("MANDT", "char(3)"), ("VBELN", "char(10)"), ("ERDAT", "date"),
                     ("NETWR", "decimal(15,2)"), ("KUNNR", "char(10)"), ("WAERK", "char(5)")],
            "VBAP": [("MANDT", "char(3)"), ("VBELN", "char(10)"), ("POSNR", "char(6)"),
                     ("MATNR", "char(18)"), ("KWMENG", "decimal(13,3)"), ("NETWR", "decimal(15,2)")],
            "KNA1": [("MANDT", "char(3)"), ("KUNNR", "char(10)"), ("NAME1", "char(35)"),
                     ("LAND1", "char(3)"), ("ORT01", "char(35)")],
        },
        "expected_joins": ["VBAK.VBELN -> VBAP.VBELN", "VBAK.KUNNR -> KNA1.KUNNR"],
        "probes": ["sales orders with NETWR over 1000", "orders created this month",
                   "customer named Acme", "VBAK where WAERK = EUR"],
    },
    "Microsoft Dynamics (PascalCase)": {
        "tables": {
            "CustTable": [("AccountNum", "nvarchar(20)"), ("Name", "nvarchar(100)"),
                          ("CustGroup", "nvarchar(10)"), ("Currency", "nvarchar(3)")],
            "SalesTable": [("SalesId", "nvarchar(20)"), ("CustAccount", "nvarchar(20)"),
                           ("SalesStatus", "int"), ("CreatedDateTime", "datetime")],
        },
        "expected_joins": ["SalesTable.CustAccount -> CustTable.AccountNum"],
        "probes": ["sales created this year", "CustTable where CustGroup = Retail",
                   "customer named Contoso"],
    },
    "Odoo (FK name ≠ table name)": {
        "tables": {
            "res_partner": [("id", "int"), ("name", "varchar"), ("email", "varchar"),
                            ("country_id", "int"), ("is_company", "bool"), ("active", "bool"),
                            ("create_date", "timestamp")],
            "sale_order": [("id", "int"), ("name", "varchar"), ("partner_id", "int"),
                           ("amount_total", "numeric"), ("state", "varchar"), ("date_order", "timestamp")],
            "res_country": [("id", "int"), ("name", "varchar"), ("code", "varchar")],
        },
        "expected_joins": ["sale_order.partner_id -> res_partner.id",
                           "res_partner.country_id -> res_country.id"],
        "probes": ["sale orders with amount_total over 5000", "orders date_order last month",
                   "partner named Acme", "sale_order where state = done"],
    },
    "ERPNext / Frappe (string PK named 'name')": {
        "tables": {
            "tabCustomer": [("name", "varchar(140)"), ("customer_name", "varchar(140)"),
                            ("customer_group", "varchar(140)"), ("territory", "varchar(140)")],
            "tabSalesInvoice": [("name", "varchar(140)"), ("customer", "varchar(140)"),
                                ("posting_date", "date"), ("grand_total", "decimal(18,6)"),
                                ("status", "varchar(30)")],
        },
        "expected_joins": ["tabSalesInvoice.customer -> tabCustomer.name"],
        "probes": ["sales invoices with grand_total over 10000", "invoices posting this month",
                   "customer named Acme", "tabSalesInvoice where status = Paid"],
    },
    "Healthcare (composite names)": {
        "tables": {
            "patients": [("patient_id", "int"), ("mrn", "varchar(20)"), ("first_name", "varchar(50)"),
                         ("last_name", "varchar(50)"), ("dob", "date"), ("gender", "char(1)")],
            "encounters": [("encounter_id", "int"), ("patient_id", "int"), ("admit_date", "date"),
                           ("discharge_date", "date"), ("department", "varchar(50)")],
            "observations": [("obs_id", "int"), ("encounter_id", "int"), ("code", "varchar(20)"),
                             ("value_num", "decimal(10,2)"), ("observed_at", "timestamp")],
        },
        "expected_joins": ["encounters.patient_id -> patients.patient_id",
                           "observations.encounter_id -> encounters.encounter_id"],
        "probes": ["encounters admitted this year", "observations with value_num > 140",
                   "patient named Alice", "encounters where department = Cardiology"],
    },
    "HR": {
        "tables": {
            "employees": [("emp_id", "int"), ("first_name", "varchar(50)"), ("last_name", "varchar(50)"),
                          ("hire_date", "date"), ("dept_id", "int"), ("salary", "decimal(12,2)")],
            "departments": [("dept_id", "int"), ("dept_name", "varchar(100)"), ("manager_id", "int")],
        },
        "expected_joins": ["employees.dept_id -> departments.dept_id"],
        "probes": ["employees with salary over 90000", "employees hired last year",
                   "employee named Bob", "salary between 50000 and 80000"],
    },
    "Logistics": {
        "tables": {
            "shipments": [("shipment_id", "int"), ("tracking_no", "varchar(40)"), ("carrier_id", "int"),
                          ("origin", "varchar(80)"), ("destination", "varchar(80)"), ("status", "varchar(20)"),
                          ("shipped_at", "timestamp"), ("weight_kg", "decimal(10,2)")],
            "carriers": [("carrier_id", "int"), ("carrier_name", "varchar(80)")],
        },
        "expected_joins": ["shipments.carrier_id -> carriers.carrier_id"],
        "probes": ["shipments with weight_kg over 100", "shipments shipped past 7 days",
                   "shipments where status = delivered", "shipments to destination Berlin"],
    },
    "Star schema / warehouse (_key FKs)": {
        "tables": {
            "fact_sales": [("date_key", "int"), ("product_key", "int"), ("customer_key", "int"),
                           ("store_key", "int"), ("quantity", "int"), ("amount", "decimal(15,2)")],
            "dim_customer": [("customer_key", "int"), ("customer_name", "varchar(120)"),
                             ("segment", "varchar(40)"), ("region", "varchar(40)")],
            "dim_product": [("product_key", "int"), ("product_name", "varchar(120)"),
                            ("category", "varchar(40)"), ("unit_price", "decimal(12,2)")],
        },
        "expected_joins": ["fact_sales.customer_key -> dim_customer.customer_key",
                           "fact_sales.product_key -> dim_product.product_key"],
        "probes": ["sales with amount over 500", "fact_sales where quantity > 10",
                   "customer named Acme", "sales in region West"],
    },
    "Banking (2 FKs to same table + self-ref)": {
        "tables": {
            "accounts": [("account_id", "int"), ("account_name", "varchar(80)"), ("balance", "decimal(18,2)"),
                         ("opened_at", "date"), ("owner_id", "int")],
            "transfers": [("transfer_id", "int"), ("from_account_id", "int"), ("to_account_id", "int"),
                          ("amount", "decimal(18,2)"), ("transferred_at", "timestamp"), ("status", "varchar(20)")],
            "customers": [("customer_id", "int"), ("full_name", "varchar(120)")],
        },
        "expected_joins": ["transfers.from_account_id -> accounts.account_id",
                           "transfers.to_account_id -> accounts.account_id",
                           "accounts.owner_id -> customers.customer_id"],
        "probes": ["transfers with amount over 1000", "transfers this month",
                   "accounts with balance over 5000", "transfers where status = pending"],
    },
    "E-commerce (junction + conventional _id)": {
        "tables": {
            "customers": [("customer_id", "int"), ("name", "varchar(120)"), ("email", "varchar(120)"),
                          ("created_at", "timestamp")],
            "orders": [("order_id", "int"), ("customer_id", "int"), ("order_date", "date"),
                       ("total", "decimal(12,2)"), ("status", "varchar(20)")],
            "order_items": [("order_id", "int"), ("product_id", "int"), ("quantity", "int"),
                            ("unit_price", "decimal(12,2)")],
            "products": [("product_id", "int"), ("product_name", "varchar(120)"), ("price", "decimal(12,2)"),
                         ("in_stock", "boolean"), ("category", "varchar(40)")],
        },
        "expected_joins": ["orders.customer_id -> customers.customer_id",
                           "order_items.order_id -> orders.order_id",
                           "order_items.product_id -> products.product_id"],
        "probes": ["orders with total over 100", "products where category = electronics",
                   "in stock products", "orders this week"],
    },
    "Postgres modern (uuid / jsonb / bool / array)": {
        "tables": {
            "events": [("id", "uuid"), ("name", "varchar(120)"), ("payload", "jsonb"),
                       ("tags", "text[]"), ("created_at", "timestamptz"), ("is_public", "boolean"),
                       ("priority", "smallint")],
            "app_users": [("id", "uuid"), ("email", "varchar(120)"), ("metadata", "jsonb"),
                          ("active", "boolean"), ("signup_date", "date")],
        },
        "expected_joins": ["(none declared)"],
        "probes": ["events with priority > 3", "active users", "public events",
                   "users signed up last month", "events created after 2024-01-01"],
    },
}

# Capability probes: a single clean schema, queries grouped by the SQL feature
# they *should* produce. Shows which predicate kinds the NL layer can/can't emit.
_CAP_TABLES = {
    "orders": [("order_id", "int"), ("customer", "varchar(80)"), ("status", "varchar(20)"),
               ("amount", "decimal(12,2)"), ("amount_usd", "decimal(12,2)"), ("discount_pct", "decimal(5,2)"),
               ("weight_kg", "decimal(10,2)"), ("email", "varchar(120)"), ("created_at", "timestamp"),
               ("is_paid", "boolean")],
}
CAPABILITY_PROBES = {
    "equality (baseline)":     "orders where status = shipped",
    "numeric comparison":      "orders with amount over 100",
    "currency symbol":         "orders with amount over $100",
    "thousands separator":     "orders with amount over 1,000",
    "percentage":              "orders with discount_pct over 10%",
    "unit-suffixed column":    "orders with weight over 100",       # column is weight_kg
    "money-suffixed column":   "orders with amount over 100 usd",   # column is amount_usd
    "boolean predicate":       "paid orders",                        # is_paid = true
    "boolean explicit":        "orders where is_paid = true",
    "IN list":                 "orders where status in (paid, pending)",
    "negation":                "orders where status is not cancelled",
    "NULL / missing":          "orders with no email",
    "LIKE / contains":         "orders where email contains gmail",
    "multiple filters":        "orders where status = paid and amount over 500",
}


def _to_plain(tables):
    return {t: [c for c, _ in cols] for t, cols in tables.items()}


def _to_types(tables):
    return {t: {c: ty for c, ty in cols} for t, cols in tables.items()}


def run():
    for label, spec in SCHEMAS.items():
        schema = _to_plain(spec["tables"])
        types = _to_types(spec["tables"])
        print("=" * 78)
        print(label)
        print("=" * 78)

        # 1) FK / relationship inference (schema-only heuristic: col endswith _id).
        graph = build_relationship_graph(schema)
        found = sorted(
            f"{t}.{fk} -> {ref}.{refcol}"
            for t, edges in graph.items() for fk, (ref, refcol) in edges.items()
        )
        print(f"  Joins expected : {spec['expected_joins']}")
        print(f"  Joins inferred : {found or '(none)'}")

        # 2) Human-identifier column per table (drives name-literal lookups).
        idents = {t: _find_identifier_column(t, schema) for t in schema}
        print(f"  Name columns   : {idents}")

        # 3) Filter / comparison / name-literal extraction per probe.
        print("  Probes:")
        for q in spec["probes"]:
            filters = extract_filters(q, schema, types)
            names = [f for f in extract_value_filters(q, schema, list(schema)) if f not in filters]
            allf = filters + names
            print(f"    {q!r}")
            print(f"        -> {allf or '(no filter)'}")
        print()


def run_capabilities():
    print("#" * 78)
    print("# CAPABILITY MATRIX — which predicate kinds the NL layer can emit")
    print("#" * 78)
    schema = _to_plain(_CAP_TABLES)
    types = _to_types(_CAP_TABLES)
    for label, q in CAPABILITY_PROBES.items():
        filters = extract_filters(q, schema, types)
        names = [f for f in extract_value_filters(q, schema, list(schema)) if f not in filters]
        allf = filters + names
        print(f"  {label:24} {q!r}")
        print(f"      -> {allf or '(no filter)'}")
    print()
    print("  Exotic type classification:")
    for ty in ["uuid", "jsonb", "text[]", "timestamptz", "boolean", "smallint", "money", "numeric(10,2)"]:
        print(f"    {ty:16} -> {classify_sql_type(ty)}")


if __name__ == "__main__":
    run()
    run_capabilities()
