"""Conservative edge-border removal for downloaded images and videos.

Only near-uniform rows and columns connected to the outside edge are removed.
Darkness alone is never treated as a border, which protects low-key photos and
videos whose subject is surrounded by a black background.
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat


logger = logging.getLogger("MediaProcessor")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov"}

_IMAGE_TOLERANCE = 4
_IMAGE_COVERAGE = 0.995
_VIDEO_TOLERANCE = 6
_VIDEO_COVERAGE = 0.995
_BORDER_COLOR_DRIFT = 6
_STANDARD_MAX_SIDE_RATIO = 0.25
_STANDARD_MIN_RETAINED_AREA = 0.60
_EXTENDED_MAX_SIDE_RATIO = 0.40
_EXTENDED_MIN_RETAINED_AREA = 0.30
_VIDEO_SAMPLE_FRAMES = 16
_VIDEO_CONSENSUS = 0.90


@dataclass(frozen=True)
class EdgeCrop:
    """Pixel counts to remove from the four outside edges."""

    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    @property
    def changed(self) -> bool:
        return any((self.left, self.top, self.right, self.bottom))

    def box(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (self.left, self.top, width - self.right, height - self.bottom)


@dataclass(frozen=True)
class ProcessResult:
    """Outcome from a media-processing attempt."""

    path: Path
    changed: bool
    crop: EdgeCrop = EdgeCrop()
    original_size: tuple[int, int] | None = None
    output_size: tuple[int, int] | None = None
    reason: str = ""
    requires_review: bool = False
    confidence: str = ""


@dataclass(frozen=True)
class VideoCropDecision:
    """Consensus video crop plus whether a person must approve it."""

    crop: EdgeCrop = EdgeCrop()
    requires_review: bool = False
    confidence: str = "none"


def _normalized_rgba(image: Image.Image) -> Image.Image:
    """Return display-oriented RGBA pixels with hidden transparent RGB cleared."""
    displayed = ImageOps.exif_transpose(image)
    rgba = displayed.convert("RGBA")
    return Image.alpha_composite(Image.new("RGBA", rgba.size, (0, 0, 0, 0)), rgba)


def _line_color_and_uniformity(
    line: Image.Image,
    *,
    tolerance: int,
    coverage: float,
) -> tuple[tuple[int, int, int, int], bool]:
    representative = tuple(int(value) for value in ImageStat.Stat(line).median)
    reference = Image.new("RGBA", line.size, representative)
    histogram = ImageChops.difference(line, reference).histogram()
    pixel_count = line.width * line.height
    uniform = all(
        sum(histogram[channel * 256 : channel * 256 + tolerance + 1]) / pixel_count
        >= coverage
        for channel in range(4)
    )
    return representative, uniform


def _scan_edge(
    image: Image.Image,
    side: str,
    *,
    tolerance: int,
    coverage: float,
    min_border: int,
    max_side_ratio: float,
) -> int:
    width, height = image.size
    dimension = height if side in {"top", "bottom"} else width
    max_border = max(min_border, int(dimension * max_side_ratio))

    if side not in {"left", "top", "right", "bottom"}:  # pragma: no cover
        raise ValueError(f"Unknown edge: {side}")
    horizontal = side in {"top", "bottom"}
    reverse = side in {"right", "bottom"}
    line_count = height if horizontal else width
    indices = range(line_count - 1, -1, -1) if reverse else range(line_count)

    def line_at(index: int) -> Image.Image:
        if horizontal:
            return image.crop((0, index, width, index + 1))
        return image.crop((index, 0, index + 1, height))

    border_color: tuple[int, int, int, int] | None = None
    count = 0
    for index in indices:
        color, uniform = _line_color_and_uniformity(
            line_at(index), tolerance=tolerance, coverage=coverage
        )
        if not uniform:
            break
        if border_color is None:
            border_color = color
        elif (
            max(abs(value - base) for value, base in zip(color, border_color))
            > _BORDER_COLOR_DRIFT
        ):
            break
        count += 1
        if count > max_border:
            # A huge flat region may be intentional composition or a solid image.
            return 0

    return count if count >= min_border else 0


def detect_uniform_edges(
    image: Image.Image,
    *,
    tolerance: int = _IMAGE_TOLERANCE,
    coverage: float = _IMAGE_COVERAGE,
    min_border: int = 2,
    max_side_ratio: float = _STANDARD_MAX_SIDE_RATIO,
    min_retained_area: float = _STANDARD_MIN_RETAINED_AREA,
) -> EdgeCrop:
    """Detect conservative, edge-connected, near-uniform rows and columns."""
    rgba = _normalized_rgba(image)
    width, height = rgba.size
    crop = EdgeCrop(
        **{
            side: _scan_edge(
                rgba,
                side,
                tolerance=tolerance,
                coverage=coverage,
                min_border=min_border,
                max_side_ratio=max_side_ratio,
            )
            for side in ("left", "top", "right", "bottom")
        }
    )

    output_width = width - crop.left - crop.right
    output_height = height - crop.top - crop.bottom
    if output_width <= 0 or output_height <= 0:
        return EdgeCrop()
    retained_area = (output_width * output_height) / (width * height)
    if retained_area < min_retained_area:
        return EdgeCrop()
    return crop


def _crop_within_limits(
    crop: EdgeCrop,
    width: int,
    height: int,
    *,
    max_side_ratio: float,
    min_retained_area: float,
) -> bool:
    if not crop.changed:
        return False
    if crop.left / width > max_side_ratio or crop.right / width > max_side_ratio:
        return False
    if crop.top / height > max_side_ratio or crop.bottom / height > max_side_ratio:
        return False
    output_width = width - crop.left - crop.right
    output_height = height - crop.top - crop.bottom
    if output_width <= 0 or output_height <= 0:
        return False
    retained_area = (output_width * output_height) / (width * height)
    return retained_area >= min_retained_area


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}_original.bak")


def _image_save_options(image: Image.Image, image_format: str) -> dict:
    options: dict = {}
    if image.info.get("icc_profile"):
        options["icc_profile"] = image.info["icc_profile"]
    exif = image.getexif()
    if exif:
        exif.pop(274, None)  # Pixels have already been display-oriented.
        options["exif"] = exif.tobytes()
    if image_format == "WEBP":
        options.update(quality=95, method=6)
    elif image_format == "JPEG":
        options.update(quality=95, optimize=True)
    elif image_format == "PNG":
        options.update(optimize=True)
    return options


def process_image(
    path: Path, *, dry_run: bool = False, force_review: bool = False
) -> ProcessResult:
    """Detect and optionally crop a downloaded image, preserving the original."""
    path = Path(path)
    backup = _backup_path(path)
    if backup.exists() and not dry_run:
        return ProcessResult(path, False, reason="original backup already exists")

    try:
        with Image.open(path) as source:
            image_format = source.format or path.suffix.removeprefix(".").upper()
            normalized = _normalized_rgba(source)
            original_size = normalized.size
            crop = detect_uniform_edges(normalized)
            requires_review = False
            if not crop.changed:
                crop = detect_uniform_edges(
                    normalized,
                    max_side_ratio=_EXTENDED_MAX_SIDE_RATIO,
                    min_retained_area=_EXTENDED_MIN_RETAINED_AREA,
                )
                requires_review = crop.changed
            if not crop.changed:
                return ProcessResult(
                    path,
                    False,
                    original_size=original_size,
                    output_size=original_size,
                    reason="no uniform edge border",
                )
            box = crop.box(*original_size)
            output_size = (box[2] - box[0], box[3] - box[1])
            if requires_review and not force_review:
                return ProcessResult(
                    path,
                    False,
                    crop,
                    original_size,
                    output_size,
                    "large image crop requires review",
                    requires_review=True,
                    confidence="review",
                )
            if dry_run:
                return ProcessResult(
                    path,
                    True,
                    crop,
                    original_size,
                    output_size,
                    "dry run",
                    confidence="manual" if force_review else "standard",
                )

            cropped = normalized.crop(box)
            if image_format == "JPEG":
                cropped = cropped.convert("RGB")
            save_options = _image_save_options(source, image_format)
    except (OSError, ValueError) as exc:
        return ProcessResult(path, False, reason=f"image analysis failed: {exc}")

    temporary = path.with_name(f".{path.stem}.crop{path.suffix}")
    path.replace(backup)
    try:
        cropped.save(temporary, format=image_format, **save_options)
        with Image.open(temporary) as check:
            if check.size != output_size:
                raise OSError(
                    f"unexpected output size {check.size}, expected {output_size}"
                )
            check.verify()
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        backup.replace(path)
        raise

    return ProcessResult(
        path,
        True,
        crop,
        original_size,
        output_size,
        "cropped",
        confidence="manual" if requires_review else "standard",
    )


def _probe_video(path: Path) -> tuple[int, int, float] | None:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        values = {}
        for line in completed.stdout.splitlines():
            key, _, value = line.partition("=")
            values[key] = value
        return int(values["width"]), int(values["height"]), float(values["duration"])
    except (KeyError, ValueError, OSError, subprocess.SubprocessError):
        return None


def _probe_video_bitrate(path: Path) -> int | None:
    """Return the source video-stream bitrate when the container reports it."""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=bit_rate",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        bitrate = int(completed.stdout.strip())
        return bitrate if bitrate > 0 else None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def _consensus_value(values: list[int], required: int) -> int:
    """Return the largest crop supported by at least ``required`` samples."""
    positive = [value for value in values if value > 0]
    if len(positive) < required:
        return 0
    return sorted(positive, reverse=True)[required - 1]


def _video_crop_from_frames(
    frame_paths: list[Path], width: int, height: int
) -> VideoCropDecision:
    frame_crops: list[EdgeCrop] = []
    for frame_path in frame_paths:
        with Image.open(frame_path) as frame:
            frame_crops.append(
                detect_uniform_edges(
                    frame,
                    tolerance=_VIDEO_TOLERANCE,
                    coverage=_VIDEO_COVERAGE,
                    min_border=2,
                    max_side_ratio=_EXTENDED_MAX_SIDE_RATIO,
                    min_retained_area=_EXTENDED_MIN_RETAINED_AREA,
                )
            )
    if not frame_crops:
        return VideoCropDecision()

    required = math.ceil(len(frame_crops) * _VIDEO_CONSENSUS)
    values_by_side = {
        side: [getattr(frame_crop, side) for frame_crop in frame_crops]
        for side in ("left", "top", "right", "bottom")
    }
    crop = EdgeCrop(
        **{
            side: _consensus_value(values, required)
            for side, values in values_by_side.items()
        }
    )

    def even_crop(candidate: EdgeCrop) -> EdgeCrop:
        # libx264 requires even dimensions. Crop less rather than risk content.
        left = candidate.left - candidate.left % 2
        top = candidate.top - candidate.top % 2
        right = candidate.right - candidate.right % 2
        bottom = candidate.bottom - candidate.bottom % 2
        if (width - left - right) % 2:
            right = max(0, right - 1)
        if (height - top - bottom) % 2:
            bottom = max(0, bottom - 1)
        return EdgeCrop(left, top, right, bottom)

    crop = even_crop(crop)
    if not _crop_within_limits(
        crop,
        width,
        height,
        max_side_ratio=_EXTENDED_MAX_SIDE_RATIO,
        min_retained_area=_EXTENDED_MIN_RETAINED_AREA,
    ):
        return VideoCropDecision()
    if _crop_within_limits(
        crop,
        width,
        height,
        max_side_ratio=_STANDARD_MAX_SIDE_RATIO,
        min_retained_area=_STANDARD_MIN_RETAINED_AREA,
    ):
        return VideoCropDecision(crop, confidence="standard")

    active_sides = [side for side in values_by_side if getattr(crop, side) > 0]
    paired_axis = (crop.top > 0 and crop.bottom > 0) or (
        crop.left > 0 and crop.right > 0
    )
    all_frames_support = all(
        all(value > 0 for value in values_by_side[side]) for side in active_sides
    )
    stable = all(
        max(values_by_side[side]) - min(values_by_side[side])
        <= max(16, int((height if side in {"top", "bottom"} else width) * 0.02))
        for side in active_sides
    )
    if paired_axis and all_frames_support and stable:
        conservative = even_crop(
            EdgeCrop(
                **{
                    side: min(values_by_side[side]) if side in active_sides else 0
                    for side in values_by_side
                }
            )
        )
        if _crop_within_limits(
            conservative,
            width,
            height,
            max_side_ratio=_EXTENDED_MAX_SIDE_RATIO,
            min_retained_area=_EXTENDED_MIN_RETAINED_AREA,
        ):
            return VideoCropDecision(
                conservative, confidence="high-confidence-large-border"
            )

    return VideoCropDecision(crop, requires_review=True, confidence="review")


def process_video(
    path: Path, *, dry_run: bool = False, force_review: bool = False
) -> ProcessResult:
    """Sample a video across its duration and remove consensus uniform borders."""
    path = Path(path)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return ProcessResult(path, False, reason="ffmpeg or ffprobe unavailable")
    backup = _backup_path(path)
    if backup.exists() and not dry_run:
        return ProcessResult(path, False, reason="original backup already exists")
    probe = _probe_video(path)
    if not probe:
        return ProcessResult(path, False, reason="video probe failed")
    width, height, duration = probe
    if duration <= 0:
        return ProcessResult(path, False, reason="invalid video duration")

    with tempfile.TemporaryDirectory(prefix="media-crop-") as temp_dir:
        frame_pattern = Path(temp_dir) / "frame-%03d.png"
        interval = max(duration / (_VIDEO_SAMPLE_FRAMES + 1), 0.04)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-vf",
                    f"fps=1/{interval}",
                    "-frames:v",
                    str(_VIDEO_SAMPLE_FRAMES),
                    str(frame_pattern),
                ],
                capture_output=True,
                timeout=max(60, int(duration)),
                check=True,
            )
            frame_paths = sorted(Path(temp_dir).glob("frame-*.png"))
            decision = _video_crop_from_frames(frame_paths, width, height)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return ProcessResult(path, False, reason=f"video analysis failed: {exc}")

    original_size = (width, height)
    crop = decision.crop
    if not crop.changed:
        return ProcessResult(
            path,
            False,
            original_size=original_size,
            output_size=original_size,
            reason="no consensus uniform edge border",
        )
    box = crop.box(width, height)
    output_size = (box[2] - box[0], box[3] - box[1])
    if decision.requires_review and not force_review:
        return ProcessResult(
            path,
            False,
            crop,
            original_size,
            output_size,
            "large video crop requires review",
            requires_review=True,
            confidence="review",
        )
    if dry_run:
        return ProcessResult(
            path,
            True,
            crop,
            original_size,
            output_size,
            "dry run",
            confidence="manual" if force_review else decision.confidence,
        )

    temporary = path.with_name(f".{path.stem}.crop{path.suffix}")
    source_bitrate = _probe_video_bitrate(path)
    path.replace(backup)
    try:
        video_encoding = ["-c:v", "libx264", "-preset", "medium"]
        if source_bitrate:
            video_encoding.extend(
                [
                    "-b:v",
                    str(source_bitrate),
                    "-maxrate",
                    str(int(source_bitrate * 1.5)),
                    "-bufsize",
                    str(source_bitrate * 2),
                ]
            )
        else:
            video_encoding.extend(["-crf", "27"])
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(backup),
                "-vf",
                f"crop={output_size[0]}:{output_size[1]}:{crop.left}:{crop.top}",
                "-c:a",
                "copy",
                *video_encoding,
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            capture_output=True,
            timeout=max(300, int(duration * 3)),
            check=True,
        )
        del completed
        output_probe = _probe_video(temporary)
        if not output_probe or output_probe[:2] != output_size:
            raise OSError("cropped video failed dimension validation")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        backup.replace(path)
        raise

    return ProcessResult(
        path,
        True,
        crop,
        original_size,
        output_size,
        "cropped",
        confidence="manual" if force_review else decision.confidence,
    )


def process_media(
    path: Path, *, dry_run: bool = False, force_review: bool = False
) -> ProcessResult:
    """Process one supported image or video."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return process_image(path, dry_run=dry_run, force_review=force_review)
    if suffix in VIDEO_EXTENSIONS:
        return process_video(path, dry_run=dry_run, force_review=force_review)
    return ProcessResult(path, False, reason="unsupported media type")


def log_process_result(
    result: ProcessResult, target_logger: logging.Logger = logger
) -> None:
    """Write a concise, consistent processing result to the service log."""
    if result.changed:
        target_logger.info(
            "Auto-crop: %s %s -> %s, edges L%d T%d R%d B%d for %s",
            "would crop" if result.reason == "dry run" else "cropped",
            result.original_size,
            result.output_size,
            result.crop.left,
            result.crop.top,
            result.crop.right,
            result.crop.bottom,
            result.path.name,
        )
    elif result.requires_review:
        target_logger.warning(
            "Auto-crop: review required %s -> %s, edges L%d T%d R%d B%d for %s",
            result.original_size,
            result.output_size,
            result.crop.left,
            result.crop.top,
            result.crop.right,
            result.crop.bottom,
            result.path.name,
        )
    else:
        target_logger.debug(
            "Auto-crop: skipped %s: %s", result.path.name, result.reason
        )
