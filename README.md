# EzExp

**A Mac-style universal file finder for Windows.** EzExp catalogs *every file
on every fixed drive* into a fast local index, so anything on your PC is one
search away — no more digging through folders. Finder-style browsing,
Spotlight-style quick search, real previews, Quick Launch for your apps,
Obsidian vault support with a link graph, and full file management (create,
rename, move, drag-and-drop) in a single portable exe.

- **Fully local.** No account, no cloud, no telemetry — the code contains no
  network calls at all. Your index never leaves your PC.
- **Portable.** One exe (~46 MB). No installer, no admin rights. It keeps its
  data in `%LOCALAPPDATA%\EzExp` and its settings in the registry, and works
  the same on any Windows 10/11 PC.
- **Fast at real scale.** Developed and verified against a live index of
  **2,179,340 files**: searches and folder opens come back in milliseconds
  because everything is served from the index, never by walking the disk.

## Getting EzExp

### Option A — build it yourself from this repo (recommended)

1. Click the green **Code** button → **Download ZIP** (or `git clone`).
2. Extract the ZIP anywhere and double-click **`build.bat`**.
   - If the PC has no Python, build.bat installs it automatically via
     **winget** (built into Windows 10/11), then installs the build
     dependencies (PySide6, PyInstaller, Pillow), generates the app icon,
     and builds. Total: a few minutes, fully automatic.
3. Run **`dist\ezexp.exe`**.
   - Windows SmartScreen may say *"Windows protected your PC"* on first
     run — the exe is not code-signed. Click **More info → Run anyway**
     (one time only).

### Option B — get the exe from someone who built it

Send/receive the single file `ezexp.exe` — nothing else is needed.
Same SmartScreen note as above.

## First launch & the index

- On first launch EzExp **indexes your entire PC in the background** —
  watch the spinner and counter in the status bar. A few minutes on an
  SSD; longer on a hard drive or a machine with millions of files.
- **Search and browsing already work while the first scan runs**, and
  closing the app mid-scan is safe: the scan's completion flag is only
  written at the very end, so the next launch simply resumes and
  finishes the job.
- Later launches take a quick **incremental (delta) scan** — seconds —
  and the app re-checks quietly **every 10 minutes** while it runs.
- **Rescan now** (sidebar) forces an incremental pass; **Rebuild index**
  wipes the catalog and re-indexes from scratch (with a confirmation).
- The index lives at `%LOCALAPPDATA%\EzExp\index.db` (SQLite, WAL mode).
  Expect a few hundred MB up to ~2 GB on a big PC — one row per file.
  Deleting that folder resets EzExp to a fresh first launch.

## Features

### Instant search
- Type in the search bar (top right) — results appear as you type,
  best matches first. Matching is powered by an **FTS5 trigram
  index**, so any substring of a name matches too (`eeti` or `notes`
  both find `meeting-notes.txt`), with prefix matches ranked first.
- Search combines freely with the sidebar place and the filter chips.
- Views show up to **600 results** per query; the status bar says so
  when a view is truncated ("refine to narrow").

### Browsing
- Double-click (or press Enter on) a folder to open it *inside* EzExp;
  the **‹** title-bar button walks back through your history.
- **Sidebar places**: All Files, your real Desktop / Documents /
  Downloads / Pictures / Music / Videos (resolved through the Windows
  shell, so OneDrive-redirected folders land correctly), every fixed
  drive, your Obsidian vaults, and Quick Launch.
- **Filter chips**: All, Folders, Documents, Images, Videos, Music,
  Archives, Code — plus the **Graph** chip (below).
- **Sort control**: Default, Name (A–Z), Newest first, Oldest first,
  Largest first, Smallest first. *All Files → All* defaults to
  most-recently-modified first; a folder defaults to folders-first,
  then A–Z.

### Details & previews
Selecting anything fills the details pane (right side):
- **Images** render in full; **videos** show a real thumbnail frame
  from the Windows shell (play badge — Open plays it); **text, code
  and Markdown** files show their first lines; **folders** show their
  item count; **apps** show name, kind, and location with
  *Show in Folder* / *Open* buttons.
- Real file icons throughout, taken from the Windows shell — including
  for shortcuts, where EzExp shows the **target app's own icon**
  (without the little shortcut-arrow overlay).

