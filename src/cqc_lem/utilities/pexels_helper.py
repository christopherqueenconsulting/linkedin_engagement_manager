"""Pexels stock media lookup — the LAST-RESORT visual, never the intended one.

Both halves are fallbacks: `download_pexels_video` is what `_generate_video_src` reaches for after a
Runway render fails (and after any premium credits are refunded), and `get_photo` backs
`carousel_creator.get_pexels_image_path`. Nothing here authors a brief or preserves an avatar
likeness — reaching this module already means the good path lost.

`api` is constructed at import from `PEXELS_API_KEY`, so this module is imported lazily inside the
functions that need it rather than at module top level in its callers.
"""
# Import API class from pexels_api package
import os
import random

import requests
from pexels_api import API
from pexels_api.tools import Photo

# Create API object
api = API(os.environ['PEXELS_API_KEY'])

_PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"


def search_videos(query: str, per_page: int = 10) -> list[dict]:
    """Search Pexels for stock videos matching query. Returns a list of video dicts."""
    pexels_key = os.environ.get("PEXELS_API_KEY")
    if not pexels_key:
        return []
    response = requests.get(
        _PEXELS_VIDEO_SEARCH_URL,
        headers={"Authorization": pexels_key},
        params={"query": query, "per_page": per_page, "orientation": "landscape"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("videos", [])


def get_video_file_url(video: dict, quality: str = "sd") -> str | None:
    """Return the download URL for the best-matching quality tier in a Pexels video dict."""
    files = video.get("video_files", [])
    for f in files:
        if f.get("quality") == quality and f.get("file_type") == "video/mp4":
            return f.get("link")
    # Fallback: any mp4
    for f in files:
        if f.get("file_type") == "video/mp4":
            return f.get("link")
    return None


def download_pexels_video(query: str, dest_dir: str) -> str | None:
    """Search Pexels for a video matching query and download the first result to dest_dir.
    Returns the local file path, or None if no video is found or download fails.
    """
    videos = search_videos(query)
    if not videos:
        return None
    video = random.choice(videos[:5])
    url = get_video_file_url(video)
    if not url:
        return None
    video_id = video.get("id", "pexels")
    dest_path = os.path.join(dest_dir, f"pexels_{video_id}.mp4")
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest_path


def get_photo(query: str) -> Photo:
    """One stock photo for `query`, chosen at RANDOM from the result page rather than top-ranked.

    Deliberate: carousel slides across many posts would otherwise all land on the same handful of
    over-used Pexels images. Raises `IndexError` when the search returns nothing, which
    `get_pexels_image_path` catches to fall back to its default path.
    """
    photos = get_photos(query)
    # Select 1 random photo from entries
    selected_photo = random.choice(photos)
    return selected_photo

def get_photos(query: str, num_of_photos: int = 25) -> list[Photo]:
    """First page of Pexels photo results for `query` — the pool `get_photo` samples from.

    Page size is what makes the random pick meaningful, so it is a parameter rather than a constant.
    Network and auth failures propagate from the SDK; callers own the fallback.
    """
    # Search for photos
    api.search(query, page=1, results_per_page=num_of_photos)
    # Get photo entries
    photos = api.get_entries()

    return photos

