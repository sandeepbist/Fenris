"""The sidecar entry: `python -m fenris api --port N` starts the engine.

The desktop shell spawns this binary (PyInstaller-frozen) with
FENRIS_HOME pointed at the app's data directory; the same entry works
for bare `python -m fenris` in dev.
"""
from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "api":
        port = 8714
        if "--port" in sys.argv:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        import uvicorn

        uvicorn.run("fenris.api:app", host="127.0.0.1", port=port)
        return
    from fenris.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
