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
# Environment variables
# ============================================================

API_KEY = os.environ.get("API_KEY", "")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")

GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file"
]

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+[^\s]*",
    re.IGNORECASE,
)


# ============================================================
# Google Drive
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

    creds.refresh(GoogleAuthRequest())

    return build(
        "drive",
        "v3",
        credentials=creds
    )


def upload_to_drive(local_path, filename):
    service = get_drive_service()

    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID] if DRIVE_FOLDER_ID else [],
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
# Cookies
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
# Download
# ============================================================

def _download_once(
    url,
    fmt,
    tmp_dir,
    player_clients,
    use_cookies
):
    """
    מבצע ניסיון הורדה אחד.

    החשוב:
    אין כאן בחירה של format_id ולאחר מכן ניסיון להוריד
    את אותו ID מחדש.

    yt-dlp מקבל selector כללי ובוחר מתוך הפורמטים
    הזמינים בפועל באותו ניסיון.
    """

    outtmpl = os.path.join(
        tmp_dir,
        "%(title)s.%(ext)s"
    )

    cookies_path = None

    if use_cookies:
        cookies_path = write_cookies_file(tmp_dir)

    extractor_args = {
        "youtube": {
            "player_client": player_clients
        }
    }

    common_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": extractor_args,
    }

    if cookies_path:
        common_opts["cookiefile"] = cookies_path

    # --------------------------------------------------------
    # AUDIO
    # --------------------------------------------------------

    if fmt == "audio":

        ydl_opts = {
            **common_opts,

            # אודיו זמין ראשון, ואם לא קיים - פורמט אחר
            "format": "ba/b",

            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

    # --------------------------------------------------------
    # VIDEO
    # --------------------------------------------------------

    else:

        ydl_opts = {
            **common_opts,

            # קודם וידאו+אודיו נפרדים.
            # אם זה לא אפשרי, progressive.
            "format": "bv*+ba/b",

            "merge_output_format": "mp4",
        }

    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

        title = info.get(
            "title",
            "download"
        )

    # --------------------------------------------------------
    # Find resulting file
    # --------------------------------------------------------

    expected_ext = (
        "mp3"
        if fmt == "audio"
        else "mp4"
    )

    expected_path = os.path.join(
        tmp_dir,
        f"{title}.{expected_ext}"
    )

    if os.path.exists(expected_path):
        return expected_path, title

    # לפעמים yt-dlp/FFmpeg משנה את שם הקובץ.
    files = []

    for filename in os.listdir(tmp_dir):

        if filename == "cookies.txt":
            continue

        full_path = os.path.join(
            tmp_dir,
            filename
        )

        if os.path.isfile(full_path):
            files.append(full_path)

    if not files:
        raise RuntimeError(
            "ההורדה הסתיימה אך הקובץ הסופי לא נמצא"
        )

    # הקובץ הגדול ביותר
    files.sort(
        key=lambda p: os.path.getsize(p),
        reverse=True
    )

    return files[0], title


# ============================================================
# Fallback combinations
# ============================================================

FALLBACK_CLIENT_COMBOS = [

    # Android
    (["android"], False),
    (["android"], True),

    # iOS
    (["ios"], False),
    (["ios"], True),

    # Android Music
    (["android_music"], False),
    (["android_music"], True),

    # TV / embedded
    (
        ["tv_simply", "web_embedded"],
        False
    ),

    (
        ["tv_simply", "web_embedded"],
        True
    ),

    # Web
    (["web"], False),
    (["web"], True),

    # Mobile web
    (["mweb"], False),
    (["mweb"], True),
]


def download_from_youtube(
    url,
    fmt,
    tmp_dir,
    player_clients=None,
    use_cookies=True
):
    """
    מנסה מספר clients ואפשרויות cookies.

    בכל ניסיון yt-dlp עצמו בוחר את הפורמט הזמין.
    אין שמירה של format_id בין ניסיון לניסיון.
    """

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

    # אם auto:
    # קודם וידאו, ואם הכל נכשל - אודיו
    if fmt == "auto":
        formats_to_try = [
            "video",
            "audio"
        ]
    else:
        formats_to_try = [fmt]

    last_error = None

    for target_fmt in formats_to_try:

        for clients, cookies_flag in attempts:

            try:

                client_name = "_".join(
                    clients
                )

                sub_dir = os.path.join(
                    tmp_dir,
                    f"try_{target_fmt}_{client_name}_{int(cookies_flag)}"
                )

                os.makedirs(
                    sub_dir,
                    exist_ok=True
                )

                return _download_once(
                    url=url,
                    fmt=target_fmt,
                    tmp_dir=sub_dir,
                    player_clients=clients,
                    use_cookies=cookies_flag
                )

            except Exception as e:

                last_error = e

                print(
                    f"Download attempt failed: "
                    f"format={target_fmt}, "
                    f"clients={clients}, "
                    f"cookies={cookies_flag}"
                )

                traceback.print_exc()

                continue

    if last_error:
        raise last_error

    raise RuntimeError(
        "ההורדה נכשלה ללא שגיאה מפורטת"
    )


# ============================================================
# Health check
# ============================================================

@app.route("/", methods=["GET"])
def health():

    return jsonify({
        "status": "ok"
    })


# ============================================================
# Formats diagnostic endpoint
# ============================================================

@app.route("/formats", methods=["POST"])
def list_formats():

    provided_key = request.headers.get(
        "X-API-KEY",
        ""
    )

    if not API_KEY or provided_key != API_KEY:

        return jsonify({
            "success": False,
            "error": "unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    url = data.get("url")

    player_clients = data.get(
        "player_client",
        ["tv_simply", "web_embedded"]
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

            cookies_path = None

            if use_cookies:
                cookies_path = write_cookies_file(
                    tmp_dir
                )

            extractor_args = {
                "youtube": {
                    "player_client": player_clients
                }
            }

            ydl_opts = {
                "quiet": True,
                "skip_download": True,
                "extractor_args": extractor_args,
            }

            if cookies_path:
                ydl_opts["cookiefile"] = cookies_path

            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                info = ydl.extract_info(
                    url,
                    download=False,
                    process=False
                )

                formats = (
                    info.get("formats", [])
                    or []
                )

                simplified = []

                for f in formats:

                    simplified.append({
                        "format_id": f.get(
                            "format_id"
                        ),
                        "ext": f.get(
                            "ext"
                        ),
                        "acodec": f.get(
                            "acodec"
                        ),
                        "vcodec": f.get(
                            "vcodec"
                        ),
                        "height": f.get(
                            "height"
                        ),
                        "fps": f.get(
                            "fps"
                        ),
                        "abr": f.get(
                            "abr"
                        ),
                        "tbr": f.get(
                            "tbr"
                        ),
                        "note": f.get(
                            "format_note"
                        ),
                        "has_url": bool(
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
                    "count": len(simplified),
                    "real_count": len(real_formats),
                    "real_formats": real_formats,
                    "all_formats": simplified,
                })

    except Exception as e:

        traceback.print_exc()

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# Google Chat
# ============================================================

@app.route("/chat", methods=["POST"])
def chat():

    event = request.get_json(
        silent=True
    ) or {}

    event_type = event.get(
        "type",
        ""
    )

    # Bot added to space
    if event_type == "ADDED_TO_SPACE":

        return jsonify({
            "text":
                "שלום! שלחו לי קישור YouTube "
                "ואני אוריד אותו ואעלה ל-Drive 🎵"
        })

    # Ignore other event types
    if event_type != "MESSAGE":

        return jsonify({})

    message_text = (
        event.get("message", {})
        or {}
    ).get(
        "text",
        ""
    ) or ""

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

    # אם כתוב video -> video
    # אחרת audio
    fmt = (
        "video"
        if "video" in message_text.lower()
        else "audio"
    )

    try:

        with tempfile.TemporaryDirectory() as tmp_dir:

            local_path, title = download_from_youtube(
                url=url,
                fmt=fmt,
                tmp_dir=tmp_dir,

                # מתחילים עם Android
                player_clients=[
                    "android"
                ],

                # כאן אנחנו מאפשרים fallback
                use_cookies=False
            )

            drive_link, _ = upload_to_drive(
                local_path,
                os.path.basename(local_path)
            )

            return jsonify({
                "text":
                    f"✅ הורד והועלה: "
                    f"{title}\n"
                    f"{drive_link}"
            })

    except Exception as e:

        traceback.print_exc()

        return jsonify({
            "text":
                f"❌ שגיאה בהורדה: {e}"
        })


# ============================================================
# Direct download API
# ============================================================

@app.route("/download", methods=["POST"])
def download():

    provided_key = request.headers.get(
        "X-API-KEY",
        ""
    )

    if not API_KEY or provided_key != API_KEY:

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
            "error": "missing 'url'"
        }), 400

    try:

        with tempfile.TemporaryDirectory() as tmp_dir:

            local_path, title = download_from_youtube(
                url=url,
                fmt=fmt,
                tmp_dir=tmp_dir,
                player_clients=player_clients,
                use_cookies=use_cookies
            )

            drive_link, file_id = upload_to_drive(
                local_path,
                os.path.basename(local_path)
            )

            return jsonify({
                "success": True,
                "title": title,
                "driveLink": drive_link,
                "fileId": file_id
            })

    except Exception as e:

        traceback.print_exc()

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# Start server
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
