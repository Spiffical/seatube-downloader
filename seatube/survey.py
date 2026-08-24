"""Quick reconnaissance: which dives and locations have annotations at all.

Orders one bulk SeaTube annotation export (STEXPORT) from ONC for a date
range and summarizes it by dive/location and by annotator.  Much faster than
a full fetch for wide date ranges -- use it to decide where to point
``seatube fetch``.
"""

from __future__ import annotations

import csv
import os
import zipfile
from collections import defaultdict
from typing import Any, Dict, List, Optional


def run_survey(
    token: str,
    start_date: str,
    end_date: str,
    *,
    taxonomy_id: int = 1,
    location_code: Optional[str] = None,
    output_dir: str = "search_results",
) -> Dict[str, Any]:
    """Order and summarize a SeaTube annotation export.

    Returns {"scopes": [...], "annotators": [...]} where scopes are
    dive/location rows sorted by annotation count.  Ordering the export takes
    ONC a minute or two for wide ranges.
    """
    from onc.onc import ONC  # the ONC SDK handles the order/poll/download loop

    os.makedirs(output_dir, exist_ok=True)
    onc = ONC(token, outPath=output_dir)

    filters = {
        "dataProductCode": "STEXPORT",
        "extension": "csv",
        "taxonomyId": taxonomy_id,
        "dateFrom": start_date,
        "dateTo": end_date,
    }
    if location_code:
        filters["locationCode"] = location_code

    result = onc.orderDataProduct(filters)

    zip_path = None
    for row in result.get("downloadResults", []):
        file_name = row.get("file", "")
        if file_name.endswith(".zip"):
            zip_path = os.path.join(output_dir, file_name)
            break
    if not zip_path or not os.path.exists(zip_path):
        return {"scopes": [], "annotators": [], "export_zip": None}

    scopes: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"annotations": 0, "annotators": set()})
    annotators: Dict[str, int] = defaultdict(int)

    with zipfile.ZipFile(zip_path, "r") as z:
        for file_name in z.namelist():
            if not file_name.endswith(".csv"):
                continue
            with z.open(file_name) as f:
                content = f.read().decode("utf-8-sig").splitlines()
                for row in csv.DictReader(content):
                    # Column names vary slightly between STEXPORT releases.
                    loc = row.get("Location") or row.get("LocationCode") or "Unknown Location"
                    dive = row.get("Dive") or row.get("Dive ID") or "No Dive ID"
                    start = row.get("Start Date") or row.get("Observation Date") or "Unknown"
                    annotator = row.get("Creator") or row.get("User") or "Unknown"

                    key = f"{loc} | {dive} | around {start[:10]}"
                    scopes[key]["annotations"] += 1
                    scopes[key]["annotators"].add(annotator)
                    annotators[annotator] += 1

    scope_rows = [
        {"scope": key, "annotations": v["annotations"], "annotators": sorted(v["annotators"])}
        for key, v in scopes.items()
    ]
    scope_rows.sort(key=lambda r: -r["annotations"])
    annotator_rows = [
        {"annotator": name, "annotations": count}
        for name, count in sorted(annotators.items(), key=lambda kv: -kv[1])
    ]
    return {"scopes": scope_rows, "annotators": annotator_rows, "export_zip": zip_path}
