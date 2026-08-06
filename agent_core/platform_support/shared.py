from __future__ import annotations

import asyncio
import stat
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .._utils import now_iso
from ..storage import getProjectRoot, getStateRoot, loadAuth, loadDaemonState, loadMcpServers, loadPlugins
from ..runtime.workspace import captureWorkspaceSnapshot, summarizeWorkspaceCapabilities


REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_ENTRY_PATH = REPO_ROOT / "dist" / "index.js"
REPO_PACKAGE_JSON_PATH = REPO_ROOT / "package.json"
BUNDLED_PACKAGE_JSON_PATH = REPO_ROOT / "package" / "package.json"
DEFAULT_CHROME_NATIVE_HOST_NAME = "com.drost_ai.tess"
DEFAULT_CHROME_EXTENSION_ORIGIN = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
