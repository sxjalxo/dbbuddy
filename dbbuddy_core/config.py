"""Global configuration for DBBuddy.

This module provides centralized configuration settings for the entire system,
including debug flags and other global parameters.

Phase 16.2 of the production polish upgrade.
"""

import os

# Global debug flag - single source of truth for debug mode
DEBUG = False

# Strict contract mode. When True, internal contract violations (e.g. the planner
# returning a non-dict execution plan) raise ContractViolation instead of being
# silently recovered. Off by default to preserve lenient production behavior;
# flip via env DBBUDDY_STRICT=1 (recommended for tests/CI).
STRICT_MODE = os.getenv("DBBUDDY_STRICT", "0").strip().lower() in ("1", "true", "yes", "on")

# Logging configuration
LOG_LEVEL = "DEBUG" if DEBUG else "WARNING"
LOG_FILE = None  # Set to file path to enable file logging

# Cache configuration
CACHE_ENABLED = True
CACHE_TTL_QUERY = 300  # 5 minutes
CACHE_TTL_RETRIEVAL = 600  # 10 minutes
CACHE_TTL_PLAN = 600  # 10 minutes

# Vector store configuration
VECTOR_STORE_ENABLED = True
# Overridable so throwaway work (benchmarks, harnesses, CI) can index into a
# scratch directory instead of the developer's real vector store.
CHROMADB_PERSIST_DIRECTORY = os.getenv("CHROMADB_PERSIST_DIRECTORY", "./chroma_db")

# Adaptive improvement configuration
ADAPTIVE_IMPROVEMENT_ENABLED = True
PLANNER_CORRECTION_ENABLED = True
CONFIDENCE_ADJUSTMENT_ENABLED = True

# System intelligence configuration
SYSTEM_INTELLIGENCE_ENABLED = True
INTELLIGENCE_STORAGE_PATH = "dbbuddy_core/intelligence_data.json"

# API configuration
API_DEBUG_MODE = DEBUG
API_RETURN_DEBUG_INFO = DEBUG

# Performance configuration
MAX_RETRIEVAL_RESULTS = 10
MIN_CONFIDENCE_THRESHOLD = 0.7
