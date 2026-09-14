"""
SolarAssistant client — reads metrics and writes settings via SA local REST API.

Uses py_solar_assistant.DeviceClient which connects via HTTP Basic Auth:
  admin:<sa_web_password>

SA must have a web password configured (Configuration → Security).
Set the password in sa-config.yaml under sa.password.
The host should include the port if SA is not on port 80:
  sa.host: "localhost:8080"   (non-default port example; default host is "localhost" on port 80)
  sa.password: "your_password"

All public functions return safe defaults when SA is unreachable,
so the app keeps running with cached/forecast data even if SA is offline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

_INVERTER_PREFIX = "inverter_1"
_SLOT_COUNT = 3
_WORK_MODE_DEFAULT_OPTIONS = (
    "On-grid",
    "Limit power to UPS load",
    "Limit power to home load",
    "AC coupling",
)
_BATTERY_DISCHARGE_MODE_DEFAULT_OPTIONS = (
    "Standby",
    "UPS load only",
    "UPS and home loads",
    "Grid export enabled",
)
# Map alternate discharge-mode labels to exact SA enum strings.
_BATTERY_DISCHARGE_MODE_WRITE_ALIASES: dict[str, str] = {
    "UPS loads only": "UPS load only",
    "Grid sell": "Grid export enabled",
}
_SOLAR_POWER_PRIORITY_DEFAULT_OPTIONS = (
    "Load first",
    "Battery first",
    "Grid first",
)
_GRID_VOLTAGE_TOPICS = tuple(
    f"{_INVERTER_PREFIX}/grid_voltage_{n}" for n in (1, 2, 3)
)
WORK_MODE_ON_GRID = "On-grid"
WORK_MODE_LIMIT_HOME_LOAD = "Limit power to home load"
BATTERY_DISCHARGE_MODE_GRID_EXPORT = "Grid export enabled"
BATTERY_DISCHARGE_MODE_UPS_AND_HOME = "UPS and home loads"
WORK_MODE_BATTERY_DISCHARGE_PAIR: dict[str, str] = {
    WORK_MODE_ON_GRID: BATTERY_DISCHARGE_MODE_GRID_EXPORT,
    WORK_MODE_LIMIT_HOME_LOAD: BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
}
BATTERY_DISCHARGE_WORK_MODE_PAIR: dict[str, str] = {
    v: k for k, v in WORK_MODE_BATTERY_DISCHARGE_PAIR.items()
}
WORK_MODE_VERIFY_TIMEOUT_S = 90.0
BATTERY_DISCHARGE_VERIFY_TIMEOUT_S = 90.0
GRID_CHARGE_VERIFY_TIMEOUT_S = 90.0
WORK_MODE_VERIFY_INTERVAL_S = 5.0
ENUM_VERIFY_FIRST_PAUSE_S = 3.0
_SA_CLIENT_TIMEOUT_S = 35.0
_SA_TIMEOUT_S = 30
_SA_LOCK_WAIT_S = 2.0
_RULES_CACHE_TTL_S = 120
_METRICS_CACHE_TTL_S = 20
_RULES_GLOB = f"{_INVERTER_PREFIX}/*"
_LIVE_METRIC_TOPICS = (
    "battery_1/*",
    "total/pv_power",
    "total/load_power",
    "total/grid_power",
    "total/pv_energy",
    "total/load_energy",
    "total/grid_energy_in",
    "total/grid_energy_out",
    "total/battery_energy_in",
    "total/battery_energy_out",
)
# SA daily energy topics (used when present in API response).
_SA_ENERGY_TOPICS: dict[str, str] = {
    "pv_energy_today": "total/pv_energy",
    "load_energy_today": "total/load_energy",
    "grid_buy_energy": "total/grid_energy_in",
    "grid_sell_energy": "total/grid_energy_out",
    "battery_charged_today": "total/battery_energy_in",
    "battery_discharged_today": "total/battery_energy_out",
}
# Serialize all SA REST calls — beam.smp on Pi Zero cannot handle concurrent requests.
_sa_lock: asyncio.Lock | None = None
_rules_cache: dict[str, Any] | None = None
_rules_cache_ts: float = 0.0
_metrics_cache: dict[str, Any] | None = None
_metrics_cache_ts: float = 0.0
_rules_fetch_lock: asyncio.Lock | None = None
_enum_setting_lock: asyncio.Lock | None = None


def _get_sa_lock() -> asyncio.Lock:
    global _sa_lock
    if _sa_lock is None:
        _sa_lock = asyncio.Lock()
    return _sa_lock


def _get_rules_fetch_lock() -> asyncio.Lock:
    global _rules_fetch_lock
    if _rules_fetch_lock is None:
        _rules_fetch_lock = asyncio.Lock()
    return _rules_fetch_lock


def _get_enum_setting_lock() -> asyncio.Lock:
    global _enum_setting_lock
    if _enum_setting_lock is None:
        _enum_setting_lock = asyncio.Lock()
    return _enum_setting_lock


def invalidate_rules_cache() -> None:
    global _rules_cache, _rules_cache_ts
    _rules_cache = None
    _rules_cache_ts = 0.0


async def _acquire_sa_lock(wait_s: float = _SA_LOCK_WAIT_S) -> bool:
    try:
        await asyncio.wait_for(_get_sa_lock().acquire(), timeout=wait_s)
        return True
    except asyncio.TimeoutError:
        return False


def _release_sa_lock() -> None:
    lock = _get_sa_lock()
    if lock.locked():
        lock.release()


async def _get_metrics(client: Any, *topics: str) -> list[Any]:
    """One HTTP request per topic/glob; discovery off for smaller/faster payloads."""
    return await asyncio.wait_for(
        client.get_metrics(*topics, discovery=False),
        timeout=_SA_TIMEOUT_S,
    )


def _empty_rules() -> dict[str, Any]:
    return {
        "grid_charge_enabled": False,
        "grid_export_enabled": False,
        "charge_current_a": 0.0,
        "timed_charge_enabled": None,
        "timed_discharge_enabled": None,
        "charge_slots": [],
        "discharge_slots": [],
        "work_mode": None,
        "work_mode_options": list(_WORK_MODE_DEFAULT_OPTIONS),
        "battery_discharge_mode": None,
        "battery_discharge_mode_options": list(_BATTERY_DISCHARGE_MODE_DEFAULT_OPTIONS),
        "solar_power_priority": None,
        "solar_power_priority_options": list(_SOLAR_POWER_PRIORITY_DEFAULT_OPTIONS),
        "grid_voltage_1": None,
        "grid_voltage_2": None,
        "grid_voltage_3": None,
        "sa_online": False,
    }

_DEFAULTS: dict[str, Any] = {
    "battery_soc": None,
    "battery_power": 0.0,
    "pv_power": 0.0,
    "load_power": 0.0,
    "grid_power": 0.0,
    "pv_energy_today": 0.0,
    "load_energy_today": 0.0,
    "grid_buy_energy": 0.0,
    "grid_sell_energy": 0.0,
    "sa_online": False,
}


def _work_mode_topic(cfg: dict) -> str:
    return cfg["sa"]["settings"].get("work_mode", f"{_INVERTER_PREFIX}/work_mode")


def _battery_discharge_mode_topic(cfg: dict) -> str:
    return cfg["sa"]["settings"].get(
        "battery_discharge_mode",
        f"{_INVERTER_PREFIX}/battery_discharge_mode",
    )


def _solar_power_priority_topic(cfg: dict) -> str:
    return cfg["sa"]["settings"].get(
        "solar_power_priority",
        f"{_INVERTER_PREFIX}/solar_power_priority",
    )


def _work_mode_options(cfg: dict) -> list[str]:
    opts = cfg.get("sa", {}).get("work_mode_options")
    if isinstance(opts, list) and opts:
        return [str(o) for o in opts]
    return list(_WORK_MODE_DEFAULT_OPTIONS)


def _battery_discharge_mode_options(cfg: dict) -> list[str]:
    opts = cfg.get("sa", {}).get("battery_discharge_mode_options")
    if isinstance(opts, list) and opts:
        return [_normalize_battery_discharge_mode_for_sa(str(o)) for o in opts]
    return list(_BATTERY_DISCHARGE_MODE_DEFAULT_OPTIONS)


def _normalize_battery_discharge_mode_for_sa(mode: str) -> str:
    """Map UI/config labels to exact SA inverter enum string."""
    m = str(mode).strip()
    return _BATTERY_DISCHARGE_MODE_WRITE_ALIASES.get(m, m)


def _solar_power_priority_options(cfg: dict) -> list[str]:
    opts = cfg.get("sa", {}).get("solar_power_priority_options")
    if isinstance(opts, list) and opts:
        return [str(o) for o in opts]
    return list(_SOLAR_POWER_PRIORITY_DEFAULT_OPTIONS)


def _build_client(cfg: dict):
    """Instantiate DeviceClient from config."""
    from py_solar_assistant import DeviceClient  # type: ignore

    sa = cfg["sa"]
    host = sa.get("host", "localhost")
    password = sa.get("password") or None
    if not password:
        raise ValueError(
            "SA password is not configured. "
            "Set a password in SA (Configuration → Security) and add it to sa-config.yaml."
        )
    return DeviceClient(host, password=password, timeout=_SA_CLIENT_TIMEOUT_S)


def _parse_metrics(raw_metrics: list, topic_map: dict[str, str]) -> dict[str, Any]:
    """Convert py_solar_assistant Metric objects into a flat dict.

    Uses m.topic (the actual MQTT-style topic, e.g. 'battery_1/state_of_charge')
    as the lookup key, NOT the human-readable m.name ('State of charge').
    """
    by_topic: dict[str, Any] = {}
    for m in raw_metrics:
        try:
            by_topic[m.topic] = m.value
        except Exception:
            pass

    result: dict[str, Any] = {}
    for key, topic in topic_map.items():
        val = by_topic.get(topic)
        result[key] = float(val) if val is not None else _DEFAULTS.get(key, 0.0)

    for key, topic in _SA_ENERGY_TOPICS.items():
        val = by_topic.get(topic)
        if val is not None:
            result[key] = float(val)

    result["_sa_grid_meter"] = (
        "total/grid_energy_in" in by_topic and "total/grid_energy_out" in by_topic
    )
    return result


def _grid_power_w_from_sa(
    raw_w: float,
    pv_w: float,
    load_w: float,
    battery_w: float,
    *,
    sa_grid_meter: bool,
    epsilon: float = 40.0,
) -> float:
    """Live grid power (W): import negative, export positive.

    Prefer signed ``total/grid_power`` when sa_grid_meter; otherwise sign
    gross-positive W from PV/load/battery balance.
    """
    raw = float(raw_w)
    if sa_grid_meter:
        return -raw if abs(raw) >= epsilon else 0.0
    if raw < -epsilon:
        return -raw
    return _signed_grid_power_w(raw, pv_w, load_w, battery_w, epsilon=epsilon)


def _signed_grid_power_w(
    grid_gross_w: float,
    pv_w: float,
    load_w: float,
    battery_w: float,
    *,
    epsilon: float = 40.0,
) -> float:
    """Sign gross-positive grid power from PV/load/battery balance."""
    gross = abs(float(grid_gross_w))
    if gross < epsilon:
        return 0.0
    bat_in = max(float(battery_w), 0.0)
    bat_out = max(-float(battery_w), 0.0)
    net = float(load_w) + bat_in - float(pv_w) - bat_out
    if net > epsilon:
        return -gross
    if net < -epsilon:
        return gross
    raw = float(grid_gross_w)
    if raw < -epsilon:
        return raw
    return 0.0


async def get_live_metrics(cfg: dict, *, fresh: bool = False) -> dict[str, Any]:
    """Return current live metrics from SA (with online flag)."""
    global _metrics_cache, _metrics_cache_ts

    now = time.time()
    if (
        not fresh
        and _metrics_cache is not None
        and _metrics_cache_ts > now - _METRICS_CACHE_TTL_S
    ):
        return dict(_metrics_cache)

    topic_map: dict[str, str] = cfg["sa"]["metrics"]
    if not await _acquire_sa_lock():
        if _metrics_cache:
            return dict(_metrics_cache)
        return dict(_DEFAULTS)

    try:
        client = _build_client(cfg)
        async with client as c:
            raw = await _get_metrics(c, *_LIVE_METRIC_TOPICS)
        data = _parse_metrics(raw, topic_map)
        sa_grid_meter = bool(data.pop("_sa_grid_meter", False))
        data["grid_power"] = _grid_power_w_from_sa(
            data.get("grid_power", 0.0),
            data.get("pv_power", 0.0),
            data.get("load_power", 0.0),
            data.get("battery_power", 0.0),
            sa_grid_meter=sa_grid_meter,
        )
        data["sa_online"] = True
        _metrics_cache = dict(data)
        _metrics_cache_ts = time.time()
        return data
    except Exception as exc:
        log.warning("SA metrics fetch failed: %r", exc)
        if _metrics_cache:
            stale = dict(_metrics_cache)
            stale["sa_online"] = False
            return stale
        return dict(_DEFAULTS)
    finally:
        _release_sa_lock()


async def get_rules(cfg: dict, *, fresh: bool = False) -> dict[str, Any]:
    """Return current inverter rule/settings state and timer schedule slots."""
    global _rules_cache, _rules_cache_ts

    now = time.time()
    if not fresh and _rules_cache is not None and _rules_cache_ts > now - _RULES_CACHE_TTL_S:
        return _rules_cache

    async with _get_rules_fetch_lock():
        if not fresh and _rules_cache is not None and _rules_cache_ts > now - _RULES_CACHE_TTL_S:
            return _rules_cache

        settings: dict[str, str] = cfg["sa"]["settings"]
        if not await _acquire_sa_lock(wait_s=60.0 if fresh else _SA_LOCK_WAIT_S):
            if _rules_cache is not None:
                stale = dict(_rules_cache)
                stale["stale"] = True
                return stale
            return _empty_rules()

        try:
            client = _build_client(cfg)
            async with client as c:
                # Single glob — py-solar-assistant does one HTTP call per topic arg;
                # listing 40+ slot topics caused 40+ sequential requests and froze the app.
                raw = await _get_metrics(c, _RULES_GLOB)
            by_topic: dict[str, Any] = {m.topic: m.value for m in raw}
            schedule = _parse_timer_schedule(by_topic)
            work_topic = _work_mode_topic(cfg)
            work_mode_raw = by_topic.get(work_topic)
            work_mode = str(work_mode_raw).strip() if work_mode_raw is not None else None
            bdm_topic = _battery_discharge_mode_topic(cfg)
            bdm_raw = by_topic.get(bdm_topic)
            battery_discharge_mode = str(bdm_raw).strip() if bdm_raw is not None else None
            spp_topic = _solar_power_priority_topic(cfg)
            spp_raw = by_topic.get(spp_topic)
            solar_power_priority = str(spp_raw).strip() if spp_raw is not None else None
            grid_voltages: dict[str, float | None] = {}
            for n, topic in enumerate(_GRID_VOLTAGE_TOPICS, start=1):
                raw = by_topic.get(topic)
                grid_voltages[f"grid_voltage_{n}"] = (
                    round(_float(raw), 1) if raw is not None else None
                )
            result = {
                "grid_charge_enabled": _truthy(by_topic.get(settings["grid_charge_switch"])),
                "grid_export_enabled": _truthy(by_topic.get(settings["grid_export_switch"])),
                "charge_current_a": _float(by_topic.get(settings["charge_current_limit"]), 0.0),
                "timed_charge_enabled": schedule["timed_charge_enabled"],
                "timed_discharge_enabled": schedule["timed_discharge_enabled"],
                "charge_slots": schedule["charge_slots"],
                "discharge_slots": schedule["discharge_slots"],
                "work_mode": work_mode,
                "work_mode_options": _work_mode_options(cfg),
                "battery_discharge_mode": battery_discharge_mode,
                "battery_discharge_mode_options": _battery_discharge_mode_options(cfg),
                "solar_power_priority": solar_power_priority,
                "solar_power_priority_options": _solar_power_priority_options(cfg),
                **grid_voltages,
                "sa_online": True,
            }
            _rules_cache = result
            _rules_cache_ts = time.time()
            return result
        except Exception as exc:
            log.warning("SA rules fetch failed: %r", exc)
            if _rules_cache is not None:
                stale = dict(_rules_cache)
                stale["sa_online"] = False
                stale["stale"] = True
                return stale
            return _empty_rules()
        finally:
            _release_sa_lock()


async def _read_inverter_setting(cfg: dict, topic: str) -> str | None:
    """Read one inverter setting via SA REST (used for post-write verify)."""
    if not await _acquire_sa_lock(wait_s=60.0):
        log.warning("SA read %s skipped — lock busy", topic)
        return None
    try:
        client = _build_client(cfg)
        async with client as c:
            raw = await _get_metrics(c, topic)
        if not raw:
            return None
        val = raw[0].value
        return str(val).strip() if val is not None else None
    except Exception as exc:
        log.warning("SA read %s failed: %r", topic, exc)
        return None
    finally:
        _release_sa_lock()


async def _wait_inverter_setting_confirmed(
    cfg: dict,
    *,
    topic: str,
    value: str,
    label: str,
    verify_timeout_s: float,
) -> bool:
    """Poll a single SA topic until it matches (REST can lag 30–70s behind SA UI)."""
    deadline = time.monotonic() + max(verify_timeout_s, ENUM_VERIFY_FIRST_PAUSE_S)
    await asyncio.sleep(ENUM_VERIFY_FIRST_PAUSE_S)
    while time.monotonic() < deadline:
        invalidate_rules_cache()
        after = await _read_inverter_setting(cfg, topic)
        if after == value:
            log.info("%s set to %s (SA confirmed)", label, value)
            return True
        if after is not None:
            log.info("%s pending — want %r, SA has %r", label, value, after)
        else:
            log.info("%s pending — want %r, SA read unavailable", label, value)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(WORK_MODE_VERIFY_INTERVAL_S, remaining))
    log.error("%s verify timeout after %.0fs — wanted %r", label, verify_timeout_s, value)
    return False


async def _write_metrics(cfg: dict, writes: list[tuple[str, str]], *, lock_wait_s: float = 90.0) -> None:
    """Write SA settings via WebSocket (REST POST returns 500 on some SA/SRNE builds)."""
    if not writes:
        return
    if not await _acquire_sa_lock(wait_s=lock_wait_s):
        raise TimeoutError("SolarAssistant busy")

    host = cfg["sa"]["host"]
    password = cfg["sa"]["password"]
    try:
        from py_solar_assistant import Options, connect

        sock = await asyncio.wait_for(
            connect(Options(local_ip=host, password=password)),
            timeout=10,
        )
        try:
            for topic, value in writes:
                log.info("SA write %s=%s", topic, value)
                await asyncio.wait_for(sock.set_setting(topic, value), timeout=25)
        finally:
            await sock.close()
    except Exception as ws_exc:
        log.warning("SA WebSocket write failed, trying REST: %r", ws_exc)
        client = _build_client(cfg)
        async with client as c:
            for topic, value in writes:
                await asyncio.wait_for(c.set_metric(topic, value), timeout=15)
    finally:
        _release_sa_lock()


# SRNE via SolarAssistant rejects these timer topics (API 422 Unknown register).
# Writing them aborts the whole batch before grid_charge_switch is applied.
_SRNE_UNSUPPORTED_TIMER_TOPICS = frozenset({
    f"{_INVERTER_PREFIX}/charge_using_grid_slot_{n}" for n in range(1, _SLOT_COUNT + 1)
} | {
    f"{_INVERTER_PREFIX}/charge_using_generator_slot_{n}" for n in range(1, _SLOT_COUNT + 1)
})

# SRNE rejects max_grid_charge_current above this (422 Rejected) and aborts the
# whole timer batch — Chg never reaches timed_charge / slot registers.
_MAX_GRID_CHARGE_CURRENT_A = 100
# Match charge_battery_voltage_slot (import Chg target). At 48 V, 6 kW → 125 A
# (422 Rejected); at ≥54 V amps stay within SA's 100 A accept path.
_GRID_CHARGE_BUS_V = 58.0


def _grid_charge_current_a(power_kw: float, *, max_a: int = _MAX_GRID_CHARGE_CURRENT_A) -> int:
    """Amps for SA grid-charge current limit from planned AC kW (charge bus V)."""
    if power_kw <= 0:
        return 0
    raw = int((float(power_kw) * 1000.0) / _GRID_CHARGE_BUS_V)
    return max(1, min(int(max_a), raw))


def work_mode_battery_modes_paired(
    work_mode: str | None,
    battery_discharge_mode: str | None,
) -> bool:
    """True when work mode and battery discharge mode form a known SRNE pair."""
    wm = str(work_mode or "").strip()
    bdm = _normalize_battery_discharge_mode_for_sa(str(battery_discharge_mode or ""))
    if not wm or not bdm:
        return False
    return WORK_MODE_BATTERY_DISCHARGE_PAIR.get(wm) == bdm


def _build_schedule_writes(
    schedule: dict[str, Any],
    *,
    charge_slot_nums: tuple[int, ...] | None = None,
    discharge_slot_nums: tuple[int, ...] | None = None,
    settings: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Build SA metric writes for timer schedule (skip unsupported SRNE topics)."""
    if charge_slot_nums is None:
        charge_slot_nums = (1, 2, 3)
    if discharge_slot_nums is None:
        discharge_slot_nums = (1, 2, 3)
    p = _INVERTER_PREFIX
    writes: list[tuple[str, str]] = []
    # Policy: Grid charge Enabled only while Timed charge is active; else Disabled.
    # Slot power is ignored when max_grid_charge_current is stale (e.g. 34 A from
    # a 2 kW UI toggle). Write amps from slot kW, capped at 100 A so SA does not 422.
    if settings is not None and schedule.get("timed_charge_enabled") is not None:
        writes.append(
            (
                settings["grid_charge_switch"],
                _grid_charge_switch(bool(schedule["timed_charge_enabled"])),
            )
        )
        if schedule.get("timed_charge_enabled"):
            slot_kw = max(
                (
                    float(s.get("power_kw") or 0)
                    for s in schedule.get("charge_slots") or []
                    if int(s.get("slot") or 0) in charge_slot_nums
                ),
                default=0.0,
            )
            if slot_kw > 0 and settings.get("charge_current_limit"):
                writes.append(
                    (
                        settings["charge_current_limit"],
                        str(_grid_charge_current_a(slot_kw)),
                    )
                )
    if schedule.get("timed_charge_enabled") is not None:
        writes.append((f"{p}/timed_charge", _sa_switch(bool(schedule["timed_charge_enabled"]))))
    if schedule.get("timed_discharge_enabled") is not None:
        writes.append((f"{p}/timed_discharge", _sa_switch(bool(schedule["timed_discharge_enabled"]))))
    for slot in schedule.get("charge_slots", []):
        n = int(slot["slot"])
        if n not in charge_slot_nums:
            continue
        cap = int(round(_float(slot.get("capacity_pct"), 0)))
        if cap < 1:
            cap = 1
        pwr_w = int(round(_float(slot.get("power_kw"), 0) * 1000))
        writes.extend([
            (f"{p}/charge_start_slot_{n}", _time_to_sa(slot.get("from"))),
            (f"{p}/charge_end_slot_{n}", _time_to_sa(slot.get("to"))),
            (f"{p}/charge_battery_capacity_slot_{n}", str(cap)),
            (f"{p}/charge_battery_voltage_slot_{n}", str(_float(slot.get("voltage_v"), _GRID_CHARGE_BUS_V))),
            (f"{p}/charge_power_slot_{n}", str(pwr_w)),
        ])
    for slot in schedule.get("discharge_slots", []):
        n = int(slot["slot"])
        if n not in discharge_slot_nums:
            continue
        cap = int(round(_float(slot.get("capacity_pct"), 0)))
        if cap < 1:
            cap = 1
        pwr_w = int(round(_float(slot.get("power_kw"), 0) * 1000))
        writes.extend([
            (f"{p}/discharge_start_slot_{n}", _time_to_sa(slot.get("from"))),
            (f"{p}/discharge_end_slot_{n}", _time_to_sa(slot.get("to"))),
            (f"{p}/discharge_battery_capacity_slot_{n}", str(cap)),
            (f"{p}/discharge_battery_voltage_slot_{n}", str(_float(slot.get("voltage_v"), 42.0))),
            (f"{p}/discharge_power_slot_{n}", str(pwr_w)),
        ])
    return [(t, v) for t, v in writes if t not in _SRNE_UNSUPPORTED_TIMER_TOPICS]


