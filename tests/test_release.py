import re
import tomllib
from pathlib import Path

EXPECTED_RELEASE_ACTIONS = {
    "actions/attest-build-provenance": (
        "e8998f949152b193b063cb0ec769d69d929409be",
        "v2.4.0",
    ),
    "actions/checkout": ("11d5960a326750d5838078e36cf38b85af677262", "v4.4.0"),
    "actions/setup-node": ("49933ea5288caeca8642d1e84afbd3f7d6820020", "v4.4.0"),
    "actions/setup-python": ("a26af69be951a213d495a4c3e4e4022e16d87065", "v5.6.0"),
    "astral-sh/setup-uv": ("d0cc045d04ccac9d8b7881df0226f9e82c39688e", "v6.8.0"),
    "docker/build-push-action": (
        "10e90e3645eae34f1e60eeb005ba3a3d33f178e8",
        "v6.19.2",
    ),
    "docker/login-action": ("c94ce9fb468520275223c153574b00df6fe4bcc9", "v3.7.0"),
    "docker/metadata-action": ("c299e40c65443455700f0fdfc63efafe5b349051", "v5.10.0"),
    "docker/setup-buildx-action": (
        "8d2750c68a42422c14e847fe6c8ac0403b4cbd6f",
        "v3.12.0",
    ),
}


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
    assert "gh release create" in workflow
    assert "recover_existing_release" in workflow
    assert "--clobber" not in workflow
    assert "should_recover: ${{ steps.release.outputs.should_recover }}" in workflow
    recovery_gate = (
        'if [ "$release_complete" = true ] && [ "$RECOVER_EXISTING_RELEASE" != true ]; then'
    )
    assert recovery_gate in workflow
    assert "if: needs.prepare.outputs.should_recover == 'true'" in workflow
    assert workflow.index("build and publish container") < workflow.index("tag merge commit")
    assert workflow.index("tag merge commit") < workflow.index("create GitHub release")


def test_release_workflow_pins_external_actions() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()
    uses_lines = re.findall(r"^\s*uses:\s+(.+)$", workflow, re.MULTILINE)
    references = re.findall(
        r"^\s*uses:\s+([^@\s]+)@([0-9a-f]{40})\s+#\s+(v\d+\.\d+\.\d+)\s*$",
        workflow,
        re.MULTILINE,
    )

    assert len(references) == len(uses_lines)
    assert {action for action, _, _ in references} == EXPECTED_RELEASE_ACTIONS.keys()
    for action, revision, version in references:
        assert (revision, version) == EXPECTED_RELEASE_ACTIONS[action]
