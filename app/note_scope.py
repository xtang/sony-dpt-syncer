"""Scope belongs to target handwriting, never to all questions visible on a page."""
from __future__ import annotations

import re
import unicodedata

SCOPE_VERSION = 1


def normalize_ref(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    value = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    labelled = re.fullmatch(r"(?:問題|问题|section|part)\s*(\d+)(?:\s*[:/・\-—]\s*|\s+|(?=第|問|q))(.+)", value)
    if labelled:
        value = labelled[1]+":"+labelled[2]
    # Preserve section qualifiers: 問題2:8 and 問題3:8 are different questions.
    parts = value.split(":")
    if len(parts) > 2:
        return ""
    number = re.fullmatch(r"(?:第|問|q(?:uestion)?\s*)?\s*(\d+)\s*(?:題|题|問)?", parts[-1])
    if not number:
        return ""
    suffix = str(int(number[1]))
    if len(parts) == 1:
        return suffix
    section = re.sub(r"\s+", "", parts[0])
    section = re.sub(r"^(?:問題|问题|section|part)", "", section)
    if not section or len(section) > 80:
        return ""
    return section+":"+suffix


def normalize_refs(value):
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(ref for item in value if (ref := normalize_ref(item))))[:32]



def overlapping_refs(left, right):
    # A missing section is ambiguous, not proof of distinct questions.
    return any(a == b or (a.split(":")[-1] == b.split(":")[-1] and (":" not in a or ":" not in b))
               for a in normalize_refs(left) for b in normalize_refs(right))



def resolve_review_ref(value, allowed):
    ref = normalize_ref(value)
    if ref in allowed:
        return ref
    # Recognition uses bare numbers only when numbering is unique on the page.
    # An answer may repeat a printed section label; retain the recognized identity.
    if ":" in ref and ref.split(":")[-1] in allowed:
        return ref.split(":")[-1]
    return ""


def recognition_scope(result):
    refs = normalize_refs(result.get("question_refs"))
    status = result.get("scope_status")
    if result.get("kind") == "answer":
        return (refs, "known") if status == "known" and refs else ([], "unknown")
    return [], "not_applicable" if status == "not_applicable" else "unknown"


def scoped_source(note):
    """Use an exact anchor neighbourhood; never substitute an unrelated page prefix."""
    source, anchor = note.get("source_text", ""), note.get("anchor", "").strip()
    if not anchor:
        return ""
    position = source.find(anchor)
    if position < 0:
        return ""  # Recognised anchor is supplied separately, not passed off as PDF text.
    return source[max(0, position-500):position+len(anchor)+800][:5000]


def scope_answer(answer, note):
    """Build exercise prose only from accepted reviews, including on alternate clients."""
    answer = dict(answer)
    refs = set(normalize_refs(note.get("question_refs"))) if note.get("scope_status") == "known" else set()
    raw = answer.get("exercise_reviews", [])
    raw = raw if isinstance(raw, list) else []
    reviews, seen = [], set()
    for review in raw:
        if not isinstance(review, dict):
            continue
        ref = resolve_review_ref(review.get("question_ref"), refs)
        if ref in refs and ref not in seen:
            reviews.append(dict(review, question_ref=ref))
            seen.add(ref)
    answer["exercise_reviews"] = reviews
    if note.get("kind") == "answer" or raw:
        verdicts = {"correct": "作答正确", "incorrect": "需要订正", "uncertain": "待核对"}
        answer["summary"] = "；".join(f"{r['question_ref']}：{verdicts.get(r.get('verdict'), '待核对')}" for r in reviews) or "当前作答范围尚未确认。"
        answer["explanation"] = "仅核对当前笔迹对应的题目，详见逐题解析。" if reviews else "请先确认目标笔迹对应的题目。"
        answer["examples"] = []
        answer["uncertainties"] = []
        answer["tags"] = []
        answer.pop("title", None)
    return answer
