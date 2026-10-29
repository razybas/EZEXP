# EzExp v1.2.0 Release Notes

EzExp v1.2.0 delivers a major architectural reorganization, splitting the monolithic `app.py` into a modular package, and migrates the agent chat & workspace experience into an IDE-style dock in the MainWindow.

## Highlights & Features

### 1. Pure-Move Package Split (`ezexp/`)
- Decomposed the 6,434-line single-file `app.py` into a clean, modular `ezexp/` package:
  - `ezexp/__init__.py`: Package metadata, constants (`APP_NAME`, `APP_VERSION`, `APP_DIR`, `DB_DIR`, `DB_PATH`).
  - `ezexp/settings_store.py`: Settings getters and setters (vaults, quicklaunch, launch counts).
  - `ezexp/agent_config.py`: Agent configuration and status probe helpers.
  - `ezexp/fsutil.py`: File system utilities, path helpers, category mapping, file size formatting.
  - `ezexp/shell_win.py`: Windows shell operations (recycle, file operations, native shell thumbnails).
  - `ezexp/db.py`: Database connection, schema creation, FTS management, query helpers.
  - `ezexp/search_engine.py`: Search sorting and execution (`sort_rows`, `do_search`).
  - `ezexp/indexer.py`: Full and incremental scanners, background indexing threads (`IndexerThread`, `SearchWorker`).
  - `ezexp/vault_graph.py`: Wikilink extraction and vault structure/graph builders.
  - `ezexp/widgets.py`: Reusable UI widgets (`FramelessWindow`, `TitleBar`, `SearchField`, `NameDelegate`, `ResultsTree`).
  - `ezexp/graph_view.py`: Obsidian-style vault visualizer (`GraphView`, `GraphNode`, `QLaunchButton`).
  - `ezexp/main_window.py`: Main explorer window, breadcrumbs, search results, and agent dock.
  - `ezexp/agent.py`: Agent harness bridge (`ConfirmBridge`, `AgentClient`, `BridgeWorker`, `ProbeWorker`, `AgentSettingsDialog`).
  - `ezexp/spotlight.py`: Quick-launcher search overlay and delegate (`Spotlight`, `SpotDelegate`).
  - `ezexp/hotkey.py`: Global Windows hotkey registration and event filtering (`HotkeyFilter`).
- **AST Identity**: 100% AST identity across all 97 moved functions and classes against v1.1.0 with zero logic changes.
- **Backward-Compatible Shim**: `app.py` serves as a lightweight entry shim and dynamic proxy (`__AppModule`), maintaining an identical 224-symbol public API inventory and full monkey-patching compatibility.

### 2. MainWindow IDE-Style Agent Dock
- Moved the Agent workspace and chat from the Spotlight quick launcher into a dedicated vertical dock on the extreme right of `MainWindow`.
- Collapsible side pane (width 340 px, background `#fafafc`, left border `#ececf0`), styled cleanly after modern IDE sidecars.
- Controlled via an **Agent** toggle button in the MainWindow title bar; dock visibility state is persisted across sessions in `QSettings("ezexp", "EzExp")`.
- Contains live session transcript, message input box, and collapsible workspace section.

### 3. Real-Time Context Sync
- The agent workspace header features a dynamic `Context:` indicator that updates in real-time as the user navigates through folders in the explorer.
- When viewing the global index or running broad searches, Context cleanly displays `(none)`.
- When dispatching a task to the agent harness, `MainWindow` attaches `context.current_folder` to the task payload so the SLM is aware of the exact active directory without guessing or hallucinations.

### 4. Grant Workspace Viewer & Safe Preview
- Displays all files and directories in the granted workspace with Name, Size, and Modified date.
- Safe read-only file preview capped at 200 KB; non-text or binary files display a safe `"binary or too large to preview"` notice without freezing or emitting raw binary.
- Manual *Refresh* button to re-scan grant workspace files on demand.

### 5. Spotlight Reversion to Simple Chat Overlay
- Reverted the Spotlight quick-launcher Agent view back to a focused, full-width single-panel chat overlay.
- Retains clean memory and honest confirmation capabilities while offloading complex workspace inspection to the MainWindow dock.

---

## Verification & Test Summary
- `EZEXP` test suite (`py _test.py`): All tests passed cleanly (including scanner, FTS, graph, dock toggle, live context sync, preview boundaries, and bridge workers).
- Verification confirmed zero secret keys, user home paths, or private hostnames leaked in the repository.
