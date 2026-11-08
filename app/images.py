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
from .ratelimit import ByteBudget, Reservation

# Bound decompression bombs. Pillow refuses images over twice MAX_IMAGE_PIXELS
# (and only warns between one and two times it), so this refuses over 120 MP.
MAX_DECODE_PIXELS = 120_000_000
Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS // 2
LOCK_STRIPES = 256
# The formats iNaturalist serves. Pillow picks a parser from a file's contents,
# not its name, so only these parsers are ever tried on downloaded bytes: a
# file named .jpg that holds anything else (EPS, which runs Ghostscript, TIFF,
# and so on) is rejected before any other parser touches it.
IMAGE_FORMATS = ("JPEG", "PNG", "GIF", "WEBP")  # parsers Image.open may try
DETECTED_FORMATS = {"JPEG", "MPO", "PNG", "GIF", "WEBP"}  # MPO comes from the JPEG parser
EMBEDDABLE = {"JPEG": ("image/jpeg", "jpg"), "MPO": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "GIF": ("image/gif", "gif")}


class ImageFetchError(Exception):
    """A photo could not be downloaded. Message is user-safe."""


class DownloadCancelled(Exception):
    """The job that wanted this photo was cancelled."""


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
        # One download per photo at a time. A fixed set of locks, shared by
        # photo id, so the table does not grow with every photo ever fetched.
        self._locks = [threading.Lock() for _ in range(LOCK_STRIPES)]

    def _lock_for(self, photo_id: int) -> threading.Lock:
        return self._locks[int(photo_id) % LOCK_STRIPES]

    def cached_path(self, photo_id: int) -> Path | None:
        for p in self.cache_dir.glob(f"{int(photo_id)}.*"):
            if p.suffix != ".part":
                return p
        return None

    def fetch_original(self, photo_id: int, api_url: str, cancelled=None,
                       reservation: Reservation | None = None) -> Path:
        """Download (or reuse the cached) original of a photo; returns its path.

        ``cancelled`` (optional, returns bool) is checked before the download and
        between chunks and retries; DownloadCancelled is raised when it is true.
        Downloaded bytes are taken from ``reservation`` when one is given.
        """
        def stop_if_cancelled():
            if cancelled and cancelled():
                raise DownloadCancelled()

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
            stop_if_cancelled()
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
                                        stop_if_cancelled()
                                        total += len(chunk)
                                        if total > self.settings.max_image_bytes:
                                            raise ImageFetchError(f"Photo {photo_id} is too large.")
                                        fh.write(chunk)
                                finally:
                                    # Count every byte received, even from an aborted attempt.
                                    self.budget.add(total)
                                    if reservation is not None:
                                        reservation.consume(total)
                            break
                        except (httpx.HTTPError,) as exc:
                            if attempt == 2:
                                raise ImageFetchError(f"Photo {photo_id} could not be downloaded.") from exc
                            time.sleep(2 * (attempt + 1))
                            stop_if_cancelled()
                _verify_image(Path(tmp), photo_id)
                os.replace(tmp, final)
                return final
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def fetch_into(self, photo_id: int, api_url: str, dest_dir: Path, cancelled=None,
                   reservation: Reservation | None = None) -> Path:
        """Like fetch_original, but returns a hard link to the photo in
        ``dest_dir`` (a job's own folder). Cleanup may trim the cache while a
        deck is being built; the link keeps the file until the job is done."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            src = self.fetch_original(photo_id, api_url, cancelled, reservation)
            dest = dest_dir / src.name
            try:
                os.link(src, dest)
                return dest
            except FileExistsError:
                return dest
            except FileNotFoundError:
                if attempt:  # trimmed twice in a row; give up on this photo
                    raise ImageFetchError(f"Photo {photo_id} could not be kept for the presentation.") from None
            except OSError:
                try:  # a filesystem without hard links
                    shutil.copyfile(src, dest)
                    return dest
                except FileNotFoundError:
                    if attempt:
                        raise ImageFetchError(f"Photo {photo_id} could not be kept for the presentation.") from None
        raise ImageFetchError(f"Photo {photo_id} could not be kept for the presentation.")

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


def open_image(path: Path) -> Image.Image:
    """Image.open limited to IMAGE_FORMATS. Use it for every downloaded file."""
    im = Image.open(path, formats=IMAGE_FORMATS)
    if im.format not in DETECTED_FORMATS:  # belt and braces: formats= already ensures this
        im.close()
        raise Image.UnidentifiedImageError(f"unexpected image format {im.format!r}")
    return im


def _verify_image(path: Path, photo_id: int) -> None:
    try:
        with open_image(path) as im:
            im.verify()
    except Exception as exc:  # Pillow raises many types for bad data
        raise ImageFetchError(f"Photo {photo_id} is not a readable image.") from exc


def prepare_for_slide(src: Path, work_dir: Path) -> PreparedImage:
    """Return something PowerPoint can embed, re-encoding only when necessary."""
    with open_image(src) as im:
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


ROTATE_CLOCKWISE = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}


def title_background(src: Path, out: Path, position: str = "center", max_width: int = 3840,
                     rotation: int = 0) -> PreparedImage:
    """Full-bleed 16:9 crop, desaturated and darkened so a title reads on a projector.

    The title slide is the only place a photo is ever cropped.
    """
    with open_image(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if rotation in ROTATE_CLOCKWISE:
            im = im.transpose(ROTATE_CLOCKWISE[rotation])
        # Never upscale beyond what the source supports for a 16:9 cover crop.
        cover_w = min(im.width, int(im.height * 16 / 9))
        width = max(1, min(max_width, cover_w))
        height = max(1, round(width * 9 / 16))
        centering = {"top": (0.5, 0.2), "bottom": (0.5, 0.8)}.get(position, (0.5, 0.5))
        im = ImageOps.fit(im, (width, height), method=Image.LANCZOS, centering=centering)
        im = ImageEnhance.Color(im).enhance(0.55)
        im = ImageEnhance.Brightness(im).enhance(0.42)
        im = im.filter(ImageFilter.GaussianBlur(radius=max(1.0, width / 1600)))
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, "JPEG", quality=90, subsampling=0, optimize=True)
        return PreparedImage(out, width, height, "image/jpeg", "jpg")
