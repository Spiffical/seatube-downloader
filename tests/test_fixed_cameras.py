"""Offline regressions for fixed (stationary) cameras, using a fake ONC client."""
import pytest

from seatube import AnnotationSet, FetchFilters, OncClient, OncError, SeaTube, WormsResolver
from seatube.cli import main
from seatube.fetch import AnnotationFetcher, cameras_in_range, fixed_camera_locations
from seatube.archive import parse_iso_utc

DAY0 = 1577836800  # 2020-01-01T00:00:00Z

TREE = {"html": "Fixed Cameras", "searchTreeNodeId": 11, "children": [
    {"html": "Pacific", "searchTreeNodeId": 1, "children": [
        # A site with its own camera *and* a sub-site, like Barkley Canyon Mid-East.
        {"html": "Canyon East", "searchTreeNodeId": 18, "wasDeviceFound": True,
         "latitude": 48.3, "longitude": -126.0, "siteDepth": 894.0, "children": [
             {"html": "Wally Land", "searchTreeNodeId": 19, "children": [
                 {"html": "Front Camera", "searchTreeNodeId": 3450, "wasDeviceFound": True}]}]},
        {"html": "Canyon Hydrates", "searchTreeNodeId": 16, "children": [
            {"html": "Wally Land", "searchTreeNodeId": 159, "children": [
                {"html": "Front Camera", "searchTreeNodeId": 160, "wasDeviceFound": True}]}]},
        {"html": "Inlet", "searchTreeNodeId": 2666, "wasDeviceFound": True,
         "latitude": 49.15, "longitude": -124.8, "siteDepth": 113.0},
    ]}]}
CODES = {18: "BACME", 3450: "BCMEW.V1", 160: "BACWL.V1", 2666: "CCIP"}


def taxon(name="Brachyura", aphia=106673):
    return {"taxonomyCode": "WoRMS", "taxonomyId": 1, "taxonId": 5, "taxonName": name,
            "referenceId": str(aphia)}


def located(aid, when, *, device="CAM1", taxons=None, comment=""):
    """One record as ONC's location annotation service returns it."""
    return {"annotationId": aid, "deviceCode": device, "dateFrom": when, "dateTo": when,
            "createdBy": {"userCode": "42", "firstName": "Ana", "lastName": "Diaz"},
            "modifiedBy": {"userCode": "7", "firstName": "Bo", "lastName": "Li"},
            "toBeReviewed": False, "comment": comment,
            "taxons": [taxon()] if taxons is None else taxons, "resourceTypeId": 1000}


def series(device_id, code, start, postfix=".mp4"):
    # dataFiles rows: [offset, duration, path, row_start_offset]
    return {"deviceId": device_id, "deviceCode": code, "dateStartSeconds": start,
            "defaultFileNamePostFix": postfix, "count": 1, "dataFiles": [["0", "300", "/p/", "0"]]}


class FakeOnc:
    def __init__(self, annotations=None, media=None, video_errors=None):
        self.annotations = annotations or {}
        self.media = media or {}
        self.video_errors = video_errors or {}
        self.calls = []

    def close(self):
        pass

    def fixed_camera_tree(self):
        return TREE

    def fixed_camera_details(self, node_id):
        return {"stationCode": CODES.get(node_id)}

    def location_annotations(self, code, start, end):
        self.calls.append(("annotations", code, start, end))
        return self.annotations.get(code, [])

    def stationary_video_metadata(self, node_id, resolution):
        self.calls.append(("video", node_id))
        if node_id in self.video_errors:
            raise self.video_errors[node_id]
        return {"mediaFiles": self.media.get(node_id, [])}


