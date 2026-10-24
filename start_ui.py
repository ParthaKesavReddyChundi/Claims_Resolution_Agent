"""One-click runner for the Claims Resolution Agent Web UI & API.

Usage:
    python start_ui.py
"""
import os
import sys
import webbrowser
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "api"))
sys.path.insert(0, os.path.join(_HERE, "server"))
sys.path.insert(0, os.path.join(_HERE, "policy"))
sys.path.insert(0, os.path.join(_HERE, "ledger"))

os.environ.setdefault("CRA_JWT_SIGNING_KEY", "cra-web-production-secret-2026")


def open_browser():
    time.sleep(1.2)
    print("Opening browser at http://127.0.0.1:8000 ...")
    webbrowser.open("http://127.0.0.1:8000")


def main():
    print("=" * 65)
    print("      THE CLAIMS RESOLUTION AGENT — WEB DASHBOARD")
    print("=" * 65)
    print("Server starting at: http://127.0.0.1:8000")
    print("API Documentation:  http://127.0.0.1:8000/docs")
    print("Press Ctrl+C to stop the server.\n")

    # Auto-build frontend if dist does not exist
    dist_dir = os.path.join(_HERE, "frontend", "dist")
    if not os.path.exists(dist_dir):
        print("Frontend build not found. Building static assets...")
        import subprocess
        frontend_dir = os.path.join(_HERE, "frontend")
        try:
            subprocess.run(["npm", "run", "build"], cwd=frontend_dir, check=True, shell=True)
        except Exception as e:
            print(f"Note: Automatic build skipped ({e}). Run: cd frontend && npm install && npm run build")

    # Launch browser in a background timer
    threading.Thread(target=open_browser, daemon=True).start()

    import uvicorn
    from main import app
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
