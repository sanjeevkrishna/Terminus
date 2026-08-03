"""Terminus — development launcher.

Runs the web server with all caching disabled: Jinja templates auto-reload,
static files are served with no-cache headers, and the Werkzeug reloader
restarts on code changes. Use this while iterating on HTML/CSS/JS.

For normal use, prefer web_launcher.py (browser) or desktop_launcher.py.

File path: dev_launcher.py
"""
import logging
import os
import webbrowser
from threading import Timer

from terminus.app import create_app, HOST, PORT

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app, socketio = create_app()

# --- Force everything fresh -------------------------------------------------
app.debug = True
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # don't cache static files


@app.after_request
def _no_cache(response):
    """Attach aggressive no-cache headers to every response."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    logger.info("Terminus DEV server — caching disabled, reloader on.")

    # Open the browser once (not again when the reloader forks its child).
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        Timer(1.0, _open_browser).start()

    socketio.run(app, host=HOST, port=PORT, use_reloader=True)