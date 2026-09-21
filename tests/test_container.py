from pathlib import Path


def test_container_runs_hosted_as_non_root_with_healthcheck() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "python:3.12.13-slim-bookworm" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "USER kassette" in dockerfile
    assert "EXPOSE 7860" in dockerfile
    assert "/healthz" in dockerfile
    assert 'CMD ["kassette", "serve", "--hosted"]' in dockerfile
    assert dockerfile.index("USER kassette") < dockerfile.index("CMD [")


def test_container_context_excludes_local_secrets() -> None:
    dockerignore = Path(".dockerignore").read_text().splitlines()

    assert ".env" in dockerignore
    assert ".git" in dockerignore
    assert ".venv" in dockerignore
