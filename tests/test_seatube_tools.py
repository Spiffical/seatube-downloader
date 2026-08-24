"""Offline tests: no ONC or WoRMS calls, no ffmpeg."""

from datetime import datetime, timezone

import pytest

import download_images as di
import taxon_groups as tg
from download_seatube import pick_data_file_row


# --------------------------------------------------------------------------
# archive-file selection
# --------------------------------------------------------------------------

def test_pick_data_file_row_never_uses_nearest_gap_row():
    """A timestamp inside a recording gap must map to no file, not the closest one."""
    media = {
        "dateStartSeconds": 1_000,
        "dataFiles": [
            ["0", "60", "first.mp4", "0", "0"],
            ["120", "60", "second.mp4", "0", "0"],
        ],
    }
    contained = datetime.fromtimestamp(1_030, tz=timezone.utc)
    in_gap = datetime.fromtimestamp(1_090, tz=timezone.utc)
    assert pick_data_file_row(media, contained) == media["dataFiles"][0]
    assert pick_data_file_row(media, in_gap) is None


def test_pick_data_file_row_accounts_for_extra_milliseconds():
    media = {"dateStartSeconds": 1_000, "dataFiles": [["10", "5", "clip.mp4", "0", "500"]]}
    before = datetime.fromtimestamp(1_010.25, tz=timezone.utc)
    after = datetime.fromtimestamp(1_010.75, tz=timezone.utc)
    assert pick_data_file_row(media, before) is None
    assert pick_data_file_row(media, after) == media["dataFiles"][0]


# --------------------------------------------------------------------------
# frame offsets
# --------------------------------------------------------------------------

def annotation(start, clip_start="2016-06-26T01:40:09.000Z", duration=300.0, **extra):
    base = {
        "annotationId": extra.pop("annotation_id", 1),
        "startDate": start,
        "archiveClipStartDate": clip_start,
        "archiveFilename": extra.pop("archive", "FILE_A.mov"),
        "clipDurationSeconds": duration,
        "taxonomy": extra.pop("taxonomy", []),
    }
    base.update(extra)
    return base


def test_annotation_offset_is_measured_from_the_archive_clip_start():
    # clipOffsetSeconds is the file's place in the media series, not a position
    # inside the file, so it must not leak into the seek time.
    ann = annotation("2016-06-26T01:42:05.000Z", clipOffsetSeconds=17995.0)
    assert di.annotation_offset(ann) == pytest.approx(116.0)


def test_annotation_offset_rejects_out_of_file_timestamps():
    assert di.annotation_offset(annotation("2016-06-26T01:39:00.000Z")) is None  # before
    assert di.annotation_offset(annotation("2016-06-26T01:46:00.000Z")) is None  # past duration
    assert di.annotation_offset(annotation("2016-06-26T01:42:05.000Z", archive=None)) is None


def test_annotation_offset_allows_unknown_duration():
    ann = annotation("2016-06-26T01:42:05.000Z", duration=None)
    assert di.annotation_offset(ann) == pytest.approx(116.0)


# --------------------------------------------------------------------------
# frame grouping and selection
# --------------------------------------------------------------------------

def test_build_frames_merges_annotations_sharing_an_instant():
    anns = [
        annotation("2016-06-26T01:42:05.000Z", annotation_id=1),
        annotation("2016-06-26T01:42:05.000Z", annotation_id=2),
        annotation("2016-06-26T01:42:07.000Z", annotation_id=3),
    ]
    frames = di.build_frames(anns, dedupe_seconds=0.0)
    assert sorted(len(f.annotations) for f in frames) == [1, 2]


def test_dedupe_window_merges_near_simultaneous_annotations():
    anns = [
        annotation("2016-06-26T01:42:05.000Z", annotation_id=1),
        annotation("2016-06-26T01:42:05.400Z", annotation_id=2),
    ]
    assert len(di.build_frames(anns, dedupe_seconds=0.0)) == 2
    assert len(di.build_frames(anns, dedupe_seconds=1.0)) == 1


def test_frames_from_different_files_never_merge():
    anns = [
        annotation("2016-06-26T01:42:05.000Z", archive="A.mov"),
        annotation("2016-06-26T01:42:05.000Z", archive="B.mov"),
    ]
    assert len(di.build_frames(anns, dedupe_seconds=0.0)) == 2


def test_select_frames_prefers_the_richest_archive_file():
    """One download should buy as many images as possible."""
    anns = [annotation(f"2016-06-26T01:42:0{i}.000Z", archive="RICH.mov", annotation_id=i)
            for i in range(1, 5)]
    anns.append(annotation("2016-06-26T01:42:09.000Z", archive="POOR.mov", annotation_id=9))
    frames = di.build_frames(anns, dedupe_seconds=0.0)

    chosen = di.select_frames(frames, max_images=3, max_videos=None, max_per_taxon=None)
    assert len(chosen) == 3
    assert {f.archive_filename for f in chosen} == {"RICH.mov"}


def test_max_videos_caps_downloads_not_just_images():
    anns = [annotation("2016-06-26T01:42:01.000Z", archive="A.mov"),
            annotation("2016-06-26T01:42:02.000Z", archive="B.mov")]
    frames = di.build_frames(anns, dedupe_seconds=0.0)
    chosen = di.select_frames(frames, max_images=None, max_videos=1, max_per_taxon=None)
    assert len({f.archive_filename for f in chosen}) == 1


