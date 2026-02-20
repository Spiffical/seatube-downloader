# SeaTube Downloader

A command-line Python tool suite for programmatically fetching annotations and their corresponding video files from Ocean Networks Canada's [SeaTube](https://data.oceannetworks.ca/SeaTube) system, leveraging the Oceans 3.0 API.

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

### Authentication setup
You can pass your ONC API token directly via the `--token` flag, or you can create a `.env` file in the base of this project and place your token inside it. Start by copying the example environment variable file:

```bash
cp .env.example .env
```

Open `.env` and paste in your token securely.

## Tools

### 1. `search_seatube.py` (Preliminary Search)

Before downloading heavy video files, you can use the `search_seatube.py` script to do a preliminary query. This orders the SeaTube Expert Annotation data over a broad time frame and summarizes which dives, locations, and annotators actually have matching data.

```bash
python search_seatube.py \
    --taxonomy-id 1 \
    --start-date 2023-01-01T00:00:00.000Z \
    --end-date 2023-12-31T23:59:59.000Z
```

This returns a clear terminal breakdown of how many WoRMS annotations were found per location and dive, helping you explicitly identify which parameters to feed to the downloader.

### 2. `download_seatube.py` (Export & Download)

This script utilizes the `STEXPORT` (SeaTube Export) data product from the ONC API to retrieve the actual CSV records and any visual media.

```bash
python download_seatube.py [options]
```

#### Options

| Argument | Description | Required | Example |
|---|---|---|---|
| `--token` | Your ONC API Token. | **No** (if using `.env`) | `1234abcd-56ef...` |
| `--taxonomy-id` | Optional taxonomy filter. Set to `1` for the World Register of Marine Species (WoRMS). | No | `1` |
| `--user-id` | Filter annotations created by a specific User ID. | No | `12323` |
| `--dive-id` | Download annotations for specific dive(s). Comma separated. | No | `23331,23332` |
| `--location-code` | Four or five-letter ONC location code (e.g., CBBNC). | No | `CBBNC` |
| `--start-date` | Start date (ISO8601 string) | No | `2023-01-01T00:00:00.000Z` |
| `--end-date` | End date (ISO8601 string) | No | `2023-01-31T23:59:59.000Z` |
| `--include-snapshots` | Pass this flag to include thumbnail PNGs in the STEXPORT. | No | `--include-snapshots` |
| `--download-videos` | **[WIP]** Pass this flag to parse the annotation CSV and initiate downloads of the source `.mp4` video files from the ONC archive. | No | `--download-videos` |

#### Example

**Download a generic set of WoRMS (id 1) annotations with thumbnail images across January 2023 at Cascadia Basin:**

```bash
python download_seatube.py \
    --taxonomy-id 1 \
    --location-code CBBNC \
    --start-date 2023-01-01T00:00:00.000Z \
    --end-date 2023-01-31T23:59:59.000Z \
    --include-snapshots
```
