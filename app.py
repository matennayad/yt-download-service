import os
import re
import json
import tempfile
import traceback

from flask import Flask, request, jsonify
import yt_dlp

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")

GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+[^\s]*",
    re.IGNORECASE,
)

DEFAULT_PLAYER_CLIENT = ["android"]


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
    return build("drive", "v3", credentials=creds)


def upload_to_drive(local_path, filename):
    service = get_drive_service()
    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID] if DRIVE_FOLDER_ID else [],
    }
    media = MediaFileUpload(local_path, resumable=True)
    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink"
    ).execute()
    return uploaded.get("webViewLink"), uploaded.get("id")


def write_cookies_file(tmp_dir):
    if not YOUTUBE_COOKIES:
        return None
    cookies_path = os.path.join(tmp_dir, "cookies.txt")
    with open(cookies_path, "w", encoding="utf-8") as f:
        f.write(YOUTUBE_COOKIES)
    return cookies_path


def _build_available_format(info, fmt):
    """
    בוחר פורמט מתוך הפורמטים שיוטיוב באמת החזיר.
    לא מסתמך על format קשיח שעלול לא להיות זמין בסרטון מסוים.
    """
    formats = [
        f for f in (info.get("formats") or [])
        if not str(f.get("format_id", "")).startswith("sb")
    ]

    if not formats:
        raise RuntimeError("YouTube לא החזיר פורמטים זמינים")

    if fmt == "audio":
        audio = [
            f for f in formats
            if f.get("acodec") not in (None, "none")
        ]
        if not audio:
            raise RuntimeError("לא נמצא פורמט אודיו זמין")

        # מעדיף bitrate גבוה, ובשוויון פורמט עם גודל ידוע.
        audio.sort(
            key=lambda f: (
                f.get("abr") or 0,
                f.get("tbr") or 0,
                f.get("filesize") or f.get("filesize_approx") or 0,
            ),
            reverse=True,
        )
        return str(audio[0]["format_id"])

    # קודם מחפשים progressive: וידאו + אודיו באותו פורמט.
    progressive = [
        f for f in formats
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]

    if progressive:
        progressive.sort(
            key=lambda f: (
                f.get("height") or 0,
                f.get("fps") or 0,
                f.get("tbr") or 0,
            ),
            reverse=True,
        )
        return str(progressive[0]["format_id"])

    video = [
        f for f in formats
        if f.get("vcodec") not in (None, "none")
    ]
    audio = [
        f for f in formats
        if f.get("acodec") not in (None, "none")
    ]

    if not video or not audio:
        raise RuntimeError("לא נמצאו גם וידאו וגם אודיו זמינים")

    video.sort(
        key=lambda f: (
            f.get("height") or 0,
            f.get("fps") or 0,
            f.get("tbr") or 0,
        ),
        reverse=True,
    )
    audio.sort(
        key=lambda f: (
            f.get("abr") or 0,
            f.get("tbr") or 0,
        ),
        reverse=True,
    )

    return f'{video[0]["format_id"]}+{audio[0]["format_id"]}'


def _try_download_once(url, fmt, tmp_dir, player_clients, use_cookies):
    outtmpl = os.path.join(tmp_dir, "%(title)s.%(ext)s")
    cookies_path = write_cookies_file(tmp_dir) if use_cookies else None
    extractor_args = {"youtube": {"player_client": player_clients}}

    common_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "extractor_args": extractor_args,
    }

    if cookies_path:
        common_opts["cookiefile"] = cookies_path

    # שלב 1: מוציאים את רשימת הפורמטים בפועל ובוחרים אחד מהם אוטומטית.
    with yt_dlp.YoutubeDL({
        **common_opts,
        "skip_download": True,
    }) as ydl:
        info = ydl.extract_info(url, download=False)
        selected_format = _build_available_format(info, fmt)

    # שלב 2: מורידים דווקא את הפורמט שנמצא בפועל.
    ydl_opts = {
        **common_opts,
        "format": selected_format,
    }

    if fmt == "audio":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "download")

    if fmt == "audio":
        final_path = os.path.join(tmp_dir, f"{title}.mp3")
    else:
        final_path = os.path.join(tmp_dir, f"{title}.mp4")

    if not os.path.exists(final_path):
        files = [
            f for f in os.listdir(tmp_dir)
            if f != "cookies.txt"
        ]
        if files:
            # מעדיף את הקובץ הגדול ביותר במקרה שהשם/סיומת השתנו.
            files.sort(
                key=lambda f: os.path.getsize(os.path.join(tmp_dir, f)),
                reverse=True,
            )
            final_path = os.path.join(tmp_dir, files[0])

    if not os.path.exists(final_path):
        raise RuntimeError("ההורדה הסתיימה אך הקובץ הסופי לא נמצא")

    return final_path, title


