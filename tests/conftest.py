import sys
from pathlib import Path

# Make the repo root importable (report_parser, app, config).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
