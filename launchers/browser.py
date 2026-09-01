"""Browser launcher — serves Terminus for use in a normal browser tab.

File path: launchers/browser.py
"""

import logging

from terminus.app import HOST, create_app

from . import common

logger = logging.getLogger(__name__)


def main(port=None, log_level=None, open_browser=True):
    common.configure_logging(log_level)

    resolved = common.resolve_port(port)
    # The origin allowlist and launch token derive from the port, so the app
    # cannot be built before it is known.
    app, socketio = create_app(resolved)

    if open_browser and not common.under_reloader():
        common.open_browser_when_ready(resolved)

    logger.info("Terminus (browser) on http://%s:%s", HOST, resolved)

    try:
        # allow_unsafe_werkzeug: Flask-SocketIO 5.x refuses the Werkzeug server
        # without it unless app.debug is set. Local, single-user, 127.0.0.1.
        socketio.run(
            app,
            host=HOST,
            port=resolved,
            use_reloader=True,
            allow_unsafe_werkzeug=True,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted.")
    return 0
