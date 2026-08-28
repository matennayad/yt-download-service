import os
import re
import tempfile
import traceback

from flask import Flask, request, jsonify
import yt_dlp

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


app = Flask(__name__)


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

API_KEY = os.environ.get("API_KEY", "")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")

GOOGLE_OAUTH_CLIENT_ID = os.environ.get(
    "GOOGLE_OAUTH_CLIENT_ID", ""
)

GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get(
    "GOOGLE_OAUTH_CLIENT_SECRET", ""
)

GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get(
    "GOOGLE_OAUTH_REFRESH_TOKEN", ""
)

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file"
]


# ============================================================
# TOR PROXY (local SOCKS5, started in the same container)
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
# YOUTUBE URL
# ============================================================

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/watch\?v=|youtu\.be/)"
    r"[\w\-]+[^\s]*",
    re.IGNORECASE
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

    return [
        str(x).strip()
        for x in player_clients
        if str(x).strip()
    ]


# ============================================================
# DEFAULT CLIENT STRATEGY
# ============================================================

DEFAULT_CLIENT_ATTEMPTS = [

    (["web_embedded"], False),
    (["web_embedded"], True),

    (["tv_simply"], False),
    (["tv_simply"], True),

    (["tv"], False),
    (["tv"], True),

    (["android_vr"], False),
    (["android_vr"], True),

    (["web_safari"], False),
    (["web_safari"], True),

    (["mweb"], False),
    (["mweb"], True),

    (["android"], False),
    (["android"], True),

    (["ios"], False),
    (["ios"], True),

    (["web"], False),
    (["web"], True),
]


# ============================================================
# BUILD YT-DLP OPTIONS
# ============================================================

def build_ytdlp_options(
    tmp_dir,
    fmt,
    player_clients,
    use_cookies,
    diagnostic=False,
    use_proxy=False
):

    outtmpl = os.path.join(
        tmp_dir,
        "%(title)s.%(ext)s"
    )

    # ========================================================
    # YOUTUBE + PO TOKEN PROVIDER
    # ========================================================

    extractor_args = {
        "youtube": {
            "player_client": player_clients
        },

        "youtubepot-bgutilhttp": {
            "base_url": "http://127.0.0.1:4416"
        }
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

    # ========================================================
    # PROXY (Tor)
    # ========================================================

    if use_proxy:

        options["proxy"] = TOR_PROXY_URL

    # ========================================================
    # LOGGING
    # ========================================================

    if diagnostic:

        options["quiet"] = False
        options["no_warnings"] = False

    else:

        options["quiet"] = True
        options["no_warnings"] = True

    # ========================================================
    # FORMAT
    # ========================================================

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

    # ========================================================
    # COOKIES
    # ========================================================

    if use_cookies:

        cookies_path = write_cookies_file(
            tmp_dir
        )

        if cookies_path:

            options["cookiefile"] = cookies_path

    return options


# ============================================================
# FIND FINAL FILE
# ============================================================

def find_downloaded_file(
    tmp_dir,
    fmt
):

    ignored = {
        "cookies.txt"
    }

    candidates = []

    for root, dirs, files in os.walk(tmp_dir):

        for filename in files:

            if filename in ignored:
                continue

            if filename.endswith(".part"):
                continue

            if filename.endswith(".ytdl"):
                continue

            full_path = os.path.join(
                root,
                filename
            )

            if not os.path.isfile(full_path):
                continue

            try:
                size = os.path.getsize(
                    full_path
                )
            except OSError:
                continue

            if size <= 0:
                continue

            lower_name = filename.lower()

            if fmt == "audio":

                allowed = (
                    ".mp3",
                    ".m4a",
                    ".opus",
                    ".webm",
                    ".aac",
                    ".wav"
                )

            else:

                allowed = (
                    ".mp4",
                    ".mkv",
                    ".webm",
                    ".mov",
                    ".avi"
                )

            if lower_name.endswith(allowed):

                candidates.append(
                    (full_path, size)
                )

    if not candidates:

        for root, dirs, files in os.walk(tmp_dir):

            for filename in files:

                if filename in ignored:
                    continue

                if filename.endswith(".part"):
                    continue

                if filename.endswith(".ytdl"):
                    continue

                full_path = os.path.join(
                    root,
                    filename
                )

                if not os.path.isfile(full_path):
                    continue

                try:
                    size = os.path.getsize(
                        full_path
                    )
                except OSError:
                    continue

                if size > 0:

                    candidates.append(
                        (full_path, size)
                    )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[1],
        reverse=True
    )

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
    use_proxy=False
):

    ydl_opts = build_ytdlp_options(
        tmp_dir=tmp_dir,
        fmt=fmt,
        player_clients=player_clients,
        use_cookies=use_cookies,
        diagnostic=False,
        use_proxy=use_proxy
    )

    print(
        "Starting yt-dlp attempt:",
        {
            "format": fmt,
            "clients": player_clients,
            "cookies": use_cookies,
            "yt_dlp_version": YTDLP_VERSION
        }
    )

    with yt_dlp.YoutubeDL(
        ydl_opts
    ) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

    if not info:

        raise RuntimeError(
            "yt-dlp לא החזיר מידע על הסרטון"
        )

    title = info.get(
        "title",
        "download"
    )

    final_path = find_downloaded_file(
        tmp_dir,
        fmt
    )

    if not final_path:

        raise RuntimeError(
            "ההורדה הסתיימה אך הקובץ הסופי לא נמצא"
        )

    return (
        final_path,
        title
    )


