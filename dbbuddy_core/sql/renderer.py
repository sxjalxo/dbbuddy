"""Value rendering for SQL fragments.

The parameterized path binds values through the driver (``%s`` placeholders); the
inline path — used for display and validation, never execution — needs literals.
``render_literal`` owns that inline rendering.
"""

from typing import Any


def render_literal(value: Any) -> str:
    """Render a value as an inline SQL literal (display / validation path only).

    Strings are single-quoted with embedded quotes escaped; everything else is
    stringified as-is. Never used for execution — the parameterized path binds
    values through the driver instead.
    """
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)
