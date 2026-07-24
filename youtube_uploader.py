"""
youtube_uploader.py
Handles auth + upload for THIS channel only.

Uses its own client_secrets.json and token file, completely separate from
the Shorts channel's credentials -> separate quota pool, separate account.

First run will open a browser for you to log in with the SECOND Google
account (the one that owns the Lebanese Rap Archives channel) and authorize.
After that, token.pickle is reused automatically.
"""

import os
import pickle
import socket
from pathlib import Path

# large uploads on slow connections were hitting the default socket timeout
socket.setdefaulttimeout(600)

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

BASE_DIR = Path(__file__).parent
CLIENT_SECRETS_FILE = BASE_DIR / "client_secrets.json"   # download from Google Cloud Console for account #2
TOKEN_FILE = BASE_DIR / "token.pickle"


def get_authenticated_service():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRETS_FILE.exists():
                raise FileNotFoundError(
                    f"Missing {CLIENT_SECRETS_FILE}. Download OAuth client secrets "
                    f"for the SECOND Google account from Google Cloud Console "
                    f"(YouTube Data API v3 must be enabled on that project)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("youtube", "v3", credentials=creds)


def upload_video(file_path, title, description, tags=None, privacy_status="private",
                  category_id="10"):
    """
    category_id 10 = Music (fits this channel's content). Change if needed.
    privacy_status: "public" | "unlisted" | "private"  (default: private —
    videos land on the channel unpublished so you can review, then publish)
    Returns the uploaded video's YouTube ID.
    """
    youtube = get_authenticated_service()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        }
    }

    media = MediaFileUpload(str(file_path), chunksize=-1, resumable=True, mimetype="video/mp4")

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    print(f"[youtube_uploader] Uploading '{title}'...")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"[youtube_uploader] Upload progress: {int(status.progress() * 100)}%")

    video_id = response["id"]
    print(f"[youtube_uploader] Upload complete -> https://youtu.be/{video_id}")
    return video_id


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python youtube_uploader.py <video_path> <title>")
        sys.exit(1)
    upload_video(sys.argv[1], sys.argv[2], description="Test upload")
