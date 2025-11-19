from __future__ import annotations
from typing import Any, Dict, List, Tuple, Optional
import re
import html as ihtml
from typing import TYPE_CHECKING
from bs4 import BeautifulSoup, NavigableString, Tag  # type: ignore
from analyzer.image import analyze_images  # type: ignore
from integrations.tapd.story_utils import (
    extract_story_attachments,
    extract_story_comments,
    extract_story_tags,
    summarize_attachment,
    summarize_comment,
)

if TYPE_CHECKING:  # pragma: no cover
    from core.config import Config


def html_to_text(html: str) -> str:
    """Very small HTML→plain converter for TAPD descriptions.

    - Convert <br> / <p> to newlines; <li> to '- ' lines
    - Strip remaining tags; unescape entities
    - Collapse 3+ blank lines to max 2
    """
    if not html:
        return ""
    s = html
    # normalize line breaks for common tags
    s = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", s)
    s = re.sub(r"(?i)</\s*p\s*>", "\n\n", s)
    s = re.sub(r"(?i)<\s*p\s*>", "", s)
    # li/ul/ol
    s = re.sub(r"(?i)<\s*li\s*>\s*", "- ", s)
    s = re.sub(r"(?i)</\s*li\s*>", "\n", s)
    # strip remaining tags
    s = re.sub(r"<[^>]+>", "", s)
    # unescape HTML entities
    s = ihtml.unescape(s)
    # normalize whitespace
    s = re.sub(r"\r\n?|\r", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def build_blocks(
    description: str,
    analysis: Dict[str, Any],
    feature_points: List[str],
    *,
    story_id: Optional[str] = None,
    cache_dir: Optional[str] = None,
) -> list:
    """Build Notion block payloads. Skeleton structure, SDK-specific later."""
    blocks: list = []

    # Convert HTML-ish descriptions to plain text that Notion can render nicely
    desc = description or ""
    img_infos = []
    if desc and ("<" in desc and ">" in desc):
        # Try rich conversion to Notion blocks
        html_blocks, img_urls = html_to_blocks(desc, return_images=True)
        if html_blocks:
            blocks.append({
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "原始描述"}}]},
            })
            blocks.extend(html_blocks)
            # Analyze images (best-effort)
            if img_urls:
                img_infos = analyze_images(img_urls, story_id=story_id, cache_dir=cache_dir)
        else:
            # Fallback to plain text
            desc = html_to_text(desc)
    if desc and not blocks:
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": "原始描述"}}]},
        })
        blocks.append({
            "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": desc[:1900]}}]},
        })

    if analysis:
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": "内容分析"}}]},
        })
        for k, v in analysis.items():
            txt = f"{k}: {', '.join(v) if isinstance(v, list) else str(v)}"
            blocks.append({
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": txt[:1900]}}]},
            })

    if feature_points:
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": "功能点"}}]},
        })
        for p in feature_points:
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"text": {"content": p[:200]}}]},
            })

    if img_infos:
        blocks.append({
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": "图片分析"}}]},
        })
        for info in img_infos:
            summ = []
            desc = info.get("description")
            if desc:
                summ.append(desc)
            elif info.get("format") and (info.get("width") and info.get("height")):
                summ.append(f"{info['format']} {info['width']}x{info['height']}")
            if info.get("content_type") and not summ:
                summ.append(info["content_type"])
            if info.get("reason") and not info.get("ok"):
                summ.append(f"解析失败: {info['reason']}")
            text = info.get('url','')
            if summ:
                text = f"{text} — {'; '.join(summ)}"
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"text": {"content": text[:1800]}}]},
            })

    return blocks


