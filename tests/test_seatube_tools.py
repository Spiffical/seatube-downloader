"""Offline tests: no ONC or WoRMS calls, no ffmpeg.

Coverage concentrates on the places where a quiet mistake produces
plausible-but-wrong data: mapping a timestamp to the archive file that truly
contains it, the seek offset inside that file, lineage-to-group matching,
and the download-minimizing frame selection.
"""

from datetime import datetime, timezone

import pytest

import seatube.taxonomy as tg
from seatube.annotations import Annotation, AnnotationSet, ReviewFilters
from seatube.archive import data_file_row_containing
from seatube.images import Frame, build_frames, select_frames


# ---------------------------------------------------------------------------
# archive-file containment
# ---------------------------------------------------------------------------

def test_data_file_row_containing_never_uses_nearest_gap_row():
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
    assert data_file_row_containing(media, contained) == media["dataFiles"][0]
    assert data_file_row_containing(media, in_gap) is None


def test_data_file_row_containing_accounts_for_extra_milliseconds():
    media = {"dateStartSeconds": 1_000, "dataFiles": [["10", "5", "clip.mp4", "0", "500"]]}
    before = datetime.fromtimestamp(1_010.25, tz=timezone.utc)
    after = datetime.fromtimestamp(1_010.75, tz=timezone.utc)
    assert data_file_row_containing(media, before) is None
    assert data_file_row_containing(media, after) == media["dataFiles"][0]


# ---------------------------------------------------------------------------
# offsets inside the archive file
# ---------------------------------------------------------------------------

def record(start, clip_start="2016-06-26T01:40:09.000Z", duration=300.0, **extra):
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


def test_offset_is_measured_from_the_archive_clip_start():
    # clipOffsetSeconds is the file's place in the media series, not a position
    # inside the file, so it must not leak into the seek time.
    ann = Annotation(record("2016-06-26T01:42:05.000Z", clipOffsetSeconds=17995.0))
    assert ann.offset_in_file_seconds() == pytest.approx(116.0)


def test_offset_rejects_out_of_file_timestamps():
    assert Annotation(record("2016-06-26T01:39:00.000Z")).offset_in_file_seconds() is None
    assert Annotation(record("2016-06-26T01:46:00.000Z")).offset_in_file_seconds() is None
    assert Annotation(record("2016-06-26T01:42:05.000Z", archive=None)).offset_in_file_seconds() is None


def test_offset_allows_unknown_duration():
    ann = Annotation(record("2016-06-26T01:42:05.000Z", duration=None))
    assert ann.offset_in_file_seconds() == pytest.approx(116.0)


# ---------------------------------------------------------------------------
# frame planning
# ---------------------------------------------------------------------------

def annotations(*records_):
    return [Annotation(r) for r in records_]


def test_build_frames_merges_annotations_sharing_an_instant():
    frames = build_frames(annotations(
        record("2016-06-26T01:42:05.000Z", annotation_id=1),
        record("2016-06-26T01:42:05.000Z", annotation_id=2),
        record("2016-06-26T01:42:07.000Z", annotation_id=3),
    ))
    assert sorted(len(f.annotations) for f in frames) == [1, 2]


def test_dedupe_window_merges_near_simultaneous_annotations():
    anns = annotations(
        record("2016-06-26T01:42:05.000Z", annotation_id=1),
        record("2016-06-26T01:42:05.400Z", annotation_id=2),
    )
    assert len(build_frames(anns, dedupe_seconds=0.0)) == 2
    assert len(build_frames(anns, dedupe_seconds=1.0)) == 1


def test_frames_from_different_files_never_merge():
    frames = build_frames(annotations(
        record("2016-06-26T01:42:05.000Z", archive="A.mov"),
        record("2016-06-26T01:42:05.000Z", archive="B.mov"),
    ))
    assert len(frames) == 2


def test_select_frames_prefers_the_richest_archive_file():
    """One download should buy as many images as possible."""
    records_ = [record(f"2016-06-26T01:42:0{i}.000Z", archive="RICH.mov", annotation_id=i)
                for i in range(1, 5)]
    records_.append(record("2016-06-26T01:42:09.000Z", archive="POOR.mov", annotation_id=9))
    frames = build_frames(annotations(*records_))
    chosen = select_frames(frames, max_images=3)
    assert len(chosen) == 3
    assert {f.archive_filename for f in chosen} == {"RICH.mov"}


