"""Generate the organism table from the same definitions the API searches."""
from pathlib import Path
import sys

from seatube import organism_groups

HEADER = '''# Organism search catalog

These are **search definitions**, not a list of organisms guaranteed to occur in SeaTube or in a particular fetch. The package searches recorded annotations; it does not run visual detection on the archive. The table is generated from the Python vocabulary so documentation and behavior stay aligned.

```python
from seatube import organism_groups, SeaTube

organism_groups()           # every definition; no token or network needed
organism_groups("sponge")   # search names, aliases, descriptions, or ancestors
sea = SeaTube()
# sea.available_groups(annotations)  # counts in YOUR saved annotation set
# sea.search(annotations, "crabs")
```

## How matching works

An annotation matches when one of its recorded taxon names or WoRMS lineage names equals a group's ancestor (case-insensitive). Descendants are included. For example, `Brachyura` is inside `crabs`, while an annotation identified only as `Crustacea` is too broad to establish that it is a crab.

Scientific names at any rank are also searchable: `sea.search(annotations, "Chionoecetes tanneri")` or `sea.search(annotations, "Sebastes")`. This compares recorded/lineage names; it is not a global species occurrence query or a fuzzy synonym resolver. `annotations.filter(taxon_contains="...")` instead performs a label substring search. `annotations.filter(aphia_ids=[...])` matches exact external IDs without descendant expansion.

Unknown group spellings passed through `groups=` raise an error. In `search()`, an unrecognized common name is treated as a scientific name and can return no results; check the catalog first. Spaces, hyphens, underscores, and case are normalized for group names.

## Complete vocabulary

Aliases select the **whole group** shown in their row, not just the organism suggested by the alias. Groups overlap and their counts must not be added together as independent totals.

| Group | Scope / description | Ancestor names | Accepted aliases |
|---|---|---|---|
'''
FOOTER = '''
## Biological scope to check before sampling

- `crabs` includes Anomura (hermit and king crabs, squat lobsters and relatives). Use `true-crabs` or a scientific name when that is too broad.
- `octopus`, `squid`, and `cephalopods` all select `octopus-and-squid`, defined by Cephalopoda. For octopus-only work, search a narrower scientific taxon such as `Octopoda`.
- `kelp` and `seaweed` select `algae`, which includes broad ochrophyte lineages, not only macroalgae or kelps. Use a narrower scientific taxon such as `Laminariales` when appropriate.
- `jellyfish` excludes hydrozoan jellyfish; `hydroids` includes Hydrozoa, including hydromedusae. The group names are convenience labels, not mutually exclusive ecological categories.
- `soft-corals` includes all Octocorallia (including sea pens); `corals` combines several lineages and includes stylasterid hydrocorals.
- `worms` is a convenience union of Annelida, Nemertea, Sipuncula and Echiura, not every worm-shaped organism or every worm phylum. The alias `polychaetes` currently selects this entire union; use `Polychaeta` as an explicit scientific-name filter via `taxa=["Polychaeta"]` for that narrower taxon.
- `marine-mammals` currently matches Mammalia and `seabirds` matches Aves. The rules do not independently verify a marine lifestyle. `bacteria` matches bacterial annotations, not only visible mats.
- `nudibranchs` means Nudibranchia, not every sea slug; `snails` matches all Gastropoda, including slugs.

WoRMS classifications can change. Cached lineages record the classification used for a run, while common-name presets encode the ancestor strings above. Keep the cache with your outputs; inspect and refresh it deliberately when taxonomy changes. An unresolved lineage yields a warning and conservative matching, not evidence of absence.

## What results can support

Annotation counts describe observations and identification effort. They are not population abundance, counts of unique individuals, bounding boxes, or independent confirmation of presence. Generic labels cannot establish a more specific species, and unannotated organisms may appear in the same frame. Review the extracted media and source annotations before using labels as research ground truth.

Definitions and lineage lookups use the [WoRMS REST classification service](https://www.marinespecies.org/rest/). The [SeaTube overview](https://data.oceannetworks.ca/SeaTube) describes the source annotation system and its taxonomies. See the [guide](guide.md) to move from a search to frames, clips and provenance exports.
'''


def render():
    lines = []
    for row in organism_groups():
        ancestors = ", ".join(f'`{name}`' for name in row["ancestors"])
        aliases = ", ".join(f'`{name}`' for name in row["aliases"]) or "—"
        lines.append(f'| `{row["group"]}` | {row["description"]} | {ancestors} | {aliases} |')
    return HEADER + "\n".join(lines) + "\n" + FOOTER


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "docs" / "organisms.md"
    content = render()
    if "--check" in sys.argv:
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            raise SystemExit("Organism catalog is stale: run python scripts/update_organism_docs.py")
    else:
        target.write_text(content, encoding="utf-8")
