import dbbuddy.main as cli


def test_ai_fallback_note_prints_once_to_stderr(capsys):
    cli._ai_fallback_note_printed = False
    result = {"metadata": {"ai_requested": True, "ai_used": False}}

    cli.maybe_print_ai_fallback_note(True, result)
    cli.maybe_print_ai_fallback_note(True, result)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("AI provider unavailable.") == 1
    assert captured.err.count("Using the deterministic query planner instead.") == 1


def test_ai_fallback_note_suppressed_when_ai_not_requested(capsys):
    cli._ai_fallback_note_printed = False
    result = {"metadata": {"ai_requested": False, "ai_used": False}}

    cli.maybe_print_ai_fallback_note(False, result)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_ai_fallback_note_uses_semantic_provenance(capsys):
    cli._ai_fallback_note_printed = False
    fallback_result = {
        "semantic_layer": {
            "users": {"id": {"term": "identifier", "source": "rule"}},
        }
    }

    cli.maybe_print_ai_fallback_note(True, fallback_result)

    captured = capsys.readouterr()
    assert "AI provider unavailable." in captured.err


def test_ai_fallback_note_suppressed_for_empty_semantic_layer(capsys):
    # An empty layer (the query matched no mapped tables) is not evidence that
    # the AI provider fell back — it must not trigger the note.
    cli._ai_fallback_note_printed = False

    cli.maybe_print_ai_fallback_note(True, {"semantic_layer": {}})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_ai_fallback_note_not_printed_when_ai_contributed(capsys):
    cli._ai_fallback_note_printed = False
    ai_result = {
        "semantic_layer": {
            "users": {"id": {"term": "identifier", "source": "ai", "provider": "local"}},
        }
    }

    cli.maybe_print_ai_fallback_note(True, ai_result)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
