"""Load db/seed/seed.py (outside the backend package) for the seed tests."""

import importlib.util
from pathlib import Path

SEED_PATH = Path(__file__).resolve().parents[2] / "db" / "seed" / "seed.py"


def load_seed_module():
    spec = importlib.util.spec_from_file_location("servicemesh_seed", SEED_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = load_seed_module()