def test_max_videos_caps_downloads_not_just_images():
    frames = build_frames(annotations(
        record("2016-06-26T01:42:01.000Z", archive="A.mov"),
        record("2016-06-26T01:42:02.000Z", archive="B.mov"),
    ))
    chosen = select_frames(frames, max_videos=1)
    assert len({f.archive_filename for f in chosen}) == 1


def test_max_per_taxon_balances_the_set():
    crab = {"taxonName": "Chionoecetes tanneri", "taxonomyCode": "WoRMS"}
    frames = build_frames(annotations(*[
        record(f"2016-06-26T01:42:{i:02d}.000Z", annotation_id=i, taxonomy=[crab])
        for i in range(1, 6)
    ]))
    assert len(select_frames(frames, max_per_taxon=2)) == 2


def test_image_name_is_stable_and_encodes_the_offset():
    frame = Frame("INSITEZEUS_20160626T014009.000Z-LOW.MOV", 116.0, "2016-06-26T01:42:05.000Z")
    assert frame.image_name == "INSITEZEUS_20160626T014009.000Z-LOW_t0116.00"


# ---------------------------------------------------------------------------
# taxon groups
# ---------------------------------------------------------------------------

def test_group_aliases_and_unknown_names():
    assert tg.normalize_group_name("crab") == "crabs"
    assert tg.normalize_group_name("Sea Star") == "sea-stars"
    assert tg.normalize_group_name("SPONGES") == "sponges"
    with pytest.raises(KeyError):
        tg.normalize_group_name("dinosaurs")


def test_wanted_ancestor_names_merges_groups_and_bare_taxa():
    assert tg.wanted_ancestor_names(["sponges"], ["Brachyura"]) == {"porifera", "brachyura"}


def test_aphia_id_comes_from_reference_id_or_url():
    assert tg.aphia_id_from_taxon({"taxonomyCode": "WoRMS", "referenceId": "1360"}) == 1360
    url = "https://www.marinespecies.org/aphia.php?p=taxdetails&id=106673"
    assert tg.aphia_id_from_taxon({"taxonomyCode": "WoRMS", "taxonUrl": url}) == 106673
    # taxonId is an ONC-internal id and must never be mistaken for an AphiaID
    assert tg.aphia_id_from_taxon({"taxonomyCode": "WoRMS", "taxonId": 364}) is None
    assert tg.aphia_id_from_taxon({"taxonomyCode": "CMECS", "referenceId": "12"}) is None


LINEAGES = {
    442165: ["Biota", "Animalia", "Arthropoda", "Crustacea", "Malacostraca",
             "Decapoda", "Brachyura", "Oregoniidae", "Chionoecetes", "Chionoecetes tanneri"],
    164811: ["Biota", "Animalia", "Porifera", "Demospongiae"],
}


def fake_resolver(**kwargs):
    return tg.WormsResolver(None, fetcher=lambda aid: LINEAGES.get(aid, []), **kwargs)


CRAB = {"taxonName": "Chionoecetes tanneri", "taxonomyCode": "WoRMS", "referenceId": "442165"}
SPONGE = {"taxonName": "Demospongiae", "taxonomyCode": "WoRMS", "referenceId": "164811"}


def test_lineage_match_finds_a_species_below_the_group_ancestor():
    resolver = fake_resolver()
    assert resolver.taxon_matches(CRAB, tg.wanted_ancestor_names(["crabs"], []))
    assert not resolver.taxon_matches(CRAB, tg.wanted_ancestor_names(["sponges"], []))


def test_label_match_needs_no_lookup():
    """An annotation already labelled with the ancestor matches offline."""
    resolver = tg.WormsResolver(None, offline=True)
    assert resolver.taxon_matches({"taxonName": "Porifera", "taxonomyCode": "WoRMS"},
                                  tg.wanted_ancestor_names(["sponges"], []))


def test_offline_resolver_records_what_it_could_not_check():
    resolver = tg.WormsResolver(None, offline=True)
    assert not resolver.taxon_matches(CRAB, tg.wanted_ancestor_names(["crabs"], []))
    assert resolver.unresolved == {"442165"}


def test_groups_for_labels_the_output():
    groups = fake_resolver().groups_for(CRAB)
    assert "crabs" in groups and "true-crabs" in groups and "crustaceans" in groups


