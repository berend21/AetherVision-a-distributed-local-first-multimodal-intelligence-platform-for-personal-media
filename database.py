import sqlite3
from threading import Lock, BoundedSemaphore
import logging
from pathlib import Path
import json
from datetime import datetime, timezone
from collections import OrderedDict

logger = logging.getLogger("imagesorter")




def get_db(db_path: Path):
    conn = sqlite3.connect(
        str(db_path),
        timeout=10,
        isolation_level=None,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-16000")
    conn.execute("PRAGMA mmap_size=268435456")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")

    return conn

class LRUCache:
    def __init__(self, max_size_mb=200):
        self.max_size = max_size_mb * 1024 * 1024
        self.cache = OrderedDict()
        self.current_size = 0
        self.lock = Lock()

    @staticmethod
    def _size(value):
        return len(value) if isinstance(value, bytes) else 100

    def get(self, key):
        with self.lock:
            value = self.cache.get(key)

            if value is not None:
                self.cache.move_to_end(key)

            return value

    def delete(self, key):
        with self.lock:
            value = self.cache.pop(key, None)

            if value is not None:
                self.current_size -= self._size(value)

    def set(self, key, value):
        size = self._size(value)

        if size > self.max_size:
            self.delete(key)
            return

        with self.lock:
            old = self.cache.pop(key, None)

            if old is not None:
                self.current_size -= self._size(old)

            while (
                self.current_size + size > self.max_size
                and self.cache
            ):
                _, oldest = self.cache.popitem(last=False)
                self.current_size -= self._size(oldest)

            self.cache[key] = value
            self.current_size += size

    def clear(self):
        with self.lock:
            self.cache.clear()
            self.current_size = 0

    def stats(self):
        with self.lock:
            return {
                "items": len(self.cache),
                "size_mb": round(
                    self.current_size / 1024 / 1024, 2
                ),
                "max_size_mb": round(
                    self.max_size / 1024 / 1024, 2
                )
            }

class DatabasePool:

    def __init__(self, db_path: Path, max_connections=5):

        self.db_path = db_path
        self.max_connections = max_connections

        self.lock = Lock()
        self.connections = []

        # Actually limit simultaneous database connections.
        self.connection_slots = BoundedSemaphore(max_connections)

    def get_connection(self):
        self.connection_slots.acquire()
        try:
            with self.lock:
                if self.connections:
                    return self.connections.pop()
            return get_db(self.db_path)

        except Exception:
            self.connection_slots.release()
            raise
    def return_connection(self, conn):
        try:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except sqlite3.Error:
                conn.close()
                return
            with self.lock:

                if len(self.connections) < self.max_connections:
                    self.connections.append(conn)
                else:
                    conn.close()
        finally:
            self.connection_slots.release()

    def execute(self, query, params=None):
        conn = self.get_connection()
        try:
            return conn.execute(
                query,
                params or ()
            ).fetchall()

        finally:
            self.return_connection(conn)
    def execute_one(self, query, params=None):
        conn = self.get_connection()
        try:
            return conn.execute(
                query,
                params or ()
            ).fetchone()

        finally:
            self.return_connection(conn)
    def execute_write(self, query, params=None):
        with db_lock:
            conn = self.get_connection()
            try:
                conn.execute(
                    query,
                    params or ()
                )
                return True
            finally:
                self.return_connection(conn)

    def execute_many(self, query, params_list):
        with db_lock:
            conn = self.get_connection()
            try:
                conn.executemany(
                    query,
                    params_list
                )
                return True
            finally:
                self.return_connection(conn)

    def close_all(self):
        with self.lock:
            while self.connections:
                conn = self.connections.pop()

                try:
                    conn.close()
                except sqlite3.Error:
                    logger.exception("Failed to close database connection")

def init_db(db_path: Path):

    conn = get_db(db_path)

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        has_photos = conn.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            AND name='photos'
        """).fetchone()

        if not has_photos:
            logger.info("Creating new photo database")

            conn.execute("""
                CREATE TABLE photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_hash TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    file_size INTEGER,
                    file_modified TEXT,
                    width INTEGER,
                    height INTEGER,
                    date_taken TEXT,
                    manual_date TEXT,
                    exif_camera TEXT,
                    exif_gps_lat REAL,
                    exif_gps_lon REAL,
                    city_name TEXT,
                    subdivision_name TEXT,
                    region_name TEXT,
                    country_name TEXT,
                    country_code TEXT,
                    location_distance_km REAL,
                    location_type TEXT,
                    location_indexed_at TEXT,
                    thumb_webp BLOB,
                    dominant_color TEXT,
                    tags_json TEXT,
                    categories_json TEXT,
                    caption TEXT,
                    detailed_caption TEXT,
                    ocr_text TEXT,
                    embedding BLOB,
                    indexed_at TEXT,
                    processed_at TEXT
                )
            """)
        else:
            logger.info("Using existing photo database")


        conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL UNIQUE,
            file_hash TEXT,
            file_size INTEGER,
            file_modified TEXT,
            duration REAL,
            width INTEGER,
            height INTEGER,
            codec TEXT,
            date_taken TEXT,
            thumb_webp BLOB,
            indexed_at TEXT,
            processed_at TEXT
        )
        """)
        indexes = [
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_photos_filepath_unique
            ON photos(filepath)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_photos_file_hash
            ON photos(file_hash)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_filename
            ON photos(filename)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_date_taken
            ON photos(date_taken)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_file_modified
            ON photos(file_modified)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_photos_date_id
            ON photos(date_taken, id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_photos_gps
            ON photos(exif_gps_lat, exif_gps_lon)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_photos_city
            ON photos(city_name)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_photos_country
            ON photos(country_name)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_photos_filename_nocase
            ON photos(filename COLLATE NOCASE)
            """
        ]
        

        for statement in indexes:
            conn.execute(statement)

        has_fts = conn.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            AND name='photos_fts'
        """).fetchone()

        rebuild_fts = False

        if has_fts:

            fts_columns = {
                row['name']
                for row in conn.execute(
                    "PRAGMA table_info(photos_fts)"
                ).fetchall()
            }

            required_columns = {
                'filename',
                'caption',
                'detailed_caption',
                'ocr_text',
                'tags_text',
                'categories_text',
                'city_name',
                'subdivision_name',
                'region_name',
                'country_name'
            }

            if not required_columns.issubset(fts_columns):
                logger.warning("FTS schema outdated; rebuilding")
                rebuild_fts = True

        else:
            rebuild_fts = True

        conn.execute("BEGIN IMMEDIATE")

        try:
            if rebuild_fts:
                logger.info("Creating/rebuilding FTS5 index")
                conn.execute("DROP TABLE IF EXISTS photos_fts")
                conn.execute("""
                    CREATE VIRTUAL TABLE photos_fts USING fts5(
                        filename,
                        caption,
                        detailed_caption,
                        ocr_text,
                        tags_text,
                        categories_text,
                        city_name,
                        subdivision_name,
                        region_name,
                        country_name
                    )
                """)

                rows = conn.execute("""
                    SELECT
                        id,
                        filename,
                        caption,
                        detailed_caption,
                        ocr_text,
                        tags_json,
                        categories_json,
                        city_name,
                        subdivision_name,
                        region_name,
                        country_name
                    FROM photos
                """).fetchall()

                batch = []

                for row in rows:

                    tags_text = extract_searchable_json(
                        row['tags_json']
                    )

                    categories_text = extract_searchable_json(
                        row['categories_json']
                    )

                    batch.append((
                        row['id'],
                        row['filename'] or '',
                        row['caption'] or '',
                        row['detailed_caption'] or '',
                        row['ocr_text'] or '',
                        tags_text,
                        categories_text,
                        row['city_name'] or '',
                        row['subdivision_name'] or '',
                        row['region_name'] or '',
                        row['country_name'] or ''
                    ))

                if batch:
                    conn.executemany("""
                        INSERT INTO photos_fts (
                            rowid,
                            filename,
                            caption,
                            detailed_caption,
                            ocr_text,
                            tags_text,
                            categories_text,
                            city_name,
                            subdivision_name,
                            region_name,
                            country_name
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, batch)

                logger.info(
                    "FTS5 rebuild complete: %s records",
                    len(batch)
                )

            conn.execute("COMMIT")

        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")

            logger.exception("FTS5 initialization failed")
            raise

    except Exception:
        logger.exception("Database initialization failed")
        raise

    finally:
        conn.close()

    logger.info("Database initialization successful")

def safe_json_load(value, default=None):

    if default is None:
        default = {}

    if not value:
        return default

    try:
        return json.loads(value)

    except (ValueError, TypeError):
        return default

def extract_searchable_json(value):

    if not value:
        return ''

    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return ''

    parts = []


    def collect(item):

        if isinstance(item, dict):
            for key, val in item.items():
                parts.append(str(key))
                collect(val)

        elif isinstance(item, list):
            for val in item:
                collect(val)

        elif item is not None:
            parts.append(str(item))

    collect(parsed)

    return ' '.join(parts)
def build_fts_query(search):

    if not search:
        return None

    search = search.strip()

    if not search:
        return None

    terms = search.split()

    if not terms:
        return None

    terms = [
        '"' + term.replace('"', '""') + '"'
        for term in terms
    ]

    return " AND ".join(terms)
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()





def update_photo_fts(conn, photo_id):

    row = conn.execute("""
        SELECT
            id,
            filename,
            caption,
            detailed_caption,
            ocr_text,
            tags_json,
            categories_json,
            city_name,
            subdivision_name,
            region_name,
            country_name
        FROM photos
        WHERE id = ?
    """, (photo_id,)).fetchone()

    if not row:
        return

    tags_text = extract_searchable_json(row['tags_json'])
    categories_text = extract_searchable_json(row['categories_json'])

    # FTS5 does not support UPSERT.
    # Delete the existing entry first.
    conn.execute("""
        DELETE FROM photos_fts
        WHERE rowid = ?
    """, (photo_id,))

    # Insert the updated record.
    conn.execute("""
        INSERT INTO photos_fts (
            rowid,
            filename,
            caption,
            detailed_caption,
            ocr_text,
            tags_text,
            categories_text,
            city_name,
            subdivision_name,
            region_name,
            country_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row['id'],
        row['filename'] or '',
        row['caption'] or '',
        row['detailed_caption'] or '',
        row['ocr_text'] or '',
        tags_text,
        categories_text,
        row['city_name'] or '',
        row['subdivision_name'] or '',
        row['region_name'] or '',
        row['country_name'] or ''
    ))


db_lock = Lock()
db_pool = None
thumb_cache = LRUCache(max_size_mb=200)

