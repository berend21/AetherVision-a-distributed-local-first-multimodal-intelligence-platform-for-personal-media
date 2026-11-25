
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("imagesorter.google_photos")


def find_google_json(image_path: Path) -> Path | None:

    image_path = Path(image_path)

    candidates = [
        Path(str(image_path) + ".json"),
        Path(str(image_path) + ".supplemental-metadata.json"),
        image_path.with_suffix(".json"),
    ]

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None

def extract_google_metadata(image_path: Path) -> dict:

    result = {}
    json_path = find_google_json(image_path)

    if not json_path:
        return result

    try:
        with json_path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)

        # Description and title
        if data.get("description"):
            result["description"] = str(data["description"]).strip()

        if data.get("title"):
            result["title"] = str(data["title"]).strip()

        # Convert Google Unix timestamps to ISO format
        def parse_timestamp(value):
            if not value:
                return None

            try:
                dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
                return dt.replace(tzinfo=None).isoformat(timespec="seconds")
            except (ValueError, TypeError, OverflowError, OSError):
                return None

        # Original photo date
        taken = data.get("photoTakenTime") or {}
        result["date"] = parse_timestamp(taken.get("timestamp"))

        # Google Photos creation date
        creation = data.get("creationTime") or {}
        result["creation_time"] = parse_timestamp(creation.get("timestamp"))

        # GPS coordinates
        for geo_key in ("geoData", "geoDataExif"):

            geo = data.get(geo_key) or {}

            try:
                lat = float(geo.get("latitude", 0))
                lon = float(geo.get("longitude", 0))

                if (-90 <= lat <= 90 and -180 <= lon <= 180 and (lat != 0 or lon != 0)):
                    result["gps_lat"] = lat
                    result["gps_lon"] = lon

                    if geo.get("altitude") is not None:
                        result["gps_altitude"] = float(geo["altitude"])
                    break

            except (ValueError, TypeError):
                continue

        result["metadata_source"] = "google_photos"
        return result

    except (OSError, json.JSONDecodeError, UnicodeError) as e:
        logger.warning("Google Photos JSON read failed for %s: %s", json_path, e)
        return {}


def merge_metadata(exif_data: dict, google_data: dict) -> dict:
    result = dict(exif_data)
    field_mapping = {
        "date": "date",
        "gps_lat": "gps_lat",
        "gps_lon": "gps_lon",
        "gps_altitude": "gps_altitude",
        "description": "google_description",
        "title": "google_title",
    }

    for source_key, target_key in field_mapping.items():
        if result.get(target_key) is None or result.get(target_key) == "":
            if google_data.get(source_key) is not None:
                result[target_key] = google_data[source_key]

    if google_data:
        result["metadata_source"] = ("merged" if exif_data else "google_photos")

    return result

def extract_and_merge(image_path: Path, exif_data: dict = None) -> dict:
    google_data = extract_google_metadata(image_path)
    return merge_metadata(exif_data or {}, google_data)
