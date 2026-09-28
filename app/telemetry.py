"""Telemetry ingestion: per-entry validation, dedup and ordering.

Bad rows are never dropped silently: each rejected row is reported with one of
four distinguishable reasons.

* ``missing_field``        required field absent (``ts`` / ``metrics``), entry
                           not an object, or metrics empty;
* ``type_mismatch``        field present but of the wrong shape/type
                           (unparseable timestamp, non-numeric metric value,
                           non-string metric key, naive timestamp, ...);
* ``timestamp_regression`` timestamp older than the latest timestamp already
                           committed for the device;
* ``duplicate``            same timestamp seen earlier in this batch or already
                           stored for the device.

Ordering is defined by timestamp regardless of row order, so a batch arriving
shuffled (10:02 before 10:01) produces the same final time series as the same
data arriving in order.
"""

from __future__ import annotations

from datetime import datetime

from app.models import TelemetryEntry
from app.store import DeviceRecord, TelemetryPoint


def _parse_ts(raw: object) -> tuple[datetime | None, str | None]:
    """Parse a timestamp into a timezone-aware UTC datetime.

    Returns ``(value, error)`` with exactly one element set. Missing input is
    reported separately by the caller, so here present-but-bad values are
    always ``type_mismatch``.
    """
    if isinstance(raw, datetime):
        dt = raw
    elif isinstance(raw, str):
        try:
            text = raw.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        except (ValueError, TypeError):
            return None, "type_mismatch"
    else:
        return None, "type_mismatch"
    if dt.tzinfo is None:
        return None, "type_mismatch"
    return dt, None


def _validate_metrics(raw: object) -> tuple[dict[str, float] | None, str | None]:
    if not isinstance(raw, dict):
        return None, "type_mismatch"
    if len(raw) == 0:
        return None, "missing_field"
    metrics: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            return None, "type_mismatch"
        # bool is a subclass of int: explicitly reject it as a measurement.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "type_mismatch"
        metrics[key] = float(value)
    return metrics, None


def validate_entry(raw: object) -> tuple[TelemetryEntry | None, str | None, str | None]:
    """Validate shape/types of one raw payload.

    Returns ``(entry, reason, message)``; exactly one of entry/reason is set.
    Cross-entry rules (duplicate / regression) are applied later by
    :func:`process_batch`, because they depend on stored state.
    """
    if not isinstance(raw, dict):
        return None, "missing_field", "entry must be a JSON object"
    if "ts" not in raw:
        return None, "missing_field", "missing required field 'ts'"
    if "metrics" not in raw:
        return None, "missing_field", "missing required field 'metrics'"

    ts, ts_error = _parse_ts(raw["ts"])
    if ts_error is not None:
        return None, ts_error, "field 'ts' must be an ISO-8601 timestamp with timezone"
    metrics, m_error = _validate_metrics(raw["metrics"])
    if m_error is not None:
        if m_error == "missing_field":
            return None, "missing_field", "field 'metrics' must contain at least one metric"
        return None, "type_mismatch", "every metric value must be a number"

    return TelemetryEntry(ts=ts, metrics=metrics), None, None


def process_batch(device: DeviceRecord, raw_entries: list) -> tuple[list[TelemetryPoint], list]:
    """Validate, classify and commit one batch atomically.

    Returns ``(accepted_points, rejected_items)`` where each rejected item is a
    dict ``{"index", "reason", "message"}``. Classification is deliberately
    independent of row arrival order: every row is classified against the
    durable stored state only.

    * a timestamp already stored, or repeated within this batch, is rejected
      as ``duplicate`` (first occurrence wins);
    * a timestamp older than the latest *stored* timestamp is rejected as
      ``timestamp_regression`` (the device clock moved backwards relative to
      durable state);
    * every other unique timestamp is accepted and stored keyed by timestamp,
      so a shuffled batch ``[10:02, 10:01]`` converges with ``[10:01, 10:02]``
      on exactly the same final series.

    Late points are therefore tolerated as long as they are not older than
    durable state, while genuine clock regressions stay distinguishable. The
    whole classification happens under the device lock and only fully
    accepted rows are committed, so concurrent readers never see rows that
    were ultimately rejected.
    """
    rejected: list[dict] = []
    accepted: list[TelemetryPoint] = []

    with device.lock:
        stored = device.points  # ts -> metrics
        stored_latest = max(stored) if stored else None
        batch_seen: set[datetime] = set()

        # Phase 1: shape/type validation; preserves per-row indexes.
        shaped: list[tuple[int, TelemetryEntry]] = []
        for index, raw in enumerate(raw_entries):
            entry, reason, message = validate_entry(raw)
            if reason is not None:
                rejected.append({"index": index, "reason": reason, "message": message})
            else:
                assert entry is not None
                shaped.append((index, entry))

        # Phase 2: order-independent cross-entry rules against durable state.
        for index, entry in shaped:
            ts = entry.ts
            if ts in stored:
                rejected.append({
                    "index": index,
                    "reason": "duplicate",
                    "message": f"timestamp {ts.isoformat()} already stored for device",
                })
                continue
            if ts in batch_seen:
                rejected.append({
                    "index": index,
                    "reason": "duplicate",
                    "message": f"timestamp {ts.isoformat()} appears more than once in this batch",
                })
                continue
            if stored_latest is not None and ts < stored_latest:
                rejected.append({
                    "index": index,
                    "reason": "timestamp_regression",
                    "message": (
                        f"timestamp {ts.isoformat()} is older than the latest "
                        f"stored timestamp {stored_latest.isoformat()}"
                    ),
                })
                continue
            batch_seen.add(ts)
            accepted.append(TelemetryPoint(ts=ts, metrics=entry.metrics))

        # Commit only after the whole batch has been classified; the mapping
        # is always read back sorted by timestamp.
        for point in accepted:
            stored[point.ts] = dict(point.metrics)

    # Order-independent output as well (sorted by timestamp).
    accepted.sort(key=lambda p: p.ts)
    rejected.sort(key=lambda item: item["index"])
    return accepted, rejected
