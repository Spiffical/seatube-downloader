# Contributing

The project helps researchers turn SeaTube annotations into inspectable datasets. Keep Python as the primary interface; CLI commands should be thin wrappers around shared library behavior.

## Local development

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m build
```

Tests use synthetic data and block HTTP calls. If ffmpeg and ffprobe are installed, the integration test generates a small local test video and checks real frame/clip extraction. It does not need an ONC token or external video. Use `pytest -m 'not integration'` when those tools are unavailable. CI runs offline unit tests on Python 3.9, 3.12 and 3.14, plus a separate ffmpeg integration check.

## Changes worth testing carefully

- Timestamp-to-archive containment, file boundaries, recording gaps, and device selection.
- ONC IDs versus WoRMS AphiaIDs, taxonomic lineage membership, and unresolved classifications.
- Download limits, interruption recovery, and provenance retained across repeated selections.
- Both ROV and fixed-camera normalization, pagination, and request failures.

Do not weaken containment or silently ignore upstream failures to obtain more results. A missing match is better represented as uncertainty than as a confidently mislabeled frame. Keep tokens, private `.env` files, downloaded datasets, and personal information out of commits and issue reports.

When changing the organism vocabulary, update `TAXON_GROUPS` / `GROUP_ALIASES` and regenerate the catalog with `python scripts/update_organism_docs.py`. Explain broad common-name meanings and overlapping groups. A new catalog entry is not a claim of data availability.

The research notebook uses real ONC data and requires an ONC token. CI checks notebook structure and Python syntax without credentials; it does not claim live-service coverage. Before changing the notebook workflow, verify it locally:

```bash
python -m pip install nbformat nbclient ipykernel
python scripts/check_notebook.py                            # structure/syntax only
python scripts/check_notebook.py --execute                  # live metadata and taxonomy
python scripts/check_notebook.py --execute --download-media # also extract from one real archive
```

Use `--env-file /path/to/private/.env` with `--execute` to select a token file. The token is passed privately to the kernel, never embedded in notebook source. Execution saves a notebook with real results to ignored `downloads/research_walkthrough/verified.ipynb`. Commit the source notebook with cleared outputs. Keep `DOWNLOAD_MEDIA=False` as the user-facing default; extraction is an explicit choice after reviewing the plan. Test fixtures in the unit suite remain generated and offline.

## Reporting a problem

Include the package/Python versions, a minimal Python snippet, the affected camera mode and date range, and the error with tokens removed. For media mapping issues, include the annotation ID, archive filename, and timestamps if shareable. Please distinguish "no annotations found", "taxonomy unresolved", "not mapped to video", and "download failed".

## Credit and licensing

ONC data and WoRMS taxonomy have their own attribution and usage requirements; see the README. No repository-wide software license has been declared here. A maintainer must choose one before implying unrestricted reuse or redistribution of the code.
