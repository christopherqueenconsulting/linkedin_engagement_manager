"""Cross-cutting helpers: the default posting-time model, small file/URL utilities, AWS client factories.

The posting-time half is the load-bearing one. `get_post_time` is the entry point schedulers should
use — it is the only one that applies the per-user learned model AND the waking-hour clamp (issue
#621), so an hour that leaves it is never overnight. `get_best_posting_time` is the raw default
model underneath it and is NOT clamped.
"""

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

# (connect, read) seconds for outbound asset downloads. A generous read budget because render hosts
# hand back multi-MB files, but a bounded one: `requests` defaults to waiting forever, and no task in
# this tree sets a Celery time limit, so an unanswered socket parks a worker slot until the process
# is recycled.
DOWNLOAD_TIMEOUT = (5, 60)


def debug_function(func):
    """Trace a call against the module-level `DEBUG_LEVEL`, read at CALL time not at decoration time.

    That timing is what lets a test patch `utils.DEBUG_LEVEL` and change functions already decorated.

    `DEBUG_LEVEL == 2` is a dry run, not a louder one: the wrapped function is never invoked and the
    wrapper returns None. Any other level runs it normally; 0 runs it silently.
    """
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
    """Create `folder_path` and any missing parents, doing nothing if it is already there.

    Not atomic — the existence check and `os.makedirs` are separate calls, so two workers creating
    the same assets subtree at the same moment can still raise `FileExistsError`.
    """
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
    """The two answers `are_you_satisfied()` accepts; the values ARE the numbers typed at its prompt."""

    YES = 1
    NO = 0


def get_top_level_domain(url: str) -> str:
    """The registrable domain of `url` — subdomains dropped, multi-part public suffixes kept whole.

    `blog.linkedin.com` is `linkedin.com`, and `www.bbc.co.uk` is `bbc.co.uk`, never `co.uk`.
    `db.get_cookies` matches stored cookie domains with `LIKE %tld%`, so a suffix trimmed one label
    too far would match every unrelated site under it.
    """
    extracted = tldextract.extract(url)
    return f"{extracted.domain}.{extracted.suffix}"


def get_best_posting_times():
    """The default weekday -> local posting time model, keyed by `date.weekday()` (0 = Monday).

    Read fresh on every call so `DEFAULT_POSTING_HOURS` can be retuned without a restart; a malformed
    or short override is ignored (with a warning) rather than partially applied, because a half-read
    override would silently move some weekdays and not others.
    """
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
    """The default model's local time for `selected_date`'s weekday — the raw model only.

    No per-user learning and NO waking-hour clamp: schedulers want `get_post_time`, which layers
    both on top. Only the date's weekday is used, so the calendar date itself is irrelevant.
    """
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
    """`best_time` as a zero-padded 12-hour display string ("02:00 PM"), for UI copy only.

    Never parse this back — it carries no date, no timezone and no seconds.
    """
    # Format the best time to 12-hour format
    best_time_12hr = best_time.strftime("%I:%M %p")
    return best_time_12hr


