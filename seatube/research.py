"""A small Python entry point for the full research workflow."""

from __future__ import annotations

from pathlib import Path

from .annotations import AnnotationSet
from .archive import parse_iso_utc
from .client import OncClient
from .clips import ClipDownloader
from .fetch import AnnotationFetcher, FetchFilters, cameras_in_range, dives_in_range, fixed_camera_locations
from .images import ImageDownloader
from .taxonomy import WormsResolver, organism_groups, search_terms


class SeaTube:
    """Fetch once, explore repeatedly, then explicitly download selected media.

    ``data_dir`` holds the taxonomy cache and default output directories. ONC
    credentials come from ``token`` or ONC_TOKEN in the environment/.env. No
    network requests or directories are created by construction.
    """

    def __init__(self, token=None, *, data_dir="downloads", offline_taxa=False,
                 client=None, resolver=None) -> None:
        self.data_dir = Path(data_dir)
        self.client = client if client is not None else (
            OncClient(token) if token is not None else OncClient.from_env())
        self.resolver = resolver if resolver is not None else WormsResolver(
            str(self.data_dir / ".worms_cache.json"), offline=offline_taxa)

    groups = staticmethod(organism_groups)

    def dives(self, start_date: str, end_date: str):
        """Discover ROV dive IDs and dates through ONC (metadata only)."""
        start, end = parse_iso_utc(start_date), parse_iso_utc(end_date)
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        return dives_in_range(self.client, start, end)

    def cameras(self, start_date: str, end_date: str, *, location_contains=None,
                include_empty=False):
        """Discover fixed-camera sites annotated in a date range (metadata only).

        The fixed-camera counterpart of ``dives()``: pass a row's
        ``searchTreeNodeId`` to ``fetch(node_ids={...})``. Rows count all
        annotations and the WoRMS-labelled ones a default fetch keeps.
        """
        return cameras_in_range(self.client, parse_iso_utc(start_date), parse_iso_utc(end_date),
                                location_contains=location_contains, include_empty=include_empty)

    def locations(self):
        """List every fixed-camera site in ONC's tree, annotated or not."""
        return fixed_camera_locations(self.client)

    def fetch(self, start_date: str, end_date: str, *, organisms=(), save_to=None, **filters):
        """Fetch annotation metadata and map it to video; never downloads video.

        Omit ``organisms`` to inventory all WoRMS-labelled observations in the
        selected sources. Extra keywords are FetchFilters fields: pass
        ``dive_ids`` for ROV dives or ``node_ids`` for fixed cameras, and the
        camera mode follows; with neither, every dive in the range is scanned.
        """
        if organisms:
            if "groups" in filters or "taxon_names" in filters:
                raise ValueError("Use organisms or groups/taxon_names, not both")
            groups, taxa = search_terms(organisms)
            filters.update(groups=groups, taxon_names=taxa)
        self.client.require_token()
        result = AnnotationFetcher(self.client, self.resolver).fetch(
            FetchFilters(start_date, end_date, **filters))
        if save_to is not None:
            result.save(save_to)
        return result

    def search(self, annotations: AnnotationSet, organisms, **filters):
        """Search a local annotation set using this workspace's taxonomy cache."""
        return annotations.search(organisms, resolver=self.resolver, **filters)

    def available_groups(self, annotations: AnnotationSet, *, include_empty=False):
        """Count groups actually annotated in this dataset, with mapping counts."""
        return annotations.group_summary(self.resolver, include_empty=include_empty)

    def image_downloader(self, output_dir=None, **options):
        return ImageDownloader(self.client, output_dir or str(self.data_dir / "images"),
                               resolver=self.resolver, **options)

    def clip_downloader(self, output_dir=None, **options):
        return ClipDownloader(self.client, output_dir or str(self.data_dir / "clips"),
                              resolver=self.resolver, **options)

    def close(self):
        self.resolver.save()
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
