#!/usr/bin/env python3
"""
WoW Addon Updater
=================
A small, portable Windows tool that scans a World of Warcraft AddOns folder,
looks every addon up on CurseForge, shows installed vs. available version and
lets you update each addon with one click.

Standard library only (tkinter + urllib) so it packages into a small
single-file .exe with PyInstaller.

CurseForge access goes through the official CurseForge API, which needs an
API key (apply via the form linked from API_KEY_URL below). The key is asked
for on first run and stored in WoWAddonUpdater.json next to the program.
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass, field

APP_NAME = "WoW Addon Updater"
APP_VERSION = "1.0.0"
CONFIG_NAME = "WoWAddonUpdater.json"

DEFAULT_ADDONS_PATH = r"D:\World of Warcraft\_forever_\Interface\AddOns"
API_BASE = "https://api.curseforge.com"
API_KEY_URL = "https://support.curseforge.com/support/solutions/articles/9000208346"
WOW_GAME_ID = 1

# CurseForge "game version type" (flavour) ids. The live list is read from the
# API at runtime; these are only the fallback when that call fails.
FALLBACK_FLAVORS = [
    (88568, "WoW Forever"),
    (517, "WoW Retail"),
    (67408, "WoW Classic Era"),
]
# install sub-folder -> (keyword to find in the API flavour name, fallback id)
FOLDER_FLAVOR_HINTS = {
    "_forever_": ("forever", 88568),
    "_retail_": ("retail", 517),
    "_classic_era_": ("", 67408),
}

RELEASE, BETA, ALPHA = 1, 2, 3
CHANNEL_LABELS = {RELEASE: "Release only", BETA: "Release + Beta", ALPHA: "Release + Beta + Alpha"}

# Entry status values
ST_OK = "Up to date"
ST_UPDATE = "Update available"
ST_DIFF = "Different version"
ST_NEWER = "Newer than CurseForge"
ST_UNLINKED = "Not linked"
ST_NOFILE = "No file for this game version"
ST_MISSING = "Not found on CurseForge"
ST_QUEUED = "Queued…"
ST_UPDATING = "Updating…"
ST_UPDATED = "Updated"
ST_ERROR = "Error"
ST_MANUAL = "Manual download only"


# --------------------------------------------------------------------------
# Configuration (portable: lives next to the .exe)
# --------------------------------------------------------------------------

def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    override = os.environ.get("WAU_CONFIG")
    if override:
        return override
    primary = os.path.join(app_dir(), CONFIG_NAME)
    try:  # portable: keep settings next to the program whenever that folder is writable
        with open(primary, "a", encoding="utf-8"):
            pass
        if os.path.getsize(primary) == 0:
            os.remove(primary)
        return primary
    except OSError:
        pass
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "WoWAddonUpdater", CONFIG_NAME)


class Config:
    DEFAULTS = {
        "addons_path": DEFAULT_ADDONS_PATH,
        "api_key": "",
        "api_base": API_BASE,
        "flavor_id": 0,          # 0 = detect from the folder name
        "channel": RELEASE,
        "backup": True,
        "only_updates": False,
        "geometry": "",
        "profiles": {},          # per AddOns folder: links / nomatch / installed
    }

    def __init__(self, path: str | None = None):
        self.path = path or config_path()
        self.data = json.loads(json.dumps(self.DEFAULTS))
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def profile(self) -> dict:
        key = os.path.normcase(os.path.normpath(self.data["addons_path"]))
        prof = self.data["profiles"].setdefault(key, {})
        prof.setdefault("links", {})       # folder -> CurseForge project id
        prof.setdefault("nomatch", {})     # folder -> date of last failed lookup
        prof.setdefault("installed", {})   # project id -> install record
        return prof


# --------------------------------------------------------------------------
# Scanning the AddOns folder
# --------------------------------------------------------------------------

_TOC_FIELD = re.compile(r"^##\s*([^:]+?)\s*:\s*(.*?)\s*$")
_UI_ESCAPES = re.compile(
    r"\|c[0-9a-fA-F]{8}|\|cn[^:|]*:|\|r|\|T[^|]*\|t|\|A[^|]*\|a|\|n", re.S)
_PLACEHOLDER = re.compile(r"@[\w-]+@")


@dataclass
class LocalAddon:
    folder: str
    title: str = ""
    version: str = ""
    interface: str = ""
    project_id: int | None = None
    deps: list = field(default_factory=list)


def clean_title(text: str) -> str:
    return _UI_ESCAPES.sub("", text or "").strip()


def parse_toc(path: str) -> dict:
    fields: dict = {}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                m = _TOC_FIELD.match(line.strip())
                if m:
                    fields.setdefault(m.group(1).strip().lower(), m.group(2))
    except OSError:
        pass
    return fields


def find_toc(folder_path: str, folder: str) -> str | None:
    try:
        tocs = [f for f in os.listdir(folder_path) if f.lower().endswith(".toc")]
    except OSError:
        return None
    if not tocs:
        return None
    low = {f.lower(): f for f in tocs}
    exact = low.get(folder.lower() + ".toc")
    if exact:
        return os.path.join(folder_path, exact)
    # Flavour specific TOCs: Addon_Mainline.toc, Addon-Classic.toc, ...
    prefixed = sorted(f for f in tocs
                      if re.match(re.escape(folder) + r"[-_]", f, re.I))
    for hint in ("forever", "mainline", "vanilla", "classic"):
        for f in prefixed:
            if hint in f.lower():
                return os.path.join(folder_path, f)
    return os.path.join(folder_path, (prefixed or sorted(tocs))[0])


def scan_addons(addons_path: str) -> list:
    """Return one LocalAddon per addon folder (Blizzard's own are skipped)."""
    result = []
    for name in sorted(os.listdir(addons_path), key=str.lower):
        full = os.path.join(addons_path, name)
        if not os.path.isdir(full) or name.startswith(".") or name.lower().startswith("blizzard_"):
            continue
        toc = find_toc(full, name)
        if not toc:
            continue
        f = parse_toc(toc)
        version = (f.get("version") or "").strip()
        if _PLACEHOLDER.search(version):
            version = ""  # unpackaged development checkout
        pid = None
        raw = (f.get("x-curse-project-id") or "").strip()
        if raw.isdigit() and int(raw) > 0:
            pid = int(raw)
        deps = f.get("requireddeps") or f.get("dependencies") or f.get("requireddep") or ""
        result.append(LocalAddon(
            folder=name,
            title=clean_title(f.get("title", "")) or name,
            version=version,
            interface=(f.get("interface") or "").strip(),
            project_id=pid,
            deps=[d.strip() for d in deps.split(",") if d.strip()],
        ))
    return result


# --------------------------------------------------------------------------
# Version comparison
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = (text or "").strip().lower()
    if text.endswith(".zip"):
        text = text[:-4]
    return re.sub(r"\s+", "", text)


def _numeric(text: str) -> tuple | None:
    """Most detailed number group in a version string, e.g. 'TomTom-v4.0.5' -> (4, 0, 5)."""
    groups = re.findall(r"\d+(?:\.\d+)*", text or "")
    if not groups:
        return None
    best = max(reversed(groups), key=lambda g: g.count("."))
    return tuple(int(p) for p in best.split("."))


def compare_versions(installed: str, latest_name: str, latest_file: str = "") -> str:
    """Return 'same', 'older', 'newer' or 'unknown' for the installed version."""
    inst = _norm(installed)
    if not inst:
        return "unknown"
    core = inst[1:] if re.match(r"v\d", inst) else inst
    pattern = re.compile(r"(?<![0-9])(?<![0-9]\.)" + re.escape(core) + r"(?!\.?[0-9])")
    for candidate in (_norm(latest_name), _norm(latest_file)):
        if candidate and (candidate == inst or pattern.search(candidate)):
            return "same"
    a, b = _numeric(inst), _numeric(_norm(latest_name)) or _numeric(_norm(latest_file))
    if a and b and len(a) == len(b):
        return "same" if a == b else ("older" if a < b else "newer")
    return "unknown"


# --------------------------------------------------------------------------
# CurseForge API client
# --------------------------------------------------------------------------

class CurseForgeError(Exception):
    pass


def _key_allowed_host(host: str) -> bool:
    host = (host or "").lower()
    return host.endswith(".forgecdn.net") or host.endswith(".curseforge.com") \
        or host in ("forgecdn.net", "curseforge.com")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never forward the API key to a foreign host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _key_allowed_host(urllib.parse.urlsplit(newurl).hostname or ""):
            for store in (new.headers, new.unredirected_hdrs):
                for name in [k for k in store if k.lower() == "x-api-key"]:
                    del store[name]
        return new


class CurseForge:
    def __init__(self, api_key: str, base: str = API_BASE, timeout: int = 30):
        self.api_key = (api_key or "").strip()
        self.base = (base or API_BASE).rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_SafeRedirect())
        self._base_host = urllib.parse.urlsplit(self.base).hostname or ""

    # -- low level --------------------------------------------------------
    def _headers(self, url: str) -> dict:
        headers = {"Accept": "application/json",
                   "User-Agent": f"WoWAddonUpdater/{APP_VERSION} (personal use)"}
        host = urllib.parse.urlsplit(url).hostname or ""
        if self.api_key and (host == self._base_host or _key_allowed_host(host)):
            headers["x-api-key"] = self.api_key
        return headers

    def _request(self, method: str, path: str, params: dict | None = None, body=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = self._headers(url)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise CurseForgeError(
                        "CurseForge rejected the API key (HTTP %d). "
                        "Check the key under Settings." % exc.code) from None
                if exc.code == 404:
                    raise CurseForgeError("Not found on CurseForge (HTTP 404).") from None
                last = "CurseForge returned HTTP %d." % exc.code
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = "Could not reach CurseForge: %s" % getattr(exc, "reason", exc)
            time.sleep(1.5 * (attempt + 1))
        raise CurseForgeError(last or "CurseForge request failed.")

    # -- endpoints --------------------------------------------------------
    def version_types(self) -> list:
        data = self._request("GET", f"/v1/games/{WOW_GAME_ID}/version-types").get("data") or []
        return [(int(t["id"]), t.get("name") or t.get("slug") or str(t["id"])) for t in data]

    def mods(self, ids: list) -> dict:
        found = {}
        ids = sorted(set(int(i) for i in ids))
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            data = self._request("POST", "/v1/mods", body={"modIds": chunk, "filterPcOnly": True})
            for mod in data.get("data") or []:
                found[int(mod["id"])] = mod
        return found

    def search_slug(self, slug: str) -> dict | None:
        data = self._request("GET", "/v1/mods/search",
                             params={"gameId": WOW_GAME_ID, "slug": slug, "pageSize": 5})
        for mod in data.get("data") or []:
            if mod.get("gameId") == WOW_GAME_ID and (mod.get("slug") or "").lower() == slug.lower():
                return mod
        return None

    def files(self, file_ids: list) -> dict:
        found = {}
        ids = sorted(set(int(i) for i in file_ids))
        for start in range(0, len(ids), 50):
            data = self._request("POST", "/v1/mods/files", body={"fileIds": ids[start:start + 50]})
            for f in data.get("data") or []:
                found[int(f["id"])] = f
        return found

    def mod_files(self, mod_id: int, flavor_id: int) -> list:
        data = self._request("GET", f"/v1/mods/{mod_id}/files",
                             params={"gameVersionTypeId": flavor_id, "pageSize": 30})
        return data.get("data") or []

    def download_url(self, mod_id: int, file_id: int) -> str:
        data = self._request("GET", f"/v1/mods/{mod_id}/files/{file_id}/download-url")
        return data.get("data") or ""

    def download(self, url: str, dest: str, progress=None) -> None:
        if urllib.parse.urlsplit(url).scheme not in ("https", "http"):
            raise CurseForgeError("Unexpected download address.")
        req = urllib.request.Request(url, headers=self._headers(url))
        try:
            with self.opener.open(req, timeout=self.timeout) as resp, open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    block = resp.read(65536)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
        except urllib.error.HTTPError as exc:
            raise CurseForgeError("Download failed (HTTP %d)." % exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise CurseForgeError("Download failed: %s" % getattr(exc, "reason", exc)) from None


# --------------------------------------------------------------------------
# Matching installed addons to CurseForge projects and checking versions
# --------------------------------------------------------------------------

@dataclass
class Entry:
    key: str
    name: str
    folders: list
    installed: str = ""
    project_id: int | None = None
    matched_by: str = ""        # toc / linked / installed / name
    url: str = ""
    available: str = ""
    latest: dict | None = None  # CurseForge file object
    status: str = ST_UNLINKED
    detail: str = ""
    primary: str = ""           # folder whose TOC supplies the version

    @property
    def can_update(self) -> bool:
        return self.latest is not None and self.status in (ST_UPDATE, ST_DIFF, ST_ERROR)


def detect_flavor(addons_path: str, flavors: list) -> int:
    """Pick the CurseForge flavour id from the install sub-folder (_forever_, ...)."""
    parts = [p.lower() for p in re.split(r"[\\/]+", addons_path or "")]
    for folder, (keyword, fallback) in FOLDER_FLAVOR_HINTS.items():
        if folder in parts:
            if keyword:
                for fid, name in flavors:
                    if keyword in name.lower():
                        return fid
            return fallback
    return 0


def slug_candidates(folder: str, title: str) -> list:
    out = []
    for text in (folder, title):
        text = re.sub(r"[!']", "", text or "")
        spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        for variant in (text, spaced):
            dashed = re.sub(r"[^a-z0-9]+", "-", variant.lower()).strip("-")
            for slug in (dashed, dashed.replace("-", "")):
                if len(slug) >= 3 and slug not in out:
                    out.append(slug)
    return out[:4]


def parse_link(text: str):
    """'12345' -> ('id', 12345); a curseforge.com/wow/addons/<slug> URL -> ('slug', slug)."""
    text = (text or "").strip()
    if text.isdigit():
        return "id", int(text)
    m = re.search(r"curseforge\.com/wow/addons/([A-Za-z0-9_-]+)", text)
    if m:
        return "slug", m.group(1).lower()
    if re.fullmatch(r"[A-Za-z0-9_-]{3,}", text):
        return "slug", text.lower()
    return None, None


def _related(folder: str, dep: str) -> bool:
    """True when `folder` looks like a module of `dep` (DBM-Raids -> DBM-Core)."""
    f, d = folder.lower(), dep.lower()
    if f == d:
        return False
    head = lambda s: re.split(r"[-_]", s)[0]
    return f.startswith(d) or (len(head(f)) >= 3 and head(f) == head(d))


def build_entries(addons: list, profile: dict, client: CurseForge | None, progress=None) -> list:
    """Group addon folders by CurseForge project. Unmatched folders get their own entry."""
    by_folder = {a.folder: a for a in addons}
    assigned: dict = {}   # folder -> (project id, how)

    blocked = {f for f, link in profile["links"].items() if link == 0}  # unlinked by the user
    for a in addons:
        link = profile["links"].get(a.folder)
        if a.folder in blocked:
            continue
        if isinstance(link, int) and link > 0:
            assigned[a.folder] = (link, "linked")
        elif a.project_id:
            assigned[a.folder] = (a.project_id, "toc")
    for pid, rec in profile["installed"].items():
        for folder in rec.get("folders", []):
            if folder in by_folder and folder not in assigned and folder not in blocked:
                assigned[folder] = (int(pid), "installed")

    # Modules without their own id inherit the project of the addon they extend.
    for _ in range(3):
        for a in addons:
            if a.folder in assigned or a.folder in blocked:
                continue
            for dep in a.deps:
                if dep in assigned and _related(a.folder, dep):
                    assigned[a.folder] = (assigned[dep][0], "module")
                    break

    # Last resort: look the folder name up on CurseForge (cached for a week when it fails).
    today = _dt.date.today()
    todo = []
    for a in addons:
        if a.folder in assigned or a.folder in blocked:
            continue
        stamp = profile["nomatch"].get(a.folder)
        try:
            fresh = stamp and (today - _dt.date.fromisoformat(stamp)).days < 7
        except ValueError:
            fresh = False
        if not fresh:
            todo.append(a)
    if client:
        for n, a in enumerate(todo, 1):
            if progress:
                progress("Looking up %s on CurseForge… (%d/%d)" % (a.folder, n, len(todo)))
            mod = None
            for slug in slug_candidates(a.folder, a.title):
                mod = client.search_slug(slug)
                if mod:
                    break
            if mod:
                assigned[a.folder] = (int(mod["id"]), "name")
                profile["nomatch"].pop(a.folder, None)
            else:
                profile["nomatch"][a.folder] = today.isoformat()
        # a second module pass now that more parents are known
        for a in addons:
            if a.folder not in assigned and a.folder not in blocked:
                for dep in a.deps:
                    if dep in assigned and _related(a.folder, dep):
                        assigned[a.folder] = (assigned[dep][0], "module")
                        break

    groups: dict = {}
    for a in addons:
        pid, how = assigned.get(a.folder, (None, ""))
        groups.setdefault("p%d" % pid if pid else "f:" + a.folder, []).append((a, how, pid))
    entries = []
    for key, members in groups.items():
        folders = {a.folder for a, _how, _pid in members}
        needed = {d for a, _how, _pid in members for d in a.deps if d in folders}
        # main folder: not a module, preferably the one the others depend on, then the shortest name
        main, how, pid = min(members, key=lambda m: (m[1] == "module", m[0].folder not in needed,
                                                     len(m[0].folder), m[0].folder.lower()))
        version = main.version or next((a.version for a, _h, _p in members if a.version), "")
        entries.append(Entry(key=key, name=main.title, folders=sorted(folders, key=str.lower),
                             installed=version, project_id=pid, matched_by=how, primary=main.folder))
    return entries


def pick_latest_index(mod: dict, flavor_id: int, channel: int) -> dict | None:
    cands = [i for i in (mod.get("latestFilesIndexes") or [])
             if i.get("gameVersionTypeId") == flavor_id and (i.get("releaseType") or RELEASE) <= channel]
    return max(cands, key=lambda i: i.get("fileId", 0)) if cands else None


def file_label(f: dict) -> str:
    label = (f.get("displayName") or f.get("fileName") or "").strip()
    return label[:-4] if label.lower().endswith(".zip") else label


def evaluate(entry: Entry, profile: dict) -> None:
    """Set entry.status by comparing the installed version with entry.latest."""
    f = entry.latest
    entry.available = file_label(f)
    rec = profile["installed"].get(str(entry.project_id))
    if rec and rec.get("toc_version", None) == entry.installed and rec.get("file_id"):
        # Installed through this tool and untouched since: compare exact file ids.
        if int(rec["file_id"]) == int(f["id"]):
            entry.status = ST_OK
        elif int(rec["file_id"]) < int(f["id"]):
            entry.status = ST_UPDATE
        else:
            entry.status = ST_NEWER
        return
    result = compare_versions(entry.installed, f.get("displayName", ""), f.get("fileName", ""))
    entry.status = {"same": ST_OK, "older": ST_UPDATE, "newer": ST_NEWER}.get(result, ST_DIFF)
    if entry.status == ST_DIFF:
        entry.detail = ("The installed version text does not match CurseForge's latest file, "
                        "so it may be out of date. Updating installs the latest file.")


def check_entries(entries: list, profile: dict, client: CurseForge, flavor_id: int,
                  channel: int, progress=None) -> None:
    linked = [e for e in entries if e.project_id]
    if progress:
        progress("Checking %d addons on CurseForge…" % len(linked))
    mods = client.mods([e.project_id for e in linked])
    wanted: dict = {}
    for e in linked:
        mod = mods.get(e.project_id)
        if not mod:
            e.status, e.detail = ST_MISSING, "CurseForge has no project with id %d." % e.project_id
            continue
        e.url = ((mod.get("links") or {}).get("websiteUrl")
                 or "https://www.curseforge.com/wow/addons/%s" % mod.get("slug", ""))
        idx = pick_latest_index(mod, flavor_id, channel)
        if idx:
            cached = next((f for f in (mod.get("latestFiles") or []) if f.get("id") == idx["fileId"]), None)
            if cached:
                e.latest = cached
            else:
                wanted[int(idx["fileId"])] = e
        else:
            # The index can lag behind for a new game version: ask for the file list directly.
            try:
                files = [f for f in client.mod_files(e.project_id, flavor_id)
                         if (f.get("releaseType") or RELEASE) <= channel]
            except CurseForgeError:
                files = []
            if files:
                e.latest = max(files, key=lambda f: f.get("id", 0))
            else:
                e.status = ST_NOFILE
                e.detail = "This project has no file for the selected game version / release channel."
    if wanted:
        details = client.files(list(wanted))
        for fid, e in wanted.items():
            if fid in details:
                e.latest = details[fid]
            else:
                e.status, e.detail = ST_ERROR, "CurseForge did not return details for file %d." % fid
    for e in linked:
        if e.latest is not None:
            evaluate(e, profile)


# --------------------------------------------------------------------------
# Installing an update
# --------------------------------------------------------------------------

class UpdateError(Exception):
    pass


class ManualDownload(UpdateError):
    """The author has switched off downloads through the API."""


_BAD_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')


def _zip_plan(zf: zipfile.ZipFile) -> list:
    """Validate every entry and return the top-level addon folders in the archive."""
    tops = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        parts = [p for p in name.split("/") if p != ""]
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or any(p in (".", "..") for p in parts):
            raise UpdateError("The downloaded archive contains an unsafe path (%s)." % info.filename)
        if any(_BAD_NAME.search(p) or p.endswith((" ", ".")) for p in parts):
            raise UpdateError("The downloaded archive contains an invalid file name (%s)." % info.filename)
        if len(parts) >= 2 and parts[0] not in tops:
            tops.append(parts[0])
    if not tops:
        raise UpdateError("The downloaded archive does not contain any addon folders.")
    return tops


def _rmtree(path: str) -> None:
    def onerror(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=onerror)


def install_zip(zip_path: str, addons_path: str, backup: bool = True) -> list:
    """Replace the addon folders contained in zip_path. Returns the folder names installed.

    The old folders are moved aside first and put back if anything fails, so an
    interrupted update never leaves a half-written addon behind.
    """
    interface_dir = os.path.dirname(os.path.normpath(addons_path))
    staging = os.path.join(interface_dir, ".wau_staging_%d" % os.getpid())
    backup_root = os.path.join(interface_dir, "AddOns_Backup")
    _rmtree(staging)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            tops = _zip_plan(zf)
            os.makedirs(staging)
            root = os.path.realpath(staging)
            for info in zf.infolist():
                parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
                if len(parts) < 2 and not info.is_dir():
                    continue  # loose file at the archive root: not part of an addon
                target = os.path.realpath(os.path.join(staging, *parts))
                if target != root and not target.startswith(root + os.sep):
                    raise UpdateError("The downloaded archive contains an unsafe path.")
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile:
        _rmtree(staging)
        raise UpdateError("The downloaded file is not a valid zip archive.") from None
    except Exception:
        _rmtree(staging)
        raise

    moved = []      # (folder, where the old copy went)
    placed = []
    try:
        for top in tops:
            dest = os.path.join(addons_path, top)
            if os.path.exists(dest):
                old = os.path.join(backup_root if backup else staging, ("" if backup else "_old_") + top)
                os.makedirs(os.path.dirname(old), exist_ok=True)
                _rmtree(old)
                shutil.move(dest, old)
                moved.append((top, old))
            shutil.move(os.path.join(staging, top), dest)
            placed.append(top)
    except (OSError, shutil.Error) as exc:
        for top in placed:
            _rmtree(os.path.join(addons_path, top))
        for top, old in moved:
            try:
                shutil.move(old, os.path.join(addons_path, top))
            except OSError:
                pass
        _rmtree(staging)
        raise UpdateError("Could not replace the addon files (%s). Close World of Warcraft "
                          "and try again." % (getattr(exc, "strerror", None) or exc)) from None
    _rmtree(staging)
    return tops


def update_entry(entry: Entry, cfg: Config, client: CurseForge, progress=None) -> None:
    """Download and install entry.latest, then record what was installed."""
    f = entry.latest
    if not f:
        raise UpdateError("Nothing to install.")
    url = f.get("downloadUrl") or ""
    if not url:
        try:
            url = client.download_url(entry.project_id, int(f["id"]))
        except CurseForgeError:
            url = ""
    if not url:
        raise ManualDownload("The author does not allow downloads outside the CurseForge app. "
                             "Use the addon's CurseForge page to download it manually.")
    addons_path = cfg["addons_path"]
    fd, tmp = tempfile.mkstemp(prefix="wau_", suffix=".zip")
    os.close(fd)
    try:
        client.download(url, tmp, progress)
        size = int(f.get("fileLength") or 0)
        if size and os.path.getsize(tmp) != size:
            raise UpdateError("The download is incomplete (size mismatch). Try again.")
        sha1 = next((h.get("value") for h in (f.get("hashes") or []) if h.get("algo") == 1), None)
        if sha1:
            digest = hashlib.sha1()
            with open(tmp, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
            if digest.hexdigest().lower() != sha1.lower():
                raise UpdateError("The download failed its checksum. Try again.")
        folders = install_zip(tmp, addons_path, backup=bool(cfg["backup"]))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    # Re-read the installed version from the new files and remember the exact file id.
    fresh = {a.folder: a for a in scan_addons(addons_path) if a.folder in folders}
    new_version = fresh[entry.primary].version if entry.primary in fresh else ""
    if not new_version:
        new_version = next((a.version for a in fresh.values() if a.version), "")
    entry.installed = new_version or entry.installed
    entry.folders = sorted(set(entry.folders) | set(folders), key=str.lower)
    profile = cfg.profile()
    profile["installed"][str(entry.project_id)] = {
        "file_id": int(f["id"]),
        "file_name": f.get("fileName", ""),
        "toc_version": entry.installed,
        "folders": folders,
        "date": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    cfg.save()


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui(selftest: bool = False) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    COLORS = {
        ST_OK: "#2e7d32", ST_UPDATED: "#2e7d32", ST_UPDATE: "#c75300", ST_DIFF: "#c75300",
        ST_ERROR: "#c62828", ST_MISSING: "#c62828", ST_MANUAL: "#c75300", ST_UNLINKED: "#777777",
        ST_NOFILE: "#777777", ST_NEWER: "#1565c0", ST_QUEUED: "#555555", ST_UPDATING: "#1565c0",
    }
    ROW_BG = ("#ffffff", "#f4f6f8")
    HEAD_BG = "#e3e7eb"
    COLS = [("Addon", 1), ("Installed", 20), ("Available", 24), ("Status", 24)]

    class Tooltip:
        def __init__(self, widget, text_fn):
            self.widget, self.text_fn, self.tip = widget, text_fn, None
            widget.bind("<Enter>", self.show, add="+")
            widget.bind("<Leave>", self.hide, add="+")

        def show(self, _e=None):
            text = self.text_fn()
            if not text or self.tip:
                return
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 2
            self.tip.wm_geometry("+%d+%d" % (x, y))
            tk.Label(self.tip, text=text, justify="left", bg="#ffffe1", relief="solid", bd=1,
                     wraplength=520, padx=6, pady=3).pack()

        def hide(self, _e=None):
            if self.tip:
                self.tip.destroy()
                self.tip = None

    class App:
        def __init__(self, root):
            self.root = root
            self.cfg = Config()
            self.entries: list = []
            self.rows: dict = {}
            self.flavors = list(FALLBACK_FLAVORS)
            self.flavors_loaded = False
            self.jobs: queue.Queue = queue.Queue()
            self.events: queue.Queue = queue.Queue()
            self.pending = 0
            self.checking = False

            root.title("%s %s" % (APP_NAME, APP_VERSION))
            root.minsize(820, 420)
            root.geometry(self.cfg["geometry"] or "1100x660")
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.build()
            threading.Thread(target=self.worker, daemon=True).start()
            root.after(80, self.pump)
            root.after(200, self.first_run)

        # -- layout --------------------------------------------------------
        def build(self):
            root = self.root
            top = ttk.Frame(root, padding=(10, 10, 10, 4))
            top.pack(fill="x")
            ttk.Label(top, text="AddOns folder:").grid(row=0, column=0, sticky="w")
            self.path_var = tk.StringVar(value=self.cfg["addons_path"])
            ttk.Entry(top, textvariable=self.path_var).grid(row=0, column=1, sticky="ew", padx=6)
            ttk.Button(top, text="Browse…", command=self.browse).grid(row=0, column=2)
            ttk.Label(top, text="Game version:").grid(row=0, column=3, sticky="w", padx=(14, 0))
            self.flavor_var = tk.StringVar()
            self.flavor_box = ttk.Combobox(top, textvariable=self.flavor_var, state="readonly", width=24)
            self.flavor_box.grid(row=0, column=4, padx=(6, 0))
            self.flavor_box.bind("<<ComboboxSelected>>", self.on_flavor)
            top.columnconfigure(1, weight=1)

            bar = ttk.Frame(root, padding=(10, 4, 10, 8))
            bar.pack(fill="x")
            self.check_btn = ttk.Button(bar, text="Scan & check for updates", command=self.start_check)
            self.check_btn.pack(side="left")
            self.all_btn = ttk.Button(bar, text="Update all", command=self.update_all, state="disabled")
            self.all_btn.pack(side="left", padx=8)
            self.only_var = tk.BooleanVar(value=bool(self.cfg["only_updates"]))
            ttk.Checkbutton(bar, text="Show only addons with updates", variable=self.only_var,
                            command=self.render).pack(side="left", padx=8)
            ttk.Button(bar, text="Settings…", command=self.settings).pack(side="right")

            self.btn_px = 0
            table = ttk.Frame(root, padding=(10, 0, 10, 0))
            table.pack(fill="both", expand=True)
            self.header = tk.Frame(table, bg=HEAD_BG)
            self.header.pack(fill="x")
            body = tk.Frame(table, bd=1, relief="solid")
            body.pack(fill="both", expand=True)
            self.canvas = tk.Canvas(body, highlightthickness=0, bg=ROW_BG[0])
            self.scroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=self.scroll.set)
            self.scroll.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.inner = tk.Frame(self.canvas, bg=ROW_BG[0])
            self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
            self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
            self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
            root.bind_all("<MouseWheel>", self.on_wheel)
            root.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
            root.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))

            probe = ttk.Button(root, text="Open page", width=11)
            self.btn_px = probe.winfo_reqwidth() + 16
            probe.destroy()
            self.columns(self.header, pad_right=self.scroll.winfo_reqwidth() + 2)
            for col, (title, width) in enumerate(COLS):
                tk.Label(self.header, text=title, bg=HEAD_BG, anchor="w", width=width, padx=6,
                         pady=5).grid(row=0, column=col, sticky="ew")
            self.columns(self.inner)

            foot = ttk.Frame(root, padding=(10, 6, 10, 8))
            foot.pack(fill="x")
            self.status_var = tk.StringVar(value="Ready.")
            ttk.Label(foot, textvariable=self.status_var).pack(side="left", fill="x", expand=True)
            self.progress = ttk.Progressbar(foot, length=180, mode="determinate")
            self.progress.pack(side="right")
            self.refresh_flavors()

        def columns(self, frame, pad_right=0):
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_columnconfigure(len(COLS), minsize=self.btn_px + pad_right)

        def on_wheel(self, event):
            if self.inner.winfo_height() > self.canvas.winfo_height():
                self.canvas.yview_scroll(int(-event.delta / 120) * 3 or (-3 if event.delta > 0 else 3), "units")

        # -- flavour selection --------------------------------------------
        def refresh_flavors(self):
            names = ["Auto-detect from folder"] + ["%s  (%d)" % (name, fid) for fid, name in self.flavors]
            self.flavor_box["values"] = names
            current = int(self.cfg["flavor_id"] or 0)
            index = 0
            for i, (fid, _name) in enumerate(self.flavors, 1):
                if fid == current:
                    index = i
            self.flavor_box.current(index)

        def on_flavor(self, _e=None):
            i = self.flavor_box.current()
            self.cfg["flavor_id"] = 0 if i <= 0 else self.flavors[i - 1][0]
            self.cfg.save()

        def flavor_name(self, fid):
            return next((name for f, name in self.flavors if f == fid), "game version %d" % fid)

        # -- first run / settings -----------------------------------------
        def first_run(self):
            if not self.cfg["api_key"]:
                self.settings(first=True)
            if self.cfg["api_key"] and os.path.isdir(self.cfg["addons_path"]):
                self.start_check()

        def settings(self, first=False):
            win = tk.Toplevel(self.root)
            win.title("Settings")
            win.transient(self.root)
            win.resizable(False, False)
            frm = ttk.Frame(win, padding=14)
            frm.pack(fill="both", expand=True)
            intro = ("CurseForge only allows version checks and downloads through its official API, "
                     "which needs an API key. Apply for one with the form linked below, then paste it here. "
                     "It is stored in %s next to this program." % CONFIG_NAME)
            ttk.Label(frm, text=intro, wraplength=460, justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
            link = tk.Label(frm, text=API_KEY_URL, fg="#1565c0", cursor="hand2")
            link.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 10))
            link.bind("<Button-1>", lambda e: webbrowser.open(API_KEY_URL))
            ttk.Label(frm, text="API key:").grid(row=2, column=0, sticky="w")
            key_var = tk.StringVar(value=self.cfg["api_key"])
            key_entry = ttk.Entry(frm, textvariable=key_var, width=52, show="•")
            key_entry.grid(row=2, column=1, sticky="ew", padx=6)
            show_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(frm, text="Show", variable=show_var,
                            command=lambda: key_entry.configure(show="" if show_var.get() else "•")
                            ).grid(row=2, column=2)
            ttk.Label(frm, text="Releases:").grid(row=3, column=0, sticky="w", pady=(10, 0))
            chan_box = ttk.Combobox(frm, state="readonly", width=26, values=list(CHANNEL_LABELS.values()))
            chan_box.current(max(0, min(2, int(self.cfg["channel"]) - 1)))
            chan_box.grid(row=3, column=1, sticky="w", padx=6, pady=(10, 0))
            backup_var = tk.BooleanVar(value=bool(self.cfg["backup"]))
            ttk.Checkbutton(frm, text="Keep the previous version of each addon in Interface\\AddOns_Backup",
                            variable=backup_var).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))

            def save():
                self.cfg["api_key"] = key_var.get().strip()
                self.cfg["channel"] = chan_box.current() + 1
                self.cfg["backup"] = bool(backup_var.get())
                self.cfg.save()
                win.destroy()

            btns = ttk.Frame(frm)
            btns.grid(row=5, column=0, columnspan=3, sticky="e", pady=(14, 0))
            ttk.Button(btns, text="Save", command=save).pack(side="right")
            ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=6)
            key_entry.focus_set()
            win.bind("<Return>", lambda e: save())
            win.bind("<Escape>", lambda e: win.destroy())
            win.grab_set()
            self.root.wait_window(win)

        def browse(self):
            start = self.path_var.get() if os.path.isdir(self.path_var.get()) else ""
            chosen = filedialog.askdirectory(title="Select the Interface\\AddOns folder", initialdir=start)
            if chosen:
                self.path_var.set(os.path.normpath(chosen))

        # -- background work ----------------------------------------------
        def worker(self):
            while True:
                job = self.jobs.get()
                try:
                    job()
                except Exception as exc:  # last line of defence: never kill the worker
                    self.events.put(("status", "Unexpected error: %s" % exc))
                finally:
                    self.events.put(("job_done",))

        def submit(self, job):
            self.pending += 1
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self.jobs.put(job)

        def pump(self):
            try:
                while True:
                    ev = self.events.get_nowait()
                    kind = ev[0]
                    if kind == "status":
                        self.status_var.set(ev[1])
                    elif kind == "entries":
                        self.entries = ev[1]
                        self.render()
                    elif kind == "row":
                        self.refresh_row(ev[1])
                    elif kind == "flavors":
                        self.flavors, self.flavors_loaded = ev[1], True
                        self.refresh_flavors()
                    elif kind == "check_done":
                        self.checking = False
                        self.check_btn.configure(state="normal")
                    elif kind == "error":
                        messagebox.showerror(APP_NAME, ev[1], parent=self.root)
                    elif kind == "job_done":
                        self.pending -= 1
                        if self.pending <= 0:
                            self.pending = 0
                            self.progress.stop()
                            self.progress.configure(mode="determinate", value=0)
                        self.update_all_button()
            except queue.Empty:
                pass
            self.root.after(80, self.pump)

        def client(self):
            return CurseForge(self.cfg["api_key"], self.cfg["api_base"])

        def start_check(self):
            if self.checking:
                return
            path = os.path.normpath(self.path_var.get().strip().strip('"'))
            if not os.path.isdir(path):
                messagebox.showerror(APP_NAME, "This AddOns folder does not exist:\n\n%s" % path, parent=self.root)
                return
            self.cfg["addons_path"] = path
            self.cfg["only_updates"] = bool(self.only_var.get())
            self.cfg.save()
            if not self.cfg["api_key"]:
                self.settings(first=True)
                if not self.cfg["api_key"]:
                    self.submit(self.job_scan_only)
                    return
            self.checking = True
            self.check_btn.configure(state="disabled")
            self.submit(self.job_check)

        def job_scan_only(self):
            addons = scan_addons(self.cfg["addons_path"])
            entries = build_entries(addons, self.cfg.profile(), None)
            self.events.put(("entries", entries))
            self.events.put(("status", "%d addons found. Add a CurseForge API key under Settings "
                                       "to check for updates." % len(entries)))

        def job_check(self):
            say = lambda text: self.events.put(("status", text))
            try:
                say("Scanning %s…" % self.cfg["addons_path"])
                addons = scan_addons(self.cfg["addons_path"])
                profile = self.cfg.profile()
                client = self.client()
                if not self.flavors_loaded:
                    try:
                        live = client.version_types()
                        if live:
                            self.flavors = sorted(live, key=lambda t: t[1].lower())
                            self.events.put(("flavors", self.flavors))
                    except CurseForgeError as exc:
                        if "API key" in str(exc):
                            raise
                flavor_id = int(self.cfg["flavor_id"] or 0) or detect_flavor(self.cfg["addons_path"], self.flavors)
                if not flavor_id:
                    self.events.put(("entries", build_entries(addons, profile, None)))
                    self.events.put(("error", "Could not tell which game version this folder belongs to.\n\n"
                                              "Pick it in the 'Game version' list and check again."))
                    say("Pick a game version, then check again.")
                    return
                entries = build_entries(addons, profile, client, progress=say)
                check_entries(entries, profile, client, flavor_id, int(self.cfg["channel"]), progress=say)
                self.cfg.save()
                self.events.put(("entries", entries))
                updates = sum(1 for e in entries if e.status in (ST_UPDATE, ST_DIFF))
                unlinked = sum(1 for e in entries if e.status == ST_UNLINKED)
                text = "%d addons checked against %s — %d with updates" % (
                    len(entries), self.flavor_name(flavor_id), updates)
                if unlinked:
                    text += ", %d not linked to CurseForge" % unlinked
                say(text + ".")
            except CurseForgeError as exc:
                say(str(exc))
                self.events.put(("error", str(exc)))
            except OSError as exc:
                say("Could not read the AddOns folder: %s" % exc)
            finally:
                self.events.put(("check_done",))

        # -- table ---------------------------------------------------------
        def visible(self):
            order = {ST_UPDATE: 0, ST_DIFF: 0, ST_QUEUED: 0, ST_UPDATING: 0, ST_ERROR: 0, ST_MANUAL: 0,
                     ST_UPDATED: 1}
            items = sorted(self.entries, key=lambda e: (order.get(e.status, 2), e.name.lower()))
            if self.only_var.get():
                items = [e for e in items if order.get(e.status, 2) < 2]
            return items

        def render(self):
            for child in self.inner.winfo_children():
                child.destroy()
            self.rows = {}
            items = self.visible()
            if not items:
                text = "No addons to show." if self.entries else "Press 'Scan & check for updates' to begin."
                tk.Label(self.inner, text=text, bg=ROW_BG[0], fg="#777777", pady=24).grid(
                    row=0, column=0, columnspan=len(COLS) + 1)
            for r, e in enumerate(items):
                bg = ROW_BG[r % 2]
                cells = []
                for col, (_title, width) in enumerate(COLS):
                    lab = tk.Label(self.inner, bg=bg, anchor="w", width=width, padx=6, pady=4)
                    lab.grid(row=r, column=col, sticky="nsew")
                    cells.append(lab)
                holder = tk.Frame(self.inner, bg=bg)
                holder.grid(row=r, column=len(COLS), sticky="nsew")
                btn = ttk.Button(holder, width=11)
                btn.pack(padx=8, pady=2)
                name = cells[0]
                name.bind("<Button-3>", lambda ev, e=e: self.menu(ev, e))
                name.bind("<Double-Button-1>", lambda ev, e=e: e.url and webbrowser.open(e.url))
                Tooltip(name, lambda e=e: "Folders: " + ", ".join(e.folders) +
                        ("\nCurseForge project %s (matched by %s) — double-click to open the page, "
                         "right-click for options" % (e.project_id, e.matched_by) if e.project_id else
                         "\nRight-click for options"))
                Tooltip(cells[1], lambda e=e: e.installed)
                Tooltip(cells[2], lambda e=e: e.available)
                Tooltip(cells[3], lambda e=e: e.detail)
                cells[3].bind("<Button-1>", lambda ev, e=e: e.detail and messagebox.showinfo(
                    e.name, e.detail, parent=self.root))
                self.rows[e.key] = (e, cells, btn)
                self.refresh_row(e.key)
            self.canvas.yview_moveto(0)
            self.update_all_button()

        def refresh_row(self, key):
            row = self.rows.get(key)
            if not row:
                return
            e, cells, btn = row
            extra = "  (+%d folders)" % (len(e.folders) - 1) if len(e.folders) > 1 else ""
            cells[0].configure(text=e.name + extra)
            cells[1].configure(text=e.installed or "—")
            cells[2].configure(text=e.available or "—")
            shown = e.status + (" — click for details" if e.status == ST_ERROR and e.detail else "")
            cells[3].configure(text=shown, fg=COLORS.get(e.status, "#000000"),
                               cursor="hand2" if e.detail else "")
            if e.status == ST_UNLINKED:
                btn.configure(text="Link…", state="normal", command=lambda: self.link(e))
            elif e.status == ST_MANUAL and e.url:
                btn.configure(text="Open page", state="normal", command=lambda: webbrowser.open(e.url))
            elif e.can_update:
                btn.configure(text="Retry" if e.status == ST_ERROR else "Update", state="normal",
                              command=lambda: self.queue_update(e))
            elif e.status in (ST_QUEUED, ST_UPDATING):
                btn.configure(text="Update", state="disabled")
            else:
                btn.configure(text="Up to date" if e.status in (ST_OK, ST_UPDATED) else "—", state="disabled")
            self.update_all_button()

        def update_all_button(self):
            n = sum(1 for e in self.entries if e.can_update and e.status != ST_ERROR)
            self.all_btn.configure(text="Update all (%d)" % n if n else "Update all",
                                   state="normal" if n and not self.checking else "disabled")

        def menu(self, event, e):
            m = tk.Menu(self.root, tearoff=0)
            if e.url:
                m.add_command(label="Open CurseForge page", command=lambda: webbrowser.open(e.url))
            m.add_command(label="Open addon folder", command=lambda: self.open_folder(e))
            m.add_command(label="Link to a CurseForge project…", command=lambda: self.link(e))
            if e.project_id:
                m.add_command(label="Remove CurseForge link", command=lambda: self.unlink(e))
            m.tk_popup(event.x_root, event.y_root)

        def open_folder(self, e):
            path = os.path.join(self.cfg["addons_path"], e.folders[0])
            try:
                os.startfile(path)  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                webbrowser.open("file://" + path)

        # -- linking -------------------------------------------------------
        def link(self, e):
            text = simpledialog.askstring(
                "Link to CurseForge",
                "Paste the CurseForge page address or project ID for\n“%s”:\n\n"
                "Example: https://www.curseforge.com/wow/addons/tomtom" % e.name, parent=self.root)
            if not text:
                return
            kind, value = parse_link(text)
            if not kind:
                messagebox.showerror(APP_NAME, "That does not look like a CurseForge addon address or ID.",
                                     parent=self.root)
                return
            folders = list(e.folders)

            def job():
                try:
                    pid = value
                    if kind == "slug":
                        mod = self.client().search_slug(value)
                        if not mod:
                            self.events.put(("error", "CurseForge has no WoW addon at “%s”." % value))
                            return
                        pid = int(mod["id"])
                    links = self.cfg.profile()["links"]
                    for folder in folders:
                        links[folder] = pid
                    self.cfg.save()
                    self.events.put(("status", "Linked. Checking again…"))
                    self.job_check()
                except CurseForgeError as exc:
                    self.events.put(("error", str(exc)))
            self.submit(job)

        def unlink(self, e):
            prof = self.cfg.profile()
            for folder in e.folders:
                prof["links"][folder] = 0     # 0 = deliberately not linked
            prof["installed"].pop(str(e.project_id), None)
            self.cfg.save()
            self.start_check()

        # -- updating ------------------------------------------------------
        def queue_update(self, e):
            if e.status in (ST_QUEUED, ST_UPDATING) or not e.can_update:
                return
            e.status, e.detail = ST_QUEUED, ""
            self.refresh_row(e.key)

            def job():
                e.status = ST_UPDATING
                self.events.put(("row", e.key))
                self.events.put(("status", "Updating %s…" % e.name))
                try:
                    update_entry(e, self.cfg, self.client())
                    e.status, e.detail = ST_UPDATED, ""
                    self.events.put(("status", "%s updated to %s. Type /reload in game if WoW is running."
                                     % (e.name, e.available)))
                except ManualDownload as exc:
                    e.status, e.detail = ST_MANUAL, str(exc)
                    self.events.put(("status", "%s: %s" % (e.name, exc)))
                except (UpdateError, CurseForgeError) as exc:
                    e.status, e.detail = ST_ERROR, str(exc)
                    self.events.put(("status", "%s: %s" % (e.name, exc)))
                except OSError as exc:
                    e.status, e.detail = ST_ERROR, "File error: %s" % exc
                    self.events.put(("status", "%s: %s" % (e.name, e.detail)))
                self.events.put(("row", e.key))
            self.submit(job)

        def update_all(self):
            for e in list(self.visible()):
                if e.can_update and e.status != ST_ERROR:
                    self.queue_update(e)

        def on_close(self):
            self.cfg["geometry"] = self.root.geometry()
            self.cfg["only_updates"] = bool(self.only_var.get())
            self.cfg.save()
            self.root.destroy()

    root = tk.Tk()
    App(root)
    if selftest:            # build the window once, then quit (used by the CI build)
        root.update()
        root.destroy()
        return
    root.mainloop()


def main() -> None:
    if "--version" in sys.argv:
        print(APP_NAME, APP_VERSION)
        return
    if "--selftest" in sys.argv:
        try:
            os.environ["WAU_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="wau_"), CONFIG_NAME)
            run_gui(selftest=True)
        except BaseException:
            sys.exit(1)
        sys.exit(0)
    run_gui()


if __name__ == "__main__":
    main()
