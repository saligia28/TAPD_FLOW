from __future__ import annotations

from typing import Any, Dict, Optional, TYPE_CHECKING

from .rule_based import analyze as rule_analyze
from .llm import LLMAnalysisError, LLMNotConfigured, analyze_with_llm

if TYPE_CHECKING:  # pragma: no cover
    from core.config import Config


def run_analysis(
    description: str,
    *,
    cfg: Optional["Config"] = None,
    story: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    base = rule_analyze(description or "") or {}
    analysis = dict(base.get("analysis", {}))
    feature_points = list(base.get("feature_points", []))
    result: Dict[str, Any] = {
        "analysis": analysis,
        "feature_points": feature_points,
    }

    if not cfg or not getattr(cfg, "analysis_use_llm", False):
        return result

    title = ""
    sid = ""
    if story:
        title = str(story.get("name") or story.get("title") or "")
        sid = str(story.get("id") or story.get("story_id") or story.get("tapd_id") or "")

    try:
        llm_payload = analyze_with_llm(description or "", cfg, story_title=title, story_id=sid)
    except LLMNotConfigured:
        return result
    except LLMAnalysisError as exc:
        result["ai_error"] = str(exc)
        return result
    except Exception as exc:  # pragma: no cover - generic failure
        result["ai_error"] = str(exc)
        return result

    if not llm_payload:
        return result

    ai_insights = {}
    summary = llm_payload.get("summary")
    if summary:
        analysis.setdefault("AI摘要", summary)
        ai_insights["summary"] = summary
    key_features = _coerce_list(llm_payload.get("key_features"))
    if key_features:
        ai_insights["key_features"] = key_features
        feature_points[:] = _merge_unique(feature_points, key_features)
    risks = _coerce_list(llm_payload.get("risks"))
    if risks:
        analysis["AI风险提示"] = risks
        ai_insights["risks"] = risks
    acceptance = _coerce_list(llm_payload.get("acceptance"))
    if acceptance:
        analysis["AI验收关注"] = acceptance
        ai_insights["acceptance"] = acceptance
    test_points = _coerce_list(llm_payload.get("test_points"))
    if test_points:
        result["ai_test_points"] = test_points
        ai_insights["test_points"] = test_points

    if ai_insights:
        result["ai_insights"] = ai_insights

    result["analysis"] = analysis
    result["feature_points"] = feature_points
    return result


def _merge_unique(original: list[str], extra: list[str]) -> list[str]:
    seen = set()
    merged: list[str] = []
    for item in original + extra:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(key)
    return merged


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        parts = [seg.strip() for seg in value.replace("；", ";").split(";") if seg.strip()]
        if parts:
            return parts
    return []
