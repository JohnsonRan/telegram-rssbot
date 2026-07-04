import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlparse

from PIL import Image, ImageSequence
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


class StickerKind(str, Enum):
    STATIC = "static"
    ANIMATED = "animated"
    VIDEO = "video"


class ConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class StickerConverterSettings:
    max_size: int = 240
    fps: int = 24
    max_bytes: int = 1_000_000
    background: str = "white"

    def __post_init__(self):
        self.background_rgba  # validate eagerly so bad settings fail at construction

    @classmethod
    def from_env(cls):
        return cls(
            max_size=_positive_int("TG2GIF_MAX_SIZE", 240),
            fps=_positive_int("TG2GIF_FPS", 24),
            max_bytes=_positive_int("TG2GIF_MAX_BYTES", 1_000_000),
            background=os.getenv("TG2GIF_BACKGROUND", "white").strip().lower() or "white",
        )

    @property
    def background_rgba(self):
        if self.background in {"transparent", "none", "alpha"}:
            return (0, 0, 0, 0)
        if self.background in {"white", "#fff", "#ffffff"}:
            return (255, 255, 255, 255)
        raise ConversionError("TG2GIF_BACKGROUND 只支持 white 或 transparent。")


@dataclass(frozen=True)
class ConversionResult:
    path: Path
    bytes_size: int


@dataclass(frozen=True)
class FrameSourceInfo:
    fps: float | None = None
    duration_ms: int | None = None


