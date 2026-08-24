# SeaTube Downloader

Find annotated deep-sea video in [Ocean Networks Canada](https://data.oceannetworks.ca)'s
SeaTube archive, and turn the annotations into labelled still images.

ONC's archive holds a large body of expert annotations on ROV and fixed-camera video —
each one a taxon identification pinned to an exact instant. This turns them into an
image set you can train on:

```bash
# 20 crab images, with labels, from 2019 ROV dives
python download_seatube.py --start-date 2019-07-01T00:00:00.000Z \
                           --end-date 2019-07-31T23:59:59.000Z --group crabs
python download_images.py --group crabs --max-images 20
```

## How it works

Two steps, because they cost very different amounts of time and bandwidth.

1. **`download_seatube.py`** queries ONC, filters annotations, and works out which
   archive video file contains each one. Output is `downloads/matched_annotations.json`
   plus flat CSV/JSONL. Minutes, but only megabytes.
2. **`download_images.py`** reads that file and extracts frames. Fast per image, but
   each archive file it needs is tens to hundreds of MB.

Step 1 is the slow query; step 2 you can re-run freely with different filters and
counts, without touching the ONC API again.

## Install

```bash
git clone https://github.com/Spiffical/seatube-downloader.git
cd seatube-downloader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`ffmpeg` must be on your PATH for image extraction (`brew install ffmpeg`,
`apt install ffmpeg`).

## Get a token

Register at [data.oceannetworks.ca](https://data.oceannetworks.ca), then copy your Web
Services API token from your profile page:

```bash
cp .env.example .env      # then set ONC_TOKEN=<your-token>
```

## Step 1 — find annotations

```bash
python download_seatube.py \
  --camera-mode dive \
  --start-date 2019-07-01T00:00:00.000Z \
  --end-date 2019-07-31T23:59:59.000Z \
  --taxonomy-code WoRMS
```

`--camera-mode` picks the source: `dive` (ROV dives), `stationary` (fixed cameras), or
`both`. For fixed cameras, list the sites first:

```bash
python download_seatube.py --list-stationary-locations \
  --start-date 2023-01-01T00:00:00.000Z --end-date 2023-12-31T23:59:59.000Z
```

then narrow with `--search-tree-node-id` or `--location-name-contains "Digby Island"`.

### Filter what gets collected

| Filter | Flags |
|---|---|
| Broad taxon group | `--group crabs --group sponges` |
| Any WoRMS taxon | `--taxon-name Brachyura` |
| Reviewed only | `--reviewed-only`, `--min-total-reviews 1`, `--min-positive-review-rate 0.8`, `--require-cross-review` |
| Specific annotator | `--user-id 113530`, `--created-by-name`, `--created-by-email` |
| Specific reviewer | `--modified-by-user-id`, `--modified-by-name`, `--modified-by-email` |
| Dive or site | `--dive-id`, `--search-tree-node-id`, `--location-name-contains` |
| Video resolution | `--resolution H\|L\|S` (default `L`) |

Quality-filtered example — only annotations another person confirmed:

```bash
python download_seatube.py \
  --camera-mode dive \
  --start-date 2023-01-01T00:00:00.000Z --end-date 2023-12-31T23:59:59.000Z \
  --reviewed-only --min-total-reviews 1 --min-positive-review-rate 0.8 \
  --require-cross-review
```

## Step 2 — turn annotations into images

```bash
python download_images.py --group crabs --max-images 20
```

Writes to `images/`:

```
images/
├── INSITEZEUSPLUS_DEEPDISCOVERER_20190706T194001.000Z-LOW_t0026.00.jpg
├── ...
├── images_index.csv     # one row per image
└── images_index.jsonl   # same, plus the full annotation records
```

Each index row carries the labels and everything needed to trace the image back:

| | |
|---|---|
| `taxa` | `Inachidae` |
| `worms_aphia_ids` | `148427` |
| `groups` | `crabs; crustaceans; true-crabs` |
| `frame_utc` | `2019-07-06T19:40:27.000Z` |
| `lat`, `lon`, `depth_m` | `35.735054`, `-74.818772`, `360.5` |
| `annotation_ids`, `creators` | `6791290`, the annotator who logged it |
| `seatube_link` | opens the exact moment in SeaTube |

### Filtering by broad taxa

`--group` accepts everyday words rather than taxonomy:

```bash
python download_images.py --group crabs --group sponges --max-images 50
python download_images.py --list-groups          # all 44 groups
```

There are 44 groups — `fish`, `crabs`, `shrimp`, `sponges`, `corals`, `sea-stars`,
`anemones`, `octopus-and-squid`, `sea-cucumbers`, `nudibranchs`, `algae` and so on —
plus common synonyms (`crab`, `starfish`, `squid`, `kelp` all work).

Each group is defined by its ancestor taxa, and matching walks the real WoRMS
classification: `--group crabs` finds a *Chionoecetes tanneri* annotation because
Brachyura is in its lineage, not because of anything in the label text. Lineages are
fetched once from [WoRMS](https://www.marinespecies.org) and cached in
`images/.worms_cache.json`; `--offline-taxa` then works with no network at all.

For anything the vocabulary doesn't cover, name the taxon directly — at any rank:

```bash
python download_images.py --taxon-name Pandalidae --taxon-name Sebastes
```

### Choosing how many images

```bash
python download_images.py --group fish --max-images 10        # a quick look
python download_images.py --group fish --max-images 5000      # a training set
python download_images.py --group fish --max-per-taxon 25     # balanced across species
```

**Ask before you download.** `--dry-run` reports the exact byte cost:

```
$ python download_images.py --group crabs --max-images 4 --dry-run
103 annotations pass the filters
100 distinct frames available; 4 selected from 1 archive file(s)
  INSITEZEUSPLUS_DEEPDISCOVERER_20190706T194001.000Z-LOW.mp4  ->  4 image(s), 0.07 GB

Would download 0.07 GB to produce 4 images.
```

Other controls:

| Flag | Effect |
|---|---|
| `--max-videos N` | hard cap on archive files fetched, whatever the image count |
| `--dedupe-seconds 1.0` | merge annotations within a second onto one frame |
| `--image-format png` | PNG instead of JPEG (`--jpeg-quality 2` is near-lossless) |
| `--keep-videos` | keep the archive files for pulling more frames yourself |
| `--reviewed-only`, `--require-comment`, `--taxon-contains`, `--dive-name-contains`, `--location-contains` | narrow further without re-querying ONC |

Re-running is safe: existing images are kept and never re-downloaded.

## Why the download size matters

ONC's archive endpoint serves whole files — it ignores HTTP range requests — so a
single frame costs one entire video file, typically 70–200 MB for five minutes.

So `download_images.py` groups annotations into frames, groups frames into the files
that hold them, and visits the files that yield the most images first. Asking for 4
crab images downloaded 0.07 GB, not 0.28 GB, because all four came out of one file.
Each file is deleted as soon as its frames are extracted unless you pass
`--keep-videos`.

## What the image actually shows

The frame is the video at the annotation's exact timestamp. Annotators log an
observation when they notice it, so the animal is reliably *present*, but it may be
off-centre, partly out of view, or near the edge as the ROV moves. Annotations are
whole-frame labels: there are no bounding boxes, and a frame may show organisms nobody
annotated. Use `--keep-videos` if you want to pick better frames by hand.

## Also included

`search_seatube.py` is a quick scouting tool. It orders ONC's SeaTube annotation export
for a date range and prints which dives and locations have the most annotations, so you
know where to point step 1:

```bash
python search_seatube.py --start-date 2023-01-01T00:00:00.000Z \
                         --end-date 2023-12-31T23:59:59.000Z
```

## Tests

```bash
pip install pytest && python -m pytest tests -q
```

The suite is fully offline — no ONC, WoRMS, or ffmpeg calls — and covers the parts
where a silent mistake would produce confident, wrong data: mapping a timestamp to the
file that truly contains it, computing the seek offset within that file, and matching
lineages to groups.

## Data and credit

Video and annotations belong to Ocean Networks Canada and the expedition teams that
produced them. Check ONC's
[data usage policy](https://www.oceannetworks.ca/data-tools/data-policy/) before
publishing, and credit the annotators — `images_index.csv` records who made each
observation.
