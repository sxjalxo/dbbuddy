"""The Insights Engine — evidence-bound explanations of executed query results.

Deliberately *not* a chatbot. A chatbot answers anything; an insights engine
answers only from the evidence it was handed. That distinction is the whole
design: the engine never sees a database, never produces SQL, and every claim it
makes must cite a value from the result set it is explaining.

The deterministic planner → Predicate AST → dialect compiler path remains the
only producer of SQL. This package receives already-executed output.

See ``docs/ARCHITECTURE.md`` ("Insights Engine") for the design, and
``docs/SECURITY.md`` ("AI output validation") for why the validators — not the
prompts — are the boundary.
"""

from .models import (
    ColumnProfile,
    Finding,
    InsightBundle,
    InsightsSettings,
    ResultContext,
    settings,
)
from .service import answer_followup, generate_insights

__all__ = [
    "ColumnProfile",
    "Finding",
    "InsightBundle",
    "InsightsSettings",
    "ResultContext",
    "answer_followup",
    "generate_insights",
    "settings",
]
