import os
import re
import tempfile
import traceback
import urllib.request
import json
import subprocess

from flask import Flask, request, jsonify
import yt_dlp

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from playwright.sync_api import sync_playwright


app = Flask(__name__)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

API_KEY = os.environ.get("API_KEY", "")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")

GOOGLE_OAUTH_CLIENT_ID = os.environ.get(
    "GOOGLE_OAUTH_CLIENT_ID",
    ""
)

GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get(
    "GOOGLE_OAUTH_CLIENT_SECRET",
    ""
)

GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get(
    "GOOGLE_OAUTH_REFRESH_TOKEN",
    ""
)

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file"
]


# ============================================================
# TOR PROXY
# ============================================================

TOR_PROXY_URL = "socks5h://127.0.0.1:9050"


# ============================================================
# YT-DLP VERSION
# ============================================================

YTDLP_VERSION = getattr(
    yt_dlp.version,
    "__version__",
    "unknown"
)


# ============================================================
# ANTI-BOT BYPASS VOD SCRAPER
# ============================================================

def extract_hidden_m3u8(url, season=None, episode=None):
    print(f"Starting Strict Stream Scraper for: {url} (Season: {season}, Episode: {episode})", flush=True)
    found_stream = None
    cookies_str = ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--start-maximized'
                ]
            )
            
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="he-IL",
                timezone_id="Asia/Jerusalem"
            )
            
            page = context.new_page()

            def inspect_response(response):
                nonlocal found_stream
                try:
                    res_url = response.url.lower()
                    if any(x in res_url for x in ["google-analytics", "googletagmanager", "googleadservices", "perfdrive", "analytics", "collect", "pixel", "track"]):
                        return

                    if ".m3u8" in res_url or "keshet-vod" in res_url:
                        found_stream = response.url
                        print(f"Successfully caught real stream URL: {response.url}", flush=True)
                        return

                    if any(t in response.headers.get("content-type", "").lower() for t in ["json", "text", "javascript"]):
                        body = response.text()
                        matches = re.findall(r'https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*', body)
                        for m in matches:
                            m_clean = m.replace('\\/', '/')
                            if not any(x in m_clean.lower() for x in ["analytics", "google", "ad"]):
                                found_stream = m_clean
                                print(f"Successfully caught stream inside API body: {m_clean}", flush=True)
                                return
                except:
                    pass

            page.on("response", inspect_response)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.evaluate("() => { document.querySelectorAll('.fc-consent-root, .fc-dialog-overlay').forEach(el => el.remove()); }")
                except:
                    pass

                page.mouse.wheel(0, 500)
                page.wait_for_timeout(4000)

                if episode:
                    try:
                        print(f"Searching for Episode {episode}...", flush=True)
                        ep_locator = page.locator(f"text=פרק {episode}").first
                        if ep_locator.is_visible():
                            ep_locator.click(timeout=3000)
                            print(f"Clicked Episode {episode} successfully!", flush=True)
                            page.wait_for_timeout(4000)
                    except Exception as ex:
                        print(f"Episode click note: {ex}", flush=True)
                else:
                    for selector in ["video", ".immergo-player", ".video-player", "[aria-label*='הפעל']"]:
                        try:
                            el = page.locator(selector).first
                            if el.is_visible():
                                el.click(timeout=2000)
                                break
                        except:
                            continue

            except Exception as e:
                print(f"Navigation note: {e}", flush=True)

            print("Waiting for real stream initialization...", flush=True)
            for _ in range(20):
                if found_stream:
                    break
                page.wait_for_timeout(2000)
                try:
                    content = page.content().replace('\\/', '/')
                    html_matches = re.findall(r'https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*', content)
                    for hm in html_matches:
                        if not any(x in hm.lower() for x in ["analytics", "google", "ad"]):
                            found_stream = hm
                            print(f"Caught stream in HTML: {hm}", flush=True)
                            break
                except:
                    pass
                if found_stream:
                    break

            cookies = context.cookies()
            cookies_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            browser.close()

        if found_stream:
            return found_stream, cookies_str
        else:
            raise RuntimeError("הדפדפן סרק את העמוד אך לא נמצא קישור m3u8 תקף.")

    except Exception as e:
        print(f"Scraper error: {e}", flush=True)
        raise RuntimeError(f"שגיאה בסריקה: {str(e)}")