def test_every_group_has_ancestors_and_a_description():
    for name, spec in tg.TAXON_GROUPS.items():
        assert spec["ancestors"], name
        assert spec["description"], name
        assert name == name.lower()


def test_every_alias_points_at_a_real_group():
    for alias, target in tg.GROUP_ALIASES.items():
        assert target in tg.TAXON_GROUPS, alias


# ---------------------------------------------------------------------------
# AnnotationSet
# ---------------------------------------------------------------------------

def person(user_id, first, last, email=""):
    return {"userId": user_id, "firstName": first, "lastName": last, "email": email}


def sample_set():
    return AnnotationSet([
        record("2016-06-26T01:42:05.000Z", annotation_id=1, taxonomy=[CRAB],
               createdBy=person(1, "Ada", "Lovelace"), numTotalReviews=2,
               toBeReviewed=False, diveName="DIVE-1"),
        record("2016-06-26T01:42:07.000Z", annotation_id=2, taxonomy=[CRAB],
               createdBy=person(1, "Ada", "Lovelace"), toBeReviewed=True,
               numTotalReviews=0, diveName="DIVE-1"),
        record("2016-06-26T01:42:09.000Z", annotation_id=3, taxonomy=[SPONGE],
               createdBy=person(2, "Grace", "Hopper"), toBeReviewed=False,
               numTotalReviews=1, diveName="DIVE-2", comment="nice sponge"),
    ])


def test_set_round_trips_through_json(tmp_path):
    path = tmp_path / "annotations.json"
    original = sample_set()
    original.save(str(path))
    loaded = AnnotationSet.load(str(path))
    assert len(loaded) == 3
    assert loaded[0].raw == original[0].raw


def test_filter_by_group_uses_lineage():
    result = sample_set().filter(groups=["crabs"], resolver=fake_resolver())
    assert [a.id for a in result] == [1, 2]


def test_filter_by_creator_and_reviews():
    s = sample_set()
    assert [a.id for a in s.filter(creator="lovelace")] == [1, 2]
    assert [a.id for a in s.filter(creator_id=2)] == [3]
    assert [a.id for a in s.filter(review=ReviewFilters(reviewed_only=True))] == [1, 3]
    assert [a.id for a in s.filter(review=ReviewFilters(min_total_reviews=2))] == [1]
    assert [a.id for a in s.filter(require_comment=True)] == [3]
    assert [a.id for a in s.filter(dive_contains="dive-2")] == [3]


def test_annotator_summary_counts_and_ranks():
    stats = sample_set().annotator_summary()
    assert [s.name for s in stats] == ["Ada Lovelace", "Grace Hopper"]
    ada = stats[0]
    assert ada.annotations == 2
    assert ada.reviewed == 1                 # one reviewed, one toBeReviewed
    assert ada.distinct_taxa == {"Chionoecetes tanneri"}
    assert ada.first_utc == "2016-06-26T01:42:05.000Z"
    assert ada.last_utc == "2016-06-26T01:42:07.000Z"


def test_taxon_summary_counts():
    stats = sample_set().taxon_summary()
    assert stats[0].name == "Chionoecetes tanneri" and stats[0].annotations == 2
    assert stats[0].aphia_id == 442165
    assert stats[1].name == "Demospongiae" and stats[1].annotations == 1


def test_clip_index_lists_exact_instants():
    rows = sample_set().clip_index()
    assert len(rows) == 3
    assert rows[0]["offset_seconds"] == pytest.approx(116.0)
    assert rows[0]["taxa"] == ["Chionoecetes tanneri"]


def test_clip_index_windows_merge_nearby_annotations():
    rows = sample_set().clip_index(window_seconds=10.0)
    # 116s and 118s share the [110, 120) window; 120s starts the next one.
    assert [(r["offset_seconds"], r["annotation_count"]) for r in rows] == [(110.0, 2), (120.0, 1)]
    assert rows[0]["taxa"] == ["Chionoecetes tanneri"]
    assert rows[1]["taxa"] == ["Demospongiae"]


def test_flatten_emits_one_row_per_taxon():
    rows = sample_set().flatten()
    assert len(rows) == 3
    assert rows[0]["annotation_id"] == 1
    assert rows[0]["creator_last_name"] == "Lovelace"
    assert rows[0]["archive_filename"] == "FILE_A.mov"
