import sys
from pathlib import Path

# Make ``import evals`` work however pytest is invoked.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
