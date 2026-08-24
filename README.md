# SeaTube Downloader

Explore the expert annotations in [Ocean Networks Canada](https://data.oceannetworks.ca)'s
SeaTube deep-sea video archive — who annotated what, where, and when — and
download exactly what you need: labelled still images, clip timestamps, or
whole videos.

```bash
seatube fetch --start-date 2019-07-06T00:00:00.000Z --end-date 2019-07-06T23:59:59.000Z
seatube images --group crabs --max-images 20
```

## Install

```bash
git clone https://github.com/Spiffical/seatube-downloader.git
cd seatube-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Requires `ffmpeg` on your PATH for image extraction. Get a free API token by
registering at [data.oceannetworks.ca](https://data.oceannetworks.ca), then:

```bash
cp .env.example .env      # set ONC_TOKEN=<your-token>
```

## Sixty-second tour

```bash
# 1. pull a day of ROV annotations (metadata only — megabytes, not video)
seatube fetch --start-date 2019-07-06T00:00:00.000Z --end-date 2019-07-06T23:59:59.000Z

# 2. who annotated, how much, what
seatube annotators
#   Ashley Marranzino  8069 annotations, 21 taxa, top: Actinopterygii, Crustacea...
#   Upasana Ganguly     261 annotations, 34 taxa, top: Actinopterygii, Inachidae...

# 3. what's in there, filtered to a broad group
seatube taxa --group crabs
#   Inachidae 44, Brachyura 37, Galatheoidea 6, ...   (103 crab annotations)

# 4. which videos and timestamps show them (no download)
seatube clips --group crabs --window-seconds 60 --csv crab_clips.csv

# 5. the images themselves — dry-run first to see the byte cost
seatube images --group crabs --max-images 4 --dry-run
#   Would download 0.07 GB to produce 4 images.
seatube images --group crabs --max-images 4
```

## Commands

| command | what it does |
|---|---|
| `seatube survey` | scout a date range: which dives/sites have annotations |
| `seatube fetch` | pull filtered annotations from ONC into a local file |
| `seatube annotators` | leaderboard: who annotated, how much, when, what |
| `seatube taxa` | counts by taxon |
| `seatube clips` | video files + timestamps for chosen annotations |
| `seatube images` | extract labelled stills + a CSV/JSONL index |
| `seatube videos` | download whole archive video files |
| `seatube groups` | the broad taxon-group vocabulary |
| `seatube locations` | fixed-camera site ids |

Only `survey`, `fetch`, and the actual downloads talk to ONC — everything
else slices the fetched file offline, so one fetch supports unlimited
exploration.

## Three ideas worth knowing

**Broad taxon groups.** `--group crabs` matches an *Inachidae* annotation
because Inachidae sits under Brachyura in the real WoRMS classification —
matching walks lineages, not label text. 44 groups (`seatube groups`), plus
`--taxon-name <AnyWormsTaxon>` for everything else. Lineages are cached
locally after one lookup each.

**Download cost is stated up front.** ONC serves whole archive files only
(~70–200 MB per five minutes of video), so one frame costs one file.
`seatube images` plans richest-file-first to satisfy your request with the
fewest downloads, `--dry-run` prints the exact byte cost, and
`--max-images` / `--max-videos` / `--max-per-taxon` set the budget — take
four images or five thousand.

**Provenance travels with every image.** The index carries taxa, WoRMS
AphiaIDs, position, depth, the annotator's name, and a link that opens the
exact moment in ONC's SeaTube player.

## Documentation

- **[Guide](docs/guide.md)** — a worked session with real commands and real
  output, from scouting a date range to a labelled image set
- **[Reference](docs/reference.md)** — every command and flag, the data
  model, and the design constraints (whole-file downloads, strict timestamp
  containment)
- **[Python API](docs/python-api.md)** — the same power as a library:
  `AnnotationSet`, `FetchFilters`, `ImageDownloader`

## Tests

```bash
pip install pytest && python -m pytest tests -q
```

Fully offline. Coverage concentrates where a quiet mistake would produce
plausible-but-wrong data: mapping timestamps to the archive file that truly
contains them, seek offsets, and lineage-to-group matching.

## Data and credit

Video and annotations belong to Ocean Networks Canada and the expedition
teams that produced them. Check ONC's
[data policy](https://www.oceannetworks.ca/data-tools/data-policy/) before
publishing, and credit the annotators — `seatube annotators` shows you
exactly whose expertise you're building on.
