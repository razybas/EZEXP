# EzExp

A Mac-style (white & grey, Finder/Spotlight-inspired) universal file finder
for Windows. EzExp catalogs **every file on every fixed drive** into a fast
local index, so you can search anything and jump straight to it — no more
digging through folders.

The exe is fully portable: no installer, no admin rights, nothing outside
its own data folder. Works the same on any PC it's run on.

## Using it

- **Open `ezexp.exe`** — the main Finder-style window appears.
  - **First launch**: it indexes your entire PC in the background (watch the
    status bar). Search works immediately, even while that first scan runs.
  - **Every later launch**: a quick incremental scan picks up anything new,
    changed, or deleted since last time. It also quietly re-checks every
    10 minutes.
- **Search bar** (top right): type anything — results appear as you type,
  best matches first. Combine it with:
  - **Sidebar** — All Files, your real Desktop/Documents/Downloads/Pictures/
    Music/Videos (even if OneDrive-redirected), your Obsidian vaults, and
    every fixed drive.
  - **Filter chips** — Folders, Documents, Images, Videos, Music, Archives, Code.
- **Browsing stays inside EzExp**: double-click (or Enter on) a folder and
  its contents open right there — folders first, then files, A→Z. The
  **‹ button** in the title bar walks back through where you've been.
  Double-clicking a file shows it in the details pane instead of sending
  you to Explorer.
- **Previews** (details pane, right side): images render in full, videos
  show a real thumbnail frame (with a play badge — click the preview or hit
  Open to play it), and text/notes/code files show their beginning — handy
  for skimming Obsidian `.md` notes. Folders show their item count.
- **Open with the real Explorer whenever you want**: the **Show in Folder**
  button (or right-click → Show in Explorer) reveals any result in Explorer;
  **Open** launches it with its default app; `Ctrl+Enter` in the list does
  the same.
- **Quick Launch**: pin your favourite applications or scripts (`.exe`, `.lnk`,
  `.bat`, `.cmd`) with **＋ Add App…** in the sidebar, or let EzExp surface your
  most-frequently launched programs automatically. Single-click inspects the app
  in the details panel (with its folder scoped for graph browsing); double-click
  launches it. Right-click any app button to browse its folder, reveal in Explorer,
  copy path, or unpin.
- **Obsidian vault**: click **＋ Add Obsidian Vault…** under Favourites and
  give it your vault path (e.g. `G:/mydrive/obsidian`). It becomes a pinned
  favourite — browse every folder in it and search across it like any other
  place. It's remembered between launches and scanned automatically on each
  start (works even if the vault lives on a removable drive). Right-click a
  vault in the sidebar to remove it.
- **Graph tab**: the **Graph** chip shows the graph of whatever view you're
  in — the scope always follows the current tab, and everything uses
  **progressive disclosure**: only the big nodes are shown at first, and
  clicking a node expands just that node (a purple "+" badge marks what can
  be expanded; right-click a branch to collapse it again).
  - **All Files** → "This PC" and your drives; press **C:** to unfold C:'s
    folders, press any folder to unfold deeper — down to individual files
    (press a folder in Pictures and every picture appears as a node).
  - **A drive or any folder** → that place's own tree, same expand-by-click
    behaviour.
  - **A vault selected in the sidebar** → the Obsidian-style link graph with
    the same progressive disclosure: the vault and its top-level folders
    appear as bubbles sized by note count, and clicking a bubble bursts it
    into its sub-folders and notes. Resolved `[[wikilinks]]` are drawn
    between visible notes; while a folder is collapsed, its notes' links
    attach to the bubble so no connection is ever lost. Link data is parsed
    once in the background (right after the index builds) and kept
    incrementally up to date afterwards.
  Everything is read straight from the index, so expanding is instant at any
  depth and the layout re-sorts itself into a clean radial tree (branches
  clockwise A→Z, ring radius by depth, size by item count) after every
  click. Link data comes from the one-time background analysis described
  below, so it never blocks the UI. Interactions: drag nodes, pan, scroll
  to zoom; click for details, double-click to open, right-click for
  Expand/Collapse/Open/Show in Explorer/Copy path.
- **Three-dots button** (details pane, bottom right) or **right-click** any
  result: Open, Show in Explorer, Rename, Copy as path, Copy file,
  Move to Recycle Bin, Properties.
- **Quick Search**: press **Alt+Space** anywhere in Windows to summon the
  Spotlight-style bar. Enter opens the file; folders jump into the main
  window; Ctrl+Enter reveals in Explorer; Esc closes. (Falls back to
  Ctrl+Alt+Space if Alt+Space is taken.)
- **Keep the index fresh**: "Rescan now" in the sidebar does a quick
  incremental pass; "Rebuild index" wipes the catalog and re-indexes
  everything from scratch.

## Where things live

- Index database: `%LOCALAPPDATA%\EzExp\index.db` (expect roughly 0.5–2 GB
  on a full PC — it stores one row per file).
- The app never moves or modifies your files; Rename/Recycle only happen
  when you explicitly ask for them.

## Good to know

- Content *edits* (a file's size changing without being added/removed) are
  picked up on the next rescan, not instantly — new/renamed/deleted files
  appear immediately.
- Files inside junction/symlink folders are not double-indexed (loop
  protection).
- System junk (`$Recycle.Bin`, `System Volume Information`, page/hibernation
  files) is skipped; everything else on fixed drives is indexed.

## Rebuilding the exe

Anyone can build EzExp on their own PC: install Python 3.10+ from
[python.org](https://python.org) (tick "Add Python to PATH" in the
installer), then double-click `build.bat` — it installs the two
dependencies (PySide6, PyInstaller) and produces `dist\ezexp.exe`. No other
setup needed; the first launch of that exe indexes *that* PC.

## Source layout

- `app.py` — the entire app: UI, index engine, search, actions.
- `_test.py` — smoke tests (`py _test.py`).