# ============================================================
# PLATFORM DETECTION
# ============================================================

def detect_platform(url):
    if not url:
        return "other"
    
    u = url.lower()
    
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    
    if "instagram.com" in u:
        return "instagram"
    
    if "tiktok.com" in u:
        return "tiktok"
        
    if any(domain in u for domain in ["mako.co.il", "n12.co.il", "13tv.co.il", "kan.org.il", "now14.co.il", "c14.co.il", "ynet.co.il", "immergo.tv", ".m3u8"]):
        return "news_il"

    return "other"


# ============================================================
# VIDEO URL VALIDATION (multi-platform + Israeli News + m3u8)
# ============================================================

VIDEO_URL_PATTERNS = [
    re.compile(r"(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w\-]+[^\s]*", re.IGNORECASE),
    re.compile(r"(https?://)?(www\.)?instagram\.com/(p|reel|reels|tv)/[\w\-]+[^\s]*", re.IGNORECASE),
    re.compile(r"(https?://)?(www\.|vm\.|vt\.)?tiktok\.com/(@[\w.\-]+/video/\d+|[\w]+)[^\s]*", re.IGNORECASE),
    re.compile(r"(https?://)?([a-zA-Z0-9-]+\.)?(mako\.co\.il|n12\.co\.il|13tv\.co\.il|kan\.org\.il|now14\.co\.il|c14\.co\.il|ynet\.co\.il|immergo\.tv)/.+", re.IGNORECASE),
    re.compile(r".*\.m3u8.*", re.IGNORECASE)
]


def is_valid_video_url(url):
    if not url:
        return False

    text = url.strip()

    return any(
        pattern.match(text)
        for pattern in VIDEO_URL_PATTERNS
    )


# ============================================================
# GOOGLE DRIVE
# ============================================================

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=DRIVE_SCOPES,
    )

    creds.refresh(
        GoogleAuthRequest()
    )

    return build(
        "drive",
        "v3",
        credentials=creds
    )


def upload_to_drive(local_path, filename):
    service = get_drive_service()

    file_metadata = {
        "name": filename,
        "parents": (
            [DRIVE_FOLDER_ID]
            if DRIVE_FOLDER_ID
            else []
        )
    }

    media = MediaFileUpload(
        local_path,
        resumable=True
    )

    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink"
    ).execute()

    return (
        uploaded.get("webViewLink"),
        uploaded.get("id")
    )


# ============================================================
# NORMALIZE CLIENTS
# ============================================================

def normalize_player_clients(player_clients):
    if player_clients is None:
        return None

    if isinstance(player_clients, str):
        player_clients = [
            x.strip()
            for x in player_clients.split(",")
            if x.strip()
        ]

    if not isinstance(player_clients, list):
        return None

    result = [
        str(x).strip()
        for x in player_clients
        if str(x).strip()
    ]

    return result or None


# ============================================================
# DEFAULT CLIENT STRATEGY (Cookies Disabled)
# ============================================================

DEFAULT_CLIENT_ATTEMPTS = [
    (["android"], False),
    (["ios"], False),
    (["web_embedded"], False),
    (["tv_simply"], False),
    (["tv"], False),
    (["android_vr"], False),
    (["web_safari"], False),
    (["mweb"], False),
    (["web"], False),
]


# ============================================================
# BUILD YT-DLP OPTIONS
# ============================================================

