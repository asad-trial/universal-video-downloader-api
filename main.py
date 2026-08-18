import asyncio
import ipaddress
import os
import socket
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator
from starlette.background import BackgroundTask


APP_NAME = "Universal Video Downloader API"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "https://viralzonei.blogspot.com",
    ).split(",")
    if origin.strip()
]

MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "1024"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
MAX_FORMATS = int(os.getenv("MAX_FORMATS", "40"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "1"))

# Built in the Docker image from bgutil-ytdlp-pot-provider 1.3.1.
BGUTIL_SCRIPT_PATH = os.getenv(
    "BGUTIL_SCRIPT_PATH",
    "/opt/bgutil-ytdlp-pot-provider/server/build/generate_once.js",
)

app = FastAPI(
    title=APP_NAME,
    version="1.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


class URLRequest(BaseModel):
    url: str
    format_id: Optional[str] = None
    audio_only: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Only public http/https URLs are supported.")
        return value


def is_public_hostname(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL.")

    try:
        ip = ipaddress.ip_address(hostname)
        if not ip.is_global:
            raise HTTPException(
                status_code=400,
                detail="Private/internal URLs are not allowed.",
            )
        return
    except ValueError:
        pass

    if not is_public_hostname(hostname):
        raise HTTPException(
            status_code=400,
            detail="The target host is not publicly reachable.",
        )


def extractor_name(info: dict) -> str:
    key = (info.get("extractor_key") or info.get("extractor") or "unknown").lower()
    mapping = {
        "youtube": "YouTube",
        "youtube_tab": "YouTube",
        "instagram": "Instagram",
        "facebook": "Facebook",
        "tiktok": "TikTok",
        "twitter": "Twitter/X",
        "x": "Twitter/X",
        "reddit": "Reddit",
        "vimeo": "Vimeo",
    }
    for needle, label in mapping.items():
        if needle in key:
            return label
    return info.get("webpage_url_domain") or key.title()


def human_quality(fmt: dict) -> Optional[str]:
    height = fmt.get("height")
    vcodec = fmt.get("vcodec")
    acodec = fmt.get("acodec")

    if vcodec == "none" and acodec != "none":
        return "Audio Only"

    if not height:
        return None

    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "1440p"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 480:
        return "480p"
    return f"{height}p"


def collect_formats(info: dict) -> list[dict]:
    output = []
    seen = set()

    for fmt in info.get("formats") or []:
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")
        height = fmt.get("height")
        ext = fmt.get("ext")
        fmt_id = str(fmt.get("format_id", ""))

        if not fmt_id or (vcodec == "none" and acodec == "none"):
            continue

        quality = human_quality(fmt)
        if not quality:
            continue

        # De-duplicate similar formats.
        key = (
            quality,
            ext,
            "video" if vcodec != "none" else "audio",
            bool(fmt.get("url")),
        )
        if key in seen:
            continue
        seen.add(key)

        output.append(
            {
                "format_id": fmt_id,
                "quality": quality,
                "height": height,
                "ext": ext,
                "fps": fmt.get("fps"),
                "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
                "has_video": vcodec != "none",
                "has_audio": acodec != "none",
                "protocol": fmt.get("protocol"),
                "direct_url": (
                    fmt.get("url")
                    if fmt.get("protocol") in {"http", "https"}
                    else None
                ),
            }
        )

    output.sort(
        key=lambda x: (
            0 if x["has_video"] else 1,
            -(x["height"] or 0),
            x["ext"] or "",
        )
    )
    return output[:MAX_FORMATS]


def ytdlp_base_options() -> dict:
    """
    Current YouTube configuration:
    - mweb client
    - bgutil PO-token provider via generation script
    - Node 22 as JS runtime for YouTube challenge solving
    - EJS scripts fetched from yt-dlp's GitHub when needed
    """
    return {
      "quiet": False,
"no_warnings": False,
"verbose": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": REQUEST_TIMEOUT,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "geo_bypass": True,
        "restrictfilenames": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["mweb"],
            },
            "youtubepot-bgutilscript": {
                "script_path": [BGUTIL_SCRIPT_PATH],
            },
        },
        "js_runtimes": {
            "node": {"path": None},
        },
        "remote_components": ["ejs:github"],
    }


def extract_info(url: str) -> dict:
    validate_public_url(url)

    try:
        with yt_dlp.YoutubeDL(ytdlp_base_options()) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {exc}")


