import sys
from pathlib import Path

# Add squad_lead directory to path so 'from brain import ...' works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
