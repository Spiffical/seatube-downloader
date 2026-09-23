"""Explore Ocean Networks Canada's SeaTube annotations and download exactly
the video frames, clips, or metadata you need.

The pieces:

- :class:`OncClient` -- every ONC endpoint the package talks to
- :class:`AnnotationFetcher` / :class:`FetchFilters` -- the online query
- :class:`AnnotationSet` -- offline filtering, summaries, clip lists, exports
- :class:`WormsResolver` / ``TAXON_GROUPS`` -- broad-group lineage matching
- :class:`ImageDownloader` -- labelled stills for as few bytes as possible
"""

from .annotations import Annotation, AnnotationSet, ReviewFilters, Taxon
from .client import OncClient, OncError
from .clips import Clip, ClipDownloader, build_clips, select_clips
from .fetch import AnnotationFetcher, FetchFilters, fixed_camera_locations
from .images import Frame, ImageDownloader, build_frames, select_frames
from .taxonomy import TAXON_GROUPS, WormsResolver, organism_groups, IncompleteTaxonomyWarning
from .research import SeaTube
from .survey import run_survey

__version__ = "0.3.0"

__all__ = [
    "Annotation",
    "AnnotationFetcher",
    "AnnotationSet",
    "Clip",
    "ClipDownloader",
    "FetchFilters",
    "Frame",
    "ImageDownloader",
    "OncClient",
    "OncError",
    "ReviewFilters",
    "SeaTube",
    "TAXON_GROUPS",
    "Taxon",
    "WormsResolver",
    "IncompleteTaxonomyWarning",
    "build_clips",
    "build_frames",
    "fixed_camera_locations",
    "organism_groups",
    "run_survey",
    "select_clips",
    "select_frames",
    "__version__",
]
