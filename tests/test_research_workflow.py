"""Offline regressions for research queries, extraction, and failure handling."""
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
import requests

from seatube import (
    AnnotationSet, ClipDownloader, FetchFilters, ImageDownloader,
    IncompleteTaxonomyWarning, OncClient, OncError, SeaTube, WormsResolver,
    build_clips, build_frames, organism_groups, select_frames,
)
from seatube.archive import select_media_file
from seatube.cli import main
from seatube.fetch import AnnotationFetcher


def annotation(offset=2, *, label="Brachyura", aid=106673, archive="EXAMPLE.mp4",
               duration=10, id=1, **extra):
    return {
        "annotationId": id, "startDate": f"2020-01-01T00:00:{offset:06.3f}Z",
        "archiveClipStartDate": "2020-01-01T00:00:00.000Z",
        "archiveFilename": archive, "clipDurationSeconds": duration,
        "taxonomy": [{"taxonName": label, "taxonomyCode": "WoRMS", "referenceId": aid}],
        "cameraMode": "dive", "depth": 1200, **extra,
    }


def resolver():
    return WormsResolver(fetcher=lambda aid: {
        106673: ["Animalia", "Arthropoda", "Crustacea", "Brachyura"],
        558: ["Animalia", "Porifera"],
    }.get(aid, []))


def test_catalog_is_discoverable_and_copies_definitions():
    crab = next(g for g in organism_groups("crab") if g["group"] == "crabs")
    assert crab["ancestors"] == ["Brachyura", "Anomura"]
    crab["ancestors"].clear()
    assert next(g for g in organism_groups() if g["group"] == "crabs")["ancestors"]
    assert organism_groups("hexactinellida")[0]["group"] == "glass-sponges"


def test_sea_pens_include_current_and_legacy_worms_ancestors():
    data = AnnotationSet([annotation(label="Pennatuloidea"), annotation(5, label="Pennatulacea")])
    assert len(data.search("sea-pens", resolver=WormsResolver(offline=True))) == 2


def test_flat_exports_retain_scientific_names_and_external_ids():
    row = AnnotationSet([annotation()]).flatten()[0]
    assert row["taxon_scientific_name"] == "Brachyura"
    assert row["worms_aphia_id"] == 106673


def test_search_strings_scientific_names_and_exact_ids():
    data = AnnotationSet([annotation(), annotation(5, label="Porifera", aid=558, id=2)])
    assert len(data.search("crab", resolver=resolver())) == 1
    assert len(data.search(["Brachyura", "sponges"], resolver=resolver())) == 2
    assert len(data.filter(groups="crabs", resolver=resolver())) == 1
    assert len(data.filter(aphia_ids=[558])) == 1
    assert len(data[:1]) == 1
    assert data.summary()["mapped_annotations"] == 2
    with pytest.raises(ValueError):
        data.search([])


def test_filter_dates_and_depth_exclude_missing_measurements():
    data = AnnotationSet([annotation(2), annotation(5, depth=None, id=2)])
    assert len(data.filter(start_date="2020-01-01T00:00:03Z")) == 1
    assert len(data.filter(min_depth_m=1000, max_depth_m=1500)) == 1
    assert not data.filter(max_depth_m=100)
    with pytest.raises(ValueError):
        data.filter(min_depth_m=100, max_depth_m=1)


