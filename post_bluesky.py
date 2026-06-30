import csv
import json
import os
import random
import socket
import time
import urllib.request
import uuid
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.auth.transport.requests import Request
from atproto import Client
from atproto_client.utils import TextBuilder

# Unique tag for this process. Used to "claim" a video file in Drive before
# downloading/posting it, so that two workflow runs (e.g. two GitHub
# accounts/workflows pointed at the same Drive folder) can't both grab the
# same file at the same time. GITHUB_RUN_ID is stable for the life of one
# workflow run, which is exactly the scope we want a claim to last for.
RUN_TAG = os.getenv("GITHUB_RUN_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
CLAIM_PREFIX = "CLAIMED_"


def get_env(name, required=True):
    """Read an env var and strip surrounding whitespace/newlines.

    GitHub Actions secrets occasionally end up with a trailing newline if
    they were copy/pasted from a file or terminal. That trailing \n gets
    silently included in API calls (e.g. Drive folder IDs), causing
    confusing 404s like "File not found: ." since the ID no longer matches
    anything. Stripping here makes the script robust to that.
    """
    value = os.getenv(name)
    if value is None:
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return ""
    return value.strip()


def get_float_env(name, default):
    """Read a float env var (e.g. a ratio/percentage knob), falling back to default.

    Accepts either a plain fraction ("0.6") or a percentage ("60" / "60%") so
    the GitHub Actions workflow input field is forgiving about format.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    raw = raw.strip().rstrip("%")
    try:
        value = float(raw)
    except ValueError:
        print(f"Warning: could not parse {name}='{raw}' as a number; using default {default}.")
        return default
    # If it looks like a percentage (>1), normalize to a 0..1 fraction.
    if value > 1:
        value = value / 100.0
    return value


def get_bool_env(name, default=True):
    """Read a boolean env var ('true'/'false', '1'/'0', 'yes'/'on'), case-insensitive."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── Content mix knobs ────────────────────────────────────────────────────
# What fraction of posts should be images vs. videos. Configurable via the
# IMAGE_RATIO / VIDEO_RATIO env vars (plain fraction like "0.6" or a
# percentage like "60"/"60%"). Whatever values are given get auto-normalized
# to sum to 1.0, so you don't have to do the math by hand when tweaking the
# workflow_dispatch inputs — e.g. IMAGE_RATIO=70, VIDEO_RATIO=30 just works.
_raw_image_ratio = get_float_env("IMAGE_RATIO", 0.60)
_raw_video_ratio = get_float_env("VIDEO_RATIO", 0.40)
_ratio_sum = _raw_image_ratio + _raw_video_ratio
if _ratio_sum <= 0:
    IMAGE_RATIO, VIDEO_RATIO = 0.60, 0.40
else:
    IMAGE_RATIO = _raw_image_ratio / _ratio_sum
    VIDEO_RATIO = _raw_video_ratio / _ratio_sum

# Of ALL posts (image or video), what fraction should skip the caption/CTA
# /link block entirely and just be raw media + hashtags. Configurable via
# NO_LINK_RATIO (plain fraction or percentage, e.g. "20" or "0.2").
NO_LINK_RATIO = get_float_env("NO_LINK_RATIO", 0.20)

# Whether to attach hashtags at all, broken out per media kind so e.g. video
# posts can carry hashtags while image posts don't (or vice versa).
HASHTAGS_ENABLED_IMAGE = get_bool_env("HASHTAGS_ENABLED_IMAGE", True)
HASHTAGS_ENABLED_VIDEO = get_bool_env("HASHTAGS_ENABLED_VIDEO", True)


def print_config_summary():
    """Log the active content-mix knobs at startup so a glance at the job
    log confirms what this run is actually configured to do."""
    print("── Content mix config ──────────────────────────")
    print(f"  Image ratio:  {IMAGE_RATIO:.0%}")
    print(f"  Video ratio:  {VIDEO_RATIO:.0%}")
    print(f"  No-link rate: {NO_LINK_RATIO:.0%}")
    print(f"  Hashtags on image posts: {HASHTAGS_ENABLED_IMAGE}")
    print(f"  Hashtags on video posts: {HASHTAGS_ENABLED_VIDEO}")
    print("─────────────────────────────────────────────────")


def get_creds():
    """
    Build Google OAuth credentials from the GOOGLE_OAUTH_CREDENTIALS env var
    instead of a token.pickle file on disk. The env var holds the standard
    "authorized_user" JSON blob (token, refresh_token, client_id,
    client_secret, scopes, ...) — the same shape you'd get from
    google-auth's own credential storage, just passed in as a secret rather
    than committed to the repo.

    Refreshes the access token if it's expired (same as token.pickle did).
    """
    from google.oauth2.credentials import Credentials

    raw = get_env("GOOGLE_OAUTH_CREDENTIALS")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "GOOGLE_OAUTH_CREDENTIALS is not valid JSON. Make sure the whole "
            "JSON blob (including the { } braces) was pasted into the secret."
        ) from e

    creds = Credentials.from_authorized_user_info(info)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def _try_ip_lookup(url, parse_fn, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return parse_fn(data)


def print_runner_ip_and_location():
    """
    Print the GitHub Actions runner's public IP and rough geolocation.

    Purely informational — this helps confirm (e.g. when something looks
    off, or you're cross-checking against Bluesky's own login-activity
    page) which network/region a given run actually executed from.

    GitHub-hosted runner IPs get hit by every workflow on the planet, so a
    single free geo-IP service can start 403/429-ing them during busy
    periods (this is what was happening with ip-api.com). We try a small
    chain of independent services and use whichever answers first; if all
    of them fail we log a warning and keep going rather than fail the whole
    job over a "nice to know" detail.
    """
    providers = [
        ("ipinfo.io", "https://ipinfo.io/json",
         lambda d: (d.get("ip", "unknown"),
                    f"{d.get('city', 'unknown')}, {d.get('region', 'unknown')}, {d.get('country', 'unknown')} (org: {d.get('org', 'unknown')})")),
        ("ifconfig.co", "https://ifconfig.co/json",
         lambda d: (d.get("ip", "unknown"),
                    f"{d.get('city', 'unknown')}, {d.get('region_name', 'unknown')}, {d.get('country', 'unknown')} (ASN: {d.get('asn_org', 'unknown')})")),
        ("ip-api.com", "https://ip-api.com/json/",
         lambda d: (d.get("query", "unknown"),
                    f"{d.get('city', 'unknown')}, {d.get('regionName', 'unknown')}, {d.get('country', 'unknown')} (ISP: {d.get('isp', 'unknown')})")),
    ]
    for name, url, parse_fn in providers:
        try:
            ip, location = _try_ip_lookup(url, parse_fn)
            print(f"Runner public IP: {ip}  [via {name}]")
            print(f"Runner location: {location}")
            return
        except Exception as e:
            print(f"Note: IP lookup via {name} failed ({e}); trying next provider...")
    print("Warning: could not determine runner IP/location from any provider.")


def print_target_account(handle):
    """
    Print which Bluesky handle this run is about to post as, in @handle form
    so it's unambiguous at a glance (e.g. "@myaccount.bsky.social") which
    account is in play. Printed before login so it's the first thing visible
    in the job log — useful when multiple workflows/accounts share this same
    script. We only ever print the handle, never the app password — GitHub
    auto-redacts secret values it recognizes, but we don't print it at all
    on principle.
    """
    display_handle = handle if handle.startswith("@") else f"@{handle}"
    print(f"Target Bluesky account: {display_handle}")
    print(f"  (app password loaded: {'yes' if get_env('BSKY_APP_PW', required=False) else 'NO — missing!'}, value not printed for security)")


# ── Daily follower report (Google Sheet) ────────────────────────────────
# One spreadsheet tracks every account's follower growth, one row per
# account per calendar day (UTC). Re-runs on the same day for the same
# handle are detected and skipped, so it doesn't matter how many times the
# workflow restarts within a day — only the first run of the day for each
# handle writes a row.
FOLLOWER_SHEET_ID = "1d1ua2bzBt94omZxYgfwZhSJ94PJwAzc6clWpSVumebw"
FOLLOWER_SHEET_TAB = "Sheet1"
FOLLOWER_SHEET_RANGE = f"{FOLLOWER_SHEET_TAB}!A:E"
FOLLOWER_SHEET_HEADER = ["Date (UTC)", "Handle", "Previous Followers", "Followers Added", "Total Followers"]


def get_sheets_service():
    """Reuse the same OAuth creds (from GOOGLE_OAUTH_CREDENTIALS) already used
    for Drive — no separate Google credential needed, as long as the token's
    scopes cover Sheets. If you get a 403 here, the credential needs to be
    re-authorized with Sheets access."""
    creds = get_creds()
    return build("sheets", "v4", credentials=creds)


def ensure_follower_sheet_header(service):
    """Write the header row if the sheet/tab is currently empty."""
    result = service.spreadsheets().values().get(
        spreadsheetId=FOLLOWER_SHEET_ID, range=f"{FOLLOWER_SHEET_TAB}!A1:E1"
    ).execute()
    if not result.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=FOLLOWER_SHEET_ID,
            range=f"{FOLLOWER_SHEET_TAB}!A1:E1",
            valueInputOption="RAW",
            body={"values": [FOLLOWER_SHEET_HEADER]},
        ).execute()
        print("Initialized follower-report sheet header.")


