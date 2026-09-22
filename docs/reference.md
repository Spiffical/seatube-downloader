# Optional CLI and data reference

Start with the [Python guide](guide.md) for research workflows. The CLI remains available as `seatube ...` or `python -m seatube ...`. Run a command with `--help` for all its options.

## Commands

| Command | Purpose | Network |
|---|---|---|
| `seatube survey` | Summarize a bulk annotation export by dive/site and annotator | ONC export |
| `seatube fetch` | Fetch annotations and map them to archive files | ONC, and WoRMS with lineage filters |
| `seatube dives` | Find ROV dive IDs in a date range | ONC |
| `seatube locations` | Find fixed-camera node IDs | ONC |
| `seatube groups` | Display the 44 group definitions | None |
| `seatube annotators` | Summarize annotators | Local, plus WoRMS if filtering by lineage |
| `seatube taxa` | Count taxon labels; `--show-groups` adds memberships | Local; WoRMS for lineage filters/memberships |
| `seatube clips` | List timestamps/player links; `--window-seconds` buckets observations | Local; WoRMS for lineage filters |
| `seatube images` | Extract JPG/PNG frames | ONC archive/size requests; optional WoRMS |
| `seatube extract-clips` | Extract short MP4 excerpts | ONC archive/size requests; optional WoRMS |
| `seatube videos` | Download whole archive files | ONC; optional WoRMS |

Examples:

```bash
seatube fetch --start-date 2019-07-06T00:00:00Z --end-date 2019-07-06T23:59:59Z
seatube taxa --group crabs --csv crab_taxa.csv
seatube clips --group crabs --window-seconds 60 --csv crab_moments.csv
seatube images --group crabs --max-images 20 --max-videos 2 --dry-run
seatube extract-clips --group crabs --before-seconds 5 --after-seconds 10 --max-clips 5 --dry-run
```

Omit `--dry-run` to extract media. This flag plans and optionally makes size requests, but does not download source video. The `clips` listing command is retained for compatibility; `extract-clips` writes actual video excerpts.

## Shared filters

Local commands read `downloads/annotations.json` by default; override with `--annotations FILE`. They accept `--group` and `--taxon-name` (repeatable), `--taxon-contains`, `--creator`, `--creator-id`, `--reviewed-only`, `--min-total-reviews`, `--require-comment`, `--dive-name-contains`, `--location-contains`, and `--camera-mode dive|stationary`.

`--worms-cache FILE` overrides the cache. `--offline-taxa` prevents WoRMS requests and warns about incomplete classification. It does not disable ONC downloads for media commands. A missing match is not proof of absence.

Table commands use `--limit N` (`0` = all); annotators, taxa and clip listings accept `--csv FILE`. Media limits are separate: `--max-images`, `--max-clips`, and `--max-videos`. `--max-per-taxon` is a strict cap for frames, including co-labelled taxa. `--max-videos` caps source-file count, not bytes.

Fetch options include `--camera-mode dive|stationary|both`, `--dive-id` (comma-separated), `--search-tree-node-id` (comma-separated), `--location-name-contains`, `--max-dives`, `--max-stationary-locations`, `--resolution H|L|S`, `--taxonomy-code`, `--taxonomy-id`, `--taxon-id` (ONC IDs, not AphiaIDs), people/review filters, `--flat-exports`, and `--output-dir`. Check `seatube fetch --help` for exact flags.

Credentials come from `ONC_TOKEN` in the environment or `.env`. `--token` is also supported, though putting tokens in shell history is best avoided.

## Annotation JSON

`AnnotationSet.save()` and `seatube fetch` write a JSON list, one dictionary per observation. It remains compatible with earlier annotation exports.

| Field | Meaning |
|---|---|
| `annotationId`, `startDate`, `endDate`, `comment` | Observation identity, time, and text |
| `taxonomy[]` | Recorded taxonomic labels and attributes |
| `taxonomy[].taxonId` | ONC-internal taxon identifier |
| `taxonomy[].referenceId`, `taxonUrl` | External WoRMS AphiaID / reference URL, when available |
| `createdBy`, `modifiedBy` | People metadata; may include email addresses |
| `toBeReviewed`, `numPositiveReviews`, `numTotalReviews` | Review signals, not independent validation |
| `cameraMode`, `diveId`, `diveName`, `cruiseName`, `stationary*` | Source/place metadata |
| `lat`, `lon`, `depth`, `heading` | Position/orientation when provided; depth in metres |
| `archiveFilename`, `archiveClipStartDate`, `clipDurationSeconds` | Containing archive file and bounds |
| `videoMappingStatus` | `strict_containment`, `no_containing_data_file`, `no_media_for_device`, or `missing_timestamp`; older records may lack a status |
| `contextualLink` | Source moment in the SeaTube player |

Taxonomy lineages are separate in `.worms_cache.json`, keyed by WoRMS AphiaID. ONC IDs must never be used as AphiaIDs. Keep caches with the analysis for reproducibility. Check raw records for personal metadata before sharing derived datasets publicly.

## Archive mapping and media indexes

Video metadata describes intervals `[start, end)`; timestamps exactly at an interval’s end belong to the next interval, if any. Millisecond corrections are included. No containing interval means no media mapping. When a device ID is supplied, a different camera is never used as a substitute.

A seek position is `annotation start - archive file start`. `clipOffsetSeconds` is the archive’s position in a larger media series and must not be used as a seek position.

Images produce `images_index.csv` and `images_index.jsonl`. Columns include `image_file`, `frame_utc`, `archive_filename`, `offset_seconds`, `taxa`, `worms_aphia_ids`, `groups`, `annotation_ids`, `annotation_count`, `camera_mode`, `dive_name`, `location`, `lat`, `lon`, `depth_m`, `creators`, and `seatube_link`.

Clips produce `clips_index.csv` and `clips_index.jsonl`. They replace image file/time with `clip_file` and `clip_start_utc`, and add `end_seconds` (relative to the archive) and `duration_seconds`. Labels apply to observations within the excerpt, not every frame of it.

JSONL rows also contain the full `annotations` list. Multi-value CSV fields are semicolon-separated. Position and the quick player link summarize the first associated annotation; inspect JSONL for all observations and their individual metadata. Repeated calls preserve existing indexed media and merge provenance for the same output filename.

Flat annotation exports are a different format: one row per annotation/taxon pair, with person, location, review, taxonomy and video fields. The complete column list is `seatube.annotations.FLAT_EXPORT_COLUMNS`.

## Operational limits

The download endpoint is treated as a whole-file service. This package does not rely on HTTP range requests. File sizes may be unknown; plans use estimates and cannot guarantee a total byte budget. Cache reuse trusts existing nonempty files; remove damaged legacy cache files before retrying.

Some metadata endpoints are used by the SeaTube web application rather than documented as stable public APIs. Endpoint failures raise errors during fetches; empty results from successful requests still require checking your query scope. The offline test suite cannot guarantee live service availability.