def test_unresolved_taxa_warn_instead_of_claiming_absence(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text('{"106673": []}')  # old empty entries must not hide failures
    r = WormsResolver(cache, offline=True)
    data = AnnotationSet([annotation(label="Unclassified crab")])
    with pytest.warns(IncompleteTaxonomyWarning):
        assert not data.search("crabs", resolver=r)
    assert r.unresolved == {"106673"}
    with pytest.warns(IncompleteTaxonomyWarning):
        data.group_summary(r)


def test_failed_lineage_not_repeated_per_annotation_or_persisted(tmp_path):
    calls = []
    r = WormsResolver(tmp_path / "cache.json", fetcher=lambda aid: calls.append(aid) or [])
    for _ in range(5):
        assert r.lineage(1) == []
    r.save()
    assert calls == [1]
    assert not (tmp_path / "cache.json").exists()
    assert r.unresolved == {"1"}


def test_group_inventory_counts_annotations_once_and_mapping_separately():
    first = annotation()
    first["taxonomy"] *= 2
    data = AnnotationSet([first, annotation(5, id=2, archive=None)])
    groups = {r["group"]: r for r in data.group_summary(resolver())}
    assert groups["crabs"]["annotations"] == 2
    assert groups["crabs"]["mapped_annotations"] == 1
    assert groups["crabs"]["archive_files"] == 1
    assert data.taxon_summary()[0].annotations == 2
    assert data.summary()["unmapped_annotations"] == 1


def test_dedupe_keeps_actual_in_bounds_instant():
    data = AnnotationSet([annotation(9.9), annotation(9.8, id=2)])
    frame = build_frames(data, dedupe_seconds=1)[0]
    assert frame.offset_seconds == 9.8
    assert frame.frame_utc == "2020-01-01T00:00:09.800Z"
    assert len(frame.annotations) == 2


def test_subcentisecond_frame_names_do_not_collide():
    frames = build_frames(AnnotationSet([annotation(2.001), annotation(2.004, id=2)]))
    assert len({f.image_name for f in frames}) == 2


def test_multilabel_frame_does_not_exceed_any_taxon_cap():
    first, second = annotation(), annotation(5, label="Porifera", aid=558, id=2)
    second["taxonomy"].extend(first["taxonomy"])
    assert len(select_frames(build_frames(AnnotationSet([first, second])), max_per_taxon=1)) == 1
    with pytest.raises(ValueError):
        select_frames([], max_images=-1)


def test_clips_clamp_merge_and_never_cross_files():
    data = AnnotationSet([annotation(1), annotation(4, id=2),
                          annotation(9, id=3), annotation(4, id=4, archive="OTHER.mp4")])
    clips = build_clips(data, before_seconds=2, after_seconds=3)
    assert [(c.archive_filename, c.offset_seconds, c.end_seconds) for c in clips] == [
        ("EXAMPLE.mp4", 0, 10), ("OTHER.mp4", 2, 7)]
    assert len(clips[0].annotations) == 3
    assert len(data.clips(max_videos=1)) == 1
    assert data.clips(max_clips=0) == []
    with pytest.warns(UserWarning, match="unknown archive duration"):
        assert not build_clips(AnnotationSet([annotation(duration=None)]))
    with pytest.raises(ValueError):
        build_clips(data, after_seconds=0)


def test_wrong_camera_is_never_used_as_fallback():
    assert select_media_file([{"deviceId": 1}], 2) is None


@pytest.mark.parametrize("kwargs", [{"camera_mode": "typo"}, {"resolution": "x"},
                                     {"page_size": 0}, {"max_dives": -1}])
def test_fetch_rejects_invalid_configuration_before_network(kwargs):
    with pytest.raises(ValueError):
        FetchFilters("2020-01-01", "2020-01-02", **kwargs)


def test_fetch_is_not_silently_partial():
    class Broken:
        def list_dives(self):
            return [{"diveId": 1, "dateFrom": "2020-01-01", "dateTo": "2020-01-02"}]

        def dive_annotations(self, dive_id):
            raise OncError("unavailable")

    with pytest.raises(RuntimeError, match="Could not fetch dive 1"):
        AnnotationFetcher(Broken()).fetch(FetchFilters("2020-01-01", "2020-01-02"))


def test_stationary_pagination_honors_page_count_even_for_short_pages():
    pages = []

    class Client:
        def stationary_annotation_page(self, **kwargs):
            pages.append(kwargs["page_num"])
            return {"totalNumOfPages": 2, "naiveAnnotationList": [{"annotationId": pages[-1]}]}

    fetcher = AnnotationFetcher(Client())
    assert len(fetcher._stationary_candidates(1, FetchFilters("2020-01-01", "2020-01-02"))) == 2
    assert pages == [1, 2]


class Response:
    headers = {"Content-Length": "6"}
    status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, **kwargs):
        yield b"abc"
        raise requests.ConnectionError("secret-token")