def get_last_follower_row(service, handle):
    """Return the most recent [date, handle, previous, added, total] row for
    this handle, or None if the handle has never been logged before."""
    result = service.spreadsheets().values().get(
        spreadsheetId=FOLLOWER_SHEET_ID, range=FOLLOWER_SHEET_RANGE
    ).execute()
    rows = result.get("values", [])
    last_row = None
    for row in rows[1:]:  # skip header
        if len(row) >= 5 and row[1] == handle:
            last_row = row
    return last_row


def append_follower_row(service, date_str, handle, previous, added, total):
    service.spreadsheets().values().append(
        spreadsheetId=FOLLOWER_SHEET_ID,
        range=FOLLOWER_SHEET_RANGE,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[date_str, handle, previous, added, total]]},
    ).execute()


def maybe_generate_daily_follower_report(client, handle):
    """
    Once per UTC calendar day per handle: look up the account's current
    follower count via the already-logged-in Bluesky client, compare it to
    the last total we logged for this handle, and append one summary row to
    the shared Google Sheet (previous total, followers gained, new total).

    Safe to call on every cycle/run — it checks the sheet first and no-ops
    if today's row for this handle already exists, so multiple workflow
    restarts within the same day only ever produce one row per handle.
    Designed to support multiple accounts/handles all appending to the same
    sheet over time.
    """
    today_str = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        service = get_sheets_service()
        ensure_follower_sheet_header(service)

        last_row = get_last_follower_row(service, handle)
        if last_row and last_row[0] == today_str:
            print(f"Follower report for {handle} already logged today ({today_str}); skipping.")
            return

        profile = client.get_profile(actor=handle)
        total_followers = profile.followers_count or 0
        previous_total = int(last_row[4]) if last_row else total_followers
        added = total_followers - previous_total

        append_follower_row(service, today_str, handle, previous_total, added, total_followers)
        print(
            f"Logged daily follower report for {handle}: "
            f"previous={previous_total}, added={added:+d}, total={total_followers}"
        )
    except Exception as e:
        print(f"Warning: could not generate/append daily follower report: {e}")


