from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

from agent_landing import __version__


ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = "0.1.0a1"


def test_release_version_is_consistent() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packaged_skill = (
        ROOT / "src/agent_landing/assets/claude/landing/SKILL.md"
    ).read_text(encoding="utf-8")
    repository_skill = (ROOT / ".claude/skills/landing/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert metadata["project"]["version"] == RELEASE_VERSION
    assert __version__ == RELEASE_VERSION
    assert f"version: {RELEASE_VERSION}" in packaged_skill
    assert f"version: {RELEASE_VERSION}" in repository_skill


def test_release_metadata_uses_spdx_license_expression() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["license"] == "MIT"
    assert "License :: OSI Approved :: MIT License" not in metadata["project"].get(
        "classifiers", []
    )


def test_repository_has_no_codex_skill_or_handoff_artifact() -> None:
    assert not (ROOT / ".agents/skills/landing").exists()
    assert not list(ROOT.glob("AGENT_HANDOFF*.md"))


def test_source_distribution_includes_release_and_test_support_files() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()

    assert "include CHANGELOG.md" in manifest
    assert "include .claude/skills/landing/SKILL.md" in manifest
    assert "recursive-include docs *.md" in manifest
    assert "recursive-include tests *.py" in manifest
