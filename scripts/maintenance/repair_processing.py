"""Run an explicitly scoped, private recording recovery manifest."""

import argparse
from pathlib import Path

from localplaud.processing_repair import run_repair_queue

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("manifest", type=Path)
parser.add_argument("--base-url", default="http://127.0.0.1:8080")
args = parser.parse_args()
run_repair_queue(args.manifest, base_url=args.base_url)
