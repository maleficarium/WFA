#!/usr/bin/env python3
"""
WoW Addon Updater (GitHub edition)
==================================
A small, portable Windows tool that scans a World of Warcraft AddOns folder,
finds the GitHub repository each addon is released from, shows installed vs.
latest released version and lets you update each addon with one click.

Standard library only (tkinter + urllib) so it packages into a small
single-file .exe with PyInstaller.

No account or key is needed: it reads the public GitHub Releases API. An
optional personal access token only raises GitHub's hourly request allowance.
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

APP_NAME = "WoW Addon Updater (GitHub)"
APP_VERSION = "1.1.0"
CONFIG_NAME = "WoWAddonUpdater-GitHub.json"

DEFAULT_ADDONS_PATH = r"D:\World of Warcraft\_forever_\Interface\AddOns"
API_BASE = "https://api.github.com"
TOKEN_URL = "https://github.com/settings/tokens"

# Game flavours: key, label, names used in a release.json manifest, words used in zip file names.
FLAVORS = [
    ("forever", "WoW Forever", ("forever",), ("forever", "camelot")),
    ("mainline", "Retail", ("mainline", "retail"), ("mainline", "retail")),
    ("vanilla", "Classic Era", ("classic", "vanilla"), ("vanilla", "classic", "era")),
    ("tbc", "TBC Classic", ("bcc", "tbc"), ("bcc", "tbc")),
    ("wrath", "Wrath Classic", ("wrath",), ("wrath", "wotlk")),
    ("cata", "Cataclysm Classic", ("cata",), ("cata", "cataclysm")),
    ("mists", "MoP Classic", ("mists",), ("mists", "mop")),
]
# install sub-folder -> flavour key
FOLDER_FLAVORS = {"_forever_": "forever", "_retail_": "mainline", "_classic_era_": "vanilla"}
# TOC interface numbers that identify a flavour when a manifest gives no flavour name
INTERFACE_RANGES = {"forever": (16000, 16999)}

# Addons whose TOC does not name their repository. Folder name (lower case) -> owner/repo.
# Each entry comes from the author's own project page; change or remove a link with right-click.
KNOWN_REPOS = {
    "questie": "Questie/Questie",
    "questiedb": "Questie/QuestieDB",
    "tomtom": "jnwhiteh/TomTom",
    "openmap": "openmap-world/openmap",
}

# Entry status values
ST_OK = "Up to date"
ST_UPDATE = "Update available"
ST_DIFF = "Different version"
ST_NEWER = "Newer than latest release"
ST_UNLINKED = "Not linked"
ST_NOFILE = "No file for this game version"
ST_NOREL = "No releases on GitHub"
ST_MISSING = "Repository not found"
ST_QUEUED = "Queued…"
ST_UPDATING = "Updating…"
ST_UPDATED = "Updated"
ST_ERROR = "Error"


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
        "token": "",             # optional GitHub personal access token
        "api_base": API_BASE,
        "flavor": "",            # "" = detect from the folder name
        "prerelease": False,
        "backup": True,
        "only_updates": False,
        "geometry": "",
        "profiles": {},          # per AddOns folder: links / installed
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
        prof.setdefault("links", {})       # folder -> owner/repo ("" = deliberately unlinked)
        prof.setdefault("installed", {})   # owner/repo (lower case) -> install record
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
    repo: str = ""              # owner/repo named in the TOC, if any
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
    for hint in ("forever", "camelot", "mainline", "vanilla", "classic"):
        for f in prefixed:
            if hint in f.lower():
                return os.path.join(folder_path, f)
    return os.path.join(folder_path, (prefixed or sorted(tocs))[0])


_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")
_GH_URL = re.compile(r"github\.com[/:]+([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+)", re.I)
_NOT_OWNERS = {"sponsors", "orgs", "users", "settings", "marketplace", "features", "topics",
               "about", "login", "apps", "collections"}


def parse_repo(text: str) -> str:
    """'https://github.com/Owner/Name/releases' or 'Owner/Name' -> 'Owner/Name'; '' if neither."""
    text = (text or "").strip()
    m = _GH_URL.search(text)
    if m:
        owner, name = m.group(1), m.group(2)
    elif _REPO.match(text):
        owner, name = text.split("/", 1)
    else:
        return ""
    name = name.rstrip(".")
    if name.lower().endswith(".git"):
        name = name[:-4]
    repo = "%s/%s" % (owner, name)
    return repo if name and owner.lower() not in _NOT_OWNERS and _REPO.match(repo) else ""


def toc_repo(fields: dict) -> str:
    """GitHub repository named anywhere in an addon's TOC metadata (X-Website, X-Github, ...)."""
    preferred = ("x-github", "x-repository", "x-repo", "x-source", "x-website", "x-url", "x-homepage")
    for key in list(preferred) + [k for k in fields if k not in preferred]:
        value = fields.get(key) or ""
        if "github.com" in value.lower():
            repo = parse_repo(value) if _GH_URL.search(value) else ""
            if repo:
                return repo
    return ""


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
        deps = f.get("requireddeps") or f.get("dependencies") or f.get("requireddep") or ""
        result.append(LocalAddon(
            folder=name,
            title=clean_title(f.get("title", "")) or name,
            version=version,
            interface=(f.get("interface") or "").strip(),
            repo=toc_repo(f),
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
# GitHub Releases client
# --------------------------------------------------------------------------

class GitHubError(Exception):
    pass


class RepoNotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    pass


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never forward the access token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            if urllib.parse.urlsplit(newurl).hostname != old_host:
                for store in (new.headers, new.unredirected_hdrs):
                    for name in [k for k in store if k.lower() == "authorization"]:
                        del store[name]
        return new


class GitHub:
    def __init__(self, token: str = "", base: str = API_BASE, timeout: int = 30):
        self.token = (token or "").strip()
        self.base = (base or API_BASE).rstrip("/")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_DropAuthOnRedirect())
        self._base = urllib.parse.urlsplit(self.base)
        self.remaining: int | None = None   # API requests left this hour, as reported by GitHub
        self._manifests: dict = {}

    # -- low level --------------------------------------------------------
    def _open(self, url: str, api: bool):
        headers = {"User-Agent": "WoWAddonUpdater/%s" % APP_VERSION}
        if api:
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
        return self.opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout)

    def _note_limit(self, headers) -> None:
        try:
            self.remaining = int(headers.get("x-ratelimit-remaining"))
        except (TypeError, ValueError, AttributeError):
            pass

    @staticmethod
    def _limit_message(headers) -> str:
        when = ""
        try:
            reset = _dt.datetime.fromtimestamp(int(headers.get("x-ratelimit-reset")))
            when = " It resets at %s." % reset.strftime("%H:%M")
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        return ("GitHub's request allowance for this hour is used up.%s An optional access token "
                "(Settings) raises the allowance from 60 to 5,000 requests per hour." % when)

    def _api(self, path: str, params: dict | None = None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        last = None
        for attempt in range(3):
            try:
                with self._open(url, api=True) as resp:
                    self._note_limit(resp.headers)
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                self._note_limit(exc.headers)
                limited = exc.headers.get("x-ratelimit-remaining") == "0" or exc.headers.get("retry-after")
                if exc.code in (403, 429) and limited:
                    raise RateLimited(self._limit_message(exc.headers)) from None
                if exc.code == 401:
                    raise GitHubError("GitHub rejected the access token (HTTP 401). "
                                      "Correct or clear it under Settings.") from None
                if exc.code in (404, 410, 451):
                    raise RepoNotFound("GitHub has no public repository at this address.") from None
                last = "GitHub returned HTTP %d." % exc.code
                if exc.code not in (500, 502, 503, 504):
                    break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last = "Could not reach GitHub: %s" % getattr(exc, "reason", exc)
            time.sleep(1.5 * (attempt + 1))
        raise GitHubError(last or "GitHub request failed.")

    def _trusted(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == "https" and parts.hostname == "github.com":
            return True
        return parts.scheme == self._base.scheme and parts.netloc == self._base.netloc  # test server

    # -- endpoints --------------------------------------------------------
    def releases(self, repo: str) -> list:
        data = self._api("/repos/%s/releases" % repo, {"per_page": 15})
        return data if isinstance(data, list) else []

    def manifest(self, url: str) -> dict | None:
        """Fetch a release.json asset (small file written by the BigWigs packager)."""
        if url in self._manifests:
            return self._manifests[url]
        result = None
        if self._trusted(url):
            try:
                with self._open(url, api=False) as resp:
                    parsed = json.loads(resp.read(2 << 20).decode("utf-8"))
                    result = parsed if isinstance(parsed, dict) else None
            except (urllib.error.URLError, OSError, ValueError):
                result = None
        self._manifests[url] = result
        return result

    def download(self, url: str, dest: str, progress=None) -> None:
        if not self._trusted(url):
            raise GitHubError("Unexpected download address: %s" % url)
        try:
            with self._open(url, api=False) as resp, open(dest, "wb") as out:
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
            raise GitHubError("Download failed (HTTP %d)." % exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise GitHubError("Download failed: %s" % getattr(exc, "reason", exc)) from None


# --------------------------------------------------------------------------
# Matching installed addons to GitHub repositories and checking versions
# --------------------------------------------------------------------------

@dataclass
class Entry:
    key: str
    name: str
    folders: list
    installed: str = ""
    repo: str = ""              # owner/repo
    matched_by: str = ""        # toc / linked / built-in list / installed / module
    url: str = ""
    available: str = ""
    latest: dict | None = None  # {"release": ..., "asset": ..., "how": ...}
    status: str = ST_UNLINKED
    detail: str = ""
    primary: str = ""           # folder whose TOC supplies the version

    @property
    def can_update(self) -> bool:
        return self.latest is not None and self.status in (ST_UPDATE, ST_DIFF, ST_ERROR)


def detect_flavor(addons_path: str) -> str:
    """Pick the game flavour from the install sub-folder (_forever_, _retail_, ...)."""
    parts = [p.lower() for p in re.split(r"[\\/]+", addons_path or "")]
    for folder, key in FOLDER_FLAVORS.items():
        if folder in parts:
            return key
    return ""


def flavor_label(key: str) -> str:
    return next((label for k, label, _m, _w in FLAVORS if k == key), key)


def _related(folder: str, dep: str) -> bool:
    """True when `folder` looks like a module of `dep` (DBM-Raids -> DBM-Core)."""
    f, d = folder.lower(), dep.lower()
    if f == d:
        return False
    head = lambda s: re.split(r"[-_]", s)[0]
    return f.startswith(d) or (len(head(f)) >= 3 and head(f) == head(d))


def build_entries(addons: list, profile: dict) -> list:
    """Group addon folders by GitHub repository. Unmatched folders get their own entry."""
    by_folder = {a.folder: a for a in addons}
    assigned: dict = {}   # folder -> (owner/repo, how)
    blocked = {f for f, link in profile["links"].items() if link == ""}  # unlinked by the user

    for a in addons:
        if a.folder in blocked:
            continue
        link = parse_repo(profile["links"].get(a.folder) or "")
        known = KNOWN_REPOS.get(a.folder.lower())
        if link:
            assigned[a.folder] = (link, "linked")
        elif a.repo:
            assigned[a.folder] = (a.repo, "toc")
        elif known:
            assigned[a.folder] = (known, "built-in list")
    for rec in profile["installed"].values():
        for folder in rec.get("folders", []):
            if folder in by_folder and folder not in assigned and folder not in blocked and rec.get("repo"):
                assigned[folder] = (rec["repo"], "installed")

    # Modules without their own link inherit the repository of the addon they extend.
    for _ in range(3):
        for a in addons:
            if a.folder in assigned or a.folder in blocked:
                continue
            for dep in a.deps:
                if dep in assigned and _related(a.folder, dep):
                    assigned[a.folder] = (assigned[dep][0], "module")
                    break

    groups: dict = {}
    for a in addons:
        repo, how = assigned.get(a.folder, ("", ""))
        groups.setdefault("r:" + repo.lower() if repo else "f:" + a.folder, []).append((a, how, repo))
    entries = []
    for key, members in groups.items():
        folders = {a.folder for a, _how, _repo in members}
        needed = {d for a, _how, _repo in members for d in a.deps if d in folders}
        # main folder: not a module, preferably the one the others depend on, then the shortest name
        main, how, repo = min(members, key=lambda m: (m[1] == "module", m[0].folder not in needed,
                                                      len(m[0].folder), m[0].folder.lower()))
        version = main.version or next((a.version for a, _h, _r in members if a.version), "")
        entries.append(Entry(key=key, name=main.title, folders=sorted(folders, key=str.lower),
                             installed=version, repo=repo, matched_by=how, primary=main.folder))
    return entries


# -- choosing the right file of a release ------------------------------------

def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def asset_flavors(name: str, folder: str = "") -> set:
    """Game flavours named in a zip file name (the addon's own name is ignored)."""
    low = name.lower()
    if folder and low.startswith(folder.lower()):
        low = low[len(folder):]
    return {key for key, _label, _meta, words in FLAVORS
            if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", low) for w in words)}


def pick_asset(release: dict, flavor: str, folder: str, client: GitHub | None, named: bool = False):
    """Return (asset, how) for the zip of `release` that fits the game flavour, or None.

    With named=True only zips whose file name starts with the addon's folder name count,
    which keeps repositories that publish several addons apart.
    """
    assets = release.get("assets") or []
    zips = [a for a in assets if (a.get("name") or "").lower().endswith(".zip")
            and a.get("state", "uploaded") == "uploaded"
            and (not named or _alnum(a["name"]).startswith(_alnum(folder)))]
    if not zips:
        return None
    by_name = {a["name"]: a for a in zips}
    meta_names = next((set(meta) for key, _l, meta, _w in FLAVORS if key == flavor), set())
    lo, hi = INTERFACE_RANGES.get(flavor, (1, 0))
    rank = lambda name: (not _alnum(name).startswith(_alnum(folder)), "nolib" in name.lower(), len(name))

    # 1. release.json, where the author states which file is for which game version
    listed = set()
    manifest_asset = next((a for a in assets if (a.get("name") or "").lower() == "release.json"), None)
    if manifest_asset and client:
        manifest = client.manifest(manifest_asset.get("browser_download_url") or "") or {}
        rows = []
        for item in manifest.get("releases") or []:
            fname = item.get("filename")
            if fname not in by_name:
                continue
            listed.add(fname)
            if item.get("nolib"):
                continue
            meta = [m for m in (item.get("metadata") or []) if isinstance(m, dict)]
            names = {str(m.get("flavor") or "").lower() for m in meta}
            fits = bool(names & meta_names) or any(
                isinstance(m.get("interface"), int) and lo <= m["interface"] <= hi for m in meta)
            if fits:
                rows.append((len(names), rank(fname), fname))
        if rows:
            return by_name[min(rows)[2]], "release.json"

    # 2. a file whose name says it is for this game version
    specific = [a for a in zips if flavor in asset_flavors(a["name"], folder)]
    if specific:
        return min(specific, key=lambda a: rank(a["name"])), "file name"

    # 3. a file that names no game version at all (one zip for every version)
    generic = [a for a in zips if a["name"] not in listed and not asset_flavors(a["name"], folder)
               and "nolib" not in a["name"].lower()]
    if generic:
        return min(generic, key=lambda a: rank(a["name"])), "single file"
    return None


def pick_release(releases: list, flavor: str, folder: str, allow_pre: bool, client: GitHub | None):
    """Newest published release that has a file for the game flavour: (release, asset, how) or None."""
    usable = [r for r in releases if isinstance(r, dict) and not r.get("draft")
              and (allow_pre or not r.get("prerelease"))]
    usable.sort(key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
    for named in (True, False):     # first by file name, then any zip in the release
        for release in usable[:8]:
            found = pick_asset(release, flavor, folder, client, named)
            if found:
                return release, found[0], found[1]
    return None


def evaluate(entry: Entry, profile: dict) -> None:
    """Set entry.status by comparing the installed version with entry.latest."""
    release, asset = entry.latest["release"], entry.latest["asset"]
    entry.available = release.get("tag_name") or release.get("name") or asset.get("name", "")
    entry.url = release.get("html_url") or "https://github.com/%s/releases" % entry.repo
    rec = profile["installed"].get(entry.repo.lower())
    if rec and rec.get("toc_version", None) == entry.installed and rec.get("asset_id"):
        # Installed through this tool and untouched since: compare the exact release file.
        if rec["asset_id"] == asset.get("id"):
            entry.status = ST_OK
        elif (release.get("published_at") or "") >= (rec.get("published_at") or ""):
            entry.status = ST_UPDATE
        else:
            entry.status = ST_NEWER
        return
    result = compare_versions(entry.installed, release.get("tag_name", ""), asset.get("name", ""))
    if result == "unknown" and release.get("name"):
        result = compare_versions(entry.installed, release["name"])
    entry.status = {"same": ST_OK, "older": ST_UPDATE, "newer": ST_NEWER}.get(result, ST_DIFF)
    if entry.status == ST_DIFF:
        entry.detail = ("The installed version text does not match the latest release, so it may be "
                        "out of date. Updating installs the latest release.")


def check_entries(entries: list, profile: dict, client: GitHub, flavor: str,
                  allow_pre: bool, progress=None) -> None:
    linked = [e for e in entries if e.repo]
    limited = ""
    for n, e in enumerate(linked, 1):
        e.url = "https://github.com/%s/releases" % e.repo
        if limited:
            e.status, e.detail = ST_ERROR, limited
            continue
        if progress:
            progress("Checking %s on GitHub… (%d/%d)" % (e.name, n, len(linked)))
        try:
            releases = client.releases(e.repo)
        except RepoNotFound as exc:
            e.status, e.detail = ST_MISSING, "%s (%s)" % (exc, e.repo)
            continue
        except RateLimited as exc:
            limited = str(exc)
            e.status, e.detail = ST_ERROR, limited
            continue
        if not releases:
            e.status = ST_NOREL
            e.detail = "%s has not published any releases, so there is nothing to compare with." % e.repo
            continue
        found = pick_release(releases, flavor, e.primary, allow_pre, client)
        if not found:
            e.status = ST_NOFILE
            e.detail = ("None of the recent releases of %s has a zip file for %s."
                        % (e.repo, flavor_label(flavor)))
            continue
        e.latest = {"release": found[0], "asset": found[1], "how": found[2]}
        evaluate(e, profile)
    if limited:
        raise RateLimited(limited)


# --------------------------------------------------------------------------
# Installing an update
# --------------------------------------------------------------------------

class UpdateError(Exception):
    pass


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


def install_zip(zip_path: str, addons_path: str, backup: bool = True, expect: set | None = None) -> list:
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
            if expect and not ({t.lower() for t in tops} & {f.lower() for f in expect}):
                raise UpdateError("The release file does not contain this addon (it holds: %s). "
                                  "Check the repository link." % ", ".join(tops))
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


def update_entry(entry: Entry, cfg: Config, client: GitHub, progress=None) -> None:
    """Download and install the release file in entry.latest, then record what was installed."""
    if not entry.latest:
        raise UpdateError("Nothing to install.")
    release, asset = entry.latest["release"], entry.latest["asset"]
    url = asset.get("browser_download_url") or ""
    if not url:
        raise UpdateError("The release file has no download address.")
    addons_path = cfg["addons_path"]
    fd, tmp = tempfile.mkstemp(prefix="wau_", suffix=".zip")
    os.close(fd)
    try:
        client.download(url, tmp, progress)
        size = int(asset.get("size") or 0)
        if size and os.path.getsize(tmp) != size:
            raise UpdateError("The download is incomplete (size mismatch). Try again.")
        digest = str(asset.get("digest") or "")
        if digest.lower().startswith("sha256:"):
            sha = hashlib.sha256()
            with open(tmp, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    sha.update(block)
            if sha.hexdigest().lower() != digest.split(":", 1)[1].strip().lower():
                raise UpdateError("The download failed its checksum. Try again.")
        folders = install_zip(tmp, addons_path, backup=bool(cfg["backup"]), expect=set(entry.folders))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    # Re-read the installed version from the new files and remember the exact release file.
    fresh = {a.folder: a for a in scan_addons(addons_path) if a.folder in folders}
    new_version = fresh[entry.primary].version if entry.primary in fresh else ""
    if not new_version:
        new_version = next((a.version for a in fresh.values() if a.version), "")
    entry.installed = new_version or entry.installed
    entry.folders = sorted(set(entry.folders) | set(folders), key=str.lower)
    profile = cfg.profile()
    profile["installed"][entry.repo.lower()] = {
        "repo": entry.repo,
        "tag": release.get("tag_name", ""),
        "asset_id": asset.get("id"),
        "asset": asset.get("name", ""),
        "published_at": release.get("published_at") or "",
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
        ST_ERROR: "#c62828", ST_MISSING: "#c62828", ST_UNLINKED: "#777777",
        ST_NOFILE: "#777777", ST_NOREL: "#777777", ST_NEWER: "#1565c0", ST_QUEUED: "#555555", ST_UPDATING: "#1565c0",
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
            self.flavor_box["values"] = ["Auto-detect from folder"] + [label for _k, label, _m, _w in FLAVORS]
            keys = [f[0] for f in FLAVORS]
            current = self.cfg["flavor"]
            self.flavor_box.current(keys.index(current) + 1 if current in keys else 0)

        def on_flavor(self, _e=None):
            i = self.flavor_box.current()
            self.cfg["flavor"] = "" if i <= 0 else FLAVORS[i - 1][0]
            self.cfg.save()

        # -- first run / settings -----------------------------------------
        def first_run(self):
            if os.path.isdir(self.cfg["addons_path"]):
                self.start_check()

        def settings(self):
            win = tk.Toplevel(self.root)
            win.title("Settings")
            win.transient(self.root)
            win.resizable(False, False)
            frm = ttk.Frame(win, padding=14)
            frm.pack(fill="both", expand=True)
            intro = ("No account is needed. Without a token GitHub allows 60 requests per hour from your "
                     "connection, and each check uses one request per addon. A personal access token with "
                     "no extra permissions raises this to 5,000. It is stored in %s next to this "
                     "program." % CONFIG_NAME)
            ttk.Label(frm, text=intro, wraplength=460, justify="left").grid(row=0, column=0, columnspan=3, sticky="w")
            link = tk.Label(frm, text=TOKEN_URL, fg="#1565c0", cursor="hand2")
            link.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 10))
            link.bind("<Button-1>", lambda e: webbrowser.open(TOKEN_URL))
            ttk.Label(frm, text="Token (optional):").grid(row=2, column=0, sticky="w")
            key_var = tk.StringVar(value=self.cfg["token"])
            key_entry = ttk.Entry(frm, textvariable=key_var, width=48, show="•")
            key_entry.grid(row=2, column=1, sticky="ew", padx=6)
            show_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(frm, text="Show", variable=show_var,
                            command=lambda: key_entry.configure(show="" if show_var.get() else "•")
                            ).grid(row=2, column=2)
            pre_var = tk.BooleanVar(value=bool(self.cfg["prerelease"]))
            ttk.Checkbutton(frm, text="Include pre-releases (alpha, beta and development builds)",
                            variable=pre_var).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
            backup_var = tk.BooleanVar(value=bool(self.cfg["backup"]))
            ttk.Checkbutton(frm, text="Keep the previous version of each addon in Interface\\AddOns_Backup",
                            variable=backup_var).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

            def save():
                self.cfg["token"] = key_var.get().strip()
                self.cfg["prerelease"] = bool(pre_var.get())
                self.cfg["backup"] = bool(backup_var.get())
                self.cfg.save()
                win.destroy()

            btns = ttk.Frame(frm)
            btns.grid(row=5, column=0, columnspan=3, sticky="e", pady=(14, 0))
            ttk.Button(btns, text="Save", command=save).pack(side="right")
            ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=6)
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
            return GitHub(self.cfg["token"], self.cfg["api_base"])

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
            self.checking = True
            self.check_btn.configure(state="disabled")
            self.submit(self.job_check)

        def job_check(self):
            say = lambda text: self.events.put(("status", text))
            entries = []
            try:
                say("Scanning %s…" % self.cfg["addons_path"])
                addons = scan_addons(self.cfg["addons_path"])
                profile = self.cfg.profile()
                entries = build_entries(addons, profile)
                flavor = self.cfg["flavor"] or detect_flavor(self.cfg["addons_path"])
                if not flavor:
                    self.events.put(("entries", entries))
                    self.events.put(("error", "Could not tell which game version this folder belongs to.\n\n"
                                              "Pick it in the 'Game version' list and check again."))
                    say("Pick a game version, then check again.")
                    return
                client = self.client()
                try:
                    check_entries(entries, profile, client, flavor, bool(self.cfg["prerelease"]), progress=say)
                finally:
                    self.events.put(("entries", entries))
                updates = sum(1 for e in entries if e.status in (ST_UPDATE, ST_DIFF))
                unlinked = sum(1 for e in entries if e.status == ST_UNLINKED)
                text = "%d addons checked for %s — %d with updates" % (
                    len(entries), flavor_label(flavor), updates)
                if unlinked:
                    text += ", %d not linked to a GitHub repository" % unlinked
                text += "."
                if client.remaining is not None and not self.cfg["token"]:
                    text += "  GitHub requests left this hour: %d." % client.remaining
                say(text)
            except GitHubError as exc:
                say(str(exc))
                self.events.put(("error", str(exc)))
            except OSError as exc:
                say("Could not read the AddOns folder: %s" % exc)
            finally:
                self.events.put(("check_done",))

        # -- table ---------------------------------------------------------
        def visible(self):
            order = {ST_UPDATE: 0, ST_DIFF: 0, ST_QUEUED: 0, ST_UPDATING: 0, ST_ERROR: 0, ST_UPDATED: 1}
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
                        ("\nGitHub: %s (matched by %s) — double-click to open the release page, "
                         "right-click for options" % (e.repo, e.matched_by) if e.repo else
                         "\nNot linked to a GitHub repository — use Link…, or right-click for options"))
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
            elif e.status == ST_NOFILE and e.url:
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
                m.add_command(label="Open GitHub release page", command=lambda: webbrowser.open(e.url))
            m.add_command(label="Open addon folder", command=lambda: self.open_folder(e))
            m.add_command(label="Link to a GitHub repository…", command=lambda: self.link(e))
            if e.repo:
                m.add_command(label="Remove GitHub link", command=lambda: self.unlink(e))
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
                "Link to GitHub",
                "Paste the GitHub repository address for\n“%s”:\n\n"
                "Example: https://github.com/Questie/Questie" % e.name,
                initialvalue=("https://github.com/" + e.repo) if e.repo else "", parent=self.root)
            if not text:
                return
            repo = parse_repo(text)
            if not repo:
                messagebox.showerror(APP_NAME, "That does not look like a GitHub repository address.\n\n"
                                               "Expected something like https://github.com/owner/name",
                                     parent=self.root)
                return
            folders = list(e.folders)

            def job():
                try:
                    self.client().releases(repo)      # confirms the repository exists and is public
                except RepoNotFound:
                    self.events.put(("error", "GitHub has no public repository at “%s”." % repo))
                    return
                except GitHubError as exc:
                    self.events.put(("error", str(exc)))
                    return
                links = self.cfg.profile()["links"]
                for folder in folders:
                    links[folder] = repo
                self.cfg.save()
                self.events.put(("status", "Linked to %s. Checking again…" % repo))
                self.job_check()
            self.submit(job)

        def unlink(self, e):
            prof = self.cfg.profile()
            for folder in e.folders:
                prof["links"][folder] = ""    # empty = deliberately not linked
            prof["installed"].pop(e.repo.lower(), None)
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
                except (UpdateError, GitHubError) as exc:
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
