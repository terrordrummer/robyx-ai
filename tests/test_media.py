"""Tests for bot/media.py — image preparation for upload."""

import os
from pathlib import Path

import pytest
from PIL import Image

from media import MediaError, prepare_image_for_upload


@pytest.fixture
def small_png(tmp_path):
    """A tiny PNG that fits every reasonable size cap."""
    path = tmp_path / "small.png"
    Image.new("RGB", (64, 64), color=(255, 0, 0)).save(path, "PNG")
    return str(path)


@pytest.fixture
def large_png(tmp_path):
    """A moderately-large PNG (~several hundred KB) that exceeds a 50 KB cap.

    PNG on a random RGB buffer compresses poorly, so the resulting file is
    noticeably larger than the uncompressed cap we will pass in.
    """
    import random
    random.seed(42)
    img = Image.new("RGB", (800, 800))
    pixels = [
        (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
        for _ in range(800 * 800)
    ]
    img.putdata(pixels)
    path = tmp_path / "large.png"
    img.save(path, "PNG")
    return str(path)


class TestPrepareImageForUpload:
    def test_returns_original_path_when_fits(self, small_png):
        # Cap is 5 MB, the 64x64 PNG fits many times over.
        result = prepare_image_for_upload(small_png, 5 * 1024 * 1024)
        assert result == small_png
        assert Path(result).exists()

    def test_compresses_when_too_big(self, large_png):
        original_size = os.path.getsize(large_png)
        cap = 50 * 1024  # 50 KB
        assert original_size > cap, "test fixture precondition"

        result = prepare_image_for_upload(large_png, cap)

        assert result != large_png
        assert Path(result).exists()
        new_size = os.path.getsize(result)
        assert new_size <= cap, (
            "compressed file %d bytes still exceeds cap %d" % (new_size, cap)
        )
        # The returned file must be a real JPEG (Pillow can open it).
        with Image.open(result) as reopened:
            assert reopened.format == "JPEG"
        os.unlink(result)

    def test_downscales_when_quality_alone_is_not_enough(self, large_png):
        # 5 KB is well below what a full-resolution 800x800 JPEG can hit even
        # at q=40, so the downscale pass must kick in.
        result = prepare_image_for_upload(large_png, 5 * 1024)
        assert result != large_png
        assert os.path.getsize(result) <= 5 * 1024
        with Image.open(result) as reopened:
            # Downscaled: at least one dimension smaller than the original.
            assert reopened.width < 800 or reopened.height < 800
        os.unlink(result)

    def test_converts_rgba_to_rgb_for_jpeg(self, tmp_path):
        # Create an RGBA PNG that's too big for a small cap, force compression.
        path = tmp_path / "rgba.png"
        img = Image.new("RGBA", (500, 500), color=(0, 255, 0, 128))
        img.save(path, "PNG")
        # Ensure it exceeds the cap we pass
        result = prepare_image_for_upload(str(path), 500)
        # JPEG does not support RGBA, so compression must have converted.
        assert result != str(path)
        with Image.open(result) as reopened:
            assert reopened.mode == "RGB"
        os.unlink(result)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(MediaError, match="File not found"):
            prepare_image_for_upload(str(tmp_path / "nope.png"), 1024 * 1024)

    def test_directory_raises(self, tmp_path):
        with pytest.raises(MediaError, match="Not a regular file"):
            prepare_image_for_upload(str(tmp_path), 1024 * 1024)

    def test_invalid_image_raises(self, tmp_path):
        path = tmp_path / "not_an_image.png"
        # Make the file big enough to trigger the compression branch, and
        # well above any imaginable header detection threshold.
        path.write_bytes(b"not a real image" * 10_000)
        with pytest.raises(MediaError, match="Cannot open image"):
            prepare_image_for_upload(str(path), 1024)
