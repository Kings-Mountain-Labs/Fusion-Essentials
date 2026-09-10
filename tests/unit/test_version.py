"""Version story: one version source (version.py), reported on the wire."""

import importlib
import re
import sys

from conftest import COMMANDS_DIR, load_mcp_server

def _version():
    if COMMANDS_DIR not in sys.path:
        sys.path.insert(0, COMMANDS_DIR)
    return importlib.import_module("mcpServer.version").__version__


def test_version_is_semver_shaped():
    assert re.match(r"^\d+\.\d+\.\d+$", _version())


def test_server_info_version_comes_from_version_module():
    mcp_server = load_mcp_server()
    srv = mcp_server.SimpleMCPServer()
    assert srv.server_info["version"] == _version()