def build_download_format(
    info: dict,
    requested_format_id: Optional[str],
    audio_only: bool,
) -> tuple[str, bool]:
    if audio_only or requested_format_id == "mp3":
        return "bestaudio/best", True

    available = {str(f.get("format_id")): f for f in info.get("formats") or []}

    if requested_format_id and requested_format_id in available:
        selected = available[requested_format_id]

        if (
            selected.get("vcodec") != "none"
            and selected.get("acodec") != "none"
        ):
            return requested_format_id, False

        height = selected.get("height") or 720
        return (
            f"{requested_format_id}+bestaudio/best[height<={int(height)}]/best",
            False,
        )

    return (
        "bestvideo[height<=2160]+bestaudio/"
        "best[height<=2160]/best",
        False,
    )


def download_media(
    url: str,
    requested_format_id: Optional[str],
    audio_only: bool,
) -> Path:
    validate_public_url(url)
    info = extract_info(url)
    format_selector, is_mp3 = build_download_format(
        info,
        requested_format_id,
        audio_only,
    )

    with tempfile.TemporaryDirectory(prefix="uvd-") as tmp:
        tmp_path = Path(tmp)
        output_template = str(tmp_path / "download.%(ext)s")

        ydl_opts = ytdlp_base_options()
        ydl_opts.update(
            {
                "skip_download": False,
                "format": format_selector,
                "outtmpl": output_template,
                "merge_output_format": "mp4",
                "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
                "overwrites": True,
            }
        )

        if is_mp3:
            ydl_opts.update(
                {
                    "format": "bestaudio/best",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
                    ],
                    "keepvideo": False,
                }
            )

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Download failed: {exc}")

        candidates = [
            p
            for p in tmp_path.iterdir()
            if p.is_file() and not p.name.endswith((".part", ".ytdl"))
        ]

        if not candidates:
            raise HTTPException(
                status_code=500,
                detail="No output file was created.",
            )

        source_file = max(candidates, key=lambda p: p.stat().st_mtime)

        persistent_temp = Path(tempfile.mkstemp(prefix="uvd-output-")[1])
        persistent_temp.unlink(missing_ok=True)
        source_file.replace(persistent_temp)
        return persistent_temp


def media_filename(info: dict, path: Path) -> str:
    title = info.get("title") or "download"
    safe = "".join(
        ch if ch.isalnum() or ch in " -_()." else "_"
        for ch in title
    )
    safe = safe[:100].strip() or "download"
    return f"{safe}.{path.suffix.lstrip('.')}"


@app.get("/")
async def root():
    return {
        "name": APP_NAME,
        "status": "ok",
        "docs": "/docs",
        "endpoints": ["/api/info", "/api/download"],
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/api/info")
async def info_get(url: str = Query(..., min_length=8)):
    info = await asyncio.to_thread(extract_info, url)

    formats = collect_formats(info)
    for fmt in formats:
        fmt["download_url"] = (
            f"/api/download?url={url}&format_id={fmt['format_id']}"
        )

    formats.insert(
        0,
        {
            "format_id": "mp3",
            "quality": "Audio MP3",
            "height": None,
            "ext": "mp3",
            "fps": None,
            "filesize": None,
            "has_video": False,
            "has_audio": True,
            "protocol": "server",
            "direct_url": None,
            "download_url": f"/api/download?url={url}&format_id=mp3",
        },
    )

    return {
        "success": True,
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "platform": extractor_name(info),
        "webpage_url": info.get("webpage_url"),
        "formats": formats,
    }


@app.post("/api/info")
async def info_post(payload: URLRequest):
    return await info_get(payload.url)


@app.get("/api/download")
async def download_get(
    url: str = Query(..., min_length=8),
    format_id: Optional[str] = None,
    audio_only: bool = False,
):
    async with download_semaphore:
        output_path = await asyncio.to_thread(
            download_media,
            url,
            format_id,
            audio_only,
        )

    info = await asyncio.to_thread(extract_info, url)
    filename = media_filename(info, output_path)
    media_type = (
        "audio/mpeg"
        if format_id == "mp3" or audio_only
        else "video/mp4"
    )

    return FileResponse(
        output_path,
        media_type=media_type,
        filename=filename,
        background=BackgroundTask(
            lambda: output_path.unlink(missing_ok=True)
        ),
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/download")
async def download_post(payload: URLRequest):
    return await download_get(
        url=payload.url,
        format_id=payload.format_id,
        audio_only=payload.audio_only,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    _: Request,
    exc: Exception,
):
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error.",
        },
    )
