import pytest
from pydantic import ValidationError

from ragany_dcs_gateway_sdk import ConfigError, GatewayConfig


def test_defaults_match_protocol_doc():
    cfg = GatewayConfig()
    assert cfg.endpoint_url("BrowseTag") == "http://192.168.1.88/soap/BrowseTag"
    assert cfg.endpoint_url("ReadTag") == "http://192.168.1.88/soap/ReadTag"
    assert cfg.auth_headers() == {}


@pytest.mark.parametrize(
    "base_url, prefix, expected",
    [
        ("http://10.0.0.1/", "/soap", "http://10.0.0.1/soap/ReadTag"),
        ("  https://gw.local  ", "soap/", "https://gw.local/soap/ReadTag"),
        ("http://10.0.0.1:8080", "", "http://10.0.0.1:8080/ReadTag"),
    ],
)
def test_url_normalisation(base_url, prefix, expected):
    assert GatewayConfig(base_url=base_url, api_prefix=prefix).endpoint_url("ReadTag") == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": ""},
        {"base_url": "192.168.1.88"},
        {"connect_timeout": 0},
        {"max_retries": -1},
        {"read_batch_size": 0},
        {"unknown_option": 1},
    ],
)
def test_invalid_config_raises_config_error(kwargs):
    with pytest.raises(ConfigError):
        GatewayConfig.create(**kwargs)


def test_token_auth_header_and_secret_masking():
    cfg = GatewayConfig(token="s3cret")
    assert cfg.auth_headers() == {"Authorization": "Bearer s3cret"}
    assert "s3cret" not in repr(cfg)

    raw = GatewayConfig(token="abc", auth_header="X-Token", auth_scheme="")
    assert raw.auth_headers() == {"X-Token": "abc"}

    assert GatewayConfig(token="   ").token is None


def test_config_is_immutable():
    cfg = GatewayConfig()
    with pytest.raises(ValidationError):
        cfg.base_url = "http://x"  # type: ignore[misc]


def test_from_env_with_overrides():
    env = {
        "RAGANY_DCS_BASE_URL": "http://10.1.1.1",
        "RAGANY_DCS_TOKEN": "tok",
        "RAGANY_DCS_MAX_RETRIES": "5",
        "RAGANY_DCS_POLL_INTERVAL": "2.5",
        "RAGANY_DCS_RETRY_ON_STATUS": "503, 504",
        "UNRELATED": "x",
    }
    cfg = GatewayConfig.from_env(env, read_timeout=30)
    assert cfg.base_url == "http://10.1.1.1"
    assert cfg.token is not None and cfg.token.get_secret_value() == "tok"
    assert cfg.max_retries == 5
    assert cfg.poll_interval == 2.5
    assert cfg.retry_on_status == frozenset({503, 504})
    assert cfg.read_timeout == 30


def test_from_env_invalid_values():
    with pytest.raises(ConfigError):
        GatewayConfig.from_env({"RAGANY_DCS_MAX_RETRIES": "many"})
    with pytest.raises(ConfigError):
        GatewayConfig.from_env({"RAGANY_DCS_RETRY_ON_STATUS": "50x"})
