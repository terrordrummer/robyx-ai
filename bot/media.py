"""Robyx — media preparation for outgoing files.

Handles image validation and compression for outgoing sends. Platforms have
different upload size limits (Telegram 10 MB for photos, Discord 8 MB free
tier, etc.) and agents must not be burdened with computing byte sizes. The
platform adapter passes its ``max_photo_bytes`` and this module returns a
path to a file that is guaranteed to fit, re-encoding as JPEG if needed.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from PIL import Image

log = logging.getLogger("robyx.media")

# Quality sweep tried for JPEG re-encoding, from best to worst.
_QUALITY_STEPS = (90, 80, 70, 60, 50, 40)
# Downscale factors applied when even q=40 is too big.
_DOWNSCALE_STEPS = (1.0, 0.75, 0.5, 0.35, 0.25)


class MediaError(Exception):
    """Raised when a media file cannot be validated or prepared for upload."""


def prepare_image_for_upload(path: str, max_bytes: int) -> str:
    """Return a filesystem path to an image ≤ ``max_bytes``.

    Behaviour:

    - If the original file already fits, the original path is returned
      unchanged.
    - Otherwise the image is re-encoded as JPEG, trying progressively lower
      quality levels (90 → 40). If that is not enough, the image is
      progressively downscaled (to 75%, 50%, 35%, 25% of each side) and the
      quality sweep is repeated at each scale.
    - The re-encoded file is written to a temporary path (suffix ``.jpg``).
      The caller is responsible for ``os.unlink``'ing the returned path if
      it differs from the input.

    Raises:
        MediaError: if the file does not exist, is not a regular file, is
            not a valid image, or cannot be compressed below ``max_bytes``
            even after the full fallback sweep.
    """
    src = Path(path)
    if not src.exists():
        raise MediaError("File not found: %s" % path)
    if not src.is_file():
        raise MediaError("Not a regular file: %s" % path)

    src_size = src.stat().st_size
    if src_size <= max_bytes:
        log.info(
            "Image fits (%.2f MB ≤ %.2f MB), no compression needed: %s",
            src_size / 1e6, max_bytes / 1e6, path,
        )
        return str(src)

    log.info(
        "Image %s is %.2f MB, exceeds %.2f MB limit — compressing",
        path, src_size / 1e6, max_bytes / 1e6,
    )

    try:
        img = Image.open(src)
        img.load()
    except (OSError, SyntaxError) as e:
        raise MediaError("Cannot open image %s: %s" % (path, e)) from e

    # JPEG cannot encode RGBA / paletted / etc.
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    original_size = img.size

    for scale in _DOWNSCALE_STEPS:
        if scale < 1.0:
            new_w = max(1, int(original_size[0] * scale))
            new_h = max(1, int(original_size[1] * scale))
            scaled = img.resize((new_w, new_h), Image.LANCZOS)
        else:
            scaled = img

        for quality in _QUALITY_STEPS:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="robyx_img_")
            os.close(tmp_fd)
            try:
                scaled.save(tmp_path, "JPEG", quality=quality, optimize=True)
                size = os.path.getsize(tmp_path)
                if size <= max_bytes:
                    log.info(
                        "Compressed %s to %.2f MB (scale=%.2f, quality=%d)",
                        path, size / 1e6, scale, quality,
                    )
                    return tmp_path
                os.unlink(tmp_path)
            except (OSError, ValueError) as e:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                log.warning(
                    "Compression attempt failed at scale=%.2f quality=%d: %s",
                    scale, quality, e,
                )

    raise MediaError(
        "Cannot compress %s below %d bytes even at minimum quality/scale"
        % (path, max_bytes)
    )
