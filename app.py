import os
import re
import tempfile
import traceback
import urllib.request
import json

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
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")

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

            browser.close()

        if found_stream:
            return found_stream
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
# COOKIES
# ============================================================

def write_cookies_file(tmp_dir):
    if not YOUTUBE_COOKIES:
        print("YOUTUBE_COOKIES: NOT CONFIGURED", flush=True)
        return None

    cookies_path = os.path.join(
        tmp_dir,
        "cookies.txt"
    )

    with open(
        cookies_path,
        "w",
        encoding="utf-8"
    ) as f:
        f.write(YOUTUBE_COOKIES)

    try:
        size = os.path.getsize(
            cookies_path
        )
    except OSError:
        size = 0

    print(
        "YOUTUBE_COOKIES: configured, "
        f"cookies.txt created ({size} bytes)", flush=True
    )

    if size == 0:
        print("WARNING: cookies.txt is empty", flush=True)
        return None

    return cookies_path


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
# DEFAULT CLIENT STRATEGY
# ============================================================

DEFAULT_CLIENT_ATTEMPTS = [
    (["android"], True),
    (["ios"], True),
    (["web_embedded"], True),
    (["tv_simply"], True),
    (["tv"], True),
    (["android_vr"], True),
    (["web_safari"], True),
    (["mweb"], True),
    (["web"], True),
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
    use_cookies,
    platform="youtube",
    diagnostic=False,
    use_proxy=False
):
    outtmpl = os.path.join(
        tmp_dir,
        "%(title)s.%(ext)s"
    )

    extractor_args = {
        "youtubepot-bgutilhttp": {
            "base_url": "http://127.0.0.1:4416"
        }
    }

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
    }
    
    if platform == "news_il":
        options["http_headers"] = {
            "Accept": "*/*",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.mako.co.il/"
        }

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

    if use_cookies and platform == "youtube":
        cookies_path = write_cookies_file(
            tmp_dir
        )
        if cookies_path:
            options["cookiefile"] = cookies_path
            print("yt-dlp configuration: Cookies ENABLED", flush=True)
        else:
            print("yt-dlp configuration: Cookies REQUESTED but unavailable", flush=True)
    else:
        print("yt-dlp configuration: Cookies DISABLED", flush=True)

    return options


# ============================================================
# FIND FINAL FILE
# ============================================================

def find_downloaded_file(tmp_dir, fmt):
    ignored = {"cookies.txt"}
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
# ONE DOWNLOAD ATTEMPT
# ============================================================

