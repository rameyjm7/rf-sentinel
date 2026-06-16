from __future__ import annotations

import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_backend():
    app_path = Path(__file__).resolve().parents[1] / "ui" / "backend" / "app.py"
    backend_dir = str(app_path.parent)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    spec = spec_from_file_location("rf_sentinel_ui_backend", app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load UI backend: {app_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    module = _load_backend()
    app = module.app
    host = os.getenv("RF_SENTINEL_HOST", os.getenv("BT_EXPLORER_HOST", "0.0.0.0"))
    port = int(os.getenv("RF_SENTINEL_PORT", os.getenv("BT_EXPLORER_PORT", "5050")))
    start_console_dashboard = getattr(module, "start_console_dashboard", None)
    if callable(start_console_dashboard):
        start_console_dashboard(host, port)
    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\n[ui] Ctrl+C received, disconnecting from sdr-gateway...", file=sys.stderr)
    finally:
        shutdown = getattr(module, "shutdown", None)
        if callable(shutdown):
            shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
