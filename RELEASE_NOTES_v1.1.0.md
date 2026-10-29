# EzExp v1.1.0 Release Notes

EzExp v1.1.0 introduces major enhancements to the AI Agent integration, seamlessly pairing EzExp with local and remote harnesses (including the `ezexp-strands` runner) with an IDE-style workspace experience, silent process execution, and live folder context awareness.

## Highlights & Features

### 1. Silent Background Harness Execution (No-Window Spawn)
- Fixed console window popping up on every agent turn on Windows.
- `BridgeWorker` now launches the harness Python runner process with `creationflags = CREATE_NO_WINDOW` (`0x08000000`).
- GUI remains responsive and visually clean without distracting background console flashes.

### 2. Live Current-Folder Context Awareness
- EzExp tracks the currently open/navigated directory in the main window (or `null` when viewing the global catalog/all files).
- The task payload sent from EzExp to the harness now includes `"context": {"current_folder": <abs path or null>}`.
- The paired harness prepends `[Context: the folder currently open in the EzExp window is <path>]` directly to the task prompt.
- The agent accurately answers questions like *"which folder am I in?"* without hallucinations or guessing.

### 3. IDE-Style Workspace Viewer Pane
- The Quick Search Agent view now features a horizontal splitter: conversation history on the left, workspace pane on the right.
- **Header details**: displays the active context folder and the grant workspace path.
- **Workspace file list**: displays all files and folders in the workspace with Name, Size, and Modified Timestamp.
- **Automatic & manual refresh**: the file list refreshes automatically whenever the agent finishes a turn, and can be refreshed manually via the *Refresh* button.
- **Safe text preview**: clicking any file in the list opens a read-only preview below the list, capped at 200 KB. Non-text or binary files safely display `"binary or too large to preview"` without crashing or emitting raw hex.
- **Collapsible toggle**: click the *Workspace* button in the mode row or the close button on the pane to collapse it; when collapsed, the conversation area expands to full width as before.
- **Strict safety boundary**: the pane is strictly a viewer; file editing and new agent tools are not permitted from the pane.

### 4. Strands Harness Pairing Notes
- Paired with `ezexp-strands` (remote `origin/main` at `2c20fbd`):
  - XML dialect tool call translation (`<tool_name attr="value">...</tool_name>`).
  - Tool name aliasing (`write_file` -> `create_file`).
  - Unparsed call audit logging in `.sessions/audit.log` and stderr visibility.
  - Deployed conditional system prompt for app visibility and whole-PC catalog search.

---

## Verification & Test Summary
- `EZEXP` test suite (`_test.py`): All tests passed (indexing, search, delta scanning, confirm bridge, harness probe, current folder context, and workspace pane GUI tests).
- `ezexp-strands` test suite (`pytest`): 28/28 tests passed.
