#!/usr/bin/env python3
"""
Gradino Studio content robot (repo: gradino-content).

Publishes due jobs from queue/ to Instagram through Meta's Graph API.
The host is picked by the token: graph.instagram.com (Instagram login)
or graph.facebook.com (Unified login / Facebook Login for Business).
Standard library only, so the GitHub runner needs no installs.

Commands
  validate   check every job in queue/ (files, JPEG, sizes, caption, date)
  check      verify the token, show the account and the publishing quota
  publish    publish every due job (needs repo variable PUBLISHING_ENABLED=true)
  refresh    refresh the long-lived token; prints ONLY the new token to stdout

Env
  IG_TOKEN            Instagram user access token (secret)
  IG_USER_ID          Instagram account ID; required for graph.facebook.com tokens
  PUBLISHING_ENABLED  "true" to allow real publishing
  GRAPH_VERSION       default v26.0
  GRAPH_HOST          optional; force https://graph.instagram.com or https://graph.facebook.com
  MEDIA_BASE_URL      optional override for public file URLs
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
PUBLISHED = ROOT / "published"
FAILED = ROOT / "failed"

GRAPH = "https://graph.instagram.com"
VERSION = os.environ.get("GRAPH_VERSION", "v26.0")
TZ = ZoneInfo("Europe/Berlin")

MAX_ATTEMPTS = 3
MAX_JOBS_PER_RUN = 5
IMAGE_MAX_BYTES = 8 * 1024 * 1024
VIDEO_MAX_BYTES = 95 * 1024 * 1024  # GitHub rejects files over 100 MB
CAPTION_MAX = 2200
HASHTAGS_MAX = 30
TYPES = {"image", "carousel", "story", "reel"}
IMAGE_EXT = {".jpg", ".jpeg"}
VIDEO_EXT = {".mp4", ".mov"}
LATIN = re.compile(r"[A-Za-z]")
CYRILLIC = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- validation

def jpeg_size(path: Path):
    """Return (width, height) of a JPEG from its SOF marker, or None."""
    with path.open("rb") as f:
        if f.read(2) != b"\xff\xd8":
            return None
        while True:
            b = f.read(1)
            while b and b != b"\xff":
                b = f.read(1)
            while b == b"\xff":
                b = f.read(1)
            if not b:
                return None
            marker = b[0]
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            seglen = int.from_bytes(f.read(2), "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                f.read(1)
                h = int.from_bytes(f.read(2), "big")
                w = int.from_bytes(f.read(2), "big")
                return w, h
            f.seek(seglen - 2, 1)


def parse_time(value):
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def validate(job: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one job."""
    errors, warnings = [], []
    kind = job.get("type")
    files = job.get("files") or []
    if kind not in TYPES:
        errors.append(f"type must be one of {sorted(TYPES)}, got {kind!r}")
    if not isinstance(files, list) or not files:
        errors.append("files must be a non-empty list of repo paths")
        files = []

    expected = {"image": (1, 1), "story": (1, 1), "reel": (1, 1), "carousel": (2, 10)}
    if kind in expected:
        lo, hi = expected[kind]
        if not lo <= len(files) <= hi:
            errors.append(f"{kind} needs {lo}-{hi} files, got {len(files)}")

    ratios = []
    for rel in files:
        p = ROOT / rel
        ext = p.suffix.lower()
        if not p.is_file():
            errors.append(f"missing file: {rel}")
            continue
        size = p.stat().st_size
        if ext in IMAGE_EXT:
            if kind == "reel":
                errors.append(f"reel needs a video, got image {rel}")
            dims = jpeg_size(p)
            if dims is None:
                errors.append(f"not a real JPEG (Instagram accepts JPEG only): {rel}")
                continue
            if size > IMAGE_MAX_BYTES:
                errors.append(f"image over 8 MB: {rel} ({size / 1e6:.1f} MB)")
            w, h = dims
            ratios.append(w / h)
            if kind in ("image", "carousel") and not 0.79 <= w / h <= 1.92:
                errors.append(f"feed image ratio {w}x{h} outside 4:5 … 1.91:1: {rel}")
            if kind == "story" and abs(w / h - 9 / 16) > 0.02:
                warnings.append(f"story is {w}x{h}, recommended 1080x1920: {rel}")
        elif ext in VIDEO_EXT:
            if kind == "image":
                errors.append(f"image post needs a JPEG, got video {rel}")
            if size > VIDEO_MAX_BYTES:
                errors.append(f"video over 95 MB (GitHub limit): {rel}")
        else:
            errors.append(f"unsupported file type {ext}: {rel} (use .jpg or .mp4)")

    if kind == "carousel" and len(set(round(r, 2) for r in ratios)) > 1:
        warnings.append("carousel images differ in ratio; Instagram crops all to the first one")

    caption = job.get("caption") or ""
    if kind == "story" and caption:
        warnings.append("stories have no caption; it will be ignored")
    if len(caption) > CAPTION_MAX:
        errors.append(f"caption is {len(caption)} chars, max {CAPTION_MAX}")
    tags = re.findall(r"#(\w+)", caption)
    if len(tags) > HASHTAGS_MAX:
        errors.append(f"{len(tags)} hashtags, max {HASHTAGS_MAX}")
    mixed = [t for t in tags if LATIN.search(t) and CYRILLIC.search(t)]
    if mixed:
        errors.append("hashtags mix Latin and Cyrillic and get cut off: "
                      + ", ".join("#" + t for t in mixed))

    cover = job.get("cover")
    if cover:
        if kind != "reel":
            warnings.append("cover is used only for reels")
        cp = ROOT / cover
        if not cp.is_file() or jpeg_size(cp) is None:
            errors.append(f"cover must be an existing JPEG: {cover}")

    try:
        parse_time(job.get("publish_at"))
    except ValueError:
        errors.append(f"publish_at is not ISO 8601: {job.get('publish_at')!r}")
    return errors, warnings


