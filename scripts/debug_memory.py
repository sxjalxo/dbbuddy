"""
Quick script to debug semantic memory state.
Run this to verify Phase 6 learning is working.
"""

import os
import sys

# Allow running as `python scripts/debug_memory.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbbuddy_core.learning_engine import debug_memory, load_memory  # noqa: E402

if __name__ == "__main__":
    print("DEBUGGING SEMANTIC MEMORY")
    print("=" * 50)

    # Load and display memory
    memory = load_memory()
    print(f"\nMemory file path: {memory}")
    print(f"Total mappings: {len(memory.get('mappings', {}))}")
    print(f"Total columns tracked: {len(memory.get('column_usage', {}))}")
    print(f"Total tables tracked: {len(memory.get('table_usage', {}))}")

    print("\n" + "=" * 50)
    print("DETAILED MEMORY STATE:")
    print("=" * 50)

    debug_memory(memory)
