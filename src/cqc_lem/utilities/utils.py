import functools
import json
import os
import random
from datetime import date, time
from enum import Enum
from urllib.parse import urlparse

import boto3
import requests
import tldextract

DEBUG_LEVEL = 3


def debug_function(func):
    global DEBUG_LEVEL

    @functools.wraps(func)
    def inner_function(*args, **kwargs):
        result = None
        if DEBUG_LEVEL >= 1:
            print(f"Before calling function {func.__name__}")
        if DEBUG_LEVEL != 2:
            result = func(*args, **kwargs)
        else:
            print(f"Calling function {func.__name__}")
        if DEBUG_LEVEL > 2:
            print(f"After calling function {func.__name__}")
        return result

    return inner_function


def create_folder_if_not_exists(folder_path):
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
        # myprint(f"Folder '{folder_path}' created.")
    # else:
    # myprint(f"Folder '{folder_path}' already exists.")


def are_you_satisfied():
    """Prompts the user to select if they are satisfied or not."""
    enum = Satisfactory

    print("Are you satisfied?")
    for i, member in enumerate(enum):
        print(f"{member.value}: {member.name}")

    default = Satisfactory.YES
    default_value = default.value
    user_input = int(input('Enter your selection [' + str(default_value) + ']: ').strip() or default_value)

    try:
        sf = Satisfactory(user_input)
        print(f"You selected {sf.name}")
        return sf.value == default_value
    except ValueError:
        print("Invalid selection.")
        return are_you_satisfied() == default_value


class Satisfactory(Enum):
    YES = 1
    NO = 0


def get_top_level_domain(url: str) -> str:
    extracted = tldextract.extract(url)
    return f"{extracted.domain}.{extracted.suffix}"


def get_best_posting_times():
    # 2026 peak-engagement windows shifted to afternoons, strongest Tue–Thu (Wed ~4pm is the peak);
    # weekends taper. Times are user-local (the scheduler converts to UTC). Superseded per-user by
    # the data-driven recommend_post_times once enough post-stats data exists (see get_post_time).
    # Operators can override the default model per-deploy with DEFAULT_POSTING_HOURS — seven
    # comma-separated 24h hours, Monday..Sunday (e.g. "16,15,16,17,11,10,18"). Invalid values fall
    # back to the built-in model, read at call time so tests/ops can tune without a restart.
    raw = (os.environ.get("DEFAULT_POSTING_HOURS") or "").strip()
    if raw:
        from cqc_lem.utilities.logger import log_warning  # local import (matches purge_post_assets)
        try:
            hours = [int(h.strip()) for h in raw.split(",")]
            if len(hours) == 7 and all(0 <= h <= 23 for h in hours):
                return {i: time(h, 0) for i, h in enumerate(hours)}
            log_warning(f"DEFAULT_POSTING_HOURS ignored (need 7 hours 0-23, got: {raw!r})")
        except ValueError:
            log_warning(f"DEFAULT_POSTING_HOURS ignored (not a comma list of ints: {raw!r})")

    best_times = {
        0: time(16, 0),  # Monday — 4pm
        1: time(15, 0),  # Tuesday — 3pm
        2: time(16, 0),  # Wednesday — 4pm (peak)
        3: time(17, 0),  # Thursday — 5pm
        4: time(11, 0),  # Friday — late morning (afternoon drop-off into the weekend)
        5: time(10, 0),  # Saturday — mid-morning
        6: time(18, 0),  # Sunday — early evening
    }

    return best_times


def get_best_posting_time(selected_date: date):
    best_times = get_best_posting_times()

    # Get the best time for the selected date
    best_time = best_times[selected_date.weekday()]

    return best_time


# Waking-hours guardrail (issue #621 / G6). A learned "best hour" drawn from thin post-stats data —
# or an operator override — can land at 03:00 or 05:00 LOCAL, which is exactly where user 1's plan
# was putting posts. Nobody's audience is awake then, and a machine-exact overnight slot repeated
# daily is one of the cadence-regularity signals LinkedIn's 2026 enforcement looks for. Every
# planned local time is clamped into this window before it is converted to UTC and stored.
POST_HOUR_MIN = 8
POST_HOUR_MAX = 20

# Per-post schedule jitter (issue #621 / G6): posts land 15-30 minutes either side of the target
# hour so a user's calendar never reads as a machine-exact daily time. The offset is always at
# least SCHEDULE_JITTER_MIN_MINUTES — a "jitter" that can come out as 0 defeats the point.
SCHEDULE_JITTER_MIN_MINUTES = 15
SCHEDULE_JITTER_MAX_MINUTES = 30


def clamp_to_waking_hours(post_time: time) -> time:
    """Pull a LOCAL posting time into the waking window [POST_HOUR_MIN:00, POST_HOUR_MAX:59]."""
    if post_time.hour < POST_HOUR_MIN:
        return time(POST_HOUR_MIN, post_time.minute)
    if post_time.hour > POST_HOUR_MAX:
        return time(POST_HOUR_MAX, post_time.minute)
    return post_time