def load_jobs():
    jobs = []
    for path in sorted(QUEUE.glob("*.json")):
        try:
            jobs.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except json.JSONDecodeError as e:
            jobs.append((path, {"_parse_error": str(e)}))
    return jobs


def cmd_validate(_args) -> int:
    jobs = load_jobs()
    bad = 0
    for path, job in jobs:
        if "_parse_error" in job:
            log(f"✗ {path.name}: invalid JSON: {job['_parse_error']}")
            bad += 1
            continue
        errors, warnings = validate(job)
        when = parse_time(job.get("publish_at")).astimezone(TZ) if not errors else "?"
        mark = "✗" if errors else "✓"
        log(f"{mark} {path.name}  [{job.get('type')}] {len(job.get('files') or [])} file(s)  at {when}")
        for e in errors:
            log(f"    error: {e}")
        for w in warnings:
            log(f"    warn:  {w}")
        bad += bool(errors)
    log(f"{len(jobs)} job(s), {bad} with errors")
    return 1 if bad else 0


# ----------------------------------------------------------------------- API

class ApiError(Exception):
    def __init__(self, err: dict):
        self.err = err
        super().__init__(f"{err.get('type', 'Error')} {err.get('code', '')}: "
                         f"{err.get('error_user_msg') or err.get('message', '')}")


def token() -> str:
    t = os.environ.get("IG_TOKEN", "").strip()
    if not t:
        sys.exit("IG_TOKEN secret is empty. Add it in Settings → Secrets and variables → Actions.")
    return t


HOSTS = ["https://graph.instagram.com", "https://graph.facebook.com"]
_BASE: str | None = None


