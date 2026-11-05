import tomllib
from pathlib import Path


def test_package_can_be_imported() -> None:
    import jdagent

    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project.open("rb") as stream:
        declared = tomllib.load(stream)["project"]["version"]
    assert jdagent.__version__ == declared