async def set_grid_charging(
    cfg: dict,
    *,
    enabled: bool,
    power_kw: float = 0.0,
    current_a: int | None = None,
    verify: bool = True,
    verify_timeout_s: float = GRID_CHARGE_VERIFY_TIMEOUT_S,
) -> bool:
    """Enable or disable SA grid-charging rule; optionally wait for SA confirm (30–90s)."""
    settings: dict[str, str] = cfg["sa"]["settings"]
    topic = settings["grid_charge_switch"]
    target = _grid_charge_switch(enabled)
    writes: list[tuple[str, str]] = [(topic, target)]
    amps: int | None = None
    if current_a is not None:
        amps = max(1, min(_MAX_GRID_CHARGE_CURRENT_A, int(current_a)))
    elif enabled and power_kw > 0:
        amps = _grid_charge_current_a(power_kw)
    if amps is not None and settings.get("charge_current_limit"):
        writes.append((settings["charge_current_limit"], str(amps)))
    try:
        await _write_metrics(cfg, writes, lock_wait_s=90.0)
        log.info(
            "Grid charging %s (%.1f kW, %s A)",
            "ON" if enabled else "OFF",
            power_kw,
            amps if amps is not None else "—",
        )
        if not verify:
            invalidate_rules_cache()
            return True
        ok = await _wait_inverter_setting_confirmed(
            cfg,
            topic=topic,
            value=target,
            label="Grid charge",
            verify_timeout_s=verify_timeout_s,
        )
        if ok:
            invalidate_rules_cache()
        return ok
    except Exception as exc:
        log.error("Failed to set grid charging: %r", exc)
        return False


