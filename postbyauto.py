import csv
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

RUN_TAG = os.getenv("GITHUB_RUN_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
CLAIM_PREFIX = "CLAIMED_"


def get_env(name, required=True):
    value = os.getenv(name)
    if value is None:
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return ""
    return value.strip()


def get_float_env(name, default):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    raw = raw.strip().rstrip("%")
    try:
        value = float(raw)
    except ValueError:
        print(f"Warning: could not parse {name}='{raw}' as a number; using default {default}.")
        return default
    if value > 1:
        value = value / 100.0
    return value


def get_bool_env(name, default=True):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── Content mix knobs ────────────────────────────────────────────────────
_raw_image_ratio = get_float_env("IMAGE_RATIO", 0.60)
_raw_video_ratio = get_float_env("VIDEO_RATIO", 0.40)
_ratio_sum = _raw_image_ratio + _raw_video_ratio
if _ratio_sum <= 0:
    IMAGE_RATIO, VIDEO_RATIO = 0.60, 0.40
else:
    IMAGE_RATIO = _raw_image_ratio / _ratio_sum
    VIDEO_RATIO = _raw_video_ratio / _ratio_sum

NO_LINK_RATIO          = get_float_env("NO_LINK_RATIO", 0.20)
HASHTAGS_ENABLED_IMAGE = get_bool_env("HASHTAGS_ENABLED_IMAGE", True)
HASHTAGS_ENABLED_VIDEO = get_bool_env("HASHTAGS_ENABLED_VIDEO", False)

# Caption mode: "random" or "filename"
CAPTION_MODE = (get_env("CAPTION_MODE", required=False) or "filename").strip().lower()

# ── Link ─────────────────────────────────────────────────────────────────
_raw_link    = get_env("POST_LINK_URL", required=False).strip().rstrip("/") or "https://kr.teentoday.cfd"
LINK_URL     = _raw_link if _raw_link.startswith("http") else f"https://{_raw_link}"
LINK_DISPLAY_TEXT = LINK_URL.replace("https://", "").replace("http://", "")

_URL_RE = re.compile(r"https?://\S+")


def replace_links(text):
    """Swap every https?://... URL in text with LINK_URL."""
    if not text:
        return text
    return _URL_RE.sub(LINK_URL, text).strip()


def print_config_summary():
    print("── Content mix config ──────────────────────────")
    print(f"  Image ratio:              {IMAGE_RATIO:.0%}")
    print(f"  Video ratio:              {VIDEO_RATIO:.0%}")
    print(f"  No-link rate:             {NO_LINK_RATIO:.0%}")
    print(f"  Hashtags on image posts:  {HASHTAGS_ENABLED_IMAGE}")
    print(f"  Hashtags on video posts:  {HASHTAGS_ENABLED_VIDEO}")
    print(f"  Caption mode:             {CAPTION_MODE}")
    if CAPTION_MODE == "filename":
        print(f"  Filename-map sheet tab:   {get_filename_map_tab_name()}")
    else:
        print(f"  Random caption tab:       {get_caption_sheet_tab_name()}")
    print(f"  Post link:                {LINK_DISPLAY_TEXT}")
    print("─────────────────────────────────────────────────")


# ── Google credentials ───────────────────────────────────────────────────
def get_creds():
    from google.oauth2.credentials import Credentials
    raw = get_env("GOOGLE_OAUTH_CREDENTIALS")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            "GOOGLE_OAUTH_CREDENTIALS is not valid JSON."
        ) from e
    creds = Credentials.from_authorized_user_info(info)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def get_sheets_service():
    return build("sheets", "v4", credentials=get_creds())


# ── Account display ──────────────────────────────────────────────────────
def print_target_account(handle):
    display = handle if handle.startswith("@") else f"@{handle}"
    print(f"Target Bluesky account: {display}")
    print(f"  (app password loaded: {'yes' if get_env('BSKY_APP_PW', required=False) else 'NO — missing!'})")


# ── Daily follower report ────────────────────────────────────────────────
FOLLOWER_SHEET_ID     = "1d1ua2bzBt94omZxYgfwZhSJ94PJwAzc6clWpSVumebw"
FOLLOWER_SHEET_TAB    = "Sheet1"
FOLLOWER_SHEET_RANGE  = f"{FOLLOWER_SHEET_TAB}!A:F"
FOLLOWER_SHEET_HEADER = ["Date (UTC)", "Handle", "Previous Followers",
                         "Followers Added", "Total Followers", "Status"]


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


