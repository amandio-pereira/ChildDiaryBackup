"""Backup ChildDiary posts (text + photos) into month-grouped HTML + photo folders.

Usage:
    pip install -r requirements.txt
    playwright install chromium
    python childdiary_backup.py [--out backup] [--profile-dir pw_profile] [--headless]

First run opens a visible browser and asks for email/password once; the session
is then persisted in --profile-dir so later runs skip login entirely.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import getpass
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

if getattr(sys, "frozen", False):
    # Without this, the bundled Node driver picks its own browsers path
    # (observed: a .local-browsers folder inside the frozen app itself),
    # which doesn't reliably match where launch_persistent_context looks
    # afterwards -- causing "Executable doesn't exist" every single run.
    # Pinning both sides to the same explicit, writable, persistent folder
    # fixes it and makes the download actually stick between runs.
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ChildDiaryBackupBrowsers"),
    )

from playwright.sync_api import sync_playwright

BASE_URL = "https://app.childdiary.net"
PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Windows file time helpers (creation time is Windows-only; mtime is set on
# every OS via os.utime as a portable fallback).
# ---------------------------------------------------------------------------

def _datetime_to_filetime(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch_as_filetime = 116444736000000000  # 1970-01-01 expressed as FILETIME
    return int(dt.timestamp() * 10_000_000) + epoch_as_filetime


def set_file_times(path: Path, dt: datetime) -> None:
    ts = dt.timestamp()
    try:
        os.utime(path, (ts, ts))
    except OSError:
        pass

    if sys.platform != "win32":
        return

    # Creation time has no cross-platform equivalent; set it natively via
    # SetFileTime since os.utime only touches access/modified time on Windows.
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.SetFileTime.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]

    handle = kernel32.CreateFileW(
        str(path), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle == wintypes.HANDLE(-1).value or handle is None:
        return

    filetime_int = _datetime_to_filetime(dt)
    ft = wintypes.FILETIME(filetime_int & 0xFFFFFFFF, filetime_int >> 32)
    try:
        kernel32.SetFileTime(handle, ctypes.byref(ft), None, ctypes.byref(ft))
    finally:
        kernel32.CloseHandle(handle)


def parse_dt(value: str) -> datetime:
    value = value.replace("Z", "+00:00")
    # Python 3.9's fromisoformat only accepts 3 or 6 fractional digits; the
    # API sends a variable number (e.g. ".8"), so pad/truncate to 6 first.
    value = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], value)
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Browser launch (auto-installs Chromium on first run of the frozen exe,
# since that build doesn't bundle it -- see build_exe.ps1)
# ---------------------------------------------------------------------------

def install_chromium() -> None:
    from playwright._impl._driver import compute_driver_executable

    print("A descarregar Chromium (so na 1a vez, ~150MB, precisa internet)...")
    driver_executable, driver_cli = compute_driver_executable()
    result = subprocess.run([str(driver_executable), str(driver_cli), "install", "chromium"])
    if result.returncode != 0:
        raise RuntimeError("Falha ao instalar Chromium (driver Playwright)")


def launch_context(pw, profile_dir: str, headless: bool):
    try:
        return pw.chromium.launch_persistent_context(profile_dir, headless=headless)
    except Exception as exc:
        if "Executable doesn't exist" not in str(exc):
            raise
        install_chromium()
        return pw.chromium.launch_persistent_context(profile_dir, headless=headless)


# ---------------------------------------------------------------------------
# ChildDiary auth + fetch
# ---------------------------------------------------------------------------

def ensure_logged_in(page) -> None:
    page.goto(BASE_URL, wait_until="networkidle")
    if page.query_selector('input[type="password"]') is None:
        return  # already authenticated (persisted profile)

    print("Login necessario.")
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    page.fill('input[type="email"]', email)
    page.fill('input[type="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url("**/Main*", timeout=30000)


def fetch_all_entries(context) -> list[dict]:
    # type=all mirrors the "Tudo" tab and covers every entry kind (Posts,
    # Stories/"Registos", Events, ...) — filtering to type=Posts alone misses
    # "Registos" entries, which is where most photos actually live.
    entries: list[dict] = []
    seen_ids: set[str] = set()

    pinned_resp = context.request.get(f"{BASE_URL}/api/Entries/GetPinnedEntries?type=all")
    pinned_data = pinned_resp.json() if pinned_resp.ok else []
    pinned_list = pinned_data.get("Entries", pinned_data) if isinstance(pinned_data, dict) else pinned_data
    for entry in pinned_list or []:
        if entry["Id"] not in seen_ids:
            entries.append(entry)
            seen_ids.add(entry["Id"])

    page_num = 0
    while True:
        resp = context.request.get(
            f"{BASE_URL}/api/Entries?count={PAGE_SIZE}&page={page_num}&type=all"
        )
        if not resp.ok:
            raise RuntimeError(f"GET /api/Entries page={page_num} falhou: HTTP {resp.status}")
        data = resp.json()
        for entry in data["Entries"]:
            if entry["Id"] not in seen_ids:
                entries.append(entry)
                seen_ids.add(entry["Id"])
        print(f"  pagina {page_num}: {len(data['Entries'])} entradas")
        if not data["Entries"] or not data.get("HasMorePages"):
            break
        page_num += 1

    return entries


def extract_content(entry: dict) -> tuple[str | None, str]:
    # Different entry Types shape their content differently:
    # - Posts (Type 1): flat Text field (HTML).
    # - Stories/"Registos" (Type 3): a Boxes list with Type in {Title, Media, Text}.
    # - Events (Type 5): Title + Description (HTML), no Boxes.
    if entry.get("Text"):
        return entry.get("Title"), entry["Text"]

    boxes = entry.get("Boxes") or []
    if boxes:
        title = None
        text_parts = []
        for box in boxes:
            box_type = box.get("Type")
            if box_type == "Title":
                title = box.get("Text")
            elif box_type == "Text":
                text_parts.append(box.get("Text") or "")
        return title, "".join(text_parts)

    return entry.get("Title"), entry.get("Description") or ""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "webm", "m4v", "3gp"}


def render_media_tag(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in VIDEO_EXTENSIONS:
        return f'<video controls src="{path}"></video>'
    return f'<img src="{path}" loading="lazy">'


def describe_comment(comment: dict) -> tuple[str, str, str]:
    # Confirmed schema: Creator.Description + Text. Extra key fallbacks kept
    # as a safety net (dump raw instead of silently dropping data) in case
    # some entry Type shapes comments differently.
    text = comment.get("Text") or comment.get("Comment") or comment.get("Content")
    author = None
    for key in ("Creator", "Author", "User"):
        if isinstance(comment.get(key), dict):
            author = comment[key].get("Description") or comment[key].get("Name")
            break
    date = comment.get("CreatedOn") or comment.get("DisplayDate") or comment.get("Date")
    if text is None or author is None:
        return ("(desconhecido)", json.dumps(comment, ensure_ascii=False), date or "")
    return (author, text, date or "")


HTML_HEAD = """<!doctype html>
<html lang="pt">
<head>
<meta charset="utf-8">
<title>ChildDiary backup - {month}</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #f2efe9; margin: 0; padding: 24px; }}
  .container {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 1.3rem; color: #333; }}
  .entry {{ background: #fff; border-radius: 10px; padding: 16px 20px; margin-bottom: 18px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.12); }}
  .meta {{ color: #888; font-size: 0.85rem; margin-bottom: 8px; }}
  .author {{ font-weight: 600; color: #b5442e; }}
  .title {{ font-weight: 600; margin-bottom: 6px; }}
  .tags {{ margin-bottom: 8px; }}
  .tag {{ display: inline-block; background: #f2efe9; color: #888; font-size: 0.75rem;
          border-radius: 10px; padding: 2px 8px; margin-right: 4px; }}
  .text {{ line-height: 1.4; margin-bottom: 10px; }}
  .text p {{ margin: 0 0 8px; }}
  .photos {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }}
  .photos img, .photos video {{ max-width: 220px; max-height: 220px; border-radius: 6px; object-fit: cover; }}
  .reactions {{ color: #b5442e; font-size: 0.85rem; margin-bottom: 8px; }}
  .comments {{ border-top: 1px solid #eee; padding-top: 8px; }}
  .comment {{ font-size: 0.9rem; margin-bottom: 6px; }}
  .comment .author {{ font-size: 0.9rem; }}
</style>
</head>
<body>
<div class="container">
<h1>ChildDiary — {month}</h1>
"""

HTML_TAIL = """</div>
</body>
</html>
"""


def render_entry_html(entry: dict, photo_filenames: list[str], photo_dir: str = "photos") -> str:
    author = html.escape(entry.get("Creator", {}).get("Description") or "?")
    date_str = html.escape(parse_dt(entry["DisplayDate"]).strftime("%d/%m/%Y %H:%M"))
    title, body = extract_content(entry)
    # body is already HTML (<p>, <br>, <span style=...> from the rich text
    # editor) — escaping it would show the raw tags instead of rendering them.
    title_html = f'<div class="title">{html.escape(title)}</div>' if title else ""
    text = body
    recipients = entry.get("For") or []
    recipients_str = ", ".join(html.escape(r.get("Description", "")) for r in recipients if r.get("Description"))
    categories = entry.get("Categories") or []
    categories_str = " ".join(
        f'<span class="tag">{html.escape(c.get("Description", ""))}</span>' for c in categories if c.get("Description")
    )

    photos_html = ""
    if photo_filenames:
        imgs = "".join(
            render_media_tag(f"{photo_dir}/{html.escape(name)}") for name in photo_filenames
        )
        photos_html = f'<div class="photos">{imgs}</div>'

    reactions = entry.get("Reactions") or []
    reactions_html = f'<div class="reactions">Gosto: {len(reactions)}</div>' if reactions else ""

    comments = entry.get("Comments") or []
    comments_html = ""
    if comments:
        rows = []
        for c in comments:
            c_author, c_text, c_date = describe_comment(c)
            rows.append(
                f'<div class="comment"><span class="author">{html.escape(c_author)}</span> '
                f'&mdash; {html.escape(str(c_text))}</div>'
            )
        comments_html = f'<div class="comments">{"".join(rows)}</div>'

    subtitle = f' &middot; Para: {recipients_str}' if recipients_str else ""
    tags_html = f'<div class="tags">{categories_str}</div>' if categories_str else ""

    return (
        f'<div class="entry">'
        f'<div class="meta"><span class="author">{author}</span>{subtitle} &middot; {date_str}</div>'
        f'{title_html}{tags_html}'
        f'<div class="text">{text}</div>'
        f'{photos_html}{reactions_html}{comments_html}'
        f'</div>\n'
    )


ROOT_HTML = """<!doctype html>
<html lang="pt">
<head>
<meta charset="utf-8">
<title>ChildDiary backup</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #f2efe9; margin: 0; padding: 24px; }}
  .container {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 1.3rem; color: #333; }}
  .entry {{ background: #fff; border-radius: 10px; padding: 16px 20px; margin-bottom: 18px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.12); }}
  .meta {{ color: #888; font-size: 0.85rem; margin-bottom: 8px; }}
  .author {{ font-weight: 600; color: #b5442e; }}
  .title {{ font-weight: 600; margin-bottom: 6px; }}
  .tags {{ margin-bottom: 8px; }}
  .tag {{ display: inline-block; background: #f2efe9; color: #888; font-size: 0.75rem;
          border-radius: 10px; padding: 2px 8px; margin-right: 4px; }}
  .text {{ line-height: 1.4; margin-bottom: 10px; }}
  .text p {{ margin: 0 0 8px; }}
  .photos {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }}
  .photos img, .photos video {{ max-width: 220px; max-height: 220px; border-radius: 6px; object-fit: cover; }}
  .reactions {{ color: #b5442e; font-size: 0.85rem; margin-bottom: 8px; }}
  .comments {{ border-top: 1px solid #eee; padding-top: 8px; }}
  .comment {{ font-size: 0.9rem; margin-bottom: 6px; }}
  .comment .author {{ font-size: 0.9rem; }}

  .months-nav {{ margin-bottom: 16px; font-size: 0.9rem; }}
  .months-nav a {{ margin-right: 10px; color: #b5442e; text-decoration: none; }}
  .months-nav a:hover {{ text-decoration: underline; }}
  .tabs {{ margin-bottom: 12px; }}
  .tab-btn {{ background: none; border: none; border-bottom: 2px solid transparent; padding: 8px 4px;
              margin-right: 16px; font-size: 1rem; color: #888; cursor: pointer; }}
  .tab-btn.active {{ color: #b5442e; border-bottom-color: #b5442e; font-weight: 600; }}
  .photos-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; }}
  .photos-grid img, .photos-grid video {{ width: 100%; height: 160px; object-fit: cover; border-radius: 6px; }}
</style>
</head>
<body>
<div class="container">
<h1>ChildDiary — backup completo</h1>
<div class="months-nav">{months_nav}</div>
<div class="tabs">
  <button id="tab-feed-btn" class="tab-btn active" onclick="showTab('feed')">Mensagens</button>
  <button id="tab-photos-btn" class="tab-btn" onclick="showTab('photos')">Fotos</button>
</div>
<div id="tab-feed">
  <div id="feed"></div>
  <div id="feed-sentinel"></div>
</div>
<div id="tab-photos" style="display:none">
  <div id="photos-grid" class="photos-grid"></div>
  <div id="photos-sentinel"></div>
</div>
</div>
<script>
const ENTRIES = {entries_json};
const PHOTOS = {photos_json};

function makeInfiniteRenderer(items, container, batch) {{
  let shown = 0;
  return function renderMore() {{
    const next = items.slice(shown, shown + batch);
    for (const html of next) container.insertAdjacentHTML('beforeend', html);
    shown += next.length;
  }};
}}

const renderMoreEntries = makeInfiniteRenderer(ENTRIES, document.getElementById('feed'), 20);
const renderMorePhotos = makeInfiniteRenderer(PHOTOS, document.getElementById('photos-grid'), 40);

new IntersectionObserver((es) => {{ if (es[0].isIntersecting) renderMoreEntries(); }})
  .observe(document.getElementById('feed-sentinel'));
renderMoreEntries();

let photosInited = false;
function showTab(name) {{
  document.getElementById('tab-feed').style.display = name === 'feed' ? '' : 'none';
  document.getElementById('tab-photos').style.display = name === 'photos' ? '' : 'none';
  document.getElementById('tab-feed-btn').classList.toggle('active', name === 'feed');
  document.getElementById('tab-photos-btn').classList.toggle('active', name === 'photos');
  if (name === 'photos' && !photosInited) {{
    photosInited = true;
    renderMorePhotos();
    new IntersectionObserver((es) => {{ if (es[0].isIntersecting) renderMorePhotos(); }})
      .observe(document.getElementById('photos-sentinel'));
  }}
}}
</script>
</body>
</html>
"""


def generate_root_index(
    out_dir: Path,
    entries_html_desc: list[str],
    photos_html_desc: list[str],
    month_keys_asc: list[str],
) -> None:
    months_nav = " ".join(
        f'<a href="{m}/index.html">{m}</a>' for m in sorted(month_keys_asc, reverse=True)
    )
    doc = ROOT_HTML.format(
        months_nav=months_nav,
        entries_json=json.dumps(entries_html_desc),
        photos_json=json.dumps(photos_html_desc),
    )
    (out_dir / "index.html").write_text(doc, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def download_media_to_file(url: str, dest: Path) -> bool:
    # Plain urllib, not context.request: some Medias are videos of several
    # hundred MB, and Playwright's Node driver marshals response bodies as
    # base64 strings, which blows past V8's ~512MB max string length for
    # large files. The Url is already SAS-signed, so no session/cookies
    # needed -- a stream straight to disk avoids both problems.
    tmp_dest = dest.with_suffix(dest.suffix + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp, open(tmp_dest, "wb") as f:
            shutil.copyfileobj(resp, f, length=1024 * 1024)
        tmp_dest.replace(dest)
        return True
    except (urllib.error.URLError, OSError) as exc:
        print(f"  [aviso] falha ao descarregar media: {exc}")
        tmp_dest.unlink(missing_ok=True)
        return False


def download_photos(entry: dict, month_dir: Path) -> list[str]:
    filenames = []
    entry_dt = parse_dt(entry["DisplayDate"])
    photos_dir = month_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)

    for media in entry.get("Medias") or []:
        ext = (media.get("Extension") or ".jpg").lstrip(".")
        filename = f'{entry["Id"]}_{media["Id"]}.{ext}'
        dest = photos_dir / filename
        if dest.exists():
            filenames.append(filename)
            continue
        if not download_media_to_file(media["Url"], dest):
            continue
        set_file_times(dest, entry_dt)
        filenames.append(filename)

    return filenames


def main() -> None:
    # Windows consoles often default to cp1252, which cannot encode emoji or
    # the arrows Playwright puts in its error call logs -- without this, an
    # error containing either would crash while trying to *print* the error.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="backup", help="pasta de destino do backup")
    parser.add_argument("--profile-dir", default="pw_profile", help="pasta do perfil persistente do Chromium")
    parser.add_argument("--headless", action="store_true", help="corre sem janela visivel (so depois do 1o login)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    root_entries: list[tuple[datetime, str]] = []
    root_photos: list[tuple[datetime, str]] = []
    processed_months: set[str] = set()
    interrupted = False

    with sync_playwright() as pw:
        context = launch_context(pw, args.profile_dir, args.headless)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            ensure_logged_in(page)

            print("A obter lista de mensagens...")
            entries = fetch_all_entries(context)
            print(f"Total: {len(entries)} entradas")

            by_month: dict[str, list[dict]] = {}
            for entry in entries:
                month_key = parse_dt(entry["DisplayDate"]).strftime("%Y-%m")
                by_month.setdefault(month_key, []).append(entry)

            for month_key, month_entries in sorted(by_month.items()):
                month_entries.sort(key=lambda e: e["DisplayDate"])
                month_dir = out_dir / month_key
                month_dir.mkdir(parents=True, exist_ok=True)

                print(f"Mes {month_key}: {len(month_entries)} entradas")
                body_parts = []
                for entry in month_entries:
                    photo_filenames = download_photos(entry, month_dir)
                    body_parts.append(render_entry_html(entry, photo_filenames))

                    entry_dt = parse_dt(entry["DisplayDate"])
                    root_entries.append(
                        (entry_dt, render_entry_html(entry, photo_filenames, photo_dir=f"{month_key}/photos"))
                    )
                    for name in photo_filenames:
                        path = f"{month_key}/photos/{html.escape(name)}"
                        tag = render_media_tag(path)
                        img_html = tag if path.rsplit(".", 1)[-1].lower() in VIDEO_EXTENSIONS else f'<a href="{path}" target="_blank">{tag}</a>'
                        root_photos.append((entry_dt, img_html))

                html_doc = HTML_HEAD.format(month=month_key) + "".join(body_parts) + HTML_TAIL
                (month_dir / "index.html").write_text(html_doc, encoding="utf-8")
                processed_months.add(month_key)
        except Exception as exc:
            interrupted = True
            print(f"\n[erro] Backup interrompido: {exc}")
            if "disposed" in str(exc) or "closed" in str(exc):
                print("Parece que o browser/janela foi fechado a meio da corrida.")
            print("Nao fechar a janela do Chromium ate aparecer 'Backup concluido'.")
            print("O script e resumivel: corre outra vez, fotos ja descarregadas nao repetem.")
        finally:
            try:
                context.close()
            except Exception:
                pass

    if root_entries or root_photos:
        root_entries.sort(key=lambda pair: pair[0], reverse=True)
        root_photos.sort(key=lambda pair: pair[0], reverse=True)
        generate_root_index(
            out_dir,
            [html_str for _, html_str in root_entries],
            [html_str for _, html_str in root_photos],
            sorted(processed_months),
        )

    if interrupted:
        sys.exit(1)

    print(f"Backup concluido em {out_dir.resolve()}")


if __name__ == "__main__":
    main()
