"""Image fetching and preparation for generation.

Only photo URLs that came from iNaturalist API responses (held server-side in a
workspace) are fetched, and each is re-checked against ``photo_url``'s
host/path allowlist. Redirects are not followed. Downloads are size-capped,
counted against the media byte budget, and cached briefly on disk.

Photos are embedded byte-for-byte whenever PowerPoint can display them as-is.
Re-encoding happens only when needed: EXIF rotation (PowerPoint ignores the
orientation tag), CMYK, or formats PowerPoint cannot show (WebP).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .config import Settings
from .inaturalist import photo_url
from .ratelimit import ByteBudget

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 120_000_000  # bound decompression bombs
EMBEDDABLE = {"JPEG": ("image/jpeg", "jpg"), "MPO": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "GIF": ("image/gif", "gif")}


class ImageFetchError(Exception):
    """A photo could not be downloaded. Message is user-safe."""


@dataclass
class PreparedImage:
    path: Path
    width: int
    height: int
    content_type: str
    ext: str


class MediaFetcher:
    def __init__(self, settings: Settings, budget: ByteBudget, http: httpx.Client | None = None):
        self.settings = settings
        self.budget = budget
        self.cache_dir = settings.media_cache_dir
        self.http = http or httpx.Client(
            timeout=settings.media_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=False,
        )
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, photo_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(photo_id, threading.Lock())

    def cached_path(self, photo_id: int) -> Path | None:
        for p in self.cache_dir.glob(f"{int(photo_id)}.*"):
            if p.suffix != ".part":
                return p
        return None

    def fetch_original(self, photo_id: int, api_url: str) -> Path:
        """Download (or reuse the cached) original of a photo; returns its path."""
        url = photo_url(api_url, "original")
        if not url:
            raise ImageFetchError(f"Photo {photo_id} has an untrusted URL and was skipped.")
        if f"/photos/{int(photo_id)}/" not in url:
            raise ImageFetchError(f"Photo {photo_id} URL does not match its id.")
        with self._lock_for(photo_id):
            cached = self.cached_path(photo_id)
            if cached:
                os.utime(cached)  # refresh TTL
                return cached
            # Refuse once the allowance is spent. check(0) can never fail because
            # remaining() clamps at zero, so ask for at least one byte.
            self.budget.check(1)
            ext = url.rsplit(".", 1)[-1].lower()
            final = self.cache_dir / f"{int(photo_id)}.{ext}"
            fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=f"{int(photo_id)}.", suffix=".part")
            total = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    for attempt in range(3):
                        try:
                            with self.http.stream("GET", url, headers={"User-Agent": self.settings.user_agent}) as resp:
                                if resp.status_code in (403, 404):
                                    raise ImageFetchError(f"Photo {photo_id} is no longer available on iNaturalist.")
                                if resp.status_code == 429 or resp.status_code >= 500:
                                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                                if resp.status_code != 200:
                                    raise ImageFetchError(f"Photo {photo_id} could not be downloaded (HTTP {resp.status_code}).")
                                declared = int(resp.headers.get("Content-Length") or 0)
                                if declared > self.settings.max_image_bytes:
                                    raise ImageFetchError(f"Photo {photo_id} is too large.")
                                fh.seek(0)
                                fh.truncate()
                                total = 0
                                try:
                                    for chunk in resp.iter_bytes(256 * 1024):
                                        total += len(chunk)
                                        if total > self.settings.max_image_bytes:
                                            raise ImageFetchError(f"Photo {photo_id} is too large.")
                                        fh.write(chunk)
                                finally:
                                    # Count every byte received, even from an aborted attempt.
                                    self.budget.add(total)
                            break
                        except (httpx.HTTPError,) as exc:
                            if attempt == 2:
                                raise ImageFetchError(f"Photo {photo_id} could not be downloaded.") from exc
                            time.sleep(2 * (attempt + 1))
                _verify_image(Path(tmp), photo_id)
                os.replace(tmp, final)
                return final
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def cleanup(self) -> None:
        """Expire cached originals by age, then trim to the size cap (oldest first)."""
        now = time.time()
        entries = []
        for p in self.cache_dir.iterdir():
            try:
                st = p.stat()
            except OSError:
                continue
            age = now - st.st_mtime
            if age > self.settings.media_cache_ttl_seconds or (p.suffix == ".part" and age > 3600):
                p.unlink(missing_ok=True)
            else:
                entries.append((st.st_mtime, st.st_size, p))
        total = sum(e[1] for e in entries)
        for _, size, p in sorted(entries):
            if total <= self.settings.media_cache_max_bytes:
                break
            p.unlink(missing_ok=True)
            total -= size


def _verify_image(path: Path, photo_id: int) -> None:
    try:
        with Image.open(path) as im:
            im.verify()
    except Exception as exc:  # Pillow raises many types for bad data
        raise ImageFetchError(f"Photo {photo_id} is not a readable image.") from exc


def prepare_for_slide(src: Path, work_dir: Path) -> PreparedImage:
    """Return something PowerPoint can embed, re-encoding only when necessary."""
    with Image.open(src) as im:
        fmt = (im.format or "").upper()
        orientation = 1
        try:
            orientation = im.getexif().get(0x0112, 1) or 1
        except Exception:
            orientation = 1
        needs_convert = fmt not in EMBEDDABLE or orientation not in (1, 0) or im.mode in ("CMYK", "YCbCr", "I;16", "I", "F")
        if not needs_convert:
            ct, ext = EMBEDDABLE[fmt]
            return PreparedImage(src, im.width, im.height, ct, ext)
        im = ImageOps.exif_transpose(im)
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        work_dir.mkdir(parents=True, exist_ok=True)
        if has_alpha:
            out = work_dir / f"{src.stem}.png"
            im.convert("RGBA").save(out, "PNG", optimize=False)
            return PreparedImage(out, im.width, im.height, "image/png", "png")
        out = work_dir / f"{src.stem}.jpg"
        icc = im.info.get("icc_profile")
        rgb = im.convert("RGB")
        kwargs = {"quality": 95, "subsampling": 0, "optimize": True}
        if icc and im.mode != "CMYK":
            kwargs["icc_profile"] = icc
        rgb.save(out, "JPEG", **kwargs)
        return PreparedImage(out, rgb.width, rgb.height, "image/jpeg", "jpg")


def title_background(src: Path, out: Path, position: str = "center", max_width: int = 3840) -> PreparedImage:
    """Full-bleed 16:9 crop, desaturated and darkened so a title reads on a projector.

    The title slide is the only place a photo is ever cropped.
    """
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        # Never upscale beyond what the source supports for a 16:9 cover crop.
        cover_w = min(im.width, int(im.height * 16 / 9))
        width = max(640, min(max_width, cover_w))
        height = round(width * 9 / 16)
        centering = {"top": (0.5, 0.2), "bottom": (0.5, 0.8)}.get(position, (0.5, 0.5))
        im = ImageOps.fit(im, (width, height), method=Image.LANCZOS, centering=centering)
        im = ImageEnhance.Color(im).enhance(0.55)
        im = ImageEnhance.Brightness(im).enhance(0.42)
        im = im.filter(ImageFilter.GaussianBlur(radius=max(1.0, width / 1600)))
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, "JPEG", quality=90, subsampling=0, optimize=True)
        return PreparedImage(out, width, height, "image/jpeg", "jpg")


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