def api_base() -> str:
    """Pick the Graph host that accepts the token.

    Dashboard tokens from 'API setup with Instagram login' work on
    graph.instagram.com; tokens from Meta's newer Unified login
    (Facebook Login for Business) work on graph.facebook.com.
    GRAPH_HOST forces one host.
    """
    global _BASE
    if _BASE:
        return _BASE
    forced = os.environ.get("GRAPH_HOST", "").strip()
    last = None
    for host in ([forced] if forced else HOSTS):
        base = f"{host.rstrip('/')}/{VERSION}"
        try:
            api("GET", "me", {"fields": "id"}, base=base)
        except ApiError as e:
            last = e
            continue
        _BASE = base
        log(f"token accepted by {host}")
        return base
    raise ApiError({"message": f"token rejected by {', '.join(HOSTS)}: {last}"})


def on_instagram_host() -> bool:
    return api_base().startswith("https://graph.instagram.com")


def api(method: str, path: str, params: dict | None = None, base: str | None = None):
    url = f"{base or api_base()}/{path}"
    headers = {"Authorization": f"Bearer {token()}"}
    data = None
    if method == "GET" and params:
        url += "?" + urllib.parse.urlencode(params)
    elif params:
        data = urllib.parse.urlencode(params).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(body).get("error", {})
            except json.JSONDecodeError:
                err = {"message": body[:300]}
            if e.code >= 500 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise ApiError(err) from None
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise ApiError({"message": f"network: {e.reason}"}) from None


def ig_user_id() -> str:
    uid = os.environ.get("IG_USER_ID", "").strip()
    if uid:
        return uid
    if on_instagram_host():
        return str(api("GET", "me", {"fields": "user_id"})["user_id"])
    sys.exit("IG_USER_ID secret is empty. With a graph.facebook.com token the robot needs "
             "the Instagram account ID (App Dashboard → Instagram API → Instagram-Konto).")


def public_url(rel: str) -> str:
    base = os.environ.get("MEDIA_BASE_URL")
    if not base:
        repo = os.environ.get("GITHUB_REPOSITORY")
        sha = os.environ.get("GITHUB_SHA", "main")
        if not repo:
            sys.exit("GITHUB_REPOSITORY is not set; run inside GitHub Actions or set MEDIA_BASE_URL")
        base = f"https://raw.githubusercontent.com/{repo}/{sha}"
    return base.rstrip("/") + "/" + urllib.parse.quote(rel)


def check_public(url: str):
    """Make sure Instagram will be able to download the file."""
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.headers.get("Content-Type", "")
        except urllib.error.URLError as e:
            last = e
            time.sleep(10)
    raise ApiError({"message": f"file is not publicly reachable: {url} ({last})"})


def wait_ready(container_id: str, video: bool) -> None:
    deadline = time.time() + (15 * 60 if video else 5 * 60)
    time.sleep(5)
    while True:
        info = api("GET", container_id, {"fields": "status_code,status"})
        code = info.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            return
        if code in ("ERROR", "EXPIRED"):
            raise ApiError({"message": f"container {code}: {info.get('status', '')}"})
        if time.time() > deadline:
            raise ApiError({"message": f"container still {code} after waiting"})
        time.sleep(30 if video else 15)


def media_params(rel: str) -> dict:
    url = public_url(rel)
    check_public(url)
    if Path(rel).suffix.lower() in VIDEO_EXT:
        return {"video_url": url}
    return {"image_url": url}


def is_video(rel: str) -> bool:
    return Path(rel).suffix.lower() in VIDEO_EXT