async def ensure_paired_battery_discharge_mode(
    cfg: dict,
    work_mode: str,
    *,
    verify_timeout_s: float = BATTERY_DISCHARGE_VERIFY_TIMEOUT_S,
) -> bool:
    """Set battery discharge mode paired with work mode; wait for SA confirm (30–60s)."""
    target = WORK_MODE_BATTERY_DISCHARGE_PAIR.get(work_mode)
    if not target:
        return True
    topic = _battery_discharge_mode_topic(cfg)
    current = await _read_inverter_setting(cfg, topic)
    if current == target:
        log.info(
            "Battery discharge mode already %r (paired with work mode %r)",
            target,
            work_mode,
        )
        return True
    log.info(
        "Battery discharge mode %r → %r (paired with work mode %r)",
        current,
        target,
        work_mode,
    )
    return await _set_inverter_enum_setting(
        cfg,
        topic=topic,
        value=target,
        rules_field="battery_discharge_mode",
        label="Battery discharge mode",
        verify=True,
        verify_timeout_s=verify_timeout_s,
    )


async def ensure_paired_work_mode_for_battery(
    cfg: dict,
    battery_mode: str,
    *,
    verify_timeout_s: float = WORK_MODE_VERIFY_TIMEOUT_S,
) -> bool:
    """Set work mode required by battery discharge mode before writing battery."""
    required = BATTERY_DISCHARGE_WORK_MODE_PAIR.get(battery_mode)
    if not required:
        return True
    topic = _work_mode_topic(cfg)
    current = await _read_inverter_setting(cfg, topic)
    if current == required:
        log.info(
            "Work mode already %r (paired with battery discharge %r)",
            required,
            battery_mode,
        )
        return True
    log.info(
        "Work mode %r → %r before battery discharge %r",
        current,
        required,
        battery_mode,
    )
    return await _set_inverter_enum_setting(
        cfg,
        topic=topic,
        value=required,
        rules_field="work_mode",
        label="Work mode",
        verify=True,
        verify_timeout_s=verify_timeout_s,
    )