def _try_download_once(
    url,
    fmt,
    tmp_dir,
    player_clients,
    use_cookies,
    platform="youtube",
    use_proxy=False
):
    ydl_opts = build_ytdlp_options(
        tmp_dir=tmp_dir,
        fmt=fmt,
        player_clients=player_clients,
        use_cookies=use_cookies,
        platform=platform,
        diagnostic=False,
        use_proxy=use_proxy
    )

    print(
        "Starting yt-dlp attempt:",
        {
            "platform": platform,
            "format": fmt,
            "clients": player_clients,
            "cookies": use_cookies,
            "cookies_configured": bool(YOUTUBE_COOKIES),
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
# DOWNLOAD ENGINE (multi-platform)
# ============================================================

def download_video(
    url,
    fmt,
    tmp_dir,
    player_clients=None,
    use_cookies=True,
    use_proxy=False
):
    platform = detect_platform(url)
    attempts = []

    if platform == "youtube":
        requested_clients = normalize_player_clients(player_clients)
        if requested_clients:
            requested_attempt = (requested_clients, bool(use_cookies))
            attempts.append(requested_attempt)
            if use_cookies:
                no_cookie_attempt = (requested_clients, False)
                if no_cookie_attempt not in attempts:
                    attempts.append(no_cookie_attempt)

        for combo in DEFAULT_CLIENT_ATTEMPTS:
            if combo not in attempts:
                attempts.append(combo)
    else:
        attempts.append((None, bool(use_cookies)))
        if use_cookies:
            attempts.append((None, False))

    if fmt == "auto":
        formats_to_try = ["video", "audio"]
    else:
        formats_to_try = [fmt]

    last_error = None

    for target_fmt in formats_to_try:
        for clients, cookies_flag in attempts:
            safe_clients = "_".join(clients) if clients else "default"
            sub_dir = os.path.join(tmp_dir, f"try_{target_fmt}_{safe_clients}_{int(cookies_flag)}")
            os.makedirs(sub_dir, exist_ok=True)

            print("--------------------------------------------------", flush=True)
            print(
                "DOWNLOAD ATTEMPT",
                {
                    "platform": platform,
                    "format": target_fmt,
                    "clients": clients,
                    "cookies": cookies_flag,
                    "cookies_configured": bool(YOUTUBE_COOKIES),
                    "proxy": use_proxy
                }, flush=True
            )

            try:
                result = _try_download_once(
                    url=url,
                    fmt=target_fmt,
                    tmp_dir=sub_dir,
                    player_clients=clients,
                    use_cookies=cookies_flag,
                    platform=platform,
                    use_proxy=use_proxy
                )

                print(
                    "SUCCESSFUL yt-dlp attempt:",
                    {
                        "platform": platform,
                        "format": target_fmt,
                        "clients": clients,
                        "cookies": cookies_flag
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
                        "cookies": cookies_flag,
                        "error": repr(e)
                    }, flush=True
                )
                continue

    if last_error:
        raise RuntimeError(f"כל ניסיונות ההורדה נכשלו. yt-dlp האחרון החזיר: {repr(last_error)}")

    raise RuntimeError("כל ניסיונות ההורדה נכשלו")


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "yt_dlp_version": YTDLP_VERSION,
        "cookies_configured": bool(YOUTUBE_COOKIES),
        "pot_provider": "http://127.0.0.1:4416",
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
    use_cookies = data.get("use_cookies", True)
    use_proxy = data.get("use_proxy", False)
    season = data.get("season")
    episode = data.get("episode")

    if not url:
        return jsonify({"success": False, "error": "missing 'url'"}), 400

    il_news_domains = ["mako.co.il", "n12.co.il", "13tv.co.il", "kan.org.il", "now14.co.il", "c14.co.il", "ynet.co.il"]
    if ".m3u8" not in url.lower() and any(domain in url.lower() for domain in il_news_domains):
        try:
            url = extract_hidden_m3u8(url, season=season, episode=episode)
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
        "use_cookies": bool(use_cookies),
        "use_proxy": bool(use_proxy),
        "cookies_configured": bool(YOUTUBE_COOKIES),
        "pot_provider": "http://127.0.0.1:4416"
    }

    print("FORMAT REQUEST:", diagnostic, flush=True)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ydl_opts = build_ytdlp_options(
                tmp_dir=tmp_dir,
                fmt="video",
                player_clients=player_clients,
                use_cookies=use_cookies,
                platform=platform,
                diagnostic=True,
                use_proxy=use_proxy
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
    url = data.get("url")
    fmt = data.get("format", "audio")
    player_clients = data.get("player_client")
    use_cookies = data.get("use_cookies", True)
    use_proxy = data.get("use_proxy", False)
    season = data.get("season")
    episode = data.get("episode")

    if not url:
        return jsonify({"success": False, "error": "missing 'url'"}), 400

    if not is_valid_video_url(url):
        return jsonify({"success": False, "error": "קישור לא נתמך - יש לשלוח קישור תקין מיוטיוב, אינסטגרם, טיקטוק או אתרי חדשות"}), 400

    if fmt not in ("audio", "video", "auto"):
        return jsonify({"success": False, "error": "format must be 'audio', 'video' or 'auto'"}), 400

    il_news_domains = ["mako.co.il", "n12.co.il", "13tv.co.il", "kan.org.il", "now14.co.il", "c14.co.il", "ynet.co.il"]
    if ".m3u8" not in url.lower() and any(domain in url.lower() for domain in il_news_domains):
        try:
            url = extract_hidden_m3u8(url, season=season, episode=episode)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    platform = detect_platform(url)

    print(
        "DOWNLOAD REQUEST:",
        {
            "platform": platform,
            "format": fmt,
            "player_client": player_clients,
            "use_cookies": bool(use_cookies),
            "cookies_configured": bool(YOUTUBE_COOKIES),
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
                use_cookies=use_cookies,
                use_proxy=use_proxy
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
