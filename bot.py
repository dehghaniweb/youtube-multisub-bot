#!/usr/bin/env python3
"""
Worker: download a YouTube video + subtitles, burn / embed them, upload the result to Telegram.

Runs inside GitHub Actions (see .github/workflows/download.yml); the Telegram bot "brain"
(Google Apps Script) starts it with: ACTION, VIDEO_ID, LANGS, MODE, HEIGHT, CHAT_ID, MSG_ID, STORAGE_CHAT.

ACTION=inspect  -> list the video's subtitles (own + automatic) as buttons in Telegram
ACTION=download -> download, add the chosen subtitle, upload to Telegram
"""
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CLOUD_API = "https://api.telegram.org"
CLOUD_LIMIT = 48_000_000            # official Bot API upload limit is 50 MB
LOCAL_LIMIT = 1_950_000_000         # local Bot API server: 2000 MB
VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
RETRY_WAITS = (15, 45, 90)          # seconds between subtitle retries (YouTube answers 429 often)
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
SPEC_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*(?::(?:auto|manual))?$")   # "en", "en:auto", "en:manual"


# =====================================================================  config
@dataclass
class Cfg:
    bot_token: str = ""
    storage_chat: str = ""
    cookies_file: str = ""
    local_api: str = ""
    fallback_used: bool = False      # True when the 2 GB server could not be used
    work_dir: Path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "ytjob"

    @classmethod
    def from_env(cls) -> "Cfg":
        e = lambda k: (os.environ.get(k) or "").strip()      # noqa: E731
        cookies = e("YOUTUBE_COOKIES_FILE")
        return cls(bot_token=e("BOT_TOKEN"), storage_chat=e("STORAGE_CHAT") or e("CHAT_ID"),
                   cookies_file=cookies if cookies and Path(cookies).is_file() else "",
                   local_api=e("TG_LOCAL_API"))


