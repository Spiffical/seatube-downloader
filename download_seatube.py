import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import requests
from dotenv import load_dotenv

from taxon_groups import (
    TaxonResolver,
    format_group_table,
    normalize_group,
    resolve_wanted,
)

BASE_URL = "https://data.oceannetworks.ca"

TAXONOMY_CODE_TO_ID = {
    "worms": 1,
    "cmecs": 2,
}

TAXONOMY_ID_TO_CODE = {
    1: "WoRMS",
    2: "CMECS",
}

ML_COLUMNS = [
    "annotation_id",
    "annotation_source",
    "annotation_comment",
    "annotation_start_utc",
    "annotation_end_utc",
    "annotation_duration_seconds",
    "camera_mode",
    "dive_id",
    "dive_name",
    "cruise_name",
    "stationary_search_tree_node_id",
    "stationary_location_name",
    "stationary_location_path",
    "resource_type_id",
    "resource_type_name",
    "device_id",
    "video_resource_id",
    "video_resource_type_id",
    "creator_user_id",
    "creator_first_name",
    "creator_last_name",
    "creator_email",
    "modifier_user_id",
    "modifier_first_name",
    "modifier_last_name",
    "modifier_email",
    "created_date_utc",
    "modified_date_utc",
    "lat",
    "lon",
    "depth_m",
    "heading_deg",
    "to_be_reviewed",
    "num_positive_reviews",
    "num_total_reviews",
    "taxonomy_index",
    "taxonomy_code",
    "taxonomy_id",
    "taxon_id",
    "taxon_display_text",
    "taxon_url",
    "taxon_attributes_json",
    "archive_filename",
    "video_resolution_code",
    "video_device_code",
    "clip_offset_seconds",
    "clip_duration_seconds",
    "clip_relative_path",
    "clip_row_start_offset_seconds",
    "archive_clip_start_utc",
    "video_local_path",
    "video_downloaded",
    "contextual_link",
]


def parse_int_set(value: Optional[str]) -> Set[int]:
    if not value:
        return set()
    out: Set[int] = set()
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        out.add(int(raw))
    return out


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


def annotation_duration_seconds(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    try:
        return (parse_iso_utc(end) - parse_iso_utc(start)).total_seconds()
    except Exception:
        return None


def taxonomy_code_from_id(taxonomy_id: Optional[int]) -> Optional[str]:
    if taxonomy_id is None:
        return None
    return TAXONOMY_ID_TO_CODE.get(int(taxonomy_id), f"taxonomy-{taxonomy_id}")


class ONCApiClient:
    def __init__(self, token: Optional[str], timeout_seconds: int = 45):
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        query = {k: v for k, v in params.items() if v is not None and v != ""}
        if self.token:
            query.setdefault("token", self.token)

        resp = self.session.get(
            f"{BASE_URL}{path}",
            params=query,
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()

        status_code = payload.get("statusCode")
        if status_code not in (None, 0):
            msg = payload.get("message", "Unknown API error")
            raise RuntimeError(f"{path} failed with statusCode={status_code}: {msg}")

        return payload

    def download_archive_file(self, filename: str, output_path: str) -> None:
        if not self.token:
            raise RuntimeError("Token is required for archive downloads")

        with self.session.get(
            f"{BASE_URL}/api/archivefile/download",
            params={"filename": filename, "token": self.token},
            stream=True,
            timeout=self.timeout_seconds,
        ) as resp:
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)


def fetch_all_dives(client: ONCApiClient) -> List[Dict[str, Any]]:
    data = client.get_json("/DiveListingService", {"operation": 11})
    return data.get("payload", {}).get("dives", []) or []


def select_dives_by_date(
    dives: Sequence[Dict[str, Any]],
    start_dt: datetime,
    end_dt: datetime,
    dive_filter: Set[int],
    max_dives: Optional[int],
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []

    for dive in dives:
        dive_id = int(dive.get("diveId", 0))
        if dive_id <= 0:
            continue

        if dive_filter and dive_id not in dive_filter:
            continue

        date_from = dive.get("dateFrom")
        date_to = dive.get("dateTo")
        if not date_from or not date_to:
            continue

        try:
            dive_start = parse_iso_utc(date_from)
            dive_end = parse_iso_utc(date_to)
        except Exception:
            continue

        # Keep dives that overlap [start_dt, end_dt].
        if dive_end < start_dt or dive_start > end_dt:
            continue

        selected.append(dive)

    selected.sort(key=lambda d: d.get("dateFrom", ""))

    if max_dives is not None:
        selected = selected[:max_dives]

    return selected


def fetch_dive_annotations(client: ONCApiClient, dive_id: int) -> List[Dict[str, Any]]:
    data = client.get_json(
        "/seatubeV3/annotations",
        {"operation": 1, "diveIds": dive_id, "filter": "{}"},
    )
    return data.get("payload", {}).get("annotations", []) or []


def fetch_dive_video_metadata(client: ONCApiClient, dive_id: int, resolution: str) -> Dict[str, Any]:
    data = client.get_json(
        "/seatube/videos",
        {"operation": 1, "diveId": dive_id, "resolution": resolution},
    )
    return data.get("payload", {})


def fetch_fixed_camera_tree(client: ONCApiClient) -> Dict[str, Any]:
    data = client.get_json(
        "/SearchTreeService",
        {"operation": 15, "deviceCategoryId": 14, "label": "Fixed Cameras"},
    )
    return data.get("payload", {})


def flatten_fixed_camera_locations(root: Dict[str, Any]) -> List[Dict[str, Any]]:
    leaves: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any], path: List[str]) -> None:
        current_name = node.get("html")
        current_path = path + ([current_name] if current_name else [])
        children = node.get("children") or []
        if children:
            for child in children:
                walk(child, current_path)
            return

        leaves.append(
            {
                "searchTreeNodeId": int(node.get("searchTreeNodeId")),
                "name": node.get("html"),
                "path": " > ".join(current_path),
                "latitude": node.get("latitude"),
                "longitude": node.get("longitude"),
                "siteDepth": node.get("siteDepth"),
            }
        )

    walk(root, [])
    leaves.sort(key=lambda x: (x["path"], x["searchTreeNodeId"]))
    return leaves


