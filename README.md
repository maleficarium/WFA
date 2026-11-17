# WoW Addon Updater

Portable Windows tool: scans a WoW `Interface\AddOns` folder, looks each addon up on CurseForge,
shows installed vs. available version, and updates addons one by one (or all at once).

Single Python file, standard library only (Tkinter + urllib). Default folder:
`D:\World of Warcraft\_forever_\Interface\AddOns` (changeable in the window).

## CurseForge API key (required)

CurseForge's website blocks automated requests, and since July 2026 its download servers also
reject requests without a key. The tool therefore uses the official CurseForge API.

1. Apply for a key with the form linked from
   https://support.curseforge.com/support/solutions/articles/9000208346
   (describe it as a personal, single-user updater).
2. Start the tool and paste the key into the Settings dialog.

The key is stored in plain text in `WoWAddonUpdater.json` next to the .exe. Keep that file private
and never commit it: no key is included in this repository or in the built .exe.

## How it works

- **Installed version**: the `## Version:` line of each addon's `.toc` file.
- **Finding the addon on CurseForge**, in this order: `## X-Curse-Project-ID:` in the `.toc`;
  a link you set by hand (the **Link…** button accepts a page address such as
  `https://www.curseforge.com/wow/addons/tomtom`, or a project ID); the folder name looked up as a
  CurseForge address. Module folders (e.g. `DBM-StatusBarTimers`) are grouped under their main addon.
- **Available version**: the newest file tagged for the game version. `_forever_` is detected
  from the folder path; any other can be picked in the *Game version* list.
- **Update**: downloads the zip, checks size and SHA-1, unpacks it beside the AddOns folder, then
  swaps the folders in. The previous version is kept in `Interface\AddOns_Backup` (one copy per
  addon; can be switched off). If anything fails the old folders are put back.
- After an addon has been updated through the tool, later checks compare exact CurseForge file IDs.
  Before that, comparison is by version text; where the two texts cannot be compared the row says
  **Different version** rather than guessing.
- Right-click an addon name for: open CurseForge page, open folder, change or remove the link.

## Building the .exe

PyInstaller cannot cross-compile: a Windows .exe has to be built by a Windows Python.

| Route | How | Status |
|---|---|---|
| On the Windows 11 PC | Install Python from python.org, double-click `build_windows.bat` | Standard PyInstaller route |
| GitHub Actions | Every push builds on a Windows runner; download the `WoWAddonUpdater` artifact from the Actions tab | Standard PyInstaller route; the build also starts the .exe once as a smoke test |
| On the Pi 5 | `./build_on_pi.sh` — needs x86-64 Wine + box64 | Experimental; the Wine step is untested |

Result: `dist\WoWAddonUpdater.exe` (single file, no installer).

To try the tool on the Pi itself: `sudo apt install python3-tk && python3 addon_updater.py`.

## Notes

- Unsigned PyInstaller executables can trigger a Windows SmartScreen or antivirus warning.
- Some authors disable downloads outside the CurseForge app; those rows show **Manual download
  only** with an **Open page** button.
- Close WoW before updating, or type `/reload` afterwards.
