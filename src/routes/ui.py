"""UI routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import load_config
from ..influxdb import now_warsaw
from ..simulation_config import simulation_form_defaults

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_VALID_TABS = frozenset({"dashboard", "simulation", "config", "debug"})


def _debug_tab_enabled() -> bool:
    return load_config().get("debug_tab_enabled", True) is not False


def _work_mode_options_for_template() -> list[str]:
    opts = load_config().get("sa", {}).get("work_mode_options")
    if isinstance(opts, list) and opts:
        return [str(o) for o in opts]
    return [
        "On-grid",
        "Limit power to UPS load",
        "Limit power to home load",
        "AC coupling",
    ]


def _battery_discharge_mode_options_for_template() -> list[str]:
    opts = load_config().get("sa", {}).get("battery_discharge_mode_options")
    if isinstance(opts, list) and opts:
        return [str(o) for o in opts]
    return [
        "Standby",
        "UPS load only",
        "UPS and home loads",
        "Grid export enabled",
    ]


def _solar_power_priority_options_for_template() -> list[str]:
    opts = load_config().get("sa", {}).get("solar_power_priority_options")
    if isinstance(opts, list) and opts:
        return [str(o) for o in opts]
    return [
        "Load first",
        "Battery first",
        "Grid first",
    ]


def _render_index(request: Request, initial_tab: str = "dashboard") -> HTMLResponse:
    if initial_tab not in _VALID_TABS:
        initial_tab = "dashboard"
    if initial_tab == "debug" and not _debug_tab_enabled():
        initial_tab = "dashboard"
    context = {
        "initial_tab": initial_tab,
        "debug_tab_enabled": _debug_tab_enabled(),
        "today_date": now_warsaw().strftime("%Y-%m-%d"),
        "work_mode_options": _work_mode_options_for_template(),
        "battery_discharge_mode_options": _battery_discharge_mode_options_for_template(),
        "solar_power_priority_options": _solar_power_priority_options_for_template(),
        "simulation_form_defaults": simulation_form_defaults(),
    }
    # Try TemplateResponse(request, name, context); on TypeError use (name, context).
    try:
        response = templates.TemplateResponse(request, "index.html", context)
    except TypeError:
        context["request"] = request
        response = templates.TemplateResponse("index.html", context)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _render_index(request, "dashboard")


@router.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request) -> HTMLResponse:
    return _render_index(request, "simulation")


@router.get("/conf", response_class=HTMLResponse)
async def conf_page(request: Request) -> HTMLResponse:
    return _render_index(request, "config")


@router.get("/debug", response_class=HTMLResponse)
async def debug_page(request: Request) -> HTMLResponse:
    if not _debug_tab_enabled():
        return RedirectResponse(url="/", status_code=302)
    return _render_index(request, "debug")