def select_fixed_camera_locations(
    locations: Sequence[Dict[str, Any]],
    node_filter: Set[int],
    name_contains: Optional[str],
    max_locations: Optional[int],
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    needle = name_contains.lower() if name_contains else None

    for loc in locations:
        node_id = int(loc["searchTreeNodeId"])
        if node_filter and node_id not in node_filter:
            continue

        if needle:
            hay = f"{loc.get('name','')} {loc.get('path','')}".lower()
            if needle not in hay:
                continue

        selected.append(loc)

    if max_locations is not None:
        selected = selected[:max_locations]

    return selected


def fetch_stationary_video_metadata(client: ONCApiClient, search_tree_node_id: int, resolution: str) -> Dict[str, Any]:
    data = client.get_json(
        "/seatube/videos",
        {"operation": 1, "searchTreeNodeId": search_tree_node_id, "resolution": resolution},
    )
    return data.get("payload", {})


def build_stationary_video_index(
    client: ONCApiClient,
    selected_locations: Sequence[Dict[str, Any]],
    resolution: str,
) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    media_by_device: Dict[int, Dict[str, Any]] = {}
    location_by_device: Dict[int, Dict[str, Any]] = {}

    for loc in selected_locations:
        node_id = int(loc["searchTreeNodeId"])
        payload = fetch_stationary_video_metadata(client, node_id, resolution)
        media_files = payload.get("mediaFiles", []) or []

        for media_file in media_files:
            device_id = int(media_file.get("deviceId", 0))
            if device_id <= 0:
                continue

            existing = media_by_device.get(device_id)
            if existing is None:
                media_by_device[device_id] = media_file
                location_by_device[device_id] = loc
                continue

            # If duplicate device entries appear, keep the one with larger clip count.
            new_count = int(media_file.get("count", 0) or 0)
            old_count = int(existing.get("count", 0) or 0)
            if new_count > old_count:
                media_by_device[device_id] = media_file
                location_by_device[device_id] = loc

    return media_by_device, location_by_device


def fetch_stationary_annotation_candidates_for_device(
    client: ONCApiClient,
    device_id: int,
    start_date: str,
    end_date: str,
    user_id: Optional[int],
    page_size: int,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    page_num = 1

    while True:
        params: Dict[str, Any] = {
            "operation": 1,
            "resourceTypeId": 1000,
            "annotationSources": 6,
            "resourceId": device_id,
            "fromDate": start_date,
            "toDate": end_date,
            "pageSize": page_size,
            "pageNum": page_num,
        }
        if user_id is not None:
            params["userId"] = user_id

        data = client.get_json("/AnnotationServiceV3", params)
        payload = data.get("payload", {})
        batch = payload.get("naiveAnnotationList", []) or []

        if not batch:
            break

        rows.extend(batch)

        total_pages = payload.get("totalNumOfPages")
        if total_pages is not None and page_num >= int(total_pages):
            break

        if len(batch) < page_size:
            break

        page_num += 1

    return rows


def fetch_annotation_detail(client: ONCApiClient, annotation_id: int) -> Dict[str, Any]:
    data = client.get_json("/AnnotationServiceV3", {"annotationId": annotation_id})
    return data.get("payload", {})


def fetch_taxon_detail(
    client: ONCApiClient,
    taxonomy_id: int,
    taxon_id: int,
) -> Dict[str, Any]:
    """Resolve an ONC-internal taxon ID to its canonical taxonomy record.

    ``taxonId`` in AnnotationServiceV3 is not a WoRMS AphiaID.  The SeaTube
    UI resolves it through this ONC endpoint, whose response also carries the
    external WoRMS reference for imported taxonomies.
    """
    return client.get_json(
        f"/internal/taxonomies/{int(taxonomy_id)}/taxons/{int(taxon_id)}",
        {},
    )


def build_taxonomy_entry(
    client: ONCApiClient,
    content: Dict[str, Any],
    cache: Dict[Tuple[int, int], Optional[Dict[str, Any]]],
    *,
    resolve_name: bool,
) -> Dict[str, Any]:
    taxonomy_id = content.get("taxonomyId")
    taxon_id = content.get("taxonId")
    raw_label = str(content.get("annotation") or "").strip()
    label = raw_label if raw_label.lower() not in {"", "taxon"} else None
    entry: Dict[str, Any] = {
        "taxonomyCode": taxonomy_code_from_id(taxonomy_id),
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
            cache[key] = fetch_taxon_detail(client, *key)
        except Exception as exc:
            print(
                f"[WARN] taxonomy {key[0]} taxon {key[1]}: "
                f"name resolution failed ({exc})"
            )
            cache[key] = None

    record = cache[key]
    if not record:
        entry["resolutionStatus"] = "lookup_failed"
        return entry

    external = record.get("jsonTaxonData") or {}
    display_text = (
        record.get("commonName")
        or external.get("scientificname")
        or external.get("valid_name")
        or label
    )
    entry.update(
        {
            "displayText": display_text,
            "taxonUrl": record.get("referenceUrl") or external.get("url"),
            "referenceId": record.get("referenceId"),
            "rank": external.get("rank"),
            "scientificName": external.get("scientificname"),
            "validName": external.get("valid_name"),
            "englishNames": record.get("englishNames") or [],
            "resolutionStatus": "onc_taxonomy",
        }
    )
    return entry


def normalize_user(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = raw or {}
    return {
        "userId": raw.get("userId") if raw.get("userId") is not None else raw.get("dmasUserId"),
        "firstName": raw.get("firstName") if raw.get("firstName") is not None else raw.get("firstname"),
        "lastName": raw.get("lastName") if raw.get("lastName") is not None else raw.get("lastname"),
        "email": raw.get("email"),
    }


def annotation_time(annotation: Dict[str, Any]) -> datetime:
    ts = annotation.get("startDate") or annotation.get("dateFrom")
    if not ts:
        raise ValueError(f"Annotation {annotation.get('annotationId')} missing startDate/dateFrom")
    return parse_iso_utc(ts)


def pick_media_file(media_files: Sequence[Dict[str, Any]], annotation_device_id: Optional[int]) -> Optional[Dict[str, Any]]:
    if not media_files:
        return None
    if annotation_device_id is None:
        return media_files[0]
    for media_file in media_files:
        if media_file.get("deviceId") == annotation_device_id:
            return media_file
    return media_files[0]


def data_file_row_epoch_bounds(
    media_file: Dict[str, Any],
    row: Sequence[str],
) -> Tuple[float, float]:
    date_start = float(media_file.get("dateStartSeconds", 0))
    offset = float(row[0])
    duration = float(row[1])
    extra_ms = float(row[4]) if len(row) > 4 else 0.0
    start = date_start + offset + (extra_ms / 1000.0)
    return start, start + duration


def pick_data_file_row(media_file: Dict[str, Any], ann_time: datetime) -> Optional[Sequence[str]]:
    """Return only a data-file row that truly contains ``ann_time``.

    A previous nearest-row fallback silently mapped annotations across real
    recording gaps (including onto a different day).  Downstream code then
    clamped the negative relative timestamp to zero, producing plausible but
    false clips.  Absence of a containing row is now explicit.
    """
    target_epoch = ann_time.timestamp()
    rows = media_file.get("dataFiles", []) or []

    for row in rows:
        start_epoch, end_epoch = data_file_row_epoch_bounds(media_file, row)
        if start_epoch <= target_epoch < end_epoch:
            return row
    return None


def build_archive_file_info(media_file: Dict[str, Any], row: Sequence[str]) -> Dict[str, Any]:
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


def get_annotation_user_fields(annotation: Dict[str, Any]) -> Tuple[Optional[int], str, str]:
    created_by = annotation.get("createdBy") or {}
    user_id = created_by.get("userId")
    full_name = f"{created_by.get('firstName', '')} {created_by.get('lastName', '')}".strip()
    email = created_by.get("email") or ""
    return user_id, full_name, email


def get_person_fields(person: Optional[Dict[str, Any]]) -> Tuple[Optional[int], str, str]:
    person = person or {}
    user_id = person.get("userId")
    full_name = f"{person.get('firstName', '')} {person.get('lastName', '')}".strip()
    email = person.get("email") or ""
    return user_id, full_name, email


def get_annotation_modifier_fields(annotation: Dict[str, Any]) -> Tuple[Optional[int], str, str]:
    return get_person_fields(annotation.get("modifiedBy"))


def modifier_matches(
    annotation: Dict[str, Any],
    user_id: Optional[int],
    name_contains: Optional[str],
    email_contains: Optional[str],
) -> bool:
    ann_user_id, ann_name, ann_email = get_annotation_modifier_fields(annotation)

    if user_id is not None and ann_user_id != user_id:
        return False

    if name_contains and name_contains.lower() not in ann_name.lower():
        return False

    if email_contains and email_contains.lower() not in ann_email.lower():
        return False

    return True


def parse_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except Exception:
        return None


def parse_optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return None


def review_quality_matches(annotation: Dict[str, Any], args: argparse.Namespace) -> bool:
    to_be_reviewed = parse_optional_bool(annotation.get("toBeReviewed"))
    num_total = parse_optional_int(annotation.get("numTotalReviews"))
    num_positive = parse_optional_int(annotation.get("numPositiveReviews"))

    is_reviewed: Optional[bool] = None
    if to_be_reviewed is not None:
        is_reviewed = not to_be_reviewed
    elif num_total is not None:
        is_reviewed = num_total > 0

    if args.reviewed_only and is_reviewed is not True:
        return False

    if args.min_total_reviews is not None:
        if num_total is None or num_total < args.min_total_reviews:
            return False

    if args.min_positive_reviews is not None:
        if num_positive is None or num_positive < args.min_positive_reviews:
            return False

    if args.min_positive_review_rate is not None:
        if num_total is None or num_total <= 0 or num_positive is None:
            return False
        if (num_positive / num_total) < args.min_positive_review_rate:
            return False

    if args.require_cross_review:
        creator_id, _, _ = get_annotation_user_fields(annotation)
        modifier_id, _, _ = get_annotation_modifier_fields(annotation)
        if creator_id is None or modifier_id is None or creator_id == modifier_id:
            return False

    return True


def taxonomy_matches(
    annotation: Dict[str, Any],
    taxonomy_code: Optional[str],
    taxonomy_id: Optional[int],
    taxon_ids: Set[int],
) -> bool:
    if not taxonomy_code and taxonomy_id is None and not taxon_ids:
        return True

    taxons = annotation.get("taxons") or annotation.get("taxonomy") or []
    if not taxons:
        return False

    for taxon in taxons:
        if taxonomy_code and str(taxon.get("taxonomyCode", "")).lower() != taxonomy_code.lower():
            continue
        if taxonomy_id is not None and int(taxon.get("taxonomyId", -1)) != taxonomy_id:
            continue
        if taxon_ids and int(taxon.get("taxonId", -1)) not in taxon_ids:
            continue
        return True

    return False


def creator_matches(
    annotation: Dict[str, Any],
    user_id: Optional[int],
    name_contains: Optional[str],
    email_contains: Optional[str],
) -> bool:
    ann_user_id, ann_name, ann_email = get_annotation_user_fields(annotation)

    if user_id is not None and ann_user_id != user_id:
        return False

    if name_contains and name_contains.lower() not in ann_name.lower():
        return False

    if email_contains and email_contains.lower() not in ann_email.lower():
        return False

    return True


def build_contextual_link(resource_type_id: int, resource_id: int, annotation_id: int, ann_start: str) -> str:
    return (
        f"{BASE_URL}/SeaTube?resourceTypeId={resource_type_id}&resourceId={resource_id}"
        f"&time={ann_start}&annotationId={annotation_id}"
    )


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def build_ml_rows(matched_annotations: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for ann in matched_annotations:
        created_by = ann.get("createdBy") or {}
        modified_by = ann.get("modifiedBy") or {}

        taxons = ann.get("taxonomy") or []
        if not taxons:
            taxons = [None]

        for taxon_index, taxon in enumerate(taxons):
            attributes = []
            taxonomy_code = None
            taxonomy_id = None
            taxon_id = None
            taxon_display = None
            taxon_url = None

            if isinstance(taxon, dict):
                attributes = taxon.get("attributes") or []
                taxonomy_code = taxon.get("taxonomyCode")
                taxonomy_id = taxon.get("taxonomyId")
                taxon_id = taxon.get("taxonId")
                taxon_display = taxon.get("displayText")
                taxon_url = taxon.get("taxonUrl")

            row = {
                "annotation_id": ann.get("annotationId"),
                "annotation_source": ann.get("annotationSource"),
                "annotation_comment": ann.get("comment"),
                "annotation_start_utc": ann.get("startDate"),
                "annotation_end_utc": ann.get("endDate"),
                "annotation_duration_seconds": annotation_duration_seconds(ann.get("startDate"), ann.get("endDate")),
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
            }
            rows.append(row)

    return rows


def write_ml_csv(rows: Sequence[Dict[str, Any]], csv_path: str) -> None:
    ensure_parent_dir(csv_path)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ML_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_ml_jsonl(rows: Sequence[Dict[str, Any]], jsonl_path: str) -> None:
    ensure_parent_dir(jsonl_path)
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    load_dotenv()

    if "--list-groups" in sys.argv[1:]:
        print(format_group_table())
        raise SystemExit(0)

    parser = argparse.ArgumentParser(
        description="Download SeaTube videos that contain annotations filtered by creator and taxonomy.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "--token",
        type=str,
        default=os.getenv("ONC_TOKEN"),
        help="ONC API token (can be provided via ONC_TOKEN in .env).",
    )

    parser.add_argument(
        "--camera-mode",
        choices=["dive", "stationary", "both"],
        default="dive",
        help="Which SeaTube sources to search.",
    )

    parser.add_argument(
        "--start-date",
        type=str,
        required=True,
        help="ISO8601 UTC start date (e.g. 2023-06-01T00:00:00.000Z)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        required=True,
        help="ISO8601 UTC end date",
    )

    parser.add_argument(
        "--user-id",
        type=int,
        help="Filter annotations created by this ONC user ID",
    )
    parser.add_argument(
        "--created-by-name",
        type=str,
        help="Case-insensitive substring match on creator full name",
    )
    parser.add_argument(
        "--created-by-email",
        type=str,
        help="Case-insensitive substring match on creator email",
    )
    parser.add_argument(
        "--modified-by-user-id",
        type=int,
        help="Filter annotations by modifier/reviewer ONC user ID",
    )
    parser.add_argument(
        "--modified-by-name",
        type=str,
        help="Case-insensitive substring match on modifier/reviewer full name",
    )
    parser.add_argument(
        "--modified-by-email",
        type=str,
        help="Case-insensitive substring match on modifier/reviewer email",
    )

    parser.add_argument(
        "--reviewed-only",
        action="store_true",
        help="Keep only annotations that appear reviewed (toBeReviewed=False or numTotalReviews>0)",
    )
    parser.add_argument(
        "--min-total-reviews",
        type=int,
        help="Keep only annotations with at least this many total reviews",
    )
    parser.add_argument(
        "--min-positive-reviews",
        type=int,
        help="Keep only annotations with at least this many positive reviews",
    )
    parser.add_argument(
        "--min-positive-review-rate",
        type=float,
        help="Keep only annotations with positive_reviews/total_reviews >= this value (0..1)",
    )
    parser.add_argument(
        "--require-cross-review",
        action="store_true",
        help="Keep only annotations where modifier/reviewer user differs from creator user",
    )

    parser.add_argument(
        "--taxonomy-code",
        type=str,
        default="WoRMS",
        help="Taxonomy code filter (default: WoRMS). Use empty string to disable.",
    )
    parser.add_argument(
        "--taxonomy-id",
        type=int,
        help="Optional taxonomy ID filter (e.g. 1 for WoRMS)",
    )
    parser.add_argument(
        "--taxon-id",
        type=str,
        help="Optional comma-separated taxon IDs",
    )
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Keep only annotations in a broad taxon group, repeatable\n"
            "(e.g. --group crabs --group sponges). See --list-groups."
        ),
    )
    parser.add_argument(
        "--taxon-name",
        action="append",
        default=[],
        metavar="TAXON",
        help="Keep annotations at or below any WoRMS taxon, repeatable (e.g. Brachyura)",
    )
    parser.add_argument(
        "--list-groups",
        action="store_true",
        help="Print the broad taxon group vocabulary and exit",
    )
    parser.add_argument(
        "--worms-cache",
        type=str,
        help="WoRMS lineage cache path (default: <output-dir>/.worms_cache.json)",
    )
    parser.add_argument(
        "--offline-taxa",
        action="store_true",
        help="Never call WoRMS; match --group/--taxon-name from the cache and labels only",
    )

    # Dive-specific
    parser.add_argument(
        "--dive-id",
        type=str,
        help="Optional comma-separated dive IDs",
    )

    # Stationary-specific
    parser.add_argument(
        "--search-tree-node-id",
        type=str,
        help="Optional comma-separated fixed-camera location node IDs",
    )
    parser.add_argument(
        "--location-name-contains",
        type=str,
        help="Optional case-insensitive substring match over fixed-camera location name/path",
    )
    parser.add_argument(
        "--list-stationary-locations",
        action="store_true",
        help="List all fixed-camera location node IDs and exit",
    )
    parser.add_argument(
        "--max-stationary-locations",
        type=int,
        help="Optional cap on number of stationary locations to scan",
    )
    parser.add_argument(
        "--stationary-page-size",
        type=int,
        default=250,
        help="Page size for AnnotationServiceV3 stationary queries",
    )
    parser.add_argument(
        "--skip-taxon-name-resolution",
        action="store_true",
        help=(
            "Keep only internal stationary taxon IDs instead of resolving "
            "their names through ONC's taxonomy endpoint."
        ),
    )

    parser.add_argument(
        "--resolution",
        choices=["H", "L", "S"],
        default="L",
        help="Video resolution: H (high), L (medium), S (low)",
    )

    parser.add_argument(
        "--output-dir",
        type=str,
        default="downloads",
        help="Base output directory",
    )

    parser.add_argument(
        "--metadata-file",
        type=str,
        default="downloads/matched_annotations.json",
        help="Where to save matched annotation metadata JSON",
    )

    parser.add_argument(
        "--ml-csv-file",
        type=str,
        help="ML-ready flattened CSV output path (default: <output-dir>/annotations_ml.csv)",
    )

    parser.add_argument(
        "--ml-jsonl-file",
        type=str,
        help="ML-ready flattened JSONL output path (default: <output-dir>/annotations_ml.jsonl)",
    )

    parser.add_argument(
        "--download-videos",
        action="store_true",
        help="Download unique MP4 files for the matched annotations",
    )

    parser.add_argument(
        "--max-downloads",
        type=int,
        help="Optional cap on number of MP4 files to download",
    )

    parser.add_argument(
        "--max-dives",
        type=int,
        help="Optional cap on number of dives processed",
    )

    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=45,
        help="HTTP timeout in seconds",
    )

    args = parser.parse_args()

    if not args.token:
        parser.error("ONC token must be provided via --token or ONC_TOKEN in .env")

    if args.min_total_reviews is not None and args.min_total_reviews < 0:
        parser.error("--min-total-reviews must be >= 0")
    if args.min_positive_reviews is not None and args.min_positive_reviews < 0:
        parser.error("--min-positive-reviews must be >= 0")
    if args.min_positive_review_rate is not None and not (0.0 <= args.min_positive_review_rate <= 1.0):
        parser.error("--min-positive-review-rate must be between 0 and 1")

    for name in args.group:
        try:
            normalize_group(name)
        except KeyError:
            parser.error(f"unknown group {name!r}. Run --list-groups to see the vocabulary.")

    return args


def collect_dive_matches(
    client: ONCApiClient,
    start_dt: datetime,
    end_dt: datetime,
    args: argparse.Namespace,
    taxonomy_code: Optional[str],
    taxon_ids: Set[int],
) -> List[Dict[str, Any]]:
    dive_filter = parse_int_set(args.dive_id)

    print("\n--- Dive Step 1: Discover dives in date range (DiveListingService) ---")
    all_dives = fetch_all_dives(client)
    selected_dives = select_dives_by_date(
        dives=all_dives,
        start_dt=start_dt,
        end_dt=end_dt,
        dive_filter=dive_filter,
        max_dives=args.max_dives,
    )

    print(f"Total dives in listing: {len(all_dives)}")
    print(f"Dives selected for SeaTube annotation scan: {len(selected_dives)}")

    print("\n--- Dive Step 2: Pull SeaTube annotations and apply filters ---")
    matched: List[Dict[str, Any]] = []

    for index, dive in enumerate(selected_dives, start=1):
        dive_id = int(dive["diveId"])
        try:
            dive_annotations = fetch_dive_annotations(client, dive_id)
        except Exception as exc:
            print(f"[WARN] dive {dive_id}: failed to fetch annotations ({exc})")
            continue

        for ann in dive_annotations:
            try:
                ts = annotation_time(ann)
            except Exception:
                continue

            if ts < start_dt or ts > end_dt:
                continue

            if not creator_matches(ann, args.user_id, args.created_by_name, args.created_by_email):
                continue

            if not modifier_matches(ann, args.modified_by_user_id, args.modified_by_name, args.modified_by_email):
                continue

            if not review_quality_matches(ann, args):
                continue

            if not taxonomy_matches(ann, taxonomy_code, args.taxonomy_id, taxon_ids):
                continue

            ann_id = int(ann.get("annotationId", 0))
            ann_start = ann.get("startDate") or ""

            matched.append(
                {
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
                    "contextualLink": build_contextual_link(600, dive_id, ann_id, ann_start),
                }
            )

        if index % 10 == 0 or index == len(selected_dives):
            print(f"Processed {index}/{len(selected_dives)} dives; matched annotations so far: {len(matched)}")

    print(f"Matched dive annotations after filters: {len(matched)}")

    return matched


def collect_stationary_matches(
    client: ONCApiClient,
    start_dt: datetime,
    end_dt: datetime,
    args: argparse.Namespace,
    taxonomy_code: Optional[str],
    taxon_ids: Set[int],
) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    print("\n--- Stationary Step 1: Discover fixed-camera locations ---")
    tree = fetch_fixed_camera_tree(client)
    all_locations = flatten_fixed_camera_locations(tree)

    if args.list_stationary_locations:
        print("Fixed camera locations:")
        for loc in all_locations:
            print(f"  id={loc['searchTreeNodeId']}: {loc['path']}")
        return [], {}

    node_filter = parse_int_set(args.search_tree_node_id)
    selected_locations = select_fixed_camera_locations(
        locations=all_locations,
        node_filter=node_filter,
        name_contains=args.location_name_contains,
        max_locations=args.max_stationary_locations,
    )

    # If stationary mode is requested with no explicit location filter, use all locations.
    if args.camera_mode in ("stationary", "both") and not node_filter and not args.location_name_contains:
        selected_locations = all_locations if args.max_stationary_locations is None else all_locations[: args.max_stationary_locations]

    print(f"Total fixed-camera locations in tree: {len(all_locations)}")
    print(f"Selected fixed-camera locations: {len(selected_locations)}")

    if not selected_locations:
        print("No stationary locations selected; skipping stationary search.")
        return [], {}

    print("\n--- Stationary Step 2: Build video index per location ---")
    media_by_device, location_by_device = build_stationary_video_index(
        client=client,
        selected_locations=selected_locations,
        resolution=args.resolution,
    )

    device_ids = sorted(media_by_device.keys())
    print(f"Stationary devices with video data: {len(device_ids)}")

    print("\n--- Stationary Step 3: Query AnnotationServiceV3 by device and apply filters ---")
    matched: List[Dict[str, Any]] = []
    detail_cache: Dict[int, Dict[str, Any]] = {}
    taxon_cache: Dict[Tuple[int, int], Optional[Dict[str, Any]]] = {}

    for idx, device_id in enumerate(device_ids, start=1):
        try:
            candidates = fetch_stationary_annotation_candidates_for_device(
                client=client,
                device_id=device_id,
                start_date=args.start_date,
                end_date=args.end_date,
                user_id=args.user_id,
                page_size=args.stationary_page_size,
            )
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
                    detail = fetch_annotation_detail(client, ann_id)
                except Exception as exc:
                    print(f"[WARN] annotation {ann_id}: detail fetch failed ({exc})")
                    continue
                detail_cache[ann_id] = detail

            ann_start = detail.get("startDate") or naive.get("startDate")
            ann_end = detail.get("endDate") or naive.get("endDate")
            if not ann_start:
                continue

            try:
                ts = parse_iso_utc(ann_start)
            except Exception:
                continue

            if ts < start_dt or ts > end_dt:
                continue

            created_by = normalize_user(detail.get("createdBy"))
            modified_by = normalize_user(detail.get("modifiedBy"))

            annotation_contents = detail.get("annotationContents") or []
            taxonomy = []
            for content in annotation_contents:
                t_id = content.get("taxonomyId")
                x_id = content.get("taxonId")
                if t_id in (None, 0) and x_id in (None, 0):
                    continue
                taxonomy.append(
                    build_taxonomy_entry(
                        client,
                        content,
                        taxon_cache,
                        resolve_name=not args.skip_taxon_name_resolution,
                    )
                )

            source = detail.get("annotationSource")
            annotation_source = source.get("annotationSource") if isinstance(source, dict) else source

            ann_record = {
                "cameraMode": "stationary",
                "annotationId": ann_id,
                "diveId": None,
                "diveName": None,
                "cruiseName": None,
                "stationarySearchTreeNodeId": loc.get("searchTreeNodeId"),
                "stationaryLocationName": loc.get("name"),
                "stationaryLocationPath": loc.get("path"),
                "startDate": ann_start,
                "endDate": ann_end,
                "comment": (naive.get("annotationSummary") or "").strip(),
                "annotationSource": annotation_source,
                "createdBy": created_by,
                "modifiedBy": modified_by,
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
                or build_contextual_link(1000, int(naive.get("resourceId", 0)), ann_id, ann_start),
            }

            if not creator_matches(ann_record, args.user_id, args.created_by_name, args.created_by_email):
                continue

            if not modifier_matches(
                ann_record,
                args.modified_by_user_id,
                args.modified_by_name,
                args.modified_by_email,
            ):
                continue

            if not review_quality_matches(ann_record, args):
                continue

            effective_taxonomy_id = args.taxonomy_id
            if effective_taxonomy_id is None and taxonomy_code:
                effective_taxonomy_id = TAXONOMY_CODE_TO_ID.get(taxonomy_code.lower())

            if not taxonomy_matches(ann_record, taxonomy_code, effective_taxonomy_id, taxon_ids):
                continue

            matched.append(ann_record)

        if idx % 5 == 0 or idx == len(device_ids):
            print(f"Processed {idx}/{len(device_ids)} devices; matched stationary annotations: {len(matched)}")

    print(f"Matched stationary annotations after filters: {len(matched)}")

    return matched, media_by_device


def map_dive_annotations_to_videos(client: ONCApiClient, annotations: Sequence[Dict[str, Any]], resolution: str) -> None:
    by_dive: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for ann in annotations:
        if ann.get("cameraMode") != "dive":
            continue
        dive_id = ann.get("diveId")
        if dive_id is None:
            continue
        by_dive[int(dive_id)].append(ann)

    for dive_id, anns in by_dive.items():
        try:
            video_meta = fetch_dive_video_metadata(client, dive_id, resolution)
        except Exception as exc:
            print(f"[WARN] dive {dive_id}: failed to fetch video metadata ({exc})")
            continue

        media_files = video_meta.get("mediaFiles", []) or []
        if not media_files:
            continue

        for ann in anns:
            ann_start = ann.get("startDate")
            if not ann_start:
                continue

            ann_dt = parse_iso_utc(ann_start)
            media_file = pick_media_file(media_files, ann.get("deviceId"))
            if not media_file:
                continue

            row = pick_data_file_row(media_file, ann_dt)
            if not row:
                ann["videoMappingStatus"] = "no_containing_data_file"
                continue

            ann.update(build_archive_file_info(media_file, row))
            ann["videoMappingStatus"] = "strict_containment"
            ann["resolution"] = resolution
            ann["videoDeviceCode"] = media_file.get("deviceCode")


def map_stationary_annotations_to_videos(
    annotations: Sequence[Dict[str, Any]],
    media_by_device: Dict[int, Dict[str, Any]],
    resolution: str,
) -> None:
    for ann in annotations:
        if ann.get("cameraMode") != "stationary":
            continue

        device_id = ann.get("deviceId")
        if device_id is None:
            continue

        media_file = media_by_device.get(int(device_id))
        if not media_file:
            continue

        ann_start = ann.get("startDate")
        if not ann_start:
            continue

        ann_dt = parse_iso_utc(ann_start)
        row = pick_data_file_row(media_file, ann_dt)
        if not row:
            ann["videoMappingStatus"] = "no_containing_data_file"
            continue

        ann.update(build_archive_file_info(media_file, row))
        ann["videoMappingStatus"] = "strict_containment"
        ann["resolution"] = resolution
        ann["videoDeviceCode"] = media_file.get("deviceCode")


def main() -> None:
    args = parse_args()

    taxonomy_code = args.taxonomy_code.strip() if args.taxonomy_code is not None else None
    if taxonomy_code == "":
        taxonomy_code = None

    ml_csv_file = args.ml_csv_file or os.path.join(args.output_dir, "annotations_ml.csv")
    ml_jsonl_file = args.ml_jsonl_file or os.path.join(args.output_dir, "annotations_ml.jsonl")

    taxon_ids = parse_int_set(args.taxon_id)

    start_dt = parse_iso_utc(args.start_date)
    end_dt = parse_iso_utc(args.end_date)

    if end_dt < start_dt:
        raise ValueError("--end-date must be after --start-date")

    os.makedirs(args.output_dir, exist_ok=True)

    client = ONCApiClient(token=args.token, timeout_seconds=args.timeout_seconds)

    # Allow users to list location IDs regardless of mode.
    if args.list_stationary_locations:
        tree = fetch_fixed_camera_tree(client)
        locations = flatten_fixed_camera_locations(tree)
        print("Fixed camera locations:")
        for loc in locations:
            print(f"  id={loc['searchTreeNodeId']}: {loc['path']}")
        return

    matched: List[Dict[str, Any]] = []

    if args.camera_mode in ("dive", "both"):
        dive_matches = collect_dive_matches(
            client=client,
            start_dt=start_dt,
            end_dt=end_dt,
            args=args,
            taxonomy_code=taxonomy_code,
            taxon_ids=taxon_ids,
        )
        matched.extend(dive_matches)

    stationary_media_by_device: Dict[int, Dict[str, Any]] = {}
    if args.camera_mode in ("stationary", "both"):
        stationary_matches, stationary_media_by_device = collect_stationary_matches(
            client=client,
            start_dt=start_dt,
            end_dt=end_dt,
            args=args,
            taxonomy_code=taxonomy_code,
            taxon_ids=taxon_ids,
        )
        matched.extend(stationary_matches)

    print(f"\nTotal matched annotations across selected modes: {len(matched)}")

    wanted_taxa = resolve_wanted(args.group, args.taxon_name)
    if wanted_taxa:
        cache_path = args.worms_cache or os.path.join(args.output_dir, ".worms_cache.json")
        resolver = TaxonResolver(cache_path, offline=args.offline_taxa)
        print(
            f"\n--- Filtering to {len(wanted_taxa)} ancestor taxa "
            f"({', '.join(sorted(wanted_taxa))}) ---"
        )
        before = len(matched)
        matched = [a for a in matched if resolver.annotation_matches(a, wanted_taxa)]
        resolver.save()
        if resolver.unresolved:
            print(f"[WARN] {len(resolver.unresolved)} taxa could not be resolved and were skipped")
        print(f"Kept {len(matched)} of {before} annotations")

    print("\n--- Step 3: Map matched annotations to archive MP4 filenames ---")
    map_dive_annotations_to_videos(client, matched, args.resolution)
    map_stationary_annotations_to_videos(matched, stationary_media_by_device, args.resolution)

    filenames: List[str] = []
    seen: Set[str] = set()
    for ann in matched:
        fn = ann.get("archiveFilename")
        if fn and fn not in seen:
            seen.add(fn)
            filenames.append(fn)

    print(f"Unique archive MP4 files referenced: {len(filenames)}")

    videos_dir = os.path.join(args.output_dir, "videos")
    downloaded_files: Set[str] = set()

    if args.download_videos:
        os.makedirs(videos_dir, exist_ok=True)

        to_download = filenames
        if args.max_downloads is not None:
            to_download = to_download[: args.max_downloads]

        print("\n--- Step 4: Download archive MP4 files ---")
        for i, filename in enumerate(to_download, start=1):
            out_path = os.path.join(videos_dir, filename)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                print(f"[{i}/{len(to_download)}] exists, skipping: {filename}")
                downloaded_files.add(filename)
                continue

            print(f"[{i}/{len(to_download)}] downloading: {filename}")
            try:
                client.download_archive_file(filename, out_path)
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    downloaded_files.add(filename)
            except Exception as exc:
                print(f"[WARN] failed download for {filename}: {exc}")
    else:
        print("\n--- Step 4: Download skipped ---")
        print("Pass --download-videos to fetch MP4 files")

    # Always annotate local path if file is present, including from previous runs.
    for ann in matched:
        filename = ann.get("archiveFilename")
        if not filename:
            ann["videoLocalPath"] = None
            ann["videoDownloaded"] = False
            continue

        local_path = os.path.join(videos_dir, filename)
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            ann["videoLocalPath"] = local_path
            ann["videoDownloaded"] = True
            downloaded_files.add(filename)
        else:
            ann["videoLocalPath"] = None
            ann["videoDownloaded"] = False

    ensure_parent_dir(args.metadata_file)
    with open(args.metadata_file, "w", encoding="utf-8") as f:
        json.dump(matched, f, indent=2)

    ml_rows = build_ml_rows(matched)
    write_ml_csv(ml_rows, ml_csv_file)
    write_ml_jsonl(ml_rows, ml_jsonl_file)

    print(f"Saved metadata JSON: {args.metadata_file}")
    print(f"Saved ML CSV: {ml_csv_file}")
    print(f"Saved ML JSONL: {ml_jsonl_file}")
    print(f"Downloaded MP4 files available locally: {len(downloaded_files)}")
    print("Done.")


if __name__ == "__main__":
    main()
