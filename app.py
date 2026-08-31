import os
import re
import tempfile
import traceback
import urllib.request

from flask import Flask, request, jsonify
import yt_dlp

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ייבוא הדפדפן הוירטואלי
from playwright.sync_api import sync_playwright


app = Flask(__name__)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

API_KEY = os.environ.get("API_KEY", "")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")

GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


# ============================================================
# TOR PROXY
# ============================================================

TOR_PROXY_URL = "socks5h://127.0.0.1:9050"


# ============================================================
# YT-DLP VERSION
# ============================================================

YTDLP_VERSION = getattr(yt_dlp.version, "__version__", "unknown")


# ============================================================
# SMART SCRAPE USING HEADLESS BROWSER
# ============================================================

def extract_hidden_m3u8(url):
    print(f"Starting Virtual Browser for: {url}", flush=True)
    found_m3u8 = None

    try:
        with sync_playwright() as p:
            # הפעלת דפדפן כרום מותאם לשרת לינוקס
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            page = browser.new_page()

            # פונקציה שמאזינה לכל קובץ שהאתר מנסה לטעון ברקע
            def log_request(request):
                nonlocal found_m3u8
                if ".m3u8" in request.url:
                    # אנחנו תמיד מחפשים את קובץ המאסטר הראשי
                    if not found_m3u8 or "master" in request.url.lower():
                        found_m3u8 = request.url

            page.on("request", log_request)

            try:
                # טוענים את האתר ומחכים שהרשת תירגע (כל הסקריפטים סיימו)
                page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception as e:
                print(f"Page load note (usually fine): {e}", flush=True)

            # אם הנגן איטי, ניתן לו עוד 5 שניות לרוץ
            if not found_m3u8:
                page.wait_for_timeout(5000)

            browser.close()

        if found_m3u8:
            print(f"Virtual Browser caught m3u8: {found_m3u8}", flush=True)
            return found_m3u8
        else:
            raise RuntimeError("לא נמצא קישור וידאו. ייתכן ואין סרטון בכתבה או שהאתר שינה את המבנה שלו.")

    except Exception as e:
        print(f"Virtual browser error: {e}", flush=True)
        raise RuntimeError(f"שגיאה בהפעלת הדפדפן הוירטואלי: {str(e)}")


# ============================================================
# PLATFORM DETECTION
# ============================================================

def detect_platform(url):
    if not url: return "other"
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "instagram.com" in u: return "instagram"
    if "tiktok.com" in u: return "tiktok"
    if any(domain in u for domain in ["mako.co.il", "n12.co.il", "13tv.co.il", "kan.org.il", "now14.co.il", "c14.co.il", "ynet.co.il", "immergo.tv", ".m3u8"]):
        return "news_il"
    return "other"


# ============================================================
# VIDEO URL VALIDATION
# ============================================================

VIDEO_URL_PATTERNS = [
    re.compile(r"(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w\-]+[^\s]*", re.IGNORECASE),
    re.compile(r"(https?://)?(www\.)?instagram\.com/(p|reel|reels|tv)/[\w\-]+[^\s]*", re.IGNORECASE),
    re.compile(r"(https?://)?(www\.|vm\.|vt\.)?tiktok\.com/(@[\w.\-]+/video/\d+|[\w]+)[^\s]*", re.IGNORECASE),
    re.compile(r"(https?://)?([a-zA-Z0-9-]+\.)?(mako\.co\.il|n12\.co\.il|13tv\.co\.il|kan\.org\.il|now14\.co\.il|c14\.co\.il|ynet\.co\.il|immergo\.tv)/.+", re.IGNORECASE),
    re.compile(r".*\.m3u8.*", re.IGNORECASE)
]

def is_valid_video_url(url):
    if not url: return False
    text = url.strip()
    return any(pattern.match(text) for pattern in VIDEO_URL_PATTERNS)


# ============================================================
# GOOGLE DRIVE
# ============================================================

def get_drive_service():
    creds = Credentials(
        token=None, refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID, client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=DRIVE_SCOPES,
    )
    creds.refresh(GoogleAuthRequest())
    return build("drive", "v3", credentials=creds)