def html_to_blocks(html: str, return_images: bool = False) -> List[Dict[str, Any]] | tuple[List[Dict[str, Any]], List[str]]:
    """Convert a subset of HTML to Notion blocks.

    Supported:
    - h1/h2/h3 -> heading_1/2/3
    - p/br -> paragraph
    - ul/li -> bulleted_list_item
    - ol/li -> numbered_list_item
    - blockquote -> quote
    - hr -> divider
    - pre/code -> code block
    - img -> image (external if http/https), otherwise a paragraph with url
    Inline: strong/b, em/i, code, a href
    """
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return []

    blocks: List[Dict[str, Any]] = []
    images: List[str] = []

    def mk_rich(nodes) -> List[Dict[str, Any]]:
        rich: List[Dict[str, Any]] = []
        def recur(n, ann=None):
            a = dict(ann or {})
            if isinstance(n, NavigableString):
                txt = str(n)
                if not txt:
                    return
                rich.append({
                    "text": {"content": txt},
                    "annotations": {
                        "bold": a.get("bold", False),
                        "italic": a.get("italic", False),
                        "code": a.get("code", False),
                        "underline": a.get("underline", False),
                        "strikethrough": a.get("strikethrough", False),
                        "color": a.get("color", "default"),
                    },
                })
                return
            if not isinstance(n, Tag):
                return
            t = n.name.lower()
            if t in ("br",):
                rich.append({"text": {"content": "\n"}, "annotations": {}})
                return
            if t in ("strong", "b"):
                a["bold"] = True
            elif t in ("em", "i"):
                a["italic"] = True
            elif t == "code":
                a["code"] = True
            if t == "a":
                href = n.get("href")
                text = n.get_text() or href or ""
                if href:
                    rich.append({
                        "text": {"content": text, "link": {"url": href}},
                        "annotations": {
                            "bold": a.get("bold", False),
                            "italic": a.get("italic", False),
                            "code": a.get("code", False),
                            "underline": True,
                            "strikethrough": a.get("strikethrough", False),
                            "color": a.get("color", "default"),
                        },
                    })
                    return
            for c in n.children:
                recur(c, a)
        for n in nodes:
            recur(n, {})
        # compress empty trailing newline-only
        if rich and all(k not in rich[-1] for k in ("text", "mention")):
            rich.pop()
        return rich or [{"text": {"content": ""}}]

    def paragraph_from(tag: Tag) -> Dict[str, Any]:
        return {"type": "paragraph", "paragraph": {"rich_text": mk_rich(tag.contents)[:100]}}

    def codeblock_from(tag: Tag) -> Dict[str, Any]:
        txt = tag.get_text("\n")
        return {"type": "code", "code": {"rich_text": [{"text": {"content": txt[:2000]}}], "language": "plain text"}}

    def heading_from(tag: Tag, level: int) -> Dict[str, Any]:
        key = f"heading_{min(max(level,1),3)}"
        return {"type": key, key: {"rich_text": mk_rich(tag.contents)[:100]}}

    def list_item_from(tag: Tag, numbered: bool) -> Dict[str, Any]:
        key = "numbered_list_item" if numbered else "bulleted_list_item"
        return {"type": key, key: {"rich_text": mk_rich(tag.contents)[:100]}}

    def image_from(tag: Tag) -> Dict[str, Any]:
        src = tag.get("src") or ""
        if src.startswith("http://") or src.startswith("https://"):
            images.append(src)
            return {"type": "image", "image": {"type": "external", "external": {"url": src}}}
        # non-http src, render as text fallback
        return {"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": f"图片: {src}"}}]}}

    root_nodes = list(soup.body.children) if soup.body else list(soup.children)
    for node in root_nodes:
        if isinstance(node, NavigableString):
            txt = str(node).strip()
            if txt:
                blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": txt}}]}})
            continue
        if not isinstance(node, Tag):
            continue
        name = node.name.lower()
        if name in ("h1", "h2", "h3"):
            blocks.append(heading_from(node, int(name[1])))
        elif name == "p":
            if node.get_text(strip=True):
                blocks.append(paragraph_from(node))
        elif name == "ul":
            for li in node.find_all("li", recursive=False):
                blocks.append(list_item_from(li, numbered=False))
        elif name == "ol":
            for li in node.find_all("li", recursive=False):
                blocks.append(list_item_from(li, numbered=True))
        elif name == "blockquote":
            txt = node.get_text("\n").strip()
            if txt:
                blocks.append({"type": "quote", "quote": {"rich_text": [{"text": {"content": txt[:2000]}}]}})
        elif name in ("pre",):
            blocks.append(codeblock_from(node))
        elif name == "img":
            blocks.append(image_from(node))
        elif name == "hr":
            blocks.append({"type": "divider", "divider": {}})
        else:
            # fallback: treat as paragraph
            if node.get_text(strip=True):
                blocks.append(paragraph_from(node))
    # cap total blocks to stay safe
    out_blocks = blocks[:100]
    if return_images:
        return out_blocks, images
    return out_blocks