def test_max_per_taxon_balances_the_set():
    def crab(i):
        return annotation(f"2016-06-26T01:42:{i:02d}.000Z", annotation_id=i,
                          taxonomy=[{"taxonName": "Chionoecetes tanneri", "taxonomyCode": "WoRMS"}])
    anns = [crab(i) for i in range(1, 6)]
    frames = di.build_frames(anns, dedupe_seconds=0.0)
    chosen = di.select_frames(frames, max_images=None, max_videos=None, max_per_taxon=2)
    assert len(chosen) == 2


def test_image_name_is_stable_and_encodes_the_offset():
    frame = di.Frame("INSITEZEUS_20160626T014009.000Z-LOW.MOV", 116.0, "2016-06-26T01:42:05.000Z")
    assert frame.image_name == "INSITEZEUS_20160626T014009.000Z-LOW_t0116.00"


# --------------------------------------------------------------------------
# taxon groups
# --------------------------------------------------------------------------

def test_group_aliases_and_unknown_names():
    assert tg.normalize_group("crab") == "crabs"
    assert tg.normalize_group("Sea Star") == "sea-stars"
    assert tg.normalize_group("SPONGES") == "sponges"
    with pytest.raises(KeyError):
        tg.normalize_group("dinosaurs")


def test_resolve_wanted_merges_groups_and_bare_taxa():
    assert tg.resolve_wanted(["sponges"], ["Brachyura"]) == {"porifera", "brachyura"}


def test_aphia_id_comes_from_reference_id_or_url():
    assert tg.aphia_id_from_taxon({"taxonomyCode": "WoRMS", "referenceId": "1360"}) == 1360
    url = "https://www.marinespecies.org/aphia.php?p=taxdetails&id=106673"
    assert tg.aphia_id_from_taxon({"taxonomyCode": "WoRMS", "taxonUrl": url}) == 106673
    # taxonId is an ONC-internal id and must never be mistaken for an AphiaID
    assert tg.aphia_id_from_taxon({"taxonomyCode": "WoRMS", "taxonId": 364}) is None
    assert tg.aphia_id_from_taxon({"taxonomyCode": "CMECS", "referenceId": "12"}) is None


def fake_resolver(**kwargs):
    lineages = {
        442165: ["Biota", "Animalia", "Arthropoda", "Crustacea", "Malacostraca",
                 "Decapoda", "Brachyura", "Oregoniidae", "Chionoecetes", "Chionoecetes tanneri"],
        164811: ["Biota", "Animalia", "Porifera", "Demospongiae"],
    }
    def fetch(aphia_id):
        return lineages.get(aphia_id, [])
    return tg.TaxonResolver(None, fetcher=fetch, **kwargs)


def test_lineage_match_finds_a_species_below_the_group_ancestor():
    resolver = fake_resolver()
    crab = {"taxonName": "Chionoecetes tanneri", "taxonomyCode": "WoRMS", "referenceId": "442165"}
    assert resolver.taxon_matches(crab, tg.resolve_wanted(["crabs"], []))
    assert not resolver.taxon_matches(crab, tg.resolve_wanted(["sponges"], []))


def test_label_match_needs_no_lookup():
    """An annotation already labelled with the ancestor matches offline."""
    resolver = tg.TaxonResolver(None, offline=True)
    sponge = {"taxonName": "Porifera", "taxonomyCode": "WoRMS"}
    assert resolver.taxon_matches(sponge, tg.resolve_wanted(["sponges"], []))


def test_offline_resolver_records_what_it_could_not_check():
    resolver = tg.TaxonResolver(None, offline=True)
    crab = {"taxonName": "Chionoecetes tanneri", "taxonomyCode": "WoRMS", "referenceId": "442165"}
    assert not resolver.taxon_matches(crab, tg.resolve_wanted(["crabs"], []))
    assert resolver.unresolved == {"442165"}


def test_empty_filter_keeps_everything():
    resolver = tg.TaxonResolver(None, offline=True)
    assert resolver.annotation_matches({"taxonomy": []}, set())


def test_annotation_matches_when_any_taxon_matches():
    resolver = fake_resolver()
    ann = {"taxonomy": [
        {"taxonName": "Demospongiae", "taxonomyCode": "WoRMS", "referenceId": "164811"},
        {"taxonName": "Chionoecetes tanneri", "taxonomyCode": "WoRMS", "referenceId": "442165"},
    ]}
    assert resolver.annotation_matches(ann, tg.resolve_wanted(["crabs"], []))
    assert len(resolver.matching_taxa(ann, tg.resolve_wanted(["crabs"], []))) == 1


def test_groups_for_labels_the_output():
    resolver = fake_resolver()
    crab = {"taxonName": "Chionoecetes tanneri", "taxonomyCode": "WoRMS", "referenceId": "442165"}
    groups = resolver.groups_for(crab)
    assert "crabs" in groups and "true-crabs" in groups and "crustaceans" in groups


def test_every_group_has_ancestors_and_a_description():
    for name, spec in tg.GROUPS.items():
        assert spec["ancestors"], name
        assert spec["description"], name
        assert name == name.lower()


def test_every_alias_points_at_a_real_group():
    for alias, target in tg.ALIASES.items():
        assert target in tg.GROUPS, alias