def upload_to_drive(local_path, filename):
    service = get_drive_service()
    file_metadata = {"name": filename, "parents": ([DRIVE_FOLDER_ID] if DRIVE_FOLDER_ID else [])}
    media = MediaFileUpload(local_path, resumable=True)
    uploaded = service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink").execute()
    return (uploaded.get("webViewLink"), uploaded.get("id"))


# ============================================================
# COOKIES & CLIENTS
# ============================================================

def write_cookies_file(tmp_dir):
    if not YOUTUBE_COOKIES: return None
    cookies_path = os.path.join(tmp_dir, "cookies.txt")
    with open(cookies_path, "w", encoding="utf-8") as f:
        f.write(YOUTUBE_COOKIES)
    return cookies_path

def normalize_player_clients(player_clients):
    if player_clients is None: return None
    if isinstance(player_clients, str):
        player_clients = [x.strip() for x in player_clients.split(",") if x.strip()]
    if not isinstance(player_clients, list): return None
    result = [str(x).strip() for x in player_clients if str(x).strip()]
    return result or None

DEFAULT_CLIENT_ATTEMPTS = [
    (["android"], True), (["ios"], True), (["web_embedded"], True),
    (["tv_simply"], True), (["tv"], True), (["android_vr"], True),
    (["web_safari"], True), (["mweb"], True), (["web"], True),
    (["android"], False), (["ios"], False), (["web_embedded"], False),
    (["tv_simply"], False), (["tv"], False), (["android_vr"], False),
    (["web_safari"], False), (["mweb"], False), (["web"], False),
]


# ============================================================
# BUILD YT-DLP OPTIONS
# ============================================================

def build_ytdlp_options(tmp_dir, fmt, player_clients, use_cookies, platform="youtube", diagnostic=False, use_proxy=False):
    outtmpl = os.path.join(tmp_dir, "%(title)s.%(ext)s")
    extractor_args = {"youtubepot-bgutilhttp": {"base_url": "http://127.0.0.1:4416"}}
    if platform == "youtube" and player_clients:
        extractor_args["youtube"] = {"player_client": player_clients}

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }

    if use_proxy: options["proxy"] = TOR_PROXY_URL
    if diagnostic:
        options["quiet"], options["no_warnings"] = False, False
    else:
        options["quiet"], options["no_warnings"] = True, True

    if fmt == "audio":
        options["format"] = "bestaudio[acodec!=none]/best[acodec!=none]/best"
        options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        options["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
        options["merge_output_format"] = "mp4"

    if use_cookies and platform == "youtube":
        cookies_path = write_cookies_file(tmp_dir)
        if cookies_path: options["cookiefile"] = cookies_path
            
    return options


# ============================================================
# FIND FINAL FILE & DOWNLOAD ENGINE
# ============================================================

def find_downloaded_file(tmp_dir, fmt):
    ignored = {"cookies.txt"}
    candidates = []
    for root, dirs, files in os.walk(tmp_dir):
        for filename in files:
            if filename in ignored or filename.endswith(".part") or filename.endswith(".ytdl"): continue
            full_path = os.path.join(root, filename)
            if not os.path.isfile(full_path): continue
            try: size = os.path.getsize(full_path)
            except OSError: continue
            if size <= 0: continue
            lower_name = filename.lower()
            if fmt == "audio": allowed = (".mp3", ".m4a", ".opus", ".webm", ".aac", ".wav")
            else: allowed = (".mp4", ".mkv", ".webm", ".mov", ".avi")
            if lower_name.endswith(allowed): candidates.append((full_path, size))

    if not candidates:
        for root, dirs, files in os.walk(tmp_dir):
            for filename in files:
                if filename in ignored or filename.endswith(".part") or filename.endswith(".ytdl"): continue
                full_path = os.path.join(root, filename)
                if not os.path.isfile(full_path): continue
                try: size = os.path.getsize(full_path)
                except OSError: continue
                if size > 0: candidates.append((full_path, size))

    if not candidates: return None
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[0][0]

def _try_download_once(url, fmt, tmp_dir, player_clients, use_cookies, platform="youtube", use_proxy=False):
    ydl_opts = build_ytdlp_options(tmp_dir, fmt, player_clients, use_cookies, platform, False, use_proxy)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info: raise RuntimeError("yt-dlp לא החזיר מידע על הסרטון")
    title = info.get("title", "download")
    final_path = find_downloaded_file(tmp_dir, fmt)
    if not final_path: raise RuntimeError("ההורדה הסתיימה אך הקובץ הסופי לא נמצא")
    return (final_path, title)

def download_video(url, fmt, tmp_dir, player_clients=None, use_cookies=True, use_proxy=False):
    platform = detect_platform(url)
    attempts = []

    if platform == "youtube":
        req_clients = normalize_player_clients(player_clients)
        if req_clients:
            attempts.append((req_clients, bool(use_cookies)))
            if use_cookies: attempts.append((req_clients, False))
        for combo in DEFAULT_CLIENT_ATTEMPTS:
            if combo not in attempts: attempts.append(combo)
    else:
        attempts.append((None, bool(use_cookies)))
        if use_cookies: attempts.append((None, False))

    formats_to_try = ["video", "audio"] if fmt == "auto" else [fmt]
    last_error = None

    for target_fmt in formats_to_try:
        for clients, cookies_flag in attempts:
            safe_clients = "_".join(clients) if clients else "default"
            sub_dir = os.path.join(tmp_dir, f"try_{target_fmt}_{safe_clients}_{int(cookies_flag)}")
            os.makedirs(sub_dir, exist_ok=True)
            try:
                result = _try_download_once(url, target_fmt, sub_dir, clients, cookies_flag, platform, use_proxy)
                return result
            except Exception as e:
                last_error = e
                continue

    if last_error: raise RuntimeError(f"כל ניסיונות ההורדה נכשלו. yt-dlp האחרון החזיר: {repr(last_error)}")
    raise RuntimeError("כל ניסיונות ההורדה נכשלו")


# ============================================================
# API ENDPOINTS
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "yt_dlp_version": YTDLP_VERSION})