def load_hashtag_sets(filepath="hashtags.txt"):
    """Return a list of hashtag sets (one per non-empty line)."""
    sets = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                sets.append(line)
    return sets


def pick_random_hashtags(filepath="hashtags.txt"):
    """Pick one random hashtag set; return list of tags without the # prefix."""
    hashtag_sets = load_hashtag_sets(filepath)
    if not hashtag_sets:
        return []
    chosen_line = random.choice(hashtag_sets)
    return [word.lstrip("#") for word in chosen_line.split() if word.startswith("#")]


# ── Captions (Google Sheet) ──────────────────────────────────────────────
# Captions + their matching CTA text now live in a Google Sheet instead of
# recipes_captions.csv, so they can be edited without touching the repo.
# Expected columns (header row, any order): "captions" and either
# "link_action_caption" or the "lik_action_caption" typo variant — same
# tolerant header matching the old CSV reader used.
CAPTIONS_SHEET_ID = "1dkzjf2wX6AYyf5XH1w9mzdvOVcYy_X2boF1L8znHwME"
CAPTIONS_SHEET_TAB = "Sheet1"
CAPTIONS_SHEET_RANGE = f"{CAPTIONS_SHEET_TAB}!A:Z"

_caption_rows_cache = None  # populated once per process run, then reused


