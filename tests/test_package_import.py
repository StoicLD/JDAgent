def test_package_can_be_imported() -> None:
    import jdagent

    assert jdagent.__version__ == "0.2.0"
