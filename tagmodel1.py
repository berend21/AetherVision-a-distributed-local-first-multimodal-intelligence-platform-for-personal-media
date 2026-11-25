import sys
import json
import argparse
import gc
import warnings
import sqlite3

from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from tqdm import tqdm



warnings.filterwarnings("ignore", category=UserWarning)
if torch.cuda.is_available():
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.enable_flash_sdp(True)
    torch.backends.cuda.enable_mem_efficient_sdp(True)


@dataclass
class Config:
    ram_checkpoint: str = (
        r"H:\Imagesorter"
        r"\models\ram_plus_swin_large_14m.pth"
    )

    ram_image_size: int = 384

    image_extensions: set = field(default_factory=lambda: {
        '.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tiff'
    })
    max_image_size: int = 768

    device: str = "cuda" if torch.cuda.is_available() else "cpu"

    batch_size_embedding: int = 4
    batch_size_ram: int = 4
    batch_size_blip: int = 2
    batch_size_ocr: int = 1
    image_workers: int = 2

    use_fp16: bool = True
    use_pinned_memory: bool = True

    siglip_model: str = "google/siglip2-so400m-patch14-384"


    ocr_languages: list = field(default_factory=lambda: ["en"])


CONFIG = Config()



class PhotosDB:


    REQUIRED_COLUMNS = {
        "tags_json":         "TEXT",
        "categories_json":   "TEXT",
        "caption":           "TEXT",
        "detailed_caption":  "TEXT",
        "ocr_text":          "TEXT",
        "embedding":         "BLOB",
        "processed_at":      "TEXT",
    }

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._ensure_columns()
    def _ensure_columns(self):
        conn = self._get_conn()

        try:
            existing = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(photos)"
                ).fetchall()
            }

            for column, column_type in self.REQUIRED_COLUMNS.items():
                if column not in existing:
                    conn.execute(
                        f"ALTER TABLE photos ADD COLUMN "
                        f"{column} {column_type}"
                    )

            conn.commit()

        finally:
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=60
        )

        conn.row_factory = sqlite3.Row


        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA temp_store=MEMORY")

        return conn

    def execute_batch(self, sql, values):
        conn = self._get_conn()

        try:
            conn.executemany(sql, values)
            conn.commit()
        finally:
            conn.close()

    def update_embeddings_batch(self, items):
        values = [
            (
                embedding.astype(np.float32).tobytes(),
                photo_id
            )
            for photo_id, embedding in items
        ]

        self.execute_batch(
            "UPDATE photos SET embedding = ? WHERE id = ?",
            values
        )

    def _ensure_loaded(self):
        if self.siglip is None:
            self.siglip = SigLIPModel()
            self.siglip.load()

        if self._embeddings is None:
            self._ids, self._embeddings = self.db.get_all_embeddings()


    def get_unprocessed_ids(self) -> list[dict]:
        """
        Return rows where processing hasn't happened yet.
        'Not processed' = processed_at IS NULL
        This means: first run tags everything, subsequent runs skip them.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT id, filepath, filename
                FROM photos
                WHERE processed_at IS NULL
                ORDER BY id
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_needs_tags(self) -> list[dict]:
        """Photos that have no tags yet."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT id, filepath, filename
                FROM photos
                WHERE tags_json IS NULL
                ORDER BY id
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_needs_categories(self) -> list[dict]:
        """Photos that have no categories yet."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT id, filepath, filename
                FROM photos
                WHERE categories_json IS NULL
                ORDER BY id
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_needs_caption(self) -> list[dict]:
        """Photos that have no caption yet."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT id, filepath, filename
                FROM photos
                WHERE caption IS NULL
                ORDER BY id
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_needs_ocr(self) -> list[dict]:
        """Photos that have no OCR text yet."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT id, filepath, filename
                FROM photos
                WHERE ocr_text IS NULL
                ORDER BY id
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_needs_embedding(self) -> list[dict]:
        """Photos that have no embedding yet."""
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT id, filepath, filename
                FROM photos
                WHERE embedding IS NULL
                ORDER BY id
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_tags(self, photo_id: int, tags: dict):
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE photos SET tags_json = ? WHERE id = ?",
                (json.dumps(tags), photo_id)
            )
            conn.commit()
        finally:
            conn.close()

    def update_categories(self, photo_id: int, categories: dict):
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE photos SET categories_json = ? WHERE id = ?",
                (json.dumps(categories), photo_id)
            )
            conn.commit()
        finally:
            conn.close()

    def update_captions(self, photo_id: int, caption: str, detailed: str):
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE photos SET caption = ?, detailed_caption = ? WHERE id = ?",
                (caption, detailed, photo_id)
            )
            conn.commit()
        finally:
            conn.close()

    def update_ocr(self, photo_id: int, text: str):
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE photos SET ocr_text = ? WHERE id = ?",
                (text, photo_id)
            )
            conn.commit()
        finally:
            conn.close()

    def update_embedding(self, photo_id: int, embedding: np.ndarray):
        blob = embedding.astype(np.float32).tobytes()
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE photos SET embedding = ? WHERE id = ?",
                (blob, photo_id)
            )
            conn.commit()
        finally:
            conn.close()

    def mark_processed(self, photo_id: int):
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE photos SET processed_at = ? WHERE id = ?",
                (now, photo_id)
            )
            conn.commit()
        finally:
            conn.close()

    
    def mark_processed_batch(self, photo_ids: list[int]):
        if not photo_ids:
            return

        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()

        try:
            for start in range(0, len(photo_ids), 500):
                batch = photo_ids[start:start + 500]
                placeholders = ",".join("?" for _ in batch)

                conn.execute(f"""
                    UPDATE photos
                    SET processed_at = ?
                    WHERE tags_json IS NOT NULL
                    AND embedding IS NOT NULL
                    AND caption IS NOT NULL
                    AND detailed_caption IS NOT NULL
                    AND ocr_text IS NOT NULL
                    AND id IN ({placeholders})
                """, [now] + batch)

            conn.commit()

        finally:
            conn.close()


    def update_fts(self, photo_id: int):
        conn = self._get_conn()
        try:
            has_fts = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='photos_fts'"
            ).fetchone()
            if not has_fts:
                return

            row = conn.execute("""
                SELECT filename, caption, detailed_caption, ocr_text,
                       tags_json, categories_json
                FROM photos WHERE id = ?
            """, (photo_id,)).fetchone()

            if not row:
                return

            tags_text = ""
            cats_text = ""
            if row["tags_json"]:
                try:
                    tags_text = " ".join(json.loads(row["tags_json"]).keys())
                except Exception:
                    pass
            if row["categories_json"]:
                try:
                    cats_text = " ".join(json.loads(row["categories_json"]).keys())
                except Exception:
                    pass

            conn.execute("DELETE FROM photos_fts WHERE rowid = ?", (photo_id,))
            conn.execute("""
                INSERT INTO photos_fts(
                    rowid, filename, caption, detailed_caption,
                    ocr_text, tags_text, categories_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                photo_id,
                row["filename"] or "",
                row["caption"] or "",
                row["detailed_caption"] or "",
                row["ocr_text"] or "",
                tags_text,
                cats_text,
            ))
            conn.commit()
        except Exception as e:
            print(f"  FTS update warning for id={photo_id}: {e}")
        finally:
            conn.close()

    
    def update_fts_batch(self, photo_ids: list[int]):
        if not photo_ids:
            return

        conn = self._get_conn()

        try:
            has_fts = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='photos_fts'"
            ).fetchone()

            if not has_fts:
                return

            updates = []

            for start in range(0, len(photo_ids), 500):

                batch_ids = photo_ids[start:start + 500]

                placeholders = ",".join("?" for _ in batch_ids)

                rows = conn.execute(f"""
                    SELECT id, filename, caption, detailed_caption,
                        ocr_text, tags_json, categories_json
                    FROM photos
                    WHERE id IN ({placeholders})
                """, batch_ids).fetchall()

                for row in rows:

                    try:
                        tags = json.loads(row["tags_json"] or "{}")
                        tags_text = " ".join(tags.keys())
                    except Exception:
                        tags_text = ""

                    try:
                        cats = json.loads(row["categories_json"] or "{}")
                        cats_text = " ".join(cats.keys())
                    except Exception:
                        cats_text = ""

                    updates.append((
                        row["id"],
                        row["filename"] or "",
                        row["caption"] or "",
                        row["detailed_caption"] or "",
                        row["ocr_text"] or "",
                        tags_text,
                        cats_text
                    ))

            conn.executemany(
                "DELETE FROM photos_fts WHERE rowid = ?",
                [(item[0],) for item in updates]
            )

            conn.executemany("""
                INSERT INTO photos_fts(
                    rowid, filename, caption, detailed_caption,
                    ocr_text, tags_text, categories_text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, updates)

            conn.commit()

        except Exception as e:
            print(f"  FTS batch update warning: {e}")

        finally:
            conn.close()


    def get_total(self) -> int:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) as cnt FROM photos").fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def get_processed_count(self) -> int:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM photos WHERE processed_at IS NOT NULL"
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def get_all_embeddings(self) -> tuple[list[int], Optional[np.ndarray]]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, embedding FROM photos WHERE embedding IS NOT NULL"
            ).fetchall()
            if not rows:
                return [], None
            ids = []
            embs = []
            for r in rows:
                ids.append(r["id"])
                embs.append(np.frombuffer(r["embedding"], dtype=np.float32))
            return ids, np.stack(embs)
        finally:
            conn.close()



def clear_vram():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
        torch.cuda.ipc_collect()




class SigLIPModel:


    def __init__(self):
        self.device = CONFIG.device
        self.model = None
        self.processor = None

    def load(self):
        from transformers import AutoProcessor, AutoModel

        model_id = CONFIG.siglip_model

        print(f"  Loading SigLIP 2 ({model_id})...")

        self.processor = AutoProcessor.from_pretrained(model_id)

        self.model = AutoModel.from_pretrained(
            model_id,
            torch_dtype=(
                torch.float16 if self.device == "cuda"
                else torch.float32
            )
        ).to(self.device).eval()

        print(f"  Model: {self.model.__class__.__name__}")
        print(f"  Device: {self.device}")
        print("  SigLIP embedding model ready")

    def unload(self):
        self.model = None
        self.processor = None
        clear_vram()

    @torch.inference_mode()
    def get_image_features(self, images):
        inputs = self.processor(
            images=images,
            return_tensors="pt",
            padding=True
        )

        inputs = {
            k: v.to(self.device)
            for k, v in inputs.items()
        }

        with torch.autocast(
            device_type="cuda",
            dtype=torch.float16,
            enabled=self.device == "cuda"
        ):
            outputs = self.model.get_image_features(**inputs)

        return F.normalize(
            outputs.float(),
            p=2,
            dim=-1
        )

    @torch.inference_mode()
    def encode_text(self, query: str) -> np.ndarray:
        inputs = self.processor(
            text=[query],
            return_tensors="pt",
            padding=True,
            truncation=True
        )

        inputs = {
            k: v.to(self.device)
            for k, v in inputs.items()
        }

        with torch.autocast(
            device_type="cuda",
            dtype=torch.float16,
            enabled=self.device == "cuda"
        ):
            outputs = self.model.get_text_features(**inputs)

        embeddings = F.normalize(
            outputs.float(),
            p=2,
            dim=-1
        )

        return embeddings.cpu().numpy().flatten()



class RAMModel:

    def __init__(self):
        self.device = CONFIG.device
        self.model = None
        self.transform = None
        self.inference = None

    def load(self):
        print("  Loading official RAM++...")

        from ram.models import ram_plus
        from ram import get_transform
        from ram import inference_ram as inference

        checkpoint = CONFIG.ram_checkpoint

        if not Path(checkpoint).exists():
            raise FileNotFoundError(
                f"RAM++ checkpoint not found: {checkpoint}"
            )

        self.transform = get_transform(
            image_size=CONFIG.ram_image_size
        )

        self.model = ram_plus(
            pretrained=checkpoint,
            image_size=CONFIG.ram_image_size,
            vit="swin_l"
        )

        self.model = self.model.to(self.device).eval()
        self.inference = inference

        print("  RAM++ ready")
        print(f"  Device: {self.device}")

    def unload(self):
        self.model = None
        self.transform = None
        self.inference = None
        clear_vram()

    @torch.inference_mode()
    def tag_images(
        self,
        images: list[Image.Image]
    ) -> list[dict]:

        results = []

        for image in images:

            image_tensor = self.transform(
                image.convert("RGB")
            ).unsqueeze(0).to(self.device)

            tags, tags_chinese = self.inference(
                image_tensor,
                self.model
            )

            tag_names = [
                tag.strip()
                for tag in tags.split("|")
                if tag.strip()
            ]

            tag_dict = {
                tag: 1.0
                for tag in tag_names
            }

            results.append(tag_dict)

            del image_tensor

        return results



class BLIP2Model:
    def __init__(self):
        self.device = CONFIG.device
        self.pipe_short = None
        self.pipe_detailed = None

    def load(self):
        from transformers import pipeline
        print("  Loading BLIP captioning...")
        self.pipe_short = pipeline(
            "image-to-text",
            model="Salesforce/blip-image-captioning-base",
            device=0 if self.device == "cuda" else -1,
            torch_dtype=torch.float16,
        )
        self.pipe_detailed = pipeline(
            "image-to-text",
            model="Salesforce/blip-image-captioning-large",
            device=0 if self.device == "cuda" else -1,
            torch_dtype=torch.float16,
        )
        print("  BLIP ready (base + large)")

    def unload(self):
        self.pipe_short = self.pipe_detailed = None
        clear_vram()

    def caption(self, image: Image.Image) -> tuple[str, str]:
        image = image.convert("RGB")
        try:
            short = self.pipe_short(image, max_new_tokens=20)[0]["generated_text"]
            detailed = self.pipe_detailed(image, max_new_tokens=50)[0][
                "generated_text"
            ]
            return short, detailed
        except Exception as e:
            print(f"    BLIP error: {e}")
            return "image", "scene"





class EasyOCRModel:

    def __init__(self):
        self.reader = None

        # OCR configuration
        self.max_image_size = 3000
        self.canvas_size = 2560
        self.min_size = 6

        self.detection_batch_size = 8
        self.min_confidence = 0.25

        self.use_second_pass = True

        self.use_clahe = True

    def load(self):
        import easyocr

        print("  Loading EasyOCR...")

        self.reader = easyocr.Reader(
            ["en"],
            gpu=torch.cuda.is_available(),
            verbose=False,
            quantize=not torch.cuda.is_available()
        )

        print(
            f"  EasyOCR ready "
            f"(GPU: {torch.cuda.is_available()})"
        )

    def unload(self):
        self.reader = None

        gc.collect()

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

    def _resize(self, image):

        if max(image.size) > self.max_image_size:

            ratio = self.max_image_size / max(image.size)

            new_size = (
                max(1, int(image.width * ratio)),
                max(1, int(image.height * ratio))
            )

            image = image.resize(
                new_size,
                Image.Resampling.LANCZOS
            )

        return image

    def _preprocess(self, image):

        import cv2

        rgb = np.array(image.convert("RGB"))

        if not self.use_clahe:
            return rgb

        gray = cv2.cvtColor(
            rgb,
            cv2.COLOR_RGB2GRAY
        )

        clahe = cv2.createCLAHE(
            clipLimit=2.0,
            tileGridSize=(8, 8)
        )

        enhanced = clahe.apply(gray)

        return enhanced

    def _run_ocr(self, image, enhanced=False):

        if enhanced:
            processed = self._preprocess(image)
        else:
            processed = np.array(
                image.convert("RGB")
            )

        results = self.reader.readtext(
            processed,

            detail=1,
            paragraph=False,

            # Recognition
            decoder="greedy",
            batch_size=self.detection_batch_size,
            workers=0,

            # Detection
            text_threshold=0.65,
            low_text=0.35,
            link_threshold=0.45,

            canvas_size=self.canvas_size,
            mag_ratio=1.0,
            min_size=self.min_size,

            # Contrast
            contrast_ths=0.1,
            adjust_contrast=0.5,

            # Bounding boxes
            slope_ths=0.1,
            ycenter_ths=0.5,
            height_ths=0.5,
            width_ths=0.5,
            add_margin=0.1
        )

        return results

    @staticmethod
    def _normalize(text):

        import re
        import unicodedata

        text = unicodedata.normalize(
            "NFKC",
            text
        )

        text = text.replace("\x00", "")

        text = re.sub(
            r"\s+",
            " ",
            text
        )

        return text.strip()

    @staticmethod
    def _sort_reading_order(results):

        def position(item):

            box = item[0]

            x = min(point[0] for point in box)
            y = min(point[1] for point in box)

            height = max(
                point[1] for point in box
            ) - y

            return round(y / max(height, 10)), x

        return sorted(
            results,
            key=position
        )

    def _clean_results(self, results):

        if not results:
            return []

        results = self._sort_reading_order(results)

        final = []
        seen = set()

        for box, text, confidence in results:

            text = self._normalize(text)

            if not text:
                continue

            if confidence < self.min_confidence:
                continue

            # Ignore single-character noise, but preserve meaningful digits.
            if len(text) == 1 and not text.isdigit():
                continue

            key = text.casefold()

            if key in seen:
                continue

            seen.add(key)

            final.append(text)

        return final

    def extract_text(self, image: Image.Image) -> str:

        if self.reader is None:
            return ""

        try:

            image = image.convert("RGB")
            image = self._resize(image)

            # First pass: original image
            results = self._run_ocr(
                image,
                enhanced=False
            )

            cleaned = self._clean_results(results)

            # Recovery pass: enhanced contrast only run if the first pass finds very little.
            if (
                self.use_second_pass
                and len(cleaned) < 2
            ):

                enhanced_results = self._run_ocr(
                    image,
                    enhanced=True
                )

                enhanced_cleaned = self._clean_results(
                    enhanced_results
                )

                # Prefer the result with more recognized text.
                if sum(map(len, enhanced_cleaned)) > sum(
                    map(len, cleaned)
                ):
                    cleaned = enhanced_cleaned

            return " ".join(cleaned)

        except Exception as e:

            print(
                f"    EasyOCR ERROR: {repr(e)}"
            )

            return ""





    
def load_image_for_model(
        filepath: str,
        max_size: Optional[int] = None
    ) -> Optional[Image.Image]:

        if max_size is None:
            max_size = CONFIG.max_image_size

        try:
            with Image.open(filepath) as source:
                img = source.convert("RGB")

            if max(img.size) > max_size:
                ratio = max_size / max(img.size)

                new_size = (
                    int(img.width * ratio),
                    int(img.height * ratio)
                )

                img = img.resize(
                    new_size,
                    Image.Resampling.LANCZOS
                )

            return img

        except Exception as e:
            print(f"    Cannot load {filepath}: {e}")
            return None


class ImageProcessor:
    def __init__(self, db: PhotosDB):
        self.db = db
        self.image_executor = ThreadPoolExecutor(
        max_workers=CONFIG.image_workers
    )

    def process_all(self):

        total = self.db.get_total()
        done = self.db.get_processed_count()
        print(f"\n{'='*60}")
        print(f"  Image Categorization Pipeline")
        print(f"  Total photos in DB : {total}")
        print(f"  Already processed  : {done}")
        print(f"  Device             : {CONFIG.device}")
        print(f"{'='*60}")

        needs_tags       = self.db.get_needs_tags()

        needs_embedding  = self.db.get_needs_embedding()
        needs_caption    = self.db.get_needs_caption()
        needs_ocr        = self.db.get_needs_ocr()

        print(f"\n  Need tags       : {len(needs_tags)}")

        print(f"  Need embeddings : {len(needs_embedding)}")
        print(f"  Need captions   : {len(needs_caption)}")
        print(f"  Need OCR        : {len(needs_ocr)}")

        anything = (
            needs_tags or needs_embedding
            or needs_caption or needs_ocr
        )
        if not anything:
            print("\n  ✓ Nothing to do — all photos already processed.")
            return

        # Phase 1: RAM++ tags
        if needs_tags:
            self._run_ram(needs_tags)

        # Phase 2: SigLIP embeddings 

        if needs_embedding:
            self._run_siglip(needs_embedding)

        # Phase 3: BLIP captions
        if needs_caption:
            self._run_blip(needs_caption)

        # Phase 4: OCR
        if needs_ocr:
            self._run_ocr(needs_ocr)

        # Mark everything processed + rebuild FTS
        all_touched_ids = set()

        for lst in [
            needs_tags,
            needs_embedding,
            needs_caption,
            needs_ocr
        ]:
            for r in lst:
                all_touched_ids.add(r["id"])

        print(f"\n  Updating FTS for {len(all_touched_ids)} photos...")

        self.db.update_fts_batch(
        sorted(all_touched_ids)
        )

        self.db.mark_processed_batch(
            sorted(all_touched_ids)
        )
        print(f"\n  ✓ Done — {len(all_touched_ids)} photos processed")


    def _load_batch_images(self, rows):

        futures = [
            self.image_executor.submit(
                load_image_for_model,
                row["filepath"],
                CONFIG.max_image_size
            )
            for row in rows
        ]

        loaded = []

        for row, future in zip(rows, futures):
            img = future.result()

            if img is not None:
                loaded.append((row, img))

        return loaded
    def close(self):
        self.image_executor.shutdown(wait=True)

    
    def _run_ram(self, rows: list[dict]):
        print(f"\n  Phase 1/4: RAM++ Tagging ({len(rows)} images)")
        model = RAMModel()
        model.load()

        batch_size = CONFIG.batch_size_ram

        for i in tqdm(
            range(0, len(rows), batch_size),
            desc="  Tagging"
        ):
            batch_rows = rows[i:i + batch_size]

            loaded = self._load_batch_images(batch_rows)

            if not loaded:
                continue

            pil_imgs = [img for _, img in loaded]

            try:
                tags_list = model.tag_images(pil_imgs)

                tag_updates = []

                for (row, _), tags in zip(loaded, tags_list):
                    tag_updates.append((
                        json.dumps(tags),
                        row["id"]
                    ))

                if tag_updates:
                    self.db.execute_batch(
                        "UPDATE photos SET tags_json = ? WHERE id = ?",
                        tag_updates
                    )

            except Exception as e:
                print(f"    Batch error: {e}")

            finally:
                del pil_imgs
                del loaded

        model.unload()


    
    def _run_siglip(self, rows: list[dict]):
        total = len(rows)

        print(
            f"\n  Phase 2/4: SigLIP Image Embeddings ({total} images)"
        )

        model = SigLIPModel()
        model.load()

        batch_size = CONFIG.batch_size_embedding

        try:
            for i in tqdm(
                range(0, total, batch_size),
                desc="  Embedding"
            ):
                batch_rows = rows[i:i + batch_size]
                loaded = self._load_batch_images(batch_rows)

                if not loaded:
                    continue

                images = [img for _, img in loaded]

                try:
                    image_embeds = model.get_image_features(images)

                    embeddings = image_embeds.cpu().numpy()

                    updates = []

                    for (row, _), embedding in zip(loaded, embeddings):
                        updates.append((
                            row["id"],
                            embedding
                        ))

                    if updates:
                        self.db.update_embeddings_batch(updates)

                except Exception as e:
                    print(f"    SigLIP batch error: {e}")

                finally:
                    del images
                    del loaded

        finally:
            model.unload()


    
    def _run_blip(self, rows: list[dict]):
        print(f"\n  Phase 3/4: BLIP Captioning ({len(rows)} images)")

        model = BLIP2Model()
        model.load()

        batch_size = CONFIG.batch_size_blip

        for i in tqdm(
            range(0, len(rows), batch_size),
            desc="  Captioning"
        ):
            batch_rows = rows[i:i + batch_size]
            loaded = self._load_batch_images(batch_rows)

            if not loaded:
                continue

            images = [img for _, img in loaded]

            try:
                short_results = model.pipe_short(
                    images,
                    batch_size=batch_size,
                    max_new_tokens=20
                )

                detailed_results = model.pipe_detailed(
                    images,
                    batch_size=batch_size,
                    max_new_tokens=50
                )

                updates = []

                for idx, (row, _) in enumerate(loaded):
                    caption = short_results[idx][0]["generated_text"]
                    detailed = detailed_results[idx][0]["generated_text"]

                    updates.append((
                        caption,
                        detailed,
                        row["id"]
                    ))

                self.db.execute_batch(
                    """
                    UPDATE photos
                    SET caption = ?, detailed_caption = ?
                    WHERE id = ?
                    """,
                    updates
                )

            except Exception as e:
                print(f"    BLIP batch error: {e}")

            finally:
                del images
                del loaded

        model.unload()

    def load_image_for_ocr(self, filepath: str):
        try:
            with Image.open(filepath) as source:
                return source.convert("RGB")
        except Exception as e:
            print(f"    OCR image error {filepath}: {e}")
            return None
        
    def _run_ocr(self, rows: list[dict]):
        print(f"\n  Phase 4/4: EasyOCR ({len(rows)} images)")
        model = EasyOCRModel()
        model.load()
        if model.reader is None:
            print("  Skipping OCR (not installed)")

            for row in rows:
                self.db.update_ocr(row["id"], "")
            return

        for row in tqdm(rows, desc="  OCR"):
            img = self.load_image_for_ocr(row["filepath"])
            if not img:
                self.db.update_ocr(row["id"], "")
                continue
            try:
                text = model.extract_text(img)
                self.db.update_ocr(row["id"], text)
            except Exception as e:
                print(f"    Error {row['filename']}: {e}")
                self.db.update_ocr(row["id"], "")

        model.unload()



class ImageSearcher:
    def __init__(self, db: PhotosDB):
        self.db = db
        self.siglip = None
        self._ids = None
        self._embeddings = None

    def _ensure_loaded(self):
        if self.siglip is None:
            self.siglip = SigLIPModel()
            self.siglip.load()
        if self._embeddings is None:
            self._ids, self._embeddings = self.db.get_all_embeddings()

    def semantic_search(
        self, query: str, top_k: int = 20
    ) -> list[tuple[int, float]]:

        self._ensure_loaded()

        if self._embeddings is None or len(self._embeddings) == 0:
            return []

        query_emb = self.siglip.encode_text(query)

        if self._embeddings.ndim != 2:
            raise ValueError("Invalid image embedding matrix")

        if self._embeddings.shape[1] != query_emb.shape[0]:
            raise ValueError(
                "Embedding dimension mismatch. "
                "Check that image and text embeddings use the same model."
            )

        if len(self._ids) != len(self._embeddings):
            raise ValueError("Embedding IDs and matrix rows do not match")

        embeddings = self._embeddings.astype(np.float32, copy=False)

        sims = embeddings @ query_emb.astype(np.float32, copy=False)

        top_k = max(1, min(top_k, len(sims)))

        top_idx = np.argsort(sims)[::-1][:top_k]

        return [
            (self._ids[i], float(sims[i]))
            for i in top_idx
        ]

    def unload(self):
        if self.siglip:
            self.siglip.unload()


def print_gpu_stats(label="GPU Stats"):
    stats = gpu_stats()

    if not stats:
        print(f"\n  {label}: CUDA not available")
        return

    print(
        f"\n  {label}: "
        f"Allocated: {stats['allocated']:.2f} GB | "
        f"Reserved: {stats['reserved']:.2f} GB | "
        f"Peak: {stats['peak']:.2f} GB"
    )
def gpu_stats():
    if not torch.cuda.is_available():
        return {}

    return {
        "allocated": round(
            torch.cuda.memory_allocated() / 1024**3, 2
        ),
        "reserved": round(
            torch.cuda.memory_reserved() / 1024**3, 2
        ),
        "peak": round(
            torch.cuda.max_memory_allocated() / 1024**3, 2
        ),
    }    

def main():
    parser = argparse.ArgumentParser(
        description="Categorize images in the photos database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Process images:  python categorize.py
  Custom DB path:  python categorize.py --database ./images/.photos.db
  Search:          python categorize.py --search "sunset beach"
  Stats:           python categorize.py --stats
        """,
    )
    parser.add_argument(
        "--database", "-d",
        type=Path,
        default=Path(r"H:\Imagesorter\images\.photos.db"),
        help="Path to .photos.db (default: ./images/.photos.db)",
    )
    parser.add_argument("--search", "-s", type=str, help="Semantic search query")
    parser.add_argument("--stats", action="store_true", help="Show database stats")
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process ALL photos (ignore processed_at)",
    )

    args = parser.parse_args()

    if not args.database.exists():
        print(f"\n  ✗ Database not found: {args.database}")
        print(f"    Start the gallery backend first, or specify --database")
        sys.exit(1)

    db = PhotosDB(args.database)

    if args.stats:
        total = db.get_total()
        done = db.get_processed_count()
        print(f"\n  Database       : {args.database}")
        print(f"  Total photos   : {total}")
        print(f"  Processed      : {done}")
        print(f"  Remaining      : {total - done}")
        return

    if args.search:
        print(f"\n   Searching: '{args.search}'")
        searcher = ImageSearcher(db)
        results = searcher.semantic_search(args.search, top_k=10)
        if results:
            for i, (photo_id, score) in enumerate(results, 1):
                conn = db._get_conn()
                row = conn.execute(
                    "SELECT filename FROM photos WHERE id = ?", (photo_id,)
                ).fetchone()
                conn.close()
                name = row["filename"] if row else f"id={photo_id}"
                print(f"    {i:2d}. {name}  (score: {score:.3f})")
        else:
            print("    No results")
        searcher.unload()
        return

    if args.force:
        print("  Force mode: clearing processed_at for all photos...")
        conn = db._get_conn()
        conn.execute(
            "UPDATE photos SET processed_at = NULL, tags_json = NULL, "
            "categories_json = NULL, caption = NULL, detailed_caption = NULL, "
            "ocr_text = NULL, embedding = NULL"
        )
        conn.commit()
        conn.close()

    processor = ImageProcessor(db)

    try:
        processor.process_all()
    finally:
        processor.close()

 

if __name__ == "__main__":
    main()
