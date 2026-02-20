# SeaTube Downloader

A command-line Python tool for programmatically fetching annotations and their corresponding video files from Ocean Networks Canada's [SeaTube](https://data.oceannetworks.ca/SeaTube) system, leveraging the Oceans 3.0 API.

## Installation

You will need an ONC API Token. You can generate one from your Ocean Networks Canada profile under the "Web Services API" tab.

```bash
# Clone the repository
git clone <repository_url>
cd seatube-downloader

# Setup a python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the ONC python client library
pip install -r requirements.txt
```

## Usage

This tool utilizes the `STEXPORT` (SeaTube Export) data product from the ONC API to retrieve annotations and media. 

```bash
python download_seatube.py --token YOUR_TOKEN [options]
```

### Options

| Argument | Description | Required | Example |
|---|---|---|---|
| `--token` | Your ONC API Token. | **Yes** | `1234abcd-56ef...` |
| `--taxonomy-id` | Optional taxonomy filter. Set to `1` for the World Register of Marine Species (WoRMS). | No | `1` |
| `--user-id` | Filter annotations created by a specific User ID. | No | `12323` |
| `--dive-id` | Download annotations for specific dive(s). Comma separated. | No | `23331,23332` |
| `--location-code` | Four or five-letter ONC location code (e.g., CBBNC). | No | `CBBNC` |
| `--start-date` | Start date (ISO8601 string) | No | `2023-01-01T00:00:00.000Z` |
| `--end-date` | End date (ISO8601 string) | No | `2023-01-31T23:59:59.000Z` |
| `--include-snapshots` | Pass this flag to include thumbnail PNGs in the STEXPORT. | No | `--include-snapshots` |
| `--download-videos` | **[WIP]** Pass this flag to parse the annotation CSV and initiate downloads of the source `.mp4` video files from the ONC archive. | No | `--download-videos` |

### Examples

**1. Download a generic set of WoRMS (id 1) annotations with thumbnail images across January 2023 at Cascadia Basin:**

```bash
python download_seatube.py \
    --token 77d0e9cf-92dc-4bd8-9a6f-390807e8336d \
    --taxonomy-id 1 \
    --location-code CBBNC \
    --start-date 2023-01-01T00:00:00.000Z \
    --end-date 2023-01-31T23:59:59.000Z \
    --include-snapshots
```

**2. Download WoRMS annotations & video thumbnails from a specific annotator for a specific dive:**

```bash
python download_seatube.py \
    --token YOUR_TOKEN \
    --dive-id 23331 \
    --user-id 1582 \
    --taxonomy-id 1 \
    --include-snapshots
```