def load_caption_rows():
    """Return a list of (caption, link_action_caption) tuples from the
    captions Google Sheet. Cached after the first call so a long-running
    loop (many post cycles per workflow run) doesn't re-fetch the sheet
    every time a caption is needed."""
    global _caption_rows_cache
    if _caption_rows_cache is not None:
        return _caption_rows_cache

    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=CAPTIONS_SHEET_ID, range=CAPTIONS_SHEET_RANGE
    ).execute()
    values = result.get("values", [])
    if not values:
        print("Warning: captions sheet is empty.")
        _caption_rows_cache = []
        return _caption_rows_cache

    header = [h.strip().lower() for h in values[0]]

    def col_index(*names):
        for name in names:
            if name in header:
                return header.index(name)
        return None

    caption_idx = col_index("captions", "caption")
    cta_idx = col_index("link_action_caption", "lik_action_caption")

    if caption_idx is None or cta_idx is None:
        print(
            f"Warning: captions sheet header {header} is missing a 'captions' "
            "and/or 'link_action_caption' column."
        )
        _caption_rows_cache = []
        return _caption_rows_cache

    rows = []
    for row in values[1:]:
        caption = row[caption_idx].strip() if len(row) > caption_idx else ""
        cta = row[cta_idx].strip() if len(row) > cta_idx else ""
        if caption and cta:
            rows.append((caption, cta))

    print(f"Loaded {len(rows)} caption/CTA pairs from captions sheet.")
    _caption_rows_cache = rows
    return _caption_rows_cache


def pick_random_caption_and_cta():
    """Pick one random (caption, cta) pair; return ('', '') if none found."""
    rows = load_caption_rows()
    if not rows:
        return "", ""
    return random.choice(rows)


def pick_random_caption_only():
    """Pick just a random caption (no CTA), for no-link posts.

    No-link posts still want *something* describing the media, but the CTA
    line only makes sense paired with the link below it, so we drop the CTA
    and reuse the caption half of the same sheet row.
    """
    rows = load_caption_rows()
    if not rows:
        return ""
    caption, _cta = random.choice(rows)
    return caption


def claim_file(service, file_id, current_name):
    """
    Try to "claim" a Drive file by renaming it with this run's unique tag.

    Drive has no real locking primitive, so we approximate one: rename is a
    single atomic write, and we immediately re-fetch the file to confirm our
    rename is still in effect. If a different concurrent run (e.g. a second
    GitHub Actions workflow polling the same folder) renamed the file in the
    moment between our list() and our update(), the re-fetch will show their
    tag instead of ours and we back off rather than risk a double-post.

    Returns the new (claimed) filename on success, or None if we lost the race.
    """
    claimed_name = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    service.files().update(
        fileId=file_id,
        body={"name": claimed_name},
    ).execute()

    # Re-fetch to make sure nobody else won/overwrote the claim in between.
    check = service.files().get(fileId=file_id, fields="id, name").execute()
    if check.get("name") != claimed_name:
        print(f"Lost claim race on file {file_id} (now named '{check.get('name')}'); skipping.")
        return None
    return claimed_name


def choose_media_kind():
    """Randomly choose 'image' or 'video' for this run, per IMAGE_RATIO/VIDEO_RATIO."""
    return random.choices(
        population=["image", "video"],
        weights=[IMAGE_RATIO, VIDEO_RATIO],
        k=1,
    )[0]


def _download_file(service, file_id, local_path):
    """Download a Drive file's bytes to local_path (handles large files via chunked download)."""
    request = service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()


