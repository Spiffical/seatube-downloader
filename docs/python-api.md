# Python API

The Python API is the primary interface. The CLI calls the same library. See the [guide](guide.md) for a complete workflow and the [organism catalog](organisms.md) for biological scope.

## `SeaTube`: the research workspace

```python
from seatube import SeaTube, AnnotationSet

sea = SeaTube(token=None, data_dir="downloads", offline_taxa=False)
```

`token=None` reads `ONC_TOKEN` from the environment or `.env`. Construction makes no network requests. `data_dir` sets the taxonomy cache location and default media output directories; explicit `save_to` and output paths are relative to your working directory. Supply `client=` or `resolver=` for custom clients, caches, or testing. Use a context manager or `close()` to close the HTTP session and save the cache.

| Method | Returns | Network / side effects |
|---|---|---|
| `groups(query="")` | Group dictionaries: `group`, `description`, `ancestors`, `aliases` | None |
| `dives(start_date, end_date)` | ONC dive dictionaries, including `diveId` | ONC metadata |
| `locations()` | Fixed-camera dictionaries, including `searchTreeNodeId` | ONC metadata |
| `fetch(start_date, end_date, organisms=(), save_to=None, **filters)` | `AnnotationSet` | ONC metadata; WoRMS for organism filters; optional JSON save |
| `search(annotations, organisms, **filters)` | A filtered `AnnotationSet` | WoRMS for uncached lineages |
| `available_groups(annotations, include_empty=False)` | Count dictionaries, sorted by count | WoRMS for uncached lineages |
| `image_downloader(output_dir=None, **options)` | `ImageDownloader` | None until methods are called |
| `clip_downloader(output_dir=None, **options)` | `ClipDownloader` | None until methods are called |

`organisms` accepts a string or a list of strings. Known groups and aliases select the catalog definition; other names are treated as scientific names. There is no automatic spelling correction, global occurrence index, or arbitrary common-name translation. The query matches any supplied organism. Extra filter fields apply together.

`organism_groups(query="")` is also exported as a standalone, network-free catalog function.

## `AnnotationSet`: local observations

Load with `AnnotationSet.load(path)` or construct from raw dictionaries with `AnnotationSet(records)`. It supports iteration, indexing, slicing, `len()`, `.records`, and `.save(path)`.

| Method | Purpose |
|---|---|
| `summary()` | Counts of records, mapped/unmapped observations, archives, labels, places, and dates; no network |
| `search(organisms, resolver=None, **filters)` | Group/alias/scientific-name search |
| `filter(**filters)` | Explicit filtering; see below |
| `group_summary(resolver=None, include_empty=False)` | Per-group `annotations`, `mapped_annotations`, `archive_files`, `taxa`, and catalog definition |
| `taxon_summary(resolver=None)` | `TaxonStats` dataclasses; distinct annotation count per recorded label |
| `annotator_summary()` | `AnnotatorStats` dataclasses; attribution and observation summaries |
| `frames(dedupe_seconds=0, max_images=None, max_videos=None, max_per_taxon=None)` | Planned `Frame` objects, no network |
| `clips(before_seconds=5, after_seconds=5, max_clips=None, max_videos=None)` | Planned `Clip` objects, no network |
| `clip_index(window_seconds=0)` | Timestamp/player-link dictionaries; does not extract media |
| `flatten()` | Flat dictionaries, one per annotation/taxon pair |
| `write_flat_csv(path)`, `write_flat_jsonl(path)` | Tabular export |

`filter()` accepts:

- **Taxonomy:** `groups`, `taxa` (ancestor/scientific names), `taxon_contains` (label substring), `aphia_ids` (exact external IDs, no descendant expansion), `resolver`.
- **People:** `creator`, `creator_id`, `creator_email`, `modifier`, `modifier_id`, `modifier_email`. Names/emails use substring matching; IDs are exact.
- **Place/time:** `dive_contains`, `location_contains` (fixed-camera name/path), `camera_mode`, `start_date`, `end_date`, `min_depth_m`, `max_depth_m`.
- **Quality:** `review=ReviewFilters(...)`, `require_comment`.

Within `groups` and `taxa`, names form a union. Other fields combine with AND; matching retains whole annotations, including their other taxa. Date and depth bounds are inclusive; missing measurements fail an active bound. Naive dates/timestamps are interpreted as UTC.

`ReviewFilters` accepts `reviewed_only=False`, `min_total_reviews=None`, `min_positive_reviews=None`, `min_positive_review_rate=None` (0–1), and `require_cross_review=False`. The last field compares creator and modifier IDs and is only a review proxy.

## Fetch configuration and lower-level access

```python
from seatube import AnnotationFetcher, FetchFilters, OncClient, WormsResolver

client = OncClient.from_env()
resolver = WormsResolver("downloads/.worms_cache.json")
fetcher = AnnotationFetcher(client, resolver=resolver)
filters = FetchFilters(
    start_date="2019-07-06T00:00:00Z",
    end_date="2019-07-06T23:59:59Z",
    groups=("crabs",),
)
# annotations = fetcher.fetch(filters)  # online
```

