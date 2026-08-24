# Python API

Everything the CLI does is a thin wrapper over the `seatube` package.

```python
from seatube import (
    OncClient, AnnotationFetcher, FetchFilters, AnnotationSet,
    ReviewFilters, WormsResolver, ImageDownloader,
    build_frames, select_frames,
)
```

## Fetch annotations

```python
client = OncClient.from_env()          # reads ONC_TOKEN from .env
fetcher = AnnotationFetcher(client, resolver=WormsResolver("downloads/.worms_cache.json"))

result = fetcher.fetch(FetchFilters(
    start_date="2019-07-06T00:00:00.000Z",
    end_date="2019-07-06T23:59:59.000Z",
    groups=("crabs",),                 # broad groups, lineage-matched
    review=ReviewFilters(reviewed_only=True),
))
result.save("downloads/annotations.json")
```

## Explore offline

```python
annotations = AnnotationSet.load("downloads/annotations.json")   # 8487 records

crabs = annotations.filter(groups=["crabs"], resolver=WormsResolver("downloads/.worms_cache.json"))
len(crabs)                             # 103

for stats in annotations.annotator_summary()[:3]:
    print(stats.name, stats.annotations, stats.top_taxa)
# Ashley Marranzino 8069 ['Actinopterygii', 'Crustacea', 'Myctophidae']
# Upasana Ganguly 261 ['Actinopterygii', 'Inachidae', 'Brachyura']
# Herbert Leavitt 77 ['Actinopterygii', 'Crustacea', 'Cephalopoda']

for stats in crabs.taxon_summary()[:2]:
    print(stats.name, stats.aphia_id, stats.annotations)
# Inachidae 148427 44
# Brachyura 106673 37

for row in crabs.clip_index(window_seconds=60)[:2]:
    print(row["archive_filename"], row["offset_seconds"], row["taxa"])
```

`AnnotationSet` is a sequence of `Annotation` objects with typed accessors:

```python
ann = crabs[0]
ann.taxon_labels            # ['Inachidae']
ann.creator_name            # who logged it
ann.offset_in_file_seconds()  # seek position inside ann.archive_filename
ann.seatube_link            # the moment, in ONC's player
ann.raw                     # the full underlying record
```

## Extract images

```python
frames = select_frames(
    build_frames(crabs),
    max_images=20,
    max_per_taxon=5,
)

downloader = ImageDownloader(client, "images", resolver=WormsResolver("images/.worms_cache.json"))
print(downloader.describe_plan(frames))   # byte cost, downloads nothing
rows = downloader.download(frames)        # images + images_index.csv/.jsonl
```

## ML-ready tables

```python
rows = annotations.flatten()               # one dict per (annotation, taxon)
annotations.write_flat_csv("annotations_flat.csv")
annotations.write_flat_jsonl("annotations_flat.jsonl")

import pandas as pd
df = pd.DataFrame(rows)
df.groupby("taxon_display_text").size().sort_values(ascending=False)
```

## Raw ONC access

`OncClient` exposes the endpoints directly if you need something the
higher-level objects don't cover:

```python
dives = client.list_dives()                    # every dive: diveId, referenceDiveId, dates...
dive_id = dives[0]["diveId"]                   # e.g. 1473 == EX1903L2_Dive14

client.dive_annotations(dive_id)
client.dive_video_metadata(dive_id, "L")
client.fixed_camera_tree()                     # node ids for stationary queries

ann = annotations[0]                           # from any AnnotationSet
client.annotation_detail(ann.id)
client.archive_file_size(ann.archive_filename)
client.download_archive_file(ann.archive_filename, "local.mp4")

taxon = ann.taxa[0].raw                        # taxonomyId/taxonId are ONC-internal
client.taxon_detail(taxon["taxonomyId"], taxon["taxonId"])
```
