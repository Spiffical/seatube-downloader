# Changelog

## Unreleased

- Make fixed (stationary) cameras as easy to use as ROV dives. `sea.cameras(start, end)` and `seatube cameras` list the fixed cameras annotated in a date range, with node IDs, ONC location codes, and WoRMS-labelled counts. `fetch(node_ids=...)` / `--node-id` selects them without a separate `camera_mode`. The notebook runs a dive or a fixed-camera example via `SOURCE`.
- Fetch fixed-camera annotations from ONC's location annotation service, which is what SeaTube's fixed-camera player shows. It returns taxa, people and review fields in one request per site. The old per-device query asked only for SeaScribe-sourced annotations, so it missed most fixed-camera annotations (0 of 129 at China Creek in February 2022), and it made one extra request per annotation.
- Fix fixed-camera failures. All-camera fetches crashed on sites without SeaTube video. Timestamps without milliseconds were rejected. Sites with sub-sites (e.g. Barkley Canyon Mid-East) were never listed. Only one deployment per camera was searched, so annotations from other deployments stayed unmapped. A camera redeployed between sites could be credited to the wrong site.
- Fixed-camera records now carry a unique site label, the ONC location code, the site's position and nominal depth (so depth filters apply), and a SeaTube player link. A `camera_mode` that contradicts `dive_ids`/`node_ids` raises instead of silently scanning dives.
- Compatibility: `FetchFilters.camera_mode` now defaults to `None` (inferred from the selection; still dives when nothing is selected). `page_size`, `resolve_taxon_names`, `--page-size` and `--skip-taxon-name-resolution` are accepted but no longer used. `FetchFilters.matches_taxonomy_ids()` drops its internal `taxonomy_id_override` argument. Saved fixed-camera files from earlier versions still load.

- Add author/last-editor discovery and selection by one or several user IDs, with a real crab example that carries the people filters through media planning and exports. Distinguish last editors from confirmed expert reviewers.

- Replace the synthetic notebook tutorial with a live ONC workflow, direct registration/profile links, private `.env` token loading, and optional frame/clip extraction from one shared archive.
- Separate credential-free notebook validation in CI from explicit local execution against ONC; keep executed results under ignored downloads.
- Remove the synthetic public example dataset and simplify the README.

## 0.3.0

- Make Python the primary documented workflow with `SeaTube`, common-name/scientific-name search, catalog discovery, and a runnable offline notebook.
- Add per-dataset group availability, mapping diagnostics, date/depth/exact-AphiaID filters, and convenience frame/clip planning.
- Add bounded, overlapping-interval-aware MP4 extraction with source annotation indexes; keep timestamp-only clip listings and the optional CLI.
- Validate fetch/media limits, warn on incomplete taxonomy, and raise on failed fetch/download/extraction requests instead of silently returning partial results.
- Make downloads/extraction atomic, retain earlier indexed outputs across selections, fix near-boundary deduplication and millisecond filename collisions, enforce per-label frame caps, and avoid mapping to a different identified camera.
- Add offline regression tests, real local ffmpeg verification, and GitHub Actions checks.

### Compatibility notes

Existing annotation JSON, low-level Python classes, and CLI commands remain supported. `seatube clips` still lists timestamps; `seatube extract-clips` creates media. Library progress now uses logging. Transient taxonomy failures produce warnings; ONC fetch/media failures now raise exceptions. If older code relied on partial results after errors, handle these exceptions explicitly. Frame deduplication now extracts a real annotation instant; outputs from older rounded timestamps may have different names.