`SeaTube.fetch(**filters)` forwards these `FetchFilters` fields:

| Field | Default / interpretation |
|---|---|
| `camera_mode` | `"dive"`; alternatives `"stationary"`, `"both"` |
| `taxonomy_code`, `taxonomy_id` | `"WoRMS"`, `None`; `taxonomy_code=None` disables the code constraint |
| `taxon_ids` | Empty set; ONC-internal taxon IDs, **not** WoRMS AphiaIDs |
| `groups`, `taxon_names` | Empty sequences; alternative to `organisms=` |
| `creator_id`, `creator_name`, `creator_email` | No people constraint |
| `modifier_id`, `modifier_name`, `modifier_email` | No modifier constraint |
| `review` | `ReviewFilters()` |
| `dive_ids`, `node_ids` | Empty sets; discover using `dives()` / `locations()` |
| `location_contains` | No fixed-camera name/path constraint |
| `max_dives`, `max_stationary_locations` | No scan caps; caps select the first sources in sorted order |
| `resolution` | `"L"`; `"H"` / `"S"` also supported |
| `page_size` | `250`; positive integer |
| `resolve_taxon_names` | `True`; resolves fixed-camera taxonomy records through ONC |

Fetch configuration is validated before the scan. Failed annotation/taxonomy/video-metadata requests raise exceptions. Taxonomy classification failures at WoRMS produce `IncompleteTaxonomyWarning` and conservatively omit unconfirmed matches; inspect `resolver.unresolved`.

`OncClient(token=None, timeout_seconds=45)` does not read `.env` by itself; use `from_env()` for that behavior. Methods include `list_dives()`, `dive_annotations(id)`, `dive_video_metadata(id, resolution)`, `fixed_camera_tree()`, `stationary_video_metadata(node_id, resolution)`, `annotation_detail(id)`, `taxon_detail(taxonomy_id, taxon_id)`, `archive_file_size(filename)`, and `download_archive_file(filename, output_path)`. `OncError` reports transport/API failures without credential-bearing request URLs. Transient GET/HEAD failures receive bounded retries; failed streamed downloads must be retried by the caller.

## Taxonomy resolver

`WormsResolver(cache_path=None, offline=False, timeout_seconds=45)` resolves classifications by WoRMS AphiaID and caches successful nonempty lineages. `lineage(id)` returns names, `groups_for(taxon_dict)` returns groups, and `save()` persists the cache. A `fetcher=` callback supports offline tests.

`offline=True` forbids WoRMS requests. Cached classifications and exact label matches can still work. Missing external IDs, empty responses, or lookup errors mean classification is unresolved. Failed IDs are attempted once per resolver session (with transport retries inside the lookup); create a new resolver to retry. Caches do not automatically expire; retain the one used for an analysis and refresh it deliberately when updating taxonomy.

## Frames, clips, and downloads

`build_frames(annotations, dedupe_seconds=0)` and `select_frames(frames, max_images=None, max_videos=None, max_per_taxon=None)` are the lower-level frame planners. `build_clips(...)` and `select_clips(...)` perform clip planning and limiting. Limits must be nonnegative integers or `None`; zero selects nothing.

A `Frame` carries `archive_filename`, `offset_seconds`, `frame_utc`, `annotations`, and `image_name`. A `Clip` also carries `end_seconds` and `duration_seconds`; its `frame_utc` is the excerpt start. Both preserve the original annotation instants in `annotations`.

```python
from seatube import ImageDownloader, ClipDownloader

images = ImageDownloader(client, "outputs/images", image_format="jpg", jpeg_quality=2,
                         video_dir=None, keep_videos=False, resolver=resolver)
clips = ClipDownloader(client, "outputs/clips", video_dir=None,
                       keep_videos=False, resolver=resolver)
```

Both downloaders support `plan(items, check_sizes=True)`, `describe_plan(items)`, and `download(items)`:

- A plan row has `archive_filename`, `outputs`, `pending_outputs`, `needs_download`, and `download_bytes`. Unknown bytes are `None`; cached work costs `0` additional bytes. `check_sizes=False` forbids HEAD lookups.
- `download()` returns rows for this call; the on-disk index also retains earlier outputs in that directory. It raises on download/extraction failures, reuses nonempty outputs/cached archives, and publishes new media atomically. Use one writer per output/cache directory.
- `ImageDownloader` produces JPG or PNG. `jpeg_quality` is ffmpeg’s 1–31 quality scale, lower is higher quality.
- `ClipDownloader` produces silent H.264 MP4. Cuts are re-encoded and limited to source frame precision.

Library progress uses the `seatube` logger. To see it in a script or notebook:

```python
import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
```
