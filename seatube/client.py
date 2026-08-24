"""HTTP client for Ocean Networks Canada's public data APIs.

Every ONC endpoint the package talks to lives here, so the rest of the code
can be read without knowing ONC's URL layout.  All methods return parsed
payloads, not raw responses.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://data.oceannetworks.ca"


class OncClient:
    """Authenticated access to data.oceannetworks.ca.

    Get a token by registering at https://data.oceannetworks.ca and copying
    the Web Services API token from your profile page.
    """

    def __init__(self, token: Optional[str] = None, timeout_seconds: int = 45) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    @classmethod
    def from_env(cls, timeout_seconds: int = 45) -> "OncClient":
        """Build a client from ONC_TOKEN in the environment or a .env file."""
        from dotenv import load_dotenv

        load_dotenv()
        return cls(token=os.getenv("ONC_TOKEN"), timeout_seconds=timeout_seconds)

    def require_token(self) -> None:
        if not self.token:
            raise RuntimeError(
                "An ONC token is required. Pass --token, or set ONC_TOKEN in .env "
                "(register at https://data.oceannetworks.ca to get one)."
            )

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # dives (ROV)
    # ------------------------------------------------------------------

    def list_dives(self) -> List[Dict[str, Any]]:
        data = self.get_json("/DiveListingService", {"operation": 11})
        return data.get("payload", {}).get("dives", []) or []

    def dive_annotations(self, dive_id: int) -> List[Dict[str, Any]]:
        data = self.get_json(
            "/seatubeV3/annotations",
            {"operation": 1, "diveIds": int(dive_id), "filter": "{}"},
        )
        return data.get("payload", {}).get("annotations", []) or []

    def dive_video_metadata(self, dive_id: int, resolution: str) -> Dict[str, Any]:
        data = self.get_json(
            "/seatube/videos",
            {"operation": 1, "diveId": int(dive_id), "resolution": resolution},
        )
        return data.get("payload", {})

    # ------------------------------------------------------------------
    # fixed cameras
    # ------------------------------------------------------------------

    def fixed_camera_tree(self) -> Dict[str, Any]:
        data = self.get_json(
            "/SearchTreeService",
            {"operation": 15, "deviceCategoryId": 14, "label": "Fixed Cameras"},
        )
        return data.get("payload", {})

    def stationary_video_metadata(self, search_tree_node_id: int, resolution: str) -> Dict[str, Any]:
        data = self.get_json(
            "/seatube/videos",
            {"operation": 1, "searchTreeNodeId": int(search_tree_node_id), "resolution": resolution},
        )
        return data.get("payload", {})

    def stationary_annotation_page(
        self,
        device_id: int,
        start_date: str,
        end_date: str,
        page_num: int,
        page_size: int,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "operation": 1,
            "resourceTypeId": 1000,
            "annotationSources": 6,
            "resourceId": int(device_id),
            "fromDate": start_date,
            "toDate": end_date,
            "pageSize": page_size,
            "pageNum": page_num,
        }
        if user_id is not None:
            params["userId"] = user_id
        return self.get_json("/AnnotationServiceV3", params).get("payload", {})

    # ------------------------------------------------------------------
    # annotations and taxonomy
    # ------------------------------------------------------------------

    def annotation_detail(self, annotation_id: int) -> Dict[str, Any]:
        data = self.get_json("/AnnotationServiceV3", {"annotationId": int(annotation_id)})
        return data.get("payload", {})

    def taxon_detail(self, taxonomy_id: int, taxon_id: int) -> Dict[str, Any]:
        """Resolve an ONC-internal taxon ID to its canonical taxonomy record.

        ``taxonId`` in AnnotationServiceV3 is not a WoRMS AphiaID.  The
        SeaTube UI resolves it through this endpoint, whose response also
        carries the external WoRMS reference for imported taxonomies.
        """
        return self.get_json(
            f"/internal/taxonomies/{int(taxonomy_id)}/taxons/{int(taxon_id)}",
            {},
        )

    # ------------------------------------------------------------------
    # archive video files
    # ------------------------------------------------------------------

    def download_archive_file(self, filename: str, output_path: str) -> None:
        """Fetch one whole archive file.

        ONC ignores HTTP Range headers on this endpoint (verified: a Range
        request returns 200 with the full body), so partial downloads are
        impossible -- callers must budget for whole files.
        """
        self.require_token()
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

    def archive_file_size(self, filename: str) -> Optional[int]:
        """Size in bytes via a HEAD request, or None if unavailable."""
        try:
            resp = self.session.head(
                f"{BASE_URL}/api/archivefile/download",
                params={"filename": filename, "token": self.token},
                timeout=self.timeout_seconds,
                allow_redirects=True,
            )
            resp.raise_for_status()
            length = resp.headers.get("Content-Length")
            return int(length) if length else None
        except Exception:
            return None
