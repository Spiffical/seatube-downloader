"""The ``seatube`` command: explore ONC's SeaTube archive from the terminal.

    survey      which dives/locations have annotations in a date range
    fetch       pull filtered annotations from ONC into a local file
    annotators  who annotated, how much, when
    taxa        what was annotated, counts by taxon
    clips       video files + timestamps for chosen annotations
    images      extract labelled still images
    videos      download whole archive video files
    groups      the broad taxon-group vocabulary
    locations   fixed-camera location ids

``fetch`` is the only command that must query ONC; everything below it works
offline on the fetched file, so you can slice one fetch many ways for free.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .annotations import AnnotationSet, ReviewFilters
from .client import OncClient
from .fetch import AnnotationFetcher, FetchFilters, fixed_camera_locations
from .images import ImageDownloader, build_frames, select_frames
from .taxonomy import WormsResolver, format_group_table, normalize_group_name

DEFAULT_ANNOTATIONS = "downloads/annotations.json"
LEGACY_ANNOTATIONS = "downloads/matched_annotations.json"


# ---------------------------------------------------------------------------
# shared option groups
# ---------------------------------------------------------------------------

def add_date_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start-date", required=True,
                        help="ISO8601 UTC start (e.g. 2023-06-01T00:00:00.000Z)")
    parser.add_argument("--end-date", required=True, help="ISO8601 UTC end")


def add_offline_filter_args(parser: argparse.ArgumentParser) -> None:
    """Filters that run locally on an already-fetched annotation file."""
    group = parser.add_argument_group("filters")
    group.add_argument("--group", action="append", default=[], metavar="NAME",
                       help="Broad taxon group, repeatable (see `seatube groups`)")
    group.add_argument("--taxon-name", action="append", default=[], metavar="TAXON",
                       help="Any WoRMS ancestor name, repeatable (e.g. Brachyura)")
    group.add_argument("--taxon-contains", help="Substring match on the taxon label")
    group.add_argument("--creator", help="Substring match on annotator name")
    group.add_argument("--creator-id", type=int, help="Exact ONC user id of the annotator")
    group.add_argument("--reviewed-only", action="store_true",
                       help="Keep only annotations that appear reviewed")
    group.add_argument("--min-total-reviews", type=int)
    group.add_argument("--require-comment", action="store_true")
    group.add_argument("--dive-name-contains")
    group.add_argument("--location-contains")
    group.add_argument("--camera-mode", choices=["dive", "stationary"],
                       help="Keep only one camera mode")


def add_annotations_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--annotations", default=DEFAULT_ANNOTATIONS,
                        help=f"Annotation file from `seatube fetch` (default: {DEFAULT_ANNOTATIONS})")


def validate_groups(parser: argparse.ArgumentParser, names: Sequence[str]) -> None:
    for name in names:
        try:
            normalize_group_name(name)
        except KeyError:
            parser.error(f"unknown group {name!r}. Run `seatube groups` for the vocabulary.")


def load_annotations(args: argparse.Namespace) -> AnnotationSet:
    path = Path(args.annotations)
    if not path.exists():
        legacy = Path(LEGACY_ANNOTATIONS)
        if str(path) == DEFAULT_ANNOTATIONS and legacy.exists():
            print(f"[note] {path} not found; using legacy {legacy}")
            path = legacy
        else:
            raise SystemExit(f"[ERROR] {path} not found. Run `seatube fetch` first.")
    result = AnnotationSet.load(str(path))
    print(f"Loaded {len(result)} annotations from {path}")
    return result


def make_resolver(args: argparse.Namespace) -> WormsResolver:
    cache = getattr(args, "worms_cache", None) or str(
        Path(args.annotations).parent / ".worms_cache.json"
    )
    return WormsResolver(cache, offline=getattr(args, "offline_taxa", False))


def apply_offline_filters(annotations: AnnotationSet, args: argparse.Namespace) -> AnnotationSet:
    resolver = make_resolver(args)
    result = annotations.filter(
        groups=args.group,
        taxa=args.taxon_name,
        taxon_contains=args.taxon_contains,
        creator=args.creator,
        creator_id=args.creator_id,
        review=ReviewFilters(
            reviewed_only=args.reviewed_only,
            min_total_reviews=args.min_total_reviews,
        ),
        require_comment=args.require_comment,
        dive_contains=args.dive_name_contains,
        location_contains=args.location_contains,
        camera_mode=args.camera_mode,
        resolver=resolver,
    )
    resolver.save()
    if resolver.unresolved:
        print(f"[WARN] {len(resolver.unresolved)} taxa could not be resolved and were skipped")
    if len(result) != len(annotations):
        print(f"{len(result)} annotations pass the filters")
    return result


def write_csv(path: str, rows: List[Dict[str, Any]], columns: Sequence[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {path}")


def print_table(rows: List[Dict[str, Any]], columns: List[str], limit: Optional[int]) -> None:
    if not rows:
        print("Nothing matched.")
        return
    shown = rows if limit is None else rows[:limit]
    widths = {
        c: max(len(c), max(len(str(r.get(c, ""))) for r in shown)) for c in columns
    }
    header = "  ".join(f"{c:<{widths[c]}}" for c in columns)
    print(header)
    print("-" * len(header))
    for row in shown:
        print("  ".join(f"{str(row.get(c, '')):<{widths[c]}}" for c in columns))
    if limit is not None and len(rows) > limit:
        print(f"... and {len(rows) - limit} more (use --limit 0 for all, --csv for a file)")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_survey(args: argparse.Namespace) -> int:
    from .survey import run_survey

    client = OncClient.from_env() if not args.token else OncClient(args.token)
    client.token = args.token or client.token
    client.require_token()
    print("Ordering a SeaTube annotation export from ONC (can take a minute or two)...")
    result = run_survey(
        client.token, args.start_date, args.end_date,
        taxonomy_id=args.taxonomy_id, location_code=args.location_code,
    )
    if not result["scopes"]:
        print("No annotations found for this range.")
        return 1

    rows = [
        {"scope": r["scope"], "annotations": r["annotations"],
         "annotators": ", ".join(r["annotators"][:3]) + ("..." if len(r["annotators"]) > 3 else "")}
        for r in result["scopes"]
    ]
    print_table(rows, ["scope", "annotations", "annotators"], args.limit or None)
    print("\nTop annotators across the range:")
    print_table(result["annotators"][:10], ["annotator", "annotations"], None)
    print("\nUse these dives/locations and dates with `seatube fetch`.")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    client = OncClient(args.token, timeout_seconds=args.timeout_seconds)
    client.require_token()

    cache = args.worms_cache or os.path.join(args.output_dir, ".worms_cache.json")
    fetcher = AnnotationFetcher(client, resolver=WormsResolver(cache))
    filters = FetchFilters(
        start_date=args.start_date,
        end_date=args.end_date,
        camera_mode=args.camera_mode,
        taxonomy_code=args.taxonomy_code,
        taxonomy_id=args.taxonomy_id,
        taxon_ids={int(x) for x in (args.taxon_id or "").split(",") if x.strip()},
        groups=tuple(args.group),
        taxon_names=tuple(args.taxon_name),
        creator_id=args.creator_id,
        creator_name=args.creator,
        creator_email=args.creator_email,
        modifier_id=args.modifier_id,
        modifier_name=args.modifier,
        modifier_email=args.modifier_email,
        review=ReviewFilters(
            reviewed_only=args.reviewed_only,
            min_total_reviews=args.min_total_reviews,
            min_positive_reviews=args.min_positive_reviews,
            min_positive_review_rate=args.min_positive_review_rate,
            require_cross_review=args.require_cross_review,
        ),
        dive_ids={int(x) for x in (args.dive_id or "").split(",") if x.strip()},
        node_ids={int(x) for x in (args.search_tree_node_id or "").split(",") if x.strip()},
        location_contains=args.location_name_contains,
        max_stationary_locations=args.max_stationary_locations,
        resolution=args.resolution,
        max_dives=args.max_dives,
        page_size=args.page_size,
        resolve_taxon_names=not args.skip_taxon_name_resolution,
    )

    result = fetcher.fetch(filters)

    out = Path(args.output_dir) / "annotations.json"
    result.save(str(out))
    print(f"\nSaved {len(result)} annotations to {out}")
    if args.flat_exports:
        csv_path = Path(args.output_dir) / "annotations_flat.csv"
        jsonl_path = Path(args.output_dir) / "annotations_flat.jsonl"
        result.write_flat_csv(str(csv_path))
        result.write_flat_jsonl(str(jsonl_path))
        print(f"Flat exports: {csv_path}, {jsonl_path}")
    print("Explore it offline: seatube annotators / taxa / clips / images")
    return 0


def cmd_annotators(args: argparse.Namespace) -> int:
    annotations = apply_offline_filters(load_annotations(args), args)
    stats = annotations.annotator_summary()
    rows = [
        {
            "annotator": s.name,
            "user_id": s.user_id or "",
            "annotations": s.annotations,
            "reviewed": s.reviewed,
            "taxa": len(s.distinct_taxa),
            "dives/sites": len(s.places),
            "first": (s.first_utc or "")[:10],
            "last": (s.last_utc or "")[:10],
            "top_taxa": ", ".join(s.top_taxa),
        }
        for s in stats
    ]
    print_table(rows, ["annotator", "user_id", "annotations", "reviewed", "taxa",
                       "dives/sites", "first", "last", "top_taxa"], args.limit or None)
    if args.csv:
        write_csv(args.csv, rows, list(rows[0].keys()) if rows else [])
    return 0


def cmd_taxa(args: argparse.Namespace) -> int:
    annotations = apply_offline_filters(load_annotations(args), args)
    resolver = make_resolver(args) if args.show_groups else None
    stats = annotations.taxon_summary(resolver=resolver)
    if resolver is not None:
        resolver.save()
    rows = [
        {
            "taxon": s.name,
            "aphia_id": s.aphia_id or "",
            "annotations": s.annotations,
            "annotators": len(s.annotators),
            "dives/sites": len(s.places),
            **({"groups": ", ".join(s.groups)} if args.show_groups else {}),
        }
        for s in stats
    ]
    columns = ["taxon", "aphia_id", "annotations", "annotators", "dives/sites"]
    if args.show_groups:
        columns.append("groups")
    print_table(rows, columns, args.limit or None)
    if args.csv:
        write_csv(args.csv, rows, columns)
    return 0


def cmd_clips(args: argparse.Namespace) -> int:
    annotations = apply_offline_filters(load_annotations(args), args)
    rows = annotations.clip_index(window_seconds=args.window_seconds)
    if not rows:
        print("No annotations are mapped to archive files.")
        return 1
    display = [
        {
            "archive_file": r["archive_filename"],
            "offset_s": r["offset_seconds"],
            "utc": r["first_utc"],
            "count": r["annotation_count"],
            "taxa": ", ".join(r["taxa"][:4]) + ("..." if len(r["taxa"]) > 4 else ""),
        }
        for r in rows
    ]
    display.sort(key=lambda r: -r["count"])
    print_table(display, ["archive_file", "offset_s", "utc", "count", "taxa"], args.limit or None)
    print(f"\n{len(rows)} rows across {len({r['archive_filename'] for r in rows})} archive file(s). "
          "Watch any moment via the seatube_link column in --csv output.")
    if args.csv:
        flat = [
            {**r, "taxa": "; ".join(r["taxa"]),
             "annotation_ids": "; ".join(str(i) for i in r["annotation_ids"])}
            for r in rows
        ]
        write_csv(args.csv, flat, ["archive_filename", "offset_seconds", "window_seconds",
                                   "first_utc", "annotation_count", "taxa", "annotation_ids",
                                   "place", "seatube_link"])
    return 0


def cmd_images(args: argparse.Namespace) -> int:
    if not args.dry_run:
        client = OncClient(args.token, timeout_seconds=args.timeout_seconds)
        client.require_token()
    else:
        client = OncClient(args.token, timeout_seconds=args.timeout_seconds)

    annotations = apply_offline_filters(load_annotations(args), args)
    frames = build_frames(annotations, dedupe_seconds=args.dedupe_seconds)
    if not frames:
        print("No frames to extract. Loosen the filters, or check that the fetch "
              "mapped annotations to archive files.")
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

    downloader = ImageDownloader(
        client,
        args.output_dir,
        image_format=args.image_format,
        jpeg_quality=args.jpeg_quality,
        video_dir=args.video_dir,
        keep_videos=args.keep_videos,
        resolver=make_resolver(args),
    )
    if args.dry_run:
        print(downloader.describe_plan(selected))
        return 0
    downloader.download(selected)
    return 0


def cmd_videos(args: argparse.Namespace) -> int:
    client = OncClient(args.token, timeout_seconds=args.timeout_seconds)
    client.require_token()
    annotations = apply_offline_filters(load_annotations(args), args)
    filenames = sorted({a.archive_filename for a in annotations if a.archive_filename})
    if args.max_files is not None:
        filenames = filenames[: args.max_files]
    if not filenames:
        print("No archive files referenced by these annotations.")
        return 1
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{len(filenames)} archive file(s) to download into {out_dir}")
    for i, name in enumerate(filenames, start=1):
        path = out_dir / name
        if path.exists() and path.stat().st_size > 1_000_000:
            print(f"[{i}/{len(filenames)}] exists, skipping: {name}")
            continue
        print(f"[{i}/{len(filenames)}] downloading {name}")
        try:
            client.download_archive_file(name, str(path))
        except Exception as exc:
            print(f"[WARN] failed: {exc}")
    return 0


def cmd_groups(args: argparse.Namespace) -> int:
    print(format_group_table())
    return 0


def cmd_locations(args: argparse.Namespace) -> int:
    client = OncClient(args.token)
    locations = fixed_camera_locations(client)
    rows = [{"id": loc["searchTreeNodeId"], "path": loc["path"]} for loc in locations]
    print_table(rows, ["id", "path"], args.limit or None)
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    from dotenv import load_dotenv

    load_dotenv()
    token_default = os.getenv("ONC_TOKEN")

    parser = argparse.ArgumentParser(
        prog="seatube",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, token: bool = True) -> None:
        if token:
            p.add_argument("--token", default=token_default,
                           help="ONC API token (or ONC_TOKEN in .env)")
        p.add_argument("--limit", type=int, default=25,
                       help="Rows to print (0 = all; default 25)")

    # survey
    p = sub.add_parser("survey", help="which dives/locations have annotations in a date range")
    add_date_args(p)
    p.add_argument("--taxonomy-id", type=int, default=1, help="1=WoRMS (default), 2=CMECS")
    p.add_argument("--location-code", help="Optional ONC location code (e.g. CBBNC)")
    common(p)
    p.set_defaults(func=cmd_survey)

    # fetch
    p = sub.add_parser("fetch", help="pull filtered annotations from ONC into a local file")
    add_date_args(p)
    p.add_argument("--camera-mode", choices=["dive", "stationary", "both"], default="dive")
    p.add_argument("--group", action="append", default=[], metavar="NAME",
                   help="Broad taxon group, repeatable (see `seatube groups`)")
    p.add_argument("--taxon-name", action="append", default=[], metavar="TAXON",
                   help="Any WoRMS ancestor name, repeatable")
    p.add_argument("--taxonomy-code", default="WoRMS",
                   help="Taxonomy code filter (default WoRMS; empty string disables)")
    p.add_argument("--taxonomy-id", type=int)
    p.add_argument("--taxon-id", help="Comma-separated ONC-internal taxon ids")
    p.add_argument("--creator", help="Substring match on annotator name")
    p.add_argument("--creator-id", type=int, help="Exact ONC user id")
    p.add_argument("--creator-email")
    p.add_argument("--modifier", help="Substring match on reviewer name")
    p.add_argument("--modifier-id", type=int)
    p.add_argument("--modifier-email")
    p.add_argument("--reviewed-only", action="store_true")
    p.add_argument("--min-total-reviews", type=int)
    p.add_argument("--min-positive-reviews", type=int)
    p.add_argument("--min-positive-review-rate", type=float)
    p.add_argument("--require-cross-review", action="store_true")
    p.add_argument("--dive-id", help="Comma-separated dive ids")
    p.add_argument("--search-tree-node-id", help="Comma-separated fixed-camera node ids")
    p.add_argument("--location-name-contains")
    p.add_argument("--max-stationary-locations", type=int)
    p.add_argument("--resolution", choices=["H", "L", "S"], default="L",
                   help="Video resolution the annotations map onto (default L)")
    p.add_argument("--max-dives", type=int)
    p.add_argument("--page-size", type=int, default=250)
    p.add_argument("--skip-taxon-name-resolution", action="store_true")
    p.add_argument("--output-dir", default="downloads")
    p.add_argument("--flat-exports", action="store_true",
                   help="Also write one-row-per-taxon CSV and JSONL")
    p.add_argument("--worms-cache")
    p.add_argument("--timeout-seconds", type=int, default=45)
    common(p)
    p.set_defaults(func=cmd_fetch)

    # offline explorers
    for name, func, help_text in [
        ("annotators", cmd_annotators, "who annotated, how much, when"),
        ("taxa", cmd_taxa, "what was annotated, counts by taxon"),
        ("clips", cmd_clips, "video files + timestamps for chosen annotations"),
    ]:
        p = sub.add_parser(name, help=help_text)
        add_annotations_arg(p)
        add_offline_filter_args(p)
        p.add_argument("--csv", help="Also write the full table to this CSV file")
        p.add_argument("--worms-cache")
        p.add_argument("--offline-taxa", action="store_true",
                       help="Never call WoRMS; use the cache and labels only")
        if name == "taxa":
            p.add_argument("--show-groups", action="store_true",
                           help="Label each taxon with its broad groups (uses WoRMS)")
        if name == "clips":
            p.add_argument("--window-seconds", type=float, default=0.0,
                           help="Merge annotations within N-second windows (0 = exact instants)")
        common(p, token=False)
        p.set_defaults(func=func)

    # images
    p = sub.add_parser("images", help="extract labelled still images")
    add_annotations_arg(p)
    add_offline_filter_args(p)
    p.add_argument("--output-dir", default="images")
    p.add_argument("--max-images", type=int, help="Stop after this many images")
    p.add_argument("--max-videos", type=int, help="Hard cap on archive files downloaded")
    p.add_argument("--max-per-taxon", type=int, help="Cap images per taxon, for a balanced set")
    p.add_argument("--dedupe-seconds", type=float, default=0.0,
                   help="Merge annotations within N seconds onto one frame")
    p.add_argument("--image-format", choices=["jpg", "png"], default="jpg")
    p.add_argument("--jpeg-quality", type=int, default=2, help="ffmpeg -q:v; 2 is near-lossless")
    p.add_argument("--video-dir", help="Archive cache dir (default <output-dir>/_videos)")
    p.add_argument("--keep-videos", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="Report the download cost in bytes, write nothing")
    p.add_argument("--worms-cache")
    p.add_argument("--offline-taxa", action="store_true")
    p.add_argument("--timeout-seconds", type=int, default=600)
    common(p)
    p.set_defaults(func=cmd_images)

    # videos
    p = sub.add_parser("videos", help="download whole archive video files")
    add_annotations_arg(p)
    add_offline_filter_args(p)
    p.add_argument("--output-dir", default="downloads/videos")
    p.add_argument("--max-files", type=int, help="Cap on archive files to download")
    p.add_argument("--worms-cache")
    p.add_argument("--offline-taxa", action="store_true")
    p.add_argument("--timeout-seconds", type=int, default=600)
    common(p)
    p.set_defaults(func=cmd_videos)

    # groups / locations
    p = sub.add_parser("groups", help="the broad taxon-group vocabulary")
    p.set_defaults(func=cmd_groups)

    p = sub.add_parser("locations", help="fixed-camera location ids")
    common(p)
    p.set_defaults(func=cmd_locations)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "group"):
        validate_groups(parser, args.group)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
