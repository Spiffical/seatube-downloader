# Guide: exploring SeaTube end to end

A worked session, with real commands and real output. The running example is
NOAA's *Okeanos Explorer* dive EX1903L2_Dive14 (July 6 2019, ~360 m down on
the southeast US continental margin), which carries several thousand expert
annotations.

Every command below except `survey` and `fetch` runs **offline** on a local
file — fetch once, slice as many ways as you like.

## 1. Scout a date range — `survey`

Before committing to a fetch, see where annotations exist at all:

```bash
seatube survey --start-date 2019-07-01T00:00:00.000Z --end-date 2019-07-31T23:59:59.000Z
```

`survey` orders one bulk annotation export from ONC (a minute or two) and
prints which dives/locations have annotations, with counts and annotator
names, plus a top-annotators tally. Use it to pick the dates and places worth
fetching.

## 2. Pull the annotations — `fetch`

```bash
seatube fetch \
  --start-date 2019-07-06T00:00:00.000Z \
  --end-date 2019-07-06T23:59:59.000Z
```

```
Dives overlapping the date range: 1 (of 2065 listed)
Matched dive annotations after filters: 8487
Mapping annotations to archive video files (strict containment)...
8487/8487 annotations mapped to an archive file
Saved 8487 annotations to downloads/annotations.json
```

This is the only expensive step, and it costs minutes and megabytes — no
video moves. Each annotation is stamped with the archive video file that
*truly contains* its timestamp (see [reference.md](reference.md#archive-file-mapping)
for why "truly" matters).

You can filter at fetch time too — the same `--group`, `--creator`,
`--reviewed-only`, etc. work here — but fetching broadly and filtering
offline is usually better: one fetch, many slices.

To target specific dives instead of a whole date range, list them first —
`seatube dives` is where dive ids come from:

```bash
seatube dives --start-date 2019-07-01T00:00:00.000Z --end-date 2019-07-10T00:00:00.000Z
```

```
dive_id  dive             start             end               area              comment
------------------------------------------------------------------------------------------
1463     EX1903L2_Dive13  2019-07-05T12:00  2019-07-05T21:00  Southeast U.S.    Roanoke Minor Canyon...
1473     EX1903L2_Dive14  2019-07-06T12:00  2019-07-06T23:00  Southeast U.S.    Bodie Seep
...
```

```bash
seatube fetch --dive-id 1473 \
  --start-date 2019-07-06T00:00:00.000Z --end-date 2019-07-06T23:59:59.000Z
```

Fixed cameras work the same way: `seatube locations` is where site ids come
from.

```bash
seatube locations
```

```
id    path
--------------------------------------------------------------------------------------
...
2334  Fixed Cameras > Pacific > British Columbia North Coast > Douglas Channel > Hartley Bay Shore Station
2335  Fixed Cameras > Pacific > British Columbia North Coast > Douglas Channel > Hartley Bay Underwater Network
...
```

```bash
seatube fetch --camera-mode stationary --search-tree-node-id 2335 \
  --start-date 2021-10-01T00:00:00.000Z --end-date 2021-12-31T23:59:59.000Z
```

(Or skip the ids entirely: `--location-name-contains "Hartley Bay"`.)

## 3. Who annotated? — `annotators`

```bash
seatube annotators
```

```
annotator          user_id  annotations  reviewed  taxa  dives/sites  first       last        top_taxa
----------------------------------------------------------------------------------------------------------------------------------------
Ashley Marranzino  113530   8069         8068      21    1            2019-07-06  2019-07-06  Actinopterygii, Crustacea, Myctophidae
Upasana Ganguly    49360    261          257       34    1            2019-07-06  2019-07-06  Actinopterygii, Inachidae, Brachyura
Herbert Leavitt    90370    77           76        11    1            2019-07-06  2019-07-06  Actinopterygii, Crustacea, Cephalopoda
Tara Luke          44145    72           72        29    1            2019-07-06  2019-07-06  Bathymodiolus, Inachidae, Myxine glutinosa
...
```

Volume, review status, taxonomic breadth, activity span, and favourite taxa
per annotator. `--csv annotators.csv` writes the full table (including user
ids and emails, which ONC publishes with the annotations).

To keep only one person's work, filter any command with `--creator` (name
substring) or `--creator-id` — the exact id from the `user_id` column above:

