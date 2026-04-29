from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from .._utils import now_iso, pretty_json
from ..storage import getStateRoot
from ..tools import ToolDefinition


PROTOCOL_VERSION = "2024-11-05"

