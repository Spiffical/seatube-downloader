"""Query ONC for annotations and link each one to its archive video file.

This is the expensive, online half of the package: it walks dives or fixed
cameras, applies the filters, and stamps every kept annotation with the
archive file that truly contains its timestamp.  Everything after it --
summaries and clip listings -- can use the saved result. Lineage matching
may call WoRMS; media extraction downloads the required archive files.
"""

from __future__ import annotations

from collections import defaultdict
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Collection, Dict, List, Optional, Sequence, Set, Tuple

from .annotations import Annotation, AnnotationSet, ReviewFilters, person_matches
from .archive import (
    archive_info_from_row,
    media_row_containing,
    parse_iso_utc,
    select_media_file,
    to_iso_utc,
)
from .client import BASE_URL, OncClient, OncError
from .taxonomy import WormsResolver, wanted_ancestor_names

NO_SEATUBE_VIDEO = 9001  # ONC's statusCode for a fixed-camera site without SeaTube video
logger = logging.getLogger(__name__)


@dataclass
class FetchFilters:
    """Everything that narrows a fetch, in one place.

    ``camera_mode`` defaults to what the IDs ask for: ``node_ids`` or
    ``location_contains`` alone mean fixed cameras, ``dive_ids`` alone (or
    nothing) means ROV dives, and both together mean both.
    """

    start_date: str
    end_date: str
    camera_mode: Optional[str] = None              # dive | stationary | both

    # what
    taxonomy_code: Optional[str] = "WoRMS"
    taxonomy_id: Optional[int] = None
    taxon_ids: Set[int] = field(default_factory=set)
    groups: Tuple[str, ...] = ()                   # broad groups, e.g. ("crabs",)
    taxon_names: Tuple[str, ...] = ()              # WoRMS ancestors, e.g. ("Brachyura",)

    # who
    creator_id: Optional[int] = None
    creator_name: Optional[str] = None
    creator_email: Optional[str] = None
    modifier_id: Optional[int] = None
    modifier_name: Optional[str] = None
    modifier_email: Optional[str] = None
    review: ReviewFilters = field(default_factory=ReviewFilters)

    # where
    dive_ids: Set[int] = field(default_factory=set)
    node_ids: Set[int] = field(default_factory=set)     # fixed-camera searchTreeNodeIds
    location_contains: Optional[str] = None             # fixed-camera label/path/location code
    max_stationary_locations: Optional[int] = None

    # how
    resolution: str = "L"                          # H | L | S
    max_dives: Optional[int] = None
    # No longer used: fixed-camera annotations now arrive whole from ONC's
    # location service. Kept so existing calls and CLI flags still work.
    page_size: int = 250
    resolve_taxon_names: bool = True

    def __post_init__(self) -> None:
        from .images import validate_limit

        if parse_iso_utc(self.end_date) < parse_iso_utc(self.start_date):
            raise ValueError("end_date must be on or after start_date")
        wants_fixed = bool(self.node_ids or self.location_contains
                           or self.max_stationary_locations is not None)
        wants_dives = bool(self.dive_ids or self.max_dives is not None)
        if self.camera_mode is None:
            self.camera_mode = ("both" if wants_fixed and wants_dives
                                else "stationary" if wants_fixed else "dive")
        if self.camera_mode not in {"dive", "stationary", "both"}:
            raise ValueError("camera_mode must be 'dive', 'stationary', or 'both'")
        # Silently ignoring a selection would scan the wrong sources.
        if self.camera_mode == "dive" and wants_fixed:
            raise ValueError("node_ids, location_contains and max_stationary_locations select fixed "
                             "cameras; use camera_mode='stationary' or 'both', or omit camera_mode")
        if self.camera_mode == "stationary" and wants_dives:
            raise ValueError("dive_ids and max_dives select ROV dives; use camera_mode='dive' "
                             "or 'both', or omit camera_mode")
        if self.resolution not in {"H", "L", "S"}:
            raise ValueError("resolution must be 'H', 'L', or 'S'")
        for name in ("max_dives", "max_stationary_locations"):
            validate_limit(name, getattr(self, name))
        if not isinstance(self.page_size, int) or self.page_size <= 0:
            raise ValueError("page_size must be a positive integer")
        wanted_ancestor_names(self.groups, self.taxon_names)

    def effective_taxonomy_code(self) -> Optional[str]:
        code = (self.taxonomy_code or "").strip()
        return code or None

    def matches_people_and_reviews(self, ann: Annotation) -> bool:
        if not person_matches(ann.creator, self.creator_id, self.creator_name, self.creator_email):
            return False
        if not person_matches(ann.modifier, self.modifier_id, self.modifier_name, self.modifier_email):
            return False
        return self.review.matches(ann)

    def matches_taxonomy_ids(self, record: Dict[str, Any]) -> bool:
        """The cheap taxonomy filter: code / taxonomy id / ONC taxon ids."""
        code = self.effective_taxonomy_code()
        if not code and self.taxonomy_id is None and not self.taxon_ids:
            return True
        taxons = record.get("taxons") or record.get("taxonomy") or []
        if not taxons:
            return False
        for taxon in taxons:
            if code and str(taxon.get("taxonomyCode", "")).lower() != code.lower():
                continue
            if self.taxonomy_id is not None and int(taxon.get("taxonomyId", -1)) != self.taxonomy_id:
                continue
            if self.taxon_ids and int(taxon.get("taxonId", -1)) not in self.taxon_ids:
                continue
            return True
        return False


