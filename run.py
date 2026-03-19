"""Temporary test runner — adds parent dir to sys.path so tennis_model is importable."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Change working directory to Downloads so config.json / data/ resolve correctly
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tennis_model.cli import main
main()