@app.route("/formats", methods=["POST"])
def list_formats():
    provided_key = request.headers.get("X-API-KEY", "")
    if not API_KEY or provided_key != API_KEY: return jsonify({"success": False, "error": "unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url: return jsonify({"success": False, "error": "missing 'url'"}), 400

    il_news_domains = ["mako.co.il", "n12.co.il", "13tv.co.il", "kan.org.il", "now14.co.il", "c14.co.il", "ynet.co.il"]
    if any(domain in url.lower() for domain in il_news_domains):
        try:
            url = extract_hidden_m3u8(url)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    platform = detect_platform(url)
    player_clients = normalize_player_clients(data.get("player_client"))
    if platform == "youtube" and not player_clients: player_clients = ["web_embedded"]

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ydl_opts = build_ytdlp_options(tmp_dir, "video", player_clients, data.get("use_cookies", True), platform, True, data.get("use_proxy", False))
            ydl_opts["skip_download"] = True
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
            return jsonify({"success": True, "title": info.get("title")})
    except Exception as e:
        return jsonify({"success": False, "error": repr(e)}), 500


@app.route("/download", methods=["POST"])
def download():
    provided_key = request.headers.get("X-API-KEY", "")
    if not API_KEY or provided_key != API_KEY: return jsonify({"success": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    fmt = data.get("format", "audio")

    if not url: return jsonify({"success": False, "error": "missing 'url'"}), 400
    if not is_valid_video_url(url): return jsonify({"success": False, "error": "קישור לא נתמך"}), 400

    il_news_domains = ["mako.co.il", "n12.co.il", "13tv.co.il", "kan.org.il", "now14.co.il", "c14.co.il", "ynet.co.il"]
    if any(domain in url.lower() for domain in il_news_domains):
        try:
            url = extract_hidden_m3u8(url)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    platform = detect_platform(url)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path, title = download_video(
                url=url, fmt=fmt, tmp_dir=tmp_dir,
                player_clients=data.get("player_client"),
                use_cookies=data.get("use_cookies", True),
                use_proxy=data.get("use_proxy", False)
            )
            drive_link, file_id = upload_to_drive(local_path, os.path.basename(local_path))
            return jsonify({"success": True, "platform": platform, "title": title, "driveLink": drive_link, "fileId": file_id})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": repr(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