# רשימת נסיונות גיבוי: כל client מנוסה גם בלי וגם עם עוגיות; עוצרים ברגע שמשהו מצליח
FALLBACK_CLIENT_COMBOS = [
    (["android"], False),
    (["android"], True),
    (["ios"], False),
    (["ios"], True),
    (["android_music"], False),
    (["android_music"], True),
    (["tv_simply", "web_embedded"], False),
    (["tv_simply", "web_embedded"], True),
    (["web"], False),
    (["web"], True),
    (["mweb"], False),
    (["mweb"], True),
]


def download_from_youtube(url, fmt, tmp_dir, player_clients=None, use_cookies=True):
    """
    בודק אוטומטית את הפורמטים הזמינים בכל ניסיון, בוחר פורמט קיים, ואם זה נכשל מנסה אפשרויות גיבוי נוספות.
    אם fmt == "auto": מנסה קודם להשיג וידאו בכל השילובים, ורק אם אף אחד לא הצליח -
    עובר לנסות אודיו בכל השילובים.
    """
    attempts = []
    if player_clients is not None:
        attempts.append((player_clients, use_cookies))
    for combo in FALLBACK_CLIENT_COMBOS:
        if combo not in attempts:
            attempts.append(combo)

    formats_to_try = ["video", "audio"] if fmt == "auto" else [fmt]

    last_error = None
    for target_fmt in formats_to_try:
        for clients, cookies_flag in attempts:
            try:
                sub_dir = os.path.join(
                    tmp_dir, f"try_{target_fmt}_{'_'.join(clients)}_{int(cookies_flag)}"
                )
                os.makedirs(sub_dir, exist_ok=True)
                return _try_download_once(url, target_fmt, sub_dir, clients, cookies_flag)
            except Exception as e:
                last_error = e
                continue

    raise last_error


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/formats", methods=["POST"])
def list_formats():
    """נתיב אבחון בלבד: לא מוריד כלום, רק מציג אילו פורמטים יוטיוב מציע בפועל."""
    provided_key = request.headers.get("X-API-KEY", "")
    if not API_KEY or provided_key != API_KEY:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    player_clients = data.get("player_client", ["tv_simply", "web_embedded"])
    use_cookies = data.get("use_cookies", True)
    if not url:
        return jsonify({"success": False, "error": "missing 'url'"}), 400

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cookies_path = write_cookies_file(tmp_dir) if use_cookies else None
            extractor_args = {"youtube": {"player_client": player_clients}}
            ydl_opts = {
                "quiet": True,
                "skip_download": True,
                "extractor_args": extractor_args,
            }
            if cookies_path:
                ydl_opts["cookiefile"] = cookies_path

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
                formats = info.get("formats", []) or []
                simplified = [
                    {
                        "format_id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "acodec": f.get("acodec"),
                        "vcodec": f.get("vcodec"),
                        "abr": f.get("abr"),
                        "note": f.get("format_note"),
                        "has_url": bool(f.get("url")),
                    }
                    for f in formats
                ]
                real_formats = [f for f in simplified if not str(f["format_id"]).startswith("sb")]
                return jsonify({
                    "success": True,
                    "count": len(simplified),
                    "real_count": len(real_formats),
                    "real_formats": real_formats,
                    "all_formats": simplified,
                })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/chat", methods=["POST"])
def chat():
    """מקבל אירועים מ-Google Chat: הודעה עם קישור יוטיוב -> מוריד ומעלה ל-Drive -> עונה עם קישור."""
    event = request.get_json(silent=True) or {}
    event_type = event.get("type", "")

    if event_type == "ADDED_TO_SPACE":
        return jsonify({"text": "שלום! שלחו לי קישור YouTube ואני אוריד אותו ואעלה ל-Drive 🎵"})

    if event_type != "MESSAGE":
        return jsonify({})

    message_text = (event.get("message", {}) or {}).get("text", "") or ""
    match = YOUTUBE_URL_RE.search(message_text)
    if not match:
        return jsonify({"text": "לא מצאתי קישור YouTube בהודעה. שלחו לי קישור תקין."})

    url = match.group(0)
    fmt = "video" if "video" in message_text.lower() else "audio"

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path, title = download_from_youtube(
                url, fmt, tmp_dir,
                player_clients=DEFAULT_PLAYER_CLIENT,
                use_cookies=False,
            )
            drive_link, _ = upload_to_drive(local_path, os.path.basename(local_path))
            return jsonify({"text": f"✅ הורד והועלה: {title}\n{drive_link}"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"text": f"❌ שגיאה בהורדה: {e}"})


@app.route("/download", methods=["POST"])
def download():
    provided_key = request.headers.get("X-API-KEY", "")
    if not API_KEY or provided_key != API_KEY:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    fmt = data.get("format", "audio")  # אפשר גם "video" או "auto"
    player_clients = data.get("player_client")
    use_cookies = data.get("use_cookies", True)

    if not url:
        return jsonify({"success": False, "error": "missing 'url'"}), 400

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path, title = download_from_youtube(url, fmt, tmp_dir, player_clients, use_cookies)
            drive_link, file_id = upload_to_drive(local_path, os.path.basename(local_path))

            return jsonify({
                "success": True,
                "title": title,
                "driveLink": drive_link,
                "fileId": file_id
            })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
