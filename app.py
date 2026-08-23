import os
import json
import tempfile
import traceback

from flask import Flask, request, jsonify
import yt_dlp

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

# ---- Configuration (set these as environment variables on Render) ----
API_KEY = os.environ.get("API_KEY", "")  # shared secret so random people can't use your service
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")  # the Drive folder to upload into
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # full JSON key, as a string
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")  # contents of a cookies.txt file, as a string

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def get_drive_service():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
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
    """
    If YOUTUBE_COOKIES env var is set, write it out to a temp file
    and return its path. Otherwise return None.
    """
    if not YOUTUBE_COOKIES:
        return None
    cookies_path = os.path.join(tmp_dir, "cookies.txt")
    with open(cookies_path, "w", encoding="utf-8") as f:
        f.write(YOUTUBE_COOKIES)
    return cookies_path


def download_from_youtube(url, fmt, tmp_dir):
    """
    fmt: 'audio' or 'video'
    Returns (local_file_path, title)
    """
    outtmpl = os.path.join(tmp_dir, "%(title)s.%(ext)s")
    cookies_path = write_cookies_file(tmp_dir)

    extractor_args = {"youtube": {"player_client": ["default", "web_embedded"]}}

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
    else:  # video
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
        # Figure out the actual output filename yt-dlp produced
        if fmt == "audio":
            final_path = os.path.join(tmp_dir, f"{title}.mp3")
        else:
            final_path = os.path.join(tmp_dir, f"{title}.mp4")

        # Fallback: if the exact name doesn't match (special characters etc.),
        # just grab whatever single file is in the temp dir (excluding cookies.txt).
        if not os.path.exists(final_path):
            files = [f for f in os.listdir(tmp_dir) if f != "cookies.txt"]
            if files:
                final_path = os.path.join(tmp_dir, files[0])

        return final_path, title


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/download", methods=["POST"])
def download():
    # --- simple auth check ---
    provided_key = request.headers.get("X-API-KEY", "")
    if not API_KEY or provided_key != API_KEY:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    fmt = data.get("format", "audio")  # 'audio' or 'video'

    if not url:
        return jsonify({"success": False, "error": "missing 'url'"}), 400

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path, title = download_from_youtube(url, fmt, tmp_dir)
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
