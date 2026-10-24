"""Shared limits and filesystem flags for the fixed remote programs."""

import os
import re


MAX_REQUEST = 150 * 1024 * 1024
MAX_FILE_TOTAL = 104857600
NOFOLLOW = os.O_NOFOLLOW
DIRECTORY = os.O_DIRECTORY
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
