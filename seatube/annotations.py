"""Annotation records and the operations you do on a set of them.

``AnnotationSet`` is the package's central object: everything downstream of a
fetch -- filtering, annotator leaderboards, taxon counts, clip listings, ML
exports, image extraction -- runs off one of these, entirely offline.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .archive import parse_iso_utc
from .taxonomy import WormsResolver, aphia_id_from_taxon, taxon_names


# ---------------------------------------------------------------------------
# record wrappers
# ---------------------------------------------------------------------------

class Taxon:
    """One taxonomy entry on an annotation."""

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.raw = raw

    @property
    def names(self) -> List[str]:
        return taxon_names(self.raw)

    @property
    def primary_name(self) -> str:
        names = self.names
        return names[0] if names else "unknown"

    @property
    def aphia_id(self) -> Optional[int]:
        return aphia_id_from_taxon(self.raw)

    @property
    def taxonomy_code(self) -> Optional[str]:
        return self.raw.get("taxonomyCode")

    @property
    def display_text(self) -> Optional[str]:
        return self.raw.get("displayText")

    def __repr__(self) -> str:
        return f"Taxon({self.primary_name!r}, aphia_id={self.aphia_id})"


class Annotation:
    """One SeaTube annotation, wrapping the raw record from ONC."""

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.raw = raw

    # -- identity and time -------------------------------------------------

    @property
    def id(self) -> Optional[int]:
        return self.raw.get("annotationId")

    @property
    def start_utc(self) -> Optional[str]:
        return self.raw.get("startDate")

    @property
    def start(self) -> Optional[datetime]:
        try:
            return parse_iso_utc(self.raw["startDate"])
        except (KeyError, ValueError, TypeError):
            return None

    @property
    def comment(self) -> str:
        return str(self.raw.get("comment") or "").strip()

    @property
    def seatube_link(self) -> Optional[str]:
        return self.raw.get("contextualLink")

    # -- where -------------------------------------------------------------

    @property
    def camera_mode(self) -> Optional[str]:
        return self.raw.get("cameraMode")

    @property
    def dive_name(self) -> Optional[str]:
        return self.raw.get("diveName")

    @property
    def location_name(self) -> Optional[str]:
        return self.raw.get("stationaryLocationName") or self.raw.get("cruiseName")

    @property
    def place(self) -> str:
        """Dive name for ROV annotations, location name for fixed cameras."""
        return self.dive_name or self.raw.get("stationaryLocationName") or "unknown"

    @property
    def lat(self) -> Optional[float]:
        return self.raw.get("lat")

    @property
    def lon(self) -> Optional[float]:
        return self.raw.get("lon")

    @property
    def depth_m(self) -> Optional[float]:
        return self.raw.get("depth")

    # -- who ---------------------------------------------------------------

    @property
    def creator(self) -> Dict[str, Any]:
        return self.raw.get("createdBy") or {}

    @property
    def creator_id(self) -> Optional[int]:
        return self.creator.get("userId")

    @property
    def creator_name(self) -> str:
        c = self.creator
        return f"{c.get('firstName') or ''} {c.get('lastName') or ''}".strip()

    @property
    def creator_email(self) -> str:
        return self.creator.get("email") or ""

    @property
    def modifier(self) -> Dict[str, Any]:
        return self.raw.get("modifiedBy") or {}

    @property
    def modifier_id(self) -> Optional[int]:
        return self.modifier.get("userId")

    @property
    def modifier_name(self) -> str:
        m = self.modifier
        return f"{m.get('firstName') or ''} {m.get('lastName') or ''}".strip()

    # -- review state --------------------------------------------------------

    @property
    def total_reviews(self) -> Optional[int]:
        return _optional_int(self.raw.get("numTotalReviews"))

    @property
    def positive_reviews(self) -> Optional[int]:
        return _optional_int(self.raw.get("numPositiveReviews"))

    @property
    def is_reviewed(self) -> Optional[bool]:
        to_be_reviewed = _optional_bool(self.raw.get("toBeReviewed"))
        if to_be_reviewed is not None:
            return not to_be_reviewed
        total = self.total_reviews
        return total > 0 if total is not None else None

    # -- what --------------------------------------------------------------

    @property
    def taxa(self) -> List[Taxon]:
        return [Taxon(t) for t in self.raw.get("taxonomy") or []]

    @property
    def taxon_labels(self) -> List[str]:
        return [t.primary_name for t in self.taxa]

    # -- video linkage -------------------------------------------------------

    @property
    def archive_filename(self) -> Optional[str]:
        return self.raw.get("archiveFilename")

    @property
    def clip_duration_seconds(self) -> Optional[float]:
        try:
            return float(self.raw["clipDurationSeconds"])
        except (KeyError, TypeError, ValueError):
            return None

    def offset_in_file_seconds(self) -> Optional[float]:
        """Seconds from the start of the archive file to this annotation.

        ``clipOffsetSeconds`` on the record is the file's offset within the
        device's media series, not a position inside the file, so the offset
        must come from the timestamps.  Annotations that fall outside the
        file are dropped rather than clamped -- clamping silently produces
        confident, wrong frames.
        """
        start = self.raw.get("startDate")
        clip_start = self.raw.get("archiveClipStartDate")
        if not start or not clip_start or not self.raw.get("archiveFilename"):
            return None
        try:
            rel = (parse_iso_utc(start) - parse_iso_utc(clip_start)).total_seconds()
        except (ValueError, TypeError):
            return None
        if rel < 0:
            return None
        duration = self.clip_duration_seconds
        if duration is not None and rel >= duration:
            return None
        return rel

    def __repr__(self) -> str:
        return f"Annotation(id={self.id}, taxa={self.taxon_labels}, start={self.start_utc})"


# ---------------------------------------------------------------------------
# filter predicates (shared by AnnotationSet.filter and the live fetcher)
# ---------------------------------------------------------------------------

def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def _optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return None


def person_matches(
    person: Dict[str, Any],
    user_id: Optional[int],
    name_contains: Optional[str],
    email_contains: Optional[str],
) -> bool:
    if user_id is not None and person.get("userId") != user_id:
        return False
    if name_contains:
        name = f"{person.get('firstName') or ''} {person.get('lastName') or ''}".strip()
        if name_contains.lower() not in name.lower():
            return False
    if email_contains:
        if email_contains.lower() not in (person.get("email") or "").lower():
            return False
    return True


@dataclass
class ReviewFilters:
    """Review-quality gates, applied identically online and offline."""

    reviewed_only: bool = False
    min_total_reviews: Optional[int] = None
    min_positive_reviews: Optional[int] = None
    min_positive_review_rate: Optional[float] = None
    require_cross_review: bool = False

    def matches(self, ann: Annotation) -> bool:
        if self.reviewed_only and ann.is_reviewed is not True:
            return False
        total = ann.total_reviews
        positive = ann.positive_reviews
        if self.min_total_reviews is not None:
            if total is None or total < self.min_total_reviews:
                return False
        if self.min_positive_reviews is not None:
            if positive is None or positive < self.min_positive_reviews:
                return False
        if self.min_positive_review_rate is not None:
            if total is None or total <= 0 or positive is None:
                return False
            if (positive / total) < self.min_positive_review_rate:
                return False
        if self.require_cross_review:
            if ann.creator_id is None or ann.modifier_id is None:
                return False
            if ann.creator_id == ann.modifier_id:
                return False
        return True


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------

@dataclass
class AnnotatorStats:
    name: str
    user_id: Optional[int]
    email: str
    annotations: int = 0
    reviewed: int = 0
    distinct_taxa: Set[str] = field(default_factory=set)
    places: Set[str] = field(default_factory=set)
    first_utc: Optional[str] = None
    last_utc: Optional[str] = None
    taxon_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def top_taxa(self) -> List[str]:
        ranked = sorted(self.taxon_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [name for name, _ in ranked[:3]]


@dataclass
class TaxonStats:
    name: str
    aphia_id: Optional[int]
    annotations: int = 0
    annotators: Set[str] = field(default_factory=set)
    places: Set[str] = field(default_factory=set)
    groups: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# the set
# ---------------------------------------------------------------------------

class AnnotationSet:
    """A queryable collection of SeaTube annotations."""

    def __init__(self, records: Iterable[Dict[str, Any]]) -> None:
        self.annotations: List[Annotation] = [
            a if isinstance(a, Annotation) else Annotation(a) for a in records
        ]

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "AnnotationSet":
        return cls(json.loads(Path(path).read_text()))

    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([a.raw for a in self.annotations], indent=2, ensure_ascii=False))

    # -- container protocol ----------------------------------------------------

    def __len__(self) -> int:
        return len(self.annotations)

    def __iter__(self) -> Iterator[Annotation]:
        return iter(self.annotations)

    def __getitem__(self, index: int) -> Annotation:
        return self.annotations[index]

    @property
    def records(self) -> List[Dict[str, Any]]:
        return [a.raw for a in self.annotations]

    # -- filtering -------------------------------------------------------------

    def filter(
        self,
        *,
        groups: Sequence[str] = (),
        taxa: Sequence[str] = (),
        taxon_contains: Optional[str] = None,
        creator: Optional[str] = None,
        creator_id: Optional[int] = None,
        creator_email: Optional[str] = None,
        modifier: Optional[str] = None,
        modifier_id: Optional[int] = None,
        modifier_email: Optional[str] = None,
        review: Optional[ReviewFilters] = None,
        require_comment: bool = False,
        dive_contains: Optional[str] = None,
        location_contains: Optional[str] = None,
        camera_mode: Optional[str] = None,
        resolver: Optional[WormsResolver] = None,
    ) -> "AnnotationSet":
        """Return the annotations passing every given condition.

        ``groups``/``taxa`` need a ``WormsResolver`` for lineage lookups; one
        without a cache is created if none is passed.
        """
        from .taxonomy import wanted_ancestor_names

        wanted = wanted_ancestor_names(groups, taxa)
        if wanted and resolver is None:
            resolver = WormsResolver(None)

        kept: List[Annotation] = []
        for ann in self.annotations:
            if camera_mode and ann.camera_mode != camera_mode:
                continue
            if not person_matches(ann.creator, creator_id, creator, creator_email):
                continue
            if not person_matches(ann.modifier, modifier_id, modifier, modifier_email):
                continue
            if review is not None and not review.matches(ann):
                continue
            if require_comment and not ann.comment:
                continue
            if dive_contains:
                if dive_contains.lower() not in str(ann.dive_name or "").lower():
                    continue
            if location_contains:
                blob = " ".join(
                    str(ann.raw.get(k) or "")
                    for k in ("stationaryLocationName", "stationaryLocationPath")
                ).lower()
                if location_contains.lower() not in blob:
                    continue
            if taxon_contains:
                haystack = " ".join(
                    " ".join(t.names) + " " + str(t.display_text or "") for t in ann.taxa
                ).lower()
                if taxon_contains.lower() not in haystack:
                    continue
            if wanted and not resolver.annotation_matches(ann.raw, wanted):
                continue
            kept.append(ann)

        result = AnnotationSet([])
        result.annotations = kept
        return result

    # -- summaries ----------------------------------------------------------------

    def annotator_summary(self) -> List[AnnotatorStats]:
        """Who annotated, how much, when, and what -- sorted by volume."""
        stats: Dict[Tuple[Optional[int], str], AnnotatorStats] = {}
        for ann in self.annotations:
            key = (ann.creator_id, ann.creator_name or "unknown")
            entry = stats.get(key)
            if entry is None:
                entry = AnnotatorStats(
                    name=ann.creator_name or "unknown",
                    user_id=ann.creator_id,
                    email=ann.creator_email,
                )
                stats[key] = entry
            entry.annotations += 1
            if ann.is_reviewed:
                entry.reviewed += 1
            for label in ann.taxon_labels:
                entry.distinct_taxa.add(label)
                entry.taxon_counts[label] = entry.taxon_counts.get(label, 0) + 1
            entry.places.add(ann.place)
            ts = ann.start_utc
            if ts:
                if entry.first_utc is None or ts < entry.first_utc:
                    entry.first_utc = ts
                if entry.last_utc is None or ts > entry.last_utc:
                    entry.last_utc = ts
        return sorted(stats.values(), key=lambda s: (-s.annotations, s.name))

    def taxon_summary(self, resolver: Optional[WormsResolver] = None) -> List[TaxonStats]:
        """What was annotated and how often -- sorted by count."""
        stats: Dict[str, TaxonStats] = {}
        for ann in self.annotations:
            for taxon in ann.taxa:
                name = taxon.primary_name
                entry = stats.get(name)
                if entry is None:
                    entry = TaxonStats(name=name, aphia_id=taxon.aphia_id)
                    if resolver is not None:
                        entry.groups = resolver.groups_for(taxon.raw)
                    stats[name] = entry
                entry.annotations += 1
                if ann.creator_name:
                    entry.annotators.add(ann.creator_name)
                entry.places.add(ann.place)
        return sorted(stats.values(), key=lambda s: (-s.annotations, s.name))

    def clip_index(self, window_seconds: float = 0.0) -> List[Dict[str, Any]]:
        """Videos and timestamps for these annotations, without downloading.

        With ``window_seconds`` > 0, annotations landing in the same window of
        the same archive file are merged into one row -- useful for finding
        dense stretches worth watching.
        """
        rows: Dict[Tuple[str, float], Dict[str, Any]] = {}
        for ann in self.annotations:
            offset = ann.offset_in_file_seconds()
            if offset is None:
                continue
            if window_seconds > 0:
                bucket = float(int(offset // window_seconds) * window_seconds)
            else:
                bucket = round(offset, 3)
            key = (ann.archive_filename, bucket)
            row = rows.get(key)
            if row is None:
                row = {
                    "archive_filename": ann.archive_filename,
                    "offset_seconds": bucket,
                    "window_seconds": window_seconds or None,
                    "first_utc": ann.start_utc,
                    "annotation_count": 0,
                    "taxa": set(),
                    "annotation_ids": [],
                    "place": ann.place,
                    "seatube_link": ann.seatube_link,
                }
                rows[key] = row
            row["annotation_count"] += 1
            row["taxa"].update(ann.taxon_labels)
            row["annotation_ids"].append(ann.id)
            if ann.start_utc and ann.start_utc < row["first_utc"]:
                row["first_utc"] = ann.start_utc
        out = []
        for key in sorted(rows):
            row = rows[key]
            row["taxa"] = sorted(row["taxa"])
            out.append(row)
        return out

    # -- ML-ready exports -----------------------------------------------------------

    def flatten(self) -> List[Dict[str, Any]]:
        """One flat row per (annotation, taxon) pair, ready for CSV/JSONL."""
        from .archive import parse_iso_utc as _parse

        def duration(start: Optional[str], end: Optional[str]) -> Optional[float]:
            if not start or not end:
                return None
            try:
                return (_parse(end) - _parse(start)).total_seconds()
            except Exception:
                return None

        rows: List[Dict[str, Any]] = []
        for wrapped in self.annotations:
            ann = wrapped.raw
            created_by = ann.get("createdBy") or {}
            modified_by = ann.get("modifiedBy") or {}
            taxons = ann.get("taxonomy") or [None]

            for taxon_index, taxon in enumerate(taxons):
                attributes: List[Any] = []
                taxonomy_code = taxonomy_id = taxon_id = taxon_display = taxon_url = None
                if isinstance(taxon, dict):
                    attributes = taxon.get("attributes") or []
                    taxonomy_code = taxon.get("taxonomyCode")
                    taxonomy_id = taxon.get("taxonomyId")
                    taxon_id = taxon.get("taxonId")
                    taxon_display = taxon.get("displayText")
                    taxon_url = taxon.get("taxonUrl")

                rows.append({
                    "annotation_id": ann.get("annotationId"),
                    "annotation_source": ann.get("annotationSource"),
                    "annotation_comment": ann.get("comment"),
                    "annotation_start_utc": ann.get("startDate"),
                    "annotation_end_utc": ann.get("endDate"),
                    "annotation_duration_seconds": duration(ann.get("startDate"), ann.get("endDate")),
                    "camera_mode": ann.get("cameraMode"),
                    "dive_id": ann.get("diveId"),
                    "dive_name": ann.get("diveName"),
                    "cruise_name": ann.get("cruiseName"),
                    "stationary_search_tree_node_id": ann.get("stationarySearchTreeNodeId"),
                    "stationary_location_name": ann.get("stationaryLocationName"),
                    "stationary_location_path": ann.get("stationaryLocationPath"),
                    "resource_type_id": ann.get("resourceTypeId"),
                    "resource_type_name": ann.get("resourceTypeName"),
                    "device_id": ann.get("deviceId"),
                    "video_resource_id": ann.get("videoResourceId"),
                    "video_resource_type_id": ann.get("videoResourceTypeId"),
                    "creator_user_id": created_by.get("userId"),
                    "creator_first_name": created_by.get("firstName"),
                    "creator_last_name": created_by.get("lastName"),
                    "creator_email": created_by.get("email"),
                    "modifier_user_id": modified_by.get("userId"),
                    "modifier_first_name": modified_by.get("firstName"),
                    "modifier_last_name": modified_by.get("lastName"),
                    "modifier_email": modified_by.get("email"),
                    "created_date_utc": ann.get("createdDate"),
                    "modified_date_utc": ann.get("modifiedDate"),
                    "lat": ann.get("lat"),
                    "lon": ann.get("lon"),
                    "depth_m": ann.get("depth"),
                    "heading_deg": ann.get("heading"),
                    "to_be_reviewed": ann.get("toBeReviewed"),
                    "num_positive_reviews": ann.get("numPositiveReviews"),
                    "num_total_reviews": ann.get("numTotalReviews"),
                    "taxonomy_index": taxon_index,
                    "taxonomy_code": taxonomy_code,
                    "taxonomy_id": taxonomy_id,
                    "taxon_id": taxon_id,
                    "taxon_display_text": taxon_display,
                    "taxon_url": taxon_url,
                    "taxon_attributes_json": json.dumps(attributes, ensure_ascii=False),
                    "archive_filename": ann.get("archiveFilename"),
                    "video_resolution_code": ann.get("resolution"),
                    "video_device_code": ann.get("videoDeviceCode"),
                    "clip_offset_seconds": ann.get("clipOffsetSeconds"),
                    "clip_duration_seconds": ann.get("clipDurationSeconds"),
                    "clip_relative_path": ann.get("clipRelativePath"),
                    "clip_row_start_offset_seconds": ann.get("clipRowStartOffsetSeconds"),
                    "archive_clip_start_utc": ann.get("archiveClipStartDate"),
                    "video_local_path": ann.get("videoLocalPath"),
                    "video_downloaded": ann.get("videoDownloaded"),
                    "contextual_link": ann.get("contextualLink"),
                })
        return rows

    def write_flat_csv(self, path: str) -> None:
        rows = self.flatten()
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FLAT_EXPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def write_flat_jsonl(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for row in self.flatten():
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


FLAT_EXPORT_COLUMNS = [
    "annotation_id", "annotation_source", "annotation_comment",
    "annotation_start_utc", "annotation_end_utc", "annotation_duration_seconds",
    "camera_mode", "dive_id", "dive_name", "cruise_name",
    "stationary_search_tree_node_id", "stationary_location_name", "stationary_location_path",
    "resource_type_id", "resource_type_name", "device_id",
    "video_resource_id", "video_resource_type_id",
    "creator_user_id", "creator_first_name", "creator_last_name", "creator_email",
    "modifier_user_id", "modifier_first_name", "modifier_last_name", "modifier_email",
    "created_date_utc", "modified_date_utc",
    "lat", "lon", "depth_m", "heading_deg",
    "to_be_reviewed", "num_positive_reviews", "num_total_reviews",
    "taxonomy_index", "taxonomy_code", "taxonomy_id", "taxon_id",
    "taxon_display_text", "taxon_url", "taxon_attributes_json",
    "archive_filename", "video_resolution_code", "video_device_code",
    "clip_offset_seconds", "clip_duration_seconds", "clip_relative_path",
    "clip_row_start_offset_seconds", "archive_clip_start_utc",
    "video_local_path", "video_downloaded", "contextual_link",
]
