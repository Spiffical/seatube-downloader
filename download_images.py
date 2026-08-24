#!/usr/bin/env python3
"""Turn matched SeaTube annotations into annotated still images.

ONC serves annotated video, not stills, and its archive endpoint ignores HTTP
range requests -- a single frame costs one whole archive file (~200 MB for a
five-minute clip).  So the work here is mostly about downloading as little as
possible: annotations are grouped into frames, frames are grouped into the
archive files that contain them, and files are visited in the order that
yields the most images per download.  Each file is deleted once its frames are
out unless --keep-videos is passed.

Input is the matched_annotations.json written by download_seatube.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

from download_seatube import ONCApiClient, parse_iso_utc, to_iso_utc
from taxon_groups import (
    TaxonResolver,
    aphia_id_from_taxon,
    format_group_table,
    normalize_group,
    resolve_wanted,
    taxon_names,
)

INDEX_COLUMNS = [
    "image_file",
    "frame_utc",
    "archive_filename",
    "offset_seconds",
    "taxa",
    "worms_aphia_ids",
    "groups",
    "annotation_ids",
    "annotation_count",
    "camera_mode",
    "dive_name",
    "location",
    "lat",
    "lon",
    "depth_m",
    "creators",
    "seatube_link",
]


class Frame:
    """One still to extract: a point in one archive file, plus its labels."""

    def __init__(self, archive_filename: str, offset_seconds: float, frame_utc: str) -> None:
        self.archive_filename = archive_filename
        self.offset_seconds = offset_seconds
        self.frame_utc = frame_utc
        self.annotations: List[Dict[str, Any]] = []

    @property
    def key(self) -> Tuple[str, float]:
        return (self.archive_filename, round(self.offset_seconds, 3))

    @property
    def image_name(self) -> str:
        stem = Path(self.archive_filename).stem.replace(" ", "_")
        return f"{stem}_t{self.offset_seconds:07.2f}"

    def taxa(self, resolver: Optional[TaxonResolver] = None) -> List[Dict[str, Any]]:
        """Distinct taxonomy entries across every annotation on this frame."""
        seen: Dict[str, Dict[str, Any]] = {}
        for ann in self.annotations:
            for taxon in ann.get("taxonomy") or []:
                names = taxon_names(taxon)
                label = names[0] if names else "unknown"
                if label not in seen:
                    entry = {"name": label, "aphia_id": aphia_id_from_taxon(taxon), "groups": []}
                    if resolver is not None:
                        entry["groups"] = resolver.groups_for(taxon)
                    seen[label] = entry
        return list(seen.values())


def annotation_offset(annotation: Dict[str, Any]) -> Optional[float]:
    """Seconds from the start of the archive file to the annotation instant.

    ``clipOffsetSeconds`` is the file's offset within the device's media
    series, not a position inside the file, so the offset has to come from the
    timestamps.  Annotations that fall outside the file are dropped rather
    than clamped -- clamping silently produces confident, wrong frames.
    """
    start = annotation.get("startDate")
    clip_start = annotation.get("archiveClipStartDate")
    if not start or not clip_start or not annotation.get("archiveFilename"):
        return None
    try:
        rel = (parse_iso_utc(start) - parse_iso_utc(clip_start)).total_seconds()
    except (ValueError, TypeError):
        return None
    if rel < 0:
        return None
    try:
        duration = float(annotation.get("clipDurationSeconds"))
    except (TypeError, ValueError):
        duration = None
    if duration is not None and rel >= duration:
        return None
    return rel


def passes_review_filters(annotation: Dict[str, Any], args: argparse.Namespace) -> bool:
    if args.reviewed_only:
        to_be_reviewed = annotation.get("toBeReviewed")
        total = annotation.get("numTotalReviews") or 0
        if to_be_reviewed is not False and not total:
            return False
    if args.min_total_reviews is not None:
        if (annotation.get("numTotalReviews") or 0) < args.min_total_reviews:
            return False
    return True


def passes_text_filters(annotation: Dict[str, Any], args: argparse.Namespace) -> bool:
    if args.taxon_contains:
        needle = args.taxon_contains.lower()
        haystack = " ".join(
            " ".join(taxon_names(t)) + " " + str(t.get("displayText") or "")
            for t in (annotation.get("taxonomy") or [])
        ).lower()
        if needle not in haystack:
            return False
    if args.dive_name_contains:
        name = str(annotation.get("diveName") or "")
        if args.dive_name_contains.lower() not in name.lower():
            return False
    if args.location_contains:
        blob = " ".join(str(annotation.get(k) or "") for k in
                        ("stationaryLocationName", "stationaryLocationPath"))
        if args.location_contains.lower() not in blob.lower():
            return False
    if args.require_comment and not str(annotation.get("comment") or "").strip():
        return False
    return True


def build_frames(
    annotations: Sequence[Dict[str, Any]],
    dedupe_seconds: float,
) -> List[Frame]:
    """Collapse annotations onto the frames that show them."""
    frames: Dict[Tuple[str, float], Frame] = {}
    for ann in annotations:
        offset = annotation_offset(ann)
        if offset is None:
            continue
        bucket = round(offset / dedupe_seconds) * dedupe_seconds if dedupe_seconds > 0 else offset
        key = (ann["archiveFilename"], round(bucket, 3))
        frame = frames.get(key)
        if frame is None:
            clip_start = parse_iso_utc(ann["archiveClipStartDate"])
            frame = Frame(
                archive_filename=ann["archiveFilename"],
                offset_seconds=round(bucket, 3),
                frame_utc=to_iso_utc(clip_start + timedelta(seconds=bucket)),
            )
            frames[key] = frame
        frame.annotations.append(ann)
    return list(frames.values())


def select_frames(
    frames: Sequence[Frame],
    *,
    max_images: Optional[int],
    max_videos: Optional[int],
    max_per_taxon: Optional[int],
) -> List[Frame]:
    """Choose frames, cheapest-first.

    Archive files are ranked by how many wanted frames they hold, so a small
    --max-images is satisfied by the fewest possible downloads.
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


