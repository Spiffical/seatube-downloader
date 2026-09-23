"""Plan and extract short MP4 excerpts around annotated observations."""

from __future__ import annotations

import math
import warnings
from datetime import timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .annotations import Annotation
from .archive import parse_iso_utc, to_iso_utc
from .images import Frame, ImageDownloader, INDEX_COLUMNS, extract_media, select_frames


class Clip(Frame):
    """One [start, end) interval inside one archive, with source annotations."""

    def __init__(self, archive_filename: str, offset_seconds: float,
                 end_seconds: float, frame_utc: str) -> None:
        super().__init__(archive_filename, offset_seconds, frame_utc)
        self.end_seconds = end_seconds

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.offset_seconds

    @property
    def image_name(self) -> str:
        return f"{super().image_name}_to{self.end_seconds:08.3f}"

    def __repr__(self) -> str:
        return (f"Clip({self.archive_filename!r}, start={self.offset_seconds:.3f}s, "
                f"duration={self.duration_seconds:.3f}s)")


def build_clips(annotations: Iterable[Annotation], *, before_seconds: float = 5,
                after_seconds: float = 5) -> List[Clip]:
    """Merge overlapping excerpts, clamping to known archive boundaries.

    A clip never spans archive files or recording gaps. Unknown durations are
    skipped with a warning: without an end bound we cannot promise containment.
    The annotation instant is a label, not a claim of visibility throughout.
    """
    if (not math.isfinite(before_seconds) or before_seconds < 0 or
            not math.isfinite(after_seconds) or after_seconds <= 0):
        raise ValueError("before_seconds must be >= 0 and after_seconds > 0 (both finite)")
    candidates = []
    unknown_duration = 0
    for ann in annotations:
        offset = ann.offset_in_file_seconds()
        if offset is None:
            continue
        duration = ann.clip_duration_seconds
        if duration is None:
            unknown_duration += 1
            continue
        start, end = max(0.0, offset - before_seconds), min(duration, offset + after_seconds)
        utc = to_iso_utc(parse_iso_utc(ann.raw["archiveClipStartDate"]) + timedelta(seconds=start))
        clip = Clip(ann.archive_filename, start, end, utc)
        clip.annotations.append(ann)
        candidates.append(clip)
    if unknown_duration:
        warnings.warn(f"Skipped {unknown_duration} annotation(s) with unknown archive duration; "
                      "frames and clip_index() remain available.", UserWarning, stacklevel=2)
    merged: List[Clip] = []
    for clip in sorted(candidates, key=lambda c: (c.archive_filename, c.offset_seconds)):
        if (merged and merged[-1].archive_filename == clip.archive_filename and
                clip.offset_seconds <= merged[-1].end_seconds):
            merged[-1].end_seconds = max(merged[-1].end_seconds, clip.end_seconds)
            merged[-1].annotations.extend(clip.annotations)
        else:
            merged.append(clip)
    return merged


def select_clips(clips: Sequence[Clip], *, max_clips: Optional[int] = None,
                 max_videos: Optional[int] = None) -> List[Clip]:
    """Cap excerpt and archive counts, preferring archives with more excerpts."""
    return select_frames(clips, max_images=max_clips, max_videos=max_videos)


class ClipDownloader(ImageDownloader):
    """Extract silent H.264 MP4 excerpts and CSV/JSONL provenance indexes.

    Re-encoding avoids keyframe-only cuts. Timing is limited to source frame
    precision. Entire archive files must still be downloaded first.
    """

    index_stem = "clips_index"
    file_column = "clip_file"
    index_columns = ["clip_file", "clip_start_utc", "end_seconds", "duration_seconds"] + [
        key for key in INDEX_COLUMNS if key not in {"image_file", "frame_utc"}
    ]

    def __init__(self, client, output_dir: str, *, video_dir=None,
                 keep_videos: bool = False, resolver=None) -> None:
        super().__init__(client, output_dir, video_dir=video_dir,
                         keep_videos=keep_videos, resolver=resolver)
        self.image_format = "mp4"

    def _extract(self, video_path: Path, frame: Clip, image_path: Path) -> None:
        extract_media([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{frame.offset_seconds:.3f}", "-i", str(video_path),
            "-t", f"{frame.duration_seconds:.3f}", "-map", "0:v:0", "-an",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ], image_path)

    def _index_row(self, frame: Clip, image_file: str) -> dict:
        row = super()._index_row(frame, image_file)
        row["clip_file"] = row.pop("image_file")
        row["clip_start_utc"] = row.pop("frame_utc")
        row["end_seconds"] = frame.end_seconds
        row["duration_seconds"] = frame.duration_seconds
        return row
