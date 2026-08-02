"""Terminus — web launcher.

Runs the Flask + Socket.IO server for use in a normal browser. Intended
for local development or when you prefer a browser tab over the desktop
window. Serves on 127.0.0.1 only.

File path: web_launcher.py
"""
import logging
import webbrowser
from threading import Timer

from terminus.app import create_app, HOST, PORT

logging.basicConfig(level=logging.INFO)

app, socketio = create_app()


def _open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    # Open the browser shortly after the server starts (skip when the
    # Werkzeug reloader spawns its child process, to avoid a double tab).
    import os
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        Timer(1.0, _open_browser).start()

    socketio.run(app, host=HOST, port=PORT, use_reloader=True)