def test_interrupted_download_never_publishes_partial_file_or_token(tmp_path, monkeypatch):
    client = OncClient("secret-token")
    monkeypatch.setattr(client.session, "get", lambda *a, **kw: Response())
    target = tmp_path / "file.mp4"
    target.write_bytes(b"previous-good-file")
    with pytest.raises(OncError) as err:
        client.download_archive_file("file.mp4", target)
    assert "secret-token" not in str(err.value)
    assert target.read_bytes() == b"previous-good-file"
    assert not target.with_name("file.mp4.part").exists()


def test_json_http_failure_redacts_url(monkeypatch):
    client = OncClient("secret-token")

    def fail(*args, **kwargs):
        raise requests.HTTPError("https://example?token=secret-token",
                                 response=SimpleNamespace(status_code=401))

    monkeypatch.setattr(client.session, "get", fail)
    with pytest.raises(OncError, match="HTTP 401") as err:
        client.list_dives()
    assert "secret-token" not in str(err.value)


def test_planning_unknown_sizes_cache_and_missing_ffmpeg(tmp_path, monkeypatch):
    frames = AnnotationSet([annotation()]).frames()
    client = SimpleNamespace(token=None)
    downloader = ImageDownloader(client, tmp_path)
    assert downloader.plan(frames, check_sizes=False)[0]["download_bytes"] is None
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        downloader.download(frames)
    assert not (tmp_path / "_videos").exists()
    (tmp_path / (frames[0].image_name + ".jpg")).write_bytes(b"already-rendered")
    assert downloader.plan(frames)[0]["download_bytes"] == 0
    assert len(downloader.download(frames)) == 1  # no token/ffmpeg needed for existing outputs


def test_archive_path_cannot_escape_output_directory(tmp_path):
    downloader = ImageDownloader(SimpleNamespace(token=None), tmp_path)
    with pytest.raises(ValueError, match="Invalid archive filename"):
        downloader.plan(AnnotationSet([annotation(archive="../outside.mp4")]).frames())


def test_repeated_selections_preserve_previous_provenance(tmp_path):
    data = AnnotationSet([annotation(), annotation(5, id=2)])
    downloader = ImageDownloader(SimpleNamespace(token=None), tmp_path)
    for frame in data.frames():
        (tmp_path / (frame.image_name + ".jpg")).write_bytes(b"already-rendered")
        downloader.download([frame])
    records = [json.loads(line) for line in (tmp_path / "images_index.jsonl").read_text().splitlines()]
    assert len(records) == 2
    assert {r["annotations"][0]["annotationId"] for r in records} == {1, 2}


def test_facade_reuses_taxonomy_cache_and_has_no_constructor_io(tmp_path):
    workspace = tmp_path / "research"
    with SeaTube(client=OncClient(), data_dir=workspace, resolver=resolver()) as sea:
        assert not workspace.exists()
        assert len(sea.search(AnnotationSet([annotation()]), "crabs")) == 1
        assert sea.available_groups(AnnotationSet([annotation()]))
        assert sea.groups("crab")


def test_cli_remains_available_and_handles_bad_input():
    assert main(["groups"]) == 0
    with pytest.raises(SystemExit) as result:
        main(["extract-clips", "--help"])
    assert result.value.code == 0
    assert main(["fetch", "--start-date", "2020-02-02", "--end-date", "2020-01-01"]) == 1


