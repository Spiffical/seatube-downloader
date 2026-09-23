# SeaTube Downloader

[![Tests](https://github.com/Spiffical/seatube-downloader/actions/workflows/tests.yml/badge.svg)](https://github.com/Spiffical/seatube-downloader/actions/workflows/tests.yml)

Find annotated organisms in Ocean Networks Canada’s SeaTube archive. Search by organism, dive, location, or date, then extract frames and video clips with their labels and source metadata.

Searches use existing annotations; the package does not detect organisms in unannotated video. Available data depends on your selected dates and locations.

[Notebook](examples/research_walkthrough.ipynb) · [Research guide](docs/guide.md) · [Organism catalog](docs/organisms.md) · [API reference](docs/python-api.md)

## Searchable organisms

Search **44 built-in groups** or a scientific name at any taxonomic rank. Matches follow [WoRMS lineages](https://www.marinespecies.org/rest/), so a species can match a broad group without that group's name appearing in its label.

| Organisms | Example search names |
|---|---|
| Fishes | `fish`, `rockfish`, `sharks-and-rays` |
| Crustaceans | `crabs`, `true-crabs`, `hermit-crabs`, `squat-lobsters`, `shrimp`, `lobsters`, `barnacles` |
| Echinoderms | `sea-stars`, `brittle-stars`, `sea-urchins`, `sea-cucumbers`, `crinoids` |
| Corals and other cnidarians | `corals`, `hard-corals`, `soft-corals`, `black-corals`, `sea-pens`, `anemones`, `tube-anemones`, `jellyfish`, `hydroids` |
| Sponges and comb jellies | `sponges`, `glass-sponges`, `ctenophores` |
| Molluscs | `octopus-and-squid`, `snails`, `nudibranchs`, `bivalves` |
| Other groups | `worms`, `tunicates`, `bryozoans`, `brachiopods`, `sea-spiders`, `marine-mammals`, `seabirds`, `algae`, `bacteria` |
| Scientific taxa | `Chionoecetes tanneri`, `Sebastes`, `Brachyura`, or another recorded or lineage name |

The [catalog](docs/organisms.md) lists every definition and alias. Check broad aliases: `crabs` includes Brachyura and Anomura, `squid` selects all cephalopods, and `kelp` selects the wider algae group. Use `true-crabs` or a narrower scientific name when needed.

## Install

Requires Python 3.9 or newer.

```bash
git clone https://github.com/Spiffical/seatube-downloader.git
cd seatube-downloader
python3 -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[notebooks]"
```

The notebook extra includes JupyterLab and pandas. For scripts alone, install with `python -m pip install -e .`.

Media extraction also requires [ffmpeg](https://ffmpeg.org/download.html) on `PATH`: `brew install ffmpeg` on macOS or `sudo apt install ffmpeg` on Ubuntu. Metadata queries and planning do not need it.

## Get an ONC token

1. [Register for Oceans 3.0](https://data.oceannetworks.ca/Registration) and [sign in](https://data.oceannetworks.ca).
2. Open [Profile](https://data.oceannetworks.ca/Profile) → **Web Services API** → **Copy Token**. Generate a token first if none exists. [ONC instructions](https://oceannetworkscanada.github.io/Oceans3.0-API/Home.html#how-to-obtain-an-onc-token).
3. Create `.env` in the repository root, beside `pyproject.toml`:

```dotenv
ONC_TOKEN=your-token-here
```

Keep the token private. `.env` is ignored by Git; do not paste its contents into notebook cells or outputs. An `ONC_TOKEN` environment variable takes precedence over `.env`.

Run `jupyter lab` and open [examples/research_walkthrough.ipynb](examples/research_walkthrough.ipynb). The notebook queries real annotations and requires your token. Video downloads are a separate opt-in.

## Find data and plan a download

```python
from seatube import SeaTube

sea = SeaTube(data_dir="downloads")  # loads ONC_TOKEN and caches taxonomy
sea.groups("crab")                   # inspect supported search definitions

annotations = sea.fetch(
    start_date="2019-07-06T00:00:00Z",
    end_date="2019-07-06T23:59:59Z",
    save_to="downloads/annotations.json",
)
print(annotations.summary())

# What was actually annotated in this dataset?
for row in sea.available_groups(annotations):
    print(row["group"], row["annotations"], row["mapped_annotations"])

crabs = sea.search(annotations, "crabs")
# Also accepts ["sponges", "sea-stars"], "Sebastes", or a species name.
print(crabs.taxon_summary())

frames = crabs.frames(max_images=20, max_videos=2)
images = sea.image_downloader("outputs/crab_frames", keep_videos=True)
print(images.describe_plan(frames))  # estimates cost; no video download
```

Fetches retrieve metadata only. ROV dives are the default; select fixed cameras with `camera_mode="stationary"`, or both sources with `"both"`. Use `sea.dives(...)` and `sea.locations()` to discover IDs. The [guide](docs/guide.md) covers date, place, depth, annotator, and review filters.

## Extract frames or clips

After checking the plan:

```python
image_rows = images.download(frames)

clips = crabs.clips(before_seconds=5, after_seconds=5, max_clips=5, max_videos=2)
video = sea.clip_downloader("outputs/crab_clips", video_dir="outputs/crab_frames/_videos")
print(video.describe_plan(clips))
clip_rows = video.download(clips)
sea.close()
```

Overlapping excerpts merge and stop at archive boundaries. Use `crabs.clip_index()` for timestamps and SeaTube links without extracting media. Frames and clips share cached source files where their selections overlap.

Each output has a CSV/JSONL index with taxa, IDs, archive offsets, annotators, location/depth when supplied, and source links. JSONL also retains the original annotations. Save a subset with `crabs.save(...)`, `write_flat_csv(...)`, or `write_flat_jsonl(...)`.

## Limits to keep in mind

- **Whole-file downloads:** one frame can require a full source archive. Plans favor files with more requested outputs; they do not minimize bytes or produce a representative sample. Unknown sizes stay unknown. `max_videos` caps files, not bytes.
- **Taxonomic resolution:** species queries cannot recover observations labelled only to family or phylum. Unknown names are not spell-corrected. Groups overlap, so their counts are not additive.
- **Network access:** saved annotations support repeated local analysis. Uncached lineages require WoRMS; `offline_taxa=True` uses cached classifications and warns about unresolved taxa.
- **Interpretation:** annotations are observations, not abundance estimates or bounding boxes. Zero matches do not establish absence, and an organism may not remain visible throughout a clip. Inspect media before treating labels as ground truth.

## Optional CLI and documentation

```bash
seatube groups
seatube taxa --annotations downloads/annotations.json
seatube images --group crabs --max-images 20 --max-videos 2 --dry-run
seatube extract-clips --group crabs --max-clips 5 --max-videos 2 --dry-run
```

See the [CLI and data reference](docs/reference.md) for commands and output schemas, [Contributing](CONTRIBUTING.md) for development and tests, and the [Changelog](CHANGELOG.md) for changes.

## Data and credit

This is an independent tool for [ONC SeaTube](https://data.oceannetworks.ca/SeaTube). Follow ONC’s [data policy](https://www.oceannetworks.ca/data-tools/data-policy/) and [citation guidance](https://www.oceannetworks.ca/data/how-to-cite-onc/). Credit ONC, the expedition teams, annotators, and taxonomy sources as appropriate.
