"""Configuration template store and merge."""

from src.config import load_runtime_config, save_config
from src.config_templates import (
    INSTALLED_DEFAULT_TEMPLATE,
    extract_template_payload,
    merge_runtime_onto_template,
    resolve_active_template_name,
    validate_template_name,
)
from src.sqlite_store import (
    load_config_template,
    reset_connection_for_tests,
)


def test_validate_template_name():
    assert validate_template_name("Chwarznienska 9kW") == "Chwarznienska 9kW"
    try:
        validate_template_name("")
        assert False
    except ValueError:
        pass


def test_extract_template_strips_password():
    payload = extract_template_payload({
        "location": {"latitude": 1.0},
        "sa": {"host": "localhost", "password": "secret"},
        "plan_overrides": {"2026-01-01": {}},
    })
    assert payload["location"]["latitude"] == 1.0
    assert "password" not in (payload.get("sa") or {})
    assert "plan_overrides" not in payload


def test_seed_template_loaded(tmp_path, monkeypatch):
    db_path = tmp_path / "solar_smart.db"
    yaml_path = tmp_path / "config-templates.yaml"
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.config_templates.TEMPLATES_YAML_PATH", yaml_path)
    yaml_path.write_text(
        'default_template: "Chwarznienska 9kW"\ntemplates:\n  "Chwarznienska 9kW":\n    location:\n      latitude: 54.5\n',
        encoding="utf-8",
    )
    reset_connection_for_tests()
    from src.sqlite_store import _connect

    _connect()
    tpl = load_config_template("Chwarznienska 9kW")
    assert tpl is not None
    assert tpl["location"]["latitude"] == 54.5


def test_merge_runtime_password_wins():
    merged = merge_runtime_onto_template(
        {"sa": {"host": "localhost"}},
        {"sa": {"password": "pw"}},
    )
    assert merged["sa"]["password"] == "pw"
    assert merged["sa"]["host"] == "localhost"


def test_save_config_splits_template_and_runtime(tmp_path, monkeypatch):
    cfg_yaml = tmp_path / "sa-config.yaml"
    db_path = tmp_path / "solar_smart.db"
    yaml_path = tmp_path / "config-templates.yaml"
    monkeypatch.setenv("SOLAR_CONFIG_PATH", str(cfg_yaml))
    monkeypatch.setattr("src.sqlite_store._DB_PATH", db_path)
    monkeypatch.setattr("src.config_templates.TEMPLATES_YAML_PATH", yaml_path)
    yaml_path.write_text(
        'default_template: "Chwarznienska 9kW"\ntemplates:\n  "Chwarznienska 9kW":\n    location:\n      latitude: 1.0\n',
        encoding="utf-8",
    )
    reset_connection_for_tests()
    from src.sqlite_store import _connect

    _connect()

    save_config({
        "active_template": "Chwarznienska 9kW",
        "location": {"latitude": 54.5, "longitude": 18.5},
        "smart_mode_enabled": True,
        "sa": {"password": "secret"},
    })
    runtime = load_runtime_config()
    assert runtime.get("active_template") == "Chwarznienska 9kW"
    assert runtime.get("smart_mode_enabled") is True
    assert runtime.get("location") is None
    tpl = load_config_template("Chwarznienska 9kW")
    assert tpl["location"]["latitude"] == 54.5
    assert "password" not in tpl.get("sa", {})


def test_resolve_active_template_name():
    installed = INSTALLED_DEFAULT_TEMPLATE
    assert resolve_active_template_name({"active_template": "A"}, installed_default=installed) == "A"
    assert resolve_active_template_name({}, installed_default=installed, template_names=["Z", "A"]) == "Z"
    assert resolve_active_template_name({}, installed_default=installed) == installed
    assert resolve_active_template_name({}, installed_default=installed, template_names=[]) == installed