def fetch_latest_media(preferred_kind):
    """
    Look through the upload folder for the newest unclaimed file whose
    mimeType matches preferred_kind ('image' or 'video'). Files of the other
    kind, and files already claimed by another concurrent run, are skipped
    (left untouched) so a future run can pick them up.

    Returns (file_dict, local_path, kind) or (None, None, None) if nothing
    matching was found this cycle.
    """
    creds = get_creds()
    service = build("drive", "v3", credentials=creds)
    folder_id = get_env("UPLOAD_FOLDER_ID")
    results = service.files().list(
        q=f"'{folder_id}' in parents",
        orderBy="createdTime desc",
        pageSize=25,
    ).execute()
    files = results.get("files", [])
    if not files:
        print("No files found in upload folder.")
        return None, None, None

    prefix = f"{preferred_kind}/"  # "image/" or "video/"

    for file in files:
        mime_type = file.get("mimeType", "")
        original_name = file["name"]

        if original_name.startswith(CLAIM_PREFIX):
            print(f"Skipping '{original_name}' — already claimed by another run.")
            continue

        if not mime_type.startswith(prefix):
            # Not the kind we're posting this run — leave it for later.
            continue

        print(f"Found {preferred_kind}: {original_name} ({mime_type})")

        claimed_name = claim_file(service, file["id"], original_name)
        if claimed_name is None:
            continue

        print(f"Claimed '{original_name}' as '{claimed_name}'.")
        local_path = f"/tmp/{original_name}"
        _download_file(service, file["id"], local_path)

        file["claimed_name"] = claimed_name
        file["original_name"] = original_name
        file["mime_type"] = mime_type
        return file, local_path, preferred_kind

    print(f"No unclaimed {preferred_kind} files found in upload folder.")
    return None, None, None


def move_file(file_id, restore_name=None):
    creds = get_creds()
    service = build("drive", "v3", credentials=creds)
    upload_id = get_env("UPLOAD_FOLDER_ID")
    processed_id = get_env("PROCESSED_FOLDER_ID")

    body = {}
    if restore_name:
        # Drop the CLAIMED_<run>__ prefix once we're safely done with the
        # file, so the processed folder shows clean original filenames.
        body["name"] = restore_name

    service.files().update(
        fileId=file_id,
        addParents=processed_id,
        removeParents=upload_id,
        body=body,
    ).execute()
    print("Moved file to processed folder.")


MAX_POST_LENGTH = 300  # Bluesky's grapheme limit per post
LOOP_INTERVAL_SECONDS = 3900  # 65 minutes between cycles (only used by main()'s loop mode)

# ── Link definition ───────────────────────────────────────────────────────
# Bluesky shows a "Leaving Bluesky" confirmation interstitial whenever the
# displayed link text doesn't match the href's domain (phishing protection).
# To get a plain clickable link that opens directly with no warning, the
# *displayed* text must be exactly the bare domain — same text Bluesky's own
# UI would render for a link facet pointing at that domain.
LINK_URL = "https://kr.teentoday.cfd"
LINK_DISPLAY_TEXT = "kr.teentoday.cfd"


def build_post(tags: list[str], with_link: bool) -> TextBuilder:
    """
    Layout when with_link=True:

        Caption line
        \n
        <link_action_caption from the same CSV row>
        foodieposts.com   (clickable link, opens with no warning)
        \n
        #tag1 #tag2 #tag3 ...   (omitted entirely if tags is empty)

    Layout when with_link=False (no CTA, no link — just caption + tags):

        Caption line
        \n
        #tag1 #tag2 #tag3 ...   (omitted entirely if tags is empty)
    """
    tb = TextBuilder()

    if with_link:
        caption, cta = pick_random_caption_and_cta()
    else:
        caption, cta = pick_random_caption_only(), ""

    if caption:
        tb.text(caption)
        tb.text("\n\n")

    if with_link:
        # Plain-text CTA line (matched to the caption above from the same
        # CSV row), then the clickable domain link on the line below it.
        # Display text == bare domain == href domain, so Bluesky opens it
        # directly instead of showing the leaving-site warning.
        if cta:
            tb.text(cta)
            tb.text("\n")
        tb.link(LINK_DISPLAY_TEXT, LINK_URL)
        tb.text("\n\n")

    for i, tag in enumerate(tags):
        tb.tag(f"#{tag}", tag)
        if i < len(tags) - 1:
            tb.text(" ")

    return tb


def post_video_to_bluesky(client, video_name, local_path, tags, with_link):
    text_builder = build_post(tags, with_link)
    with open(local_path, "rb") as f:
        video_bytes = f.read()
    client.send_video(
        text=text_builder,
        video=video_bytes,
        video_alt=video_name,
    )


def post_image_to_bluesky(client, image_name, local_path, tags, with_link):
    text_builder = build_post(tags, with_link)
    with open(local_path, "rb") as f:
        image_bytes = f.read()
    client.send_image(
        text=text_builder,
        image=image_bytes,
        image_alt=image_name,
    )


