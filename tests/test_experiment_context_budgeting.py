import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks" / "experiments"))

import context_budgeting as exp  # noqa: E402


def test_build_context_pool_includes_filler_and_calculator_file():
    items = exp.build_context_pool()
    assert len(items) == len(exp.FILLER_TOPICS) + 1
    file_items = [i for i in items if i.source == "calculator.py"]
    assert len(file_items) == 1
    assert "def add" in file_items[0].content


def test_build_context_pool_file_item_has_higher_importance_than_filler():
    items = exp.build_context_pool()
    file_item = next(i for i in items if i.source == "calculator.py")
    filler_items = [i for i in items if i.source != "calculator.py"]
    assert all(file_item.importance > f.importance for f in filler_items)
    assert all(file_item.relevance > f.relevance for f in filler_items)


def test_task_succeeded_detects_correct_diagnosis():
    assert exp.task_succeeded("The function should add but instead it subtracts the values.")
    assert exp.task_succeeded("It uses the minus operator instead of plus.")


def test_task_succeeded_false_for_unrelated_or_wrong_response():
    assert not exp.task_succeeded("I don't see any issues with this code.")
    assert not exp.task_succeeded("The weather today is sunny and warm.")
