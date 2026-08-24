"""Internal data-contract enforcement.

The pipeline guarantees certain shapes between stages (e.g. the planner returns
a dict execution plan). In production these are recovered leniently so a single
bad stage cannot crash a request. Enabling STRICT_MODE (config.STRICT_MODE /
env DBBUDDY_STRICT=1) turns those silent recoveries into loud failures, which is
what you want in tests and CI to catch contract regressions early.
"""

import logging

logger = logging.getLogger(__name__)


class ContractViolation(RuntimeError):
    """Raised when an internal data contract is violated and STRICT_MODE is on."""


def fail_contract(message: str) -> None:
    """Signal a contract violation.

    In strict mode this raises ContractViolation. Otherwise it logs and returns,
    letting the caller fall back to its lenient recovery path.
    """
    # Imported lazily so the flag can be toggled (e.g. in tests) before use.
    from dbbuddy_core.config import STRICT_MODE

    if STRICT_MODE:
        raise ContractViolation(message)
    logger.warning(f"Contract violation (recovered): {message}")
