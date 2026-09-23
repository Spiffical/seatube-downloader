# Find organisms, frames, and clips with Python

This guide is for researchers using a notebook or a Python script. First install the package and configure an ONC token as described in the [README](../README.md). You only need ffmpeg when extracting media. The [notebook](../examples/research_walkthrough.ipynb) walks through a real dive, starting with ONC registration and token setup. It queries live metadata by default; media downloads are optional.

## 1. Choose your biological search

```python
from seatube import SeaTube, AnnotationSet, ReviewFilters

sea = SeaTube(data_dir="downloads")
sea.groups()          # all 44 search groups, descriptions, ancestors, aliases
sea.groups("coral")   # discover relevant definitions
```

A group is a rule for matching a taxonomic lineage. It is not an inventory of available video. For the exact biological scope, see the [organism catalog](organisms.md).

`"crabs"` includes true crabs and anomurans; `"true-crabs"` is narrower. Use a scientific name such as `"Chionoecetes tanneri"` when you need species-level annotations. That search cannot promote an observation labelled only `Brachyura` to species level. Multiple names mean **any** of those organisms, not necessarily co-occurrence in the same frame.

## 2. Find places and dates

Choose a small date range for the first fetch. Listing dives returns ONC IDs, names, dates, and other metadata:

```python
dives = sea.dives("2019-07-06T00:00:00Z", "2019-07-06T23:59:59Z")
for dive in dives:
    print(dive["diveId"], dive.get("referenceDiveId"), dive.get("dateFrom"))

locations = sea.locations()  # fixed-camera tree, not a list of organism occurrences
for location in locations[:5]:
    print(location["searchTreeNodeId"], location["path"])
```

If you need to scout a longer period, `run_survey()` orders ONC’s STEXPORT annotation product and summarizes its scopes and annotators:

```python
from seatube import run_survey

# Online metadata export; may take several minutes. No video is downloaded.
# survey = run_survey(sea.client.token, "2019-07-01T00:00:00Z", "2019-08-01T00:00:00Z")
# survey["scopes"][:10]
```

The survey’s scope strings help locate activity; get authoritative numeric IDs from `dives()` or `locations()`. A camera listed in the tree need not have observations in your date range.

## 3. Fetch annotations once

```python
annotations = sea.fetch(
    start_date="2019-07-06T00:00:00Z",
    end_date="2019-07-06T23:59:59Z",
    camera_mode="dive",  # also "stationary" or "both"
    resolution="L",      # mapped source video resolution; H/L/S
    save_to="downloads/annotations.json",
)
print(annotations.summary())
```

This fetches annotation and video **metadata**, not video bytes. It includes WoRMS-labelled observations by default. A default fetch is not restricted to one organism, so you can explore it repeatedly. For a smaller result, pass `organisms="sponges"`; that filters the returned observations and archive mapping work, but still scans the selected sources for annotations.

Use `dive_ids={...}` with IDs returned by `dives()`, or `node_ids={...}` for fixed cameras. `max_dives=3` or `max_stationary_locations=2` can bound an exploratory scan, but limits deliberately make it incomplete. Date-only strings mean **midnight UTC**, not the whole day; use explicit start and end timestamps when you want a day's observations. Query endpoints are inclusive in this package.

A request failure raises an exception rather than returning a silently partial fetch. A successful request with no containing video file remains in the dataset as an unmapped observation. Some endpoints used here support the SeaTube web application and can change independently of the public SDK.

## 4. Discover what was actually annotated

```python
annotations = AnnotationSet.load("downloads/annotations.json")
print(annotations.summary())

for row in sea.available_groups(annotations):
    print(row["group"], row["annotations"], row["mapped_annotations"], row["taxa"])

for taxon in annotations.taxon_summary():
    print(taxon.name, taxon.aphia_id, taxon.annotations)
```

`available_groups()` can look up uncached lineages at WoRMS. One annotation may contribute to several groups: a crab is also a crustacean. Counts represent annotated records, not numbers of animals. `mapped_annotations` tells you how many records have usable positions inside source video files.

For a notebook table, install the notebook extra and wrap the returned rows in `pandas.DataFrame(...)`. `taxon_summary()` and `annotator_summary()` return dataclasses; use `dataclasses.asdict()` before converting those to a table.

## 5. Select observations

```python
crabs = sea.search(annotations, "crabs")
organisms = sea.search(annotations, ["sponges", "sea-stars"])  # union / OR
species = sea.search(annotations, "Chionoecetes tanneri")

reviewed_crabs = sea.search(
    annotations,
    "crabs",
    min_depth_m=500,
    max_depth_m=2000,
    review=ReviewFilters(reviewed_only=True, min_total_reviews=1),
)

# Text search is separate: useful for inspecting the actual vocabulary.
label_matches = annotations.filter(taxon_contains="Chionoecetes")
# An AphiaID selects that exact recorded taxon, without expanding descendants.
exact_taxon = annotations.filter(aphia_ids=[106673])
```

