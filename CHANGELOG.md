# Changelog

## 0.3.0

- Make Python the primary documented workflow with `SeaTube`, common-name/scientific-name search, catalog discovery, and a runnable offline notebook.
- Add per-dataset group availability, mapping diagnostics, date/depth/exact-AphiaID filters, and convenience frame/clip planning.
- Add bounded, overlapping-interval-aware MP4 extraction with source annotation indexes; keep timestamp-only clip listings and the optional CLI.
- Validate fetch/media limits, warn on incomplete taxonomy, and raise on failed fetch/download/extraction requests instead of silently returning partial results.
- Make downloads/extraction atomic, retain earlier indexed outputs across selections, fix near-boundary deduplication and millisecond filename collisions, enforce per-label frame caps, and avoid mapping to a different identified camera.
- Add offline regression tests, real local ffmpeg verification, and GitHub Actions checks.

### Compatibility notes

Existing annotation JSON, low-level Python classes, and CLI commands remain supported. `seatube clips` still lists timestamps; `seatube extract-clips` creates media. Library progress now uses logging. Transient taxonomy failures produce warnings; ONC fetch/media failures now raise exceptions. If older code relied on partial results after errors, handle these exceptions explicitly. Frame deduplication now extracts a real annotation instant; outputs from older rounded timestamps may have different names.