### Quick Launch
- Pin favourite applications or scripts (`.exe`, `.lnk`, `.bat`,
  `.cmd`) with **＋ Add App…**, and EzExp also surfaces your
  most-frequently launched programs automatically.
- **Single-press** an app to inspect it in the details pane *and* open
  its folder in the main panel — for shortcuts the **target's** folder
  is opened, not the shortcut's location. **Double-press** launches it.
- Right-click an app button: browse its folder, reveal in Explorer,
  copy path, or remove it from Quick Launch (removing works for
  auto-surfaced frequent apps too).

### File management
Right-click any result — or use the **⋯** button in the details pane:
- **New File… / New Folder…** — created in the selected folder (or the
  open folder when nothing is selected) and indexed immediately, so it
  shows up at once. Name collisions and invalid names are refused with
  a message.
- **Open**, **Show in Explorer**, **Rename…**, **Copy as path**,
  **Copy file**, **Properties**.
- **Cut / Copy / Paste** — in the menus and via **Ctrl+X / Ctrl+C /
  Ctrl+V**. The clipboard uses the standard Windows format, so a Cut
  in EzExp pastes as a move in Explorer and vice versa. Multi-select
  is supported.
- **Drag-and-drop** — drag rows onto a folder to move them, hold
  **Ctrl** to copy, and drag files *in* from Explorer to copy them
  into the open folder. Dropping onto a file targets its folder;
  dropping onto empty space targets the open folder.
- Moves and copies are **index-synced**: the catalog updates in the
  same operation, so a moved file is searchable at its new home
  instantly — no rescan needed. EzExp **never overwrites**: a name
  collision produces the Explorer-style `name (2)` copy, and dropping
  a folder into itself is refused.
- **Move to Recycle Bin** (with confirmation) for deletes — nothing is
  ever deleted permanently or silently.

### Obsidian vaults
- **＋ Add Obsidian Vault…** pins any folder as a vault favourite:
  browsed and searched like any other place, remembered between
  launches, and scanned on every start (even on removable drives).
- `[[wikilinks]]` are parsed in the background after indexing and kept
  incrementally up to date — the vault's link graph (below) is built
  from that persisted analysis, never by re-reading your notes on the
  spot.

### Graph view
The **Graph** chip graphs whatever you're looking at — the scope
follows the current tab:
- **All Files** → "This PC" and your drives; **a drive or folder** →
  that place's folder tree; **a vault** → the Obsidian-style link
  graph (folder bubbles sized by note count, resolved wikilinks drawn
  between visible notes; links from collapsed folders attach to the
  bubble so no connection is lost).
- **Progressive disclosure**: only the large nodes appear first;
  click a node to expand just that branch (a purple **+** badge marks
  expandable nodes), right-click to collapse. Everything is read from
  the index, so expanding is instant at any depth, and the view
  re-lays itself into a clean radial tree after every click, with a
  gentle idle float. Nodes carry real icons.
- Interactions: drag nodes, pan, scroll to zoom, click for details,
  double-click to open, right-click for Expand / Collapse / Open /
  Show in Explorer / Copy path.

### Spotlight quick search
- **Alt+Space** anywhere in Windows summons the quick-search bar
  (falls back to **Ctrl+Alt+Space** if Alt+Space is taken; if both are
  taken, the main window's status bar says so and everything else
  still works).
- **Enter** opens the top match, folders jump into the main window,
  **Ctrl+Enter** reveals in Explorer, **Esc** closes.

### Small print that matters
- **Single instance**: launching EzExp twice just tells you it's
  already running (and a lock left behind by a crashed process is
  detected and taken over automatically).
- **Crash log**: if anything ever goes wrong, the traceback is
  appended to `%LOCALAPPDATA%\EzExp\error.log` — send that file with
  a bug report.
