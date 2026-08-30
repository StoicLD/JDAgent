import subprocess
import sys


def test_importing_jdagent_does_not_import_pymilvus() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import jdagent; "
                "assert 'pymilvus' not in sys.modules; "
                "assert 'jdagent.knowledge.milvus' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
