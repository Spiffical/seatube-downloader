import argparse
import sys
import os
import json
import csv
from onc.onc import ONC

def parse_args():
    parser = argparse.ArgumentParser(description="Download SeaTube annotations and videos from ONC.")

    # Core requirements API
    parser.add_argument('--token', type=str, required=True, help="ONC Web Services API token")
    
    # Taxonomic and User filtering
    parser.add_argument('--taxonomy-id', type=int, default=1, help="Taxonomy ID (Default 1 for WoRMS)")
    parser.add_argument('--user-id', type=int, help="Annotator User ID")
    
    # Discovery filtering
    parser.add_argument('--dive-id', type=str, help="Comma separated list of integer Dive IDs (e.g. 23331,23332)")
    parser.add_argument('--location-code', type=str, help="Location code (e.g. CBBNC)")
    parser.add_argument('--start-date', type=str, help="ISO8601 start date timestamp (e.g. 2023-01-01T00:00:00.000Z)")
    parser.add_argument('--end-date', type=str, help="ISO8601 end date timestamp")
    
    # Data Product Options (DPO) flags
    parser.add_argument('--include-snapshots', action='store_true', help="Include video thumbnail snapshots in the STEXPORT ZIP.")
    
    # TODO flag
    parser.add_argument('--download-videos', action='store_true', help="Parse the annotations CSV to download matching raw video clips from the archive.")
    
    return parser.parse_args()


def download_seatube_annotations(args, onc: ONC):
    # 1. Build Data Product Options for STEXPORT
    filters = {
        "dataProductCode": "STEXPORT",
        "extension": "csv",
        "taxonomyId": args.taxonomy_id
    }

    if args.user_id:
        filters["userId"] = args.user_id

    if args.dive_id:
        # Pass a list of integers to the API
        filters["diveId"] = [int(i.strip()) for i in args.dive_id.split(',')]

    if args.location_code:
        filters["locationCode"] = args.location_code

    if args.start_date:
        filters["dateFrom"] = args.start_date
        
    if args.end_date:
        filters["dateTo"] = args.end_date

    if args.include_snapshots:
        filters["dpo_includeVideoSnapshots"] = 1

    print("\n--- Requesting STEXPORT Data Product Settings ---")
    print(json.dumps(filters, indent=2))
    print("-------------------------------------------------")
    
    try:
        # Request, run, and download the data product ZIP to our output folder
        res = onc.orderDataProduct(filters)
        print("Data product downloaded successfully!")
        
        # Determine the downloaded folder path that onc library returned
        if not hasattr(onc, 'outPath'):
            print("Note: Output path configuration check failed.")
            return []

        # Return the location of the result
        return res['downloadResults']

    except Exception as e:
        print(f"\nAPI Request Error: {e}")
        print("Ensure your filters actually yield files or that your Token is valid.")
        sys.exit(1)


def parse_and_download_videos(download_results, onc: ONC):
    """
    (WIP Function)
    If the --download-videos flag is set, this reads the CSV and pulls the original videos.
    """
    # Look for the STEXPORT CSV within the downloaded ZIP package outputs...
    print("\nParsing Annotation CSV to trigger video downloads...")
    print("WIP - Video pulling logic will depend on how you want to download MP4 clips from the archive.")
    

def main():
    args = parse_args()

    # Set up our working directory for outputs
    output_dir = "downloads"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Initialize ONC Connection
    onc = ONC(args.token, outPath=output_dir)
    
    # 1. Execute SeaTube Export (STEXPORT) Download
    results = download_seatube_annotations(args, onc)

    # 2. Iterate annotations and download videos (MP4 Archivefiles)
    if args.download_videos and results:
        parse_and_download_videos(results, onc)
        
    print("\nDone!")

if __name__ == "__main__":
    main()