def log_account_ban_to_sheet(handle, status="⛔ ACCOUNT TAKEN DOWN / BANNED"):
    today_str = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        service = get_sheets_service()
        ensure_follower_sheet_header(service)
        last_row   = get_last_follower_row(service, handle)
        last_total = int(last_row[4]) if last_row and len(last_row) >= 5 else 0
        append_follower_row(service, today_str, handle,
                            previous=last_total, added=0, total=last_total, status=status)
        print(f"Logged status '{status}' for {handle}.")
    except Exception as e:
        print(f"Warning: could not log to sheet: {e}")


def maybe_generate_daily_follower_report(client, handle):
    today_str = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        service = get_sheets_service()
        ensure_follower_sheet_header(service)
        last_row = get_last_follower_row(service, handle)
        if last_row and last_row[0] == today_str:
            print(f"Follower report for {handle} already logged today; skipping.")
            return
        profile        = client.get_profile(actor=handle)
        total          = profile.followers_count or 0
        previous_total = int(last_row[4]) if last_row and len(last_row) >= 5 else total
        added          = total - previous_total
        append_follower_row(service, today_str, handle, previous_total, added, total)
        print(f"Follower report: previous={previous_total}, added={added:+d}, total={total}")
    except Exception as e:
        print(f"Warning: follower report failed: {e}")


# ── Error types ──────────────────────────────────────────────────────────
class AccountTakenDownError(Exception):
    """Fatal — disable workflow forever."""

class NoMediaFoundError(Exception):
    """Clean exit — keep schedule running."""


# ── Hashtags ─────────────────────────────────────────────────────────────
def pick_random_hashtags(filepath="hashtags.txt"):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            sets = [l.strip() for l in f if l.strip()]
        if not sets:
            return []
        chosen = random.choice(sets)
        return [w.lstrip("#") for w in chosen.split() if w.startswith("#")]
    except FileNotFoundError:
        return []


# ── Random captions sheet ────────────────────────────────────────────────
CAPTIONS_SHEET_ID     = "1dkzjf2wX6AYyf5XH1w9mzdvOVcYy_X2boF1L8znHwME"
CAPTIONS_SHEET_HEADER = ["captions", "link_action_caption"]
_caption_rows_cache   = None


def get_caption_sheet_tab_name():
    return get_env("CAPTION_SHEET_NAME", required=False) or "Sheet1"


