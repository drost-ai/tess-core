from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
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
    daemonStatePath,
    ensureBaseLayout,
    getDaemonDir,
    getStateRoot,
    loadDaemonEvents,
    loadDaemonState,
    readLastDaemonEventCursor,
    rotateDaemonEventsIfOversized,
    saveDaemonState,
    withRecordTransaction,
)
from ..tasks.supervisor import createTaskSupervisor