def inlet_onc():
    return FakeOnc(
        annotations={"CCIP": [
            located(1, "2020-01-01T00:01:00.000Z"),                     # first deployment
            located(2, "2020-01-02T00:02:00.000Z"),                     # second deployment
            located(3, "2020-01-01T01:01:00.000Z"),                     # only CAM2 recorded then
            located(4, "2020-01-01T00:03:00.000Z", taxons=[], comment="current l to r"),
            located(5, "2020-01-05T00:00:00.000Z"),                     # outside the range
        ]},
        media={2666: [series(24164, "CAM1", DAY0), series(99, "CAM2", DAY0 + 3600),
                      series(24164, "CAM1", DAY0 + 86400)]},
    )


def test_tree_lists_sites_with_subsites_and_labels_them_uniquely():
    sites = {s["searchTreeNodeId"]: s for s in fixed_camera_locations(FakeOnc())}
    assert set(sites) == {18, 3450, 160, 2666}  # 18 has a sub-site but its own camera too
    assert sites[18]["label"] == "Canyon East"
    assert sites[3450]["label"] == "Canyon East > Wally Land > Front Camera"
    assert sites[160]["label"] == "Canyon Hydrates > Wally Land > Front Camera"
    assert sites[2666]["siteDepth"] == 113.0


def test_node_ids_alone_fetch_fixed_cameras_and_map_every_deployment():
    onc = inlet_onc()
    data = AnnotationFetcher(onc).fetch(FetchFilters(
        "2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z", node_ids={2666}))  # FakeOnc has no dives
    by_id = {a.id: a for a in data}
    assert set(by_id) == {1, 2, 3}  # comment-only and out-of-range records are dropped
    assert by_id[1].archive_filename == "CAM1_20200101T000000.000Z.mp4"
    assert by_id[2].archive_filename == "CAM1_20200102T000000.000Z.mp4"
    assert by_id[2].offset_in_file_seconds() == 120
    # Another camera's recording is never borrowed.
    assert by_id[3].archive_filename is None
    assert by_id[3].raw["videoMappingStatus"] == "no_containing_data_file"

    first = by_id[1]
    assert first.camera_mode == "stationary"
    assert first.place == "Inlet" and first.location_code == "CCIP"
    assert first.depth_m == 113.0 and first.lat == 49.15
    assert first.creator_id == 42 and first.creator_name == "Ana Diaz" and first.modifier_id == 7
    assert first.raw["deviceId"] == 24164
    assert first.seatube_link.endswith("/app/fixed-camera-locations/2666?time=2020-01-01T00:01:00.000Z")
    assert onc.calls[0] == ("annotations", "CCIP", "2020-01-01T00:00:00.000Z", "2020-01-03T00:00:00.000Z")
    row = data.flatten()[0]
    assert row["stationary_location_code"] == "CCIP" and row["camera_mode"] == "stationary"


def test_fixed_camera_results_work_with_offline_search_people_and_depth():
    data = AnnotationFetcher(inlet_onc()).fetch(FetchFilters(
        "2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z", location_contains="ccip"))
    resolver = WormsResolver(fetcher=lambda aid: ["Animalia", "Arthropoda", "Brachyura"])
    crabs = data.search("crabs", resolver=resolver, min_depth_m=100, max_depth_m=200)
    assert len(crabs) == 3
    assert crabs.people_summary() == [{"user_id": 42, "name": "Ana Diaz", "annotations": 3}]
    assert len(crabs.filter(creator_ids=[42], location_contains="Inlet")) == 3
    assert len(crabs.frames()) == 2 and len(crabs.clips()) == 2


def test_site_without_seatube_video_stays_unmapped_but_other_failures_raise():
    annotations = {"BACWL.V1": [located(9, "2020-01-01T00:01:00.000Z")]}
    no_video = OncError("Invalid search tree node id: 160", status_code=9001)
    data = AnnotationFetcher(FakeOnc(annotations, video_errors={160: no_video})).fetch(
        FetchFilters("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", node_ids={160}))
    assert data[0].raw["videoMappingStatus"] == "no_media_for_device"
    assert data.summary()["places"] == ["Canyon Hydrates > Wally Land > Front Camera"]

    broken = FakeOnc(annotations, video_errors={160: OncError("HTTP 503")})
    with pytest.raises(RuntimeError, match="video metadata for fixed camera"):
        AnnotationFetcher(broken).fetch(
            FetchFilters("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", node_ids={160}))


