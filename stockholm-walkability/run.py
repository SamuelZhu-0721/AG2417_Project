"""Start the database-backed API and frontend after running backend/data_prep.py.

Run `python run.py` from this directory, or pass this file's path from anywhere.
Ctrl+C stops both child processes. Preparation initializes and populates the
database separately, before either server is launched.
"""

from __future__ import annotations

from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

BASE_DIR = Path(__file__).resolve().parent


def _check_port(port):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"Port {port} is already in use. Stop that server before running this launcher.") from exc


def _wait_ready(url, process, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Server exited during startup. Check the messages above; run backend/data_prep.py first.")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Server did not become ready within {timeout} seconds.")


def _stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    processes = []
    try:
        _check_port(8000)
        _check_port(3000)
        backend = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=BASE_DIR / "backend",
        )
        processes.append(backend)
        _wait_ready("http://127.0.0.1:8000/health", backend)
        frontend = subprocess.Popen(
            [sys.executable, "-m", "http.server", "3000", "--bind", "127.0.0.1", "--directory", str(BASE_DIR / "frontend")],
        )
        processes.append(frontend)
        _wait_ready("http://127.0.0.1:3000/", frontend)
        print("\nFrontend: http://localhost:3000\nAPI:      http://localhost:8000\nAPI docs: http://localhost:8000/docs\n"
              "Press Ctrl+C to stop both servers.\n", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        raise RuntimeError("A server stopped unexpectedly; stopping the other server.")
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for process in reversed(processes):
            _stop(process)


def _handle_shutdown(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    # Convert normal shutdown signals into the same cleanup path as Ctrl+C.
    signal.signal(signal.SIGTERM, _handle_shutdown)
    raise SystemExit(main())
