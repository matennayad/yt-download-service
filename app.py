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


def download_from_youtube(url, fmt, tmp_dir, player_clients=None, use_cookies=True):
    outtmpl = os.path.join(tmp_dir, "%(title)s.%(ext)s")
    cookies_path = write_cookies_file(tmp_dir) if use_cookies else None

    if player_clients is None:
        player_clients = ["tv_simply", "web_embedded"]
    extractor_args = {"youtube": {"player_client": player_clients}}

    if fmt == "audio":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "noplaylist": True,
            "quiet": True,
            "extractor_args": extractor_args,
        }
    else:
        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "extractor_args": extractor_args,
        }

    if cookies_path:
        ydl_opts["cookiefile"] = cookies_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "download")
        if fmt == "audio":
            final_path = os.path.join(tmp_dir, f"{title}.mp3")
        else:
            final_path = os.path.join(tmp_dir, f"{title}.mp4")

        if not os.path.exists(final_path):
            files = [f for f in os.listdir(tmp_dir) if f != "cookies.txt"]
            if files:
                final_path = os.path.join(tmp_dir, files[0])

        return final_path, title


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
    fmt = data.get("format", "audio")
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
