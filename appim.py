import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from io import BytesIO
from PIL import Image, ExifTags, ImageFile, ImageOps
from flask import Flask, request, jsonify, Response, render_template, send_file, send_from_directory, abort
from threading import Thread, Lock, Event, BoundedSemaphore
import logging
import time
import os
from flask_wtf.csrf import CSRFProtect, CSRFError
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import ctypes
from flask_wtf.csrf import generate_csrf
from scripts.location_resolver import LocationResolver
import database
import subprocess


from database import (get_db, LRUCache, DatabasePool, init_db, safe_json_load, extract_searchable_json, build_fts_query, now_iso, update_photo_fts, db_lock, thumb_cache)
from google_photos_metadata import extract_and_merge

FFPROBE_PATH = r"H:\Imagesorter\ffmpeg-2026-11-19-git-01ca75d350-full_build\ffmpeg-2026-11-19-git-01ca75d350-full_build\bin\ffprobe.exe"
FFMPEG_PATH = r"H:\Imagesorter\ffmpeg-2026-11-19-git-01ca75d350-full_build\ffmpeg-2026-11-19-git-01ca75d350-full_build\bin\ffmpeg.exe"
if not os.path.isfile(FFPROBE_PATH):
    raise FileNotFoundError(f"FFprobe not found: {FFPROBE_PATH}")

if not os.path.isfile(FFMPEG_PATH):
    raise FileNotFoundError(f"FFmpeg not found: {FFMPEG_PATH}")


print("FFprobe:", FFPROBE_PATH)
print("FFmpeg:", FFMPEG_PATH)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("imagesorter")

load_dotenv(Path(__file__).resolve().parent / ".env")
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tiff'}
VIDEO_EXTENSIONS = {'.mp4','.mov','.avi', '.mkv', '.webm', '.m4v','.wmv', '.3gp'}
THUMB_SIZE = (300, 300)
WEBP_QUALITY = 75
PER_PAGE = 100
INTERVAL_SCAN = 900
Image.MAX_IMAGE_PIXELS = 40_000_000
ImageFile.LOAD_TRUNCATED_IMAGES = False
INDEX_WORKERS = int(os.environ.get("INDEX_WORKERS", max(1, min(2, os.cpu_count() or 2))))
PMTILES_FILE = r"H:\Imagesorter\images\data\world-europe.pmtiles"




def extract_dominant_color(img: Image.Image) -> str:
    try:
        small = img.copy()
        small.thumbnail((32, 32), Image.Resampling.BILINEAR)

        if small.mode != 'RGB':
            small = small.convert('RGB')

        pixels = list(small.getdata())

        if not pixels:
            return "#1a1a1a"

        avg_r = sum(p[0] for p in pixels) // len(pixels)
        avg_g = sum(p[1] for p in pixels) // len(pixels)
        avg_b = sum(p[2] for p in pixels) // len(pixels)

        small.close()

        return f"#{avg_r:02x}{avg_g:02x}{avg_b:02x}"

    except Exception:
        return "#1a1a1a"



def extract_exif_safe(img: Image.Image) -> dict:
    data = {}
    try:
        exif = img.getexif()
        if not exif:
            return data

        for tag_id in [36867, 36868, 306]:
            try:
                val = exif.get(tag_id)
                if val:
                    if isinstance(val, bytes):
                        val = val.decode('utf-8', errors='ignore')
                    val = str(val).strip().replace('\x00', '')
                    if val and len(val) >= 19:
                        data['date'] = datetime.strptime(val[:19], "%Y:%m:%d %H:%M:%S").isoformat()
                        break
            except Exception as e:
                print(f"Something went wrong: {e}")

        try:
            make = exif.get(271, '')
            model = exif.get(272, '')
            if isinstance(make, bytes):
                make = make.decode('utf-8', errors='ignore')
            if isinstance(model, bytes):
                model = model.decode('utf-8', errors='ignore')
            make = str(make).strip().replace('\x00', '')
            model = str(model).strip().replace('\x00', '')
            if make or model:
                data['camera'] = f"{make} {model}".strip()
        except Exception as e:
            print(f"Something went wrong: {e}")

        try:
            if hasattr(ExifTags, 'IFD'):
                gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
                if gps and 2 in gps and 4 in gps:
                    def to_dec(coords, ref):
                        if isinstance(ref, bytes):
                            ref = ref.decode('ascii', errors='ignore')
                        ref = str(ref).strip().upper()
                        d, m, s = [float(x) for x in coords]
                        dec = d + m / 60 + s / 3600
                        return (
                            -dec
                            if ref in ('S', 'W')
                            else dec)
                    data['gps_lat'] = to_dec(gps[2], gps.get(1, 'N'))
                    data['gps_lon'] = to_dec(gps[4], gps.get(3, 'E'))
        except Exception as e:
            print(f"Something went wrong: {e}")
    except Exception as e:
        print(f"Something went wrong: {e}")

    return data



def make_thumbnail_webp(img):
    thumb = img.copy()

    try:
        thumb.thumbnail(
            THUMB_SIZE,
            Image.Resampling.LANCZOS
        )

        if thumb.mode != 'RGB':
            converted = thumb.convert('RGB')
            thumb.close()
            thumb = converted

        buf = BytesIO()

        thumb.save(
            buf,
            format='WEBP',
            quality=WEBP_QUALITY,
            method=4
        )

        return buf.getvalue()

    finally:
        thumb.close()


def make_thumbnail_data(img):
    thumb = img.copy()
    rgb = None
    small = None

    try:
        thumb.thumbnail(
            THUMB_SIZE,
            Image.Resampling.LANCZOS
        )

        if thumb.mode != 'RGB':
            rgb = thumb.convert('RGB')
            thumb.close()
            thumb = rgb
            rgb = None

        small = thumb.copy()
        small.thumbnail(
            (32, 32),
            Image.Resampling.BILINEAR
        )

        pixels = list(small.getdata())

        if pixels:
            count = len(pixels)

            avg_r = sum(p[0] for p in pixels) // count
            avg_g = sum(p[1] for p in pixels) // count
            avg_b = sum(p[2] for p in pixels) // count

            dominant_color = (
                f"#{avg_r:02x}{avg_g:02x}{avg_b:02x}"
            )
        else:
            dominant_color = "#1a1a1a"

        buf = BytesIO()

        thumb.save(
            buf,
            format='WEBP',
            quality=WEBP_QUALITY,
            method=4
        )

        return buf.getvalue(), dominant_color

    finally:
        if small is not None:
            small.close()

        if rgb is not None:
            rgb.close()

        thumb.close()



