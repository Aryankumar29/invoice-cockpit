"""Write the Mermaid source of every compiled graph to docs/graphs/, so the diagrams in the README are generated
from the code, not drawn by hand.    python scripts/draw_graphs.py"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from cockpit.core import seed
    from cockpit.graph import extraction, investigation
    from cockpit.investigate import Tools
    from cockpit.mail import MailIntake
    from cockpit.pipeline import Pipeline
    from cockpit.store import Store

    class _Model(FakeListChatModel):
        def bind_tools(self, tools, **kw):
            return self

    out = ROOT / "docs" / "graphs"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = Store(d)
        graphs = {
            "invoice": Pipeline(store, stage_delay=0).graph,
            "extraction": extraction.graph,
            "investigation": investigation.build(_Model(responses=[""]), Tools(seed.context()), 6, 40000),
            "mail": MailIntake(store, lambda n, b: {}).graph,
        }
        for name, g in graphs.items():
            (out / f"{name}.mmd").write_text(g.get_graph().draw_mermaid(), encoding="utf-8")
            print(f"docs/graphs/{name}.mmd")


if __name__ == "__main__":
    main()
