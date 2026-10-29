import os
import sys
import json
import subprocess

from ezexp import APP_DIR

DEFAULT_AGENT_CONFIG = {
    "backend": "endpoint",
    "harness_path": "",
    "harness_python": "py",
    "base_url": "https://integrate.api.nvidia.com/v1",
    "model": "meta/llama-3.3-70b-instruct",
    "api_key": "",
    "grant_dir": "",
}

_harness_probe_cache = {}

def clear_harness_probe_cache():
    global _harness_probe_cache
    _harness_probe_cache.clear()

def harness_available(cfg=None):
    if cfg is None:
        cfg = load_agent_config()
    backend = str(cfg.get("backend") or "harness").strip().lower()
    hp = (cfg.get("harness_path") or "").strip()
    python_bin = (cfg.get("harness_python") or "py").strip() or "py"
    if not hp or not os.path.isdir(hp):
        return False, f"Directory does not exist: '{hp}'" if hp else "Path is empty"

    cache_key = (backend, hp, python_bin)
    if cache_key in _harness_probe_cache:
        return _harness_probe_cache[cache_key]

    try:
        proc = subprocess.run(
            [python_bin, "-c", "import harness"],
            cwd=hp,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
        if proc.returncode == 0:
            res = (True, "")
            _harness_probe_cache[cache_key] = res
            return res
        else:
            stderr_lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
            last_line = stderr_lines[-1] if stderr_lines else f"Exited with code {proc.returncode}"
            return (False, last_line)
    except subprocess.TimeoutExpired as e:
        return (False, f"Harness availability check timed out after 30s: {e}")
    except Exception as e:
        return (False, str(e))

def agent_config_path():
    return os.path.join(APP_DIR, "agent.json")

def load_agent_config():
    cfg = dict(DEFAULT_AGENT_CONFIG)
    p = agent_config_path()
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg

def save_agent_config(cfg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        full_cfg = dict(DEFAULT_AGENT_CONFIG)
        if isinstance(cfg, dict):
            full_cfg.update(cfg)
        with open(agent_config_path(), "w", encoding="utf-8") as fh:
            json.dump(full_cfg, fh, indent=2)
        clear_harness_probe_cache()
    except Exception:
        pass

def agent_status(cfg=None):
    if cfg is None:
        cfg = load_agent_config()
    backend = (cfg.get("backend") or "endpoint").lower()
    if backend == "harness":
        avail, err = harness_available(cfg)
        if not avail:
            return False, f"Organism Harness not available: {err}. Set the harness folder via 'Endpoint…' above."
        grant_dir = (cfg.get("grant_dir") or "").strip()
        if not grant_dir:
            return False, "Set a grant folder via 'Endpoint…' above — it becomes the harness workspace."
        return True, f"Organism Harness ready · workspace: {grant_dir}"
    else:
        api_key = (cfg.get("api_key") or "").strip()
        if not api_key:
            return False, "Setup required: No API key configured. Click 'Endpoint…' above to configure your OpenAI-compatible endpoint and API key."
        return True, ""
