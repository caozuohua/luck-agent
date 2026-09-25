from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_image_ships_production_runtime_and_uses_hashed_dependencies():
    # This regression previously built an image missing main.py's runtime import.
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert "runtime/" not in ignored
    assert ".venv/" in ignored
    assert "*.db" in ignored
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert (ROOT / "runtime/graph_runtime.py").exists()
