"""Prepare a small batch of current-session, daemon-authorized image pixels."""
import base64
import io
from pathlib import Path

from hakimi.chat.image_permissions import allowed_images_for_session
from hakimi.core.context_window import select_context

MAX_IMAGES = 2
MAX_IMAGE_BYTES = 512 * 1024


def prepare_images(payload, base, *, now=None, requested=None):
    """Prefer authorized inbound images, then a specifically recalled sent sticker."""
    from hakimi.core.media_codes import code_map, resolve_image
    session = payload.get("session_key", "")
    paths = allowed_images_for_session(session, Path(base), now)[-8:]
    codes = code_map(paths)
    selected = {}
    if requested is not None:
        # Model-facing handles are the same stable codes as in the chat rows;
        # a code or path counts only if it names an authorized image.
        wanted = {name for name in requested if name in codes}
        wanted |= {resolve_image(name, paths) for name in requested if name not in codes} - {None}
        selected = {name: (codes[name], "") for name in paths if name in wanted}
    elif paths:
        messages, _ = select_context(payload)
        for row in messages:
            if row.get("session_key") != session:
                continue
            text = str(row.get("text", ""))[:32000].replace("\\", "/")
            listed = row.get("image_paths", [])
            for name in paths:
                if name.replace("\\", "/") in text or (isinstance(listed, list) and name in listed):
                    selected.pop(name, None)
                    selected[name] = (codes[name], str(row.get("message_id", ""))[:100])
    result = []
    for name, (alias, message_id) in list(selected.items())[-MAX_IMAGES:]:
        image = _encode_image(Path(name), alias, message_id)
        if image:
            result.append(image)
    if requested is None and len(result) < MAX_IMAGES:
        recalled = prepare_recalled_sticker(payload, base, now=now)
        if recalled:
            result.append(recalled)
    return result


def prepare_recalled_sticker(payload, base, *, now=None):
    """Only the receipt resolver can authorize a previously sent sticker."""
    if payload.get('internal_review') or payload.get('cold_topic_request'):
        return None
    from hakimi.chat.sent_media_context import recalled_sticker
    matched = recalled_sticker(payload, base, now=now)
    if matched:
        message_id, path = matched
        return _encode_image(path, 'R1', str(message_id))
    return None


def _encode_image(path, alias, message_id):
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        if path.stat().st_size > 12 * 1024 * 1024:
            return None
        with Image.open(path) as source:
            if source.width * source.height > 24_000_000:
                return None
            image = ImageOps.exif_transpose(source)
            # A still frame is sufficient for the conversational image brief.
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            if image.width * image.height > 1_000_000:
                scale = (1_000_000 / (image.width * image.height)) ** .5
                image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            for quality in (90, 78, 65):
                encoded = io.BytesIO()
                rgb.save(encoded, "JPEG", quality=quality)
                data = encoded.getvalue()
                if len(data) <= MAX_IMAGE_BYTES:
                    break
            if len(data) > MAX_IMAGE_BYTES:
                return None
            return {"id": alias, "message_id": message_id, "media_type": "image/jpeg",
                    "data": base64.b64encode(data).decode("ascii"),
                    "width": rgb.width, "height": rgb.height}
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