Additional filters include `creator`, `creator_id`, `dive_contains`, `location_contains`, `camera_mode`, `start_date`, and `end_date`. Different filter fields combine with **AND**. Records with unknown depth are excluded when a depth bound is requested. Location text filters apply to fixed-camera name/path; use `dive_contains` for ROV dive names.

The results retain each entire source annotation, including co-labelled taxa. Matching a crab does not mean every returned taxon label is a crab. Review fields are useful quality signals, but “reviewed” does not establish identification accuracy. `require_cross_review` checks for different creator/modifier IDs; it is only a proxy, not a reviewer-history audit.

## 6. Plan and extract frames

```python
frames = crabs.frames(max_images=20, max_videos=2, max_per_taxon=10)
images = sea.image_downloader("outputs/crab_frames", keep_videos=True)
plan = images.plan(frames)  # structured records; may request Content-Length headers
print(images.describe_plan(frames))

# After inspecting the plan:
# image_rows = images.download(frames)
```

`max_per_taxon` is a strict cap on each recorded label, including co-labelled taxa; it does not enforce equal class sizes. File selection is deterministic, prefers files with more requested frames, and is not a representative random sample. For ecological comparisons, choose a sampling design rather than treating this download heuristic as one.

With `dedupe_seconds=1`, nearby annotations are bucketed together and the earliest actual annotation instant in the bucket is extracted. Other labels can refer to slightly different times; use the default `0` if that distinction matters. Records sharing exactly one file and instant always merge.

A frame costs a whole source archive download unless that file is already cached. Sizes may be unknown; `max_videos` is a file-count limit. `plan(check_sizes=False)` performs no network requests. `keep_videos=True` retains new source downloads; by default, newly downloaded source files are removed after successful extraction. Existing files supplied through `video_dir` are retained.

## 7. Plan and extract clips

```python
clips = crabs.clips(before_seconds=5, after_seconds=10, max_clips=5, max_videos=2)
video = sea.clip_downloader("outputs/crab_clips", video_dir="outputs/crab_frames/_videos")
print(video.describe_plan(clips))

# clip_rows = video.download(clips)
```

The same source cache can serve images and clips. Each excerpt covers the annotation instant plus the requested context, clamped to the containing archive. Overlapping/touching intervals in the same file merge, so a merged clip can exceed the nominal context length. Files are never stitched across boundaries or gaps; observations without known source duration are skipped with a warning.

Clips are silent H.264 MP4s, re-encoded for accurate cuts to source frame precision. An annotation does not establish that the organism is visible for the full clip duration. For browsing without downloads:

```python
moments = crabs.clip_index()                  # exact timestamp rows
windows = crabs.clip_index(window_seconds=60) # coarse buckets for browsing, not media excerpts
```

## 8. Export and retain provenance

```python
crabs.save("outputs/crab_annotations.json")
crabs.write_flat_csv("outputs/crab_annotations.csv")
crabs.write_flat_jsonl("outputs/crab_annotations.jsonl")

# Optional pandas analysis:
# import pandas as pd
# observations = pd.DataFrame(crabs.flatten())
sea.close()  # also available as: with SeaTube(...) as sea:
```

Flat tables have one row per annotation/taxon pair, including an empty-taxon row for unlabelled records. Keep the raw annotation JSON and taxonomy cache with your dataset. Media JSONL indexes contain the source records; CSV indexes provide compact, one-row-per-output summaries. Repeated selections into one output directory preserve earlier indexed outputs and merge labels for the same media filename.

A failed download or extraction raises an error. Temporary files are not published as completed media, and completed archive batches are indexed. Re-run the same selection after fixing the cause; completed nonempty outputs are reused. Existing user-supplied cache files are trusted, not checksum-verified. Do not run concurrent writers against the same output directory or taxonomy cache.

## If nothing matches

1. Inspect `annotations.summary()` and `taxon_summary()` before filtering. Confirm dates, camera mode, and selected dive/camera IDs.
2. Check whether the annotation resolution supports your question. A generic `Porifera` observation cannot satisfy a species-specific sponge search.
3. Read `IncompleteTaxonomyWarning` and `sea.resolver.unresolved`. Offline caches, missing AphiaIDs, unavailable lineages, or failed WoRMS requests can omit matches. Retry with a new online `SeaTube` session after the problem is resolved.
4. Compare matched versus mapped counts. Unmapped records cannot produce media; recording gaps never fall back to a nearby file or another identified camera.
5. Broaden the date/location scope deliberately. A zero count describes the returned annotation set, not the absence of organisms in the ocean.

To repeat analysis without network access, load a saved JSON file, construct `SeaTube(offline_taxa=True)` with its saved cache, and call only local search, summaries, exports, and planning with `check_sizes=False`. Discovery and `fetch()` remain online even when `offline_taxa=True`.