async def set_grid_export(cfg: dict, *, enabled: bool) -> bool:
    """Enable or disable SA timed discharge (grid export schedule)."""
    settings: dict[str, str] = cfg["sa"]["settings"]
    try:
        await _write_metrics(
            cfg,
            [(settings["grid_export_switch"], _sa_switch(enabled))],
            lock_wait_s=90.0,
        )
        log.info("Grid export %s", "ON" if enabled else "OFF")
        return True
    except Exception as exc:
        log.error("Failed to set grid export: %r", exc)
        return False


async def set_work_mode(
    cfg: dict,
    mode: str,
    *,
    verify: bool = True,
    verify_timeout_s: float = WORK_MODE_VERIFY_TIMEOUT_S,
) -> bool:
    """Write Work mode to SA, then paired battery discharge mode; poll until SA confirms."""
    mode = str(mode).strip()
    if not mode:
        return False
    async with _get_enum_setting_lock():
        ok = await _set_inverter_enum_setting(
            cfg,
            topic=_work_mode_topic(cfg),
            value=mode,
            rules_field="work_mode",
            label="Work mode",
            verify=verify,
            verify_timeout_s=verify_timeout_s,
        )
        if not ok:
            return False
        return await ensure_paired_battery_discharge_mode(cfg, mode)


