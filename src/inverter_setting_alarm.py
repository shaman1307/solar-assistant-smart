"""Alarm when a quarter rules read disagrees with saved capacity settings."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Callable

log = logging.getLogger(__name__)

# Screen labels from Configuration → Simulation.
_CAPACITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("to_grid_battery_capacity", "To grid battery capacity (%)"),
    ("back_to_battery_capacity", "Back to battery capacity (%)"),
)

CAPACITY_WRITE_ATTEMPTS = 3
CAPACITY_WRITE_PAUSE_S = 30

_alarm_active = False
_test_alarm = False
_mismatches: list[dict[str, Any]] = []
_checked_at: str | None = None
# idle: readings match, or a mismatch has not been handled yet.
# accepted: a write landed. The next divergent quarter alarms without writing.
# hold: alarm already sent. Later divergent quarters do not write again.
_sync_phase = "idle"


def _capacity_reading_complete(rules: dict[str, Any]) -> bool:
    """Both capacity registers came back as numbers."""
    return all(rules.get(key) is not None for key, _label in _CAPACITY_FIELDS)


def capacity_setting_mismatches(
    cfg: dict[str, Any],
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return fields whose inverter reading is not the saved configuration value.

    A missing inverter reading is not a mismatch.
    """
    saved = (cfg or {}).get("inverter") or {}
    found: list[dict[str, Any]] = []
    for key, label in _CAPACITY_FIELDS:
        if key not in rules or rules.get(key) is None:
            continue
        if key not in saved or saved.get(key) is None:
            continue
        inverter = int(rules[key])
        configured = int(saved[key])
        if inverter != configured:
            found.append({
                "key": key,
                "label": label,
                "inverter": inverter,
                "configured": configured,
            })
    return found


def alarm_status() -> dict[str, Any]:
    """Current alarm for the UI."""
    return {
        "active": _alarm_active,
        "test": _test_alarm,
        "mismatches": list(_mismatches),
        "checked_at": _checked_at,
    }


def set_test_alarm(active: bool) -> dict[str, Any]:
    """Turn the on-screen alarm on or off without reading the inverter or sending mail."""
    global _alarm_active, _test_alarm, _mismatches, _checked_at
    if active:
        _test_alarm = True
        _alarm_active = True
        if not _mismatches:
            _checked_at = "test"
    elif _test_alarm:
        _test_alarm = False
        if not _mismatches:
            _alarm_active = False
            _checked_at = None
    return alarm_status()


def reset_capacity_sync_state() -> None:
    """Clear the in-memory alarm and the write/hold phase."""
    global _alarm_active, _test_alarm, _mismatches, _checked_at, _sync_phase
    _alarm_active = False
    _test_alarm = False
    _mismatches = []
    _checked_at = None
    _sync_phase = "idle"


