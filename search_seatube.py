import argparse
import sys
import os
import zipfile
import csv
from collections import defaultdict
from onc.onc import ONC

def parse_args():
    parser = argparse.ArgumentParser(description="Search SeaTube for annotated videos and summarize results.",
                                     formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument('--token', type=str, required=True, help="ONC Web Services API User token")
    parser.add_argument('--taxonomy-id', type=int, default=1, help="Taxonomy ID to filter by. Default: 1 (WoRMS)")
    parser.add_argument('--location-code', type=str, help="ONC Location code (e.g. CBBNC)")
    parser.add_argument('--start-date', type=str, required=True, help="ISO8601 start date timestamp (e.g. 2022-01-01T00:00:00.000Z)")
    parser.add_argument('--end-date', type=str, required=True, help="ISO8601 end date timestamp")
    
    return parser.parse_args()

def search_seatube(args):
    # Setup working directory for the search outputs
    output_dir = "search_results"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    onc = ONC(args.token, outPath=output_dir)

    filters = {
        "dataProductCode": "STEXPORT",
        "extension": "csv",
        "taxonomyId": args.taxonomy_id,
        "dateFrom": args.start_date,
        "dateTo": args.end_date
    }

    if args.location_code:
        filters["locationCode"] = args.location_code

    print(f"\n--- Searching annotations from {args.start_date} to {args.end_date} ---")
    print("Ordering STEXPORT from ONC Server. This may take a minute or two depending on the date range...")
    
    try:
        # We order the Data Product
        res = onc.orderDataProduct(filters)
        
        # Look for the downloaded zip file in the results
        zip_path = None
        for result in res.get('downloadResults', []):
            file_name = result.get('file', '')
            if file_name.endswith('.zip'):
                zip_path = os.path.join(output_dir, file_name)
                break
        
        if not zip_path or not os.path.exists(zip_path):
            print("No annotation export ZIP was returned by the API. It is possible no annotations match your search.")
            return

        summarize_results(zip_path)

    except Exception as e:
        print(f"\nAPI Search Error: {e}")
        print("Note: The API may return an error if your search is too broad or if no annotations exist for the period.")

def summarize_results(zip_path):
    print(f"\nSuccessfully downloaded annotations: {zip_path}")
    print("Parsing STEXPORT CSV to summarize available videos...\n")
    
    # We will aggregate to group by Dive, Location, and Video Start/End
    # Key: (Dive ID or LocationCode), Value: Set of video start dates or count
    # Since SeaTube export CSV can vary slightly based on release, we safely search for known columns.
    
    video_summary = defaultdict(lambda: {"annotations": 0, "annotators": set()})

    with zipfile.ZipFile(zip_path, "r") as z:
        for file_name in z.namelist():
            if file_name.endswith(".csv"):
                with z.open(file_name) as f:
                    content = f.read().decode("utf-8-sig").splitlines()
                    reader = csv.DictReader(content)
                    
                    for row in reader:
                        # Depending on the STEXPORT, columns might be named 'Location', 'Dive', 'Start Date', 'User'
                        loc = row.get("Location") or row.get("LocationCode") or "Unknown Location"
                        dive = row.get("Dive") or row.get("Dive ID") or "No Dive ID"
                        start = row.get("Start Date") or row.get("Observation Date") or "Unknown Start"
                        annotator = row.get("Creator") or row.get("User") or "Unknown"
                        
                        group_key = f"Location: {loc} | Dive: {dive} | Observation Around: {start[:10]}"
                        
                        video_summary[group_key]["annotations"] += 1
                        video_summary[group_key]["annotators"].add(annotator)
                        
    if not video_summary:
        print("No annotations found in the CSV!")
        return
        
    print("-" * 80)
    print(f"{'Data Scope':<55} | {'Annotations':<11} | {'Annotators'}")
    print("-" * 80)
    
    # Sort by number of annotations
    sorted_summary = sorted(video_summary.items(), key=lambda x: x[1]['annotations'], reverse=True)
    
    for key, stats in sorted_summary:
        annotator_str = ", ".join(list(stats["annotators"])[:3])
        if len(stats["annotators"]) > 3:
            annotator_str += "..."
            
        print(f"{key:<55} | {stats['annotations']:<11} | {annotator_str}")
        
    print("-" * 80)
    print("\nYou can use these Location/Dive dates with `download_seatube.py` to retrieve the full videos!")


if __name__ == "__main__":
    search_seatube(parse_args())
