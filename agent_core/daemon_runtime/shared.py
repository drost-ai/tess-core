from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from .._utils import now_iso
from ..storage import (
    appendDaemonEvent,
    clearDaemonState,
    daemonLogsDir,
    daemonPidPath,
    daemonSocketPath,
    ensureBaseLayout,
    getStateRoot,
    loadDaemonEvents,
    loadDaemonState,
    saveDaemonState,
)
from ..task_supervisor import createTaskSupervisor
