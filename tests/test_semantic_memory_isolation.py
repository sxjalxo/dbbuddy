"""Semantic memory must not cross database boundaries.

Learned mappings used to live in one flat ``{term: {target: count}}`` map shared
by every database the process ever touched. Two customers whose schemas both have
a "price" concept — ``products.price`` in one, ``products.unit_price`` in the
other — taught the same term contradictory targets, and whichever was seen more
often was injected into *both*. That is a tenant isolation defect, not a tuning
problem: the fix has to make the leak impossible, not unlikely.
"""

import json

import pytest

from dbbuddy_core import learning_engine as le
from dbbuddy_core.semantic_enhancer import enhance_query


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(le, "MEMORY_FILE", tmp_path / "semantic_memory.json")
    yield


def _learn(scope: str, term: str, target: str, times: int = 3):
    memory = le.load_memory(scope)
    for _ in range(times):
        le.add_mapping(memory, term, target)
    le.save_memory(memory, scope)


SCHEMA_A = {"products": ["id", "name", "price"]}
SCHEMA_B = {"products": ["id", "name", "unit_price"]}


class TestScopeIsolation:
    def test_a_mapping_learned_on_one_database_is_invisible_to_another(self):
        _learn("hostA|shop|mysql", "price", "products.price")

        assert le.get_best_mapping("price", le.load_memory("hostA|shop|mysql")) == "products.price"
        assert le.get_best_mapping("price", le.load_memory("hostB|erp|mysql")) is None

    def test_saving_one_scope_leaves_the_others_intact(self):
        _learn("hostA|shop|mysql", "price", "products.price")
        _learn("hostB|erp|mysql", "price", "products.unit_price")

        assert le.get_best_mapping("price", le.load_memory("hostA|shop|mysql")) == "products.price"
        assert le.get_best_mapping("price", le.load_memory("hostB|erp|mysql")) == "products.unit_price"

    def test_frequency_counts_do_not_pool_across_databases(self):
        # Pooled counts let a term learned often on one database cross the
        # learning threshold instantly on every other — the mapping arrives
        # "already trusted" without that database ever having taught it.
        _learn("hostA|shop|mysql", "price", "products.price", times=50)
        memory_b = le.load_memory("hostB|erp|mysql")
        assert le.get_best_mapping("price", memory_b, threshold=2) is None

    def test_unscoped_learning_never_leaks_into_a_named_database(self):
        _learn(le.DEFAULT_SCOPE, "price", "products.price")
        assert le.get_best_mapping("price", le.load_memory("hostA|shop|mysql")) is None

    def test_scope_is_derived_from_host_database_and_engine(self):
        class _Cfg:
            host, database, engine = "Db.Internal", "Shop", "MySQL"

        assert le.memory_scope(_Cfg()) == "db.internal|shop|mysql"
        assert le.memory_scope(None) == le.DEFAULT_SCOPE


class TestV1Migration:
    def test_unattributable_v1_mappings_are_discarded(self, tmp_path):
        # v1 recorded no owner, so every entry is unattributable. Assigning one
        # would be a guess, and the wrong guess is the bug being fixed.
        le.MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        le.MEMORY_FILE.write_text(json.dumps({
            "version": "v1",
            "mappings": {"price": {"products.price": 40}},
            "column_usage": {}, "table_usage": {},
        }), encoding="utf-8")

        assert le.get_best_mapping("price", le.load_memory("hostA|shop|mysql")) is None
        assert le.get_best_mapping("price", le.load_memory(le.DEFAULT_SCOPE)) is None

    def test_a_corrupt_memory_file_does_not_break_a_query(self):
        le.MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        le.MEMORY_FILE.write_text("{not json", encoding="utf-8")
        assert le.load_memory("hostA|shop|mysql")["mappings"] == {}


class TestEnhancerBoundary:
    def test_the_enhancer_reads_only_its_own_scope(self):
        _learn("hostA|shop|mysql", "price", "products.price")

        # Same term, same *valid* column name in both schemas — schema validation
        # alone cannot tell these apart, which is why scoping is the primary
        # boundary and validation the backstop.
        out_a = enhance_query("total price", schema=SCHEMA_A, scope="hostA|shop|mysql")
        out_b = enhance_query("total price", schema=SCHEMA_A, scope="hostB|erp|mysql")

        assert "products.price" in out_a
        assert out_b == "total price"

    def test_an_injection_absent_from_the_active_schema_is_dropped(self):
        _learn("hostA|shop|mysql", "price", "products.price")

        # Right scope, wrong schema: the backstop catches a memory written before
        # scoping existed, or a column dropped since it was learned.
        out = enhance_query("total price", schema=SCHEMA_B, scope="hostA|shop|mysql")
        assert "products.price" not in out

    def test_enhancement_is_additive_and_preserves_the_original_text(self):
        _learn("hostA|shop|mysql", "price", "products.price")
        out = enhance_query("total Price", schema=SCHEMA_A, scope="hostA|shop|mysql")
        # Capitalization survives — the intent builder uses it to spot name literals.
        assert out.startswith("total Price")
