"""Block confirmed Nailong stickers without requiring a collection allowlist.

Unknown ordinary images remain usable. Perceptual fingerprints recognize only
some near-duplicates of confirmed denied images, not all Nailong content. The
model's visual instructions supplement this small local blocklist. The caller
must retain its existing image validation and session/path checks, and call
require_allowed on the actual bytes before copying/sending. Catalog errors do
not disable ordinary stickers; the built-in name/SHA blocks always remain.
"""
from __future__ import annotations
from hakimi import paths

import hashlib
import io
import json
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageStat

BASE_DIR = paths.base()
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_CATALOG_BYTES = 512 * 1024
MAX_ENTRIES = 2048
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
FIXED_DENIED_NAMES = frozenset({
    "blue_maid_dance.gif", "deepseek_yellow_pajamas.jpg",
    "smiling_critter_shocked.gif", "yellow_fat_belly_dance.gif",
})
FIXED_DENIED_SHA256 = frozenset({
    "21f62ed50d917f8baf16be3f08be14b2f581ed86d48d486767f84a6798391918",
    "82d93e1378010dce26c74933f9893fa881116fe20f8a723ef6379be986a5fba1",
    "afdc1fd12dcf6f00312c84e9b04b8a2d6398aee0a91a6dc8339405606e15a0b2",
    "20de2792c18dc0c78eeda39252375bb29e0abc02c2999717ef570b6da3448545",
})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DHASH = re.compile(r"[0-9a-f]{16}\Z")


class StickerPolicyError(ValueError):
    """Invalid image input or a denied image; caller must not save/send."""


def _check_data(data: bytes) -> None:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise StickerPolicyError("图片为空或超过 8 MiB。")


def _check_name(name: str) -> None:
    if (not isinstance(name, str) or not name or len(name) > 180
            or any(c in name for c in '/\\:') or any(ord(c) < 32 for c in name)
            or Path(name).suffix.lower() not in IMAGE_EXTENSIONS):
        raise StickerPolicyError("表情名称必须是受支持图片的单一文件名。")
    if name.casefold() in FIXED_DENIED_NAMES:
        raise StickerPolicyError("该名称属于已禁止的奶龙表情。")


def _pairs_no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise StickerPolicyError("表情记录存在重复字段。")
        out[key] = value
    return out


def _validate_sample(sample) -> None:
    if (not isinstance(sample, dict) or set(sample) != {"dhash", "mean_rgb", "aspect_milli"}
            or not isinstance(sample["dhash"], str) or not _DHASH.fullmatch(sample["dhash"])
            or not isinstance(sample["mean_rgb"], list) or len(sample["mean_rgb"]) != 3
            or any(type(x) is not int or not 0 <= x <= 255 for x in sample["mean_rgb"])
            or type(sample["aspect_milli"]) is not int or not 1 <= sample["aspect_milli"] <= 100000):
        raise StickerPolicyError("表情感知指纹格式无效。")


def load_catalog(base: Path = BASE_DIR) -> dict:
    """Read optional local deny data; missing/bad data keeps the built-in blocks.

    Historical approved_sha256/metadata fields are ignored. There is no approval
    requirement or chat-supplied policy path. Invalid custom entries are skipped
    independently so they cannot disable normal sticker collection.
    """
    result = {"blocked_sha256": FIXED_DENIED_SHA256, "blocked_perceptual": []}
    base = Path(base).resolve()
    path = base / "stickers" / "catalog.json"
    try:
        if path.resolve() != path.absolute() or path.is_symlink():
            return result
        with path.open("rb") as handle:
            raw = handle.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            return result
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except (OSError, UnicodeError, ValueError):
        return result
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        return result
    hashes = data.get("blocked_sha256", [])
    if isinstance(hashes, list):
        result["blocked_sha256"] = FIXED_DENIED_SHA256 | frozenset(
            item for item in hashes[:MAX_ENTRIES] if isinstance(item, str) and _SHA.fullmatch(item))
    samples = data.get("blocked_perceptual", [])
    if isinstance(samples, list):
        for sample in samples[:MAX_ENTRIES]:
            try:
                _validate_sample(sample)
            except StickerPolicyError:
                continue
            result["blocked_perceptual"].append(sample)
    return result