async def set_battery_discharge_mode(
    cfg: dict,
    mode: str,
    *,
    verify: bool = True,
    verify_timeout_s: float = BATTERY_DISCHARGE_VERIFY_TIMEOUT_S,
) -> bool:
    """Write battery discharge mode; set paired work mode first if needed."""
    mode = _normalize_battery_discharge_mode_for_sa(mode)
    if not mode:
        return False
    async with _get_enum_setting_lock():
        if not await ensure_paired_work_mode_for_battery(cfg, mode):
            return False
        return await _set_inverter_enum_setting(
            cfg,
            topic=_battery_discharge_mode_topic(cfg),
            value=mode,
            rules_field="battery_discharge_mode",
            label="Battery discharge mode",
            verify=verify,
            verify_timeout_s=verify_timeout_s,
        )


async def _set_work_mode_only(
    cfg: dict,
    mode: str,
    *,
    verify: bool = True,
    verify_timeout_s: float = WORK_MODE_VERIFY_TIMEOUT_S,
) -> bool:
    """Write Work mode only (no paired battery write). Caller holds enum lock."""
    mode = str(mode).strip()
    if not mode:
        return False
    return await _set_inverter_enum_setting(
        cfg,
        topic=_work_mode_topic(cfg),
        value=mode,
        rules_field="work_mode",
        label="Work mode",
        verify=verify,
        verify_timeout_s=verify_timeout_s,
    )


