import sys
from pathlib import Path

# Add governance directory to path so 'from main import ...' works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
