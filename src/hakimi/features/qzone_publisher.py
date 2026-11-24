"""
QQ 空间 (Qzone) 图文说说自动发布器 (qzone_publisher.py)

原理：
1. 调用本地 NapCat OneBot 接口 `http://127.0.0.1:3000/get_cookies` (domain="qzone.qq.com") 动态获取最新 `p_skey` 与 `uin`。
2. 根据 `p_skey` 计算 QQ 空间 `g_tk` 校验值。
3. 若指定了配图 `--image`，先将图片 Base64 上传至 `up.qzone.qq.com/cgi-bin/upload/cgi_upload_image` 获取 `pic_bo` 与 `richval`。
4. 调用 `emotion_cgi_publish_v6` 发布带图（或纯文字）说说，并记录到 `state/qzone_posts.jsonl`。
"""
from hakimi import paths

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from hakimi.features.qzone_state import BASE_DIR, QZONE_LOG, QZONE_TIMEZONE, STATE_DIR, daily_lock, daily_status  # noqa: F401 (re-export)

sys.stdout.reconfigure(encoding="utf-8")

STATE_DIR.mkdir(exist_ok=True)
HTTP_API = "http://127.0.0.1:3000"


def calc_gtk(p_skey: str) -> int:
    hash_val = 5381
    for ch in p_skey:
        hash_val += (hash_val << 5) + ord(ch)
    return hash_val & 0x7FFFFFFF


