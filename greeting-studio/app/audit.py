"""Consistency audit across unit templates.

Produces a units x core-items matrix with red/yellow/green statuses, plus
contradiction and stale-info findings.
"""
import re

# Hardcoded-value detectors per core item: if the template covers the item but
# embeds a literal value where a merge field should be, flag yellow (stale risk).
STALE_PATTERNS = {
    "entry": re.compile(r"\b(code|keypad|lock(box)?)\b[^.\n]{0,40}?\b(\d{3,8})\b", re.IGNORECASE),
    "wifi": re.compile(r"\b(password|pass)\b[\s:]*[\"']?[A-Za-z0-9!@#$%^&*_-]{6,}", re.IGNORECASE),
}

TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)


def _find_merge_fields(content: str) -> set:
    return set(re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", content))


def check_item(content: str, item: dict) -> dict:
    """Return {'status': 'green'|'yellow'|'red', 'detail': str} for one core item."""
    lower = content.lower()
    fields = _find_merge_fields(content)
    has_field = any(f in fields for f in item.get("merge_fields", []))
    has_keyword = any(kw in lower for kw in item.get("keywords", []))

    if not has_field and not has_keyword:
        return {"status": "red", "detail": f"No {item['label'].lower()} found in template."}

    stale_re = STALE_PATTERNS.get(item["key"])
    if stale_re and stale_re.search(content) and not has_field:
        return {
            "status": "yellow",
            "detail": (f"{item['label']} appears hardcoded — use a merge field like "
                       f"{{{{{item['merge_fields'][0]}}}}} instead." if item.get("merge_fields")
                       else f"{item['label']} appears hardcoded."),
        }

    if item.get("merge_fields") and not has_field:
        return {
            "status": "yellow",
            "detail": (f"{item['label']} is mentioned but no merge field "
                       f"({', '.join('{{' + f + '}}' for f in item['merge_fields'])}) is used."),
        }
    return {"status": "green", "detail": ""}


def extract_times(content: str, near_keyword: str) -> list:
    """Find times mentioned in the same sentence as a keyword (e.g. check-in)."""
    times = []
    sentences = re.split(r"[.\n!?]+", content)
    for sentence in sentences:
        lower = sentence.lower()
        if near_keyword in lower or near_keyword.replace("-", " ") in lower:
            for m in TIME_RE.finditer(sentence):
                hour = int(m.group(1))
                minute = m.group(2) or "00"
                ampm = m.group(3).upper()
                times.append(f"{hour}:{minute} {ampm}")
    return times


def run_audit(units_with_templates: list, core_items: list) -> dict:
    """units_with_templates: [{'unit': {...}, 'template': str or None}]"""
    matrix = []
    contradictions = []
    checkin_times = {}
    checkout_times = {}

    for entry in units_with_templates:
        unit = entry["unit"]
        template = entry["template"]
        row = {"unit_id": unit["id"], "unit_name": unit["name"], "has_template": template is not None,
               "items": {}}
        if template is None:
            for item in core_items:
                row["items"][item["key"]] = {"status": "red", "detail": "No template yet."}
        else:
            for item in core_items:
                row["items"][item["key"]] = check_item(template, item)
            ci = extract_times(template, "check-in") + extract_times(template, "checkin")
            co = extract_times(template, "checkout") + extract_times(template, "check-out")
            if ci:
                checkin_times[unit["name"]] = sorted(set(ci))
            if co:
                checkout_times[unit["name"]] = sorted(set(co))
        matrix.append(row)

    def find_time_conflicts(times_by_unit: dict, label: str):
        distinct = {}
        for unit_name, times in times_by_unit.items():
            for t in times:
                distinct.setdefault(t, []).append(unit_name)
        if len(distinct) > 1:
            desc = "; ".join(f"{t} ({', '.join(sorted(set(names)))})" for t, names in sorted(distinct.items()))
            contradictions.append({
                "type": label,
                "detail": f"Templates state different {label} times: {desc}. "
                          f"If these should match across units, align them (or use a merge field).",
            })

    find_time_conflicts(checkin_times, "check-in")
    find_time_conflicts(checkout_times, "checkout")

    return {"matrix": matrix, "contradictions": contradictions}
