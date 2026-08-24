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


def upload_to_drive(
    local_path,
    filename
):

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
# YT-DLP OPTIONS
# ============================================================

def build_ytdlp_options(
    tmp_dir,
    fmt,
    player_clients,
    use_cookies
):

    outtmpl = os.path.join(
        tmp_dir,
        "%(title)s.%(ext)s"
    )

    extractor_args = {
        "youtube": {
            "player_client": player_clients
        }
    }

    options = {
        "outtmpl": outtmpl,

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "extractor_args": extractor_args,

        # ====================================================
        # IMPORTANT
        #
        # We deliberately avoid forcing a specific format ID.
        # yt-dlp chooses a format that is actually available.
        # ====================================================

        "format": (
            # Audio:
            # Prefer audio-only, but allow a normal format
            # if audio-only is unavailable.
            "bestaudio[acodec!=none]/best[acodec!=none]/best"
            if fmt == "audio"

            else

            # Video:
            # Prefer combined video+audio.
            # If unavailable, use separate video/audio.
            # If that is also unavailable, use best.
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo+bestaudio/"
            "best[ext=mp4]/"
            "best"
        ),

        "ignoreerrors": False,

        "continuedl": True,

        "nopart": False,

        # Try to use the available URLs instead of requiring
        # a previously selected format ID.
        "check_formats": "selected",

        # Prevent playlist downloads.
        "noplaylist": True,
    }

    if use_cookies:

        cookies_path = write_cookies_file(
            tmp_dir
        )

        if cookies_path:
            options["cookiefile"] = cookies_path

    # ========================================================
    # AUDIO
    # ========================================================

    if fmt == "audio":

        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    # ========================================================
    # VIDEO
    # ========================================================

    else:

        options["merge_output_format"] = "mp4"

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

                if lower_name.endswith(
                    (
                        ".mp3",
                        ".m4a",
                        ".opus",
                        ".webm",
                        ".aac",
                        ".wav"
                    )
                ):
                    candidates.append(
                        (full_path, size)
                    )

            else:

                if lower_name.endswith(
                    (
                        ".mp4",
                        ".mkv",
                        ".webm",
                        ".mov",
                        ".avi"
                    )
                ):
                    candidates.append(
                        (full_path, size)
                    )

    # ========================================================
    # FALLBACK
    # ========================================================

    if not candidates:

        for root, dirs, files in os.walk(
            tmp_dir
        ):

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

                if not os.path.isfile(
                    full_path
                ):
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
    use_cookies
):

    ydl_opts = build_ytdlp_options(
        tmp_dir=tmp_dir,
        fmt=fmt,
        player_clients=player_clients,
        use_cookies=use_cookies
    )

    print(
        "Starting yt-dlp attempt:",
        {
            "format": fmt,
            "clients": player_clients,
            "cookies": use_cookies
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
# FALLBACK CLIENTS
# ============================================================

FALLBACK_CLIENT_COMBOS = [

    (["android"], False),
    (["android"], True),

    (["ios"], False),
    (["ios"], True),

    (["android_music"], False),
    (["android_music"], True),

    (["web_embedded"], False),
    (["web_embedded"], True),

    (["tv_simply"], False),
    (["tv_simply"], True),

    (["web"], False),
    (["web"], True),

    (["mweb"], False),
    (["mweb"], True),
]


# ============================================================
# MAIN DOWNLOAD FUNCTION
# ============================================================

def download_from_youtube(
    url,
    fmt,
    tmp_dir,
    player_clients=None,
    use_cookies=True
):

    attempts = []

    if player_clients is not None:

        attempts.append(
            (
                player_clients,
                use_cookies
            )
        )

    for combo in FALLBACK_CLIENT_COMBOS:

        if combo not in attempts:
            attempts.append(combo)

    # ========================================================
    # AUTO
    # ========================================================

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

                return _try_download_once(
                    url=url,
                    fmt=target_fmt,
                    tmp_dir=sub_dir,
                    player_clients=clients,
                    use_cookies=cookies_flag
                )

            except Exception as e:

                last_error = e

                print(
                    "Download attempt failed:",
                    target_fmt,
                    clients,
                    cookies_flag,
                    str(e)
                )

                continue

    if last_error:

        raise last_error

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
        "status": "ok"
    })


# ============================================================
# FORMATS - DIAGNOSTIC
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

    url = data.get(
        "url"
    )

    player_clients = data.get(
        "player_client",
        ["android"]
    )

    use_cookies = data.get(
        "use_cookies",
        True
    )

    if not url:

        return jsonify({
            "success": False,
            "error": "missing 'url'"
        }), 400

    try:

        with tempfile.TemporaryDirectory() as tmp_dir:

            cookies_path = (
                write_cookies_file(tmp_dir)
                if use_cookies
                else None
            )

            extractor_args = {
                "youtube": {
                    "player_client": player_clients
                }
            }

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extractor_args": extractor_args,
            }

            if cookies_path:

                ydl_opts["cookiefile"] = (
                    cookies_path
                )

            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                info = ydl.extract_info(
                    url,
                    download=False,
                    process=False
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

                        "fps":
                            f.get("fps"),

                        "note":
                            f.get("format_note"),

                        "has_url":
                            bool(
                                f.get("url")
                            ),
                    })

                real_formats = [

                    f
                    for f in simplified

                    if not str(
                        f["format_id"]
                    ).startswith("sb")
                ]

                return jsonify({

                    "success": True,

                    "count":
                        len(simplified),

                    "real_count":
                        len(real_formats),

                    "real_formats":
                        real_formats,

                    "all_formats":
                        simplified,
                })

    except Exception as e:

        traceback.print_exc()

        return jsonify({

            "success": False,

            "error":
                str(e)

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

                    player_clients=None,

                    use_cookies=True
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

    url = data.get(
        "url"
    )

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

                    use_cookies=use_cookies
                )
            )

            # =================================================
            # UPLOAD TO GOOGLE DRIVE
            # =================================================

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

                "title": title,

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