async def _set_battery_discharge_mode_only(
    cfg: dict,
    mode: str,
    *,
    verify: bool = True,
    verify_timeout_s: float = BATTERY_DISCHARGE_VERIFY_TIMEOUT_S,
) -> bool:
    """Write battery discharge mode only (no paired work-mode write). Caller holds enum lock."""
    mode = _normalize_battery_discharge_mode_for_sa(mode)
    if not mode:
        return False
    return await _set_inverter_enum_setting(
        cfg,
        topic=_battery_discharge_mode_topic(cfg),
        value=mode,
        rules_field="battery_discharge_mode",
        label="Battery discharge mode",
        verify=verify,
        verify_timeout_s=verify_timeout_s,
    )


async def apply_export_start_modes(cfg: dict) -> bool:
    """Export window start: On-grid → Grid export (then caller enables timed).

    On-grid opens the grid path (SRNE: Limit home blocks sell). Grid export lets
    the battery participate. Soft-fail BDM: if On-grid confirms but Grid export
    verify times out, still return True so the timer can apply; mid-quarter retries
    can finish pairing.
    """
    async with _get_enum_setting_lock():
        wm_ok = await _set_work_mode_only(cfg, WORK_MODE_ON_GRID)
        if not wm_ok:
            return False
        bdm_ok = await _set_battery_discharge_mode_only(
            cfg, BATTERY_DISCHARGE_MODE_GRID_EXPORT,
        )
        if not bdm_ok:
            log.warning(
                "Battery Grid export not confirmed after On-grid — "
                "continuing (soft-fail); mid-quarter may retry",
            )
        return True


