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

_alarm_active = False
_test_alarm = False
_mismatches: list[dict[str, Any]] = []
_checked_at: str | None = None


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


def record_capacity_reading(
    cfg: dict[str, Any],
    rules: dict[str, Any],
    *,
    checked_at: str | None = None,
    send_email: Callable[[dict[str, Any], list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """Update the alarm from one rules reading. Mail only when the alarm starts."""
    global _alarm_active, _test_alarm, _mismatches, _checked_at
    found = capacity_setting_mismatches(cfg, rules)
    started = bool(found) and (not _alarm_active or _test_alarm)
    _test_alarm = False
    _alarm_active = bool(found)
    _mismatches = found
    _checked_at = checked_at
    if started:
        sender = send_email or send_alarm_email
        try:
            sender(cfg, found)
        except Exception:
            log.exception("Inverter setting alarm email failed")
    return alarm_status()


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