def dives_in_range(
    client: OncClient,
    start_dt: datetime,
    end_dt: datetime,
    dive_ids: Set[int] = frozenset(),
    max_dives: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Dives whose [dateFrom, dateTo] overlaps the range, sorted by start.

    This is also how you discover dive ids for ``--dive-id``: each row
    carries ``diveId`` and the human name (``referenceDiveId``).
    """
    selected = []
    for dive in client.list_dives():
        dive_id = int(dive.get("diveId", 0))
        if dive_id <= 0:
            continue
        if dive_ids and dive_id not in dive_ids:
            continue
        date_from, date_to = dive.get("dateFrom"), dive.get("dateTo")
        if not date_from or not date_to:
            continue
        try:
            dive_start, dive_end = parse_iso_utc(date_from), parse_iso_utc(date_to)
        except Exception:
            continue
        if dive_end < start_dt or dive_start > end_dt:
            continue
        selected.append(dive)
    selected.sort(key=lambda d: d.get("dateFrom", ""))
    if max_dives is not None:
        selected = selected[:max_dives]
    return selected


# ---------------------------------------------------------------------------
# fixed-camera sites
# ---------------------------------------------------------------------------

def fixed_camera_locations(client: OncClient) -> List[Dict[str, Any]]:
    """Every camera site in ONC's fixed-camera tree, with a short unique ``label``.

    Sites can contain sub-sites (Barkley Canyon Mid-East contains Wally
    Land), so a node counts when ONC flags a device there, not only when it
    is a leaf. Several sites share a name such as "Front Camera"; ``label``
    is the shortest path suffix naming exactly one of them.
    """
    sites: List[Dict[str, Any]] = []
    parts: List[List[str]] = []

    def walk(node: Dict[str, Any], path: List[str]) -> None:
        current_name = node.get("html")
        current_path = path + ([current_name] if current_name else [])
        children = node.get("children") or []
        if node.get("searchTreeNodeId") is not None and (node.get("wasDeviceFound") or not children):
            sites.append({
                "searchTreeNodeId": int(node.get("searchTreeNodeId")),
                "name": current_name,
                "path": " > ".join(current_path),
                "latitude": node.get("latitude"),
                "longitude": node.get("longitude"),
                "siteDepth": node.get("siteDepth"),
            })
            # The root ("Fixed Cameras") is common to every path.
            parts.append(current_path[1:] or current_path or [str(node.get("searchTreeNodeId"))])
        for child in children:
            walk(child, current_path)

    walk(client.fixed_camera_tree(), [])
    for site, label in zip(sites, _unique_suffixes(parts)):
        site["label"] = label
    sites.sort(key=lambda x: (x["path"], x["searchTreeNodeId"]))
    return sites


def _unique_suffixes(paths: List[List[str]]) -> List[str]:
    labels = []
    for parts in paths:
        for k in range(1, len(parts) + 1):
            if sum(other[-k:] == parts[-k:] for other in paths) == 1:
                break
        labels.append(" > ".join(parts[-k:]))
    return labels


def select_fixed_cameras(
    client: OncClient,
    node_ids: Collection[int] = (),
    location_contains: Optional[str] = None,
    max_locations: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fixed-camera sites matching the selection, each with its ONC ``locationCode``.

    ``location_contains`` matches the label, tree path, or location code.
    Unknown node ids raise instead of quietly selecting nothing.
    """
    sites = fixed_camera_locations(client)
    if node_ids:
        wanted = {int(n) for n in node_ids}
        unknown = sorted(wanted - {s["searchTreeNodeId"] for s in sites})
        if unknown:
            raise ValueError(f"Unknown fixed-camera node id(s) {unknown}. "
                             "List valid ids with sea.cameras(...) or `seatube cameras`.")
        sites = [s for s in sites if s["searchTreeNodeId"] in wanted]

    def lookup_codes(rows: List[Dict[str, Any]]) -> None:
        for site in rows:
            if "locationCode" in site:
                continue
            try:
                details = client.fixed_camera_details(site["searchTreeNodeId"])
            except Exception as exc:
                raise RuntimeError(f"Could not look up fixed camera {site['label']}: {exc}") from exc
            site["locationCode"] = details.get("stationCode")

    if location_contains:
        lookup_codes(sites)  # codes are matchable too, e.g. "CCIP"
        needle = location_contains.lower()
        sites = [s for s in sites
                 if needle in " ".join(str(s.get(k) or "") for k in ("label", "path", "locationCode")).lower()]
    if max_locations is not None:
        sites = sites[:max_locations]
    lookup_codes(sites)
    return sites


def site_annotations(
    client: OncClient,
    site: Dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
) -> List[Dict[str, Any]]:
    """Raw annotations for one fixed-camera site whose start lies in the range."""
    code = site.get("locationCode")
    if not code:
        logger.warning(f"ONC gives no location code for fixed camera {site['label']}; skipped")
        return []
    try:
        raw = client.location_annotations(code, to_iso_utc(start_dt), to_iso_utc(end_dt))
    except Exception as exc:
        raise RuntimeError(f"Could not fetch annotations for fixed camera {site['label']} ({code}): {exc}") from exc
    kept = []
    for ann in raw:
        try:
            ts = parse_iso_utc(ann.get("dateFrom") or "")
        except (TypeError, ValueError):
            continue
        if start_dt <= ts <= end_dt:
            kept.append(ann)
    return kept


def _has_worms_taxon(ann: Dict[str, Any]) -> bool:
    return any(str(t.get("taxonomyCode") or "").lower() == "worms" for t in ann.get("taxons") or [])


def cameras_in_range(
    client: OncClient,
    start_dt: datetime,
    end_dt: datetime,
    *,
    node_ids: Collection[int] = (),
    location_contains: Optional[str] = None,
    include_empty: bool = False,
) -> List[Dict[str, Any]]:
    """Fixed-camera sites annotated in the range -- the counterpart of ``dives_in_range``.

    Each row is a site from ``fixed_camera_locations`` plus ``locationCode``,
    ``annotations`` (all of them), ``worms_annotations`` (those with a WoRMS
    taxon, which a default fetch keeps) and the first/last annotation times.
    Sorted by WoRMS-labelled count. Whether video exists is checked by the
    fetch, which reports mapped versus unmapped annotations.
    """
    if end_dt < start_dt:
        raise ValueError("end_date must be on or after start_date")
    rows = []
    for site in select_fixed_cameras(client, node_ids, location_contains):
        anns = site_annotations(client, site, start_dt, end_dt)
        if not anns and not include_empty:
            continue
        times = sorted(a["dateFrom"] for a in anns)
        rows.append({
            **site,
            "annotations": len(anns),
            "worms_annotations": sum(_has_worms_taxon(a) for a in anns),
            "first_utc": times[0] if times else None,
            "last_utc": times[-1] if times else None,
        })
    rows.sort(key=lambda r: (-r["worms_annotations"], -r["annotations"], r["label"]))
    return rows


def seatube_link(resource_type_id: int, resource_id: int, annotation_id: int, ann_start: str) -> str:
    return (
        f"{BASE_URL}/SeaTube?resourceTypeId={resource_type_id}&resourceId={resource_id}"
        f"&time={ann_start}&annotationId={annotation_id}"
    )


def fixed_camera_link(search_tree_node_id: int, ann_start: str) -> str:
    """The SeaTube fixed-camera player, cued to the annotation instant."""
    return f"{BASE_URL}/app/fixed-camera-locations/{search_tree_node_id}?time={ann_start}"


def _normalized_user(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = raw or {}
    user_id = raw.get("userId") if raw.get("userId") is not None else raw.get("dmasUserId")
    if user_id is None and raw.get("userCode") not in (None, ""):
        try:
            user_id = int(raw["userCode"])  # the location service sends IDs as strings
        except (TypeError, ValueError):
            user_id = None
    return {
        "userId": user_id,
        "firstName": raw.get("firstName") if raw.get("firstName") is not None else raw.get("firstname"),
        "lastName": raw.get("lastName") if raw.get("lastName") is not None else raw.get("lastname"),
        "email": raw.get("email"),
    }


def stationary_record(ann: Dict[str, Any], site: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a location-service annotation to the package's record shape."""
    ann_start = ann.get("dateFrom")
    return {
        "cameraMode": "stationary",
        "annotationId": int(ann.get("annotationId") or 0),
        "diveId": None,
        "diveName": None,
        "cruiseName": None,
        "stationarySearchTreeNodeId": site["searchTreeNodeId"],
        "stationaryLocationName": site["label"],
        "stationaryLocationPath": site["path"],
        "stationaryLocationCode": site.get("locationCode"),
        "startDate": ann_start,
        "endDate": ann.get("dateTo"),
        "comment": str(ann.get("comment") or "").strip(),
        "annotationSource": ann.get("annotationSource"),
        "createdBy": _normalized_user(ann.get("createdBy")),
        "modifiedBy": _normalized_user(ann.get("modifiedBy")),
        "createdDate": ann.get("createdDate"),
        "modifiedDate": ann.get("modifiedDate"),
        "taxonomy": ann.get("taxons") or [],
        "videoResourceId": None,                    # the device id, once mapped
        "videoResourceTypeId": 1000,
        "resourceTypeId": ann.get("resourceTypeId"),
        "resourceTypeName": None,
        "deviceId": None,
        "deviceCode": ann.get("deviceCode"),
        # A fixed camera sits at its site; ONC's site depth is negative above sea level.
        "lat": site.get("latitude"),
        "lon": site.get("longitude"),
        "depth": site.get("siteDepth"),
        "heading": None,
        "toBeReviewed": ann.get("toBeReviewed"),
        "numPositiveReviews": ann.get("numPositiveReviews"),
        "numTotalReviews": ann.get("numTotalReviews"),
        "contextualLink": fixed_camera_link(site["searchTreeNodeId"], ann_start),
    }


class AnnotationFetcher:
    """Runs a filtered annotation query against ONC.

    >>> fetcher = AnnotationFetcher(OncClient.from_env())
    >>> result = fetcher.fetch(FetchFilters(start_date=..., end_date=..., groups=("crabs",)))
    >>> result.save("downloads/annotations.json")
    """

    def __init__(self, client: OncClient, resolver: Optional[WormsResolver] = None) -> None:
        self.client = client
        self.resolver = resolver

    # ------------------------------------------------------------------

    def fetch(self, filters: FetchFilters) -> AnnotationSet:
        start_dt = parse_iso_utc(filters.start_date)
        end_dt = parse_iso_utc(filters.end_date)
        if end_dt < start_dt:
            raise ValueError("end_date must be after start_date")

        records: List[Dict[str, Any]] = []

        if filters.camera_mode in ("dive", "both"):
            records.extend(self._dive_records(filters, start_dt, end_dt))
        if filters.camera_mode in ("stationary", "both"):
            records.extend(self._stationary_records(filters, start_dt, end_dt))

        logger.info(f"\nMatched annotations across selected modes: {len(records)}")

        # Broad-group / lineage filtering happens before archive mapping so
        # discarded annotations never cost a video-metadata request.
        wanted = wanted_ancestor_names(filters.groups, filters.taxon_names)
        if wanted:
            resolver = self.resolver or WormsResolver(None)
            logger.info(f"Filtering to {len(wanted)} ancestor taxa ({', '.join(sorted(wanted))})")
            resolver.unresolved.clear()
            before = len(records)
            records = [r for r in records if resolver.annotation_matches(r, wanted)]
            resolver.save()
            resolver.warn_unresolved()
            logger.info(f"Kept {len(records)} of {before} annotations")

        logger.info("Mapping annotations to archive video files (strict containment)...")
        self._attach_dive_archive_info(records, filters.resolution)
        self._attach_stationary_archive_info(records, filters.resolution)
        mapped = sum(1 for r in records if r.get("archiveFilename"))
        logger.info(f"{mapped}/{len(records)} annotations mapped to an archive file")

        return AnnotationSet(records)

    # ------------------------------------------------------------------
    # dives
    # ------------------------------------------------------------------

    def _dive_records(
        self,
        filters: FetchFilters,
        start_dt: datetime,
        end_dt: datetime,
    ) -> List[Dict[str, Any]]:
        selected = dives_in_range(
            self.client, start_dt, end_dt,
            dive_ids=filters.dive_ids, max_dives=filters.max_dives,
        )
        logger.info(f"Dives overlapping the date range: {len(selected)}")

        matched: List[Dict[str, Any]] = []
        for index, dive in enumerate(selected, start=1):
            dive_id = int(dive["diveId"])
            try:
                dive_annotations = self.client.dive_annotations(dive_id)
            except Exception as exc:
                raise RuntimeError(f"Could not fetch dive {dive_id}: {exc}") from exc

            for ann in dive_annotations:
                ts_raw = ann.get("startDate") or ann.get("dateFrom")
                if not ts_raw:
                    continue
                try:
                    ts = parse_iso_utc(ts_raw)
                except Exception:
                    continue
                if ts < start_dt or ts > end_dt:
                    continue

                ann_id = int(ann.get("annotationId", 0))
                record = {
                    "cameraMode": "dive",
                    "annotationId": ann_id,
                    "diveId": dive_id,
                    "diveName": ann.get("diveName") or dive.get("referenceDiveId"),
                    "cruiseName": ann.get("cruiseName"),
                    "stationarySearchTreeNodeId": None,
                    "stationaryLocationName": None,
                    "stationaryLocationPath": None,
                    "startDate": ts_raw,
                    "endDate": ann.get("endDate"),
                    "comment": ann.get("comment"),
                    "annotationSource": ann.get("annotationSource"),
                    "createdBy": ann.get("createdBy"),
                    "modifiedBy": ann.get("modifiedBy"),
                    "createdDate": ann.get("createdDate"),
                    "modifiedDate": ann.get("modifiedDate"),
                    "taxonomy": ann.get("taxons", []),
                    "videoResourceId": ann.get("videoResourceId"),
                    "videoResourceTypeId": ann.get("videoResourceTypeId"),
                    "resourceTypeId": ann.get("resourceTypeId"),
                    "resourceTypeName": ann.get("resourceTypeName"),
                    "deviceId": ann.get("deviceId"),
                    "lat": ann.get("lat"),
                    "lon": ann.get("lon"),
                    "depth": ann.get("depth"),
                    "heading": ann.get("heading"),
                    "toBeReviewed": ann.get("toBeReviewed"),
                    "numPositiveReviews": ann.get("numPositiveReviews"),
                    "numTotalReviews": ann.get("numTotalReviews"),
                    "contextualLink": seatube_link(600, dive_id, ann_id, ts_raw),
                }

                wrapped = Annotation(record)
                if not filters.matches_people_and_reviews(wrapped):
                    continue
                if not filters.matches_taxonomy_ids(record):
                    continue
                matched.append(record)

            if index % 10 == 0 or index == len(selected):
                logger.info(f"  {index}/{len(selected)} dives scanned; matched so far: {len(matched)}")

        logger.info(f"Matched dive annotations after filters: {len(matched)}")
        return matched

    # ------------------------------------------------------------------
    # fixed cameras
    # ------------------------------------------------------------------

    def _stationary_records(
        self,
        filters: FetchFilters,
        start_dt: datetime,
        end_dt: datetime,
    ) -> List[Dict[str, Any]]:
        sites = select_fixed_cameras(
            self.client, filters.node_ids, filters.location_contains, filters.max_stationary_locations,
        )
        logger.info(f"Fixed-camera sites selected: {len(sites)}")

        matched: List[Dict[str, Any]] = []
        for index, site in enumerate(sites, start=1):
            for ann in site_annotations(self.client, site, start_dt, end_dt):
                record = stationary_record(ann, site)
                if not filters.matches_people_and_reviews(Annotation(record)):
                    continue
                if not filters.matches_taxonomy_ids(record):
                    continue
                matched.append(record)
            if index % 10 == 0 or index == len(sites):
                logger.info(f"  {index}/{len(sites)} sites scanned; matched so far: {len(matched)}")

        logger.info(f"Matched fixed-camera annotations after filters: {len(matched)}")
        return matched

    # ------------------------------------------------------------------
    # archive-file mapping
    # ------------------------------------------------------------------

    def _attach_dive_archive_info(self, records: List[Dict[str, Any]], resolution: str) -> None:
        by_dive: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for record in records:
            if record.get("cameraMode") == "dive" and record.get("diveId") is not None:
                by_dive[int(record["diveId"])].append(record)

        for dive_id, anns in by_dive.items():
            try:
                video_meta = self.client.dive_video_metadata(dive_id, resolution)
            except Exception as exc:
                raise RuntimeError(f"Could not fetch video metadata for dive {dive_id}: {exc}") from exc
            media_files = video_meta.get("mediaFiles", []) or []
            for record in anns:
                media_file = select_media_file(media_files, record.get("deviceId"))
                self._attach_archive_info(record, [media_file] if media_file else [], resolution)

    def _attach_stationary_archive_info(self, records: List[Dict[str, Any]], resolution: str) -> None:
        """Map each annotation within its own site's recordings.

        Cameras move between sites, so only the site that reported the
        annotation is searched, across every deployment of that device.
        """
        by_site: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for record in records:
            if record.get("cameraMode") == "stationary" and record.get("stationarySearchTreeNodeId") is not None:
                by_site[int(record["stationarySearchTreeNodeId"])].append(record)

        for node_id, anns in by_site.items():
            label = anns[0].get("stationaryLocationName") or node_id
            try:
                media_files = self.client.stationary_video_metadata(node_id, resolution).get("mediaFiles", []) or []
            except OncError as exc:
                if exc.status_code != NO_SEATUBE_VIDEO:
                    raise RuntimeError(f"Could not fetch video metadata for fixed camera {label}: {exc}") from exc
                logger.warning(f"SeaTube lists no video for fixed camera {label}; "
                               f"its {len(anns)} annotation(s) stay unmapped")
                media_files = []
            except Exception as exc:
                raise RuntimeError(f"Could not fetch video metadata for fixed camera {label}: {exc}") from exc
            for record in anns:
                own = _device_media(media_files, record)
                if own and record.get("deviceId") is None:
                    record["deviceId"] = record["videoResourceId"] = own[0].get("deviceId")
                self._attach_archive_info(record, own, resolution)

    @staticmethod
    def _attach_archive_info(
        record: Dict[str, Any],
        media_files: Sequence[Dict[str, Any]],
        resolution: str,
    ) -> None:
        ann_start = record.get("startDate")
        if not media_files or not ann_start:
            record["videoMappingStatus"] = "no_media_for_device" if not media_files else "missing_timestamp"
            return
        media_file, row = media_row_containing(media_files, parse_iso_utc(ann_start))
        if not row:
            record["videoMappingStatus"] = "no_containing_data_file"
            return
        record.update(archive_info_from_row(media_file, row))
        record["videoMappingStatus"] = "strict_containment"
        record["resolution"] = resolution
        record["videoDeviceCode"] = media_file.get("deviceCode")


def _device_media(media_files: Sequence[Dict[str, Any]], record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The media series recorded by this annotation's camera -- never another one."""
    code, device_id = record.get("deviceCode"), record.get("deviceId")
    if code:
        return [m for m in media_files if m.get("deviceCode") == code]
    if device_id is not None:
        return [m for m in media_files if m.get("deviceId") == device_id]
    return []