async def apply_home_modes(cfg: dict) -> bool:
    """Export end / charge prepare: Work mode Limit home → Battery UPS and home.

    Caller must clear Timed discharge *before* this when ending an export window.
    """
    async with _get_enum_setting_lock():
        wm_ok = await _set_work_mode_only(cfg, WORK_MODE_LIMIT_HOME_LOAD)
        if not wm_ok:
            return False
        return await _set_battery_discharge_mode_only(
            cfg, BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
        )


async def set_solar_power_priority(
    cfg: dict,
    priority: str,
    *,
    verify: bool = True,
    verify_timeout_s: float = WORK_MODE_VERIFY_TIMEOUT_S,
) -> bool:
    """Write Solar power priority to SA."""
    return await _set_inverter_enum_setting(
        cfg,
        topic=_solar_power_priority_topic(cfg),
        value=priority,
        rules_field="solar_power_priority",
        label="Solar power priority",
        verify=verify,
        verify_timeout_s=verify_timeout_s,
    )


async def _set_inverter_enum_setting(
    cfg: dict,
    *,
    topic: str,
    value: str,
    rules_field: str,
    label: str,
    verify: bool,
    verify_timeout_s: float,
) -> bool:
    value = str(value).strip()
    if not value:
        return False
    try:
        await _write_metrics(cfg, [(topic, value)], lock_wait_s=90.0)
        if not verify:
            invalidate_rules_cache()
            log.info("%s write sent — %s (no verify)", label, value)
            return True

        ok = await _wait_inverter_setting_confirmed(
            cfg,
            topic=topic,
            value=value,
            label=label,
            verify_timeout_s=verify_timeout_s,
        )
        if ok:
            invalidate_rules_cache()
        return ok
    except Exception as exc:
        log.error("Failed to set %s: %r", label, exc)
        return False


async def set_timed_power_flags(
    cfg: dict,
    *,
    timed_charge_enabled: bool | None = None,
    timed_discharge_enabled: bool | None = None,
) -> bool:
    """Write SA Power tab toggles; Timed charge also asserts Grid charge policy."""
    p = _INVERTER_PREFIX
    writes: list[tuple[str, str]] = []
    if timed_charge_enabled is not None:
        writes.append((f"{p}/timed_charge", _sa_switch(bool(timed_charge_enabled))))
    if timed_discharge_enabled is not None:
        writes.append((f"{p}/timed_discharge", _sa_switch(bool(timed_discharge_enabled))))
    if not writes:
        return False
    try:
        await _write_metrics(cfg, writes, lock_wait_s=90.0)
        log.info(
            "Timed power flags — charge=%s discharge=%s",
            timed_charge_enabled,
            timed_discharge_enabled,
        )
        invalidate_rules_cache()
        if timed_charge_enabled is not None:
            # Policy 1: Grid charge follows Timed charge (verify can take 30–90s).
            ok_grid = await set_grid_charging(
                cfg,
                enabled=bool(timed_charge_enabled),
                verify=True,
            )
            if not ok_grid:
                return False
        return True
    except Exception as exc:
        log.error("Failed to set timed power flags: %r", exc)
        return False


async def set_timer_schedule(cfg: dict, schedule: dict[str, Any]) -> bool:
    """Write selected timer slot(s) to SA (from charge_slots / discharge_slots in body)."""
    settings: dict[str, str] = cfg["sa"]["settings"]
    charge_nums = tuple(int(s["slot"]) for s in schedule.get("charge_slots", []))
    discharge_nums = tuple(int(s["slot"]) for s in schedule.get("discharge_slots", []))
    if not charge_nums and not discharge_nums:
        log.info("Timer schedule write skipped — no charge/discharge slots in payload")
        return True
    writes = _build_schedule_writes(
        schedule,
        charge_slot_nums=charge_nums,
        discharge_slot_nums=discharge_nums,
        settings=settings,
    )
    try:
        await _write_metrics(cfg, writes, lock_wait_s=90.0)
        log.info("Timer schedule saved to SA (charge=%s discharge=%s).", charge_nums, discharge_nums)
        invalidate_rules_cache()
        return True
    except Exception as exc:
        log.error("Failed to save timer schedule: %r", exc)
        return False


async def apply_hourly_schedule_to_sa(cfg: dict, schedule: dict[str, Any]) -> bool:
    """Auto-sync: write slot 1 for enabled timed charge and/or discharge only."""
    slim: dict[str, Any] = {
        "timed_charge_enabled": bool(schedule.get("timed_charge_enabled")),
        "timed_discharge_enabled": bool(schedule.get("timed_discharge_enabled")),
        "charge_slots": [],
        "discharge_slots": [],
    }
    if slim["timed_charge_enabled"]:
        slim["charge_slots"] = [schedule.get("charge_slots", [{}])[0]]
    if slim["timed_discharge_enabled"]:
        slim["discharge_slots"] = [schedule.get("discharge_slots", [{}])[0]]
    if not slim["charge_slots"] and not slim["discharge_slots"]:
        # No active slots — still assert Grid charge off when Timed charge is off.
        if not slim["timed_charge_enabled"]:
            ok_grid = await set_grid_charging(cfg, enabled=False, verify=True)
            if not ok_grid:
                return False
        log.info("Hourly schedule apply skipped — no timed charge/discharge enabled")
        return True
    ok = await set_timer_schedule(cfg, slim)
    if ok:
        log.info(
            "Hourly schedule applied — hour=%s action=%s charge=%s discharge=%s (slot 1)",
            schedule.get("target_hour"),
            schedule.get("planned_action"),
            schedule.get("timed_charge_enabled"),
            schedule.get("timed_discharge_enabled"),
        )
    return ok


