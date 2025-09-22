from __future__ import annotations
import re
from typing import Dict, List


def _split_points(text: str) -> List[str]:
    # naive split by newline / punctuation; keep non-empty trimmed lines
    raw = re.split(r"[\n\r]+|[;；]|[。]", text or "")
    pts = [s.strip(" -•*\t") for s in raw]
    return [s for s in pts if s]


def analyze(description: str) -> Dict[str, object]:
    # Very simple heuristic placeholders
    # Coerce None/empty to empty string for robust handling
    desc = description or ""
    lines = _split_points(desc)
    feature_points = []
    for s in lines:
        if len(s) < 300:
            feature_points.append(s)
    # crude extraction
    goals = [s for s in lines if re.search(r"目标|目的|期望|As .* I want|so that", s, re.I)]
    acceptance = [s for s in lines if re.search(r"验收|AC:|Acceptance|Criteria", s, re.I)]

    analysis = {
        "目标": goals[:5],
        "验收标准": acceptance[:10],
        "摘要": (desc[:200] + "...") if len(desc) > 200 else desc,
    }
    return {
        "analysis": analysis,
        "feature_points": feature_points[:20],
    }
