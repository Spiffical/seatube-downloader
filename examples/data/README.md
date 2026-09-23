# Offline tutorial fixtures

`annotations.json` contains **six fabricated observations**. Names, times, IDs, depth, review signals, the dive, and archive filename are for demonstration only. `SYNTHETIC_EXAMPLE.mp4` does not exist in ONC. Do not attempt to download this media or treat these records as research observations.

The four taxonomic names and their WoRMS AphiaIDs are real: Brachyura (106673), Porifera (558), Actinopterygii (10194), and Galatheoidea (106685). `lineages.json` contains their classification names retrieved from the [WoRMS REST service](https://www.marinespecies.org/rest/) on 2026-09-22. They are included solely to allow offline demonstration of lineage matching.

Expected behavior: four synthetic crab annotations (three Brachyura and one Galatheoidea), of which three map to the synthetic video; one sponge annotation; one fish annotation. One crab observation intentionally lacks a containing archive file. Groups overlap: all crab observations also match crustaceans.
