"""PyInstaller entry point for the Windows meeting-room build."""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _configure_user_data_paths() -> None:
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    data_dir = local_app_data / "GoodListener" / "data"
    audio_dir = data_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("GOOD_LISTENER_DB_PATH", str(data_dir / "good-listener.db"))
    os.environ.setdefault("GOOD_LISTENER_AUDIO_DIR", str(audio_dir))
    os.environ.setdefault("GOOD_LISTENER_KEY_PATH", str(data_dir / "master.key.dpapi"))


def _open_browser_when_ready(url: str) -> None:
    if os.environ.get("GOOD_LISTENER_NO_BROWSER") == "1":
        return
    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=0.5) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.25)


def _argument_value(name: str, default: str) -> str:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default


def main() -> None:
    _configure_user_data_paths()
    host = _argument_value("--host", "127.0.0.1")
    port = _argument_value("--port", "8765")
    url = f"http://{host}:{port}"
    threading.Thread(
        target=_open_browser_when_ready,
        args=(url,),
        name="good-listener-browser",
        daemon=True,
    ).start()

    from panel.realtime_app import main as run_server

    run_server()


if __name__ == "__main__":
    main()
