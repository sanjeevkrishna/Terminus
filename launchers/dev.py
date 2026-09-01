"""Development launcher — no caching, reloader on, debugger off.

Use while iterating on HTML/CSS/JS: templates auto-reload and static files are
served with no-cache headers, so a refresh always shows your edits.

File path: launchers/dev.py
"""

import logging

from terminus.app import HOST, create_app

from . import common

logger = logging.getLogger(__name__)


def main(port=None, log_level=None, open_browser=True):
    common.configure_logging(log_level or "DEBUG")

    resolved = common.resolve_port(port)
    app, socketio = create_app(resolved)

    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    @app.after_request
    def _no_cache(response):
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    if open_browser and not common.under_reloader():
        common.open_browser_when_ready(resolved)

    logger.info(
        "Terminus DEV on http://%s:%s — caching off, reloader on.",
        HOST,
        resolved,
    )

    try:
        # use_debugger=False deliberately: the interactive debugger is a
        # remote-code-execution surface, and this server is reachable from any
        # page in your browser.
        socketio.run(
            app,
            host=HOST,
            port=resolved,
            use_reloader=True,
            use_debugger=False,
            allow_unsafe_werkzeug=True,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted.")
    return 0