async def apply_plan_to_sa(
    cfg: dict,
    sim: dict[str, Any],
) -> bool:
    """Push plan-derived timer schedule (+ grid charge permission) to SA."""
    schedule = sim.get("proposed_schedule") or {}
    if not await set_timer_schedule(cfg, schedule):
        return False

    charge_pwr = max(
        (float(s.get("power_kw", 0)) for s in schedule.get("charge_slots", [])),
        default=0.0,
    )
    # Policy 1: Grid charge Enabled only while Timed charge is active.
    return await set_grid_charging(
        cfg,
        enabled=bool(schedule.get("timed_charge_enabled")),
        power_kw=charge_pwr or 2.0,
        verify=True,
    )


# ---------------------------------------------------------------------------
# Timer schedule (SA → Configuration → Timer schedule)
# ---------------------------------------------------------------------------

def _timer_schedule_topics() -> list[str]:
    p = _INVERTER_PREFIX
    topics = [f"{p}/timed_charge", f"{p}/timed_discharge"]
    for n in range(1, _SLOT_COUNT + 1):
        topics.extend([
            f"{p}/charge_start_slot_{n}",
            f"{p}/charge_end_slot_{n}",
            f"{p}/charge_battery_capacity_slot_{n}",
            f"{p}/charge_battery_voltage_slot_{n}",
            f"{p}/charge_power_slot_{n}",
            f"{p}/charge_using_grid_slot_{n}",
            f"{p}/charge_using_generator_slot_{n}",
            f"{p}/discharge_start_slot_{n}",
            f"{p}/discharge_end_slot_{n}",
            f"{p}/discharge_battery_capacity_slot_{n}",
            f"{p}/discharge_battery_voltage_slot_{n}",
            f"{p}/discharge_power_slot_{n}",
        ])
    return topics


def _parse_timer_schedule(by_topic: dict[str, Any]) -> dict[str, Any]:
    p = _INVERTER_PREFIX
    charge_slots: list[dict[str, Any]] = []
    discharge_slots: list[dict[str, Any]] = []

    for n in range(1, _SLOT_COUNT + 1):
        gen = _truthy(by_topic.get(f"{p}/charge_using_generator_slot_{n}"))
        grid_raw = by_topic.get(f"{p}/charge_using_grid_slot_{n}")
        charge_slots.append({
            "slot": n,
            "from": _time_hhmm(by_topic.get(f"{p}/charge_start_slot_{n}")),
            "to": _time_hhmm(by_topic.get(f"{p}/charge_end_slot_{n}")),
            "capacity_pct": _float(by_topic.get(f"{p}/charge_battery_capacity_slot_{n}"), 0.0),
            "voltage_v": _float(by_topic.get(f"{p}/charge_battery_voltage_slot_{n}"), 0.0),
            "power_w": int(round(_float(by_topic.get(f"{p}/charge_power_slot_{n}"), 0.0))),
            "power_kw": round(_float(by_topic.get(f"{p}/charge_power_slot_{n}"), 0.0) / 1000.0, 2),
            "grid": _truthy(grid_raw) if grid_raw is not None else not gen,
            "generator": gen,
        })
        discharge_slots.append({
            "slot": n,
            "from": _time_hhmm(by_topic.get(f"{p}/discharge_start_slot_{n}")),
            "to": _time_hhmm(by_topic.get(f"{p}/discharge_end_slot_{n}")),
            "capacity_pct": _float(by_topic.get(f"{p}/discharge_battery_capacity_slot_{n}"), 0.0),
            "voltage_v": _float(by_topic.get(f"{p}/discharge_battery_voltage_slot_{n}"), 0.0),
            "power_w": int(round(_float(by_topic.get(f"{p}/discharge_power_slot_{n}"), 0.0))),
            "power_kw": round(_float(by_topic.get(f"{p}/discharge_power_slot_{n}"), 0.0) / 1000.0, 2),
        })

    return {
        "timed_charge_enabled": _optional_bool(by_topic.get(f"{p}/timed_charge")),
        "timed_discharge_enabled": _optional_bool(by_topic.get(f"{p}/timed_discharge")),
        "charge_slots": charge_slots,
        "discharge_slots": discharge_slots,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _time_hhmm(val: Any) -> str:
    if val is None:
        return "00:00"
    s = str(val).strip()
    return s[:5] if len(s) >= 5 else s


def _time_to_sa(val: Any) -> str:
    """SA select options use HH:MM (not HH:MM:SS)."""
    return _time_hhmm(val)


def _sa_switch(enabled: bool) -> str:
    """SRNE switch metrics use payload_on/off 1/0."""
    return "1" if enabled else "0"


def _sa_bool(enabled: bool) -> str:
    return _sa_switch(enabled)


def _grid_charge_switch(enabled: bool) -> str:
    """inverter_1/grid_charge uses Enabled/Disabled."""
    return "Enabled" if enabled else "Disabled"


def _slot_bool(val: Any) -> str:
    return _sa_bool(_truthy(val))


def _truthy(val: Any) -> bool:
    return str(val).lower() in ("1", "true", "yes", "enable", "enabled", "on")


def _optional_bool(val: Any) -> bool | None:
    if val is None:
        return None
    return _truthy(val)


def _float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
