# EzExp v1.3.0

Everything since v1.2.0, in one build.

**Agent window**
- The agent can now read files and run Python inside the folder you grant
  it (previously it could only create files), with a per-action confirm
  prompt — it can inventory a folder, write a tool for it, run it, and
  show you the real output.
- New **Harness mode** setting (single / pipeline / fanout) for the
  optional local-harness backend.
- Agent Endpoint Settings: bring your own OpenAI-compatible API
  (cloud or local Ollama). No key is bundled; nothing is preconfigured.

**Indexing**
- Live USN journal indexing on NTFS drives (changes appear without
  rescans), with the classic scan kept as a safety net.
- Honest scan status: the status line now says whether a scan is a full
  scan or a quick delta, and an interrupted full scan resumes as a delta
  instead of restarting from zero on every launch.
- The Google Drive cache folder is no longer walked (it duplicated the
  Drive volume and could stall scans for minutes).

**Graph**
- The vault link graph now renders immediately from the links already
  known and grows as analysis continues in the background, instead of
  waiting for the full analysis to finish. Link analysis also continues
  during quick delta scans now.

**Fixes**
- Details pane is collapsible and no longer squeezes the results list.
- Agent settings dialog renders in the app's light theme.
- Sidebar rows scroll instead of overlapping when the window is short.
