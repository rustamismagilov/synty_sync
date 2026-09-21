#!/usr/bin/env python3
from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from queue import Queue
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from send2trash import send2trash
from tqdm import tqdm

LIBRARY_URL_TEMPLATE = "https://syntystore.com/apps/downloads/orders/{customer_id}"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_USE_COLOR = sys.stdout.isatty()

def _color(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text

TAG_INFO = _color("93", "[•]")   # yellow
TAG_OK   = _color("92", "[✓]")   # green
TAG_ERR  = _color("91", "[✗]")   # red


def _fit_table_widths(widths: list[int]) -> list[int]:
    """Reserve borders/padding and one spare terminal column to avoid auto-wrap."""
    available = max(len(widths), shutil.get_terminal_size((120, 24)).columns - 1
                    - (3 * len(widths) + 1))
    widths = list(widths)
    while sum(widths) > available:
        widest = max(range(len(widths)), key=widths.__getitem__)
        widths[widest] -= 1
    return widths


def _wrapped_table_row(cells, widths) -> str:
    # Break long filenames as well as prose; never truncate identifying text.
    columns = [
        [line for paragraph in str(cell).split("\n")
         for line in (textwrap.wrap(paragraph, width=width,
                                    break_long_words=True, break_on_hyphens=False) or [""])]
        for cell, width in zip(cells, widths)
    ]
    return "\n".join(
        "│ " + " │ ".join(
            (column[line] if line < len(column) else "").ljust(width)
            for column, width in zip(columns, widths)
        ) + " │"
        for line in range(max(map(len, columns)))
    )


@dataclass
class RemoteFile:
    pack_title: str            # e.g. "POLYGON - Battle Royale Pack"
    base_name: str             # e.g. "POLYGON_BattleRoyale"
    variant: Optional[str]     # e.g. "Unity_2022_3" or None for icons / inline-version files
    version: Optional[str]     # e.g. "v1_9_0" or None
    size_str: str              # raw "(105 MB)"
    download_url: str
    is_icon: bool = False

    @property
    def variant_slot(self) -> str:
        if self.is_icon:
            return f"{self.base_name}::ICON"
        if self.variant:
            return f"{self.base_name}::{self.variant}"
        return f"{self.base_name}::SOURCE"  # inline-version files like "..._Source_Files | v4"

    @property
    def version_tuple(self) -> tuple[int, ...]:
        if not self.version:
            return ()
        nums = re.findall(r"\d+", self.version)
        return tuple(int(num) for num in nums) if nums else ()


@dataclass
class LocalFile:
    path: Path
    base_name: str
    variant: Optional[str]
    version: Optional[str]


def _format_bytes(n: float) -> str:
    if n < 1024:
        return f"{n:.0f}B"
    if n < 1024**2:
        return f"{n / 1024:.0f}KB"
    if n < 1024**3:
        return f"{n / 1024**2:.0f}MB"
    return f"{n / 1024**3:.1f}GB"


_SIZE_RE = re.compile(r"([\d.]+)\s*(KB|MB|GB|TB|B)?", re.I)
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def _parse_size_str(size_str: str) -> int:
    if not size_str:
        return 0
    match = _SIZE_RE.search(size_str)
    if not match:
        return 0
    return int(float(match.group(1)) * _SIZE_UNITS.get((match.group(2) or "B").upper(), 1))


_BAR_FMT = ("{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
            "[RUN {elapsed}, ETA {remaining}, TOTAL @TOTAL@]")


def _guess_ext(variant: Optional[str]) -> str:
    if not variant:
        return ""
    variant_lower = variant.lower()
    if variant_lower.startswith("unity"):
        return ".unitypackage"
    if variant_lower.startswith(("unreal", "ue", "godot", "source")):
        return ".zip"
    return ""


def _detect_format(remote: "RemoteFile") -> str:
    if remote.is_icon:
        return "icon"
    if remote.variant:
        variant_lower = remote.variant.lower()
        if variant_lower.startswith("unity"):
            return "unity"
        if variant_lower.startswith(("unreal", "ue")):
            return "unreal"
        if variant_lower.startswith("godot"):
            return "godot"
        if variant_lower.startswith("source"):
            return "source"
    return "source"


def _engine_family(variant: Optional[str]) -> str:
    if not variant:
        return "SOURCE"
    variant_lower = variant.lower()
    if variant_lower.startswith("unity"):
        return "Unity"
    if variant_lower.startswith(("unreal", "ue")):
        return "Unreal"
    if variant_lower.startswith("godot"):
        return "Godot"
    if variant_lower.startswith("source"):
        return "SOURCE"
    return variant


def _engine_version_tuple(variant: Optional[str]) -> tuple[int, ...]:
    if not variant:
        return ()
    return tuple(int(num) for num in re.findall(r"\d+", variant))


def filter_latest_only(remote_files: list["RemoteFile"]) -> list["RemoteFile"]:
    """Per (base_name, engine_family), keep only the file with the newest
    (engine_version, pack_version). Icons are always kept."""
    groups: dict[tuple[str, str], list[RemoteFile]] = {}
    for remote in remote_files:
        if remote.is_icon:
            groups.setdefault((remote.base_name, "ICON"), []).append(remote)
            continue
        groups.setdefault((remote.base_name, _engine_family(remote.variant)), []).append(remote)

    kept: list[RemoteFile] = []
    for files in groups.values():
        if len(files) == 1:
            kept.append(files[0])
            continue
        best = max(files, key=lambda remote: (_engine_version_tuple(remote.variant), remote.version_tuple))
        kept.append(best)
    return kept


def expected_filename(remote: "RemoteFile", existing_paths: list[Path]) -> str:
    if remote.is_icon:
        return remote.base_name  # already has the .png/.jpg extension
    base = "_".join(filter(None, [remote.base_name, remote.variant, remote.version]))
    if existing_paths:
        return base + existing_paths[0].suffix
    return base + _guess_ext(remote.variant)


def make_session(cookies_path: Path) -> requests.Session:
    if not cookies_path.exists():
        sys.exit(f"{TAG_ERR} Cookies file not found: {cookies_path}\n"
                 f"    Export your syntystore.com cookies as Netscape format and save here.")
    jar = MozillaCookieJar(str(cookies_path))
    jar.load(ignore_discard=True, ignore_expires=True)
    session = requests.Session()
    session.cookies = jar
    session.headers.update(DEFAULT_HEADERS)
    return session


_CUSTOMER_ID_PATTERNS = (
    re.compile(r"/apps/downloads/orders/(\d+)"),
    re.compile(r"gid://shopify/Customer/(\d+)"),
    re.compile(r'"customer_id"\s*:\s*"?(\d+)'),
    re.compile(r'data-customer-id="(\d+)"'),
    re.compile(r"/orders/(\d+)"),
)


def _probe_for_customer_id(session: requests.Session, url: str) -> tuple[Optional[str], int, str]:
    """Returns (customer_id, status_code, debug). status_code is 0 on network error."""
    try:
        response = session.get(url, allow_redirects=True, timeout=30)
    except requests.RequestException as exc:
        return None, 0, f"request failed: {exc}"

    final_match = re.search(r"/orders/(\d+)", response.url)
    if final_match:
        return final_match.group(1), response.status_code, f"HTTP {response.status_code}"

    for pattern in _CUSTOMER_ID_PATTERNS:
        body_match = pattern.search(response.text)
        if body_match:
            return body_match.group(1), response.status_code, f"HTTP {response.status_code}"

    return None, response.status_code, f"HTTP {response.status_code}, final={response.url}"


def detect_customer_id(session: requests.Session) -> str:
    probes = (
        "https://syntystore.com/apps/downloads/orders",
        "https://syntystore.com/apps/downloads",
        "https://syntystore.com/account.json",
        "https://syntystore.com/account/orders",
        "https://syntystore.com/account",
        "https://account.syntystore.com/",
        "https://account.syntystore.com/orders",
    )
    statuses: list[int] = []
    for url in probes:
        customer_id, status, _debug = _probe_for_customer_id(session, url)
        statuses.append(status)
        if customer_id:
            return customer_id
    if statuses and all(code == 429 for code in statuses):
        sys.exit(f"{TAG_INFO} Rate-limited by Synty's CDN (HTTP 429). Wait a few minutes and try again. Reduce --workers if it keeps happening.")
    sys.exit(f"{TAG_INFO} cookies.txt is likely expired. Repeat the Cookies setup step.")


def fetch_library_packs(session: requests.Session, customer_id: str) -> list[dict]:
    packs = []
    page = 1
    base = LIBRARY_URL_TEMPLATE.format(customer_id=customer_id)
    while True:
        url = f"{base}?line_items_page={page}"
        response = session.get(url, timeout=30)
        if response.status_code in (302, 401, 403) or "login" in response.url.lower():
            sys.exit(f"{TAG_ERR} Authentication failed. Cookies may have expired. Re-export while logged in")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        items = soup.select("a.sky-pilot-list-item")
        if not items:
            break
        for link in items:
            heading = link.select_one(".sky-pilot-file-heading")
            title = heading.get_text(strip=True) if heading else link.get_text(strip=True)
            href = urljoin(response.url, link["href"])
            packs.append({"title": title, "url": href})
        # next link?
        next_link = soup.find("a", string=re.compile(r"Next"))
        if not next_link:
            break
        page += 1
        if page > 100:  # safety
            break
    return packs


_VERSION_RE = re.compile(
    r"^(?P<variant>.+?)\s*\|\s*"
    r"(?P<version>v[\d_.]+(?:[A-Z][A-Z0-9_]*)?)\s*$",
    re.I,
)

# recognized variant suffix (trailing part of a heading that names a build, not the pack)
_VARIANT_SUFFIX_RE = re.compile(
    r"_(Source_?[A-Za-z][A-Za-z0-9_]*|Unity[A-Za-z0-9_.]*|Unreal[A-Za-z0-9_.]*|Godot[A-Za-z0-9_.]*|UE\d+|\d+[._]\d+(?:[._]\d+)*)$",
    re.IGNORECASE,
)

# trailing "(340 KB)" / "(1.2 MB)" sometimes baked into a Synty heading text
_SIZE_TAIL_RE = re.compile(r"\s*\(\s*[\d.]+\s*(?:B|KB|MB|GB|TB)\s*\)\s*$", re.IGNORECASE)


def parse_pack_page(session: requests.Session, pack_url: str, pack_title: str) -> list[RemoteFile]:
    response = session.get(pack_url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    files: list[RemoteFile] = []

    for wrap in soup.select(".sky-pilot-file-wrapper"):
        heading = wrap.select_one(".sky-pilot-file-heading")
        if not heading:
            continue
        size_span = heading.select_one(".sky-pilot-file-size")
        size_str = size_span.get_text(strip=True) if size_span else ""

        # variant span is the one that's not the file-size span
        variant_span = None
        for span in heading.find_all("span"):
            if "sky-pilot-file-size" in span.get("class", []):
                continue
            variant_span = span
            break
        variant_text = variant_span.get_text(strip=True) if variant_span else None

        base_parts = []
        for child in heading.children:
            if getattr(child, "name", None) is None:  # NavigableString
                text = str(child).strip()
                if text:
                    base_parts.append(text)
        base_name = " ".join(base_parts).strip()

        # Synty occasionally inlines the file size into the heading text instead of
        # using a separate sky-pilot-file-size span, e.g. "...| v2(340 KB)"
        if variant_text:
            variant_text = _SIZE_TAIL_RE.sub("", variant_text).strip() or None
        base_name = _SIZE_TAIL_RE.sub("", base_name).strip()

        # source-files / engine entries embed the version inline,
        # e.g. "..._Source_Files | v4" or "..._2022_3 | v1_2_0"
        inline_match = _VERSION_RE.match(base_name)
        if inline_match and not variant_text:
            raw_base = inline_match.group("variant").strip()
            inline_version = inline_match.group("version")
            suffix_match = _VARIANT_SUFFIX_RE.search(raw_base)
            if suffix_match:
                base_name = raw_base[: suffix_match.start()]
                variant_text = f"{suffix_match.group(1)} | {inline_version}"
            else:
                base_name = raw_base
                variant_text = f"SOURCE | {inline_version}"

        variant, version = None, None
        if variant_text:
            match = _VERSION_RE.match(variant_text)
            if match:
                variant = match.group("variant").strip()
                version = match.group("version").strip()
            else:
                variant = variant_text

        # heading without a separator may still carry an implicit variant suffix
        # e.g. "SIMPLE_Port_Source_Files" -> base=SIMPLE_Port, variant=Source_Files
        if variant is None:
            suffix_match = _VARIANT_SUFFIX_RE.search(base_name)
            if suffix_match:
                variant = suffix_match.group(1)
                base_name = base_name[: suffix_match.start()]

        is_icon = "_ICON" in base_name.upper() and base_name.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))

        download_link = wrap.select_one(".sky-pilot-actions a.sky-pilot-button")
        if not download_link or not download_link.get("href"):
            continue
        download_url = urljoin(response.url, download_link["href"])

        files.append(RemoteFile(
            pack_title=pack_title,
            base_name=base_name,
            variant=variant,
            version=version,
            size_str=size_str,
            download_url=download_url,
            is_icon=is_icon,
        ))
    return files