def post_to_bluesky(client, media_name, local_path, kind, with_link):
    # Hashtag on/off is configurable separately per media kind.
    hashtags_enabled = HASHTAGS_ENABLED_IMAGE if kind == "image" else HASHTAGS_ENABLED_VIDEO
    tags = pick_random_hashtags("hashtags.txt") if hashtags_enabled else []

    if kind == "video":
        post_video_to_bluesky(client, media_name, local_path, tags, with_link)
    else:
        post_image_to_bluesky(client, media_name, local_path, tags, with_link)

    print(f"Posted {kind} to Bluesky (with_link={with_link}):")
    if with_link:
        print("  Link:", LINK_DISPLAY_TEXT)
    if tags:
        print("  Tags:", " ".join(f"#{t}" for t in tags))
    else:
        print("  Tags: (hashtags disabled for this media kind)")


def release_claim(file_id, original_name):
    """
    Rename a claimed file back to its original name if something failed
    after claiming but before the move-to-processed step. Without this, a
    failed post would leave the file stuck with a CLAIMED_ prefix forever,
    invisible to future fetch calls.
    """
    try:
        creds = get_creds()
        service = build("drive", "v3", credentials=creds)
        service.files().update(fileId=file_id, body={"name": original_name}).execute()
        print(f"Released claim on '{original_name}' after failure.")
    except Exception as e:
        print(f"Warning: failed to release claim on file {file_id}: {e}")


def run_once():
    """Run a single fetch -> post -> move cycle.

    Each cycle independently rolls:
      1. image vs video, per IMAGE_RATIO/VIDEO_RATIO
      2. with-link vs no-link, per NO_LINK_RATIO

    We log into Bluesky once at the top of the cycle (rather than inside the
    posting step) so the account handle gets printed and the daily follower
    report can run even on cycles where there's no new media to post.

    If the preferred media kind has no unclaimed files waiting, we fall back
    to the other kind rather than skipping the whole cycle — so an empty
    image folder doesn't stall posting when videos are available (and vice
    versa).
    """
    handle = get_env("BSKY_HANDLE")
    app_pw = get_env("BSKY_APP_PW")
    print_target_account(handle)
    client = Client()
    client.login(handle, app_pw)

    maybe_generate_daily_follower_report(client, handle)

    preferred_kind = choose_media_kind()
    fallback_kind = "video" if preferred_kind == "image" else "image"

    file, local_path, kind = fetch_latest_media(preferred_kind)
    if not file:
        print(f"No unclaimed {preferred_kind} available; trying fallback ({fallback_kind}).")
        file, local_path, kind = fetch_latest_media(fallback_kind)

    if not file:
        print("No new media of either kind this cycle.")
        return

    with_link = random.random() >= NO_LINK_RATIO  # NO_LINK_RATIO chance of False
    original_name = file.get("original_name", file["name"])

    try:
        post_to_bluesky(client, original_name, local_path, kind, with_link)
    except Exception:
        # Posting failed (e.g. transient API error) — give the file back so
        # it's eligible to be picked up and retried next cycle, rather than
        # leaving it stuck under a CLAIMED_ name indefinitely.
        release_claim(file["id"], original_name)
        raise

    move_file(file["id"], restore_name=original_name)
    # Clean up the local temp copy so disk doesn't fill up over a long-running loop
    try:
        os.remove(local_path)
    except OSError:
        pass


def main():
    """
    Loop forever, running one post cycle every LOOP_INTERVAL_SECONDS.
    Each cycle is wrapped in try/except so a single failure (e.g. a transient
    API error) doesn't kill the whole loop - it just gets logged and retried
    next cycle.

    The workflow restarts this job periodically (GitHub's 6-hour job hard
    cap), so this loop doesn't need to run forever on its own — just until
    GitHub kills it, at which point the next scheduled trigger spins up a
    fresh run that picks up right where this leaves off.
    """
    print_runner_ip_and_location()
    print_config_summary()
    print(f"Starting loop. Posting every {LOOP_INTERVAL_SECONDS} seconds.")
    while True:
        cycle_start = time.time()
        try:
            run_once()
        except Exception as e:
            print(f"Error during cycle: {e}")

        elapsed = time.time() - cycle_start
        sleep_for = max(0, LOOP_INTERVAL_SECONDS - elapsed)
        print(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.1f}s...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
