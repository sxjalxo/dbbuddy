"""DB Buddy - deterministic NL-to-SQL CLI and semantic-layer engine."""

# Single source of truth for the version. pyproject.toml derives from this via
# [tool.setuptools.dynamic]  version = {attr = "dbbuddy.__version__"}, so the
# packaged metadata and `dbbuddy --version` can never drift apart.
__version__ = "1.0.0"
