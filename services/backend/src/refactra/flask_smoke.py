"""Pytest plugin for observing requests to staged Flask route handlers."""

from inspect import getsourcefile, unwrap
from pathlib import Path

from flask import request, request_started

from refactra.python_test_support import NO_ROUTE_REQUESTS


def pytest_addoption(parser):
    parser.addoption("--refactra-route-file", action="append", default=[])


def pytest_configure(config):
    paths = {str(Path("/workspace") / path) for path in config.getoption("--refactra-route-file")}
    config._refactra_route_reached = False

    def observe(app, **_):
        if request.endpoint is None or request.endpoint == "static":
            return
        view = app.view_functions.get(request.endpoint)
        try:
            file = getsourcefile(unwrap(view)) if view is not None else None
        except (TypeError, ValueError):
            return
        if file in paths:
            config._refactra_route_reached = True

    request_started.connect(observe, weak=False)
    config.add_cleanup(lambda: request_started.disconnect(observe))


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 0 and not session.config._refactra_route_reached:
        session.exitstatus = NO_ROUTE_REQUESTS
