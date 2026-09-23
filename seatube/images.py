"""Turn annotations into labelled still images, downloading as little as possible.

ONC serves annotated video, not stills, and its archive endpoint ignores HTTP
range requests -- a single frame costs one whole archive file (70-200 MB for
five minutes).  So the planning here is all about download economy:
annotations collapse into frames, frames group into the archive files that
contain them, and files are visited richest-first so a small request is
satisfied by the fewest downloads.  Files are deleted after extraction unless
kept explicitly.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import shutil
import subprocess
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .annotations import Annotation
from .archive import parse_iso_utc, to_iso_utc
from .client import OncClient
from .taxonomy import WormsResolver

INDEX_COLUMNS = [
    "image_file", "frame_utc", "archive_filename", "offset_seconds",
    "taxa", "worms_aphia_ids", "groups", "annotation_ids", "annotation_count",
    "camera_mode", "dive_name", "location", "lat", "lon", "depth_m",
    "creators", "seatube_link",
]
logger = logging.getLogger(__name__)


def validate_limit(name: str, value: Optional[int]) -> None:
    if value is not None and (not isinstance(value, int) or value < 0):
        raise ValueError(f"{name} must be a non-negative integer or None")


def archive_path(directory: Path, name: str) -> Path:
    """Archive metadata must describe a filename, never a path outside the cache."""
    if not name or Path(name).name != name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"Invalid archive filename: {name!r}")
    return directory / name


def extract_media(command: List[str], output_path: Path) -> None:
    """Publish only a completed ffmpeg output; failed attempts remain retryable."""
    temporary = output_path.with_name(output_path.stem + ".part" + output_path.suffix)
    try:
        result = subprocess.run(command + [str(temporary)], capture_output=True, text=True)
        if result.returncode or not temporary.exists() or not temporary.stat().st_size:
            raise RuntimeError(f"ffmpeg failed for {output_path.name}: {result.stderr.strip()[:300]}")
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)


class Frame:
    """One still to extract: a point in one archive file, plus its labels."""

    def __init__(self, archive_filename: str, offset_seconds: float, frame_utc: str) -> None:
        self.archive_filename = archive_filename
        self.offset_seconds = offset_seconds
        self.frame_utc = frame_utc
        self.annotations: List[Annotation] = []

    @property
    def image_name(self) -> str:
        stem = Path(self.archive_filename).stem.replace(" ", "_")
        # Keep legacy centisecond names, but don't collide for distinct ms frames.
        offset = f"{self.offset_seconds:07.2f}"
        if abs(self.offset_seconds - round(self.offset_seconds, 2)) > 1e-7:
            offset = f"{self.offset_seconds:08.3f}"
        return f"{stem}_t{offset}"

    def taxa(self, resolver: Optional[WormsResolver] = None) -> List[Dict[str, Any]]:
        """Distinct taxonomy entries across every annotation on this frame."""
        seen: Dict[str, Dict[str, Any]] = {}
        for ann in self.annotations:
            for taxon in ann.taxa:
                label = taxon.primary_name
                if label not in seen:
                    entry = {"name": label, "aphia_id": taxon.aphia_id, "groups": []}
                    if resolver is not None:
                        entry["groups"] = resolver.groups_for(taxon.raw)
                    seen[label] = entry
        return list(seen.values())


def build_frames(annotations: Iterable[Annotation], dedupe_seconds: float = 0.0) -> List[Frame]:
    """Merge time buckets, retaining the earliest actual observation in each.

    With deduplication, other labels may refer to nearby instants. Original
    annotation timestamps remain in the JSONL index for inspection.
    """
    if not math.isfinite(dedupe_seconds) or dedupe_seconds < 0:
        raise ValueError("dedupe_seconds must be finite and non-negative")
    frames: Dict[Tuple[str, float], Frame] = {}
    for ann in annotations:
        offset = ann.offset_in_file_seconds()
        if offset is None:
            continue
        bucket = round(offset / dedupe_seconds) * dedupe_seconds if dedupe_seconds > 0 else offset
        key = (ann.archive_filename, round(bucket, 3))
        frame = frames.get(key)
        if frame is None:
            clip_start = parse_iso_utc(ann.raw["archiveClipStartDate"])
            frame = Frame(
                archive_filename=ann.archive_filename,
                offset_seconds=offset,
                frame_utc=to_iso_utc(clip_start + timedelta(seconds=offset)),
            )
            frames[key] = frame
        elif offset < frame.offset_seconds:
            frame.offset_seconds = offset
            frame.frame_utc = ann.start_utc
        frame.annotations.append(ann)
    return list(frames.values())


def select_frames(
    frames: Sequence[Frame],
    *,
    max_images: Optional[int] = None,
    max_videos: Optional[int] = None,
    max_per_taxon: Optional[int] = None,
) -> List[Frame]:
    """Choose frames, most frames per archive first (not a byte optimizer).

    Archive files are ranked by how many wanted frames they hold, so a small
    ``max_images`` tends to need fewer downloads. Per-label caps can change
    that ranking, and file byte sizes are not considered here.
    """
    for name, value in (("max_images", max_images), ("max_videos", max_videos),
                        ("max_per_taxon", max_per_taxon)):
        validate_limit(name, value)
    by_archive: Dict[str, List[Frame]] = defaultdict(list)
    for frame in frames:
        by_archive[frame.archive_filename].append(frame)

    ranked = sorted(by_archive.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    if max_videos is not None:
        ranked = ranked[:max_videos]

    taxon_counts: Dict[str, int] = defaultdict(int)
    chosen: List[Frame] = []
    for _, archive_frames in ranked:
        for frame in sorted(archive_frames, key=lambda f: f.offset_seconds):
            if max_images is not None and len(chosen) >= max_images:
                return chosen
            labels = [t["name"] for t in frame.taxa()]
            if max_per_taxon is not None and labels:
                if any(taxon_counts[label] >= max_per_taxon for label in labels):
                    continue
            for label in labels:
                taxon_counts[label] += 1
            chosen.append(frame)
    return chosen


class ImageDownloader:
    """Downloads archive files and extracts the planned frames.

    >>> downloader = ImageDownloader(client, output_dir="images")
    >>> frames = select_frames(build_frames(annotation_set), max_images=20)
    >>> print(downloader.describe_plan(frames))     # byte cost, no download
    >>> downloader.download(frames)                 # images + index files
    """

    index_stem = "images_index"
    index_columns = INDEX_COLUMNS
    file_column = "image_file"

    def __init__(
        self,
        client: OncClient,
        output_dir: str,
        *,
        image_format: str = "jpg",
        jpeg_quality: int = 2,
        video_dir: Optional[str] = None,
        keep_videos: bool = False,
        resolver: Optional[WormsResolver] = None,
    ) -> None:
        if image_format not in {"jpg", "png"}:
            raise ValueError("image_format must be 'jpg' or 'png'")
        if not isinstance(jpeg_quality, int) or not 1 <= jpeg_quality <= 31:
            raise ValueError("jpeg_quality must be an integer between 1 and 31")
        self.client = client
        self.output_dir = Path(output_dir)
        self.image_format = image_format
        self.jpeg_quality = jpeg_quality
        self.video_dir = Path(video_dir) if video_dir else self.output_dir / "_videos"
        self.keep_videos = keep_videos
        self.resolver = resolver

    # -- planning -----------------------------------------------------------

    @staticmethod
    def archives_for(frames: Sequence[Frame]) -> List[str]:
        return sorted({f.archive_filename for f in frames})

    def describe_plan(self, frames: Sequence[Frame]) -> str:
        """Human-readable cost report: files, image counts, byte sizes."""
        lines = []
        total = 0
        unknown = 0
        for entry in self.plan(frames):
            name, size = entry["archive_filename"], entry["download_bytes"]
            if size is not None:
                total += size
            else:
                unknown += 1
            count = sum(1 for f in frames if f.archive_filename == name)
            size_text = f"{size / 1e9:.2f} GB" if size is not None else "size unknown"
            lines.append(f"  {name}  ->  {count} output(s), {size_text}")
        note = f" (+{unknown} file(s) of unknown size)" if unknown else ""
        lines.append(f"\nEstimated download: {total / 1e9:.2f} GB{note} for {len(frames)} output(s).")
        return "\n".join(lines)

    def plan(self, frames: Sequence[Frame], *, check_sizes: bool = True) -> List[Dict[str, Any]]:
        """Structured archive budget. HEAD requests only if check_sizes=True.

        Unknown sizes are None, never zero. Cached archives and already
        extracted outputs do not incur another download.
        """
        rows = []
        for name in self.archives_for(frames):
            cached = archive_path(self.video_dir, name)
            here = [f for f in frames if f.archive_filename == name]
            pending = [f for f in here if not self._complete(self._target(f))]
            needs_download = bool(pending) and not self._complete(cached)
            size = (self.client.archive_file_size(name)
                    if needs_download and check_sizes and self.client.token else None)
            rows.append({"archive_filename": name, "outputs": len(here),
                         "pending_outputs": len(pending), "needs_download": needs_download,
                         "download_bytes": size if needs_download else 0})
        return rows

    def _target(self, frame: Frame) -> Path:
        return self.output_dir / f"{frame.image_name}.{self.image_format}"

    @staticmethod
    def _complete(path: Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    # -- execution ----------------------------------------------------------

    def download(self, frames: Sequence[Frame]) -> List[Dict[str, Any]]:
        """Fetch archives (richest-first), extract frames, write the index.

        Returns the index rows.  Existing images are never re-extracted, and
        an archive file is only downloaded if one of its frames is missing.
        """
        for frame in frames:
            archive_path(self.video_dir, frame.archive_filename)
        if any(not self._complete(self._target(f)) for f in frames) and not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is required for extraction. Install it and add it to PATH.")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.video_dir.mkdir(parents=True, exist_ok=True)

        by_archive: Dict[str, List[Frame]] = defaultdict(list)
        for frame in frames:
            by_archive[frame.archive_filename].append(frame)
        archives = self.archives_for(frames)

        rows: List[Dict[str, Any]] = []
        records: List[Dict[str, Any]] = []
        written = 0

        for i, name in enumerate(archives, start=1):
            frames_here = sorted(by_archive[name], key=lambda f: f.offset_seconds)
            targets = {f: self._target(f) for f in frames_here}
            pending = [f for f, path in targets.items() if not self._complete(path)]

            video_path = archive_path(self.video_dir, name)
            downloaded_now = False
            if pending:
                if self._complete(video_path):
                    logger.info("Using cached %s", name)
                else:
                    logger.info("Downloading %s for %s output(s)", name, len(pending))
                    self.client.download_archive_file(name, str(video_path))
                    downloaded_now = True
            else:
                logger.info("All outputs already present for %s", name)

            for frame in frames_here:
                image_path = targets[frame]
                if not self._complete(image_path):
                    try:
                        self._extract(video_path, frame, image_path)
                    except BaseException:
                        self._write_index(rows, records)
                        raise
                    written += 1
                row = self._index_row(frame, image_path.name)
                rows.append(row)
                records.append({**row, "annotations": [a.raw for a in frame.annotations]})

            self._write_index(rows, records)

            if downloaded_now and not self.keep_videos:
                video_path.unlink(missing_ok=True)

        if self.resolver is not None:
            self.resolver.save()
        if not self.keep_videos:
            try:
                self.video_dir.rmdir()
            except OSError:
                pass

        self._write_index(rows, records)
        logger.info("%s new output(s), %s indexed in %s", written, len(rows), self.output_dir)
        return rows

    # -- internals ----------------------------------------------------------

    def _extract(self, video_path: Path, frame: Frame, image_path: Path) -> None:
        self._extract_frame(video_path, frame.offset_seconds, image_path)

    def _extract_frame(self, video_path: Path, offset_seconds: float, image_path: Path) -> bool:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{offset_seconds:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
        ]
        if image_path.suffix.lower() in {".jpg", ".jpeg"}:
            cmd += ["-q:v", str(self.jpeg_quality)]
        extract_media(cmd, image_path)
        return True

    def _index_row(self, frame: Frame, image_file: str) -> Dict[str, Any]:
        taxa = frame.taxa(self.resolver)
        first = frame.annotations[0]
        groups = sorted({g for t in taxa for g in t.get("groups") or []})
        creators = sorted({a.creator_name for a in frame.annotations} - {""})
        return {
            "image_file": image_file,
            "frame_utc": frame.frame_utc,
            "archive_filename": frame.archive_filename,
            "offset_seconds": round(frame.offset_seconds, 3),
            "taxa": "; ".join(t["name"] for t in taxa),
            "worms_aphia_ids": "; ".join(str(t["aphia_id"]) for t in taxa if t["aphia_id"]),
            "groups": "; ".join(groups),
            "annotation_ids": "; ".join(str(a.id) for a in frame.annotations),
            "annotation_count": len(frame.annotations),
            "camera_mode": first.camera_mode,
            "dive_name": first.dive_name,
            "location": first.location_name,
            "lat": first.lat,
            "lon": first.lon,
            "depth_m": first.depth_m,
            "creators": "; ".join(creators),
            "seatube_link": first.seatube_link,
        }

    def _write_index(self, rows: List[Dict[str, Any]], records: List[Dict[str, Any]]) -> None:
        # A later selection into the same directory must not erase provenance
        # for the files produced by earlier successful calls.
        index_jsonl = self.output_dir / (self.index_stem + ".jsonl")
        existing = {}
        if index_jsonl.exists():
            for line in index_jsonl.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                filename = record[self.file_column]
                if self._complete(archive_path(self.output_dir, filename)):
                    existing[filename] = record
        for record in records:
            filename = record[self.file_column]
            previous = existing.get(filename, {})
            # Identical media can acquire additional labels in another search.
            annotations = {json.dumps(a, sort_keys=True): a
                           for a in previous.get("annotations", []) + record["annotations"]}
            if previous:
                for key in ("taxa", "worms_aphia_ids", "groups", "annotation_ids", "creators"):
                    record[key] = "; ".join(sorted(set(
                        str(previous.get(key) or "").split("; ") + str(record.get(key) or "").split("; ")
                    ) - {""}))
                record["annotation_count"] = len(annotations)
            existing[filename] = {**record, "annotations": list(annotations.values())}
        records = [existing[name] for name in sorted(existing)]
        rows = [{k: record.get(k) for k in self.index_columns} for record in records]
        index_csv = self.output_dir / (self.index_stem + ".csv")
        temp_csv = index_csv.with_suffix(".csv.tmp")
        with temp_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.index_columns)
            writer.writeheader()
            writer.writerows(rows)
        temp_jsonl = index_jsonl.with_suffix(".jsonl.tmp")
        with temp_jsonl.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        temp_jsonl.replace(index_jsonl)
        temp_csv.replace(index_csv)
        logger.info("Wrote %s and %s", index_csv, index_jsonl)
