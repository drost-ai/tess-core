from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import queue
import re
import signal
import socket
import subprocess
import threading
import time
import uuid
import zlib
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .._utils import now_iso, pretty_json
from ..storage import getStateRoot
from ..tools import ToolDefinition


PROTOCOL_VERSION = "2025-06-18"
