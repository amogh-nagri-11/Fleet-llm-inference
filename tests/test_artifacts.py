from context.artifacts import Artifact, ArtifactStore, ArtifactType, summarize_text
from context.manager import ContextManager
from context.models import ContextType


# ── summarize_text ───────────────────────────────────────────

def test_summarize_prefers_keyword_matched_lines():
    content = "\n".join([f"line {i}" for i in range(20)] + ["FAIL: test_auth_expired_token"])
    summary = summarize_text(content)
    assert "FAIL: test_auth_expired_token" in summary
    assert "21 lines total" in summary
    assert "1 matched" in summary


def test_summarize_falls_back_to_first_lines_when_no_match():
    content = "\n".join(f"line {i}" for i in range(20))
    summary = summarize_text(content)
    assert "line 0" in summary
    assert "first 10 shown" in summary


def test_summarize_notes_hidden_count_when_truncated():
    content = "\n".join(f"ERROR line {i}" for i in range(50))
    summary = summarize_text(content, max_lines=5)
    assert "50 matched" in summary
    assert "... (45 more not shown)" in summary


def test_summarize_no_truncation_note_when_everything_shown():
    content = "\n".join(f"line {i}" for i in range(3))
    summary = summarize_text(content, max_lines=10)
    assert "not shown" not in summary


def test_summarize_single_line_content():
    summary = summarize_text("just one line")
    assert "just one line" in summary


# ── Artifact / ArtifactStore ──────────────────────────────────

def test_artifact_computes_summary_and_token_count_on_creation():
    content = "x" * 4000
    artifact = Artifact(type=ArtifactType.LOG, content=content)
    assert artifact.summary
    assert artifact.token_count == 1000


def test_store_create_and_get():
    store = ArtifactStore()
    artifact = store.create(ArtifactType.TOOL_OUTPUT, "line1\nline2\nline3", source="run_tests")
    fetched = store.get(artifact.id)
    assert fetched is artifact
    assert fetched.source == "run_tests"


def test_store_get_missing_returns_none():
    store = ArtifactStore()
    assert store.get("nope") is None


def test_store_get_excerpt_returns_line_range():
    store = ArtifactStore()
    content = "\n".join(f"line {i}" for i in range(100))
    artifact = store.create(ArtifactType.LOG, content)

    excerpt = store.get_excerpt(artifact.id, 10, 13)
    assert excerpt == "line 10\nline 11\nline 12"


def test_store_get_excerpt_missing_artifact_returns_none():
    store = ArtifactStore()
    assert store.get_excerpt("nope", 0, 5) is None


def test_to_reference_item_is_small_and_carries_artifact_id():
    store = ArtifactStore()
    large_content = "\n".join(f"noise line {i}" for i in range(1000)) + "\nFAIL: something broke"
    artifact = store.create(ArtifactType.TOOL_OUTPUT, large_content, source="run_tests")

    ref = store.to_reference_item(artifact, workflow_id="wf-1", agent_id="agent-1")

    assert ref.type == ContextType.SUMMARY
    assert ref.artifact_id == artifact.id
    assert ref.workflow_id == "wf-1"
    assert ref.agent_id == "agent-1"
    # The whole point: reference is much smaller than the original.
    assert ref.token_count < artifact.token_count / 10


# ── ContextManager integration ────────────────────────────────

def test_manager_record_artifact_stores_reference_not_raw_content():
    manager = ContextManager()
    large_content = "\n".join(f"noise {i}" for i in range(2000)) + "\nERROR: boom"

    item = manager.record_artifact(
        large_content, ArtifactType.TOOL_OUTPUT, workflow_id="wf-1", source="run_tests"
    )

    assert item.artifact_id is not None
    assert item.content != large_content
    assert item.token_count < estimate_tokens_of(large_content) / 10

    candidates = manager.get_candidate_context("wf-1")
    assert candidates == [item]


def test_manager_get_artifact_roundtrips_full_content():
    manager = ContextManager()
    large_content = "full original content " * 500

    item = manager.record_artifact(large_content, ArtifactType.DOCUMENT, workflow_id="wf-1")

    fetched = manager.get_artifact(item.artifact_id)
    assert fetched.content == large_content


def test_manager_get_artifact_excerpt():
    manager = ContextManager()
    content = "\n".join(f"line {i}" for i in range(50))

    item = manager.record_artifact(content, ArtifactType.LOG, workflow_id="wf-1")

    excerpt = manager.get_artifact_excerpt(item.artifact_id, 0, 3)
    assert excerpt == "line 0\nline 1\nline 2"


def estimate_tokens_of(text: str) -> int:
    return max(1, len(text) // 4)
