import re
import tomllib
from pathlib import Path


def test_release_version_sources_agree() -> None:
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    init = Path("src/kassette/__init__.py").read_text()
    match = re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE)
    lock = tomllib.loads(Path("uv.lock").read_text())
    locked = [item["version"] for item in lock["package"] if item["name"] == "kassette"]

    assert match is not None
    assert match.group(1) == version
    assert locked == [version]
    assert f"## {version}\n" in Path("CHANGELOG.md").read_text()


def test_release_workflow_publishes_before_tagging() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()

    assert "contents: write" in workflow
    assert "packages: write" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert "uv build" in workflow
    assert "push: true" in workflow
    assert "actions/attest-build-provenance@v2" in workflow
    assert "gh release create" in workflow
    assert workflow.index("build and publish container") < workflow.index("tag merge commit")
    assert workflow.index("tag merge commit") < workflow.index("create GitHub release")
