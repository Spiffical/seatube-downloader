"""Query ONC for annotations and link each one to its archive video file.

This is the expensive, online half of the package: it walks dives or fixed
cameras, applies the filters, and stamps every kept annotation with the
archive file that truly contains its timestamp.  Everything after it --
summaries, clip listings, image extraction -- runs offline on the result.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .annotations import Annotation, AnnotationSet, ReviewFilters, person_matches
from .archive import (
    archive_info_from_row,
    data_file_row_containing,
    parse_iso_utc,
    select_media_file,
)
from .client import BASE_URL, OncClient
from .taxonomy import WormsResolver, wanted_ancestor_names

TAXONOMY_CODE_TO_ID = {"worms": 1, "cmecs": 2}
TAXONOMY_ID_TO_CODE = {1: "WoRMS", 2: "CMECS"}


@dataclass
class FetchFilters:
    """Everything that narrows a fetch, in one place."""

    start_date: str
    end_date: str
    camera_mode: str = "dive"                      # dive | stationary | both

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
    node_ids: Set[int] = field(default_factory=set)
    location_contains: Optional[str] = None
    max_stationary_locations: Optional[int] = None

    # how
    resolution: str = "L"                          # H | L | S
    max_dives: Optional[int] = None
    page_size: int = 250
    resolve_taxon_names: bool = True

    def effective_taxonomy_code(self) -> Optional[str]:
        code = (self.taxonomy_code or "").strip()
        return code or None

    def matches_people_and_reviews(self, ann: Annotation) -> bool:
        if not person_matches(ann.creator, self.creator_id, self.creator_name, self.creator_email):
            return False
        if not person_matches(ann.modifier, self.modifier_id, self.modifier_name, self.modifier_email):
            return False
        return self.review.matches(ann)

    def matches_taxonomy_ids(
        self,
        record: Dict[str, Any],
        taxonomy_id_override: Optional[int] = None,
    ) -> bool:
        """The cheap taxonomy filter: code / taxonomy id / ONC taxon ids.

        Stationary records pass ``taxonomy_id_override`` because their
        taxonomy entries are built ONC-side and carry the numeric id even
        when only a code was requested.
        """
        code = self.effective_taxonomy_code()
        taxonomy_id = taxonomy_id_override if taxonomy_id_override is not None else self.taxonomy_id
        if not code and taxonomy_id is None and not self.taxon_ids:
            return True
        taxons = record.get("taxons") or record.get("taxonomy") or []
        if not taxons:
            return False
        for taxon in taxons:
            if code and str(taxon.get("taxonomyCode", "")).lower() != code.lower():
                continue
            if taxonomy_id is not None and int(taxon.get("taxonomyId", -1)) != taxonomy_id:
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


def fixed_camera_locations(client: OncClient) -> List[Dict[str, Any]]:
    """Flatten ONC's fixed-camera tree into (id, name, path) rows."""
    leaves: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any], path: List[str]) -> None:
        current_name = node.get("html")
        current_path = path + ([current_name] if current_name else [])
        children = node.get("children") or []
        if children:
            for child in children:
                walk(child, current_path)
            return
        leaves.append({
            "searchTreeNodeId": int(node.get("searchTreeNodeId")),
            "name": node.get("html"),
            "path": " > ".join(current_path),
            "latitude": node.get("latitude"),
            "longitude": node.get("longitude"),
            "siteDepth": node.get("siteDepth"),
        })

    walk(client.fixed_camera_tree(), [])
    leaves.sort(key=lambda x: (x["path"], x["searchTreeNodeId"]))
    return leaves


def seatube_link(resource_type_id: int, resource_id: int, annotation_id: int, ann_start: str) -> str:
    return (
        f"{BASE_URL}/SeaTube?resourceTypeId={resource_type_id}&resourceId={resource_id}"
        f"&time={ann_start}&annotationId={annotation_id}"
    )


