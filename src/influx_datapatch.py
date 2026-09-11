"""Write, delete, and linearly fill Influx points (Solar Assistant db)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote

import requests

from .influxdb import INFLUXDB_DB, INFLUXDB_URL

log = logging.getLogger(__name__)

_WRITE_TIMEOUT_S = 30
_QUERY_TIMEOUT_S = 15


def parse_utc(ts: str) -> datetime:
    """Parse ``YYYY-MM-DDTHH:MM:SSZ`` (or offset) to aware UTC."""
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def escape_measurement(name: str) -> str:
    """Escape spaces in an Influx measurement name for line protocol."""
    return name.replace(" ", r"\ ").replace(",", r"\,")


def line_protocol(
    measurement: str,
    value: float,
    ts: datetime,
    *,
    integer: bool = False,
    field: str = "combined",
) -> str:
    """One line-protocol sample at second precision."""
    name = escape_measurement(measurement)
    epoch = int(ts.astimezone(timezone.utc).timestamp())
    if integer:
        payload = f"{field}={int(round(float(value)))}i"
    else:
        payload = f"{field}={float(value)}"
    return f"{name} {payload} {epoch}"


def interpolate(v0: float, v1: float, frac: float) -> float:
    """Linear value at fraction ``frac`` in ``[0, 1]``."""
    return float(v0) + (float(v1) - float(v0)) * frac


def step_times(start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    """UTC instants from ``start`` inclusive to ``end`` exclusive."""
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if step <= timedelta(0):
        raise ValueError("step must be positive")
    out: list[datetime] = []
    t = start
    while t < end:
        out.append(t)
        t = t + step
    return out


def influx_query(sql: str) -> dict[str, Any]:
    """Run one InfluxQL statement; return the first result object."""
    url = f"{INFLUXDB_URL}/query?db={INFLUXDB_DB}&epoch=s&q={quote(sql)}"
    resp = requests.get(url, timeout=_QUERY_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json().get("results", [{}])[0]


def write_lines(lines: Iterable[str], *, precision: str = "s") -> int:
    """POST line protocol. Return the number of non-empty lines written."""
    body_lines = [ln for ln in lines if ln.strip()]
    if not body_lines:
        return 0
    url = f"{INFLUXDB_URL}/write?db={INFLUXDB_DB}&precision={precision}"
    resp = requests.post(
        url, data="\n".join(body_lines) + "\n", timeout=_WRITE_TIMEOUT_S,
    )
    if resp.status_code not in (204, 200):
        raise RuntimeError(f"Influx write HTTP {resp.status_code}: {resp.text}")
    return len(body_lines)


def write_point(
    measurement: str,
    value: float,
    ts: datetime,
    *,
    integer: bool = False,
    field: str = "combined",
) -> None:
    """Write one sample."""
    write_lines([line_protocol(measurement, value, ts, integer=integer, field=field)])


def read_points(
    measurement: str,
    start: datetime,
    end: datetime,
    *,
    field: str = "combined",
) -> list[tuple[datetime, float]]:
    """Read ``field`` in ``[start, end)`` ordered by time."""
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    sql = (
        f'SELECT "{field}" FROM "{measurement}" '
        f"WHERE time >= '{_rfc3339(start)}' AND time < '{_rfc3339(end)}' "
        f"ORDER BY time ASC"
    )
    result = influx_query(sql)
    series = (result.get("series") or [None])[0]
    if not series:
        return []
    cols = series.get("columns") or []
    try:
        ti = cols.index("time")
        vi = cols.index(field)
    except ValueError:
        return []
    out: list[tuple[datetime, float]] = []
    for row in series.get("values") or []:
        ts_raw, val = row[ti], row[vi]
        if val is None:
            continue
        out.append((_epoch_to_utc(ts_raw), float(val)))
    return out


def last_point_before(
    measurement: str,
    ts: datetime,
    *,
    field: str = "combined",
    lookback: timedelta = timedelta(hours=6),
) -> tuple[datetime, float] | None:
    """Latest sample strictly before ``ts``."""
    points = read_points(measurement, ts - lookback, ts, field=field)
    return points[-1] if points else None


def first_point_at_or_after(
    measurement: str,
    ts: datetime,
    *,
    field: str = "combined",
    lookahead: timedelta = timedelta(hours=6),
) -> tuple[datetime, float] | None:
    """Earliest sample at or after ``ts``."""
    points = read_points(
        measurement, ts, ts + lookahead, field=field,
    )
    return points[0] if points else None


def delete_at_times(measurement: str, times: Iterable[datetime]) -> int:
    """Delete samples at the given UTC seconds. Return how many DELETE statements ran."""
    n = 0
    for ts in times:
        rfc = _rfc3339(ts.astimezone(timezone.utc))
        sql = f'DELETE FROM "{measurement}" WHERE time = \'{rfc}\''
        url = f"{INFLUXDB_URL}/query?db={INFLUXDB_DB}&q={quote(sql)}"
        resp = requests.post(url, timeout=_WRITE_TIMEOUT_S)
        resp.raise_for_status()
        err = (resp.json().get("results") or [{}])[0].get("error")
        if err:
            raise RuntimeError(f"Influx DELETE: {err}")
        n += 1
    log.info("Influx deleted %s point(s) from %s", n, measurement)
    return n


def drop_values_in_range(
    measurement: str,
    start: datetime,
    end: datetime,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    field: str = "combined",
) -> int:
    """Delete points in ``[start, end)`` whose value is outside the keep-window."""
    doomed: list[datetime] = []
    for ts, val in read_points(measurement, start, end, field=field):
        if min_value is not None and val >= min_value:
            doomed.append(ts)
            continue
        if max_value is not None and val <= max_value:
            doomed.append(ts)
    return delete_at_times(measurement, doomed)


def fill_linear_gap(
    measurement: str,
    gap_start: datetime,
    gap_end: datetime,
    step: timedelta,
    *,
    integer: bool = False,
    field: str = "combined",
) -> int:
    """Write linear samples on ``step`` from ``gap_start`` to ``gap_end`` (exclusive).

    Endpoints are the last sample before ``gap_start`` and the first at/after ``gap_end``.
    """
    left = last_point_before(measurement, gap_start, field=field)
    right = first_point_at_or_after(measurement, gap_end, field=field)
    if left is None or right is None:
        raise RuntimeError(
            f"No Influx neighbors for {measurement} around "
            f"{_rfc3339(gap_start)} .. {_rfc3339(gap_end)}"
        )
    t0, v0 = left
    t1, v1 = right
    span = (t1 - t0).total_seconds()
    if span <= 0:
        raise RuntimeError(f"Non-positive neighbor span for {measurement}")
    lines: list[str] = []
    for t in step_times(gap_start, gap_end, step):
        frac = (t - t0).total_seconds() / span
        val = interpolate(v0, v1, frac)
        lines.append(
            line_protocol(measurement, val, t, integer=integer, field=field)
        )
    n = write_lines(lines)
    log.info("Influx filled %s sample(s) on %s", n, measurement)
    return n


def _rfc3339(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_to_utc(raw: Any) -> datetime:
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    return parse_utc(str(raw))
