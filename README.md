# SeaTube Downloader

[![Tests](https://github.com/Spiffical/seatube-downloader/actions/workflows/tests.yml/badge.svg)](https://github.com/Spiffical/seatube-downloader/actions/workflows/tests.yml)

**Find annotated marine organisms in Ocean Networks Canada’s SeaTube archive, then extract frames or short video clips from Python.** Use it in a notebook or a script: discover dives and cameras, inspect which taxa were annotated, filter observations, and download only the archive files needed for your selection.

This package searches **existing annotations**. It does not detect organisms in unannotated video. A supported search group does not guarantee that matching observations exist in your selected dates or locations.

**Start here:** [Python walkthrough](docs/guide.md) · [Runnable notebook](examples/research_walkthrough.ipynb) · [Organism catalog](docs/organisms.md) · [API reference](docs/python-api.md)

## What can I search for?

There are **44 built-in groups**, plus scientific names at any taxonomic rank. Matching follows the annotation’s [WoRMS classification](https://www.marinespecies.org/rest/): a species can match a broader group even when the common name is absent from its label.

| Organisms of interest | Example search names |
|---|---|
| Fishes | `fish`, `rockfish`, `sharks-and-rays` |
| Crustaceans | `crabs`, `true-crabs`, `hermit-crabs`, `squat-lobsters`, `shrimp`, `lobsters`, `barnacles` |
| Echinoderms | `sea-stars`, `brittle-stars`, `sea-urchins`, `sea-cucumbers`, `crinoids` |
| Corals and other cnidarians | `corals`, `hard-corals`, `soft-corals`, `black-corals`, `sea-pens`, `anemones`, `tube-anemones`, `jellyfish`, `hydroids` |
| Sponges and comb jellies | `sponges`, `glass-sponges`, `ctenophores` |
| Molluscs | `octopus-and-squid`, `snails`, `nudibranchs`, `bivalves` |
| Other groups | `worms`, `tunicates`, `bryozoans`, `brachiopods`, `sea-spiders`, `marine-mammals`, `seabirds`, `algae`, `bacteria` |
| A particular scientific taxon | `Chionoecetes tanneri`, `Sebastes`, `Brachyura`, or another name carried by a label or lineage |

Read the [complete catalog](docs/organisms.md) for ancestor definitions, aliases, and scope. For example, `crabs` includes **Brachyura and Anomura**, while `true-crabs` selects Brachyura only. `squid` is an alias for **all cephalopods**, and `kelp` selects the wider algae group; use a scientific name to narrow either search.

## Install

Python 3.9 or newer. Install from a local clone; all analysis after installation can be done in Python.

```bash
git clone https://github.com/Spiffical/seatube-downloader.git
cd seatube-downloader
python3 -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[notebooks]"
```

The notebook extra installs JupyterLab and pandas. For scripts alone, use `python -m pip install -e .`.

For live ONC queries and downloads, register at [Oceans 3.0](https://data.oceannetworks.ca), copy your Web Services API token from your profile, and put it in a file named `.env` in your working directory:

```dotenv
ONC_TOKEN=your-token-here
```

Keep this file private; it is ignored by Git. For **media extraction only**, install [ffmpeg](https://ffmpeg.org/download.html) and make it available on `PATH` (for example, `brew install ffmpeg` on macOS or `sudo apt install ffmpeg` on Ubuntu). Metadata search and planning do not need ffmpeg.

## A research workflow in Python

```python
from seatube import SeaTube

sea = SeaTube(data_dir="downloads")  # loads ONC_TOKEN; keeps a reusable taxonomy cache
sea.groups("crab")                   # supported search definitions; no network

# Metadata only: fetch a manageable date range before downloading any video.
annotations = sea.fetch(
    start_date="2019-07-06T00:00:00Z",
    end_date="2019-07-06T23:59:59Z",
    save_to="downloads/annotations.json",
)
print(annotations.summary())

# What is actually annotated in THIS dataset? Counts may overlap between groups.
for row in sea.available_groups(annotations):
    print(row["group"], row["annotations"], row["mapped_annotations"])

crabs = sea.search(annotations, "crabs")
# Other searches: ["sponges", "sea-stars"], "Sebastes", "Chionoecetes tanneri"
print(crabs.taxon_summary())

# Plan a small image set. These limits are deliberate: source files can be large.
frames = crabs.frames(max_images=20, max_videos=2)
images = sea.image_downloader("outputs/crab_frames")
print(images.describe_plan(frames))  # optional HEAD requests for sizes; no video download
```

When the selection and estimated cost look right:

```python
image_rows = images.download(frames)  # JPEGs + images_index.csv + images_index.jsonl

clips = crabs.clips(before_seconds=5, after_seconds=5, max_clips=5, max_videos=2)
video = sea.clip_downloader("outputs/crab_clips")
print(video.describe_plan(clips))
clip_rows = video.download(clips)     # MP4 excerpts + clips_index.csv + clips_index.jsonl
sea.close()
```

`clips()` merges overlapping excerpts and trims them to the containing archive file. `clip_index()` gives timestamps and player links without extracting video. Images and clips retain the source annotations, taxa, IDs, annotator names, location/depth when supplied, and SeaTube links in their indexes.

**Try it without a token:** the [notebook](examples/research_walkthrough.ipynb) and [Python example](examples/offline_workflow.py) start with six clearly marked synthetic observations and a bundled taxonomy cache. Their default path never contacts ONC or downloads media.

## What to expect

- **Search scope:** ROV dives by default; fixed cameras with `camera_mode="stationary"`, or both with `"both"`. Choose dates, dive IDs, or camera locations; this is not a prebuilt index of the entire SeaTube archive.
- **Taxonomy:** groups and scientific names match labels or ancestor names. Species-level searches do not recover annotations identified only to a family or phylum. Unknown names are not automatically spell-corrected.
- **Network:** repeated exploration uses saved annotations. New lineage lookups may call WoRMS. Use `offline_taxa=True` with a populated cache for offline matching; incomplete classification raises a warning.
- **Download cost:** extraction downloads whole source archive files. Plans prefer files containing more requested outputs; this is a heuristic, not a byte-minimization guarantee. Missing size estimates are explicitly unknown. `max_videos` caps file count, not bytes.
- **Scientific interpretation:** labels identify observations, not bounding boxes, individuals, abundance, or verified presence throughout an excerpt. Zero matches can reflect incomplete annotation, unknown taxonomy, a recording gap, or a narrow query. Inspect images before treating labels as ground truth.

## Documentation and optional CLI

- [Research guide](docs/guide.md): discovery, filters, frames, clips, exports, and troubleshooting.
- [Organism catalog](docs/organisms.md): all groups and what each one includes.
- [Python API](docs/python-api.md): public classes, methods, and parameters.
- [CLI and data reference](docs/reference.md): optional terminal commands and output schemas.
- [Contributing](CONTRIBUTING.md): setup, offline tests, and project conventions.
- [Changelog](CHANGELOG.md): changes in the Python research workflow.

The CLI remains available for batch jobs and shell workflows:

```bash
seatube groups
seatube taxa --annotations downloads/annotations.json
seatube images --group crabs --max-images 20 --max-videos 2 --dry-run
seatube extract-clips --group crabs --max-clips 5 --max-videos 2 --dry-run
```

## Data and credit

This is an independent tool for working with [ONC SeaTube](https://data.oceannetworks.ca/SeaTube). Follow ONC’s [data policy](https://www.oceannetworks.ca/data-tools/data-policy/) and [citation guidance](https://www.oceannetworks.ca/data/how-to-cite-onc/) when using their data. Credit the expedition teams, annotators, ONC, and the taxonomy source as appropriate. The bundled tutorial observations are synthetic and must not be cited as ocean observations.