def fingerprint(data: bytes) -> dict:
    """Bounded offline near-duplicate helper; never writes anything.

    Eight evenly spaced frames provide auxiliary duplicate detection. They do
    not prove the semantic contents of unsampled frames or arbitrary variants.
    """
    _check_data(data)
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                raise StickerPolicyError("图片实际格式不受支持。")
            probe.verify()
        samples = []
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
            frames = getattr(source, "n_frames", 1)
            if (min(width, height) < 1 or width * height > 20_000_000
                    or frames > 512 or width * height * frames > 150_000_000):
                raise StickerPolicyError("图片解码工作量超出近似检查上限。")
            for index in sorted({round(i * (frames - 1) / 7) for i in range(8)}):
                source.seek(index)
                rgba = source.convert("RGBA")
                image = Image.new("RGBA", rgba.size, "white")
                image.alpha_composite(rgba)
                image = image.convert("RGB")
                small = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
                pixels = list(small.tobytes())
                bits = 0
                for y in range(8):
                    for x in range(8):
                        bits = (bits << 1) | (pixels[y * 9 + x] > pixels[y * 9 + x + 1])
                sample = {"dhash": f"{bits:016x}",
                          "mean_rgb": [round(x) for x in ImageStat.Stat(image.resize((16, 16))).mean],
                          "aspect_milli": max(1, round(width * 1000 / height))}
                if sample not in samples:
                    samples.append(sample)
        return {"sha256": hashlib.sha256(data).hexdigest(), "samples": samples}
    except StickerPolicyError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise StickerPolicyError("无法解析图片进行近似检查。") from exc


def matches_blocked_fingerprint(samples: list, blocked: list) -> bool:
    """Conservative near-duplicate hint, never a semantic 'not Nailong' verdict."""
    for candidate in samples:
        _validate_sample(candidate)
        for reference in blocked:
            _validate_sample(reference)
            if (abs(candidate["aspect_milli"] - reference["aspect_milli"])
                    > reference["aspect_milli"] * 0.05):
                continue
            if max(abs(a - b) for a, b in zip(candidate["mean_rgb"], reference["mean_rgb"])) > 15:
                continue
            if (int(candidate["dhash"], 16) ^ int(reference["dhash"], 16)).bit_count() <= 4:
                return True
    return False


def require_allowed(data: bytes, name: str, base: Path = BASE_DIR) -> dict:
    """Block known originals/near-duplicates; allow other validated image bytes.

    The caller retains its normal image parser/size/path/session validation.
    A skipped near-duplicate check is not a claim that the image is not Nailong.
    """
    _check_data(data)
    _check_name(name)
    catalog = load_catalog(base)
    digest = hashlib.sha256(data).hexdigest()
    if digest in catalog["blocked_sha256"]:
        raise StickerPolicyError("图片命中已禁止的奶龙内容指纹。")
    checked = False
    if catalog["blocked_perceptual"]:
        try:
            known_variant = matches_blocked_fingerprint(
                fingerprint(data)["samples"], catalog["blocked_perceptual"])
            checked = True
        except StickerPolicyError:
            # Oversized decode work must not impose an extra collection ban.
            known_variant = False
        if known_variant:
            raise StickerPolicyError("图片近似已禁止内容；不得收藏或发送。")
    return {"sha256": digest, "allowed": True, "near_duplicate_checked": checked}


@lru_cache(maxsize=512)
def _listing_digest(path: str, size: int, mtime_ns: int, ctime_ns: int) -> str:
    # Only accelerates the advisory list; require_allowed always hashes fresh bytes.
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_IMAGE_BYTES + 1)
    _check_data(data)
    return hashlib.sha256(data).hexdigest()


def available_stickers(base: Path = BASE_DIR) -> list[str]:
    """List collected/* and hakimi_* excluding known forbidden names/SHA.

    No per-image perceptual decoding occurs here. Call require_allowed on the
    actual bytes at final use for the additional bounded near-duplicate check.
    Avatars, reference images, and Qzone artwork are intentionally not listed.
    """
    try:
        base = Path(base).resolve()
        catalog = load_catalog(base)
        directory = base / "stickers"
        collected = directory / "collected"
        if collected.resolve() != collected.absolute():
            return []
        paths = list(directory.glob("hakimi_*")) + list(collected.glob("*"))
        if len(paths) > MAX_ENTRIES:
            return []
        allowed = []
        for path in sorted(paths):
            try:
                _check_name(path.name)
                if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file():
                    continue
                stat = path.stat()
                if not 0 < stat.st_size <= MAX_IMAGE_BYTES:
                    continue
                digest = _listing_digest(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                if digest not in catalog["blocked_sha256"]:
                    allowed.append(path.relative_to(directory).as_posix())
            except (OSError, StickerPolicyError):
                continue
        return allowed
    except (OSError, StickerPolicyError):
        return []


# Compatibility names for callers being upgraded; neither requires approval.
require_approved = require_allowed
approved_stickers = available_stickers