def safe_read_file(filepath: str) -> tuple[bytes | None, str | None]:

    path = Path(filepath)

    try:
        if path.exists() and path.is_file():
            return path.read_bytes(), None
    except OSError:
        pass

    try:
        with open(filepath, 'rb') as f:
            data = f.read()
        return data, None
    except OSError:
        pass

    if os.name == 'nt':
        try:

            buf = ctypes.create_unicode_buffer(512)
            result = ctypes.windll.kernel32.GetShortPathNameW(
                str(path), buf, 512
            )
            if result > 0:
                short_path = buf.value
                with open(short_path, 'rb') as f:
                    data = f.read()
                return data, None
        except Exception:
            pass

    if os.name == 'nt':
        try:
            abs_path = os.path.abspath(filepath)
            if not abs_path.startswith('\\\\?\\'):
                abs_path = '\\\\?\\' + abs_path
            with open(abs_path, 'rb') as f:
                data = f.read()
            return data, None
        except OSError:
            pass

    try:
        chunks = []
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                chunks.append(chunk)
        if chunks:
            return b''.join(chunks), None
    except OSError:
        pass

    return None, f"Cannot read file: {filepath}"


def safe_check_exists(filepath: str) -> bool:
    """Check if file exists, handling Windows path edge cases."""
    try:
        return os.path.isfile(filepath)
    except (OSError, ValueError):
        pass

    try:
        return Path(filepath).is_file()
    except (OSError, ValueError):
        pass

    if os.name == 'nt':
        try:
            abs_path = os.path.abspath(filepath)
            if not abs_path.startswith('\\\\?\\'):
                abs_path = '\\\\?\\' + abs_path
            return os.path.isfile(abs_path)
        except (OSError, ValueError):
            pass

    return False