def ensure_caption_tab_exists(service, tab_name):
    meta     = service.spreadsheets().get(spreadsheetId=CAPTIONS_SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab_name in existing:
        return
    print(f"Caption tab '{tab_name}' not found — creating it.")
    service.spreadsheets().batchUpdate(
        spreadsheetId=CAPTIONS_SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=CAPTIONS_SHEET_ID,
        range=f"{tab_name}!A1:B1",
        valueInputOption="RAW",
        body={"values": [CAPTIONS_SHEET_HEADER]},
    ).execute()
    print(f"Created caption tab '{tab_name}'.")


def load_caption_rows():
    global _caption_rows_cache
    if _caption_rows_cache is not None:
        return _caption_rows_cache
    tab_name = get_caption_sheet_tab_name()
    service  = get_sheets_service()
    ensure_caption_tab_exists(service, tab_name)
    result   = service.spreadsheets().values().get(
        spreadsheetId=CAPTIONS_SHEET_ID, range=f"{tab_name}!A:Z"
    ).execute()
    values   = result.get("values", [])
    if not values:
        _caption_rows_cache = []
        return _caption_rows_cache
    header = [h.strip().lower() for h in values[0]]
    def ci(*names):
        for n in names:
            if n in header: return header.index(n)
        return None
    cap_idx = ci("captions", "caption")
    cta_idx = ci("link_action_caption", "lik_action_caption")
    rows = []
    for row in values[1:]:
        cap = row[cap_idx].strip() if cap_idx is not None and len(row) > cap_idx else ""
        cta = row[cta_idx].strip() if cta_idx is not None and len(row) > cta_idx else ""
        if cap:
            rows.append((cap, cta))
    print(f"Loaded {len(rows)} random captions from tab '{tab_name}'.")
    _caption_rows_cache = rows
    return _caption_rows_cache


def pick_random_caption_and_cta():
    rows = load_caption_rows()
    return random.choice(rows) if rows else ("", "")


def pick_random_caption_only():
    rows = load_caption_rows()
    return random.choice(rows)[0] if rows else ""


# ── Filename → Caption sheet ─────────────────────────────────────────────
# Sheet columns: "File Name" (A) | "Caption" (B) | ignored (C, D...)
# Any URL in a caption is replaced with POST_LINK_URL on load.
FILENAME_MAP_SHEET_ID = "12KXL16nrcpsPXCrtycvt9it-irK4vPjJQm4anAILNFk"
_filename_map_cache   = None


def get_filename_map_tab_name():
    return get_env("FILENAME_MAP_TAB", required=False) or "Sheet1"


def ensure_filename_map_tab_exists(service, tab_name):
    meta     = service.spreadsheets().get(spreadsheetId=FILENAME_MAP_SHEET_ID).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if tab_name in existing:
        return
    print(f"Filename-map tab '{tab_name}' not found — creating it.")
    service.spreadsheets().batchUpdate(
        spreadsheetId=FILENAME_MAP_SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=FILENAME_MAP_SHEET_ID,
        range=f"{tab_name}!A1:B1",
        valueInputOption="RAW",
        body={"values": [["File Name", "Caption"]]},
    ).execute()
    print(f"Created filename-map tab '{tab_name}'.")


def load_filename_map():
    """Return {normalised_key: caption} — keys stored both with and without
    extension so either form matches. URLs in captions replaced with LINK_URL."""
    global _filename_map_cache
    if _filename_map_cache is not None:
        return _filename_map_cache

    tab_name = get_filename_map_tab_name()
    service  = get_sheets_service()
    ensure_filename_map_tab_exists(service, tab_name)

    result = service.spreadsheets().values().get(
        spreadsheetId=FILENAME_MAP_SHEET_ID, range=f"{tab_name}!A:D"
    ).execute()
    values = result.get("values", [])
    if len(values) < 2:
        print(f"Warning: filename-map tab '{tab_name}' has no rows yet.")
        _filename_map_cache = {}
        return _filename_map_cache

    header = [h.strip().lower() for h in values[0]]
    def ci(*names):
        for n in names:
            if n in header: return header.index(n)
        return None
    fn_idx  = ci("file name", "filename", "file_name")
    cap_idx = ci("caption", "captions")

    if fn_idx is None or cap_idx is None:
        print(f"Warning: filename-map header {header} needs 'File Name' and 'Caption'.")
        _filename_map_cache = {}
        return _filename_map_cache

    mapping = {}
    for row in values[1:]:
        filename = row[fn_idx].strip()  if len(row) > fn_idx  else ""
        raw_cap  = row[cap_idx].strip() if len(row) > cap_idx else ""
        caption  = replace_links(raw_cap)
        if filename and caption:
            key_full = filename.lower()
            key_bare = os.path.splitext(key_full)[0]
            mapping[key_full] = caption
            mapping[key_bare] = caption

    print(f"Loaded {len(values)-1} filename→caption rows "
          f"({len(mapping)//2} unique files, URLs → {LINK_DISPLAY_TEXT}).")
    _filename_map_cache = mapping
    return _filename_map_cache


def get_caption_for_file(media_name, with_link):
    """Return (caption, cta). In filename mode captions already contain the
    link inline so cta is empty to avoid a duplicate link block."""
    if CAPTION_MODE == "filename":
        mapping  = load_filename_map()
        key_full = media_name.lower()
        key_bare = os.path.splitext(key_full)[0]
        caption  = mapping.get(key_full) or mapping.get(key_bare)
        if caption:
            print(f"  Caption: matched '{media_name}' from filename map")
            return caption, ""   # link already embedded in caption
        print(f"  Caption: no match for '{media_name}', falling back to random")

    if with_link:
        cap, cta = pick_random_caption_and_cta()
        return replace_links(cap), replace_links(cta)
    return replace_links(pick_random_caption_only()), ""


# ── Image compression ────────────────────────────────────────────────────
BLUESKY_IMAGE_LIMIT = 1_900_000   # Bluesky hard limit is 2 000 000; use 1.9 MB to be safe


def compress_image_bytes(image_bytes):
    """Return image_bytes compressed to under BLUESKY_IMAGE_LIMIT.
    Tries progressive JPEG quality reduction first, then resizing."""
    if len(image_bytes) <= BLUESKY_IMAGE_LIMIT:
        return image_bytes
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        original_size = len(image_bytes)
        quality = 85
        while quality >= 20:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= BLUESKY_IMAGE_LIMIT:
                print(f"  Compressed image {original_size:,} → {len(data):,} bytes (q={quality})")
                return data
            quality -= 15
        # Quality reduction wasn't enough — scale down
        w, h = img.size
        while w > 400:
            w, h = int(w * 0.75), int(h * 0.75)
            small = img.resize((w, h), Image.LANCZOS)
            buf   = io.BytesIO()
            small.save(buf, format="JPEG", quality=75, optimize=True)
            data  = buf.getvalue()
            if len(data) <= BLUESKY_IMAGE_LIMIT:
                print(f"  Resized+compressed image to {w}x{h}, {len(data):,} bytes")
                return data
        return data
    except Exception as e:
        print(f"  Warning: image compression failed ({e}); posting original")
        return image_bytes


# ── Drive helpers ────────────────────────────────────────────────────────
def claim_file(service, file_id, current_name):
    claimed_name = f"{CLAIM_PREFIX}{RUN_TAG}__{current_name}"
    service.files().update(fileId=file_id, body={"name": claimed_name}).execute()
    check = service.files().get(fileId=file_id, fields="id, name").execute()
    if check.get("name") != claimed_name:
        print(f"Lost claim race on '{current_name}'; skipping.")
        return None
    return claimed_name


def choose_media_kind():
    return random.choices(["image", "video"], weights=[IMAGE_RATIO, VIDEO_RATIO], k=1)[0]


def _download_file(service, file_id, local_path):
    request = service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()


def fetch_latest_media(preferred_kind, allowed_names=None):
    """
    Find the newest unclaimed Drive file matching preferred_kind.

    allowed_names — when set (filename mode), only files whose name (with or
    without extension, lowercased) appear in this set will be considered.
    All other files are left untouched for future runs.
    """
    creds     = get_creds()
    service   = build("drive", "v3", credentials=creds)
    folder_id = get_env("UPLOAD_FOLDER_ID")
    results   = service.files().list(
        q=f"'{folder_id}' in parents",
        orderBy="createdTime desc",
        pageSize=50,
    ).execute()
    files = results.get("files", [])
    if not files:
        print("No files found in upload folder.")
        return None, None, None

    mime_prefix = f"{preferred_kind}/"

    for file in files:
        mime_type     = file.get("mimeType", "")
        original_name = file["name"]

        if original_name.startswith(CLAIM_PREFIX):
            print(f"Skipping '{original_name}' — already claimed.")
            continue

        if not mime_type.startswith(mime_prefix):
            continue   # wrong kind this cycle

        # ── Filename-mode filter ──────────────────────────────────────────
        if allowed_names is not None:
            key_full = original_name.lower()
            key_bare = os.path.splitext(key_full)[0]
            if key_full not in allowed_names and key_bare not in allowed_names:
                print(f"Skipping '{original_name}' — not in filename-caption sheet.")
                continue

        print(f"Found {preferred_kind}: {original_name}")
        claimed_name = claim_file(service, file["id"], original_name)
        if claimed_name is None:
            continue

        print(f"Claimed as '{claimed_name}'.")
        local_path = f"/tmp/{original_name}"
        _download_file(service, file["id"], local_path)

        file["claimed_name"]  = claimed_name
        file["original_name"] = original_name
        file["mime_type"]     = mime_type
        return file, local_path, preferred_kind

    print(f"No unclaimed {preferred_kind} files found.")
    return None, None, None


def move_file(file_id, restore_name=None):
    creds        = get_creds()
    service      = build("drive", "v3", credentials=creds)
    upload_id    = get_env("UPLOAD_FOLDER_ID")
    processed_id = get_env("PROCESSED_FOLDER_ID")
    body         = {"name": restore_name} if restore_name else {}
    service.files().update(
        fileId=file_id,
        addParents=processed_id,
        removeParents=upload_id,
        body=body,
    ).execute()
    print("Moved file to processed folder.")


# ── Post building ────────────────────────────────────────────────────────
MAX_POST_LENGTH    = 300
LOOP_INTERVAL_SECONDS = 3900


def build_post(tags, with_link, media_name=""):
    tb            = TextBuilder()
    caption, cta  = get_caption_for_file(media_name, with_link)

    if caption:
        tb.text(caption)
        tb.text("\n\n")

    if with_link and cta:
        tb.text(cta)
        tb.text("\n")
    if with_link:
        tb.link(LINK_DISPLAY_TEXT, LINK_URL)
        tb.text("\n\n")

    for i, tag in enumerate(tags):
        tb.tag(f"#{tag}", tag)
        if i < len(tags) - 1:
            tb.text(" ")

    return tb


def post_video_to_bluesky(client, video_name, local_path, tags, with_link):
    tb = build_post(tags, with_link, media_name=video_name)
    with open(local_path, "rb") as f:
        video_bytes = f.read()
    client.send_video(text=tb, video=video_bytes, video_alt=video_name)


def post_image_to_bluesky(client, image_name, local_path, tags, with_link):
    tb = build_post(tags, with_link, media_name=image_name)
    with open(local_path, "rb") as f:
        image_bytes = f.read()
    image_bytes = compress_image_bytes(image_bytes)   # ← auto-compress to < 2 MB
    client.send_image(text=tb, image=image_bytes, image_alt=image_name)


def post_to_bluesky(client, media_name, local_path, kind, with_link):
    hashtags_enabled = HASHTAGS_ENABLED_IMAGE if kind == "image" else HASHTAGS_ENABLED_VIDEO
    tags = pick_random_hashtags("hashtags.txt") if hashtags_enabled else []

    if kind == "video":
        post_video_to_bluesky(client, media_name, local_path, tags, with_link)
    else:
        post_image_to_bluesky(client, media_name, local_path, tags, with_link)

    print(f"Posted {kind} (with_link={with_link})")
    if with_link:
        print(f"  Link: {LINK_DISPLAY_TEXT}")
    if tags:
        print(f"  Tags: {' '.join('#'+t for t in tags)}")


def release_claim(file_id, original_name):
    try:
        creds   = get_creds()
        service = build("drive", "v3", credentials=creds)
        service.files().update(fileId=file_id, body={"name": original_name}).execute()
        print(f"Released claim on '{original_name}'.")
    except Exception as e:
        print(f"Warning: failed to release claim: {e}")


# ── Main cycle ───────────────────────────────────────────────────────────
def run_once():
    handle = get_env("BSKY_HANDLE")
    app_pw = get_env("BSKY_APP_PW")
    print_target_account(handle)
    client = Client()
    try:
        client.login(handle, app_pw)
    except Exception as e:
        err = str(e)
        if "AccountTakedown" in err or "AccountSuspended" in err:
            raise AccountTakenDownError(f"Account {handle} taken down / suspended.") from e
        if "AuthenticationRequired" in err or "Invalid identifier or password" in err:
            raise AccountTakenDownError(
                f"Auth failed for {handle} — wrong handle or app password."
            ) from e
        raise

    maybe_generate_daily_follower_report(client, handle)

    # In filename mode: pre-load the map and only fetch Drive files that are in it.
    allowed_names = None
    if CAPTION_MODE == "filename":
        mapping = load_filename_map()
        if not mapping:
            raise NoMediaFoundError(
                "Filename-map sheet is empty — add File Name / Caption rows, then retry."
            )
        allowed_names = set(mapping.keys())
        print(f"Filename mode: {len(allowed_names)//2} files eligible from caption sheet.")

    preferred_kind = choose_media_kind()
    fallback_kind  = "video" if preferred_kind == "image" else "image"

    file, local_path, kind = fetch_latest_media(preferred_kind, allowed_names=allowed_names)
    if not file:
        print(f"No matching {preferred_kind} in Drive; trying {fallback_kind}.")
        file, local_path, kind = fetch_latest_media(fallback_kind, allowed_names=allowed_names)

    if not file:
        raise NoMediaFoundError(
            "No matching files found in Drive upload folder. "
            "Will check again at next scheduled run."
        )

    with_link     = random.random() >= NO_LINK_RATIO
    original_name = file.get("original_name", file["name"])

    try:
        post_to_bluesky(client, original_name, local_path, kind, with_link)
    except Exception as e:
        err = str(e)
        if "AccountTakedown" in err or "AccountSuspended" in err:
            release_claim(file["id"], original_name)
            raise AccountTakenDownError(f"Account {handle} taken down mid-cycle.") from e
        release_claim(file["id"], original_name)
        raise

    move_file(file["id"], restore_name=original_name)
    try:
        os.remove(local_path)
    except OSError:
        pass


def main():
    print_config_summary()
    print(f"Starting loop. Posting every {LOOP_INTERVAL_SECONDS} seconds.")
    while True:
        cycle_start = time.time()
        try:
            run_once()
        except NoMediaFoundError as e:
            print(f"\n{'='*60}\nNO MEDIA: {e}\nStopping — schedule keeps running.\n{'='*60}\n")
            sys.exit(0)
        except AccountTakenDownError as e:
            handle  = get_env("BSKY_HANDLE", required=False) or "unknown"
            err_str = str(e)
            reason  = ("🔑 AUTH FAILED — wrong handle or app password"
                       if "Auth failed" in err_str or "app password" in err_str
                       else "⛔ ACCOUNT TAKEN DOWN / BANNED")
            print(f"\n{'='*60}\n{err_str}\n→ {reason}\n{'='*60}\n")
            log_account_ban_to_sheet(handle, status=reason)
            sys.exit(1)
        except Exception as e:
            print(f"Error during cycle: {e}")

        elapsed    = time.time() - cycle_start
        sleep_for  = max(0, LOOP_INTERVAL_SECONDS - elapsed)
        print(f"Cycle done in {elapsed:.1f}s. Sleeping {sleep_for:.1f}s...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
