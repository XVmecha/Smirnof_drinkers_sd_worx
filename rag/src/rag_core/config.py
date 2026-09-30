from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    docs_dir: Path
    index_path: Path
    max_tool_calls: int = 8

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            docs_dir=Path(os.environ.get("DOCS_DIR", "./docs")).resolve(),
            index_path=Path(os.environ.get("INDEX_PATH", "./data/index.json")).resolve(),
            max_tool_calls=int(os.environ.get("MAX_TOOL_CALLS", "8")),
        )
