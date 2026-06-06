"""
CitationClaw 本地开发启动脚本

    python start.py
    python start.py --port 8080
"""
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent))

from citationclaw.__main__ import main

if __name__ == "__main__":
    main()