class Indexer:
    def __init__(self, folder: Path, db_path: Path, location_resolver):
        self.folder = folder
        self.db_path = db_path
        self.location_resolver = location_resolver
        self._running = False
        self._stop_event = Event()
        self._scan_lock = Lock()
        self._last_scan_complete = False
        self._filename_lock = Lock()
        self._reserved_filenames = set()

    def scan_folder(self) -> list:
        images = []
        self._last_scan_complete = True

        def scan_dir(path):
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                scan_dir(entry.path)

                            elif entry.is_file(follow_symlinks=False):
                                ext = os.path.splitext(entry.name)[1].lower()

                                if ext in IMAGE_EXTENSIONS:
                                    images.append(Path(entry.path))

                        except OSError as e:
                            self._last_scan_complete = False
                            print(
                                f"  Scan warning: cannot inspect "
                                f"{entry.path}: {e}"
                            )

            except OSError as e:
                self._last_scan_complete = False
                print(f"  Scan warning: cannot read directory {path}: {e}")

        if not self.folder.is_dir():
            self._last_scan_complete = False
            print(f"  Scan failed: folder unavailable: {self.folder}")
            return []

        scan_dir(self.folder)

        if not self._last_scan_complete:
            print(
                "  WARNING: Scan incomplete. "
                "Missing database records will NOT be removed."
            )

        return sorted(images)

    def get_indexed(self) -> dict:
        rows = database.db_pool.execute(
            "SELECT id, file_hash, filepath, file_size, file_modified FROM photos"
        )
        return {
            r['filepath']: {
                'id': r['id'],
                'file_hash': r['file_hash'],
                'file_size': r['file_size'],
                'file_modified': r['file_modified']
            }
            for r in rows
        }

    def process_new_image(self, path: Path):
        try:

            file_data, error = safe_read_file(str(path))

            if file_data is None:
                raise OSError(error)

            fhash = hashlib.sha256(file_data).hexdigest()

            return self.process_image(path, file_data, fhash), None

        except Exception as e:
            return None, (path.name, str(e))


    def process_images_parallel(self, paths):

        results = []
        errors = []

        if not paths:
            return results, errors

        batch_size = max(INDEX_WORKERS * 4, 4)

        with ThreadPoolExecutor(
            max_workers=INDEX_WORKERS,
            thread_name_prefix="ImageWorker"
        ) as executor:

            for start in range(0, len(paths), batch_size):

                batch = paths[start:start + batch_size]

                futures = {
                    executor.submit(
                        self.process_new_image,
                        path
                    ): path
                    for path in batch
                }

                for future in as_completed(futures):

                    path = futures[future]

                    try:
                        result, error = future.result()

                        if error:
                            errors.append(error)

                        elif result:
                            results.append(result)

                    except Exception as e:

                        errors.append(
                            (path.name, str(e))
                        )

        return results, errors


    def process_image(
        self,
        path: Path,
        file_data: bytes = None,
        fhash: str = None
    ) -> tuple:

        if file_data is None:
            file_data = path.read_bytes()

        if fhash is None:
            fhash = hashlib.sha256(file_data).hexdigest()

        stat = path.stat()


        exif = {}
        location = None

        with Image.open(BytesIO(file_data)) as original:

            original.load()

            exif = extract_exif_safe(original)
            #if no exif check if jsonfile
            exif = extract_and_merge(path, exif)

            if (
                exif.get('gps_lat') is not None
                and exif.get('gps_lon') is not None
            ):
                try:
                    location = self.location_resolver.cached_reverse_geocode(
                        exif['gps_lat'],
                        exif['gps_lon']
                    )
                except Exception:
                    logger.exception(
                        "Reverse geocoding failed: %s",
                        path.name
                    )

            oriented = ImageOps.exif_transpose(original)

            try:
                thumb_webp, dominant_color = make_thumbnail_data(
                    oriented
                )

                width, height = oriented.size

            finally:
                if oriented is not original:
                    oriented.close()

        unique_filename = self.get_unique_filename(
        path.name,
        str(path.absolute())
    )

        data = (
            unique_filename,
            str(path.absolute()),
            stat.st_size,
            datetime.fromtimestamp(stat.st_mtime).isoformat(),
            width,
            height,
            exif.get('date'),
            exif.get('camera'),
            exif.get('gps_lat'),
            exif.get('gps_lon'),
            thumb_webp,
            dominant_color,
            now_iso(),

            location.get('city') if location else None,
            location.get('subdivision') if location else None,
            location.get('region') if location else None,
            location.get('country') if location else None,
            location.get('country_code') if location else None,
            location.get('distance_km') if location else None,
            location.get('location_type') if location else None,
            now_iso() if location else None
        )

        return fhash, data
    def quick_scan(self, show_progress=True):
        if not self._scan_lock.acquire(blocking=False):
            if show_progress:
                print("  Scan already running; skipping.")
            return 0

        try:
            return self._quick_scan_impl(show_progress)
        finally:
            self._scan_lock.release()


    def _quick_scan_impl(self, show_progress=True):
        scan_started = time.perf_counter()
        with self._filename_lock:
            self._reserved_filenames.clear()
        db_started = time.perf_counter()
        existing = self.get_indexed()
        existing_count = len(existing)
        db_time = time.perf_counter() - db_started

        folder_started = time.perf_counter()
        images = self.scan_folder()
        folder_count = len(images)
        folder_time = time.perf_counter() - folder_started

        print(
            f"  Timing: DB={db_time:.2f}s | "
            f"folder={folder_time:.2f}s"
        )

        scan_time = time.perf_counter() - scan_started

        if show_progress:
            print(
                f"  Found {folder_count:,} images "
                f"in {scan_time:.2f}s"
            )


        if show_progress:
            print(f"  Database: {existing_count} | Folder: {folder_count}")

        new_images = []
        new_image_paths = []

        found_paths = set()
        skipped = []

        compare_started = time.perf_counter()
        for i, path in enumerate(images):
            try:
                path_str = str(path.absolute())
                found_paths.add(path_str)
                stat = path.stat()
                file_mtime = datetime.fromtimestamp(stat.st_mtime).isoformat()

                if path_str in existing:
                    info = existing[path_str]
                    if info['file_size'] == stat.st_size and info['file_modified'] == file_mtime:
                        continue
                    file_data, error = safe_read_file(str(path))

                    if file_data is None:
                        raise OSError(error)
                    fhash = hashlib.sha256(file_data).hexdigest()

                    if fhash == info['file_hash']:
                        database.db_pool.execute_write(
                            """
                            UPDATE photos
                            SET file_modified=?, file_size=?
                            WHERE id=?
                            """,
                            (file_mtime, stat.st_size, info['id'])
                        )
                    else:
                        _, data = self.process_image(
                            path,
                            file_data=file_data,
                            fhash=fhash
                        )


                        with db_lock:
                            conn = get_db(self.db_path)
                            try:
                                conn.execute("""
                                    UPDATE photos
                                    SET
                                        file_hash=?,
                                        filename=?,
                                        filepath=?,
                                        file_size=?,
                                        file_modified=?,
                                        width=?,
                                        height=?,
                                        date_taken=?,
                                        exif_camera=?,
                                        exif_gps_lat=?,
                                        exif_gps_lon=?,
                                        thumb_webp=?,
                                        dominant_color=?,
                                        indexed_at=?,
                                        city_name=?,
                                        subdivision_name=?,
                                        region_name=?,
                                        country_name=?,
                                        country_code=?,
                                        location_distance_km=?,
                                        location_type=?,
                                        location_indexed_at=?
                                    WHERE id=?
                                """, (
                                    fhash,
                                    data[0],
                                    data[1],
                                    data[2],
                                    data[3],
                                    data[4],
                                    data[5],
                                    data[6],
                                    data[7],
                                    data[8],
                                    data[9],
                                    data[10],
                                    data[11],
                                    data[12],
                                    data[13],
                                    data[14],
                                    data[15],
                                    data[16],
                                    data[17],
                                    data[18],
                                    data[19],
                                    data[20],
                                    info['id']
                                ))

                                try:
                                    update_photo_fts(conn, info['id'])
                                except Exception as e:
                                    print(f"FTS update warning: {e}")

                            finally:
                                conn.close()

                        thumb_cache.delete(f"thumb_{info['id']}_webp")


                        if show_progress:
                            print(f"    Updated: {path.name}")

                else:
                    new_image_paths.append(path)


            except Exception as e:
                skipped.append((path.name, str(e)))
                if show_progress:
                    print(f"    Error: {path.name} - {e}")

            if show_progress and (i + 1) % 500 == 0 and not new_images:
                print(f"    Checked {i + 1}/{folder_count}...")
        compare_time = time.perf_counter() - compare_started

        if new_image_paths:
            process_started = time.perf_counter()

            if show_progress:
                print(
                    f"  Processing {len(new_image_paths):,} new images "
                    f"with {INDEX_WORKERS} workers..."
                )

            processed_results, processing_errors = self.process_images_parallel(
                new_image_paths
            )

            new_images.extend(processed_results)
            skipped.extend(processing_errors)

            process_time = time.perf_counter() - process_started

            if show_progress:
                print(
                    f"  Parallel processing: "
                    f"{len(processed_results):,} images "
                    f"in {process_time:.2f}s"
                )


        print(
            f"  Timing: comparison={compare_time:.2f}s"
        )

        if show_progress and skipped:
            print(f"    Skipped {len(skipped)} files")

        new_count = 0
        removed = 0
        print(f"  Preparing to write {len(new_images)} new images to database...")


        removed_paths = (
            set(existing.keys()) - found_paths
            if self._last_scan_complete
            else set()
        )

        if new_images or removed_paths:
            with db_lock:
                conn = get_db(self.db_path)

                try:
                    conn.execute("BEGIN")


                    if self._last_scan_complete:
                        for path_str, info in existing.items():
                            if path_str not in found_paths:
                                conn.execute(
                                    "DELETE FROM photos WHERE id=?",
                                    (info['id'],)
                                )

                                try:
                                    conn.execute(
                                        "DELETE FROM photos_fts WHERE rowid=?",
                                        (info['id'],)
                                    )
                                except Exception as e:
                                    print(f"    FTS delete warning: {e}")

                                removed += 1
                    else:
                        print(
                            "    Skipping removal of missing records "
                            "because the scan was incomplete."
                        )

                    for fhash, data in new_images:
                        try:
                            cursor = conn.execute("""
                                INSERT INTO photos (
                                    file_hash,
                                    filename,
                                    filepath,
                                    file_size,
                                    file_modified,
                                    width,
                                    height,
                                    date_taken,
                                    exif_camera,
                                    exif_gps_lat,
                                    exif_gps_lon,
                                    thumb_webp,
                                    dominant_color,
                                    indexed_at,
                                    city_name,
                                    subdivision_name,
                                    region_name,
                                    country_name,
                                    country_code,
                                    location_distance_km,
                                    location_type,
                                    location_indexed_at
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (fhash,) + data)

                            photo_id = cursor.lastrowid

                            update_photo_fts(conn, photo_id)

                            new_count += 1

                        except Exception as e:
                            print(f"DB insert error: {data[0]} - {e}")
                            raise

                    conn.execute("COMMIT")
                    check = conn.execute("""
                        SELECT COUNT(*) AS cnt,
                            COUNT(filename) AS filenames,
                            COUNT(thumb_webp) AS thumbnails
                        FROM photos
                    """).fetchone()

                    print(
                        f"    DB VERIFY: {check['cnt']} rows, "
                        f"{check['filenames']} filenames, "
                        f"{check['thumbnails']} WebP thumbnails"
                    )

                except Exception as e:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass

                    print(f"    DATABASE ERROR: {e}")
                    raise

                finally:
                    conn.close()


        if show_progress:
            parts = []
            if new_count:
                parts.append(f"+{new_count} added")
            if removed:
                parts.append(f"-{removed} removed")
            if skipped:
                parts.append(f"{len(skipped)} skipped")
            print(f"  ✓ {', '.join(parts) if parts else 'no changes'}")


        total_time = time.perf_counter() - scan_started

        if show_progress:
            print(f"  Total scan time: {total_time:.2f}s")

        return new_count

    
    def background_scan(self, interval=INTERVAL_SCAN):

        if self._running:
            return

        self._running = True
        self._stop_event.clear()

        def run():

            while not self._stop_event.wait(interval):

                if not self._running:
                    break

                try:
                    self.quick_scan(show_progress=False)

                except Exception:
                    logger.exception(
                        "Background scan failed"
                    )

        Thread(
            target=run,
            daemon=True,
            name="ImageIndexer"
        ).start()


    def stop(self):
        self._running = False
        self._stop_event.set()

    def get_unique_filename(
        self,
        filename: str,
        filepath: str
    ) -> str:
        stem = Path(filename).stem
        suffix = Path(filename).suffix
        filepath = str(Path(filepath).absolute())

        with self._filename_lock:

            count = 0

            while True:
                if count == 0:
                    candidate = filename
                else:
                    candidate = f"{stem} ({count}){suffix}"

                normalized = candidate.casefold()

                # Already reserved by another worker in this scan?
                if normalized in self._reserved_filenames:
                    count += 1
                    continue

                # Already used by another image in the database?
                existing = database.db_pool.execute_one("""
                    SELECT id
                    FROM photos
                    WHERE filename = ? COLLATE NOCASE
                    AND filepath != ?
                    LIMIT 1
                """, (candidate, filepath))

                if existing:
                    count += 1
                    continue

                # Reserve immediately, before another worker can use it.
                self._reserved_filenames.add(normalized)

                return candidate


class VideoIndexer:

    def __init__(self, folder: Path, db_path: Path):
        self.folder = folder
        self.db_path = db_path
        self.lock = Lock()

    def scan_videos(self):
        videos = []

        for root, _, filenames in os.walk(self.folder):
            for filename in filenames:

                ext = Path(filename).suffix.lower()

                if ext in VIDEO_EXTENSIONS:
                    videos.append(Path(root) / filename)

        return sorted(videos)

    def get_metadata(self, path):

        result = subprocess.run(
            [
                FFPROBE_PATH,
                "-v", "error",
                "-show_entries",
                "format=duration:format_tags=creation_time:stream=width,height,codec_type,codec_name",
                "-of", "json",
                str(path)
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )

        metadata = json.loads(result.stdout)

        video_stream = next(
            (
                stream for stream in metadata.get("streams", [])
                if stream.get("codec_type") == "video"
            ),
            None
        )

        if not video_stream:
            raise ValueError("No video stream found")

        duration = float(
            metadata.get("format", {}).get("duration", 0)
        )

        creation_time = (
            metadata.get("format", {})
            .get("tags", {})
            .get("creation_time")
        )

        if creation_time:
            try:
                parsed = datetime.fromisoformat(
                    creation_time.replace("Z", "+00:00")
                )

                date_taken = parsed.isoformat()

            except ValueError:
                date_taken = None
        else:
            date_taken = None

        return {
            "duration": duration,
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "codec": video_stream.get("codec_name"),
            "date_taken": date_taken
        }

    def make_thumbnail(self, path):

        if not os.path.isfile(FFMPEG_PATH):
            raise FileNotFoundError(
                f"FFmpeg executable does not exist: {FFMPEG_PATH}"
            )

        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"Video file does not exist: {path}"
            )

        command = [
            FFMPEG_PATH,
            "-y",
            "-ss", "00:00:01",
            "-i", str(path),
            "-frames:v", "1",
            "-vf", "scale=300:300:force_original_aspect_ratio=decrease",
            "-c:v", "libwebp",
            "-quality", "75",
            "-f", "image2pipe",
            "-"
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=60,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

        except FileNotFoundError as e:
            logger.exception(
                "Cannot start FFmpeg. Executable=%s, exists=%s",
                FFMPEG_PATH,
                os.path.isfile(FFMPEG_PATH)
            )
            raise

        if result.returncode != 0:
            error = result.stderr.decode(
                "utf-8",
                errors="replace"
            )

            raise RuntimeError(
                f"FFmpeg failed ({result.returncode}): {error}"
            )

        if not result.stdout:
            raise RuntimeError(
                "FFmpeg returned no thumbnail data"
            )

        return result.stdout

    def quick_scan(self):

        if not self.lock.acquire(blocking=False):
            return 0

        try:
            return self._quick_scan()

        finally:
            self.lock.release()

    def _quick_scan(self):

        paths = self.scan_videos()

        existing_rows = database.db_pool.execute("""
            SELECT id, filepath, file_size, file_modified
            FROM videos
        """)

        existing = {
            row["filepath"]: dict(row)
            for row in existing_rows
        }

        found = set()
        new_count = 0

        for path in paths:

            try:
                filepath = str(path.absolute())
                found.add(filepath)

                stat = path.stat()

                modified = datetime.fromtimestamp(
                    stat.st_mtime
                ).isoformat()

                old = existing.get(filepath)

                if old and (
                    old["file_size"] == stat.st_size
                    and old["file_modified"] == modified
                ):
                    continue

                file_data, error = safe_read_file(filepath)

                if file_data is None:
                    raise OSError(error)

                file_hash = hashlib.sha256(file_data).hexdigest()

                metadata = self.get_metadata(path)
                thumbnail = self.make_thumbnail(path)

                date_taken = metadata.get("date_taken")

                database.db_pool.execute_write("""
                    INSERT INTO videos (
                        filename,
                        filepath,
                        file_hash,
                        file_size,
                        file_modified,
                        duration,
                        width,
                        height,
                        codec,
                        date_taken,
                        thumb_webp,
                        indexed_at,
                        processed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                    ON CONFLICT(filepath) DO UPDATE SET
                        filename=excluded.filename,
                        file_hash=excluded.file_hash,
                        file_size=excluded.file_size,
                        file_modified=excluded.file_modified,
                        duration=excluded.duration,
                        width=excluded.width,
                        height=excluded.height,
                        codec=excluded.codec,
                        date_taken=excluded.date_taken,
                        thumb_webp=excluded.thumb_webp,
                        indexed_at=excluded.indexed_at,
                        processed_at=excluded.processed_at
                """, (
                    path.name,
                    filepath,
                    file_hash,
                    stat.st_size,
                    modified,
                    metadata["duration"],
                    metadata["width"],
                    metadata["height"],
                    metadata["codec"],
                    date_taken,
                    thumbnail,
                    now_iso(),
                    now_iso()
                ))

                new_count += 1

                logger.info("Indexed video: %s", path.name)

            except Exception:
                logger.exception(
                    "Failed to index video: %s",
                    path
                )

        # Remove videos that no longer exist.
        missing = set(existing.keys()) - found

        if missing:
            with db_lock:
                conn = get_db(self.db_path)

                try:
                    conn.executemany(
                        "DELETE FROM videos WHERE filepath = ?",
                        [(filepath,) for filepath in missing]
                    )
                finally:
                    conn.close()

        logger.info(
            "Video scan complete: %s indexed, %s removed",
            new_count,
            len(missing)
        )

        return new_count


def create_app(folder: Path, db_path: Path):
    global db_pool

    app = Flask(__name__)
    secret_key = os.environ.get("FLASK_SECRET_KEY")
    ##python -c "import secrets; print(secrets.token_hex(32))"

    if not secret_key:
        raise RuntimeError("FLASK_SECRET_KEY is missing from the environment or .env file")

    app.config.update(
        SECRET_KEY=secret_key,
        WTF_CSRF_ENABLED=True,
        WTF_CSRF_TIME_LIMIT=3600,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("FLASK_HTTPS", "0") == "1",
    )
    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https: blob:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "img-src 'self' data: blob: https:; "
            "font-src 'self' data: https:; "
            "connect-src 'self' https: wss: blob:; "
            "worker-src 'self' blob:; "
            "child-src blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none';"
        )
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )

        return response

    csrf = CSRFProtect(app)

    @app.context_processor
    def inject_csrf_token():

        return {"csrf_token": generate_csrf}

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        return jsonify({
            "error": "CSRF validation failed"
        }), 400

    init_db(db_path)
    database.db_pool = DatabasePool(db_path)


    scripts_dir = Path(__file__).resolve().parent / "scripts"

    location_resolver = LocationResolver(
        pickle_path=scripts_dir / "city_tree.pkl",
        admin2_path=scripts_dir / "admin2codes.txt"
    )

    location_resolver.load()

    indexer = Indexer(folder, db_path, location_resolver)
    video_indexer = VideoIndexer(folder, db_path)

    @app.route('/api/photos')
    def api_photos():
        page = max(1, request.args.get('page', 1, type=int))
        per_page = max( 1, min( request.args.get('per_page', PER_PAGE, type=int), 500))
        search = request.args.get('search', '').strip()
        sort = request.args.get('sort', 'date')
        order = request.args.get('order', 'desc')

        try:       
            
            if search:

                fts_query = build_fts_query(search)

                if not fts_query:
                    search = ''
                count_row = database.db_pool.execute_one(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM photos_fts
                    WHERE photos_fts MATCH ?
                    """,
                    (fts_query,)
                )

                total = count_row["cnt"] if count_row else 0
                rows = database.db_pool.execute(
                    """
                    SELECT
                        p.id,
                        p.filename,
                        p.width,
                        p.height,
                        p.date_taken,
                        p.manual_date,
                        p.file_modified,
                        p.dominant_color,
                        p.subdivision_name,
                        p.city_name,
                        p.country_name,
                        p.thumb_webp IS NOT NULL AS has_webp,
                        p.tags_json IS NOT NULL AS has_tags

                    FROM photos_fts AS f

                    JOIN photos AS p
                        ON p.id = f.rowid

                    WHERE photos_fts MATCH ?

                    ORDER BY
                        COALESCE(
                            p.date_taken,
                            p.manual_date,
                            p.file_modified
                        ) DESC,
                        p.id DESC

                    LIMIT ? OFFSET ?
                    """,
                    (
                        fts_query,
                        per_page,
                        (page - 1) * per_page
                    )
                )
                photos = [dict(r) for r in rows]

            else:
                sort_value = request.args.get('sort', 'newest')

                if sort_value == 'oldest':
                    order_by = """
                        COALESCE(date_taken, manual_date, file_modified) ASC,
                        id ASC
                    """
                elif sort_value == 'newest':
                    order_by = """
                        COALESCE(date_taken, manual_date, file_modified) DESC,
                        id DESC
                    """
                elif sort_value == 'name':
                    order_by = "filename COLLATE NOCASE ASC, id ASC"
                else:
                    order_by = "id DESC"

                total_row = database.db_pool.execute_one(
                    "SELECT COUNT(*) AS cnt FROM photos"
                )
                total = total_row["cnt"] if total_row else 0

                rows = database.db_pool.execute(
                    f"""
                    SELECT id, filename, width, height, date_taken, manual_date,
                        file_modified, dominant_color,
                        subdivision_name, city_name, country_name,
                        thumb_webp IS NOT NULL AS has_webp,
                        tags_json IS NOT NULL AS has_tags
                    FROM photos
                    ORDER BY {order_by}
                    LIMIT ? OFFSET ?
                    """,
                    (per_page, (page - 1) * per_page)
                )

                photos = [dict(r) for r in rows]

            for p in photos:
                date_str = (
                    p.get('date_taken')
                    or p.get('manual_date')
                    or p.get('file_modified')
                    or ''
                )

                p['date_group'] = date_str[:10] if date_str else 'Unknown date'

        except Exception as e:
            print(f"API error: {e}")

            return jsonify({
                'photos': [],
                'total': 0,
                'page': page,
                'per_page': per_page,
                'error': 'Internal server error'
            }), 500


        return jsonify({
            'photos': photos, 'total': total,
            'page': page, 'per_page': per_page
        })

    @app.route('/api/photo/<int:photo_id>', methods=['GET'])
    def api_photo(photo_id):
        try:
            row = database.db_pool.execute_one("""
                SELECT
                    id,
                    file_hash,
                    filename,
                    file_size,
                    file_modified,
                    width,
                    height,
                    date_taken,
                    manual_date,
                    exif_camera,
                    exif_gps_lat,
                    exif_gps_lon,
                    city_name,
                    subdivision_name,
                    region_name,
                    country_name,
                    country_code,
                    location_distance_km,
                    location_type,
                    location_indexed_at,
                    caption,
                    detailed_caption,
                    ocr_text,
                    indexed_at,
                    processed_at,
                    tags_json,
                    categories_json,
                    filepath
                FROM photos
                WHERE id = ?
            """, (photo_id,))
        except Exception as e:
            return jsonify({'error': str(e)}), 500

        if not row:
            return jsonify({'error': 'Not found'}), 404

        d = dict(row)
        d['tags'] = safe_json_load(d.get('tags_json') )
        d['categories'] = safe_json_load(d.get('categories_json'))
        d['date'] = (d.get('date_taken')  or d.get('manual_date') or d.get('file_modified'))

        if d.get('date_taken'):
            d['date_source'] = 'exif'
            d['date_editable'] = False

        elif d.get('manual_date'):
            d['date_source'] = 'manual'
            d['date_editable'] = True

        else:
            d['date_source'] = 'file_modified'
            d['date_editable'] = True
        if (
            d.get('exif_gps_lat') is not None
            and d.get('exif_gps_lon') is not None
        ):
            d['gps'] = {
                'lat': d['exif_gps_lat'],
                'lon': d['exif_gps_lon']
            }
        else:
            d['gps'] = None
        d['location'] = {
            'city': d.get('city_name'),
            'subdivision': d.get('subdivision_name'),
            'region': d.get('region_name'),
            'country': d.get('country_name'),
            'country_code': d.get('country_code'),
            'distance_km': d.get('location_distance_km'),
            'type': d.get('location_type'),
            'indexed_at': d.get('location_indexed_at')
        }


        filepath = d.get('filepath', '')
        d.pop('filepath', None)
        d['file_accessible'] = safe_check_exists(filepath)

        for key in ['thumbnail', 'thumb_webp', 'embedding', 'tags_json', 'categories_json']:
            d.pop(key, None)

        return jsonify(d)

    @app.route('/api/photo/<int:photoid>', methods=['DELETE']) 
    def apideletephoto(photoid): 
        return jsonify({ 'success': False, 'error': 'Photo deletion is currently disabled.' }), 403

    @app.route('/api/photos/batch')
    def api_photos_batch():
        ids = request.args.get('ids', '')
        if not ids:
            return jsonify({'photos': []})

        try:
            id_list = [int(i) for i in ids.split(',')[:100]]
            placeholders = ','.join('?' * len(id_list))
            rows = database.db_pool.execute(f"""
                SELECT id, filename, width, height, date_taken, manual_date, file_modified,
                       dominant_color, thumb_webp IS NOT NULL as has_webp,
                       tags_json IS NOT NULL as has_tags
                FROM photos WHERE id IN ({placeholders})
            """, tuple(id_list))

            return jsonify({'photos': [dict(r) for r in rows]})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/photo/<int:photo_id>/date', methods=['POST'])
    def api_update_photo_date(photo_id):
        try:
            data = request.get_json(silent=True) or {}
            new_date = data.get('date', '').strip()

            if not new_date:
                return jsonify({
                    'success': False,
                    'error': 'Date is required'
                }), 400

            try:
                parsed_date = datetime.fromisoformat(
                    new_date.replace('Z', '+00:00')
                )
            except ValueError:
                return jsonify({
                    'success': False,
                    'error': 'Invalid date format'
                }), 400

            new_date = parsed_date.replace(
                tzinfo=None
            ).isoformat(timespec='seconds')

            with db_lock:
                conn = get_db(db_path)

                try:
                    row = conn.execute(
                        """
                        SELECT id, date_taken, manual_date
                        FROM photos
                        WHERE id = ?
                        """,
                        (photo_id,)
                    ).fetchone()

                    if not row:
                        return jsonify({
                            'success': False,
                            'error': 'Photo not found'
                        }), 404

                    if row['date_taken']:
                        return jsonify({
                            'success': False,
                            'error': 'Photo already has an EXIF date'
                        }), 403

                    conn.execute(
                        """
                        UPDATE photos
                        SET manual_date = ?
                        WHERE id = ?
                        """,
                        (new_date, photo_id)
                    )

                finally:
                    conn.close()

            return jsonify({
                'success': True,
                'date': new_date,
                'date_source': 'manual'
            })

        except Exception as e:
            app.logger.exception('Date update failed')

            return jsonify({
                'success': False,
                'error': 'Failed to update date'
            }), 500
        

    @app.route('/maps/<path:filename>')
    def serve_map(filename):
        return send_from_directory(
            r'H:\Imagesorter\images\data',
            filename
            ##H:\Imagesorter\images\data
        )
    @app.route('/maps/world-europe.pmtiles')
    def serve_world_map():
        map_path = Path(
            r'H:\Imagesorter\images\data\world-europe.pmtiles'
        )

        if not map_path.is_file():
            abort(404)

        response = send_file(
            map_path,
            mimetype='application/vnd.pmtiles'
        )
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Accept-Ranges'] = 'bytes'
        return response
    
    @app.route("/tilesworld_z5.pmtiles")
    def serve_pmtiles():

        if not os.path.isfile(PMTILES_FILE):
            abort(404, description="PMTiles archive not found")

        response = send_file(
            PMTILES_FILE,
            mimetype="application/octet-stream",
            conditional=True,
            max_age=3600
        )

        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "private, max-age=3600"

        return response

    @app.route('/api/stats')
    def api_stats():
        try:
            total = database.db_pool.execute_one("SELECT COUNT(*) as cnt FROM photos")
            tagged = database.db_pool.execute_one(
                "SELECT COUNT(*) as cnt FROM photos WHERE tags_json IS NOT NULL"
            )
            date_range = database.db_pool.execute_one("""
                SELECT MIN(COALESCE(date_taken, manual_date, file_modified)) as oldest,
                       MAX(COALESCE(date_taken, manual_date, file_modified)) as newest
                FROM photos
            """)

            return jsonify({
                'total': total['cnt'] if total else 0,
                'with_tags': tagged['cnt'] if tagged else 0,
                'oldest': date_range['oldest'] if date_range else None,
                'newest': date_range['newest'] if date_range else None,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    
    @app.route('/api/places')
    def api_places():

        precision = request.args.get('precision', 3, type=int)
        precision = max(2, min(precision, 5))

        try:
            rows = database.db_pool.execute("""
                SELECT
                    ROUND(exif_gps_lat, ?) AS lat,
                    ROUND(exif_gps_lon, ?) AS lon,
                    COUNT(*) AS count
                FROM photos
                WHERE exif_gps_lat IS NOT NULL
                AND exif_gps_lon IS NOT NULL
                AND exif_gps_lat BETWEEN -90 AND 90
                AND exif_gps_lon BETWEEN -180 AND 180
                GROUP BY
                    ROUND(exif_gps_lat, ?),
                    ROUND(exif_gps_lon, ?)
            """, (
                precision,
                precision,
                precision,
                precision
            ))

            locations = [
                {
                    'lat': row['lat'],
                    'lon': row['lon'],
                    'count': row['count']
                }
                for row in rows
            ]

            return jsonify({
                'locations': locations,
                'total_locations': len(locations),
                'total_photos': sum(
                    location['count']
                    for location in locations
                )
            })

        except Exception:
            app.logger.exception('Places API failed')

            return jsonify({
                'error': 'Failed to load places'
            }), 500

    @app.route('/api/places/photos')
    def api_place_photos():
        try:
            lat = request.args.get('lat', type=float)
            lon = request.args.get('lon', type=float)
            precision = request.args.get('precision', 3, type=int)
            precision = max(2, min(precision, 5))

            if (
                lat is None or lon is None
                or not (-90 <= lat <= 90)
                or not (-180 <= lon <= 180)
            ):
                return jsonify({'error': 'Invalid coordinates'}), 400

            target_lat = round(lat, precision)
            target_lon = round(lon, precision)

            step = 10 ** (-precision)
            half_step = step / 2

            rows = database.db_pool.execute("""
                SELECT
                    id,
                    filename,
                    width,
                    height,
                    date_taken,
                    file_modified,
                    dominant_color,
                    exif_gps_lat,
                    exif_gps_lon,
                    thumb_webp IS NOT NULL AS has_webp
                FROM photos
                WHERE exif_gps_lat BETWEEN ? AND ?
                AND exif_gps_lon BETWEEN ? AND ?
                AND ROUND(exif_gps_lat, ?) = ?
                AND ROUND(exif_gps_lon, ?) = ?
                ORDER BY COALESCE(date_taken, manual_date, file_modified) DESC
            """, (
                target_lat - half_step,
                target_lat + half_step,
                target_lon - half_step,
                target_lon + half_step,
                precision,
                target_lat,
                precision,
                target_lon
            ))

            photos = [dict(row) for row in rows]

            return jsonify({
                'photos': photos,
                'total': len(photos),
                'lat': lat,
                'lon': lon,
            })

        except Exception:
            app.logger.exception('Place photos API failed')
            return jsonify({'error': 'Failed to load location photos'}), 500
    
    @app.route('/api/videos')
    def api_videos():

        try:
            rows = database.db_pool.execute("""
                SELECT
                    id,
                    filename,
                    filepath,
                    file_size,
                    file_modified,
                    duration,
                    width,
                    height,
                    date_taken,
                    thumb_webp IS NOT NULL AS has_thumbnail
                FROM videos
                ORDER BY date_taken DESC, id DESC
            """)

            return jsonify({
                'videos': [dict(row) for row in rows],
                'total': len(rows)
            })

        except Exception:
            logger.exception("Video API failed")

            return jsonify({
                'videos': [],
                'total': 0,
                'error': 'Failed to load videos'
            }), 500
    @app.route('/api/video/<int:video_id>')
    def api_video(video_id):

        try:
            row = database.db_pool.execute_one("""
                SELECT
                    id,
                    filename,
                    filepath,
                    file_hash,
                    file_size,
                    file_modified,
                    duration,
                    width,
                    height,
                    codec,
                    date_taken,
                    indexed_at,
                    processed_at
                FROM videos
                WHERE id = ?
            """, (video_id,))

            if not row:
                return jsonify({
                    'success': False,
                    'error': 'Video not found'
                }), 404

            video = dict(row)

            video['file_accessible'] = safe_check_exists(
                video['filepath']
            )

            video.pop('filepath', None)

            return jsonify({
                'success': True,
                'video': video
            })

        except Exception:
            logger.exception("Video details API failed")

            return jsonify({
                'success': False,
                'error': 'Failed to load video'
            }), 500
    @app.route('/video-thumbnail/<int:video_id>')
    def serve_video_thumbnail(video_id):

        row = database.db_pool.execute_one("""
            SELECT thumb_webp
            FROM videos
            WHERE id = ?
        """, (video_id,))

        if not row or not row['thumb_webp']:
            abort(404)

        return Response(
            row['thumb_webp'],
            mimetype='image/webp',
            headers={
                'Cache-Control': 'private, max-age=3600'
            }
        )

    @app.route('/video/<int:video_id>')
    def serve_video(video_id):

        row = database.db_pool.execute_one("""
            SELECT filepath
            FROM videos
            WHERE id = ?
        """, (video_id,))

        if not row:
            abort(404)

        filepath = row['filepath']

        if not filepath or not safe_check_exists(filepath):
            abort(404)

        return send_file(
            filepath,
            conditional=True,
            etag=True,
            max_age=0
        )
    @app.route("/videos")
    def videos_page():
        return render_template(
            "videos.html",
            active_section="videos"
        )
    
    @app.route('/places')
    def places():
        return render_template('places.html')  

    @app.route('/albums')
    def albums():
        return render_template('albums.html')

    @app.route('/api/rescan', methods=['POST'])
    def api_rescan():
        try:
            # Scan photos
            image_count = indexer.quick_scan(show_progress=True)

            # Scan videos
            video_count = video_indexer.quick_scan()

            return jsonify({
                'success': True,
                'new_images': image_count,
                'new_videos': video_count,
                'total_new': image_count + video_count
            })

        except Exception as e:
            logger.exception("Rescan error")

            return jsonify({
                'success': False,
                'error': str(e),
                'new_images': 0,
                'new_videos': 0
            }), 500

    
    @app.route('/api/debug/path/<int:photo_id>')
    def api_debug_path(photo_id):
        row = database.db_pool.execute_one(
            "SELECT id, filepath, filename FROM photos WHERE id = ?", (photo_id,)
        )
        if not row:
            return jsonify({'error': 'Not found'}), 404

        filepath = row['filepath']
        info = {
            'id': row['id'],
            'filename': row['filename'],
            'filepath_stored': filepath,
            'filepath_repr': repr(filepath),
            'filepath_len': len(filepath),
            'os_name': os.name,
        }

        problematic = []
        for i, ch in enumerate(filepath):
            code = ord(ch)
            if code > 127:
                problematic.append({
                    'pos': i, 'char': ch,
                    'unicode': f'U+{code:04X}', 'name': repr(ch)
                })
            elif ch in '<>"|?*' and i > 1:  
                problematic.append({
                    'pos': i, 'char': ch, 'note': 'invalid on Windows'
                })
        info['problematic_chars'] = problematic

        tests = {}

        try:
            tests['os.path.exists'] = os.path.exists(filepath)
        except Exception as e:
            tests['os.path.exists'] = f'ERROR: {e}'

        try:
            tests['Path.exists'] = Path(filepath).exists()
        except Exception as e:
            tests['Path.exists'] = f'ERROR: {e}'

        try:
            with open(filepath, 'rb') as f:
                f.read(1)
            tests['open_read'] = True
        except Exception as e:
            tests['open_read'] = f'ERROR: {e}'

        if os.name == 'nt':
            abs_path = os.path.abspath(filepath)
            ext_path = '\\\\?\\' + abs_path
            info['extended_path'] = ext_path
            try:
                with open(ext_path, 'rb') as f:
                    f.read(1)
                tests['extended_path_read'] = True
            except Exception as e:
                tests['extended_path_read'] = f'ERROR: {e}'

        info['access_tests'] = tests

        data, err = safe_read_file(filepath)
        info['safe_read_success'] = data is not None
        if err:
            info['safe_read_error'] = err
        if data:
            info['file_size_bytes'] = len(data)

        return jsonify(info)

    @app.route('/thumbnail/<int:photo_id>')
    def serve_thumbnail(photo_id):
        NO_CACHE_HEADERS = {
            'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
            'Pragma': 'no-cache',
            'Expires': '0'
        }

        row = database.db_pool.execute_one(
            "SELECT id FROM photos WHERE id = ?",
            (photo_id,)
        )

        if not row:
            return Response(status=404, headers=NO_CACHE_HEADERS)

        cache_key = f"thumb_{photo_id}_webp"
        cached = thumb_cache.get(cache_key)

        if cached is not None:
            return Response(
                cached,
                mimetype='image/webp',
                headers=NO_CACHE_HEADERS
            )

        row = database.db_pool.execute_one(
            """
            SELECT thumb_webp, filepath
            FROM photos
            WHERE id = ?
            """,
            (photo_id,)
        )

        if not row:
            return Response(status=404, headers=NO_CACHE_HEADERS)

        thumb = row['thumb_webp']

        if not thumb:
            file_data, err = safe_read_file(row['filepath'])

            if file_data is None:
                return Response(status=404, headers=NO_CACHE_HEADERS)

            try:
                with Image.open(BytesIO(file_data)) as img:

                    img.load()

                    oriented = ImageOps.exif_transpose(img)

                    try:

                        thumb = make_thumbnail_webp(oriented)

                    finally:

                        if oriented is not img:
                            oriented.close()

                database.db_pool.execute_write(
                    """
                    UPDATE photos
                    SET thumb_webp = ?
                    WHERE id = ?
                    """,
                    (thumb, photo_id)
                )

            except Exception:
                return Response(status=500, headers=NO_CACHE_HEADERS)

        thumb_cache.set(cache_key, thumb)

        return Response(
            thumb,
            mimetype='image/webp',
            headers=NO_CACHE_HEADERS
        )

    @app.route('/thumbnails/batch')
    def serve_thumbnails_batch():
        ids = request.args.get('ids', '')

        if not ids:
            return jsonify({})

        try:
            id_list = list(dict.fromkeys(
                int(i) for i in ids.split(',')[:50]
            ))
        except ValueError:
            return jsonify({'error': 'Invalid IDs'}), 400

        result = {
            str(photo_id): f"/thumbnail/{photo_id}"
            for photo_id in id_list
        }

        return jsonify(result)

    @app.route('/image/<int:photo_id>')
    def serve_image(photo_id):
        try:
            row = database.db_pool.execute_one(
                "SELECT filepath, filename, file_hash FROM photos WHERE id = ?",
                (photo_id,)
            )

        except Exception as e:
            print(f"DB error for image/{photo_id}: {e}")
            return Response(status=500)

        if not row:
            return Response(status=404)

        filepath = row['filepath']
        filename = row['filename']

        ext = os.path.splitext(filename)[1].lower()

        mime_types = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp',
            '.tiff': 'image/tiff',
        }

        mimetype = mime_types.get(
            ext,
            'application/octet-stream'
        )

        if safe_check_exists(filepath):

            response = send_file(
                filepath,
                mimetype=mimetype,
                conditional=True,
                etag=row['file_hash'],
                max_age=0
            )
            response.headers['Cache-Control'] = (
                'private, no-cache, must-revalidate'
            )

            return response

        data, err = safe_read_file(filepath)

        if data is None:
            print(f"Image read failed id={photo_id}: {err}")
            print(f"  Path: {repr(filepath)}")

            data = _try_find_and_read(filename, folder)

            if data is None:
                return Response(status=404)
            else:
                print(f"  Found via filename search: {filename}")

        ext = os.path.splitext(filename)[1].lower()
        mime_types = {
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.png': 'image/png', '.gif': 'image/gif',
            '.webp': 'image/webp', '.bmp': 'image/bmp',
            '.tiff': 'image/tiff',
        }
        mimetype = mime_types.get(ext, 'image/jpeg')

        return Response(
            data,
            mimetype=mimetype,
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                'Pragma': 'no-cache',
                'Content-Length': str(len(data)),
            }
        )


    def _try_find_and_read(filename: str, base_folder: Path) -> bytes | None:

        try:
            for dirpath, _, filenames in os.walk(base_folder):
                if filename in filenames:
                    found_path = os.path.join(dirpath, filename)
                    data, err = safe_read_file(found_path)
                    if data is not None:

                        database.db_pool.execute_write(
                            "UPDATE photos SET filepath = ? WHERE filename = ?",
                            (found_path, filename)
                        )
                        return data
        except Exception:
            pass
        return None

    @app.route('/')
    def photos():
        return render_template('photos.html')


    print("Scanning...")
    indexer.quick_scan()
    print("Scanning videos...")
    video_indexer.quick_scan()
    indexer.background_scan(interval=INTERVAL_SCAN)

    return app

def main():
    folder = Path(r"H:\Imagesorter\images")

    if not folder.exists():
        print(f"Folder not found: {folder}")
        return

    db_path = folder / '.photos.db'
    print(f"\n Photo Gallery")
    print(f"   Folder: {folder}")
    print(f"   URL: http://localhost:5000\n")
    app = create_app(folder, db_path)
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    main()
