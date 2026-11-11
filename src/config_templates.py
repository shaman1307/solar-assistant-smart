"""Named installation config templates (Configuration tab settings)."""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .config import BASE_DIR, _deep_merge, _load_yaml

log = logging.getLogger(__name__)

TEMPLATES_YAML_PATH = BASE_DIR / "config-templates.yaml"
INSTALLED_DEFAULT_TEMPLATE = "Chwarznienska 9kW"
TEMPLATE_NAME_RE = re.compile(r"^[\w\s\-.]{1,64}$", re.UNICODE)

TEMPLATE_ROOT_KEYS: tuple[str, ...] = (
    "debug_tab_enabled",
    "location",
    "solar",
    "inverter",
    "battery",
    "ev",
    "simulation",
    "timer_schedule",
    "grid",
    "load",
    "sa",
)

RUNTIME_ROOT_KEYS: tuple[str, ...] = (
    "smart_mode_enabled",
    "smart_plus_enabled",
    "overrides",
    "plan_overrides",
    "active_template",
    "_charge_rate_kw",
)


def validate_template_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or not TEMPLATE_NAME_RE.match(cleaned):
        raise ValueError("Template name must be 1–64 chars (letters, digits, space, -, .)")
    return cleaned


def extract_template_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in TEMPLATE_ROOT_KEYS:
        if key not in cfg:
            continue
        if key == "sa":
            sa = deepcopy(cfg["sa"])
            sa.pop("password", None)
            if sa:
                out["sa"] = sa
            continue
        out[key] = deepcopy(cfg[key])
    return out


def extract_runtime_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in RUNTIME_ROOT_KEYS:
        if key in cfg:
            out[key] = deepcopy(cfg[key])
    sa = cfg.get("sa") or {}
    if sa.get("password") not in (None, ""):
        out.setdefault("sa", {})["password"] = sa["password"]
    return out


def merge_runtime_onto_template(
    template_payload: dict[str, Any],
    runtime_payload: dict[str, Any],
) -> dict[str, Any]:
    cfg = _deep_merge(template_payload, runtime_payload)
    runtime_sa = runtime_payload.get("sa") or {}
    if runtime_sa.get("password") not in (None, ""):
        cfg.setdefault("sa", {})["password"] = runtime_sa["password"]
    return cfg


def _load_seed_file() -> dict[str, Any]:
    if not TEMPLATES_YAML_PATH.is_file():
        return {"default_template": INSTALLED_DEFAULT_TEMPLATE, "templates": {}}
    data = _load_yaml(TEMPLATES_YAML_PATH)
    templates = data.get("templates") or {}
    default = data.get("default_template") or INSTALLED_DEFAULT_TEMPLATE
    return {"default_template": default, "templates": templates}


def seed_templates_from_yaml(conn) -> None:
    seed = _load_seed_file()
    for name, payload in (seed.get("templates") or {}).items():
        conn.execute(
            """
            INSERT OR IGNORE INTO config_templates(name, payload_json, updated_at)
            VALUES(?, ?, datetime('now'))
            """,
            (name, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO config_template_meta(key, value)
        VALUES('installed_default', ?)
        """,
        (seed.get("default_template") or INSTALLED_DEFAULT_TEMPLATE,),
    )
    conn.commit()


def resolve_active_template_name(
    runtime_cfg: dict[str, Any],
    *,
    installed_default: str,
    template_names: list[str] | None = None,
) -> str:
    if runtime_cfg.get("active_template"):
        return str(runtime_cfg["active_template"])
    if template_names:
        return template_names[0]
    return installed_default


def uses_template_mode(runtime_cfg: dict[str, Any]) -> bool:
    if runtime_cfg.get("active_template"):
        return True
    return "location" not in runtime_cfg and "solar" not in runtime_cfg


def template_meta_for_api(
    *,
    installed_default: str,
    active: str,
    names: list[str],
    explicit_active: str | None = None,
) -> dict[str, Any]:
    return {
        "installed_default": installed_default,
        "active_template": explicit_active,
        "effective_template": active,
        "names": names,
    }
