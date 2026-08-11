import subprocess
import sys
from pathlib import Path

# REDESIGN.md §28-29: hardcoded, not pluggable — a tool-registration
# mechanism would make this a framework, which is explicitly out of scope
# (§0.2). These four tools operate for real on a small sandboxed toy repo,
# not the Fleet repo itself and not mocked — genuine file I/O and a real
# pytest subprocess run.

SANDBOX_ROOT = (Path(__file__).resolve().parent / "sandbox_repo").resolve()


def _resolve(path: str) -> Path:
    """Resolves `path` relative to the sandbox root and refuses anything
    that would escape it."""
    resolved = (SANDBOX_ROOT / path).resolve()
    if not (resolved == SANDBOX_ROOT or SANDBOX_ROOT in resolved.parents):
        raise ValueError(f"Path {path!r} escapes the sandbox")
    return resolved


def read_file(path: str) -> str:
    return _resolve(path).read_text()


def write_file(path: str, content: str) -> None:
    _resolve(path).write_text(content)


def search_code(query: str) -> list[str]:
    """Substring search across the sandbox — a real, minimal
    implementation (no indexing/AST), not a mock."""
    matches = []
    for file in sorted(SANDBOX_ROOT.rglob("*.py")):
        for lineno, line in enumerate(file.read_text().splitlines(), start=1):
            if query in line:
                matches.append(f"{file.relative_to(SANDBOX_ROOT)}:{lineno}: {line.strip()}")
    return matches


def run_tests() -> dict:
    """Runs pytest against the sandbox for real and returns a small
    structured summary."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "--tb=short"],
        cwd=SANDBOX_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "output": result.stdout + result.stderr,
    }