@pytest.mark.integration
def test_real_ffmpeg_frames_clips_and_repeat_download(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe are not installed")
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=128x96:rate=10", "-t", "10", "-c:v", "libx264",
                    str(source)], check=True)
    calls = []

    class Client:
        token = "fake"

        def download_archive_file(self, name, destination):
            calls.append(name)
            shutil.copyfile(source, destination)

        def archive_file_size(self, name):
            return source.stat().st_size

    data = AnnotationSet([annotation(2), annotation(3, id=2)])
    images = ImageDownloader(Client(), tmp_path / "images")
    rows = images.download(data.frames())
    assert len(rows) == 2 and len(calls) == 1
    assert len(images.download(data.frames())) == 2 and len(calls) == 1
    clips = ClipDownloader(Client(), tmp_path / "clips")
    rows = clips.download(data.clips(before_seconds=1, after_seconds=1))
    assert len(rows) == 1 and len(calls) == 2
    output = tmp_path / "clips" / rows[0]["clip_file"]
    duration = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                        "-of", "default=noprint_wrappers=1:nokey=1", str(output)], text=True)
    assert float(duration) == pytest.approx(3, abs=0.11)
    assert not (tmp_path / "clips" / "_videos").exists()
    record = json.loads((tmp_path / "clips" / "clips_index.jsonl").read_text())
    assert len(record["annotations"]) == 2


def people_data():
    def person(uid, name):
        return {"userId": uid, "firstName": name, "lastName": "", "email": "private@example.test"}
    return AnnotationSet([
        annotation(id=1, createdBy=person(1, "Alex"), modifiedBy=person(2, "Blair")),
        annotation(3, id=2, createdBy=person(1, "Alex renamed"), modifiedBy=person(1, "Alex")),
        annotation(4, id=3, createdBy=person(2, "Blair"), modifiedBy=person(3, "Alex")),
        annotation(5, id=4, createdBy=person(3, "Alex"), modifiedBy=None),
        annotation(6, id=5, createdBy=None, modifiedBy=None),
    ])


def test_people_discovery_uses_stable_ids_and_separates_roles():
    data = people_data()
    authors = data.people_summary()
    assert authors[0] == {"user_id": 1, "name": "Alex", "annotations": 2}
    assert {p["user_id"] for p in authors if p["name"] == "Alex"} == {1, 3}
    editors = {p["user_id"]: p for p in data.people_summary("modifier")}
    assert editors[None]["annotations"] == 2
    assert editors[1]["annotations"] == 1
    assert all("email" not in p for p in authors)
    assert sum(p["annotations"] for p in authors) == len(data)
    assert data.filter(creator_ids=[2]).people_summary() == [
        {"user_id": 2, "name": "Blair", "annotations": 1}]
    with pytest.raises(ValueError, match="last editor"):
        data.people_summary("reviewer")


def test_people_filters_union_within_role_intersect_between_roles():
    data = people_data()
    assert [a.id for a in data.filter(creator_ids=[1, 2])] == [1, 2, 3]
    assert [a.id for a in data.filter(creator_ids=[1, 2], modifier_ids=[2, 3])] == [1, 3]
    assert [a.id for a in data.filter(creator_ids=[1, 2], creator_id=2)] == [3]
    assert [a.id for a in data.filter(modifier_ids=[2], modifier_id=3)] == []
    assert [a.id for a in data.search("crabs", creator_ids=[2], resolver=resolver())] == [3]
    assert [a.id for a in data.filter(modifier_ids=[3])] == [3]


def test_empty_people_selection_never_accidentally_selects_everyone():
    data = people_data()
    assert len(data.filter(creator_ids=None, modifier_ids=None)) == len(data)
    assert not data.filter(creator_ids=[])
    assert not data.filter(modifier_ids=[])
    assert not data.filter(creator_ids=[999])
    assert not data.filter(creator_ids=[]).people_summary()


@pytest.mark.parametrize("values", ["Alex", ["1"], [True], [1, True], [0]])
def test_people_filters_reject_names_and_invalid_ids(values):
    with pytest.raises(ValueError, match="ONC user IDs"):
        people_data().filter(creator_ids=values)
