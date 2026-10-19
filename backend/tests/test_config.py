from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize("port", [7497, 7496])
def test_ib_port_from_environment(monkeypatch, port):
    monkeypatch.setenv("IB_PORT", str(port))
    settings = Settings(_env_file=None)
    assert settings.ib_port == port
    assert isinstance(settings.ib_port, int)


@pytest.mark.parametrize("port", [7497, 7496])
def test_explicit_integer_ib_port_remains_supported(port):
    assert Settings(_env_file=None, ib_port=port).ib_port == port


@pytest.mark.parametrize("port", ["4002", "0", "65536", "not-a-port", "7497.0", ""])
def test_invalid_environment_ib_port_is_rejected(monkeypatch, port):
    monkeypatch.setenv("IB_PORT", port)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)
    assert error.value.errors()[0]["loc"] == ("ib_port",)


def test_default_ib_port(monkeypatch):
    monkeypatch.delenv("IB_PORT", raising=False)
    assert Settings(_env_file=None).ib_port == 7497


def test_documented_dotenv_template_loads(monkeypatch):
    monkeypatch.delenv("IB_PORT", raising=False)
    template = Path(__file__).resolve().parents[2] / ".env.example"
    settings = Settings(_env_file=template)
    assert settings.ib_port == 7497
    assert isinstance(settings.ib_port, int)
