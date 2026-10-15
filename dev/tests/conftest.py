import sys

from pathlib import Path


# The dev scripts are run as files, not installed; import them the way they import each other.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