```bash
seatube taxa --creator-id 49360               # Upasana Ganguly, per the table
seatube images --creator "Ganguly" --group crabs --max-images 10
```

## 4. What was annotated? — `taxa`

```bash
seatube taxa --group crabs
```

```
103 annotations pass the filters
taxon         aphia_id  annotations  annotators  dives/sites
------------------------------------------------------------
Inachidae     148427    44           2           1
Brachyura     106673    37           2           1
Galatheoidea  106685    6            2           1
Anomura       106671    5            1           1
...
```

Note what happened: `--group crabs` matched **Inachidae** and **Galatheidae**
annotations even though neither label contains "crab". Matching walks each
taxon's real WoRMS classification (Inachidae sits under Brachyura), not the
label text. `seatube groups` prints all 44 groups; `--taxon-name Sebastes`
works for anything the vocabulary lacks. Add `--show-groups` to label each
taxon with the groups it belongs to.

## 5. Where in the video? — `clips`

The list-of-videos-and-timestamps view. With `--window-seconds`, nearby
annotations merge so you can find dense stretches worth watching:

```bash
seatube clips --group crabs --window-seconds 60
```

```
archive_file                                                offset_s  utc                       count  taxa
------------------------------------------------------------------------------------------------------------------------------------
INSITEZEUSPLUS_DEEPDISCOVERER_20190706T202501.000Z-LOW.mp4  60.0      2019-07-06T20:26:18.000Z  4      Anomura, Brachyura, Inachidae
INSITEZEUSPLUS_DEEPDISCOVERER_20190706T140001.000Z-LOW.mp4  60.0      2019-07-06T14:01:02.000Z  2      Anomura, Paguridae
...
88 rows across 53 archive file(s).
```

Each row is: this archive file, this many seconds in, these creatures.
`--csv clips.csv` adds annotation ids and a `seatube_link` per row that opens
the exact moment in ONC's SeaTube player — often all you need, with zero
bytes of video downloaded.

## 6. Get images — `images`

Always dry-run first; it prints the exact byte cost:

```bash
seatube images --group crabs --max-images 4 --dry-run
```

```
103 annotations pass the filters
100 distinct frames available; 4 selected from 1 archive file(s)
  INSITEZEUSPLUS_DEEPDISCOVERER_20190706T194001.000Z-LOW.mp4  ->  4 image(s), 0.07 GB

Would download 0.07 GB to produce 4 images.
```

One file, not four: the planner groups frames by the archive file holding
them and visits the richest files first. Drop `--dry-run` to execute:

```
[1/1] downloading INSITEZEUSPLUS_DEEPDISCOVERER_20190706T194001.000Z-LOW.mp4 for 4 image(s)
4 new image(s) written, 4 indexed in images
```

The result is JPEGs plus `images_index.csv` / `.jsonl` carrying, per image:
taxa, WoRMS AphiaIDs, broad groups, lat/lon/depth, annotator, and the
SeaTube link back to the exact moment. Scaling up:

```bash
seatube images --group crabs --group sponges --max-images 500   # training set
seatube images --group fish --max-per-taxon 25                  # balanced classes
seatube images --group crabs --max-videos 10                    # hard byte budget
```

Re-running is safe — existing images are never re-extracted, and archive
files are deleted after use unless you pass `--keep-videos`.

## 7. Whole videos — `videos`

When you want the footage itself, not stills:

```bash
seatube videos --group crabs --max-files 3
```

Downloads the archive files referenced by the (filtered) annotations, skipping
any already present.

## One-liner recap

```bash
seatube fetch --start-date ... --end-date ...   # once, online
seatube annotators                              # who
seatube taxa --group crabs                      # what
seatube clips --group crabs --window-seconds 60 # where in the video
seatube images --group crabs --max-images 20    # the pictures themselves
```