def _str_has_html(s: str) -> bool:
    return bool(s and "<" in s and ">" in s and "</" in s)


def _blocks_from_text(value: str) -> List[Dict[str, Any]]:
    if not value:
        return []
    if _str_has_html(value):
        return html_to_blocks(value)  # type: ignore[return-value]
    # plain text -> paragraph, split by double newlines
    parts = [p.strip() for p in re.split(r"\n{2,}", value.replace("\r", "")) if p.strip()]
    blocks: List[Dict[str, Any]] = []
    for p in parts:
        blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": p[:1900]}}]}})
    return blocks


def build_page_blocks_from_story(
    story: Dict[str, Any],
    *,
    cfg: Optional["Config"] = None,
    include_analysis: bool = True,
) -> List[Dict[str, Any]]:
    """Aggregate a story's content and render as Notion blocks.

    - Prefer HTML-aware rendering for any field containing HTML
    - Sections order: 原始描述 -> 其它字段 -> 内容分析 -> 需求点 -> 图片分析
    - 内容分析/需求点来自清洗后的描述文本（去 HTML）
    """
    blocks: List[Dict[str, Any]] = []

    desc_raw = str(story.get("description") or "")
    img_infos = []
    if desc_raw:
        if _str_has_html(desc_raw):
            html_blocks, img_urls = html_to_blocks(desc_raw, return_images=True)  # type: ignore[misc]
            if html_blocks:
                blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "原始描述"}}]}})
                blocks.extend(html_blocks)
            if img_urls:
                img_infos = analyze_images(
                    img_urls,
                    story_id=_story_id_for_cache(story),
                    cache_dir=(cfg.image_cache_dir if cfg else None),
                )
        else:
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "原始描述"}}]}})
            blocks.extend(_blocks_from_text(desc_raw))

    # Other likely content fields
    FIELD_MAP: List[Tuple[str, List[str]]] = [
        ("验收标准", ["acceptance", "acceptance_criteria", "验收", "验收标准", "test_focus"]),
        ("实现步骤", ["step", "steps"]),
        ("流程", ["flows"]),
        ("功能说明", ["feature"]),
        ("备注", ["remark", "comments", "comment"]),
    ]
    added = set()
    for title, keys in FIELD_MAP:
        value = None
        for k in keys:
            if k in story and isinstance(story[k], str) and story[k].strip():
                value = story[k]
                break
        if value:
            added.add(title)
            sec_blocks = _blocks_from_text(value)
            if sec_blocks:
                blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": title}}]}})
                blocks.extend(sec_blocks)

    # Heuristic: include any long custom fields that look like HTML
    html_like_customs: List[Tuple[str, str]] = []
    for k, v in story.items():
        if not isinstance(v, str):
            continue
        if k in {"description", "name", "title"}:
            continue
        # skip fields already handled
        if any(k in ks for _, ks in FIELD_MAP):
            continue
        if _str_has_html(v) and len(v) > 16:
            html_like_customs.append((k, v))
    # render custom html-like fields under a generic section
    for k, v in html_like_customs[:4]:  # cap to avoid noise
        sec_blocks = _blocks_from_text(v)
        if sec_blocks:
            label = f"字段:{k}"
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": [{"text": {"content": label[:50]}}]}})
            blocks.extend(sec_blocks)

    tags = extract_story_tags(story)
    if tags:
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "标签"}}]}})
        for tag in tags[:30]:
            txt = str(tag)[:120]
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"text": {"content": txt}}]},
            })

    attachments = extract_story_attachments(story)
    if attachments:
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "附件"}}]}})
        for att in attachments[:20]:
            txt = summarize_attachment(att)[:1900]
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"text": {"content": txt}}]},
            })

    comments = extract_story_comments(story)
    if comments:
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "评论"}}]}})
        for cm in comments[:20]:
            txt = summarize_comment(cm)[:1900]
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"text": {"content": txt}}]},
            })

    # Analysis on cleaned description text
    analysis: Dict[str, Any] = {}
    feature_points: List[Any] = []
    ai_insights: Optional[Dict[str, Any]] = None
    ai_test_points: List[Any] = []
    ai_error: Optional[str] = None
    if include_analysis:
        from analyzer import run_analysis as _run_analysis  # local import to avoid cycle
        desc_for_nlp = html_to_text(desc_raw) if _str_has_html(desc_raw) else desc_raw
        res = _run_analysis(desc_for_nlp or "", cfg=cfg, story=story)
        analysis = res.get("analysis", {}) if isinstance(res, dict) else {}
        feature_points = res.get("feature_points", []) if isinstance(res, dict) else []
        ai_insights = res.get("ai_insights") if isinstance(res, dict) else None
        ai_test_points = res.get("ai_test_points") if isinstance(res, dict) else []
        ai_error = res.get("ai_error") if isinstance(res, dict) else None

    if analysis:
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "内容分析"}}]}})
        for k, v in analysis.items():
            txt = f"{k}: {', '.join(v) if isinstance(v, list) else str(v)}"
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": txt[:1900]}}]}})

    if feature_points:
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "需求点"}}]}})
        for p in feature_points:
            blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"text": {"content": (p or "")[:200]}}]}})

    if ai_insights:
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "AI 分析"}}]}})
        summary = ai_insights.get("summary") if isinstance(ai_insights, dict) else None
        if summary:
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": summary[:1900]}}]}})
        for key, label in (
            ("key_features", "核心要点"),
            ("risks", "潜在风险"),
            ("acceptance", "验收关注"),
        ):
            values = ai_insights.get(key) if isinstance(ai_insights, dict) else None
            if not values:
                continue
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": [{"text": {"content": label}}]}})
            for item in values[:15]:
                blocks.append({
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [{"text": {"content": str(item)[:200]}}]},
                })

    if ai_test_points:
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "AI 测试点"}}]}})
        for item in ai_test_points[:20]:
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"text": {"content": str(item)[:220]}}]},
            })

    if ai_error:
        blocks.append({
            "type": "callout",
            "callout": {
                "icon": {"emoji": "⚠️"},
                "rich_text": [{"text": {"content": f"AI 分析失败: {ai_error}"[:1900]}}],
            },
        })

    if img_infos:
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "图片分析"}}]}})
        for info in img_infos:
            summ = []
            desc = info.get("description")
            if desc:
                summ.append(desc)
            elif info.get("format") and (info.get("width") and info.get("height")):
                summ.append(f"{info['format']} {info['width']}x{info['height']}")
            if info.get("content_type") and not summ:
                summ.append(info["content_type"])
            if info.get("reason") and not info.get("ok"):
                summ.append(f"解析失败: {info['reason']}")
            text = info.get('url','')
            if summ:
                text = f"{text} — {'; '.join(summ)}"
            blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"text": {"content": text[:1800]}}]}})

    return blocks


def _story_id_for_cache(story: Dict[str, Any]) -> Optional[str]:
    sid = story.get("id") or story.get("story_id") or story.get("tapd_id")
    if sid is None:
        return None
    return str(sid)