def create_container(uid: str, job: dict) -> tuple[str, bool]:
    kind, files = job["type"], job["files"]
    caption = job.get("caption") or ""
    alt = job.get("alt_text")
    if kind == "image":
        p = media_params(files[0]) | {"caption": caption}
        if alt:
            p["alt_text"] = alt
        cid = api("POST", f"{uid}/media", p)["id"]
        wait_ready(cid, video=False)
        return cid, False
    if kind == "story":
        p = media_params(files[0]) | {"media_type": "STORIES"}
        cid = api("POST", f"{uid}/media", p)["id"]
        wait_ready(cid, video=is_video(files[0]))
        return cid, is_video(files[0])
    if kind == "reel":
        p = media_params(files[0]) | {"media_type": "REELS", "caption": caption,
                                      "share_to_feed": "true"}
        if job.get("cover"):
            p["cover_url"] = public_url(job["cover"])
        cid = api("POST", f"{uid}/media", p)["id"]
        wait_ready(cid, video=True)
        return cid, True
    # carousel
    children = []
    for rel in files:
        p = media_params(rel) | {"is_carousel_item": "true"}
        if is_video(rel):
            p["media_type"] = "VIDEO"
        cid = api("POST", f"{uid}/media", p)["id"]
        wait_ready(cid, video=is_video(rel))
        children.append(cid)
        log(f"    item {len(children)}/{len(files)} ready")
    cid = api("POST", f"{uid}/media", {"media_type": "CAROUSEL",
                                       "children": ",".join(children),
                                       "caption": caption})["id"]
    wait_ready(cid, video=False)
    return cid, False


def recent_duplicate(uid: str, caption: str):
    """Find a feed post with the same caption from the last 48 h."""
    if not caption.strip():
        return None
    data = api("GET", f"{uid}/media", {"fields": "id,caption,timestamp,permalink", "limit": 10})
    since = datetime.now(timezone.utc) - timedelta(hours=48)
    for m in data.get("data", []):
        ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        if ts >= since and (m.get("caption") or "").strip() == caption.strip():
            return m
    return None


def publish_job(uid: str, job: dict) -> dict:
    if job["type"] != "story":
        dup = recent_duplicate(uid, job.get("caption") or "")
        if dup:
            log("    already on Instagram (same caption in last 48 h), not posting again")
            return {"ig_media_id": dup["id"], "permalink": dup.get("permalink"),
                    "note": "duplicate guard: found existing post"}
    cid, _ = create_container(uid, job)
    try:
        media_id = api("POST", f"{uid}/media_publish", {"creation_id": cid})["id"]
    except ApiError:
        status = api("GET", cid, {"fields": "status_code"}).get("status_code")
        if status != "PUBLISHED":
            raise
        log("    publish call errored but container is PUBLISHED; treating as success")
        latest = api("GET", f"{uid}/media", {"fields": "id", "limit": 1}).get("data", [{}])
        media_id = latest[0].get("id", "unknown") if latest else "unknown"
    link = None
    if media_id != "unknown":
        try:
            link = api("GET", media_id, {"fields": "permalink"}).get("permalink")
        except ApiError:
            pass
    return {"ig_media_id": media_id, "permalink": link}


# ------------------------------------------------------------------- git ops

def git(*args):
    subprocess.run(["git", *args], cwd=ROOT, check=True)


def commit(message: str):
    if not os.environ.get("GITHUB_ACTIONS"):
        log(f"(local run, not committing: {message})")
        return
    git("add", "-A", "queue", "published", "failed")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git("commit", "-m", message)
    for attempt in range(4):
        if subprocess.run(["git", "push"], cwd=ROOT).returncode == 0:
            return
        git("fetch", "--depth=50", "origin", "main")
        git("rebase", "origin/main")
    raise RuntimeError("git push failed 4 times")


def move(path: Path, job: dict, folder: Path):
    folder.mkdir(exist_ok=True)
    (folder / path.name).write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
    path.unlink()


# ------------------------------------------------------------------ commands

