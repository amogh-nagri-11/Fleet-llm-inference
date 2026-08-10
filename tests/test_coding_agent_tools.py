import pytest

from examples.coding_agent import tools


@pytest.fixture
def preserve_calculator():
    """write_file() mutates a real file in the sandbox — restore it after
    so the demo always starts from its intentionally-buggy state, and so
    other tests in this file don't see each other's writes."""
    original = tools.read_file("calculator.py")
    yield
    tools.write_file("calculator.py", original)


def test_read_file_returns_real_content():
    content = tools.read_file("calculator.py")
    assert "def add(a, b):" in content


def test_write_file_then_read_file_roundtrips(preserve_calculator):
    tools.write_file("calculator.py", "def add(a, b):\n    return a + b\n")
    assert tools.read_file("calculator.py") == "def add(a, b):\n    return a + b\n"


def test_search_code_finds_matching_lines():
    matches = tools.search_code("def add")
    assert any("calculator.py" in m and "def add" in m for m in matches)


def test_search_code_no_match_returns_empty():
    assert tools.search_code("this_string_does_not_exist_anywhere") == []


def test_path_traversal_is_rejected():
    with pytest.raises(ValueError):
        tools.read_file("../../../../etc/passwd")


def test_path_traversal_with_absolute_path_is_rejected():
    with pytest.raises(ValueError):
        tools.read_file("/etc/passwd")


def test_run_tests_fails_on_the_intentional_bug(preserve_calculator):
    # calculator.py ships with add() returning a - b instead of a + b.
    result = tools.run_tests()
    assert result["passed"] is False
    assert "test_add" in result["output"]


def test_run_tests_passes_after_fix(preserve_calculator):
    fixed = tools.read_file("calculator.py").replace(
        "def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b"
    )
    tools.write_file("calculator.py", fixed)

    result = tools.run_tests()
    assert result["passed"] is True
