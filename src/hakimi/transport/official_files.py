"""QQ media upload, bounded small uploads and sequential large-file parts."""
import asyncio
import base64
import hashlib
import time
from urllib.parse import urlsplit
import urllib.request


def hashes(path):
    md5, sha1, head = hashlib.md5(), hashlib.sha1(), hashlib.md5()
    remaining = 10002432
    with path.open("rb") as stream:
        while chunk := stream.read(1024*1024):
            md5.update(chunk); sha1.update(chunk)
            head.update(chunk[:remaining]); remaining = max(0, remaining-len(chunk))
    return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "md5_10m": head.hexdigest()}


def put_part(url, path, offset, length):
    from hakimi.transport.official_base import OfficialError, NoRedirect
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise OfficialError("INVALID_UPLOAD_PART_URL")
    digest = hashlib.md5()
    def content():
        with path.open("rb") as stream:
            stream.seek(offset)
            remaining = length
            while remaining:
                data = stream.read(min(65536, remaining))
                if not data:raise OfficialError("FILE_CHANGED_DURING_UPLOAD")
                digest.update(data)
                remaining -= len(data)
                yield data
    request = urllib.request.Request(url, data=content(), method="PUT", headers={"Content-Length": str(length)})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
            if response.status not in {200, 201, 204}:raise OfficialError("UPLOAD_PART_REJECTED")
    except OSError as exc:
        raise OfficialError("UPLOAD_PART_FAILED") from None
    return digest.hexdigest()


async def upload(api, group, path, kind, *, private=False):
    from hakimi.transport.official_base import OfficialError
    stat = path.stat()
    fingerprint = ("users" if private else "groups",group,str(path),stat.st_mtime_ns,stat.st_size,kind)
    cache = getattr(api, "_file_cache", {})
    if fingerprint in cache and cache[fingerprint][0] > time.time():return cache[fingerprint][1]
    prefix = f"/v2/{'users' if private else 'groups'}/{group}"
    if stat.st_size < 5*1024*1024:
        data = await asyncio.to_thread(path.read_bytes)
        result = await api.call("POST", prefix+"/files", {"file_type": kind, "file_name": path.name,
            "srv_send_msg": False, "file_data": base64.b64encode(data).decode("ascii")})
    else:
        digest = await asyncio.to_thread(hashes, path)
        prepared = await api.call("POST", prefix+"/upload_prepare", {
            "file_type": kind, "file_size": stat.st_size, "file_name": path.name, **digest})
        upload_id, block_size = prepared.get("upload_id"), prepared.get("block_size")
        parts = prepared.get("parts", [])
        try:block_size = int(block_size)
        except (TypeError, ValueError):raise OfficialError("INVALID_UPLOAD_PREPARE") from None
        if (not upload_id or not 0 < block_size
                or not isinstance(parts, list) or not parts):raise OfficialError("INVALID_UPLOAD_PREPARE")
        try:
            parts = [{**part, "index":int(part.get("index", part.get("part_index", 0))),
                      "block_size":int(part.get("block_size") or block_size)} for part in parts]
        except (TypeError, ValueError, AttributeError):raise OfficialError("INVALID_UPLOAD_PARTS") from None
        expected = list(range(1, (stat.st_size+block_size-1)//block_size+1))
        if sorted(part.get("index", part.get("part_index", 0)) for part in parts) != expected:
            raise OfficialError("INVALID_UPLOAD_PARTS")
        covered = 0
        for part in sorted(parts, key=lambda row:row.get("index", row.get("part_index"))):
            index = part.get("index", part.get("part_index"))
            length = min(part.get("block_size") or block_size, stat.st_size-(index-1)*block_size)
            if type(length) is not int or length <= 0:raise OfficialError("INVALID_UPLOAD_PARTS")
            covered += length
            url = part.get("presigned_url", part.get("upload_url", ""))
            md5 = await asyncio.to_thread(put_part, url, path, (index-1)*block_size, length)
            for attempt in range(5):
                try:
                    await api.call("POST", prefix+"/upload_part_finish", {
                        "upload_id": upload_id, "part_index": index, "block_size": length, "md5": md5})
                    break
                except OfficialError as exc:
                    if not exc.code.endswith("40093001") or attempt == 4:raise
                    await asyncio.sleep(min(4, 2**attempt))
        if covered != stat.st_size:raise OfficialError("INVALID_UPLOAD_PARTS")
        result = await api.call("POST", prefix+"/files", {"upload_id": upload_id, "srv_send_msg": False})
    if path.stat().st_mtime_ns != stat.st_mtime_ns or path.stat().st_size != stat.st_size:
        raise OfficialError("FILE_CHANGED_DURING_UPLOAD")
    info = result.get("file_info")
    if not isinstance(info, str) or not info:raise OfficialError("INVALID_FILE_INFO")
    ttl = float(result.get("ttl") or 0)
    cache[fingerprint] = (time.time()+max(0, min(ttl if ttl > 0 else 300, 86400)-15), info)
    api._file_cache = dict(list(cache.items())[-32:])
    return info