async def sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Convert a replied Telegram sticker or image to a WeChat-friendly file."""
    message = update.message
    if not message:
        return

    target = message.reply_to_message
    if not target:
        await message.reply_text("请回复一个贴纸、照片或图片文件后发送 /sticker。")
        return

    source = _extract_source(target)
    if not source:
        await message.reply_text("请回复一个贴纸、照片或图片文件后发送 /sticker。")
        return

    status = await message.reply_text("正在转换 GIF。")

    try:
        settings = StickerConverterSettings.from_env()
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        with TemporaryDirectory(prefix="rssbot_sticker_") as tmp:
            tmp_dir = Path(tmp)
            telegram_file = await source["file_source"].get_file()
            input_path = tmp_dir / f"input{source['input_suffix'](telegram_file.file_path)}"
            output_filename = _output_filename(
                source["name_hint"](telegram_file.file_path),
                source["fallback"],
                _output_suffix(source["kind"]),
            )
            output_path = tmp_dir / output_filename

            await telegram_file.download_to_drive(input_path)
            result = await convert_to_file(input_path, output_path, source["kind"], settings)

            with result.path.open("rb") as output_file:
                await message.reply_document(
                    document=output_file,
                    filename=output_filename,
                    caption=f"转换完成，大小 {result.bytes_size // 1024} KB。",
                    disable_content_type_detection=True,
                )
    except ConversionError as exc:
        logger.exception("Sticker conversion failed")
        await message.reply_text(f"转换失败：{exc}")
    except Exception:
        logger.exception("Unexpected sticker conversion error")
        await message.reply_text("转换失败：机器人运行时发生错误。")
    finally:
        try:
            await status.delete()
        except Exception:
            logger.debug("Could not delete sticker conversion status message", exc_info=True)


def _extract_source(message):
    if message.sticker:
        kind = _sticker_kind(message.sticker)
        return {
            "file_source": message.sticker,
            "kind": kind,
            "input_suffix": lambda file_path: _sticker_input_suffix(kind, file_path),
            "name_hint": lambda file_path: file_path,
            "fallback": message.sticker.file_unique_id,
        }

    if message.document and _is_image_document(message.document):
        document = message.document
        return {
            "file_source": document,
            "kind": StickerKind.STATIC,
            "input_suffix": lambda file_path: _image_suffix(document.file_name, file_path),
            "name_hint": lambda file_path: document.file_name or file_path,
            "fallback": document.file_unique_id,
        }

    if message.photo:
        photo = message.photo[-1]
        return {
            "file_source": photo,
            "kind": StickerKind.STATIC,
            "input_suffix": lambda file_path: _image_suffix(None, file_path),
            "name_hint": lambda file_path: file_path,
            "fallback": photo.file_unique_id,
        }

    return None


def _is_image_document(document) -> bool:
    mime_type = (document.mime_type or "").lower()
    if mime_type.startswith("image/"):
        return True
    return Path(document.file_name or "").suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _sticker_kind(sticker) -> StickerKind:
    if getattr(sticker, "is_video", False):
        return StickerKind.VIDEO
    if getattr(sticker, "is_animated", False):
        return StickerKind.ANIMATED
    return StickerKind.STATIC


def _sticker_input_suffix(kind: StickerKind, file_path: str | None = None) -> str:
    suffix = Path(file_path or "").suffix.lower()
    if suffix in {".webp", ".png", ".tgs", ".webm"}:
        return suffix
    return {
        StickerKind.STATIC: ".webp",
        StickerKind.ANIMATED: ".tgs",
        StickerKind.VIDEO: ".webm",
    }[kind]


def _image_suffix(file_name: str | None, file_path: str | None) -> str:
    for value in (file_name, file_path):
        suffix = Path(value or "").suffix.lower()
        if suffix:
            return suffix
    return ".jpg"


def _output_suffix(kind: StickerKind) -> str:
    if kind == StickerKind.STATIC:
        return ".png"
    return ".gif"


def _output_filename(file_path: str | None, fallback: str, suffix: str) -> str:
    raw_name = ""
    if file_path:
        parsed = urlparse(file_path)
        path = parsed.path if parsed.scheme else file_path
        raw_name = Path(unquote(path)).name

    stem = Path(raw_name).stem if raw_name else fallback
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" .")
    safe_fallback = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", fallback).strip(" .")
    return f"{safe_stem or safe_fallback or 'sticker'}{suffix}"


async def convert_to_file(
    input_path: Path,
    output_path: Path,
    kind: StickerKind,
    settings: StickerConverterSettings,
) -> ConversionResult:
    return await asyncio.to_thread(_convert_to_file, input_path, output_path, kind, settings)


def _convert_to_file(
    input_path: Path,
    output_path: Path,
    kind: StickerKind,
    settings: StickerConverterSettings,
) -> ConversionResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if kind == StickerKind.STATIC:
        _convert_static_image(input_path, output_path, settings)
    elif kind == StickerKind.VIDEO:
        _convert_video(input_path, output_path, settings)
    elif kind == StickerKind.ANIMATED:
        _convert_tgs(input_path, output_path, settings)
    else:
        raise ConversionError(f"不支持的贴纸类型：{kind}")

    return ConversionResult(path=output_path, bytes_size=output_path.stat().st_size)


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConversionError(f"{name} 必须是正整数。") from exc
    if value <= 0:
        raise ConversionError(f"{name} 必须是正整数。")
    return value


def _size_ladder(settings: StickerConverterSettings) -> list[int]:
    sizes = [settings.max_size] + [min(settings.max_size, n) for n in (220, 200, 180, 160, 128, 128, 128)]
    return [max(16, size) for size in sizes]


def _profiles(
    settings: StickerConverterSettings,
    source_info: FrameSourceInfo | None = None,
) -> list[tuple[int, int]]:
    base_fps = _adaptive_base_fps(settings, source_info)
    fps_ladder = _fps_ladder(base_fps, source_info.fps if source_info else None)
    sizes = _size_ladder(settings)
    # Pad the fps ladder with its last rung so every size gets a partner.
    fps_ladder = (fps_ladder + [fps_ladder[-1]] * len(sizes))[: len(sizes)]
    seen = set()
    profiles = []
    for size, fps in zip(sizes, fps_ladder):
        profile = (size, max(1, fps))
        if profile not in seen:
            profiles.append(profile)
            seen.add(profile)
    return profiles


def _fps_ladder(base_fps: int, source_fps: float | None) -> list[int]:
    """Build the descending fps steps for the retry ladder.

    When the source frame rate is known, prefer fps values that divide it
    evenly (source/2, source/3, ...) so every kept frame is sampled at a
    constant interval and the GIF plays without judder. Falls back to fixed
    ratios when the source rate is unknown or has too few divisors to form a
    ladder (e.g. prime frame rates). Length may differ from the size ladder;
    _profiles aligns them.
    """
    src = round(source_fps) if source_fps and source_fps > 0 else 0
    if src >= 1:
        ladder = [src // n for n in range(1, src + 1) if src % n == 0 and src // n <= base_fps]
        if len(ladder) >= 4:
            return ladder

    return [
        base_fps,
        min(base_fps, max(20, round(base_fps * 0.75))),
        min(base_fps, max(15, round(base_fps * 0.625))),
        min(base_fps, max(12, round(base_fps * 0.5))),
        min(base_fps, 10),
        min(base_fps, 8),
        min(base_fps, 6),
        min(base_fps, 4),
    ]


def _adaptive_base_fps(
    settings: StickerConverterSettings,
    source_info: FrameSourceInfo | None,
) -> int:
    fps = settings.fps
    if source_info and source_info.fps and source_info.fps > 0:
        fps = min(fps, max(1, round(source_info.fps)))

    if source_info and source_info.duration_ms and source_info.duration_ms > 0:
        frame_budget_fps = math.ceil(120_000 / source_info.duration_ms)
        fps = min(fps, max(4, frame_budget_fps))

    return max(1, fps)


def _convert_static_image(input_path: Path, output_path: Path, settings: StickerConverterSettings) -> None:
    try:
        # First frame only; animated inputs routed here would waste memory decoding fully.
        with Image.open(input_path) as image:
            first_frame = image.convert("RGBA")
    except Exception as exc:
        raise ConversionError("静态图片或贴纸转换失败。") from exc

    last_error = None
    for size in dict.fromkeys(_size_ladder(settings)):
        try:
            _save_png_frame(_fit_frame(first_frame, size, settings), output_path)
            if _within_limit(output_path, settings):
                return
        except Exception as exc:
            last_error = exc
            break

    if _accept_oversized(output_path, settings):
        return
    raise ConversionError("静态图片或贴纸转换失败。") from last_error


def _save_png_frame(frame: Image.Image, output_path: Path) -> None:
    frame.save(output_path, format="PNG", optimize=True)


def _convert_video(input_path: Path, output_path: Path, settings: StickerConverterSettings) -> None:
    if not shutil.which("ffmpeg"):
        raise ConversionError("未找到 ffmpeg。请先安装 ffmpeg。")

    source_info = _probe_video_source_info(input_path)
    top_size, top_fps = _profiles(settings, source_info)[0]
    try:
        frames, durations = _extract_video_frames(input_path, top_size, top_fps)
    except subprocess.CalledProcessError as exc:
        logger.warning("ffmpeg conversion failed: %s", exc.stderr)
        raise ConversionError("视频贴纸转换失败。") from exc

    _encode_gif_profiles(frames, durations, output_path, settings, "视频贴纸转换失败。")


def _probe_video_source_info(input_path: Path) -> FrameSourceInfo:
    executable = shutil.which("ffprobe")
    if not executable:
        return FrameSourceInfo()

    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,r_frame_rate,duration:format=duration",
                "-of",
                "json",
                str(input_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        logger.debug("Could not probe video source info", exc_info=True)
        return FrameSourceInfo()

    streams = payload.get("streams") or []
    stream = streams[0] if streams else {}
    format_info = payload.get("format") or {}
    fps = _parse_frame_rate(stream.get("avg_frame_rate")) or _parse_frame_rate(stream.get("r_frame_rate"))
    duration_ms = _parse_duration_ms(stream.get("duration")) or _parse_duration_ms(format_info.get("duration"))
    return FrameSourceInfo(fps=fps, duration_ms=duration_ms)


def _parse_frame_rate(value) -> float | None:
    if not value:
        return None

    try:
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            rate = float(numerator) / float(denominator)
        else:
            rate = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    if not math.isfinite(rate) or rate <= 0:
        return None
    return rate


def _parse_duration_ms(value) -> int | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return max(20, round(seconds * 1000))


def _extract_video_frames(input_path: Path, size: int, fps: int) -> tuple[list[Image.Image], list[int]]:
    """Decode the video once at the top profile; retries resample these frames in memory."""
    with TemporaryDirectory(prefix="rssbot_webm_") as tmp:
        frame_dir = Path(tmp)
        frame_pattern = frame_dir / "frame_%05d.png"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(input_path),
                "-vf",
                # Scale inside ffmpeg so extracted frames are already small;
                # later profiles downscale these in memory instead of re-decoding.
                f"fps={fps},scale={size}:{size}:force_original_aspect_ratio=decrease:flags=lanczos",
                str(frame_pattern),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        frame_paths = sorted(frame_dir.glob("frame_*.png"))
        if not frame_paths:
            raise ConversionError("视频贴纸没有可用帧。")

        frames = []
        for frame_path in frame_paths:
            with Image.open(frame_path) as image:
                frames.append(image.convert("RGBA"))

        duration = max(20, round(1000 / fps))
        return frames, [duration] * len(frames)


def _convert_tgs(input_path: Path, output_path: Path, settings: StickerConverterSettings) -> None:
    errors = []
    try:
        _convert_tgs_with_rlottie(input_path, output_path, settings)
        if output_path.exists():
            return
    except Exception as exc:
        errors.append(f"rlottie-python: {exc}")

    with TemporaryDirectory(prefix="rssbot_tgs_") as tmp:
        raw_gif = Path(tmp) / "raw.gif"

        try:
            _convert_tgs_with_lottie_cli(input_path, raw_gif)
        except Exception as exc:
            errors.append(f"lottie_convert.py: {exc}")

        if not raw_gif.exists():
            detail = "；".join(errors) if errors else "没有可用的 TGS 转换器"
            raise ConversionError(f"TGS 动态贴纸转换失败：{detail}")

        _compress_gif(raw_gif, output_path, settings)


def _convert_tgs_with_rlottie(
    input_path: Path,
    output_path: Path,
    settings: StickerConverterSettings,
) -> None:
    from rlottie_python import LottieAnimation

    last_error = None
    try:
        with LottieAnimation.from_tgs(str(input_path)) as animation:
            source_fps = max(1, animation.lottie_animation_get_framerate() or settings.fps)
            total_frames = max(0, int(animation.lottie_animation_get_totalframe() or 0))
            if total_frames <= 0:
                raise ConversionError("TGS 动态贴纸没有可用帧。")

            source_info = FrameSourceInfo(
                fps=source_fps,
                duration_ms=round(total_frames / source_fps * 1000),
            )

            for size, fps in _profiles(settings, source_info):
                try:
                    frame_numbers, durations = _sample_frame_numbers(total_frames, source_fps, fps)
                    frames = [
                        _fit_frame(
                            animation.render_pillow_frame(frame_num, width=size, height=size),
                            size,
                            settings,
                        )
                        for frame_num in frame_numbers
                    ]

                    if not frames:
                        raise ConversionError("TGS 动态贴纸没有可用帧。")

                    _save_gif_frames(frames, durations, output_path, settings)
                    if _within_limit(output_path, settings):
                        return
                except Exception as exc:
                    last_error = exc
    except Exception as exc:
        last_error = exc

    if _accept_oversized(output_path, settings):
        return
    raise ConversionError("TGS 动态贴纸渲染失败。") from last_error


def _convert_tgs_with_lottie_cli(input_path: Path, output_path: Path) -> None:
    executable = shutil.which("lottie_convert.py") or shutil.which("lottie_convert")
    if not executable:
        raise ConversionError("未找到 lottie_convert.py。")

    subprocess.run(
        [executable, str(input_path), str(output_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _compress_gif(input_path: Path, output_path: Path, settings: StickerConverterSettings) -> None:
    frames, durations = _load_image_frames(input_path)
    _encode_gif_profiles(frames, durations, output_path, settings, "GIF 压缩失败。")


def _encode_gif_profiles(
    frames: list[Image.Image],
    durations: list[int],
    output_path: Path,
    settings: StickerConverterSettings,
    failure_message: str,
) -> None:
    """Re-encode in-memory frames through the retry ladder until the GIF fits max_bytes."""
    source_info = _image_source_info(frames, durations)
    last_error = None
    for size, fps in _profiles(settings, source_info):
        try:
            sampled_frames, sampled_durations = _sample_frames(frames, durations, fps)
            resized = [_fit_frame(frame, size, settings) for frame in sampled_frames]
            _save_gif_frames(resized, sampled_durations, output_path, settings)
            if _within_limit(output_path, settings):
                return
        except Exception as exc:
            last_error = exc

    if _accept_oversized(output_path, settings):
        return
    raise ConversionError(failure_message) from last_error


def _image_source_info(frames: list[Image.Image], durations: list[int]) -> FrameSourceInfo:
    if len(frames) <= 1:
        return FrameSourceInfo(duration_ms=sum(durations) if durations else None)

    total_ms = max(sum(durations), 20)
    return FrameSourceInfo(fps=len(frames) * 1000 / total_ms, duration_ms=total_ms)


def _load_image_frames(input_path: Path) -> tuple[list[Image.Image], list[int]]:
    frames = []
    durations = []
    with Image.open(input_path) as image:
        for frame in ImageSequence.Iterator(image):
            default_duration = 1000 if not frames else 100
            frames.append(frame.convert("RGBA"))
            durations.append(max(20, int(frame.info.get("duration", default_duration))))

    if not frames:
        raise ConversionError("文件没有可用图像帧。")
    return frames, durations


def _sample_frames(frames: list[Image.Image], durations: list[int], fps: int):
    if len(frames) <= 1:
        return frames, durations

    total_ms = max(sum(durations), 20)
    target_count = max(1, min(len(frames), round(total_ms / 1000 * fps)))
    if target_count >= len(frames):
        return frames, durations

    target_interval = total_ms / target_count
    selected = [frames[0]]
    selected_durations = []
    elapsed = 0
    next_sample_at = target_interval
    carried_duration = durations[0]

    for frame, duration in zip(frames[1:], durations[1:], strict=True):
        elapsed += duration
        if elapsed >= next_sample_at and len(selected) < target_count:
            selected.append(frame)
            selected_durations.append(max(20, carried_duration))
            carried_duration = duration
            next_sample_at += target_interval
        else:
            carried_duration += duration

    selected_durations.append(max(20, carried_duration))
    return selected, selected_durations


def _sample_frame_numbers(total_frames: int, source_fps: float, target_fps: int) -> tuple[list[int], list[int]]:
    if total_frames <= 0:
        return [], []

    safe_source_fps = max(1.0, source_fps)
    if total_frames == 1:
        return [0], [max(20, round(1000 / safe_source_fps))]

    source_duration_ms = total_frames / safe_source_fps * 1000
    target_count = max(1, min(total_frames, round(source_duration_ms / 1000 * max(1, target_fps))))
    if target_count >= total_frames:
        duration = max(20, round(1000 / safe_source_fps))
        return list(range(total_frames)), [duration] * total_frames

    boundaries = [math.floor(index * total_frames / target_count) for index in range(target_count + 1)]
    boundaries[-1] = total_frames
    frame_numbers = []
    durations = []
    for index in range(target_count):
        start = min(boundaries[index], total_frames - 1)
        end = min(max(start + 1, boundaries[index + 1]), total_frames)
        frame_numbers.append(start)
        durations.append(max(20, round((end - start) * 1000 / safe_source_fps)))

    return frame_numbers, durations


def _fit_frame(frame: Image.Image, size: int, settings: StickerConverterSettings) -> Image.Image:
    rgba = frame.convert("RGBA")
    contained_size = _contained_size(rgba.size, (size, size))
    contained = _resize_rgba(rgba, contained_size)
    canvas = Image.new("RGBA", (size, size), settings.background_rgba)
    left = (size - contained.width) // 2
    top = (size - contained.height) // 2
    canvas.alpha_composite(contained, (left, top))
    return canvas


def _contained_size(original: tuple[int, int], target: tuple[int, int]) -> tuple[int, int]:
    width, height = original
    max_width, max_height = target
    if width <= 0 or height <= 0:
        raise ConversionError("图片尺寸无效。")
    scale = min(max_width / width, max_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _resize_rgba(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    if image.size == size:
        return image.copy()
    return image.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")


def _save_gif_frames(
    frames: list[Image.Image],
    durations: list[int],
    output_path: Path,
    settings: StickerConverterSettings,
) -> None:
    if not frames:
        raise ConversionError("没有可保存的 GIF 帧。")

    gif_frames, has_transparency = _prepare_gif_frames(frames, settings)
    first, *rest = gif_frames
    save_kwargs = {
        "format": "GIF",
        "save_all": True,
        "append_images": rest,
        "duration": durations if len(durations) == len(frames) else durations[0],
        "loop": 0,
        "optimize": False,
        "disposal": 2,
    }
    if has_transparency:
        save_kwargs["transparency"] = 0
        save_kwargs["background"] = 0
    first.save(output_path, **save_kwargs)


def _prepare_gif_frames(
    frames: list[Image.Image],
    settings: StickerConverterSettings,
) -> tuple[list[Image.Image], bool]:
    rgba_frames = [frame.convert("RGBA") for frame in frames]
    keep_transparency = settings.background_rgba[3] == 0
    has_transparency = keep_transparency and any(_has_transparent_pixels(frame) for frame in rgba_frames)
    prepared = [_to_paletted_gif_frame(frame, has_transparency) for frame in rgba_frames]
    return prepared, has_transparency


def _has_transparent_pixels(frame: Image.Image) -> bool:
    return frame.getchannel("A").point(lambda value: 255 if value < 16 else 0).getbbox() is not None


def _to_paletted_gif_frame(frame: Image.Image, reserve_transparency: bool) -> Image.Image:
    matte_rgb = (255, 255, 255)
    rgba = frame.convert("RGBA")
    alpha = rgba.getchannel("A")
    matte = Image.new("RGBA", rgba.size, (*matte_rgb, 255))
    matted = Image.alpha_composite(matte, rgba).convert("RGB")

    if not reserve_transparency:
        return matted.quantize(colors=256, method=Image.Quantize.MEDIANCUT)

    transparent_mask = alpha.point(lambda value: 255 if value < 16 else 0)
    quantized = matted.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    source_palette = quantized.getpalette() or []
    if len(source_palette) < 255 * 3:
        source_palette.extend([0] * (255 * 3 - len(source_palette)))

    palette = list(matte_rgb) + source_palette[: 255 * 3]
    if len(palette) < 768:
        palette.extend([0] * (768 - len(palette)))

    # Shift palette indices by one so index 0 is the reserved transparent slot,
    # then stamp 0 into the transparent regions. C-level ops instead of a
    # per-pixel Python loop.
    output = quantized.point(lambda index: min(255, index + 1))
    output.putpalette(palette)
    output.paste(0, mask=transparent_mask)
    output.info["transparency"] = 0
    return output


def _within_limit(path: Path, settings: StickerConverterSettings) -> bool:
    return path.exists() and path.stat().st_size <= settings.max_bytes


def _accept_oversized(path: Path, settings: StickerConverterSettings) -> bool:
    """Best-effort fallback: keep the last output even over max_bytes, but log it."""
    if not path.exists():
        return False
    if not _within_limit(path, settings):
        logger.warning(
            "Sticker output %s is %d bytes, over the %d byte limit; sending anyway",
            path.name, path.stat().st_size, settings.max_bytes,
        )
    return True
