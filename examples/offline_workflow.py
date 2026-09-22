"""A runnable, network-free introduction using synthetic observations.

Run after installing the package: python examples/offline_workflow.py
No ONC token, video, ffmpeg, pandas, or Jupyter installation is required.
"""
from pathlib import Path

from seatube import AnnotationSet, OncClient, SeaTube, WormsResolver

DATA = Path(__file__).resolve().parent / "data"


def main():
    annotations = AnnotationSet.load(DATA / "annotations.json")
    resolver = WormsResolver(DATA / "lineages.json", offline=True)
    # An explicit unauthenticated client keeps .env credentials out of this demo.
    with SeaTube(client=OncClient(), resolver=resolver) as sea:
        print("SYNTHETIC EXAMPLE — not observed ONC data")
        print(annotations.summary())
        print("\nGroups represented in this example:")
        for group in sea.available_groups(annotations):
            print(group["group"], group["annotations"], group["mapped_annotations"])
        crabs = sea.search(annotations, "crabs")
        print("\nCrabs:", crabs.summary())
        print("Frame plan:", sea.image_downloader().plan(
            crabs.frames(max_images=2, max_videos=1), check_sizes=False))
        print("Clip plan:", crabs.clips(before_seconds=1, after_seconds=2, max_clips=2))
        assert len(crabs) == 4
        assert crabs.summary()["mapped_annotations"] == 3
        assert len(sea.search(annotations, "true-crabs")) == 3


if __name__ == "__main__":
    main()
