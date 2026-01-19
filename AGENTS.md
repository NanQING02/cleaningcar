# AGENTS.md

## Project Overview
This project is a vehicle detection and analysis system designed for the RK3588 platform. It combines a Python-based inference engine using `rknnlite` for NPU acceleration with a FastAPI web server for management and visualization.

## Environment & Build

### Prerequisites
- Platform: Linux (specifically targeted for Rockchip RK3588).
- System Dependencies: GStreamer, OpenCV (system-level), librga, librknnrt.

### Setup
The project uses a custom script to set up the environment, including a virtual environment with access to system site-packages (for OpenCV/GStreamer).

```bash
# Full setup and start
./install_runtime_venv.sh
```

### Virtual Environment
The virtual environment is located at `venv-gst`.
To activate it manually:
```bash
source venv-gst/bin/activate
```

## Running the System

### Start Web Server (Main Entry)
The web server manages the inference process.
```bash
# Uses the setup script to run in background
./install_runtime_venv.sh

# Or manually via python (after activating venv)
python -m web.server --config config.json
```

### Run Inference Standalone
You can run the detection script directly for debugging:
```bash
python run_zone_detect.py --config config.json --video /path/to/video.mp4
```

## Testing & Verification

**⚠️ Status: No Automated Tests**
There are currently no unit tests or integration tests found in the repository.

### Manual Verification
1.  **Logs**: Check `web_server_8000.log` and `logs/inference/*.log` for errors.
2.  **API**: Verify endpoints via `curl` or browser (default port 8000).
    *   Health check: `GET /` (returns HTML).
    *   Config: `GET /config`.
3.  **Output**: Check `events/` for generated CSV logs and `captures/` for images.

## Code Style Guidelines

### Python
- **Formatter**: No explicit formatter configured. Follow existing style (approx. PEP 8).
- **Indentation**: 4 spaces.
- **Naming**:
    - Variables/Functions: `snake_case`
    - Classes: `PascalCase`
    - Constants: `UPPER_CASE`
- **Type Hinting**: Used sparingly. Add type hints for new complex functions, but maintain consistency with untyped legacy code.
- **Imports**: Grouped standard library, third-party, then local.
- **Error Handling**:
    - Use `try...except Exception as exc:` patterns.
    - Log errors to stdout/stderr with context prefixes (e.g., `[server]`, `[infer]`).

### Frontend (Web)
- **Structure**: Static HTML/JS served by FastAPI (`web/templates/`, `web/static/`).
- **Framework**: Vue.js (via CDN/static file).
- **No Build Step**: Frontend files are served directly. Do not introduce npm build steps unless refactoring the entire frontend.

## Architecture Notes
- **Inference Manager**: `web/server.py` spawns and monitors `run_zone_detect.py` as a subprocess.
- **Config**: Centralized in `config.json` (managed by `config_manager.py`).
- **NPU Dependencies**: Code relies on `rknnlite` and `rga`. **Do not mock or remove these imports** without understanding the target hardware constraints. Use `try...except ImportError` guards if running off-device.

## Specific Rules
- **Do not introduce heavy dependencies** that might conflict with the embedded environment.
- **Respect hardware constraints**: Video decoding often uses GStreamer pipelines (`mppvideodec`) for hardware acceleration. Preserve these pipelines.