def extract_frame(video_path: Path, offset_seconds: float, image_path: Path, quality: int) -> bool:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{offset_seconds:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
    ]
    if image_path.suffix.lower() in {".jpg", ".jpeg"}:
        cmd += ["-q:v", str(quality)]
    cmd.append(str(image_path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not image_path.exists():
        print(f"[WARN] ffmpeg failed for {image_path.name}: {result.stderr.strip()[:200]}")
        return False
    return True


def archive_size_bytes(client: ONCApiClient, filename: str) -> Optional[int]:
    try:
        resp = client.session.head(
            "https://data.oceannetworks.ca/api/archivefile/download",
            params={"filename": filename, "token": client.token},
            timeout=client.timeout_seconds,
            allow_redirects=True,
        )
        resp.raise_for_status()
        length = resp.headers.get("Content-Length")
        return int(length) if length else None
    except Exception:
        return None


def index_row(frame: Frame, image_file: str, resolver: Optional[TaxonResolver]) -> Dict[str, Any]:
    taxa = frame.taxa(resolver)
    first = frame.annotations[0]
    groups = sorted({g for t in taxa for g in t.get("groups") or []})
    creators = sorted({
        f"{(a.get('createdBy') or {}).get('firstName', '')} "
        f"{(a.get('createdBy') or {}).get('lastName', '')}".strip()
        for a in frame.annotations
    } - {""})
    return {
        "image_file": image_file,
        "frame_utc": frame.frame_utc,
        "archive_filename": frame.archive_filename,
        "offset_seconds": round(frame.offset_seconds, 3),
        "taxa": "; ".join(t["name"] for t in taxa),
        "worms_aphia_ids": "; ".join(str(t["aphia_id"]) for t in taxa if t["aphia_id"]),
        "groups": "; ".join(groups),
        "annotation_ids": "; ".join(str(a.get("annotationId")) for a in frame.annotations),
        "annotation_count": len(frame.annotations),
        "camera_mode": first.get("cameraMode"),
        "dive_name": first.get("diveName"),
        "location": first.get("stationaryLocationName") or first.get("cruiseName"),
        "lat": first.get("lat"),
        "lon": first.get("lon"),
        "depth_m": first.get("depth"),
        "creators": "; ".join(creators),
        "seatube_link": first.get("contextualLink"),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--annotations", default="downloads/matched_annotations.json",
                        help="matched_annotations.json from download_seatube.py")
    parser.add_argument("--output-dir", default="images", help="Where images and the index are written")
    parser.add_argument("--token", default=os.getenv("ONC_TOKEN"), help="ONC API token (or ONC_TOKEN in .env)")

    parser.add_argument("--group", action="append", default=[], metavar="NAME",
                        help="Broad taxon group, repeatable (e.g. --group crabs --group sponges)")
    parser.add_argument("--taxon-name", action="append", default=[], metavar="TAXON",
                        help="Any WoRMS ancestor name, repeatable (e.g. --taxon-name Brachyura)")
    parser.add_argument("--taxon-contains", help="Case-insensitive substring match on the annotation's taxon label")
    parser.add_argument("--list-groups", action="store_true", help="Print the group vocabulary and exit")

    parser.add_argument("--reviewed-only", action="store_true", help="Keep only reviewed annotations")
    parser.add_argument("--min-total-reviews", type=int, help="Keep annotations with at least this many reviews")
    parser.add_argument("--require-comment", action="store_true", help="Keep only annotations with a comment")
    parser.add_argument("--dive-name-contains", help="Substring match on dive name")
    parser.add_argument("--location-contains", help="Substring match on stationary location name/path")

    parser.add_argument("--max-images", type=int, help="Stop after this many images")
    parser.add_argument("--max-videos", type=int, help="Never download more than this many archive files")
    parser.add_argument("--max-per-taxon", type=int, help="Cap images per taxon, for a balanced set")
    parser.add_argument("--dedupe-seconds", type=float, default=0.0,
                        help="Merge annotations within this many seconds onto one frame (0 = exact instant)")

    parser.add_argument("--image-format", choices=["jpg", "png"], default="jpg")
    parser.add_argument("--jpeg-quality", type=int, default=2, help="ffmpeg -q:v, 2 is near-lossless")
    parser.add_argument("--video-dir", help="Where archive files are cached (default: <output-dir>/_videos)")
    parser.add_argument("--keep-videos", action="store_true", help="Keep archive files after extracting frames")
    parser.add_argument("--dry-run", action="store_true", help="Report the plan and download size, write nothing")
    parser.add_argument("--worms-cache", help="WoRMS lineage cache (default: <output-dir>/.worms_cache.json)")
    parser.add_argument("--offline-taxa", action="store_true",
                        help="Never call WoRMS; use the cache and annotation labels only")
    parser.add_argument("--timeout-seconds", type=int, default=600)

    args = parser.parse_args(argv)
    if args.list_groups:
        print(format_group_table())
        raise SystemExit(0)
    for name in args.group:
        try:
            normalize_group(name)
        except KeyError:
            parser.error(f"unknown group {name!r}. Run --list-groups to see the vocabulary.")
    if not args.dry_run and not args.token:
        parser.error("ONC token must be provided via --token or ONC_TOKEN in .env")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    annotations_path = Path(args.annotations)
    if not annotations_path.exists():
        print(f"[ERROR] {annotations_path} not found. Run download_seatube.py first.")
        return 2
    annotations = json.loads(annotations_path.read_text())
    print(f"Loaded {len(annotations)} annotations from {annotations_path}")

    output_dir = Path(args.output_dir)
    cache_path = args.worms_cache or str(output_dir / ".worms_cache.json")
    wanted = resolve_wanted(args.group, args.taxon_name)
    resolver = TaxonResolver(cache_path, offline=args.offline_taxa)

    kept = [a for a in annotations if passes_review_filters(a, args) and passes_text_filters(a, args)]
    if wanted:
        print(f"Resolving taxonomy against {len(wanted)} ancestor taxa "
              f"({', '.join(sorted(wanted))}) -- WoRMS lookups are cached")
        kept = [a for a in kept if resolver.annotation_matches(a, wanted)]
        resolver.save()
        if resolver.unresolved:
            print(f"[WARN] {len(resolver.unresolved)} taxa could not be resolved and were skipped")
    print(f"{len(kept)} annotations pass the filters")

    frames = build_frames(kept, args.dedupe_seconds)
    if not frames:
        print("No frames to extract. Loosen the filters, or check that the annotations "
              "carry archiveFilename (run download_seatube.py without --skip-taxon-name-resolution).")
        return 1
    selected = select_frames(
        frames,
        max_images=args.max_images,
        max_videos=args.max_videos,
        max_per_taxon=args.max_per_taxon,
    )
    archives = sorted({f.archive_filename for f in selected})
    print(f"{len(frames)} distinct frames available; {len(selected)} selected "
          f"from {len(archives)} archive file(s)")

    client = ONCApiClient(token=args.token, timeout_seconds=args.timeout_seconds)

    if args.dry_run:
        total = 0
        unknown = 0
        for name in archives:
            size = archive_size_bytes(client, name) if args.token else None
            if size:
                total += size
            else:
                unknown += 1
            count = sum(1 for f in selected if f.archive_filename == name)
            size_text = f"{size / 1e9:.2f} GB" if size else "size unknown"
            print(f"  {name}  ->  {count} image(s), {size_text}")
        note = f" (+{unknown} of unknown size)" if unknown else ""
        print(f"\nWould download {total / 1e9:.2f} GB{note} to produce {len(selected)} images.")
        return 0

    video_dir = Path(args.video_dir) if args.video_dir else output_dir / "_videos"
    output_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []
    by_archive: Dict[str, List[Frame]] = defaultdict(list)
    for frame in selected:
        by_archive[frame.archive_filename].append(frame)

    written = 0
    for i, name in enumerate(archives, start=1):
        frames_here = sorted(by_archive[name], key=lambda f: f.offset_seconds)
        targets = {f: output_dir / f"{f.image_name}.{args.image_format}" for f in frames_here}
        pending = [f for f, path in targets.items() if not path.exists()]

        video_path = video_dir / name
        downloaded_now = False
        if pending:
            if video_path.exists() and video_path.stat().st_size > 1_000_000:
                print(f"[{i}/{len(archives)}] using cached {name}")
            else:
                print(f"[{i}/{len(archives)}] downloading {name} for {len(pending)} image(s)")
                try:
                    client.download_archive_file(name, str(video_path))
                    downloaded_now = True
                except Exception as exc:
                    print(f"[WARN] download failed for {name}: {exc}")
                    continue
        else:
            print(f"[{i}/{len(archives)}] all {len(frames_here)} image(s) already present")

        for frame in frames_here:
            image_path = targets[frame]
            if not image_path.exists():
                if not extract_frame(video_path, frame.offset_seconds, image_path, args.jpeg_quality):
                    continue
                written += 1
            row = index_row(frame, image_path.name, resolver)
            rows.append(row)
            records.append({**row, "annotations": frame.annotations})

        if downloaded_now and not args.keep_videos:
            video_path.unlink(missing_ok=True)

    resolver.save()
    if not args.keep_videos:
        try:
            video_dir.rmdir()
        except OSError:
            pass

    index_csv = output_dir / "images_index.csv"
    with index_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    index_jsonl = output_dir / "images_index.jsonl"
    with index_jsonl.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")

    print(f"\n{written} new image(s) written, {len(rows)} indexed in {output_dir}")
    print(f"  {index_csv}\n  {index_jsonl}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
