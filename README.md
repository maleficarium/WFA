# WoW Addon Updater (GitHub edition)

Portable Windows tool: scans a WoW `Interface\AddOns` folder, finds the GitHub repository each
addon is released from, shows installed vs. latest released version, and updates addons one by
one (or all at once). It only updates addons you already have; it has no search or catalogue.

Single Python file, standard library only (Tkinter + urllib). Default folder:
`D:\World of Warcraft\_forever_\Interface\AddOns` (changeable in the window).

This is the `github-source` branch. The `master` branch holds the CurseForge edition, which needs
a CurseForge API key.

## No key needed

The tool reads the public GitHub Releases API. Without a token GitHub allows 60 requests per hour
from your connection, and one check uses one request per addon. An optional personal access token
(Settings; no extra permissions needed) raises that to 5,000. It is stored in plain text in
`WoWAddonUpdater-GitHub.json` next to the .exe.

## How it works

- **Installed version**: the `## Version:` line of each addon's `.toc` file.
- **Finding the repository**, in this order: a link you set by hand (**Link…** accepts an address
  such as `https://github.com/Questie/Questie`); a GitHub address in the addon's `.toc`
  (`X-Website`, `X-Github`, …); a small built-in list (`KNOWN_REPOS` in `addon_updater.py`).
  Nothing is guessed by searching GitHub, so the tool never installs from a look-alike repository.
- **Choosing the file**: the newest published release that has a zip for your game version.
  `_forever_` is detected from the folder path; others can be picked in the *Game version* list.
  The zip is chosen from the release's `release.json` if the author provides one, otherwise from
  the file name (`…-forever.zip`), otherwise the release's single all-versions zip. Drafts and
  pre-releases are skipped unless enabled in Settings. GitHub's "Source code" archives are never used.
- **Update**: downloads the zip, checks its size and SHA-256 (when GitHub reports one), confirms
  it contains the addon's folder, unpacks it beside the AddOns folder, then swaps the folders in.
  The previous version is kept in `Interface\AddOns_Backup` (one copy per addon; can be switched
  off). If anything fails the old folders are put back.
- After an addon has been updated through the tool, later checks compare the exact release file.
  Before that, comparison is by version text; where the two cannot be compared the row says
  **Different version** rather than guessing.
- Right-click an addon name for: open release page, open folder, change or remove the link.

## Limits

- Only addons whose authors publish packaged zips as GitHub releases can be updated. Others show
  **Not linked**, **No releases on GitHub** or **No file for this game version**.
- Two different addons released from one repository are treated as one entry.

## Building the .exe

PyInstaller cannot cross-compile: a Windows .exe has to be built by a Windows Python.

| Route | How | Status |
|---|---|---|
| GitHub Actions | Every push builds on a Windows runner; download the `WoWAddonUpdater-GitHub` artifact from the Actions tab | The build also starts the .exe once as a smoke test |
| On the Windows 11 PC | Install Python from python.org, double-click `build_windows.bat` | Standard PyInstaller route |
| On the Pi 5 | `./build_on_pi.sh` — needs x86-64 Wine + box64 | Experimental; the Wine step is untested |

To try the tool on the Pi itself: `sudo apt install python3-tk && python3 addon_updater.py`.

`python tools/live_check.py` prints what the tool would pick, from the real GitHub API, for the
built-in list.

## Notes

- Unsigned PyInstaller executables can trigger a Windows SmartScreen or antivirus warning.
- Close WoW before updating, or type `/reload` afterwards.
