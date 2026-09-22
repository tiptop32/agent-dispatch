from pathlib import Path


def test_config_example_matches_packaged_defaults():
    # config.example.yaml в корне это копия дефолтов пакета: они не должны разъезжаться.
    root = Path(__file__).parent.parent
    example = (root / "config.example.yaml").read_text()
    default = (root / "agent_dispatch/config_default.yaml").read_text()
    assert example == default