def save_video_url_to_dir(video_url: str, dir_path):
    """Download a generated asset into `dir_path` under the URL path's own basename; returns that path.

    Despite the name this is the SHARED download helper — images go through it too (`image_gen`,
    `ai_helper.generate_post_image`), not just Runway video.

    `dir_path` must already exist; callers pair this with `create_folder_if_not_exists`. A non-2xx
    response raises rather than writing, and the body streams to a `.part` file that is renamed only
    once it is complete — so neither a failed render nor an interrupted download leaves a truncated
    file that later stages would treat as media. The query string is dropped, so two URLs whose paths
    end in the same file name overwrite each other in one directory.

    The timeout is not optional: a render host that accepts the connection and then stalls would
    otherwise hold a Celery slot forever, since no task in this tree sets a time limit.
    """
    # Extract the original file name from the URL
    parsed_url = urlparse(video_url)
    file_name = os.path.basename(parsed_url.path)

    video_path = os.path.join(dir_path, file_name)
    partial_path = video_path + '.part'

    # Fetch the content from the URL. Streamed rather than buffered because these assets run to tens
    # of MB and a Celery child holds them entirely in memory otherwise.
    with requests.get(video_url, timeout=DOWNLOAD_TIMEOUT, stream=True) as response:
        response.raise_for_status()  # Ensure we notice bad responses
        try:
            with open(partial_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
            os.replace(partial_path, video_path)
        except BaseException:
            # Never leave a half-written .part for the next run to mistake for media.
            if os.path.exists(partial_path):
                os.remove(partial_path)
            raise

    return video_path


def get_file_extension_from_filepath(file_path: str, remove_leading_dot: bool = False) -> str:
    """Lower-cased extension of `file_path`, INCLUDING the leading dot unless `remove_leading_dot`.

    Returns:
        `".mp4"` (or `"mp4"`), and `""` — never None — for a name carrying no extension, which
        callers concatenate or MIME-look-up directly.
    """
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
    """A fresh boto3 session carrying NO explicit credentials.

    They resolve from the environment or the instance role, so no AWS secret is ever hardcoded or
    threaded through this module.

    A new session per call, deliberately: boto3 sessions and their clients are not thread-safe, and
    these helpers are called from Celery workers.
    """
    return boto3.session.Session()


def get_aws_client(service_name: str, region_name: str):
    """The ONE place an AWS client is built, so credential resolution stays in `get_aws_session`."""
    session = get_aws_session()
    return session.client(
        service_name=service_name,
        region_name=region_name
    )


def get_cloudwatch_client(region: str = 'us-east-1'):
    """CloudWatch client for the queue-depth metrics `my_celery` publishes and reads.

    The `us-east-1` default is a fallback for callers that have no region configured — `my_celery`
    always passes `AWS_REGION` explicitly, and metrics written to the wrong region are invisible
    rather than an error.
    """
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
    """A short-lived signed WebDriver endpoint for an AWS Device Farm test grid.

    Both arguments are ARN *segments*, not ARNs: `deviceFarmProjectArn` fills the account-id slot and
    `testGridProjectArn` the project id, and the full `arn:aws:devicefarm:...:testgrid-project:...`
    is assembled here. The URL expires after `expiresInSeconds` (10 minutes by default), so it is
    fetched per session by `get_docker_driver` and must never be cached across runs.
    """
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

    Two sidecars deliberately survive it: the caption `.srt` (issue #1278) and the deck render
    receipt (issue #1513) — both are read AFTER the post publishes, which is exactly when this runs.
    """
    import shutil
    from urllib.parse import parse_qs, urlparse

    from cqc_lem import assets_dir
    from cqc_lem.utilities.deck_render import deck_render_receipt_path
    from cqc_lem.utilities.logger import log_info, log_warning

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
                    # WARNING: an asset we cannot delete is one that keeps accumulating on the
                    # volume. A one-off is a warning; a permission fault repeats forever, and that
                    # is the class of bug an escalated issue is meant to catch.
                    log_warning("purge_post_assets: could not remove asset file", exc=e,
                                post_id=post_id)
            # The caption sidecar (issue #1278) shares this video's stem and deliberately SURVIVES
            # the purge. LinkedIn re-hosts the video, which is why the local copy is dead weight —
            # but it never receives the `.srt`, so attaching captions on LinkedIn by hand is a
            # post-publish action and the file has to still be there when the author takes it.
            # `posts.caption_srt_url` points at it, and that URL must not resolve to a 404. It is a
            # few hundred bytes against the megabytes this function exists to reclaim.

    carousel_dir = os.path.join(assets_dir, "images", "carousel", str(post_id))
    for media_dir in (carousel_dir, os.path.join(assets_dir, "images", "posts", str(post_id))):
        if os.path.isdir(media_dir) and _within_assets(media_dir):
            try:
                # The render receipt (issue #1513) SURVIVES the purge, for the same reason the
                # caption sidecar does: this runs the moment the deck publishes, and the nightly
                # quality beat only scores posts that already shipped — purge the receipt here and
                # every deck reads as unmeasured forever. A few hundred bytes against the megabytes
                # of slides this reclaims.
                receipt = deck_render_receipt_path(media_dir)
                if os.path.isfile(receipt):
                    for entry in os.listdir(media_dir):
                        target = os.path.join(media_dir, entry)
                        if target == receipt:
                            continue
                        if os.path.isdir(target):
                            shutil.rmtree(target)
                        else:
                            os.remove(target)
                else:
                    shutil.rmtree(media_dir)
                removed.append(media_dir)
            except OSError as e:
                # Same condition, the directory half.
                log_warning("purge_post_assets: could not remove media directory", exc=e,
                            post_id=post_id)

    if removed:
        log_info(f"purge_post_assets: post_id={post_id} removed {len(removed)} asset path(s)")
    return removed
