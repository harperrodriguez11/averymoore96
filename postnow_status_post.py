import io
import json
import os
import random
import re
import socket
import sys
import time
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


# ── Content mix knobs ─────────────────────────────────────────────────────
# What fraction of posts should be images vs. videos. Configurable via the
# IMAGE_RATIO / VIDEO_RATIO env vars (plain fraction like "0.6" or a
# percentage like "60"/"60%"). Whatever values are given get auto-normalized
# to sum to 1.0.
_raw_image_ratio = get_float_env("IMAGE_RATIO", 0.60)
_raw_video_ratio = get_float_env("VIDEO_RATIO", 0.40)
_ratio_sum = _raw_image_ratio + _raw_video_ratio
if _ratio_sum <= 0:
    IMAGE_RATIO, VIDEO_RATIO = 0.60, 0.40
else:
    IMAGE_RATIO = _raw_image_ratio / _ratio_sum
    VIDEO_RATIO = _raw_video_ratio / _ratio_sum

# Whether to attach hashtags at all, broken out per media kind so e.g. video
# posts can carry hashtags while image posts don't (or vice versa).
HASHTAGS_ENABLED_IMAGE = get_bool_env("HASHTAGS_ENABLED_IMAGE", True)
HASHTAGS_ENABLED_VIDEO = get_bool_env("HASHTAGS_ENABLED_VIDEO", True)

# Max size (bytes) an image is compressed down to before posting. Videos are
# left untouched — only images get this treatment.
MAX_IMAGE_BYTES = int(get_float_env("MAX_IMAGE_MB", 2) * 1024 * 1024)


def print_config_summary():
    """Log the active content-mix knobs at startup so a glance at the job
    log confirms what this run is actually configured to do."""
    print("── Content mix config ──────────────────────")
    print(f"  Image ratio:  {IMAGE_RATIO:.0%}")
    print(f"  Video ratio:  {VIDEO_RATIO:.0%}")
    print(f"  Hashtags on image posts: {HASHTAGS_ENABLED_IMAGE}")
    print(f"  Hashtags on video posts: {HASHTAGS_ENABLED_VIDEO}")
    print(f"  Post-plan sheet tab: {get_post_plan_sheet_tab_name()}")
    print(f"  Max image size before posting: {MAX_IMAGE_BYTES / (1024*1024):.1f} MB")
    print(f"  Post link (used when a caption contains a URL): {LINK_DISPLAY_TEXT}")
    print("────────────────────────────────────────────")