def cmd_check(_args) -> int:
    try:
        base = api_base()
    except ApiError as e:
        log(f"✗ {e}")
        return 1
    me = {}
    if on_instagram_host():
        me = api("GET", "me", {"fields": "user_id,username,account_type,media_count"})
    uid = ig_user_id()
    acct = api("GET", uid, {"fields": "username,media_count"})
    log(f"token OK · host {base} · @{acct.get('username')} · "
        f"{me.get('account_type', 'type n/a on this host')} · "
        f"{acct.get('media_count')} posts · IG user id {uid}")
    try:
        lim = api("GET", f"{uid}/content_publishing_limit",
                  {"fields": "quota_usage,config"})
        row = (lim.get("data") or [{}])[0]
        log(f"publishing quota used: {row.get('quota_usage')} / "
            f"{(row.get('config') or {}).get('quota_total', '?')} in 24 h")
    except ApiError as e:
        log(f"quota check failed: {e}")
    if me.get("account_type") not in ("BUSINESS", None):
        log("note: stories through the API need a Business account")
    enabled = os.environ.get("PUBLISHING_ENABLED", "").lower() == "true"
    log(f"PUBLISHING_ENABLED = {enabled}")
    return cmd_validate(_args)


def cmd_publish(_args) -> int:
    now = datetime.now(timezone.utc)
    jobs = load_jobs()
    due = [(p, j) for p, j in jobs
           if "_parse_error" in j or validate(j)[0] or not j.get("publish_at")
           or parse_time(j.get("publish_at")) <= now]
    log(f"{len(jobs)} in queue, {len(due)} due")
    if not due:
        return 0
    if os.environ.get("PUBLISHING_ENABLED", "").lower() != "true":
        log("PUBLISHING_ENABLED is not 'true' — nothing posted. "
            "Set it in Settings → Secrets and variables → Actions → Variables.")
        return 0

    uid = ig_user_id()
    failures = 0
    for path, job in due[:MAX_JOBS_PER_RUN]:
        log(f"→ {path.name} [{job.get('type')}]")
        errors = [job["_parse_error"]] if "_parse_error" in job else validate(job)[0]
        if errors:
            job["error"] = errors
            move(path, job, FAILED)
            commit(f"robot: {path.name} failed validation")
            log("    ✗ invalid: " + "; ".join(errors))
            failures += 1
            continue
        try:
            result = publish_job(uid, job)
            job["result"] = result | {"published_at": datetime.now(TZ).isoformat(timespec="seconds")}
            move(path, job, PUBLISHED)
            commit(f"robot: published {path.name}")
            log(f"    ✓ {result.get('permalink') or result.get('ig_media_id')}")
        except ApiError as e:
            job["attempts"] = int(job.get("attempts", 0)) + 1
            job["last_error"] = str(e)
            if job["attempts"] >= MAX_ATTEMPTS:
                move(path, job, FAILED)
                commit(f"robot: {path.name} failed after {MAX_ATTEMPTS} attempts")
            else:
                path.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
                commit(f"robot: {path.name} attempt {job['attempts']} failed")
            log(f"    ✗ {e}")
            failures += 1
    return 1 if failures else 0


def cmd_refresh(_args) -> int:
    try:
        if not on_instagram_host():
            log("token works on graph.facebook.com (Unified login); ig_refresh_token does not "
                "apply. Check the expiry in Meta's Access Token Debugger.")
            return 0
    except ApiError as e:
        log(f"refresh skipped, token rejected: {e}")
        return 1
    url = f"{GRAPH}/refresh_access_token?" + urllib.parse.urlencode(
        {"grant_type": "ig_refresh_token", "access_token": token()})
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error", {}).get("message", "")
        except json.JSONDecodeError:
            msg = body[:200]
        log(f"refresh failed: HTTP {e.code} {msg}")
        return 1
    new = data.get("access_token")
    if not new:
        log("refresh failed: no access_token in response")
        return 1
    days = int(data.get("expires_in", 0)) // 86400
    log(f"token refreshed, valid for {days} days")
    print(new)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["validate", "check", "publish", "refresh"])
    args = ap.parse_args()
    return {"validate": cmd_validate, "check": cmd_check,
            "publish": cmd_publish, "refresh": cmd_refresh}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