def build_ytdlp_options(
    tmp_dir,
    fmt,
    player_clients,
    platform="youtube",
    diagnostic=False,
    use_proxy=False,
    cookies_str=""
):
    outtmpl = os.path.join(
        tmp_dir,
        "%(title)s.%(ext)s"
    )

    extractor_args = {}

    if platform == "youtube" and player_clients:
        extractor_args["youtube"] = {
            "player_client": player_clients
        }

    options = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "extractor_args": extractor_args,
        "ignoreerrors": False,
        "continuedl": True,
        "nopart": False,
        "check_formats": "selected",
        "hls_use_mpegts": True,
        "nocheckcertificate": True,
    }
    
    if platform == "news_il":
        options["http_headers"] = {
            "Accept": "*/*",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.mako.co.il/",
            "Origin": "https://www.mako.co.il"
        }
        if cookies_str:
            dyn_cookie_path = os.path.join(tmp_dir, "dyn_cookies.txt")
            with open(dyn_cookie_path, "w", encoding="utf-8") as cf:
                cf.write("# Netscape HTTP Cookie File\n")
                for c_item in cookies_str.split("; "):
                    if "=" in c_item:
                        c_name, c_val = c_item.split("=", 1)
                        cf.write(f".mako.co.il\tTRUE\t/\tTRUE\t0\t{c_name.strip()}\t{c_val.strip()}\n")
            options["cookiefile"] = dyn_cookie_path

    if use_proxy:
        options["proxy"] = TOR_PROXY_URL

    if diagnostic:
        options["quiet"] = False
        options["no_warnings"] = False
    else:
        options["quiet"] = True
        options["no_warnings"] = True

    if fmt == "audio":
        options["format"] = (
            "bestaudio[acodec!=none]/"
            "best[acodec!=none]/"
            "best"
        )
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        options["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/"
            "best"
        )
        options["merge_output_format"] = "mp4"

    print("yt-dlp configuration: Cookies DISABLED (Strictly No Cookies)", flush=True)

    return options


# ============================================================
# FIND FINAL FILE
# ============================================================

def find_downloaded_file(tmp_dir, fmt):
    ignored = {"cookies.txt", "dyn_cookies.txt"}
    candidates = []

    for root, dirs, files in os.walk(tmp_dir):
        for filename in files:
            if filename in ignored:
                continue
            if filename.endswith(".part"):
                continue
            if filename.endswith(".ytdl"):
                continue

            full_path = os.path.join(root, filename)
            if not os.path.isfile(full_path):
                continue

            try:
                size = os.path.getsize(full_path)
            except OSError:
                continue

            if size <= 0:
                continue

            lower_name = filename.lower()

            if fmt == "audio":
                allowed = (".mp3", ".m4a", ".opus", ".webm", ".aac", ".wav")
            else:
                allowed = (".mp4", ".mkv", ".webm", ".mov", ".avi")

            if lower_name.endswith(allowed):
                candidates.append((full_path, size))

    if not candidates:
        for root, dirs, files in os.walk(tmp_dir):
            for filename in files:
                if filename in ignored or filename.endswith(".part") or filename.endswith(".ytdl"):
                    continue

                full_path = os.path.join(root, filename)
                if not os.path.isfile(full_path):
                    continue

                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue

                if size > 0:
                    candidates.append((full_path, size))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]


# ============================================================
# FFMPEG DIRECT DOWNLOAD FOR M3U8 (Fallback/Alternative)
# ============================================================

def download_m3u8_with_ffmpeg(m3u8_url, output_path, cookies_str=""):
    headers = "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\nReferer: https://www.mako.co.il/\r\n"
    if cookies_str:
        headers += f"Cookie: {cookies_str}\r\n"

    cmd = [
        "ffmpeg",
        "-headers", headers,
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        output_path,
        "-y"
    ]
    
    print(f"Running ffmpeg download for m3u8...", flush=True)
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr[-300:]}")
    return output_path


# ============================================================
# ONE DOWNLOAD ATTEMPT
# ============================================================

def _try_download_once(
    url,
    fmt,
    tmp_dir,
    player_clients,
    platform="youtube",
    use_proxy=False,
    cookies_str=""
):
    if platform == "news_il" and ".m3u8" in url.lower():
        output_ext = "mp3" if fmt == "audio" else "mp4"
        output_path = os.path.join(tmp_dir, f"downloaded_stream.{output_ext}")
        try:
            download_m3u8_with_ffmpeg(url, output_path, cookies_str)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return output_path, "m3u8_stream"
        except Exception as ex:
            print(f"FFmpeg direct download failed, falling back to yt-dlp: {ex}", flush=True)

    ydl_opts = build_ytdlp_options(
        tmp_dir=tmp_dir,
        fmt=fmt,
        player_clients=player_clients,
        platform=platform,
        diagnostic=False,
        use_proxy=use_proxy,
        cookies_str=cookies_str
    )

    print(
        "Starting yt-dlp attempt:",
        {
            "platform": platform,
            "format": fmt,
            "clients": player_clients,
            "cookies": False,
            "yt_dlp_version": YTDLP_VERSION
        }, flush=True
    )

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if not info:
        raise RuntimeError("yt-dlp לא החזיר מידע על הסרטון")

    title = info.get("title", "download")
    final_path = find_downloaded_file(tmp_dir, fmt)

    if not final_path:
        raise RuntimeError("ההורדה הסתיימה אך הקובץ הסופי לא נמצא")

    return (final_path, title)


