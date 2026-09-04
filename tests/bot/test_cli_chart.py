from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bot.cli import build_parser


def test_chart_flag():
    p = build_parser()
    ns = p.parse_args(["run", "--chart"])
    assert ns.chart is True
    ns2 = p.parse_args(["run"])
    assert ns2.chart is False
