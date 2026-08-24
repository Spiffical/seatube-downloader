"""Locate the archive video file (and position) that contains a timestamp.

ONC's video metadata lists each device's recordings as ``dataFiles`` rows:
``[offset_in_series, duration, relative_path, row_start_offset, extra_ms]``.
The functions here turn an annotation timestamp into the one file that truly
contains it, or nothing at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence, Tuple


def parse_iso_utc(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_js_iso_compact(dt: datetime) -> str:
    # Match JavaScript: new Date(...).toISOString().replace(/[-:]/g, "")
    return to_iso_utc(dt).replace("-", "").replace(":", "")


def select_media_file(
    media_files: Sequence[Dict[str, Any]],
    annotation_device_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Prefer the media series recorded by the annotation's own device."""
    if not media_files:
        return None
    if annotation_device_id is None:
        return media_files[0]
    for media_file in media_files:
        if media_file.get("deviceId") == annotation_device_id:
            return media_file
    return media_files[0]


def data_file_epoch_bounds(
    media_file: Dict[str, Any],
    row: Sequence[str],
) -> Tuple[float, float]:
    """Absolute [start, end) epoch seconds of one dataFiles row."""
    date_start = float(media_file.get("dateStartSeconds", 0))
    offset = float(row[0])
    duration = float(row[1])
    extra_ms = float(row[4]) if len(row) > 4 else 0.0
    start = date_start + offset + (extra_ms / 1000.0)
    return start, start + duration


def data_file_row_containing(
    media_file: Dict[str, Any],
    when: datetime,
) -> Optional[Sequence[str]]:
    """Return only a data-file row that truly contains ``when``.

    A previous nearest-row fallback silently mapped annotations across real
    recording gaps (including onto a different day).  Downstream code then
    clamped the negative relative timestamp to zero, producing plausible but
    false clips.  Absence of a containing row is now explicit.
    """
    target_epoch = when.timestamp()
    for row in media_file.get("dataFiles", []) or []:
        start_epoch, end_epoch = data_file_epoch_bounds(media_file, row)
        if start_epoch <= target_epoch < end_epoch:
            return row
    return None


def archive_info_from_row(media_file: Dict[str, Any], row: Sequence[str]) -> Dict[str, Any]:
    """Fields describing the archive file a data-file row points at.

    ``clipOffsetSeconds`` locates the file within the device's media series;
    it is NOT a position inside the file.  A position inside the file is
    ``annotation.startDate - archiveClipStartDate``.
    """
    offset_seconds = float(row[0])
    duration_seconds = float(row[1])
    clip_relative_path = row[2]
    row_start_offset_seconds = float(row[3]) if len(row) > 3 else None
    extra_ms = float(row[4]) if len(row) > 4 else 0.0

    start_epoch = float(media_file["dateStartSeconds"]) + offset_seconds
    clip_start_dt = datetime.fromtimestamp(start_epoch + (extra_ms / 1000.0), tz=timezone.utc)

    device_code = media_file["deviceCode"]
    postfix = media_file["defaultFileNamePostFix"]
    filename = f"{device_code}_{to_js_iso_compact(clip_start_dt)}{postfix}"

    return {
        "archiveFilename": filename,
        "clipOffsetSeconds": offset_seconds,
        "clipDurationSeconds": duration_seconds,
        "clipRelativePath": clip_relative_path,
        "clipRowStartOffsetSeconds": row_start_offset_seconds,
        "archiveClipStartDate": to_iso_utc(clip_start_dt),
    }