# ============================================================
# DOWNLOAD ENGINE (multi-platform, No Cookies)
# ============================================================

def download_video(
    url,
    fmt,
    tmp_dir,
    player_clients=None,
    use_proxy=False,
    cookies_str=""
):
    platform = detect_platform(url)
    attempts = []

    if platform == "youtube":
        requested_clients = normalize_player_clients(player_clients)
        if requested_clients:
            attempts.append((requested_clients, False))

        for combo in DEFAULT_CLIENT_ATTEMPTS:
            if combo not in attempts:
                attempts.append(combo)
    else:
        attempts.append((None, False))

    if fmt == "auto":
        formats_to_try = ["video", "audio"]
    else:
        formats_to_try = [fmt]

    last_error = None

    for target_fmt in formats_to_try:
        for clients, _ in attempts:
            safe_clients = "_".join(clients) if clients else "default"
            sub_dir = os.path.join(tmp_dir, f"try_{target_fmt}_{safe_clients}_0")
            os.makedirs(sub_dir, exist_ok=True)

            print("--------------------------------------------------", flush=True)
            print(
                "DOWNLOAD ATTEMPT",
                {
                    "platform": platform,
                    "format": target_fmt,
                    "clients": clients,
                    "cookies": False,
                    "proxy": use_proxy
                }, flush=True
            )

            try:
                result = _try_download_once(
                    url=url,
                    fmt=target_fmt,
                    tmp_dir=sub_dir,
                    player_clients=clients,
                    platform=platform,
                    use_proxy=use_proxy,
                    cookies_str=cookies_str
                )

                print(
                    "SUCCESSFUL download attempt:",
                    {
                        "platform": platform,
                        "format": target_fmt,
                        "clients": clients,
                        "cookies": False
                    }, flush=True
                )
                return result

            except Exception as e:
                last_error = e
                print(
                    "Download attempt failed:",
                    {
                        "platform": platform,
                        "format": target_fmt,
                        "clients": clients,
                        "cookies": False,
                        "error": repr(e)
                    }, flush=True
                )
                continue

    if last_error:
        raise RuntimeError(f"כל ניסיונות ההורדה נכשלו. השגיאה האחרונה: {repr(last_error)}")

    raise RuntimeError("כל ניסיונות ההורדה נכשלו")


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "yt_dlp_version": YTDLP_VERSION,
        "cookies_configured": False,
        "supported_platforms": ["youtube", "instagram", "tiktok", "news_il"]
    })


# ============================================================
# FORMATS DIAGNOSTIC
# ============================================================