# =====================================================================  telegram
class TelegramError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Telegram:
    def __init__(self, token: str, base: str = CLOUD_API):
        self.token, self.base = token, base.rstrip("/")

    @property
    def url(self) -> str:
        return f"{self.base}/bot{self.token}"

    def call(self, method: str, retries: int = 4, **params):
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(f"{self.url}/{method}", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read())
                except Exception:                                   # noqa: BLE001
                    data = {"ok": False, "description": str(e), "error_code": e.code}
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = TelegramError(f"network: {e}")
                time.sleep(2 * (attempt + 1))
                continue
            if data.get("ok"):
                return data["result"]
            retry_after = (data.get("parameters") or {}).get("retry_after")
            last = TelegramError(data.get("description", "error"), data.get("error_code"))
            if retry_after and attempt < retries - 1:
                time.sleep(min(int(retry_after) + 1, 60))
                continue
            raise last
        raise last or TelegramError("unknown error")

    def send_message(self, chat_id, text):
        return self.call("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML",
                         link_preview_options={"is_disabled": True})

    def edit_message(self, chat_id, message_id, text, reply_markup=None):
        try:
            return self.call("editMessageText", retries=1, chat_id=chat_id, message_id=message_id,
                             text=text[:4000], parse_mode="HTML", reply_markup=reply_markup,
                             link_preview_options={"is_disabled": True})
        except TelegramError as e:
            if "not modified" in str(e).lower():
                return None
            raise

    def upload(self, method: str, field: str, path: str, fields: dict, thumb: str = None,
               timeout: int = 7200):
        """Multipart upload through curl, so a 2 GB file is streamed instead of loaded into RAM."""
        cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", "POST", f"{self.url}/{method}",
               "-F", f"{field}=@{path}"]
        if thumb:
            cmd += ["-F", f"thumbnail=@{thumb}"]
        for k, v in fields.items():
            if v is not None:
                cmd += ["--form-string", f"{k}={v}"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise TelegramError(f"curl failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise TelegramError(f"bad response: {proc.stdout[:300]}")
        if not data.get("ok"):
            raise TelegramError(data.get("description", "upload failed"), data.get("error_code"))
        return data["result"]

    def reachable(self) -> bool:
        try:
            self.call("getMe", retries=1)
            return True
        except Exception:                                           # noqa: BLE001
            return False


# =====================================================================  subtitles
LANG_NAMES = {
    "fa": "فارسی", "en": "English", "ar": "العربية", "tr": "Türkçe", "de": "Deutsch", "nl": "Nederlands",
    "fr": "Français", "es": "Español", "ru": "Русский", "it": "Italiano", "pt": "Português",
    "ja": "日本語", "ko": "한국어", "zh": "中文", "hi": "हिन्दी", "ur": "اردو", "uk": "Українська", "pl": "Polski",
}
ISO3 = {
    "fa": "fas", "en": "eng", "ar": "ara", "tr": "tur", "de": "deu", "nl": "nld", "fr": "fra",
    "es": "spa", "ru": "rus", "it": "ita", "pt": "por", "ja": "jpn", "ko": "kor", "zh": "zho",
    "hi": "hin", "ur": "urd", "uk": "ukr", "pl": "pol", "he": "heb", "id": "ind", "sv": "swe",
}
RTL = {"fa", "ar", "ur", "he", "ps", "ku", "sd", "ug"}
SKIP_KEYS = {"live_chat"}
SOURCE_LABELS = {"manual": "زیرنویس اصلی", "auto-orig": "خودکار", "auto": "خودکار",
                 "auto-translated": "ترجمهٔ خودکار"}


def base_lang(code: str) -> str:
    return code.split("-")[0].lower()


def pick_track(info: dict, lang: str, only: Optional[str] = None) -> Optional[tuple]:
    """Best subtitle track for `lang` -> (key, source) or None.

    Order: uploader's own subtitles > auto-generated in the video's own language ('xx-orig')
    > YouTube's machine translation into `lang`.   only="auto" / "manual" restricts the search.
    """
    manual = set() if only == "auto" else {k for k, v in (info.get("subtitles") or {}).items() if v and k not in SKIP_KEYS}
    auto = set() if only == "manual" else {k for k, v in (info.get("automatic_captions") or {}).items() if v}
    if lang in manual:
        return lang, "manual"
    cands = sorted(k for k in manual if base_lang(k) == base_lang(lang) and "-" in k)
    if "-" not in lang and cands:
        return cands[0], "manual"
    if f"{lang}-orig" in auto:
        return f"{lang}-orig", "auto-orig"
    if lang in auto:
        has_orig = any(k.endswith("-orig") for k in auto)
        return lang, ("auto-translated" if has_orig or manual else "auto")
    cands = sorted(k for k in auto if base_lang(k) == base_lang(lang) and not k.endswith("-orig"))
    if "-" not in lang and cands:
        return cands[0], "auto-translated"
    return None


def pick_for(info: dict, spec: str) -> Optional[tuple]:
    """spec is 'en' (best available), 'en:auto' or 'en:manual'."""
    lang, _, pref = spec.partition(":")
    return pick_track(info, lang, only=pref if pref in ("auto", "manual") else None)


_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})"
_TIMING = re.compile(rf"^{_TS}\s*-->\s*{_TS}")
_TAG = re.compile(r"<[^>]*>")