def get_creds():
    """
    Build Google OAuth credentials from the GOOGLE_OAUTH_CREDENTIALS env var
    instead of a token.pickle file on disk. The env var holds the standard
    "authorized_user" JSON blob (token, refresh_token, client_id,
    client_secret, scopes, ...).

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


def print_target_account(handle):
    """
    Print which Bluesky handle this run is about to post as. We only ever
    print the handle, never the app password.
    """
    display_handle = handle if handle.startswith("@") else f"@{handle}"
    print(f"Target Bluesky account: {display_handle}")
    print(f"  (app password loaded: {'yes' if get_env('BSKY_APP_PW', required=False) else 'NO — missing!'}, value not printed for security)")


# ── Daily follower report (Google Sheet) ────────────────────────────────
FOLLOWER_SHEET_ID = "1d1ua2bzBt94omZxYgfwZhSJ94PJwAzc6clWpSVumebw"
FOLLOWER_SHEET_TAB = "Sheet1"
FOLLOWER_SHEET_RANGE = f"{FOLLOWER_SHEET_TAB}!A:F"
FOLLOWER_SHEET_HEADER = ["Date (UTC)", "Handle", "Previous Followers", "Followers Added", "Total Followers", "Status"]


def get_sheets_service():
    """Reuse the same OAuth creds (from GOOGLE_OAUTH_CREDENTIALS) already used
    for Drive — no separate Google credential needed, as long as the token's
    scopes cover Sheets."""
    creds = get_creds()
    return build("sheets", "v4", credentials=creds)


def ensure_follower_sheet_header(service):
    result = service.spreadsheets().values().get(
        spreadsheetId=FOLLOWER_SHEET_ID, range=f"{FOLLOWER_SHEET_TAB}!A1:F1"
    ).execute()
    if not result.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=FOLLOWER_SHEET_ID,
            range=f"{FOLLOWER_SHEET_TAB}!A1:F1",
            valueInputOption="RAW",
            body={"values": [FOLLOWER_SHEET_HEADER]},
        ).execute()
        print("Initialized follower-report sheet header.")


def get_last_follower_row(service, handle):
    result = service.spreadsheets().values().get(
        spreadsheetId=FOLLOWER_SHEET_ID, range=FOLLOWER_SHEET_RANGE
    ).execute()
    rows = result.get("values", [])
    last_row = None
    for row in rows[1:]:
        if len(row) >= 2 and row[1] == handle:
            last_row = row
    return last_row


def append_follower_row(service, date_str, handle, previous, added, total, status="Active"):
    service.spreadsheets().values().append(
        spreadsheetId=FOLLOWER_SHEET_ID,
        range=FOLLOWER_SHEET_RANGE,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[date_str, handle, previous, added, total, status]]},
    ).execute()


# ── Account takedown handling ────────────────────────────────────────────

class AccountTakenDownError(Exception):
    """Raised when Bluesky returns AccountTakedown for this handle."""


class NoMediaFoundError(Exception):
    """Raised when there's no unclaimed Drive file that also has a matching
    row in the post-plan sheet this cycle."""


def log_account_ban_to_sheet(handle, status="⛔ ACCOUNT TAKEN DOWN / BANNED"):
    today_str = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        service = get_sheets_service()
        ensure_follower_sheet_header(service)
        last_row = get_last_follower_row(service, handle)
        last_total = int(last_row[4]) if last_row and len(last_row) >= 5 else 0
        append_follower_row(
            service, today_str, handle,
            previous=last_total, added=0, total=last_total,
            status=status,
        )
        print(f"Logged account ban for {handle} to report sheet.")
    except Exception as e:
        print(f"Warning: could not log ban to sheet: {e}")


def maybe_generate_daily_follower_report(client, handle):
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
        previous_total = int(last_row[4]) if last_row and len(last_row) >= 5 else total_followers
        added = total_followers - previous_total

        append_follower_row(service, today_str, handle, previous_total, added, total_followers, status="Active")
        print(
            f"Logged daily follower report for {handle}: "
            f"previous={previous_total}, added={added:+d}, total={total_followers}"
        )
    except Exception as e:
        print(f"Warning: could not generate/append daily follower report: {e}")


def load_hashtag_sets(filepath="hashtags.txt"):
    sets = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                sets.append(line)
    return sets


def pick_random_hashtags(filepath="hashtags.txt"):
    hashtag_sets = load_hashtag_sets(filepath)
    if not hashtag_sets:
        return []
    chosen_line = random.choice(hashtag_sets)
    return [word.lstrip("#") for word in chosen_line.split() if word.startswith("#")]


# ── Post plan (Google Sheet: File Name | Caption | Status) ──────────────
# Drives WHAT gets posted: only files that (a) sit unclaimed in the Drive
# upload folder, (b) have a matching "File Name" row in this sheet, AND
# (c) do NOT already have "posted" in that row's "Status" column get
# posted, using that row's "Caption" text verbatim (with any URL inside the
# caption swapped for your real POST_LINK_URL — see build_post_from_caption).
# Immediately after a successful post, that row's Status cell is written
# with "posted" so the same file/row can never be picked again.
POST_PLAN_SHEET_ID = "1juum0RextNq44mrBN1Uu7ceSZA2V4Tmb9_oly3EORmA"
POSTED_STATUS_VALUE = "posted"

# {filename: {"caption": str, "row": int (1-based sheet row), "status": str}}
# Populated once per process run and reused; entries get flipped to
# "posted" in-memory the moment we write the same value to the sheet, so a
# single long-running loop (many cycles) never re-picks a file it already
# posted this run, even before the next sheet refresh.
_post_plan_cache = None
_post_plan_status_col_idx = None  # 0-based column index of "Status", or None if absent


def get_post_plan_sheet_tab_name():
    """Tab name inside the post-plan spreadsheet. Configurable via
    POST_PLAN_SHEET_NAME; defaults to 'Sheet1' (matches the shared sheet)."""
    return get_env("POST_PLAN_SHEET_NAME", required=False) or "Sheet1"


def _column_index_to_letter(idx0):
    """0-based column index -> spreadsheet column letter(s), e.g. 0->'A', 26->'AA'."""
    idx = idx0 + 1
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def load_post_plan(force_refresh=False):
    """Return a dict of {exact Drive file name: {"caption", "row", "status"}}
    from the post-plan sheet. Cached after first call so a long-running loop
    doesn't re-fetch the sheet every cycle; pass force_refresh=True to bypass
    the cache and re-read the sheet from scratch."""
    global _post_plan_cache, _post_plan_status_col_idx
    if _post_plan_cache is not None and not force_refresh:
        return _post_plan_cache

    tab_name = get_post_plan_sheet_tab_name()
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=POST_PLAN_SHEET_ID, range=f"{tab_name}!A:Z"
    ).execute()
    values = result.get("values", [])
    if not values:
        print(f"Warning: post-plan tab '{tab_name}' is empty.")
        _post_plan_cache = {}
        return _post_plan_cache

    header = [h.strip().lower() for h in values[0]]

    def col_index(*names):
        for name in names:
            if name in header:
                return header.index(name)
        return None

    file_idx = col_index("file name", "filename", "file")
    caption_idx = col_index("caption", "captions")
    status_idx = col_index("status")
    _post_plan_status_col_idx = status_idx

    if file_idx is None or caption_idx is None:
        print(
            f"Warning: post-plan tab '{tab_name}' header {header} is missing a "
            "'File Name' and/or 'Caption' column."
        )
        _post_plan_cache = {}
        return _post_plan_cache

    if status_idx is None:
        print(
            f"Warning: post-plan tab '{tab_name}' has no 'Status' column — "
            "posted files won't be remembered and could repeat. Add a "
            "'Status' header to fix this."
        )

    plan = {}
    skipped_already_posted = 0
    for i, row in enumerate(values[1:], start=2):  # start=2: row 1 is the header
        fname = row[file_idx].strip() if len(row) > file_idx else ""
        caption = row[caption_idx].strip() if len(row) > caption_idx else ""
        status = row[status_idx].strip() if status_idx is not None and len(row) > status_idx else ""
        if not fname:
            continue
        plan[fname] = {"caption": caption, "row": i, "status": status}
        if status.lower() == POSTED_STATUS_VALUE:
            skipped_already_posted += 1

    print(
        f"Loaded {len(plan)} file→caption entries from post-plan tab '{tab_name}' "
        f"({skipped_already_posted} already marked posted)."
    )
    _post_plan_cache = plan
    return plan


def mark_post_plan_row_posted(filename, row_number):
    """Write 'posted' into the Status column for this row, and reflect the
    same change in the in-memory cache so this run's loop never re-picks it.
    Never raises — a failure to write status shouldn't crash a successful
    post, but IS printed loudly since it risks a duplicate post later."""
    global _post_plan_cache
    if _post_plan_status_col_idx is None:
        print(
            f"Warning: no 'Status' column found in post-plan sheet — could not "
            f"mark '{filename}' (row {row_number}) as posted. Add a 'Status' "
            "header to the sheet to prevent repeat posts."
        )
        return
    try:
        tab_name = get_post_plan_sheet_tab_name()
        col_letter = _column_index_to_letter(_post_plan_status_col_idx)
        service = get_sheets_service()
        service.spreadsheets().values().update(
            spreadsheetId=POST_PLAN_SHEET_ID,
            range=f"{tab_name}!{col_letter}{row_number}",
            valueInputOption="RAW",
            body={"values": [[POSTED_STATUS_VALUE]]},
        ).execute()
        if _post_plan_cache is not None and filename in _post_plan_cache:
            _post_plan_cache[filename]["status"] = POSTED_STATUS_VALUE
        print(f"Marked '{filename}' (sheet row {row_number}) as '{POSTED_STATUS_VALUE}'.")
    except Exception as e:
        print(
            f"ERROR: post succeeded but failed to write Status='{POSTED_STATUS_VALUE}' "
            f"for '{filename}' (row {row_number}): {e}. "
            "This file may get posted again next cycle — check the sheet manually."
        )


def claim_file(service, file_id, current_name):
    """
    Try to "claim" a Drive file by renaming it with this run's unique tag.
    Returns the new (claimed) filename on success, or None if we lost the race.
    """
    claimed_name = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    service.files().update(
        fileId=file_id,
        body={"name": claimed_name},
    ).execute()

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
    request = service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()


def fetch_media_matching_plan(preferred_kind, plan):
    """
    Look through the upload folder for the newest unclaimed file whose name
    has an exact match in the post-plan sheet AND whose mimeType matches
    preferred_kind ('image' or 'video'). Files not listed in the sheet, the
    wrong kind, already marked "posted" in Status, or already claimed by
    another concurrent run are skipped.

    Returns (file_dict, local_path, kind, caption, row_number) or
    (None, None, None, None, None) if nothing matching was found this cycle.
    """
    creds = get_creds()
    service = build("drive", "v3", credentials=creds)
    folder_id = get_env("UPLOAD_FOLDER_ID")
    results = service.files().list(
        q=f"'{folder_id}' in parents",
        orderBy="createdTime desc",
        pageSize=100,
    ).execute()
    files = results.get("files", [])
    if not files:
        print("No files found in upload folder.")
        return None, None, None, None, None

    prefix = f"{preferred_kind}/"  # "image/" or "video/"

    for file in files:
        original_name = file["name"]

        if original_name.startswith(CLAIM_PREFIX):
            continue

        entry = plan.get(original_name)
        if entry is None:
            # Not in the post-plan sheet — leave it alone.
            continue

        if entry["status"].strip().lower() == POSTED_STATUS_VALUE:
            # Already posted per the sheet — never repeat it.
            continue

        mime_type = file.get("mimeType", "")
        if not mime_type.startswith(prefix):
            continue

        caption = entry["caption"]
        row_number = entry["row"]
        print(f"Found {preferred_kind} in post plan: {original_name} ({mime_type})")

        claimed_name = claim_file(service, file["id"], original_name)
        if claimed_name is None:
            continue

        print(f"Claimed '{original_name}' as '{claimed_name}'.")
        local_path = f"/tmp/{original_name}"
        _download_file(service, file["id"], local_path)

        file["claimed_name"] = claimed_name
        file["original_name"] = original_name
        file["mime_type"] = mime_type
        return file, local_path, preferred_kind, caption, row_number

    print(f"No unclaimed, not-yet-posted {preferred_kind} files found that match the post-plan sheet.")
    return None, None, None, None, None


def compress_image_under_limit(local_path, max_bytes=MAX_IMAGE_BYTES):
    """
    Compress an image down to at most max_bytes using Pillow. Tries
    decreasing JPEG quality first, then progressively downscales dimensions
    if quality reduction alone isn't enough. Videos are never touched — this
    is only called for kind == 'image'.

    Re-saves to the same path (always as JPEG, since that gets the best
    size/quality tradeoff and Bluesky doesn't care about file extension —
    only the bytes + alt text matter for send_image).
    """
    from PIL import Image

    original_size = os.path.getsize(local_path)
    if original_size <= max_bytes:
        print(f"Image already under limit ({original_size / 1024:.0f} KB); skipping compression.")
        return local_path

    img = Image.open(local_path)
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    quality = 90
    while quality >= 30:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= max_bytes:
            with open(local_path, "wb") as f:
                f.write(buf.getvalue())
            print(f"Compressed image: {original_size/1024:.0f} KB -> {buf.tell()/1024:.0f} KB (quality={quality}).")
            return local_path
        quality -= 10

    # Quality reduction alone wasn't enough — downscale dimensions too.
    width, height = img.size
    scale = 0.9
    while scale > 0.3:
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        resized = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=70, optimize=True)
        if buf.tell() <= max_bytes:
            with open(local_path, "wb") as f:
                f.write(buf.getvalue())
            print(f"Compressed+resized image: {original_size/1024:.0f} KB -> {buf.tell()/1024:.0f} KB at {new_size}.")
            return local_path
        scale -= 0.1

    # Best effort — save whatever the smallest attempt produced.
    with open(local_path, "wb") as f:
        f.write(buf.getvalue())
    print(f"Warning: could not fully compress under limit; final size {buf.tell()/1024:.0f} KB.")
    return local_path


def move_file(file_id, restore_name=None):
    creds = get_creds()
    service = build("drive", "v3", credentials=creds)
    upload_id = get_env("UPLOAD_FOLDER_ID")
    processed_id = get_env("PROCESSED_FOLDER_ID")

    body = {}
    if restore_name:
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
# displayed link text doesn't match the href's domain. To get a plain
# clickable link with no warning, the *displayed* text must be exactly the
# bare domain.
_raw_link = get_env("POST_LINK_URL", required=False).strip().rstrip("/") or "https://kr.teentoday.cfd"
LINK_URL = _raw_link if _raw_link.startswith("http") else f"https://{_raw_link}"
LINK_DISPLAY_TEXT = LINK_URL.replace("https://", "").replace("http://", "")

URL_IN_CAPTION_PATTERN = re.compile(r"https?://\S+")


def build_post_from_caption(caption: str, tags: list[str]) -> TextBuilder:
    """
    Build the post text from the sheet's caption. If the caption contains a
    URL anywhere in it, that URL is swapped out for a clickable link facet
    pointing at your real POST_LINK_URL (displayed as the bare domain, so it
    opens directly with no "leaving site" warning) — everything else in the
    caption is left exactly as written. If the caption has no URL, no link
    is added at all.

        "Do you like garlic yes or no https://foodieposts.com/garlic"
          -> "Do you like garlic yes or no <kr.teentoday.cfd>" (clickable)

    Hashtags (if any) are appended on their own line at the end.
    """
    tb = TextBuilder()

    match = URL_IN_CAPTION_PATTERN.search(caption) if caption else None
    if match:
        before = caption[: match.start()]
        after = caption[match.end():]
        if before:
            tb.text(before)
        tb.link(LINK_DISPLAY_TEXT, LINK_URL)
        if after.strip():
            tb.text(after)
    elif caption:
        tb.text(caption)

    if tags:
        if caption:
            tb.text("\n\n")
        for i, tag in enumerate(tags):
            tb.tag(f"#{tag}", tag)
            if i < len(tags) - 1:
                tb.text(" ")

    return tb


def post_video_to_bluesky(client, video_name, local_path, tb):
    with open(local_path, "rb") as f:
        video_bytes = f.read()
    client.send_video(
        text=tb,
        video=video_bytes,
        video_alt=video_name,
    )


def post_image_to_bluesky(client, image_name, local_path, tb):
    with open(local_path, "rb") as f:
        image_bytes = f.read()
    client.send_image(
        text=tb,
        image=image_bytes,
        image_alt=image_name,
    )


def post_to_bluesky(client, media_name, local_path, kind, caption, tags):
    tb = build_post_from_caption(caption, tags)

    if kind == "video":
        post_video_to_bluesky(client, media_name, local_path, tb)
    else:
        post_image_to_bluesky(client, media_name, local_path, tb)

    print(f"Posted {kind} to Bluesky:")
    print(f"  Caption: {caption!r}")
    if tags:
        print("  Tags:", " ".join(f"#{t}" for t in tags))
    else:
        print("  Tags: (none)")


def release_claim(file_id, original_name):
    """Rename a claimed file back to its original name if something failed
    after claiming but before the move-to-processed step."""
    try:
        creds = get_creds()
        service = build("drive", "v3", credentials=creds)
        service.files().update(fileId=file_id, body={"name": original_name}).execute()
        print(f"Released claim on '{original_name}' after failure.")
    except Exception as e:
        print(f"Warning: failed to release claim on file {file_id}: {e}")


def run_once():
    """Run a single fetch -> post -> move cycle.

    Each cycle rolls image vs video (per IMAGE_RATIO/VIDEO_RATIO), then picks
    the newest unclaimed Drive file of that kind that also has a matching row
    in the post-plan sheet (and isn't already marked posted), using that
    row's caption as-is (with any URL in it swapped for your real link).
    """
    handle = get_env("BSKY_HANDLE")
    app_pw = get_env("BSKY_APP_PW")
    print_target_account(handle)
    client = Client()
    try:
        client.login(handle, app_pw)
    except Exception as e:
        err_str = str(e)
        if "AccountTakedown" in err_str or "AccountSuspended" in err_str:
            raise AccountTakenDownError(
                f"Account {handle} has been taken down / suspended."
            ) from e
        if "AuthenticationRequired" in err_str or "Invalid identifier or password" in err_str:
            raise AccountTakenDownError(
                f"Authentication failed for {handle} — invalid handle or app password. "
                "Fix BSKY_HANDLE / BSKY_APP_PW in repo secrets/variables, then re-enable the workflow."
            ) from e
        raise

    maybe_generate_daily_follower_report(client, handle)

    plan = load_post_plan()
    if not plan:
        raise NoMediaFoundError(
            "Post-plan sheet has no usable File Name/Caption rows. "
            "Check the sheet headers and contents."
        )

    preferred_kind = choose_media_kind()
    fallback_kind = "video" if preferred_kind == "image" else "image"

    file, local_path, kind, caption, row_number = fetch_media_matching_plan(preferred_kind, plan)
    if not file:
        print(f"No unclaimed {preferred_kind} matched the post plan; trying fallback ({fallback_kind}).")
        file, local_path, kind, caption, row_number = fetch_media_matching_plan(fallback_kind, plan)

    if not file:
        raise NoMediaFoundError(
            "No unclaimed, not-yet-posted Drive file both present in the upload "
            "folder AND listed in the post-plan sheet was found. Exiting cleanly "
            "— will check again at the next scheduled run."
        )

    original_name = file.get("original_name", file["name"])

    try:
        if kind == "image":
            local_path = compress_image_under_limit(local_path)

        hashtags_enabled = HASHTAGS_ENABLED_IMAGE if kind == "image" else HASHTAGS_ENABLED_VIDEO
        tags = pick_random_hashtags("hashtags.txt") if hashtags_enabled else []

        post_to_bluesky(client, original_name, local_path, kind, caption, tags)
    except Exception as e:
        err_str = str(e)
        if "AccountTakedown" in err_str or "AccountSuspended" in err_str:
            release_claim(file["id"], original_name)
            raise AccountTakenDownError(
                f"Account {handle} has been taken down / suspended mid-cycle."
            ) from e
        release_claim(file["id"], original_name)
        raise

    # Post succeeded — mark it posted in the sheet immediately, before the
    # Drive move, so a crash during move_file still can't cause a repeat post.
    mark_post_plan_row_posted(original_name, row_number)

    move_file(file["id"], restore_name=original_name)
    try:
        os.remove(local_path)
    except OSError:
        pass


def main():
    """
    Loop forever, running one post cycle every LOOP_INTERVAL_SECONDS.
    AccountTakenDownError exits immediately and logs the ban to the follower
    report sheet.
    """
    print_config_summary()
    print(f"Starting loop. Posting every {LOOP_INTERVAL_SECONDS} seconds.")
    while True:
        cycle_start = time.time()
        try:
            run_once()
        except NoMediaFoundError as e:
            print(f"\n{'='*60}")
            print(f"NO MEDIA FOUND: {e}")
            print("Stopping this run. Scheduled runs will continue as normal.")
            print(f"{'='*60}\n")
            sys.exit(0)
        except AccountTakenDownError as e:
            handle = get_env("BSKY_HANDLE", required=False) or "unknown"
            err_str = str(e)
            if "Authentication failed" in err_str or "app password" in err_str:
                reason = "🔑 AUTH FAILED — wrong handle or app password"
            else:
                reason = "⛔ ACCOUNT TAKEN DOWN / BANNED"
            print(f"\n{'='*60}")
            print(err_str)
            print(f"Logging to report sheet as: {reason}")
            print("Stopping workflow.")
            print(f"{'='*60}\n")
            log_account_ban_to_sheet(handle, status=reason)
            sys.exit(1)
        except Exception as e:
            print(f"Error during cycle: {e}")

        elapsed = time.time() - cycle_start
        sleep_for = max(0, LOOP_INTERVAL_SECONDS - elapsed)
        print(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.1f}s...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
