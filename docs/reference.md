# Reference

Commands, flags, the data model, and the two facts that shape the design.

## The two facts that shape everything

**1. ONC's archive endpoint serves whole files only.** It ignores HTTP Range
headers (verified: a `Range: bytes=0-999` request returns 200 with the full
body), and there is no still-frame data product for video. A single frame
therefore costs one whole archive file — typically 70–200 MB for five
minutes of video. Every download-touching command is built around minimizing
file count, and `seatube images --dry-run` always tells you the byte cost
before you commit.

**2. The position inside a file comes from timestamps, not from
`clipOffsetSeconds`.** ONC's `clipOffsetSeconds` locates a file within the
device's *media series*; the seek offset inside the file is
`annotation.startDate − archiveClipStartDate`. Confusing the two produces
plausible, confidently wrong frames. Relatedly, an annotation whose timestamp
falls in a recording gap maps to **no** file (`videoMappingStatus:
no_containing_data_file`) rather than the nearest one — the old
nearest-row behaviour silently produced false clips across gaps.

## Commands

| command | needs ONC? | what it does |
|---|---|---|
| `seatube survey` | yes | which dives/locations have annotations in a date range |
| `seatube fetch` | yes | pull filtered annotations into `downloads/annotations.json` |
| `seatube annotators` | no | leaderboard: who annotated, how much, when, what |
| `seatube taxa` | no | counts by taxon; `--show-groups` labels broad groups |
| `seatube clips` | no | video files + timestamps; `--window-seconds` merges dense stretches |
| `seatube images` | download only | extract labelled stills + index |
| `seatube videos` | yes | download whole archive files |
| `seatube groups` | no | the 44-group vocabulary |
| `seatube locations` | yes | fixed-camera site ids for `--search-tree-node-id` |

Every command accepts `--limit N` for table length (0 = all) and the offline
commands accept `--csv FILE` for the full table.

## Filters

### Offline filters (annotators / taxa / clips / images / videos)

These slice an already-fetched file — no ONC calls:

| flag | matches |
|---|---|
| `--group NAME` (repeatable) | broad taxon group via WoRMS lineage (`seatube groups`) |
| `--taxon-name TAXON` (repeatable) | anything at or below this WoRMS taxon, any rank |
| `--taxon-contains TEXT` | substring of the taxon label |
| `--creator TEXT` / `--creator-id N` | annotator name substring / exact ONC user id |
| `--reviewed-only` | annotation appears reviewed |
| `--min-total-reviews N` | at least N reviews |
| `--require-comment` | has a free-text comment |
| `--dive-name-contains TEXT` | dive name |
| `--location-contains TEXT` | fixed-camera site name/path |
| `--camera-mode dive\|stationary` | one source only |

### Fetch-time filters (`seatube fetch`)

All of the above concepts plus reviewer and review-quality gates:

| flag | effect |
|---|---|
| `--camera-mode dive\|stationary\|both` | which sources to search |
| `--taxonomy-code WoRMS` (default) | annotation taxonomy; empty string disables |
| `--taxon-id a,b` | ONC-internal taxon ids (not AphiaIDs) |
| `--creator-email`, `--modifier`, `--modifier-id`, `--modifier-email` | people filters |
| `--min-positive-reviews N`, `--min-positive-review-rate 0..1` | review quality |
| `--require-cross-review` | reviewer differs from creator |
| `--dive-id a,b` | specific dives |
| `--search-tree-node-id a,b`, `--location-name-contains` | specific fixed cameras |
| `--max-stationary-locations N`, `--max-dives N` | scan caps |
| `--resolution H\|L\|S` | which video resolution annotations map onto (default L) |
| `--skip-taxon-name-resolution` | stationary only: keep ONC-internal ids, no name lookups |
| `--flat-exports` | also write one-row-per-taxon CSV/JSONL |

## Broad taxon groups

`--group` accepts everyday words — `crabs`, `sponges`, `fish`, `sea-stars`,
`octopus-and-squid`, 44 in all, plus aliases (`crab`, `starfish`, `squid`,
`kelp`...). Each group is defined by ancestor taxa (crabs = Brachyura +
Anomura), and an annotation matches when any of its taxa sits **at or below**
an ancestor in the WoRMS classification. Lineages are fetched from
marinespecies.org once per distinct taxon and cached
(`.worms_cache.json` beside the annotations file); `--offline-taxa` forbids
network and reports what it couldn't check.

The AphiaID comes from the annotation's `referenceId` or its
`marinespecies.org` URL — never from `taxonId`, which is an ONC-internal id
that happens to look similar.

## Image economics

`seatube images` plans before it downloads:

1. annotations collapse into frames (same file + same instant;
   `--dedupe-seconds` widens the merge window),
2. frames group into the archive files that contain them,
3. files are visited **richest-first**, so `--max-images 4` is satisfied by
   one download when one file holds four wanted frames.

Caps: `--max-images` (stop after N stills), `--max-videos` (hard byte
budget: never fetch more than N files), `--max-per-taxon` (balanced sets).
Each archive file is deleted right after its frames are extracted unless
`--keep-videos`. Re-runs skip existing images entirely.

Output format: `--image-format jpg|png`, `--jpeg-quality N`
(ffmpeg `-q:v`, 2 ≈ near-lossless).

## Data model

`seatube fetch` writes `downloads/annotations.json`: a list of records, one
per annotation. The interesting fields:

| field | meaning |
|---|---|
| `annotationId`, `startDate`, `endDate`, `comment` | the observation |
| `taxonomy[]` | taxa: `displayText`, `taxonId` (ONC-internal), `referenceId` (WoRMS AphiaID), `taxonUrl`, attributes (e.g. Count) |
| `createdBy` / `modifiedBy` | annotator / reviewer (`userId`, name, email) |
| `toBeReviewed`, `numPositiveReviews`, `numTotalReviews` | review state |
| `cameraMode`, `diveId`/`diveName`/`cruiseName` or `stationary*` | provenance |
| `lat`, `lon`, `depth`, `heading` | position |
| `archiveFilename`, `archiveClipStartDate`, `clipDurationSeconds` | the video file containing the instant |
| `videoMappingStatus` | `strict_containment` or `no_containing_data_file` |
| `contextualLink` | opens the exact moment in the SeaTube player |

### Archive-file mapping

For each annotation, ONC's video metadata for the dive/device is searched
for the one `dataFiles` row whose `[start, end)` interval contains the
annotation timestamp (millisecond corrections included). No containing row →
no mapping, explicitly. The archive filename is reconstructed as
`{deviceCode}_{clipStartUTC}{postfix}` exactly as ONC names it.

### Image index

`seatube images` writes `images_index.csv` (one row per image) and
`images_index.jsonl` (same rows plus the full annotation records):
`image_file, frame_utc, archive_filename, offset_seconds, taxa,
worms_aphia_ids, groups, annotation_ids, annotation_count, camera_mode,
dive_name, location, lat, lon, depth_m, creators, seatube_link`.

### Flat exports

`seatube fetch --flat-exports` writes `annotations_flat.csv` / `.jsonl` with
one row per (annotation, taxon) pair — 54 columns covering everything above,
ready for pandas.

## Known limits

- Annotations are whole-frame labels: no bounding boxes, and unannotated
  organisms may appear in frame.
- The animal is present at the annotation instant but may be off-centre or
  partly out of view as the ROV moves; `--keep-videos` lets you re-pick
  frames by hand.
- `resolution` L is a good default; H files are much larger, S much smaller.
