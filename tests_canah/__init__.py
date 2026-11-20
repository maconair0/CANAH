"""Tests run without starting the Docker sandbox unless a test opens one itself."""
import os

os.environ.setdefault("CANAH_SANDBOX", "none")