def _normalized_user(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = raw or {}
    return {
        "userId": raw.get("userId") if raw.get("userId") is not None else raw.get("dmasUserId"),
        "firstName": raw.get("firstName") if raw.get("firstName") is not None else raw.get("firstname"),
        "lastName": raw.get("lastName") if raw.get("lastName") is not None else raw.get("lastname"),
        "email": raw.get("email"),
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
        stationary_media: Dict[int, Dict[str, Any]] = {}

        if filters.camera_mode in ("dive", "both"):
            records.extend(self._dive_records(filters, start_dt, end_dt))
        if filters.camera_mode in ("stationary", "both"):
            stationary_records, stationary_media = self._stationary_records(filters, start_dt, end_dt)
            records.extend(stationary_records)

        print(f"\nMatched annotations across selected modes: {len(records)}")

        # Broad-group / lineage filtering happens before archive mapping so
        # discarded annotations never cost a video-metadata request.
        wanted = wanted_ancestor_names(filters.groups, filters.taxon_names)
        if wanted:
            resolver = self.resolver or WormsResolver(None)
            print(f"Filtering to {len(wanted)} ancestor taxa ({', '.join(sorted(wanted))})")
            before = len(records)
            records = [r for r in records if resolver.annotation_matches(r, wanted)]
            resolver.save()
            if resolver.unresolved:
                print(f"[WARN] {len(resolver.unresolved)} taxa could not be resolved and were skipped")
            print(f"Kept {len(records)} of {before} annotations")

        print("Mapping annotations to archive video files (strict containment)...")
        self._attach_dive_archive_info(records, filters.resolution)
        self._attach_stationary_archive_info(records, stationary_media, filters.resolution)
        mapped = sum(1 for r in records if r.get("archiveFilename"))
        print(f"{mapped}/{len(records)} annotations mapped to an archive file")

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
        print(f"Dives overlapping the date range: {len(selected)}")

        matched: List[Dict[str, Any]] = []
        for index, dive in enumerate(selected, start=1):
            dive_id = int(dive["diveId"])
            try:
                dive_annotations = self.client.dive_annotations(dive_id)
            except Exception as exc:
                print(f"[WARN] dive {dive_id}: failed to fetch annotations ({exc})")
                continue

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
                    "diveName": ann.get("diveName"),
                    "cruiseName": ann.get("cruiseName"),
                    "stationarySearchTreeNodeId": None,
                    "stationaryLocationName": None,
                    "stationaryLocationPath": None,
                    "startDate": ann.get("startDate"),
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
                    "contextualLink": seatube_link(600, dive_id, ann_id, ann.get("startDate") or ""),
                }

                wrapped = Annotation(record)
                if not filters.matches_people_and_reviews(wrapped):
                    continue
                if not filters.matches_taxonomy_ids(record):
                    continue
                matched.append(record)

            if index % 10 == 0 or index == len(selected):
                print(f"  {index}/{len(selected)} dives scanned; matched so far: {len(matched)}")

        print(f"Matched dive annotations after filters: {len(matched)}")
        return matched

    # ------------------------------------------------------------------
    # fixed cameras
    # ------------------------------------------------------------------

    def _stationary_records(
        self,
        filters: FetchFilters,
        start_dt: datetime,
        end_dt: datetime,
    ) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        all_locations = fixed_camera_locations(self.client)

        selected = []
        needle = (filters.location_contains or "").lower() or None
        for loc in all_locations:
            node_id = int(loc["searchTreeNodeId"])
            if filters.node_ids and node_id not in filters.node_ids:
                continue
            if needle and needle not in f"{loc.get('name','')} {loc.get('path','')}".lower():
                continue
            selected.append(loc)
        if not filters.node_ids and not needle:
            selected = all_locations
        if filters.max_stationary_locations is not None:
            selected = selected[: filters.max_stationary_locations]

        print(f"Fixed-camera locations selected: {len(selected)} (of {len(all_locations)})")
        if not selected:
            return [], {}

        media_by_device: Dict[int, Dict[str, Any]] = {}
        location_by_device: Dict[int, Dict[str, Any]] = {}
        for loc in selected:
            payload = self.client.stationary_video_metadata(
                int(loc["searchTreeNodeId"]), filters.resolution
            )
            for media_file in payload.get("mediaFiles", []) or []:
                device_id = int(media_file.get("deviceId", 0))
                if device_id <= 0:
                    continue
                existing = media_by_device.get(device_id)
                if existing is None or int(media_file.get("count", 0) or 0) > int(existing.get("count", 0) or 0):
                    media_by_device[device_id] = media_file
                    location_by_device[device_id] = loc

        device_ids = sorted(media_by_device)
        print(f"Stationary devices with video data: {len(device_ids)}")

        matched: List[Dict[str, Any]] = []
        detail_cache: Dict[int, Dict[str, Any]] = {}
        taxon_cache: Dict[Tuple[int, int], Optional[Dict[str, Any]]] = {}

        for idx, device_id in enumerate(device_ids, start=1):
            try:
                candidates = self._stationary_candidates(device_id, filters)
            except Exception as exc:
                print(f"[WARN] device {device_id}: failed candidate fetch ({exc})")
                continue

            loc = location_by_device.get(device_id) or {}
            for naive in candidates:
                ann_id = int(naive.get("annotationId", 0))
                if ann_id <= 0:
                    continue
                detail = detail_cache.get(ann_id)
                if detail is None:
                    try:
                        detail = self.client.annotation_detail(ann_id)
                    except Exception as exc:
                        print(f"[WARN] annotation {ann_id}: detail fetch failed ({exc})")
                        continue
                    detail_cache[ann_id] = detail

                ann_start = detail.get("startDate") or naive.get("startDate")
                if not ann_start:
                    continue
                try:
                    ts = parse_iso_utc(ann_start)
                except Exception:
                    continue
                if ts < start_dt or ts > end_dt:
                    continue

                taxonomy = []
                for content in detail.get("annotationContents") or []:
                    t_id, x_id = content.get("taxonomyId"), content.get("taxonId")
                    if t_id in (None, 0) and x_id in (None, 0):
                        continue
                    taxonomy.append(self._taxonomy_entry(content, taxon_cache, filters.resolve_taxon_names))

                source = detail.get("annotationSource")
                record = {
                    "cameraMode": "stationary",
                    "annotationId": ann_id,
                    "diveId": None,
                    "diveName": None,
                    "cruiseName": None,
                    "stationarySearchTreeNodeId": loc.get("searchTreeNodeId"),
                    "stationaryLocationName": loc.get("name"),
                    "stationaryLocationPath": loc.get("path"),
                    "startDate": ann_start,
                    "endDate": detail.get("endDate") or naive.get("endDate"),
                    "comment": (naive.get("annotationSummary") or "").strip(),
                    "annotationSource": source.get("annotationSource") if isinstance(source, dict) else source,
                    "createdBy": _normalized_user(detail.get("createdBy")),
                    "modifiedBy": _normalized_user(detail.get("modifiedBy")),
                    "createdDate": detail.get("createdDate"),
                    "modifiedDate": detail.get("modifiedDate"),
                    "taxonomy": taxonomy,
                    "videoResourceId": naive.get("resourceId"),
                    "videoResourceTypeId": detail.get("resourceType", {}).get("resourceTypeId"),
                    "resourceTypeId": detail.get("resourceType", {}).get("resourceTypeId"),
                    "resourceTypeName": detail.get("resourceType", {}).get("resourceTypeName"),
                    "deviceId": naive.get("resourceId"),
                    "lat": naive.get("lat"),
                    "lon": naive.get("lon"),
                    "depth": None,
                    "heading": None,
                    "toBeReviewed": detail.get("toBeReviewed", naive.get("toBeReviewed")),
                    "numPositiveReviews": detail.get("numPositiveReviews", naive.get("numPositiveReviews")),
                    "numTotalReviews": detail.get("numTotalReviews", naive.get("numTotalReviews")),
                    "contextualLink": naive.get("contextualLink")
                    or seatube_link(1000, int(naive.get("resourceId", 0)), ann_id, ann_start),
                }

                wrapped = Annotation(record)
                if not filters.matches_people_and_reviews(wrapped):
                    continue
                taxonomy_id = filters.taxonomy_id
                if taxonomy_id is None:
                    code = filters.effective_taxonomy_code()
                    taxonomy_id = TAXONOMY_CODE_TO_ID.get(code.lower()) if code else None
                if not filters.matches_taxonomy_ids(record, taxonomy_id_override=taxonomy_id):
                    continue
                matched.append(record)

            if idx % 5 == 0 or idx == len(device_ids):
                print(f"  {idx}/{len(device_ids)} devices scanned; matched so far: {len(matched)}")

        print(f"Matched stationary annotations after filters: {len(matched)}")
        return matched, media_by_device

    def _stationary_candidates(self, device_id: int, filters: FetchFilters) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        page_num = 1
        while True:
            payload = self.client.stationary_annotation_page(
                device_id=device_id,
                start_date=filters.start_date,
                end_date=filters.end_date,
                page_num=page_num,
                page_size=filters.page_size,
                user_id=filters.creator_id,
            )
            batch = payload.get("naiveAnnotationList", []) or []
            if not batch:
                break
            rows.extend(batch)
            total_pages = payload.get("totalNumOfPages")
            if total_pages is not None and page_num >= int(total_pages):
                break
            if len(batch) < filters.page_size:
                break
            page_num += 1
        return rows

    def _taxonomy_entry(
        self,
        content: Dict[str, Any],
        cache: Dict[Tuple[int, int], Optional[Dict[str, Any]]],
        resolve_name: bool,
    ) -> Dict[str, Any]:
        """Resolve one stationary annotationContents entry to a taxonomy record."""
        taxonomy_id = content.get("taxonomyId")
        taxon_id = content.get("taxonId")
        raw_label = str(content.get("annotation") or "").strip()
        label = raw_label if raw_label.lower() not in {"", "taxon"} else None
        entry: Dict[str, Any] = {
            "taxonomyCode": TAXONOMY_ID_TO_CODE.get(int(taxonomy_id)) if taxonomy_id is not None else None,
            "taxonomyId": taxonomy_id,
            "taxonId": taxon_id,
            "displayText": label,
            "taxonUrl": None,
            "attributes": content.get("annotationAttributes") or [],
            "resolutionStatus": "annotation_label" if label else "unresolved",
        }
        if not resolve_name or taxonomy_id in (None, 0) or taxon_id in (None, 0):
            return entry

        key = (int(taxonomy_id), int(taxon_id))
        if key not in cache:
            try:
                cache[key] = self.client.taxon_detail(*key)
            except Exception as exc:
                print(f"[WARN] taxonomy {key[0]} taxon {key[1]}: name resolution failed ({exc})")
                cache[key] = None

        record = cache[key]
        if not record:
            entry["resolutionStatus"] = "lookup_failed"
            return entry

        external = record.get("jsonTaxonData") or {}
        entry.update({
            "displayText": record.get("commonName")
            or external.get("scientificname")
            or external.get("valid_name")
            or label,
            "taxonUrl": record.get("referenceUrl") or external.get("url"),
            "referenceId": record.get("referenceId"),
            "rank": external.get("rank"),
            "scientificName": external.get("scientificname"),
            "validName": external.get("valid_name"),
            "englishNames": record.get("englishNames") or [],
            "resolutionStatus": "onc_taxonomy",
        })
        return entry

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
                print(f"[WARN] dive {dive_id}: failed to fetch video metadata ({exc})")
                continue
            media_files = video_meta.get("mediaFiles", []) or []
            if not media_files:
                continue
            for record in anns:
                self._attach_archive_info(
                    record, select_media_file(media_files, record.get("deviceId")), resolution
                )

    def _attach_stationary_archive_info(
        self,
        records: List[Dict[str, Any]],
        media_by_device: Dict[int, Dict[str, Any]],
        resolution: str,
    ) -> None:
        for record in records:
            if record.get("cameraMode") != "stationary" or record.get("deviceId") is None:
                continue
            self._attach_archive_info(
                record, media_by_device.get(int(record["deviceId"])), resolution
            )

    @staticmethod
    def _attach_archive_info(
        record: Dict[str, Any],
        media_file: Optional[Dict[str, Any]],
        resolution: str,
    ) -> None:
        ann_start = record.get("startDate")
        if not media_file or not ann_start:
            return
        row = data_file_row_containing(media_file, parse_iso_utc(ann_start))
        if not row:
            record["videoMappingStatus"] = "no_containing_data_file"
            return
        record.update(archive_info_from_row(media_file, row))
        record["videoMappingStatus"] = "strict_containment"
        record["resolution"] = resolution
        record["videoDeviceCode"] = media_file.get("deviceCode")
