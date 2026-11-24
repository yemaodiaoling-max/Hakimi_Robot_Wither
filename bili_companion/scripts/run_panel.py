"""Local panel entry point for a detached, windowless operator process."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
(root / 'state').mkdir(exist_ok=True)
sys.stdout = open(root / 'state/api-panel.stdout.log', 'a', encoding='utf-8', buffering=1)
sys.stderr = open(root / 'state/api-panel.stderr.log', 'a', encoding='utf-8', buffering=1)
from bili_companion.panel.server import main

if __name__ == '__main__':
    main()