def normalize_pack_title(title: str) -> str:
    # strip Windows-illegal characters
    normalized = title.replace("|", " ")
    normalized = WINDOWS_ILLEGAL.sub("", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def find_or_make_pack_dir(root: Path, pack_title: str, dry_run: bool) -> Path:
    target_norm = normalize_pack_title(pack_title)
    target_key = target_norm.lower()
    if root.exists():
        for sub in root.iterdir():
            if sub.is_dir() and normalize_pack_title(sub.name).lower() == target_key:
                return sub
    new_dir = root / target_norm
    if not dry_run:
        new_dir.mkdir(parents=True, exist_ok=True)
    return new_dir


_LOCAL_VERSION_RE = re.compile(r"_(?P<version>v[\d_.]+(?:[A-Z][A-Z0-9_]*)?)$")
_LOCAL_ICON_RE = re.compile(r"^(?P<base>.+)_ICON$", re.IGNORECASE)
_ICON_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def parse_local_filename(path: Path) -> Optional[LocalFile]:
    stem = path.stem

    icon_match = _LOCAL_ICON_RE.match(stem)
    if icon_match and path.suffix.lower() in _ICON_EXTS:
        return LocalFile(
            path=path,
            base_name=icon_match.group("base"),
            variant="ICON",
            version=None,
        )

    version: Optional[str] = None
    rest = stem
    version_match = _LOCAL_VERSION_RE.search(stem)
    if version_match:
        version = version_match.group("version")
        rest = stem[: version_match.start()]

    suffix_match = _VARIANT_SUFFIX_RE.search(rest)
    if not suffix_match:
        return None
    return LocalFile(
        path=path,
        base_name=rest[: suffix_match.start()],
        variant=suffix_match.group(1),
        version=version,
    )


def version_tuple(version_str: Optional[str]) -> tuple[int, ...]:
    if not version_str:
        return ()
    return tuple(int(num) for num in re.findall(r"\d+", version_str))


def decide_action(remote: RemoteFile, pack_dir: Path, force: bool) -> tuple[str, list[Path]]:
    if force:
        return ("first-download", [])
    if not pack_dir.exists():
        return ("first-download", [])

    same_slot = []  # (LocalFile, (engine_version_tuple, pack_version_tuple))
    for child in pack_dir.iterdir():
        if not child.is_file():
            continue
        # Icons and unversioned auxiliary files (e.g. Read_Me.txt) match
        # by full filename; they have no engine/version suffix to parse.
        if remote.is_icon or (remote.variant is None and remote.version is None
                              and Path(remote.base_name).suffix):
            if child.name.lower() == remote.base_name.lower():
                return ("skip", [child])
            continue
        local = parse_local_filename(child)
        if not local:
            continue
        if local.base_name.lower() != remote.base_name.lower():
            continue
        # e.g. remote Unity_2022_3_v1_1_0 is an upgrade of local Unity_2021_3_v1_0_4
        if remote.variant and local.variant:
            if _engine_family(local.variant) != _engine_family(remote.variant):
                continue
        local_key = (_engine_version_tuple(local.variant), version_tuple(local.version))
        same_slot.append((local, local_key))

    if not same_slot:
        return ("first-download", [])

    remote_key = (_engine_version_tuple(remote.variant), remote.version_tuple)
    for local, local_key in same_slot:
        if local_key == remote_key:
            return ("skip", [local.path])
    # if local is ahead of remote, skip rather than downgrade
    newest_local = max(same_slot, key=lambda entry: entry[1])
    if newest_local[1] > remote_key:
        return ("skip", [newest_local[0].path])
    sorted_paths = [local.path for local, _ in sorted(same_slot, key=lambda entry: entry[1])]
    return ("new-version", sorted_paths)


def _collect_prune_candidates(plan: list) -> tuple[list[Path], set[Path]]:
    prune_set: set[Path] = set()
    new_version_paths: set[Path] = set()

    # files explicitly marked as backups are never auto-pruned
    def _is_protected(path: Path) -> bool:
        upper = path.name.upper()
        return "_ARCHIVED" in upper or "_BACKUP" in upper or "_KEEP" in upper

    for remote, _pack_dir, action, existing_paths in plan:
        if action != "new-version":
            continue
        new_ext = Path(expected_filename(remote, existing_paths)).suffix.lower()
        for old_path in existing_paths:
            if new_ext and old_path.suffix.lower() != new_ext:
                continue
            if _is_protected(old_path):
                continue
            prune_set.add(old_path)
            new_version_paths.add(old_path)

    seen_dirs: set[Path] = set()
    for _remote, pack_dir, _action, _existing_paths in plan:
        if pack_dir in seen_dirs or not pack_dir.exists():
            continue
        seen_dirs.add(pack_dir)

        groups: dict[tuple, list] = {}
        for child in pack_dir.iterdir():
            if not child.is_file() or child.name == "manifest.json":
                continue
            local = parse_local_filename(child)
            if not local or local.variant == "ICON":
                continue
            family = _engine_family(local.variant)
            slot_key = (local.base_name.lower(), family, child.suffix.lower())
            composite = (_engine_version_tuple(local.variant), version_tuple(local.version))
            groups.setdefault(slot_key, []).append((child, composite))

        for entries in groups.values():
            if len(entries) < 2:
                continue
            newest_key = max(key for _, key in entries)
            for path, key in entries:
                if key < newest_key and not _is_protected(path):
                    prune_set.add(path)

    return sorted(prune_set), new_version_paths


def download_file(session: requests.Session, remote: RemoteFile, pack_dir: Path,
                  pbar_position: int = 0) -> tuple[str, Path, Optional[str]]:
    with session.get(remote.download_url, stream=True, allow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        # prefer the filename from Content-Disposition
        content_disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition)
        if match:
            filename = requests.utils.unquote(match.group(1))
        else:
            filename = Path(urlparse(response.url).path).name
        filename = WINDOWS_ILLEGAL.sub("_", filename)
        out_path = pack_dir / filename

        total = int(response.headers.get("Content-Length", 0))
        hasher = hashlib.sha256()
        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        with open(tmp_path, "wb") as out_file, tqdm(
            total=total or None, unit="B", unit_scale=True,
            desc=f"Worker {pbar_position}: {filename[:40]}",
            position=pbar_position, leave=False, ascii=" ░▒▓█",
            dynamic_ncols=True, mininterval=0.2, disable=None,
        ) as bar:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                out_file.write(chunk)
                hasher.update(chunk)
                bar.update(len(chunk))
        tmp_path.replace(out_path)
        return ("ok", out_path, hasher.hexdigest())


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(description="Sync Synty Store library to a local folder.")
    parser.add_argument("--path", required=True, help="Local root folder where pack subfolders live")
    parser.add_argument("--cookies", default="cookies.txt", help="Path to cookies.txt (Netscape format)")
    parser.add_argument("--dry-run", action="store_true", help="Plan only, don't download")
    parser.add_argument("--force", action="store_true", help="Re-download everything")
    parser.add_argument("--pack", action="append", default=[], help="Only sync packs whose title contains this string (repeatable)")
    parser.add_argument("--no-icons", action="store_true", help="Skip ICON files")
    parser.add_argument("--formats", default="",
                        help="Comma-separated engine variants to include: unity,unreal,godot,source. Omit to include all. Icons are controlled by --no-icons.")
    parser.add_argument("--latest-only", action="store_true",
                        help="Per pack, keep only the newest version of EACH engine family")
    parser.add_argument("--prune-old", action="store_true",
                        help="After a new-version download, delete older same-slot files on disk. Use --dry-run to preview.")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent downloads (default 4)")
    args = parser.parse_args()

    root = Path(args.path)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except FileNotFoundError:
        sys.exit(f"{TAG_ERR} Path not found: {root}")
    except OSError as exc:
        sys.exit(f"{TAG_ERR} Cannot create {root}: {exc}")
    cookies_path = Path(args.cookies)
    if not cookies_path.is_absolute():
        cookies_path = Path(__file__).parent / cookies_path

    session = make_session(cookies_path)
    customer_id = detect_customer_id(session)
    print(f"{TAG_INFO} Customer id: {customer_id}")

    print(f"{TAG_INFO} Parsing library...")
    packs = fetch_library_packs(session, customer_id)
    print(f"{TAG_OK} Found {len(packs)} packs in library")

    if args.pack:
        wants = [wanted.lower() for wanted in args.pack]
        packs = [pack for pack in packs if any(wanted in pack["title"].lower() for wanted in wants)]
        print(f"{TAG_OK} Filtered to {len(packs)} packs matching {args.pack}")

    fmt_filter: Optional[set[str]] = None
    if args.formats:
        fmt_filter = {name.strip().lower() for name in args.formats.split(",") if name.strip()}

    plan = []  # list of (remote, pack_dir, action, existing_paths)
    total_bytes = 0

    def _parse_one(pack):
        try:
            return pack, parse_pack_page(session, pack["url"], pack["title"]), None
        except Exception as exc:
            return pack, None, str(exc)

    status = tqdm(total=0, position=1, leave=False, bar_format="{desc}")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(_parse_one, pack) for pack in packs]
        pbar = tqdm(as_completed(futures), total=len(futures), desc="Gathering info",
                    ascii=" ░▒▓█", bar_format=_BAR_FMT.replace("@TOTAL@", "0B"))
        for future in pbar:
            pack, remote_files, err = future.result()
            status.set_description_str(f"Just parsed: {pack['title']}")
            if err:
                tqdm.write(f"  {TAG_ERR} Failed to parse {pack['title']}: {err}")
                continue
            if args.latest_only:
                remote_files = filter_latest_only(remote_files)
            pack_dir = find_or_make_pack_dir(root, pack["title"], dry_run=True)
            for remote in remote_files:
                if args.no_icons and remote.is_icon:
                    continue
                if fmt_filter is not None and not remote.is_icon and _detect_format(remote) not in fmt_filter:
                    continue
                total_bytes += _parse_size_str(remote.size_str)
                action, existing_paths = decide_action(remote, pack_dir, args.force)
                plan.append((remote, pack_dir, action, existing_paths))
            pbar.bar_format = _BAR_FMT.replace("@TOTAL@", _format_bytes(total_bytes))
    status.set_description_str("")
    status.refresh()
    status.close()

    counts: dict[str, int] = {}
    sizes: dict[str, int] = {}
    for remote, _pack_dir, action, _existing_paths in plan:
        counts[action] = counts.get(action, 0) + 1
        sizes[action] = sizes.get(action, 0) + _parse_size_str(remote.size_str)
    to_download = [entry for entry in plan if entry[2] != "skip"]
    total_dl_bytes = sum(byte_count for action, byte_count in sizes.items() if action != "skip")

    prune_paths: list[Path] = []
    prune_new_version_paths: set[Path] = set()
    prune_bytes = 0
    if args.prune_old:
        prune_paths, prune_new_version_paths = _collect_prune_candidates(plan)
        for old_path in prune_paths:
            try:
                prune_bytes += old_path.stat().st_size
            except OSError:
                pass
    prune_count = len(prune_paths)

    def _print_summary() -> None:
        sum_rows: list[tuple[str, str]] = []
        for action, count in sorted(counts.items()):
            sum_rows.append((action, f"{count} ({_format_bytes(sizes.get(action, 0))})"))
        if args.prune_old and prune_count:
            sum_rows.append(("prune", f"{prune_count} ({_format_bytes(prune_bytes)})"))
        sum_rows.append((
            "TOTAL TO DOWNLOAD:",
            f"{len(to_download)} ({_format_bytes(total_dl_bytes)})",
        ))
        sum_headers = ("Action", "Count")
        label_w = max(len(sum_headers[0]), max((len(label) for label, _ in sum_rows), default=0))
        count_w = max(len(sum_headers[1]), max((len(value) for _, value in sum_rows), default=1))

        label_w, count_w = _fit_table_widths([label_w, count_w])

        def _sum_hline(left: str, mid: str, right: str) -> str:
            return left + mid.join("─" * (width + 2) for width in (label_w, count_w)) + right

        def _sum_row(label: str, value: str) -> str:
            return _wrapped_table_row((label, value), (label_w, count_w))

        print("\nSUMMARY:")
        print(_sum_hline("╭", "┬", "╮"))
        print(_sum_row(sum_headers[0], sum_headers[1]))
        print(_sum_hline("├", "┼", "┤"))
        for i, (action, value) in enumerate(sum_rows):
            print(_sum_row(action, value))
            if i < len(sum_rows) - 1:
                print(_sum_hline("├", "┼", "┤"))
        print(_sum_hline("╰", "┴", "╯"))

    def _existing_label(action: str) -> str:
        return "prune" if args.prune_old and action == "new-version" else "existing"

    print()
    rows = []
    for remote, pack_dir, action, existing_paths in plan:
        if action == "skip":
            continue
        new_name = expected_filename(remote, existing_paths)
        new_ext = Path(new_name).suffix.lower()
        shown_existing = [path for path in existing_paths if path.suffix.lower() == new_ext] if new_ext else list(existing_paths)
        rows.append((f"[{action}]", remote.pack_title, new_name, str(pack_dir), shown_existing, action))
    if rows:
        headers = ("Status", "Pack", "New file", "Destination")
        widths = [max(len(headers[i]), max(len(row[i]) for row in rows)) for i in range(4)]
        for row in rows:
            label = _existing_label(row[5])
            for path in row[4]:
                widths[2] = max(widths[2], len(f"({label}: {path.name})"))

        widths = _fit_table_widths(widths)

        def _hline(left: str, mid: str, right: str) -> str:
            return left + mid.join("─" * (width + 2) for width in widths) + right

        def _row(cells: tuple[str, ...]) -> str:
            return _wrapped_table_row(cells, widths)

        print(_hline("╭", "┬", "╮"))
        print(_row(headers))
        print(_hline("├", "┼", "┤"))
        for i, (status_cell, pack_cell, new_name, dest_path, shown_existing, raw_action) in enumerate(rows):
            print(_row((status_cell, pack_cell, new_name, dest_path)))
            label = _existing_label(raw_action)
            for path in shown_existing:
                print(_row(("", "", f"({label}: {path.name})", "")))
            if i < len(rows) - 1:
                print(_hline("├", "┼", "┤"))
        print(_hline("╰", "┴", "╯"))

    if args.prune_old:
        extras = [p for p in prune_paths if p not in prune_new_version_paths]
        if extras:
            extras_rows = []
            for path in extras:
                try:
                    size_str = _format_bytes(path.stat().st_size)
                except OSError:
                    size_str = "?"
                extras_rows.append((path.parent.name, path.name, size_str))
            ph = ("Pack", "File", "Size")
            ew0 = max(len(ph[0]), max(len(r[0]) for r in extras_rows))
            ew1 = max(len(ph[1]), max(len(r[1]) for r in extras_rows))
            ew2 = max(len(ph[2]), max(len(r[2]) for r in extras_rows))

            ew0, ew1, ew2 = _fit_table_widths([ew0, ew1, ew2])

            def _ex_hline(left: str, mid: str, right: str) -> str:
                return left + mid.join("─" * (w + 2) for w in (ew0, ew1, ew2)) + right

            def _ex_row(c0: str, c1: str, c2: str) -> str:
                return _wrapped_table_row((c0, c1, c2), (ew0, ew1, ew2))

            print("\nPRUNE LIST:")
            print(_ex_hline("╭", "┬", "╮"))
            print(_ex_row(*ph))
            print(_ex_hline("├", "┼", "┤"))
            for i, row in enumerate(extras_rows):
                print(_ex_row(*row))
                if i < len(extras_rows) - 1:
                    print(_ex_hline("├", "┼", "┤"))
            print(_ex_hline("╰", "┴", "╯"))
    _print_summary()

    if args.dry_run:
        return

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    if not to_download:
        if plan:
            print(f"{TAG_OK} Nothing to download. Every file in your local library is already up to date.")
        else:
            print(f"{TAG_OK} Nothing to download. No files matched the current filters.")
    else:
        try:
            answer = input(
                f"\n{TAG_INFO} Download {len(to_download)} files ({_format_bytes(total_dl_bytes)})? [Y/n]: "
            ).strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            print(f"{TAG_INFO} Aborted.")
            return

        progress_slots: Queue[int] = Queue()
        for position in range(1, args.workers + 1):
            progress_slots.put(position)

        def _do(item):
            remote, pack_dir, _action, _existing_paths = item
            position = progress_slots.get()
            try:
                pack_dir.mkdir(parents=True, exist_ok=True)
                _, path, sha = download_file(session, remote, pack_dir, pbar_position=position)
                return (item, path, sha, None)
            except Exception as exc:
                return (item, None, None, str(exc))
            finally:
                progress_slots.put(position)

        remaining = list(to_download)
        failed_items: list = []
        attempt = 0
        while remaining:
            attempt += 1
            label = "Downloading" if attempt == 1 else f"Retry {attempt - 1}: re-downloading"
            print(f"\n{TAG_INFO} {label} {len(remaining)} files with {args.workers} workers...")

            failed_items = []
            downloaded_bytes = 0
            with tqdm(total=len(remaining), desc="Files", position=0, ascii=" ░▒▓█",
                      dynamic_ncols=True, mininterval=0.2, disable=None,
                      bar_format=_BAR_FMT.replace("@TOTAL@", "0B")) as files_bar, \
                    ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = [executor.submit(_do, item) for item in remaining]
                for future in as_completed(futures):
                    item, path, sha, err = future.result()
                    files_bar.update(1)
                    remote = item[0]
                    action = item[2]
                    if err:
                        tqdm.write(f"  {TAG_ERR} {remote.base_name}: {err}")
                        failed_items.append(item)
                        continue
                    try:
                        downloaded_bytes += path.stat().st_size
                    except OSError:
                        pass
                    files_bar.bar_format = _BAR_FMT.replace("@TOTAL@", _format_bytes(downloaded_bytes))
                    key = str(path.relative_to(root))
                    manifest[key] = {
                        "pack": remote.pack_title,
                        "base": remote.base_name,
                        "variant": remote.variant,
                        "version": remote.version,
                        "size": remote.size_str,
                        "sha256": sha,
                        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "action": action,
                    }
                    manifest_path.write_text(json.dumps(manifest, indent=2))

            if not failed_items:
                break

            try:
                answer = input(f"\n{TAG_INFO} {len(failed_items)} download(s) failed. Retry? [Y/n]: ").strip().lower()
            except EOFError:
                answer = "n"
            if answer not in ("", "y", "yes"):
                break
            remaining = failed_items

        if failed_items:
            print(f"{TAG_ERR} {len(failed_items)} download(s) still failed after retries.")
        print(f"{TAG_OK} Done.")

    if args.prune_old:
        prune_paths_final, _ = _collect_prune_candidates(plan)
        if not prune_paths_final:
            print(f"{TAG_OK} No old versions to prune.")
        else:
            total = 0
            for path in prune_paths_final:
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
            print(f"\n{TAG_INFO} Pruning {len(prune_paths_final)} files ({_format_bytes(total)})...")
            failures = 0
            for path in tqdm(prune_paths_final, desc="Pruning", ascii=" ░▒▓█",
                             bar_format=_BAR_FMT.replace("@TOTAL@", _format_bytes(total))):
                try:
                    send2trash(str(path))
                    manifest.pop(str(path.relative_to(root)), None)
                except OSError as exc:
                    tqdm.write(f"  {TAG_ERR} could not prune {path.name}: {exc}")
                    failures += 1
            manifest_path.write_text(json.dumps(manifest, indent=2))
            if failures:
                print(f"{TAG_ERR} {failures} prune(s) failed.")
            else:
                print(f"{TAG_OK} Pruned {len(prune_paths_final)} files.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{TAG_INFO} Interrupted by user")
        sys.exit(130)