def _secs(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(text: str) -> list:
    """-> [(start, end, [lines])] with inline tags stripped."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        idx = next((i for i, l in enumerate(lines) if _TIMING.match(l.strip())), None)
        if idx is None:
            continue
        m = _TIMING.match(lines[idx].strip())
        start, end = _secs(*m.groups()[0:4]), _secs(*m.groups()[4:8])
        body = []
        for l in lines[idx + 1:]:
            l = html.unescape(_TAG.sub("", l)).replace("\u00a0", " ").strip()
            if l:
                body.append(l)
        cues.append((start, end, body))
    return cues


def looks_rolling(cues: list) -> bool:
    """YouTube auto-captions ('karaoke' style) repeat the previous line inside every cue."""
    if len(cues) < 4:
        return False
    tiny = sum(1 for s, e, _ in cues if e - s < 0.05)
    repeats = sum(1 for a, b in zip(cues, cues[1:]) if len(b[2]) >= 2 and a[2] and b[2][0] == a[2][-1])
    return (tiny + repeats) / len(cues) >= 0.25


def clean_rolling(cues: list) -> list:
    out, prev = [], []
    for s, e, lines in cues:
        if e - s < 0.05:                       # 10 ms transition cues
            prev = lines or prev
            continue
        new = [l for l in lines if l not in prev]
        prev = lines
        if new:
            out.append((s, e, new))
    return out


def tidy_timings(cues: list, min_dur: float = 1.0) -> list:
    out = []
    for i, (s, e, lines) in enumerate(cues):
        end = max(e, s + min_dur)
        if i + 1 < len(cues):
            nxt = cues[i + 1][0]
            if end > nxt:
                end = nxt if nxt - s >= 0.2 else max(e, s + 0.2)
        out.append((s, end, lines))
    return out


def _fmt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list, rtl: bool = False) -> str:
    """rtl=True wraps every line in RLE..PDF so burned-in Persian/Arabic gets its punctuation
    on the correct side. Use only for burned-in subtitles (players handle bidi themselves)."""
    parts = []
    for i, (s, e, lines) in enumerate(cues, 1):
        body = "\n".join(f"\u202b{l}\u202c" if rtl else l for l in lines)
        parts.append(f"{i}\n{_fmt(s)} --> {_fmt(e)}\n{body}\n")
    return "\n".join(parts)


def vtt_to_srt(vtt_text: str, rtl: bool = False) -> tuple:
    cues = [c for c in parse_vtt(vtt_text) if c[2] or c[1] - c[0] < 0.05]
    cues = clean_rolling(cues) if looks_rolling(cues) else [c for c in cues if c[2]]
    return to_srt(tidy_timings(cues), rtl), len(cues)


# =====================================================================  ffmpeg
class FFmpegError(RuntimeError):
    pass


def probe(path) -> dict:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True)
    if p.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {p.stderr[-300:]}")
    data = json.loads(p.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    return {"duration": dur, "width": v.get("width") or 0, "height": v.get("height") or 0,
            "vcodec": v.get("codec_name", ""), "acodec": a.get("codec_name", "")}


def _style(lang: str, size: int) -> str:
    font = "Noto Naskh Arabic" if base_lang(lang) in RTL else "Noto Sans"
    return (f"FontName={font},FontSize={size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            f"BorderStyle=1,Outline=1.6,Shadow=0.4,MarginV=24,Alignment=2")


def render(video: Path, out: Path, hard: Optional[tuple] = None, soft: Optional[list] = None,
           duration: float = 0.0, on_progress: Optional[Callable[[float], None]] = None,
           font_size: int = 18) -> None:
    """hard = (srt, lang) burned into the picture; soft = [(srt, lang), ...] embedded as tracks."""
    soft = soft or []
    video, out = Path(video), Path(out)
    work = video.parent
    info = probe(video)
    reencode = bool(hard) or info["vcodec"] != "h264"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
           "-i", video.name]
    for srt, _ in soft:
        cmd += ["-i", Path(srt).name]
    cmd += ["-map", "0:v:0", "-map", "0:a?"]
    for i in range(len(soft)):
        cmd += ["-map", f"{i + 1}:0"]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
        if hard:
            srt, lang = hard
            cmd += ["-vf", f"subtitles=filename={Path(srt).name}:force_style='{_style(lang, font_size)}'"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "copy"] if info["acodec"] == "aac" else ["-c:a", "aac", "-b:a", "128k"]
    if soft:
        cmd += ["-c:s", "mov_text"]
        for i, (_, lang) in enumerate(soft):
            cmd += [f"-metadata:s:s:{i}", f"language={ISO3.get(base_lang(lang), 'und')}",
                    f"-metadata:s:s:{i}", f"title={LANG_NAMES.get(base_lang(lang), lang)}",
                    f"-disposition:s:{i}", "default" if (i == 0 and not hard) else "0"]
    cmd += ["-movflags", "+faststart", out.name if out.parent == work else str(out)]

    with subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m and on_progress and duration > 0:
                on_progress(min(1.0, int(m.group(1)) / 1e6 / duration))
        err = proc.stderr.read()
        code = proc.wait()
    if code != 0:
        raise FFmpegError(f"ffmpeg failed: {err[-600:]}")


def thumbnail(video: Path, out: Path, duration: float = 0) -> Optional[Path]:
    t = min(max(duration * 0.1, 1), 15) if duration else 1
    p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", str(out)], capture_output=True)
    return out if p.returncode == 0 and Path(out).exists() and Path(out).stat().st_size > 0 else None


def split_by_size(video: Path, max_bytes: int, duration: float) -> list:
    """Cut into parts below max_bytes (stream copy, no quality loss)."""
    video = Path(video)
    size = video.stat().st_size
    if size <= max_bytes:
        return [video]
    duration = duration or probe(video)["duration"]
    factor = 0.85
    for _ in range(4):
        seg = max(5, math.floor(duration * (max_bytes * factor) / size))
        for old in video.parent.glob(f"{video.stem}.part*.mp4"):
            old.unlink()
        pattern = str(video.parent / f"{video.stem}.part%02d.mp4")
        p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-map", "0", "-c", "copy",
                            "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1",
                            "-segment_format_options", "movflags=+faststart", pattern],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise FFmpegError(f"split failed: {p.stderr[-400:]}")
        parts = sorted(video.parent.glob(f"{video.stem}.part*.mp4"))
        if parts and all(x.stat().st_size <= max_bytes for x in parts):
            return parts
        factor *= 0.7
    raise FFmpegError("could not split below the upload limit")


# =====================================================================  yt-dlp
def ydl_opts(cfg: Cfg, **extra) -> dict:
    o = {"quiet": True, "no_warnings": False, "noprogress": True,
         "remote_components": ["ejs:github"],
         "retries": 10, "fragment_retries": 10, "extractor_retries": 3, "socket_timeout": 30}
    if cfg.cookies_file:
        o["cookiefile"] = cfg.cookies_file
    # Do NOT force player_client: some clients (android_vr, web_embedded) return no caption tracks.
    clients = [c.strip() for c in os.environ.get("YT_PLAYER_CLIENTS", "").split(",") if c.strip()]
    if clients:
        o["extractor_args"] = {"youtube": {"player_client": clients}}
    o.update(extra)
    return o


def extract_info(cfg: Cfg, url: str) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL(ydl_opts(cfg, skip_download=True, ignore_no_formats_error=True)) as ydl:
        return ydl.extract_info(url, download=False)


def download_video(cfg: Cfg, url: str, work: Path, height: int, sub_keys: list, hook=None) -> None:
    import yt_dlp
    fmt = (f"bv*[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<={height}]+ba/"
           f"b[height<={height}]/bv*+ba/b")
    opts = ydl_opts(cfg, format=fmt, merge_output_format="mp4", outtmpl=str(work / "%(id)s.%(ext)s"),
                    writesubtitles=bool(sub_keys), writeautomaticsub=bool(sub_keys),
                    subtitleslangs=sub_keys, subtitlesformat="vtt", sleep_interval_subtitles=2,
                    progress_hooks=[hook] if hook else [])
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def download_subs_only(cfg: Cfg, url: str, work: Path, keys: list) -> None:
    import yt_dlp
    opts = ydl_opts(cfg, skip_download=True, ignore_no_formats_error=True,
                    outtmpl=str(work / "%(id)s.%(ext)s"), writesubtitles=True, writeautomaticsub=True,
                    subtitleslangs=keys, subtitlesformat="vtt", sleep_interval_subtitles=3)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def find_video(work: Path, vid: str) -> Optional[Path]:
    c = [p for p in work.glob(f"{vid}.*") if p.suffix.lower() in VIDEO_EXTS]
    return max(c, key=lambda p: p.stat().st_size) if c else None


def collect_subs(work: Path, vid: str, keys: list) -> dict:
    out = {}
    for k in keys:
        p = work / f"{vid}.{k}.vtt"
        if p.is_file() and p.stat().st_size > 0:
            out[k] = p
    return out


# =====================================================================  upload
def make_uploader(cfg: Cfg):
    """-> (Telegram client, size limit, using_local_server)."""
    if cfg.local_api:
        local = Telegram(cfg.bot_token, base=cfg.local_api)
        if local.reachable():
            return local, LOCAL_LIMIT, True
        print("local Bot API server not reachable -> using the normal API (50 MB limit, files get split)")
        cfg.fallback_used = True
    return Telegram(cfg.bot_token), CLOUD_LIMIT, False


def message_link(msg: dict) -> Optional[str]:
    chat, mid = msg.get("chat") or {}, msg.get("message_id")
    if not mid:
        return None
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{mid}"
    cid = str(chat.get("id", ""))
    return f"https://t.me/c/{cid[4:]}/{mid}" if cid.startswith("-100") else None


def send_one(up: Telegram, chat, path: Path, caption: str, meta: dict, thumb: Optional[Path]) -> dict:
    fields = {"chat_id": chat, "caption": caption[:1000], "parse_mode": "HTML"}
    try:
        return up.upload("sendVideo", "video", str(path), {
            **fields, "supports_streaming": "true", "duration": int(meta.get("duration") or 0),
            "width": meta.get("width") or None, "height": meta.get("height") or None},
            thumb=str(thumb) if thumb else None)
    except TelegramError as e:
        if e.code in (400, 415):                  # not accepted as a video -> send as a file
            print(f"sendVideo rejected ({e}); retrying as document")
            return up.upload("sendDocument", "document", str(path), fields, thumb=str(thumb) if thumb else None)
        raise


def _deliver_with(up, limit, chat, video, caption, thumb, duration, progress):
    meta = probe(video)
    meta["duration"] = meta["duration"] or duration
    parts = split_by_size(video, limit, meta["duration"]) if video.stat().st_size > limit else [video]
    sent = []
    for i, part in enumerate(parts, 1):
        cap = caption if len(parts) == 1 else f"{caption}\n\n🧩 بخش {i} از {len(parts)}"
        if progress:
            progress(f"⬆️ آپلود در تلگرام ({i}/{len(parts)})…")
        sent.append(send_one(up, chat, part, cap, probe(part) if len(parts) > 1 else meta, thumb))
    return sent


def deliver(cfg: Cfg, video: Path, caption: str, thumb: Optional[Path], duration: float, progress=None) -> list:
    up, limit, local = make_uploader(cfg)
    try:
        return _deliver_with(up, limit, cfg.storage_chat, video, caption, thumb, duration, progress)
    except TelegramError as e:
        if not local:
            raise
        print(f"upload through the local server failed ({e}); falling back to the normal API")
        cfg.fallback_used = True
        return _deliver_with(Telegram(cfg.bot_token), CLOUD_LIMIT, cfg.storage_chat, video, caption,
                             thumb, duration, progress)


def build_caption(title, channel, url, langs_desc, size_mb, height, duration) -> str:
    mins, secs = divmod(int(duration), 60)
    hrs, mins = divmod(mins, 60)
    dur = f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d}"
    lines = [f"🎬 <b>{html.escape(title[:200])}</b>"]
    if channel:
        lines.append(f"📺 {html.escape(channel)}")
    if langs_desc:
        lines.append(f"💬 {html.escape(langs_desc)}")
    lines.append(f"📦 {height}p · {size_mb:.0f} MB · ⏱ {dur}")
    lines.append(f"🔗 {url}")
    return "\n".join(lines)


# =====================================================================  the job
def friendly_error(ex: Exception, has_cookies: bool = False) -> str:
    t = str(ex)
    low = t.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        why = ("🔑 کوکی ثبت‌شده را یوتیوب قبول نکرد. یا منقضی شده یا هنگام چسباندن ناقص شده است."
               if has_cookies else "🔑 هنوز کوکی یوتیوب ثبت نشده است.")
        return (f"یوتیوب درخواست را ربات تشخیص داد.\n\n{why}\n\n"
                "یک کوکی تازه با روش پنجرهٔ خصوصی بگیر و این Secret را در گیت‌هاب بساز یا جایگزین کن:\nYOUTUBE_COOKIES")
    if "private video" in low:
        return "این ویدئو خصوصی است."
    if "members-only" in low or "members only" in low or "join this channel" in low:
        return "این ویدئو مخصوص اعضای کانال است."
    if "video unavailable" in low or "this video is not available" in low:
        return "ویدئو در دسترس نیست (حذف شده یا محدودیت منطقه‌ای)."
    if "live event" in low or "is live" in low:
        return "پخش زندهٔ در جریان پشتیبانی نمی‌شود؛ بعد از پایان دوباره امتحان کن."
    if "429" in low or "too many requests" in low:
        return "یوتیوب موقتاً درخواست‌ها را محدود کرده (429). چند دقیقه بعد دوباره امتحان کن."
    if "requested format is not available" in low:
        return "فرمت ویدئو در دسترس نیست؛ احتمالاً کوکی یا PO-Token مشکل دارد."
    return re.sub(r"\x1b\[[0-9;]*m", "", t)[:350]


def slug(title: str, vid: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60]
    return f"{s or 'video'}-{vid}"


def bar(frac: float, width: int = 10) -> str:
    n = max(0, min(width, int(frac * width)))
    return "▰" * n + "▱" * (width - n) + f" {int(frac * 100)}%"


class Progress:
    """Edits one Telegram message, throttled so we never hit rate limits."""

    def __init__(self, tg: Telegram, chat, msg_id, header: str = ""):
        self.tg, self.chat, self.msg_id, self.header = tg, chat, msg_id, header
        self._last, self._last_text = 0.0, ""

    def text(self, body: str, force: bool = True):
        txt = f"{self.header}\n\n{body}" if self.header else body
        now = time.time()
        if txt == self._last_text or (not force and now - self._last < 12):
            return
        self._last, self._last_text = now, txt
        try:
            self.tg.edit_message(self.chat, self.msg_id, txt)
        except Exception:                                           # noqa: BLE001
            pass

    def frac(self, label: str, f: float):
        self.text(f"{label}\n{bar(f)}", force=False)


def run_job(cfg: Cfg, tg: Telegram, job: dict) -> None:
    chat, mid, vid = job["chat_id"], job["msg_id"], job["vid"]
    langs, mode, height = job.get("langs") or [], job.get("mode", "n"), int(job.get("height") or 720)
    url = f"https://www.youtube.com/watch?v={vid}"
    work = cfg.work_dir / vid
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    prog = Progress(tg, chat, mid)

    prog.text("🔎 دریافت اطلاعات ویدئو…")
    info = extract_info(cfg, url)
    if info.get("is_live"):
        raise RuntimeError("live event")
    title = info.get("title") or vid
    channel = info.get("channel") or info.get("uploader") or ""
    prog.header = f"🎬 <b>{html.escape(title[:120])}</b>"

    # which subtitle track for every requested language
    picks, notes = [], []
    for spec in langs:
        code = spec.partition(":")[0]
        p = pick_for(info, spec)
        if p:
            picks.append((code, p[0], p[1]))
        else:
            notes.append(f"⚠️ برای این ویدئو زیرنویس «{code}» وجود ندارد.")
    if not picks:
        mode = "n"
    keys = [k for _l, k, _s in picks]

    def hook(d):
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                prog.frac("⬇️ دانلود از یوتیوب", d.get("downloaded_bytes", 0) / tot)

    prog.text("⬇️ دانلود از یوتیوب…")
    download_video(cfg, url, work, height, keys, hook)
    video = find_video(work, vid)
    if not video:
        raise RuntimeError("Video file was not produced")

    # subtitles that failed (HTTP 429 ...) are retried several times
    have = collect_subs(work, vid, keys)
    for wait in RETRY_WAITS:
        missing = [k for k in keys if k not in have]
        if not missing:
            break
        prog.text(f"💬 تلاش مجدد برای زیرنویس {', '.join(missing)}…")
        time.sleep(wait)
        try:
            download_subs_only(cfg, url, work, missing)
        except Exception as ex:                                     # noqa: BLE001
            print(f"subtitle retry failed: {ex}")
        have = collect_subs(work, vid, keys)
    for lang, key, _src in picks:
        if key not in have:
            notes.append(f"⚠️ زیرنویس «{lang}» از یوتیوب دریافت نشد.")
    picks = [p for p in picks if p[1] in have]
    if not picks:
        mode = "n"

    # convert to SRT: first language is burned in when mode == hard, the rest become tracks
    hard, soft, desc = None, [], []
    for i, (lang, key, src) in enumerate(picks):
        as_hard = (mode == "h" and i == 0)
        raw = have[key].read_text(encoding="utf-8", errors="replace")
        srt, n = vtt_to_srt(raw, rtl=as_hard and base_lang(lang) in RTL)
        if n == 0:
            notes.append(f"⚠️ زیرنویس «{lang}» خالی بود.")
            continue
        path = work / (f"hard_{i}.srt" if as_hard else f"sub_{i}.srt")
        path.write_text(srt, encoding="utf-8")
        name = LANG_NAMES.get(base_lang(lang), lang)
        how = "روی تصویر" if as_hard else "داخل فایل"
        desc.append(f"{name} ({how}، {SOURCE_LABELS.get(src, src)})")
        if as_hard:
            hard = (path, lang)
        else:
            soft.append((path, lang))

    meta = probe(video)
    duration = meta["duration"] or float(info.get("duration") or 0)
    final = work / f"{slug(title, vid)}.mp4"
    need_render = bool(hard or soft) or video.suffix.lower() != ".mp4" or meta["vcodec"] != "h264" \
        or meta["acodec"] not in ("aac", "")
    if need_render:
        label = "🎬 ساخت ویدیو با زیرنویس روی تصویر" if hard else "🎬 ساخت فایل نهایی"
        prog.text(label)
        render(video, final, hard=hard, soft=soft, duration=duration, on_progress=lambda f: prog.frac(label, f))
    else:
        video.rename(final)

    size_mb = final.stat().st_size / 1_000_000
    prog.text(f"⬆️ آپلود در تلگرام ({size_mb:.0f} MB)…")
    thumb = thumbnail(final, work / "thumb.jpg", duration)
    caption = build_caption(title, channel, url, " + ".join(desc), size_mb, meta["height"] or height, duration)
    sent = deliver(cfg, final, caption, thumb, duration, progress=prog.text)

    if cfg.fallback_used:
        notes.append("⚠️ سرور ۲ گیگابایتی در دسترس نبود؛ فایل با محدودیت ۵۰ مگابایت ارسال شد (ممکن است تکه‌تکه باشد).")
    links = [l for l in (message_link(m) for m in sent) if l]
    done = f"✅ <b>آپلود شد</b>\n\n🎬 <b>عنوان</b>\n{html.escape(title[:150])}"
    if desc:
        done += "\n\n💬 <b>زیرنویس</b>\n" + "\n".join(html.escape(d) for d in desc)
    if links:
        done += "\n\n🔗 <b>لینک پیام</b>\n" + ("\n".join(links) if len(links) == 1 else " ".join(f'<a href="{l}">{i}</a>' for i, l in enumerate(links, 1)))
    else:
        done += "\n\n📥 فایل در همین چت ارسال شد."
    if notes:
        done += "\n\n" + "\n\n".join(notes)
    prog.header = ""
    prog.text("✅ <b>آپلود شد</b>")
    try:
        tg.send_message(chat, done)          # a NEW message, so the phone gives a notification
    except Exception as ex:                  # noqa: BLE001
        print(f"could not send the final message: {ex}")
    shutil.rmtree(work, ignore_errors=True)


# =====================================================================  inspect: list subtitles as buttons
def track_label(code: str) -> str:
    return LANG_NAMES.get(base_lang(code), code)


def inspect_job(cfg: Cfg, tg: Telegram, job: dict) -> None:
    chat, mid, vid = job["chat_id"], job["msg_id"], job["vid"]
    info = extract_info(cfg, f"https://www.youtube.com/watch?v={vid}")
    if info.get("is_live"):
        raise RuntimeError("live event")
    title = info.get("title") or vid
    subs = info.get("subtitles") or {}
    caps = info.get("automatic_captions") or {}

    own = [k for k in sorted(subs) if subs[k] and k not in SKIP_KEYS][:8]
    autos = [(k[:-5], "orig") for k in sorted(caps) if k.endswith("-orig") and caps[k]]
    orig_codes = {c for c, _ in autos}
    for lang in job.get("langs") or []:                       # YouTube can machine-translate into these
        if lang not in orig_codes and pick_track(info, lang, only="auto"):
            autos.append((lang, "tr"))
    autos = autos[:6]

    rows = [[{"text": f"📝 {track_label(k)} · خود ویدیو", "callback_data": f"tr|{vid}|{k}|m"}] for k in own]
    for code, kind in autos:
        tail = "خودکار" if kind == "orig" else "ترجمهٔ خودکار"
        rows.append([{"text": f"🤖 {track_label(code)} · {tail}", "callback_data": f"tr|{vid}|{code}|a"}])
    rows.append([{"text": "🎞 فقط ویدیو (بدون زیرنویس)", "callback_data": f"dl|{vid}|-|n"}])

    def lines(items) -> str:
        return "\n".join(items) if items else "ندارد"

    head = f"🎬 <b>عنوان</b>\n{html.escape(title[:150])}\n\n"
    if own or autos:
        text = (head
                + "📝 <b>زیرنویس‌های خود ویدیو</b>\n" + lines([f"• {track_label(k)}" for k in own]) + "\n\n"
                + "🤖 <b>زیرنویس‌های خودکار یوتیوب</b>\n"
                + lines([f"• {track_label(c)} ({'خودکار' if kind == 'orig' else 'ترجمهٔ خودکار'})" for c, kind in autos]) + "\n\n"
                + "👇 <b>لطفاً یکی را انتخاب کنید</b>\n\n"
                + "📝 یعنی زیرنویسی که سازندهٔ ویدیو گذاشته است\n\n"
                + "🤖 یعنی زیرنویسی که خود یوتیوب ساخته است")
    else:
        text = head + "⚠️ این ویدیو هیچ زیرنویسی ندارد.\n\nمی‌توانی آن را بدون زیرنویس دانلود کنی."
    tg.edit_message(chat, mid, text, reply_markup={"inline_keyboard": rows})


def main() -> int:
    cfg = Cfg.from_env()
    e = lambda k: (os.environ.get(k) or "").strip()                 # noqa: E731
    vid = e("VIDEO_ID")
    if not cfg.bot_token or not re.fullmatch(r"[\w-]{11}", vid) or not e("CHAT_ID") or not e("MSG_ID").isdigit():
        print("BOT_TOKEN / VIDEO_ID / CHAT_ID / MSG_ID are missing or invalid")
        return 1
    action = e("ACTION") if e("ACTION") in ("inspect", "download") else "download"
    langs = [x for x in re.split(r"[+,\s]+", e("LANGS")) if x and SPEC_RE.match(x)]
    mode = e("MODE") if e("MODE") in ("h", "s", "n") else "n"
    height = int(e("HEIGHT")) if e("HEIGHT").isdigit() else 720
    job = {"chat_id": e("CHAT_ID"), "msg_id": int(e("MSG_ID")), "vid": vid, "langs": langs,
           "mode": mode if langs else "n", "height": min(max(height, 144), 2160)}
    tg = Telegram(cfg.bot_token)
    try:
        (inspect_job if action == "inspect" else run_job)(cfg, tg, job)
        return 0
    except Exception as ex:                                         # noqa: BLE001
        print(f"JOB FAILED: {ex!r}")
        try:
            tg.edit_message(job["chat_id"], job["msg_id"], f"❌ <b>خطا</b>\n\n{html.escape(friendly_error(ex, bool(cfg.cookies_file)))}")
        except Exception:                                           # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