def get_qzone_auth() -> tuple[str, str, int]:
    saved=Path(os.environ.get('HAKIMI_QZONE_AUTH_FILE',str(STATE_DIR/'qzone_auth.fernet')))
    if os.name!='nt' and saved.exists():
        from hakimi.core.host_secrets import crypt
        value=json.loads(crypt(saved.read_bytes(),False))
        return value['uin'],value['cookies'],calc_gtk(value['p_skey'])
    req = urllib.request.Request(
        f"{HTTP_API}/get_cookies",
        data=json.dumps({"domain": "qzone.qq.com"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8")).get("data", {})
    cookie_str = data.get("cookies", "")
    m_pskey = re.search(r"p_skey=([^;]+)", cookie_str)
    m_uin = re.search(r"uin=o?(\d+)", cookie_str)
    if not m_pskey or not m_uin:
        raise RuntimeError('无法从QQ登录状态中取得空间发布凭据')
    p_skey = m_pskey.group(1)
    uin = m_uin.group(1)
    return uin, cookie_str, calc_gtk(p_skey)


def upload_qzone_image(image_path: Path, uin: str, cookie_str: str, g_tk: int) -> tuple[str, str]:
    """上传图片到 QQ 空间并返回 (pic_bo, richval)"""
    b64_pic = base64.b64encode(image_path.read_bytes()).decode("ascii")
    skey_match = re.search(r"(?:^|;\s*)skey=([^;]+)", cookie_str)
    p_skey_match = re.search(r"p_skey=([^;]+)", cookie_str)
    skey = skey_match.group(1) if skey_match else ""
    p_skey = p_skey_match.group(1) if p_skey_match else ""

    form_data = urllib.parse.urlencode({
        "filename": "filename",
        "uin": uin,
        "skey": skey,
        "zzpaneluin": uin,
        "zzpanelkey": "",
        "p_uin": uin,
        "p_skey": p_skey,
        "qzonetoken": "",
        "uploadtype": "1",
        "albumtype": "7",
        "exttype": "0",
        "refer": "shuoshuo",
        "output_type": "jsonhtml",
        "charset": "utf-8",
        "output_charset": "utf-8",
        "upload_hd": "1",
        "hd_width": "2048",
        "hd_height": "10000",
        "hd_quality": "96",
        "backUrls": "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,http://119.147.64.75/cgi-bin/upload/cgi_upload_image",
        "url": f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={g_tk}",
        "base64": "1",
        "picfile": b64_pic,
    }).encode("utf-8")

    url = f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={g_tk}"
    req = urllib.request.Request(
        url,
        data=form_data,
        headers={
            "Cookie": cookie_str,
            "Referer": f"https://user.qzone.qq.com/{uin}",
            "Origin": "https://user.qzone.qq.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw_html = resp.read().decode("utf-8", errors="ignore")

    m_json = re.search(r"frameElement\.callback\((\{.*?\})\)", raw_html, re.S)
    if not m_json:
        raise RuntimeError(f"上传图片返回格式无法解析: {raw_html[:300]}")
    res_obj = json.loads(m_json.group(1))
    data = res_obj.get("data", {})
    if res_obj.get("ret", data.get("ret")) != 0:
        raise RuntimeError(f"图片上传失败: {res_obj}")

    pre = data.get("pre", "")
    pic_bo = pre.split("bo=", 1)[1].split("&", 1)[0] if "bo=" in pre else ""
    if not pic_bo and "url" in data and "bo=" in data["url"]:
        pic_bo = data["url"].split("bo=", 1)[1].split("&", 1)[0]

    albumid = data.get("albumid", "")
    lloc = data.get("lloc", "")
    sloc = data.get("sloc", "")
    img_type = data.get("type", 1)
    height = data.get("height", 0)
    width = data.get("width", 0)
    richval = f",{albumid},{lloc},{sloc},{img_type},{height},{width},,{height},{width}"
    return pic_bo, richval


def publish_shuoshuo(text: str, image_path: Path | None = None, *, force=False, require_fallback=False) -> dict:
    if (STATE_DIR/'diary_mode.json').exists():
        return {'skipped':True,'reason':'replaced_by_private_diary'}
    with daily_lock():
        status = daily_status()
        if not force and status["state"] != "not_published":
            return {"skipped": True, "reason": status["state"], "day": status["day"]}
        if require_fallback:
            from hakimi.features.qzone_fallback import check
            gate = check()
            if not gate["eligible"] or gate["wait_seconds"] > 0:
                return {"skipped": True, "reason": gate["reason"], "wait_seconds": gate["wait_seconds"]}
            if image_path is None or not Path(image_path).is_file():
                raise ValueError("每日说说需要新绘制的配图")
        marker = STATE_DIR / "qzone_daily.json"
        # Shared by Gemini and Codex: an uncertain send is never automatically repeated.
        record = {"day": status["day"], "state": "started"}
        marker.write_text(json.dumps(record), encoding="utf-8")
        try:
            result = _publish_shuoshuo(text, image_path)
        except Exception:
            record["state"] = "unknown"
            marker.write_text(json.dumps(record), encoding="utf-8")
            raise
        record["state"] = "published" if result.get("ret") == 0 else "failed"
        marker.write_text(json.dumps(record), encoding="utf-8")
        return result


def _publish_shuoshuo(text: str, image_path: Path | None = None) -> dict:
    uin, cookie_str, g_tk = get_qzone_auth()
    post_fields = {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "pic_template": "",
        "richtype": "",
        "richval": "",
        "special_url": "",
        "subrichtype": "",
        "con": text,
        "feedversion": "1",
        "ver": "1",
        "ugc_right": "1",
        "to_sign": "0",
        "hostuin": uin,
        "code_version": "1",
        "format": "json",
        "qzreferrer": f"https://user.qzone.qq.com/{uin}",
    }

    if image_path and image_path.exists():
        pic_bo, richval = upload_qzone_image(image_path, uin, cookie_str, g_tk)
        post_fields["richtype"] = "1"
        post_fields["richval"] = richval
        post_fields["subrichtype"] = "1"
        if pic_bo:
            post_fields["pic_bo"] = f"{pic_bo}\t{pic_bo}"

    url = f"https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6?g_tk={g_tk}"
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(post_fields).encode("utf-8"),
        headers={
            "Cookie": cookie_str,
            "Referer": f"https://user.qzone.qq.com/{uin}",
            "Origin": "https://user.qzone.qq.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8", errors="ignore"))

    log_entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uin": uin,
        "text": text,
        "image": str(image_path) if image_path else None,
        "tid": result.get("tid"),
        "ret": result.get("ret", result.get("code")),
        "message": result.get("message", ""),
    }
    with open(QZONE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    return log_entry


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    text_input = parser.add_mutually_exclusive_group(required=True)
    text_input.add_argument("--text", help="说说正文内容")
    text_input.add_argument("--text-file", type=Path, help="UTF-8正文文件，保留真实换行")
    parser.add_argument("--image", default="", help="可选：配图本地路径")
    parser.add_argument("--require-fallback", action="store_true", help="仅dsh接管且到发布时间时发布")
    parser.add_argument("--force", action="store_true", help="本机手动确认后允许同日再次发布")
    args = parser.parse_args()

    img_p = Path(args.image) if args.image else None
    text = args.text_file.read_text(encoding="utf-8-sig") if args.text_file else args.text
    res = publish_shuoshuo(text, img_p, force=args.force, require_fallback=args.require_fallback)
    print(json.dumps(res, ensure_ascii=False, indent=2))