@app.route("/formats", methods=["POST"])
def list_formats():
    provided_key = request.headers.get("X-API-KEY", "")
    if not API_KEY or provided_key != API_KEY:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    player_clients = data.get("player_client")
    use_proxy = data.get("use_proxy", False)
    season = data.get("season")
    episode = data.get("episode")

    if not url:
        return jsonify({"success": False, "error": "missing 'url'"}), 400

    cookies_str = ""
    il_news_domains = ["mako.co.il", "n12.co.il", "13tv.co.il", "kan.org.il", "now14.co.il", "c14.co.il", "ynet.co.il"]
    if ".m3u8" not in url.lower() and any(domain in url.lower() for domain in il_news_domains):
        try:
            url, cookies_str = extract_hidden_m3u8(url, season=season, episode=episode)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    platform = detect_platform(url)
    player_clients = normalize_player_clients(player_clients)

    if platform == "youtube" and not player_clients:
        player_clients = ["web_embedded"]

    diagnostic = {
        "yt_dlp_version": YTDLP_VERSION,
        "platform": platform,
        "player_client": player_clients,
        "use_cookies": False,
        "use_proxy": bool(use_proxy),
        "cookies_configured": False
    }

    print("FORMAT REQUEST:", diagnostic, flush=True)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ydl_opts = build_ytdlp_options(
                tmp_dir=tmp_dir,
                fmt="video",
                player_clients=player_clients,
                platform=platform,
                diagnostic=True,
                use_proxy=use_proxy,
                cookies_str=cookies_str
            )
            ydl_opts["skip_download"] = True

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)

            if not info:
                raise RuntimeError("yt-dlp לא החזיר מידע")

            formats = info.get("formats", []) or []
            simplified = []

            for f in formats:
                simplified.append({
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "acodec": f.get("acodec"),
                    "vcodec": f.get("vcodec"),
                    "abr": f.get("abr"),
                    "tbr": f.get("tbr"),
                    "height": f.get("height"),
                    "width": f.get("width"),
                    "fps": f.get("fps"),
                    "note": f.get("format_note"),
                    "has_url": bool(f.get("url")),
                    "protocol": f.get("protocol"),
                    "filesize": f.get("filesize"),
                    "filesize_approx": f.get("filesize_approx"),
                })

            real_formats = [f for f in simplified if not str(f["format_id"]).startswith("sb")]
            video_formats = [f for f in real_formats if f.get("vcodec") and f.get("vcodec") != "none"]
            audio_formats = [f for f in real_formats if f.get("acodec") and f.get("acodec") != "none"]
            combined_formats = [f for f in real_formats if f.get("vcodec") and f.get("vcodec") != "none" and f.get("acodec") and f.get("acodec") != "none"]

            return jsonify({
                "success": True,
                "diagnostic": diagnostic,
                "video_info": {
                    "id": info.get("id"),
                    "title": info.get("title"),
                    "extractor": info.get("extractor"),
                    "webpage_url": info.get("webpage_url"),
                    "duration": info.get("duration"),
                    "format_count": len(formats)
                },
                "count": len(simplified),
                "real_count": len(real_formats),
                "video_format_count": len(video_formats),
                "audio_format_count": len(audio_formats),
                "combined_format_count": len(combined_formats),
                "real_formats": real_formats,
                "all_formats": simplified
            })

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "success": False,
            "diagnostic": diagnostic,
            "error": repr(e),
            "error_type": type(e).__name__
        }), 500


# ============================================================
# DOWNLOAD API
# ============================================================

@app.route("/download", methods=["POST"])
def download():
    provided_key = request.headers.get("X-API-KEY", "")
    if not API_KEY or provided_key != API_KEY:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    raw_input = data.get("url", "").strip()
    
    parts = raw_input.split()
    url = parts[0] if parts else ""
    season = parts[1] if len(parts) > 1 else data.get("season")
    episode = parts[2] if len(parts) > 2 else data.get("episode")

    fmt = data.get("format", "audio")
    player_clients = data.get("player_client")
    use_proxy = data.get("use_proxy", False)

    if not url:
        return jsonify({"success": False, "error": "missing 'url'"}), 400

    if not is_valid_video_url(url):
        return jsonify({"success": False, "error": "קישור לא נתמך - יש לשלוח קישור תקין מיוטיוב, אינסטגרם, טיקטוק או אתרי חדשות"}), 400

    if fmt not in ("audio", "video", "auto"):
        return jsonify({"success": False, "error": "format must be 'audio', 'video' or 'auto'"}), 400

    cookies_str = ""
    il_news_domains = ["mako.co.il", "n12.co.il", "13tv.co.il", "kan.org.il", "now14.co.il", "c14.co.il", "ynet.co.il"]
    if ".m3u8" not in url.lower() and any(domain in url.lower() for domain in il_news_domains):
        try:
            url, cookies_str = extract_hidden_m3u8(url, season=season, episode=episode)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    platform = detect_platform(url)

    print(
        "DOWNLOAD REQUEST:",
        {
            "platform": platform,
            "format": fmt,
            "season": season,
            "episode": episode,
            "player_client": player_clients,
            "use_cookies": False,
            "use_proxy": use_proxy,
            "yt_dlp_version": YTDLP_VERSION
        }, flush=True
    )

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path, title = download_video(
                url=url,
                fmt=fmt,
                tmp_dir=tmp_dir,
                player_clients=player_clients,
                use_proxy=use_proxy,
                cookies_str=cookies_str
            )

            drive_link, file_id = upload_to_drive(local_path, os.path.basename(local_path))

            return jsonify({
                "success": True,
                "platform": platform,
                "title": title,
                "driveLink": drive_link,
                "fileId": file_id
            })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": repr(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