def apply_schedule_jitter(post_time: time, rng: random.Random = None) -> time:
    """`post_time` moved 15-30 minutes earlier or later, kept inside the waking window. The target
    hour still anchors the slot — this only stops every post in a plan sharing one exact minute.
    """
    picker = rng or random
    offset = picker.randint(SCHEDULE_JITTER_MIN_MINUTES, SCHEDULE_JITTER_MAX_MINUTES)
    if picker.random() < 0.5:
        offset = -offset
    minutes = min(POST_HOUR_MAX * 60 + 59,
                  max(POST_HOUR_MIN * 60, post_time.hour * 60 + post_time.minute + offset))
    return time(minutes // 60, minutes % 60)


def get_post_time(selected_date: date, user_id: int = None):
    """Best posting time for a date, as the user's LOCAL wall clock — callers convert it to UTC for
    storage (docs/timezone-contract.md). Prefers the user's own data-driven best hour for that
    weekday (learned by recommend_post_times, Phase 3) and falls back to the 2026 default model
    until enough data exists. Keeps the default's minutes so times aren't all on the hour, and
    never returns an hour outside the waking window (issue #621).
    """
    default = clamp_to_waking_hours(get_best_posting_time(selected_date))
    if user_id is None:
        return default
    try:
        from cqc_lem.utilities.db import get_post_engagement_rows, get_user_timezone
        from cqc_lem.utilities.post_stats import recommend_post_times
        recs = recommend_post_times(get_post_engagement_rows(user_id), top_n=7,
                                    tz=get_user_timezone(user_id))
        for r in recs:
            if r["weekday_num"] == selected_date.weekday():
                return clamp_to_waking_hours(time(r["hour"], default.minute))
    except Exception as e:
        from cqc_lem.utilities.logger import log_debug
        log_debug("Could not compute recommended post time", exc=e, user_id=user_id)
    return default


def get_12h_format_best_time(best_time: time):
    # Format the best time to 12-hour format
    best_time_12hr = best_time.strftime("%I:%M %p")
    return best_time_12hr


def save_video_url_to_dir(video_url: str, dir_path):
    # Extract the original file name from the URL
    parsed_url = urlparse(video_url)
    file_name = os.path.basename(parsed_url.path)

    # Fetch the content from the URL
    response = requests.get(video_url)
    response.raise_for_status()  # Ensure we notice bad responses

    # Save the video to the directory with the original file name
    video_path = os.path.join(dir_path, file_name)
    with open(video_path, 'wb') as f:
        f.write(response.content)

    return video_path


def get_file_extension_from_filepath(file_path: str, remove_leading_dot: bool = False) -> str:
    basename = os.path.basename(file_path)
    file_name, file_extension = os.path.splitext(basename)
    if remove_leading_dot and file_extension.startswith("."):
        # st.info("Removing leading dot from file extension: " + file_extension)
        file_extension = file_extension[1:]

    if file_extension:
        file_extension = file_extension.lower()

    # st.info("Base Name: " + basename + " | File Name: " + file_name + " | File Extension : " + file_extension)

    return file_extension


def get_aws_session():
    return boto3.session.Session()


def get_aws_client(service_name: str, region_name: str):
    session = get_aws_session()
    return session.client(
        service_name=service_name,
        region_name=region_name
    )


def get_cloudwatch_client(region: str = 'us-east-1'):
    return get_aws_client('cloudwatch', region)


def get_aws_ssm_secret(secret_name, region_name):
    """Gets the secret value from AWS Secrets Manager"""
    client = get_aws_client(
        service_name='secretsmanager',
        region_name=region_name
    )

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except Exception as e:
        raise e

    secret = get_secret_value_response['SecretString']
    secret_dict = json.loads(secret)

    return secret_dict


def get_aws_device_farm_url(deviceFarmProjectArn: str, testGridProjectArn: str, expiresInSeconds: int = 600,
                            region_name: str = "us-west-2"):
    devicefarm_client = get_aws_client(service_name="devicefarm", region_name=region_name)
    response = devicefarm_client.create_test_grid_url(
        projectArn=f"arn:aws:devicefarm:{region_name}:{deviceFarmProjectArn}:testgrid-project:{testGridProjectArn}",

        expiresInSeconds=expiresInSeconds)
    return response["url"]


def purge_post_assets(post_id, video_url=None):
    """Delete a post's generated media from the assets volume after it is published.

    LinkedIn re-hosts media at publish time, so the local copy becomes dead
    weight. Removes the post's video file (resolved from its asset URL) and its
    carousel slide directory (images/carousel/<post_id>/). Path-safe (only deletes
    inside assets_dir) and tolerant of already-missing files. Returns removed paths.
    """
    import shutil
    from urllib.parse import parse_qs, urlparse

    from cqc_lem import assets_dir
    from cqc_lem.utilities.logger import myprint

    removed = []
    assets_real = os.path.realpath(assets_dir)

    def _within_assets(path):
        try:
            return os.path.commonpath([assets_real, os.path.realpath(path)]) == assets_real
        except ValueError:
            return False

    if video_url:
        try:
            file_name = (parse_qs(urlparse(video_url).query).get("file_name") or [None])[0]
        except Exception:
            file_name = None
        if file_name:
            parts = [p for p in file_name.replace("\\", "/").split("/") if p and p not in (".", "..")]
            candidate = os.path.join(assets_dir, *parts) if parts else None
            if candidate and _within_assets(candidate) and os.path.isfile(candidate):
                try:
                    os.remove(candidate)
                    removed.append(candidate)
                except OSError as e:
                    myprint(f"purge_post_assets: could not remove {candidate}: {e}")

    for media_dir in (os.path.join(assets_dir, "images", "carousel", str(post_id)),
                      os.path.join(assets_dir, "images", "posts", str(post_id))):
        if os.path.isdir(media_dir) and _within_assets(media_dir):
            try:
                shutil.rmtree(media_dir)
                removed.append(media_dir)
            except OSError as e:
                myprint(f"purge_post_assets: could not remove {media_dir}: {e}")

    if removed:
        myprint(f"purge_post_assets: post_id={post_id} removed {len(removed)} asset path(s)")
    return removed