def test_unknown_node_ids_raise_instead_of_returning_nothing():
    with pytest.raises(ValueError, match="Unknown fixed-camera node id"):
        AnnotationFetcher(FakeOnc()).fetch(
            FetchFilters("2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", node_ids={12345}))


def test_camera_mode_follows_the_selection_and_rejects_contradictions():
    window = ("2020-01-01", "2020-01-02")
    assert FetchFilters(*window).camera_mode == "dive"
    assert FetchFilters(*window, node_ids={1}).camera_mode == "stationary"
    assert FetchFilters(*window, location_contains="Inlet").camera_mode == "stationary"
    assert FetchFilters(*window, dive_ids={1}, node_ids={2}).camera_mode == "both"
    assert FetchFilters(*window, camera_mode="stationary").camera_mode == "stationary"
    with pytest.raises(ValueError, match="select fixed cameras"):
        FetchFilters(*window, camera_mode="dive", node_ids={1})
    with pytest.raises(ValueError, match="select ROV dives"):
        FetchFilters(*window, camera_mode="stationary", dive_ids={1})


def test_camera_discovery_counts_worms_labels_and_hides_empty_sites():
    onc = inlet_onc()
    start, end = parse_iso_utc("2020-01-01T00:00:00Z"), parse_iso_utc("2020-01-03T00:00:00Z")
    rows = cameras_in_range(onc, start, end)
    assert [(r["searchTreeNodeId"], r["locationCode"], r["annotations"], r["worms_annotations"])
            for r in rows] == [(2666, "CCIP", 4, 3)]
    assert rows[0]["first_utc"] == "2020-01-01T00:01:00.000Z"
    assert len(cameras_in_range(onc, start, end, include_empty=True)) == 4
    with SeaTube(client=onc, resolver=WormsResolver(offline=True)) as sea:
        assert sea.cameras("2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z")[0]["label"] == "Inlet"
        with pytest.raises(ValueError):
            sea.cameras("2020-01-03T00:00:00Z", "2020-01-01T00:00:00Z")


def test_location_service_errors_are_never_read_as_empty_results(monkeypatch):
    client = OncClient("secret-token")
    sent = {}

    def get_json(path, params):
        sent.update(params, path=path)
        return {"statusCode": 0, "payload": {"errors": [
            {"errorMessage": "Invalid parameter locationCode. No data found for value NOPE"}]}}

    monkeypatch.setattr(client, "get_json", get_json)
    with pytest.raises(OncError, match="No data found"):
        client.location_annotations("NOPE", "2020-01-01", "2020-01-02T00:00:00Z")
    # The service rejects anything but millisecond UTC timestamps.
    assert sent["dateFrom"] == "2020-01-01T00:00:00.000Z"
    assert sent["dateTo"] == "2020-01-02T00:00:00.000Z"


def test_saved_fixed_camera_annotations_filter_by_location_code(tmp_path):
    data = AnnotationFetcher(inlet_onc()).fetch(FetchFilters(
        "2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z", node_ids={2666}))
    data.save(tmp_path / "annotations.json")
    loaded = AnnotationSet.load(tmp_path / "annotations.json")
    assert len(loaded.filter(location_contains="CCIP", camera_mode="stationary")) == 3
    assert not loaded.filter(camera_mode="dive")


def test_cli_rejects_dive_mode_with_fixed_camera_ids(capsys):
    assert main(["fetch", "--start-date", "2020-01-01", "--end-date", "2020-01-02",
                 "--camera-mode", "dive", "--node-id", "2666", "--token", "x"]) == 1
    assert "select fixed cameras" in capsys.readouterr().err
    with pytest.raises(SystemExit) as result:
        main(["cameras", "--help"])
    assert result.value.code == 0
