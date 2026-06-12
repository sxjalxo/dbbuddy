# ── Demo Query Suite ───────────────────────────────────────────────────────
"""
Curated demo queries to prove system capability across complexity tiers.

This is designed for presentation and benchmarking, showing the system's
ability to handle everything from simple selects to complex behavioral analytics.
"""

DEMO_QUERIES = {
    # 🔥 Tier 1 — Basic (sanity + correctness)
    "tier1_basic": [
        {
            "query": "List all users",
            "expected_type": "simple_select",
            "description": "Basic SELECT without WHERE clause",
            "complexity": "low"
        },
        {
            "query": "Show all products and their prices",
            "expected_type": "simple_select",
            "description": "SELECT with multiple columns",
            "complexity": "low"
        },
        {
            "query": "Get all completed orders",
            "expected_type": "filter",
            "description": "SELECT with WHERE clause",
            "complexity": "low"
        }
    ],

    # 🔥 Tier 2 — Intermediate (joins + filters)
    "tier2_intermediate": [
        {
            "query": "List users along with their order amounts",
            "expected_type": "join",
            "description": "JOIN between users and orders",
            "complexity": "medium"
        },
        {
            "query": "Show products purchased in each order",
            "expected_type": "join",
            "description": "Multi-table JOIN (orders, order_items, products)",
            "complexity": "medium"
        },
        {
            "query": "Get users from India",
            "expected_type": "filter",
            "description": "WHERE with string comparison",
            "complexity": "medium"
        },
        {
            "query": "Show orders with status pending",
            "expected_type": "filter",
            "description": "WHERE with enum-like status",
            "complexity": "medium"
        }
    ],

    # 🔥 Tier 3 — Advanced (this is where we shine)
    "tier3_advanced": [
        {
            "query": "Total revenue generated",
            "expected_type": "aggregation",
            "description": "SUM aggregation",
            "complexity": "high"
        },
        {
            "query": "Revenue per user",
            "expected_type": "aggregation",
            "description": "SUM with GROUP BY",
            "complexity": "high"
        },
        {
            "query": "Top 2 customers by spending",
            "expected_type": "group_by",
            "description": "GROUP BY + SUM + ORDER BY + LIMIT",
            "complexity": "high"
        },
        {
            "query": "Most purchased product",
            "expected_type": "aggregation",
            "description": "COUNT with GROUP BY + ORDER BY",
            "complexity": "high"
        },
        {
            "query": "Orders with more than one item",
            "expected_type": "aggregation",
            "description": "HAVING clause with COUNT",
            "complexity": "high"
        }
    ],

    # 🔥 Tier 4 — Behavioral Analytics (differentiator)
    "tier4_behavioral": [
        {
            "query": "Users who logged in from mobile",
            "expected_type": "filter",
            "description": "Event filtering with device type",
            "complexity": "very_high"
        },
        {
            "query": "Most common device used",
            "expected_type": "aggregation",
            "description": "COUNT on event device field",
            "complexity": "very_high"
        },
        {
            "query": "Users who made a purchase after login",
            "expected_type": "join",
            "description": "Event sequence analysis (login → purchase)",
            "complexity": "very_high"
        },
        {
            "query": "Number of events per user",
            "expected_type": "aggregation",
            "description": "COUNT with GROUP BY on events table",
            "complexity": "very_high"
        }
    ]
}

# Flattened list for benchmarking
ALL_DEMO_QUERIES = []
for tier, queries in DEMO_QUERIES.items():
    for q in queries:
        q["tier"] = tier
        ALL_DEMO_QUERIES.append(q)

# Demo flow script for presentation
DEMO_FLOW = [
    {
        "step": 1,
        "title": "Simple",
        "query": "List all users",
        "purpose": "Fast, correct baseline",
        "expected_behavior": "Simple SELECT without WHERE"
    },
    {
        "step": 2,
        "title": "Join",
        "query": "Users with their order amounts",
        "purpose": "Shows join reasoning",
        "expected_behavior": "JOIN between users and orders"
    },
    {
        "step": 3,
        "title": "Aggregation",
        "query": "Top customers by spending",
        "purpose": "Shows grouping + sum",
        "expected_behavior": "GROUP BY + SUM + ORDER BY + LIMIT"
    },
    {
        "step": 4,
        "title": "Complex",
        "query": "Most purchased product",
        "purpose": "Shows multi-table reasoning",
        "expected_behavior": "Multi-table JOIN with COUNT + GROUP BY"
    },
    {
        "step": 5,
        "title": "Behavioral",
        "query": "Users who logged in from mobile",
        "purpose": "Shows events table usage",
        "expected_behavior": "Event filtering with device type"
    },
    {
        "step": 6,
        "title": "Hard query",
        "query": "Users who purchased after login",
        "purpose": "Show confidence + reasoning",
        "expected_behavior": "Event sequence analysis with transparency"
    }
]

# Hard query for failure transparency demo
HARD_QUERY = {
    "query": "Users who purchased electronics after logging in from mobile",
    "expected_confidence": "medium",
    "reasoning": [
        "Multi-table join inferred",
        "Event-order relationship assumed",
        "Complex temporal sequence"
    ]
}


def get_demo_query_by_step(step_number):
    """Get query from demo flow by step number."""
    for step in DEMO_FLOW:
        if step["step"] == step_number:
            return step
    return None


def get_tier_queries(tier_name):
    """Get all queries for a specific tier."""
    return DEMO_QUERIES.get(tier_name, [])


def get_all_queries():
    """Get all demo queries flattened."""
    return ALL_DEMO_QUERIES
