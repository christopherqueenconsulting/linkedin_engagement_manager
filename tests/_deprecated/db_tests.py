import os
from datetime import datetime

from cqc_lem.utilities.db import PostStatus, update_db_post_content, update_db_post_status, update_db_post_video_url


def test_change_post_status():
    post_id = 19
    api_video_url = os.getenv("TEST_VIDEO_URL", "http://localhost:8000/assets?file_name=videos/runwayml/test.mp4")
    update_db_post_video_url(post_id, api_video_url)
    today_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    update_db_post_content(post_id, f"This is a test post. {today_date}")
    update_db_post_status(post_id, PostStatus.PENDING)


if __name__ == "__main__":
    test_change_post_status()
