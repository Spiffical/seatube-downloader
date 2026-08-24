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
import subprocess
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .annotations import Annotation, AnnotationSet
from .archive import parse_iso_utc, to_iso_utc
from .client import OncClient
from .taxonomy import WormsResolver, aphia_id_from_taxon, taxon_names

INDEX_COLUMNS = [
    "image_file", "frame_utc", "archive_filename", "offset_seconds",
    "taxa", "worms_aphia_ids", "groups", "annotation_ids", "annotation_count",
    "camera_mode", "dive_name", "location", "lat", "lon", "depth_m",
    "creators", "seatube_link",
]


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
        return f"{stem}_t{self.offset_seconds:07.2f}"

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
    """Collapse annotations onto the frames that show them."""
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
                offset_seconds=round(bucket, 3),
                frame_utc=to_iso_utc(clip_start + timedelta(seconds=bucket)),
            )
            frames[key] = frame
        frame.annotations.append(ann)
    return list(frames.values())


def select_frames(
    frames: Sequence[Frame],
    *,
    max_images: Optional[int] = None,
    max_videos: Optional[int] = None,
    max_per_taxon: Optional[int] = None,
) -> List[Frame]:
    """Choose frames, cheapest-first.

    Archive files are ranked by how many wanted frames they hold, so a small
    ``max_images`` is satisfied by the fewest possible downloads.
    """
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
                if all(taxon_counts[label] >= max_per_taxon for label in labels):
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
        for name in self.archives_for(frames):
            size = self.client.archive_file_size(name) if self.client.token else None
            if size:
                total += size
            else:
                unknown += 1
            count = sum(1 for f in frames if f.archive_filename == name)
            size_text = f"{size / 1e9:.2f} GB" if size else "size unknown"
            lines.append(f"  {name}  ->  {count} image(s), {size_text}")
        note = f" (+{unknown} file(s) of unknown size)" if unknown else ""
        lines.append(f"\nWould download {total / 1e9:.2f} GB{note} to produce {len(frames)} images.")
        return "\n".join(lines)

    # -- execution ----------------------------------------------------------

    def download(self, frames: Sequence[Frame]) -> List[Dict[str, Any]]:
        """Fetch archives (richest-first), extract frames, write the index.

        Returns the index rows.  Existing images are never re-extracted, and
        an archive file is only downloaded if one of its frames is missing.
        """
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
            targets = {f: self.output_dir / f"{f.image_name}.{self.image_format}" for f in frames_here}
            pending = [f for f, path in targets.items() if not path.exists()]

            video_path = self.video_dir / name
            downloaded_now = False
            if pending:
                if video_path.exists() and video_path.stat().st_size > 1_000_000:
                    print(f"[{i}/{len(archives)}] using cached {name}")
                else:
                    print(f"[{i}/{len(archives)}] downloading {name} for {len(pending)} image(s)")
                    try:
                        self.client.download_archive_file(name, str(video_path))
                        downloaded_now = True
                    except Exception as exc:
                        print(f"[WARN] download failed for {name}: {exc}")
                        continue
            else:
                print(f"[{i}/{len(archives)}] all {len(frames_here)} image(s) already present")

            for frame in frames_here:
                image_path = targets[frame]
                if not image_path.exists():
                    if not self._extract_frame(video_path, frame.offset_seconds, image_path):
                        continue
                    written += 1
                row = self._index_row(frame, image_path.name)
                rows.append(row)
                records.append({**row, "annotations": [a.raw for a in frame.annotations]})

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
        print(f"\n{written} new image(s) written, {len(rows)} indexed in {self.output_dir}")
        return rows

    # -- internals ----------------------------------------------------------

    def _extract_frame(self, video_path: Path, offset_seconds: float, image_path: Path) -> bool:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{offset_seconds:.3f}",
            "-i", str(video_path),
            "-frames:v", "1",
        ]
        if image_path.suffix.lower() in {".jpg", ".jpeg"}:
            cmd += ["-q:v", str(self.jpeg_quality)]
        cmd.append(str(image_path))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not image_path.exists():
            print(f"[WARN] ffmpeg failed for {image_path.name}: {result.stderr.strip()[:200]}")
            return False
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
        index_csv = self.output_dir / "images_index.csv"
        with index_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        index_jsonl = self.output_dir / "images_index.jsonl"
        with index_jsonl.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        print(f"  {index_csv}\n  {index_jsonl}")