def overlay_live_capacity(cfg: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """Copy a complete inverter capacity reading onto the plan configuration."""
    if not _capacity_reading_complete(rules):
        return cfg
    inv = cfg.setdefault("inverter", {})
    for key, _label in _CAPACITY_FIELDS:
        inv[key] = int(rules[key])
    return cfg


def saved_capacity_writes(cfg: dict[str, Any], rules: dict[str, Any]) -> list[tuple[str, str]]:
    """Saved Configuration values for fields the inverter reading does not match."""
    from .sa_client import _INVERTER_PREFIX

    return [
        (f"{_INVERTER_PREFIX}/{row['key']}", str(row["configured"]))
        for row in capacity_setting_mismatches(cfg, rules)
    ]


def _clear_alarm() -> None:
    global _alarm_active, _test_alarm, _mismatches
    _alarm_active = False
    _test_alarm = False
    _mismatches = []


def _raise_alarm(
    cfg: dict[str, Any],
    found: list[dict[str, Any]],
    send_email: Callable[[dict[str, Any], list[dict[str, Any]]], None] | None,
    reason: str,
) -> None:
    global _alarm_active, _test_alarm, _mismatches
    started = bool(found) and (not _alarm_active or _test_alarm)
    _test_alarm = False
    _alarm_active = bool(found)
    _mismatches = found
    log.info(
        "Capacity mismatch (%s): %s",
        reason,
        "; ".join(
            f"{row['label']} inverter {row['inverter']} configuration {row['configured']}"
            for row in found
        ),
    )
    if not started:
        return
    sender = send_email or send_alarm_email
    try:
        sender(cfg, found)
    except Exception:
        log.exception("Inverter setting alarm email failed")


async def reconcile_inverter_capacity(
    cfg: dict[str, Any],
    rules: dict[str, Any],
    *,
    read: Callable[[], Any],
    write: Callable[[list[tuple[str, str]]], Any],
    sleep: Callable[[], Any],
    send_email: Callable[[dict[str, Any], list[dict[str, Any]]], None] | None = None,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Write saved capacity values when the inverter differs, then return the live reading.

    The first divergent quarter writes up to three times. Before the second and
    third write, the inverter is read again. A reading that already matches the
    saved values ends the writes. The next divergent quarter sends the alarm and
    does not write. Three writes that do not land also send the alarm.
    """
    global _sync_phase, _mismatches, _alarm_active, _test_alarm, _checked_at
    _checked_at = checked_at
    if not _capacity_reading_complete(rules):
        log.info(
            "Capacity check skipped — inverter reading incomplete (alarm stays %s)",
            "on" if _alarm_active else "off",
        )
        return rules

    found = capacity_setting_mismatches(cfg, rules)
    if not found:
        _sync_phase = "idle"
        _clear_alarm()
        return rules

    if _sync_phase == "accepted":
        _sync_phase = "hold"
        _raise_alarm(
            cfg,
            found,
            send_email,
            "inverter diverged again after a successful write",
        )
        return rules

    if _sync_phase == "hold":
        _test_alarm = False
        _alarm_active = True
        _mismatches = found
        return rules

    latest = rules
    for attempt in range(1, CAPACITY_WRITE_ATTEMPTS + 1):
        if not capacity_setting_mismatches(cfg, latest):
            _sync_phase = "accepted"
            _clear_alarm()
            return latest
        try:
            await write(saved_capacity_writes(cfg, latest))
        except Exception:
            log.exception("Capacity write attempt %s failed", attempt)
        await sleep()
        latest = await read()
        if not _capacity_reading_complete(latest):
            log.info("Capacity reread incomplete after attempt %s", attempt)
            return latest
    if not capacity_setting_mismatches(cfg, latest):
        _sync_phase = "accepted"
        _clear_alarm()
        return latest
    _sync_phase = "hold"
    _raise_alarm(
        cfg,
        capacity_setting_mismatches(cfg, latest),
        send_email,
        "three writes did not change the inverter",
    )
    return latest


def send_alarm_email(cfg: dict[str, Any], mismatches: list[dict[str, Any]]) -> None:
    """Send one message to the Configuration alert email."""
    recipient = str(cfg.get("alert_email") or "").strip()
    smtp = cfg.get("alert_smtp") or {}
    host = str(smtp.get("host") or "").strip()
    if not recipient or not host:
        log.warning(
            "Inverter setting alarm is active but email is not configured "
            "(alert email or SMTP host is empty)",
        )
        return
    port = int(smtp.get("port") or 587)
    username = str(smtp.get("username") or "").strip()
    password = str(smtp.get("password") or "")
    lines = [
        "A quarter reading from the inverter does not match Configuration.",
        "",
    ]
    for row in mismatches:
        lines.append(
            f"{row['label']}: inverter {row['inverter']}, configuration {row['configured']}.",
        )
    msg = EmailMessage()
    msg["Subject"] = "Inverter setting mismatch"
    msg["From"] = username or recipient
    msg["To"] = recipient
    msg.set_content("\n".join(lines))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as client:
            if username:
                client.login(username, password)
            client.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as client:
            client.ehlo()
            client.starttls()
            client.ehlo()
            if username:
                client.login(username, password)
            client.send_message(msg)
    log.info("Inverter setting alarm email sent to %s", recipient)
