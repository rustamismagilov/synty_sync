# synty_sync

Sync your Synty Store library to a local folder. Detects new packs, version bumps, and skips files you already have.

> **Windows only.** Tested on Windows 11 with PowerShell. May work on macOS/Linux but has not yet been verified.

## WHY

Built this mostly for myself, I'm a bit of a digital hoarder and wanted a way to keep my full Synty library on disk and in sync without manually clicking through dozens of packs every time something updates.

THIS IS A TOOL FOR DOWNLOADING LEGALLY ACQUIRED SYNTY PACKS FROM THE OFFICIAL SYNTY WEBSITE (https://syntystore.com). SYNTY'S TERMS OF SERVICE APPLY TO THE ASSETS; THE SCRIPT DOES NOT CHANGE ANYTHING ABOUT LICENSING OR ATTRIBUTION.

## HOW

### Quick start

```powershell
git clone https://github.com/rustamismagilov/synty_sync.git
cd synty_sync
pip install -r requirements.txt
python synty_sync.py --path "path/to/folder" --dry-run
```

The first run with `--dry-run` shows you exactly what will be downloaded and where, without touching the filesystem. Drop `--dry-run` to actually download.

### Cookies setup

The script authenticates by reading cookies your browser already has after you log into syntystore.com. There is no automated login.

1. Log into https://syntystore.com in your browser.
2. Install **Get cookies.txt LOCALLY** for [Firefox](https://addons.mozilla.org/en-US/firefox/addon/get-cookies-txt-locally/) or [Chrome](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc).
3. While on syntystore.com, click the extension and export cookies as `cookies.txt`.
4. Save it next to `synty_sync.py` (or pass `--cookies "path/to/cookies.txt"`).

When cookies expire, the script reports "Could not detect customer id" or "Authentication failed". Repeat the process to refresh.

### CLI flags

| Flag | Description |
|---|---|
| `--path "path/to/folder"` | **Required.** Local folder where pack subfolders live. |
| `--cookies "path/to/cookies.txt"` | Path to `cookies.txt` (Netscape format). Defaults to `cookies.txt` next to the script. |
| `--dry-run` | Print the plan and exit. No files written. |
| `--force` | Re-download every file, ignoring local versions. |
| `--pack "substring"` | Only sync packs whose title contains this substring. Repeatable. |
| `--no-icons` | Skip ICON `.png` / `.jpg` files. |
| `--formats unity,unreal,godot,source` | Comma-separated engine variants to include. Omit to include all. |
| `--latest-only` | Per pack, keep only the newest version of each engine family. |
| `--prune-old` | Keep only the newest version of each build. Older copies go to the Recycle Bin. Combine with `--dry-run` to preview. |
| `--workers N` | Concurrent downloads. Default 4. |

### Common workflows

```powershell
# Preview
python synty_sync.py --path "path/to/folder" --dry-run
```

```powershell
# Preview if there is something new to download/upgrade in packs with "Sci-Fi" in the title
python synty_sync.py --path "path/to/folder" --pack "Sci-Fi" --dry-run
```

```powershell
# Preview if there is something new to download/upgrade for the latest Unity packs without icons
python synty_sync.py --path "path/to/folder" --formats unity --latest-only --no-icons --dry-run
```

```powershell
# Preview packs with "Battle Royale" in the title using 1 worker
python synty_sync.py --path "path/to/folder" --pack "Battle Royale" --workers 1 --dry-run
```

```powershell
# Sync everything to a specified folder
python synty_sync.py --path "path/to/folder"
```

```powershell
# Sync everything to a specified folder with 8 workers (faster, but can hit download limits)
python synty_sync.py --path "path/to/folder" --workers 8
```

```powershell
# Sync everything with a custom cookies file path (can be in a different folder/disk)
python synty_sync.py --path "path/to/folder" --cookies "C:/secrets/cookies.txt"
```

```powershell
# Sync only the specified pack to a specified folder (e.g. POLYGON - City pack)
python synty_sync.py --path "path/to/folder" --pack "POLYGON - City"
```

```powershell
# Sync packs whose title contains "POLYGON" OR "SIMPLE"
python synty_sync.py --path "path/to/folder" --pack "POLYGON" --pack "SIMPLE"
```

```powershell
# Sync only latest Unity packs to a specified folder
python synty_sync.py --path "path/to/folder" --formats unity --latest-only
```

```powershell
# Sync only source files without icons to a specified folder
python synty_sync.py --path "path/to/folder" --formats source --no-icons
```

```powershell
# Sync only Unity and Unreal files without icons to a specified folder
python synty_sync.py --path "path/to/folder" --formats unity,unreal --no-icons
```

```powershell
# Sync only latest Unity packs without icons to a specified folder
python synty_sync.py --path "path/to/folder" --formats unity --latest-only --no-icons
```

```powershell
# Force re-download EVERY file (useful if something seems corrupted)
python synty_sync.py --path "path/to/folder" --force
```

```powershell
# Re-download EVERY file from only "POLYGON - City pack" to a specified folder
python synty_sync.py --path "path/to/folder" --pack "POLYGON - City" --force
```

### Upgrade detection

For each remote file the script identifies a **slot** based on `(base_name, engine_variant)`. For example:

- `POLYGON_BattleRoyale_Unity_2022_3` is one slot
- `POLYGON_BattleRoyale_Unity_2021_3` is a different slot
- `POLYGON_BattleRoyale_Unreal_5_4` is yet another

Within each slot, the local pack folder is scanned for files of the same slot. Three actions can result:

- **`first-download`**: no file in this slot exists locally; download it.
- **`new-version`**: a same-slot file exists locally with a different version; download the new one. The old file is **kept by default**. Pass `--prune-old` to delete it after the new file lands.
- **`skip`**: same version (or a newer one) is already on disk. Nothing happens.

With `--prune-old`, the script also cleans up older versions sitting in your pack folders. For each slot, only the newest file stays on disk. Everything older is sent to the OS trash (Recycle Bin on Windows, Trash on macOS / Linux). Nothing is permanently deleted — you can restore from the trash if you change your mind. The output adds a **PRUNE LIST** showing exactly which files will be removed, plus a `prune` row in the summary with the total count and size. Always run with `--dry-run` first to preview.

Want to keep an old version forever? Rename it to include `_ARCHIVED`, `_BACKUP`, or `_KEEP` anywhere in the name (any case) — those files are skipped on every future prune.

`--latest-only` collapses multiple engine-version slots into one group keyed by **engine family** (Unity / Unreal / Godot / Source). The newest `(engine_version, pack_version)` wins. So an asset with Unreal 4.25, 5.0, and 5.4 builds will plan only the 5.4 build under `--latest-only`.

`--no-icons` excludes ICON files. `--formats` excludes engine variants you don't want, but never affects icons, so use both flags together if needed.

### Recognized filename patterns

Synty's file naming is inconsistent across packs, but this tool handles every variant. Examples:

| Example | Notes |
|---|---|
| `POLYGON_BattleRoyale_Unity_2022_3_v1_9_0.unitypackage` | Named engine + pack version |
| `POLYGON_Halloween_Masks_2022_3_v1_2_0.unitypackage` | Bare engine version (no `Unity_` prefix) |
| `SIMPLE_Space_Unreal_4.15_v1_0_0.zip` | Engine version with a `.` separator |
| `POLYGON_BattleRoyale_Source_Files_v4.zip` | Source files with version |
| `SIMPLE_Cars_SourceFiles.zip` | Source files without version |
| `INTERFACE_Apocalypse_HUD_Source_Sprites_v3.zip` | Any `Source_*` variant, not just `Source_Files` |
| `SidekickCharacterTool_0_4_0_UE53.zip` | UE family, no `_v…` version suffix |
| `POLYGON_Fantasy_Characters_ICON.jpg` | Icon, any common image extension (`.png` / `.jpg` / `.jpeg` / `.webp`) |

### Output

After parsing, you will be able to see a table of every planned file with columns `Status / Pack / New file / Destination`, and a summary table showing per-action count and total size, e.g.:

```
SUMMARY:
╭──────────────────────┬─────────────╮
│ Action               │ Count       │
├──────────────────────┼─────────────┤
│ first-download       │ 43 (2.3GB)  │
├──────────────────────┼─────────────┤
│ new-version          │ 1 (0.1GB)   │
├──────────────────────┼─────────────┤
│ skip                 │ 357 (25GB)  │
├──────────────────────┼─────────────┤
│ TOTAL TO DOWNLOAD:   │ 44 (2.4GB)  │
╰──────────────────────┴─────────────╯
```

In `--dry-run` the script stops after parsing the library. In normal mode it prompts if you actually want to download files (after previewing summary and all files):

```
[•] Download 44 files (2.4GB)? [Y/n]:
```

Press `Y` or `Enter` to start the download, anything else to abort. After download finishes:

- If all files are OK, you will see: `[✓] Done.`
- If some files failed, you will see an interactive prompt `[•] N download(s) failed. Retry? [Y/n]:`. Press `Y` or `Enter` to re-run only the failed items, press anything else to cancel. It will loop until everything is downloaded or you interrupt (with `Ctrl+C`).

A `manifest.json` is written into `--path` recording every successful download (sha256, size, version, action).

### Known issues

- **HTTP 503 mid-download**: Synty's CDN occasionally rate-limits. First, just wait a few minutes and let the limit reset. Then use the retry prompt at the end of the run, or rerun with `--workers 2` to reduce concurrency.
- **`[WinError 32]` on rename**: Windows antivirus briefly locks the freshly downloaded `.part` file. The retry prompt at the end of the run almost always clears it.
- **`Could not detect customer id`**: cookies.txt is missing required entries or expired. Repeat the Cookies setup step.