# ============================================================
# DOWNLOAD ENGINE
# ============================================================

def download_from_youtube(
    url,
    fmt,
    tmp_dir,
    player_clients=None,
    use_cookies=True,
    use_proxy=False
):

    attempts = []

    requested_clients = normalize_player_clients(
        player_clients
    )

    if requested_clients:

        attempts.append(
            (
                requested_clients,
                bool(use_cookies)
            )
        )

    for combo in DEFAULT_CLIENT_ATTEMPTS:

        if combo not in attempts:

            attempts.append(combo)

    if fmt == "auto":

        formats_to_try = [
            "video",
            "audio"
        ]

    else:

        formats_to_try = [
            fmt
        ]

    last_error = None

    for target_fmt in formats_to_try:

        for clients, cookies_flag in attempts:

            safe_clients = "_".join(
                clients
            )

            sub_dir = os.path.join(
                tmp_dir,
                (
                    f"try_"
                    f"{target_fmt}_"
                    f"{safe_clients}_"
                    f"{int(cookies_flag)}"
                )
            )

            os.makedirs(
                sub_dir,
                exist_ok=True
            )

            try:

                result = _try_download_once(
                    url=url,
                    fmt=target_fmt,
                    tmp_dir=sub_dir,
                    player_clients=clients,
                    use_cookies=cookies_flag,
                    use_proxy=use_proxy
                )

                print(
                    "SUCCESSFUL yt-dlp attempt:",
                    {
                        "format": target_fmt,
                        "clients": clients,
                        "cookies": cookies_flag
                    }
                )

                return result

            except Exception as e:

                last_error = e

                print(
                    "Download attempt failed:",
                    {
                        "format": target_fmt,
                        "clients": clients,
                        "cookies": cookies_flag,
                        "error": str(e)
                    }
                )

                continue

    if last_error:

        raise RuntimeError(
            "כל ניסיונות ההורדה נכשלו. "
            f"yt-dlp האחרון החזיר: {last_error}"
        )

    raise RuntimeError(
        "כל ניסיונות ההורדה נכשלו"
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def health():

    return jsonify({

        "status": "ok",

        "yt_dlp_version":
            YTDLP_VERSION

    })


# ============================================================
# FORMATS DIAGNOSTIC
# ============================================================

@app.route(
    "/formats",
    methods=["POST"]
)
def list_formats():

    provided_key = request.headers.get(
        "X-API-KEY",
        ""
    )

    if (
        not API_KEY
        or provided_key != API_KEY
    ):

        return jsonify({
            "success": False,
            "error": "unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    url = data.get("url")

    player_clients = data.get(
        "player_client"
    )

    use_cookies = data.get(
        "use_cookies",
        True
    )

    use_proxy = data.get(
        "use_proxy",
        False
    )

    if not url:

        return jsonify({
            "success": False,
            "error": "missing 'url'"
        }), 400

    player_clients = normalize_player_clients(
        player_clients
    )

    if not player_clients:

        player_clients = [
            "web_embedded"
        ]

    diagnostic = {

        "yt_dlp_version":
            YTDLP_VERSION,

        "player_client":
            player_clients,

        "use_cookies":
            bool(use_cookies),

        "use_proxy":
            bool(use_proxy),

        "cookies_configured":
            bool(YOUTUBE_COOKIES),

        "pot_provider":
            "http://127.0.0.1:4416"

    }

    print(
        "FORMAT REQUEST:",
        diagnostic
    )

    try:

        with tempfile.TemporaryDirectory() as tmp_dir:

            ydl_opts = build_ytdlp_options(
                tmp_dir=tmp_dir,
                fmt="video",
                player_clients=player_clients,
                use_cookies=use_cookies,
                diagnostic=True,
                use_proxy=use_proxy
            )

            ydl_opts["skip_download"] = True

            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                info = ydl.extract_info(
                    url,
                    download=False,
                    process=False
                )

            if not info:

                raise RuntimeError(
                    "yt-dlp לא החזיר מידע"
                )

            formats = (
                info.get(
                    "formats",
                    []
                )
                or []
            )

            simplified = []

            for f in formats:

                simplified.append({

                    "format_id":
                        f.get("format_id"),

                    "ext":
                        f.get("ext"),

                    "acodec":
                        f.get("acodec"),

                    "vcodec":
                        f.get("vcodec"),

                    "abr":
                        f.get("abr"),

                    "tbr":
                        f.get("tbr"),

                    "height":
                        f.get("height"),

                    "width":
                        f.get("width"),

                    "fps":
                        f.get("fps"),

                    "note":
                        f.get("format_note"),

                    "has_url":
                        bool(
                            f.get("url")
                        ),

                    "protocol":
                        f.get("protocol"),

                    "filesize":
                        f.get("filesize"),

                    "filesize_approx":
                        f.get("filesize_approx"),

                })

            real_formats = [

                f
                for f in simplified

                if not str(
                    f["format_id"]
                ).startswith("sb")

            ]

            video_formats = [

                f
                for f in real_formats

                if (
                    f.get("vcodec")
                    and
                    f.get("vcodec") != "none"
                )

            ]

            audio_formats = [

                f
                for f in real_formats

                if (
                    f.get("acodec")
                    and
                    f.get("acodec") != "none"
                )

            ]

            combined_formats = [

                f
                for f in real_formats

                if (
                    f.get("vcodec")
                    and
                    f.get("vcodec") != "none"
                    and
                    f.get("acodec")
                    and
                    f.get("acodec") != "none"
                )

            ]

            return jsonify({

                "success": True,

                "diagnostic":
                    diagnostic,

                "video_info": {

                    "id":
                        info.get("id"),

                    "title":
                        info.get("title"),

                    "extractor":
                        info.get("extractor"),

                    "webpage_url":
                        info.get("webpage_url"),

                    "duration":
                        info.get("duration"),

                    "format_count":
                        len(formats)

                },

                "count":
                    len(simplified),

                "real_count":
                    len(real_formats),

                "video_format_count":
                    len(video_formats),

                "audio_format_count":
                    len(audio_formats),

                "combined_format_count":
                    len(combined_formats),

                "real_formats":
                    real_formats,

                "all_formats":
                    simplified

            })

    except Exception as e:

        traceback.print_exc()

        return jsonify({

            "success": False,

            "diagnostic":
                diagnostic,

            "error":
                str(e),

            "error_type":
                type(e).__name__

        }), 500


# ============================================================
# GOOGLE CHAT
# ============================================================

@app.route(
    "/chat",
    methods=["POST"]
)
def chat():

    event = request.get_json(
        silent=True
    ) or {}

    event_type = event.get(
        "type",
        ""
    )

    if event_type == "ADDED_TO_SPACE":

        return jsonify({

            "text":
                "שלום! שלחו לי קישור YouTube "
                "ואני אוריד אותו ואעלה ל-Drive 🎵"

        })

    if event_type != "MESSAGE":

        return jsonify({})

    message = event.get(
        "message",
        {}
    ) or {}

    message_text = (
        message.get(
            "text",
            ""
        )
        or ""
    )

    match = YOUTUBE_URL_RE.search(
        message_text
    )

    if not match:

        return jsonify({

            "text":
                "לא מצאתי קישור YouTube "
                "בהודעה. שלחו לי קישור תקין."

        })

    url = match.group(0)

    fmt = (
        "video"
        if "video" in message_text.lower()
        else "audio"
    )

    try:

        with tempfile.TemporaryDirectory() as tmp_dir:

            local_path, title = (
                download_from_youtube(

                    url=url,

                    fmt=fmt,

                    tmp_dir=tmp_dir,

                    player_clients=["android"],

                    use_cookies=False,

                    use_proxy=False

                )
            )

            drive_link, _ = upload_to_drive(

                local_path,

                os.path.basename(
                    local_path
                )

            )

            return jsonify({

                "text":
                    f"✅ הורד והועלה: "
                    f"{title}\n{drive_link}"

            })

    except Exception as e:

        traceback.print_exc()

        return jsonify({

            "text":
                f"❌ שגיאה בהורדה: {e}"

        })


# ============================================================
# DOWNLOAD API
# ============================================================

@app.route(
    "/download",
    methods=["POST"]
)
def download():

    provided_key = request.headers.get(
        "X-API-KEY",
        ""
    )

    if (
        not API_KEY
        or provided_key != API_KEY
    ):

        return jsonify({

            "success": False,

            "error":
                "unauthorized"

        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    url = data.get("url")

    fmt = data.get(
        "format",
        "audio"
    )

    player_clients = data.get(
        "player_client"
    )

    use_cookies = data.get(
        "use_cookies",
        True
    )

    use_proxy = data.get(
        "use_proxy",
        False
    )

    if not url:

        return jsonify({

            "success": False,

            "error":
                "missing 'url'"

        }), 400

    if fmt not in (
        "audio",
        "video",
        "auto"
    ):

        return jsonify({

            "success": False,

            "error":
                "format must be "
                "'audio', 'video' or 'auto'"

        }), 400

    try:

        with tempfile.TemporaryDirectory() as tmp_dir:

            local_path, title = (
                download_from_youtube(

                    url=url,

                    fmt=fmt,

                    tmp_dir=tmp_dir,

                    player_clients=player_clients,

                    use_cookies=use_cookies,

                    use_proxy=use_proxy

                )
            )

            drive_link, file_id = (
                upload_to_drive(

                    local_path,

                    os.path.basename(
                        local_path
                    )

                )
            )

            return jsonify({

                "success": True,

                "title":
                    title,

                "driveLink":
                    drive_link,

                "fileId":
                    file_id

            })

    except Exception as e:

        traceback.print_exc()

        return jsonify({

            "success": False,

            "error":
                str(e)

        }), 500


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    app.run(

        host="0.0.0.0",

        port=int(
            os.environ.get(
                "PORT",
                8080
            )
        )

    )