- **Version**: 1.0.0 (stamped into the exe's file properties).

## How it works

EzExp is a **single-file PySide6 application** (`app.py`, ~4,900
lines) built into a portable exe with PyInstaller. Around that file:

- **Index engine (SQLite).** One row per file in a `files` table
  (parent, name, kind, size, mtime, drive, category), a full-text
  `fts` table (FTS5, trigram tokenizer, external-content over
  `files`), a `meta` table (scan phase, totals), and a `note_links`
  table for wikilink analysis. The database runs in **WAL mode** with
  a 30 s busy timeout on every connection; connections **retry on
  transient locks** instead of failing.
- **Instant folder opens.** Folder browsing filters by parent path
  case-insensitively, served by a dedicated
  `parent COLLATE NOCASE` index (built once, lazily, by the indexer
  thread so startup connections never queue behind it) — measured at
  **~0.8 ms** for a folder query against a 400k-row index.
- **Threads.** An **indexer thread** performs full and delta scans
  (committing per directory batch, so the catalog is never locked for
  long); a **search worker thread** answers queries off the UI thread;
  a **link analyzer** parses wikilinks in budgeted background passes.
- **Delta scans.** Each directory's stored mtime decides whether it
  needs a rescan; new, changed and deleted entries are reconciled —
  including the whole subtree when a folder disappears.
- **Settings.** Quick Launch pins, launch counts and vault favourites
  live in the registry via `QSettings` (`HKCU\Software\ezexp\EzExp`);
  everything else is the index folder above.

## Building from source

**Requirements:** Windows 10/11. That's it — `build.bat` bootstraps
the rest:

1. `build.bat` finds a working Python 3.10+ (launcher, per-user
   installs, or PATH — every candidate must answer `--version`, so
   the Microsoft Store stub can't fool it). If none exists, it
   installs Python via **winget**, then re-detects.
2. It pip-installs **PySide6, PyInstaller and Pillow**.
3. `make_icon.py` renders the app icon (`ezexp.ico`) from the app's
   own artwork at 16–256 px.
4. PyInstaller builds a one-file, windowed exe with the icon and the
   version info from `version_info.txt` →
   **`dist\ezexp.exe`** (~46 MB).

## Repository layout

| File | Purpose |
|---|---|
| `app.py` | The entire application: UI, index engine, search, graph, actions |
| `_test.py` | Self-contained test suite — run `py _test.py` |
| `build.bat` | One-click build (bootstraps Python via winget if missing) |
| `make_icon.py` | Generates `ezexp.ico` from the app's artwork |
| `version_info.txt` | Windows version resource stamped into the exe |
| `ezexp.spec` | PyInstaller spec (reference; `build.bat` drives PyInstaller by flags) |
| `README.md` | This file |

## Tests

`py _test.py` runs the whole suite and prints `ALL TESTS PASSED`:
index/scanner behaviour (including delta-scan edge cases like a
deleted folder's children and a folder replaced by a file), FTS
search ranking, wikilink parsing and the persisted link analysis,
structure/vault graph construction, Quick Launch behaviour (pin,
launch counts, remove, shortcut resolution), sorting, file creation,
cut/copy/paste and the move engine, the single-instance lock, the
crash log, and a rendered-pixel check that the selected row is
actually visible. The suite uses an isolated temporary database —
your real index is never touched.

## Troubleshooting

- **"Windows protected your PC"** — the exe is unsigned; **More info
  → Run anyway**, once. (Removing this permanently would require a
  paid code-signing certificate.)
- **First launch feels slow to fill up** — it's indexing; search
  works meanwhile. HDDs and multi-million-file PCs take longer.
- **"Index error: database is locked"** — fixed in the current build
  (connections retry and the indexer auto-retries). If you ever see
  it on an old build: close *every* EzExp window/process, relaunch.
  Deleting `%LOCALAPPDATA%\EzExp` forces a clean rebuild.
- **The exe's icon looks like a plain folder in Explorer** — Windows
  icon cache; rename the exe once or restart Explorer.
- **Alt+Space does nothing** — another app owns it; EzExp falls back
  to Ctrl+Alt+Space and notes it in the status bar.
- **Something crashed** — check `%LOCALAPPDATA%\EzExp\error.log`.

## Limitations (honest list)

- **Windows only** — EzExp is built on Windows shell APIs throughout.
- **Search matches file and folder *names*** (and paths via browse),
  not file contents.
- **Results are capped at 600 per view** — narrow with places, chips
  or a longer query.
- Content-only edits (same file, new size) are picked up on the next
  rescan, not instantly; junctions/symlinks are not double-indexed
  (loop protection), and system locations (`$Recycle.Bin`, `System
  Volume Information`, page/hibernation files) are skipped by design.

## Privacy & safety

- **No network code, no telemetry, no accounts.** Everything happens
  on your machine.
- EzExp **never moves, renames or deletes your files on its own** —
  every destructive action is one you explicitly chose, and deletes
  go to the Recycle Bin behind a confirmation.

## License

MIT — see [LICENSE](LICENSE).
