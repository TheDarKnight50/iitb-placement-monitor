"""Detects meaningful changes to existing JAFs between two scraper runs.

Pure logic (stdlib only) so it can be unit-tested without network or Google APIs.
"""
from datetime import datetime, timezone, timedelta

IST_TZ = timezone(timedelta(hours=5, minutes=30))


def fmt_ist(iso_str):
    """'2026-11-20T11:30:00Z' -> '20 Nov, 05:00 PM' (IST). Falls back to the raw value."""
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST_TZ).strftime("%d %b, %I:%M %p")
    except ValueError:
        return str(iso_str)


def _norm_tests(value):
    return sorted({str(t).strip() for t in (value or []) if str(t).strip()})


# (field in job dict, human label, how to display old/new values)
TRACKED_FIELDS = [
    ("closes_at", "Deadline", lambda job: fmt_ist(job.get("closes_at"))),
    ("salary_key", "Pay", lambda job: f"Base {job.get('base_pay', 'N/A')} / CTC {job.get('ctc', 'N/A')}"),
    ("eligible", "Eligibility", lambda job: job.get("eligible", "N/A")),
    ("tests", "Test rounds", lambda job: ", ".join(_norm_tests(job.get("tests"))) or "none"),
    ("role", "Role", lambda job: job.get("role", "N/A")),
    ("signed", "Signed", lambda job: job.get("signed", "N/A")),
]


def _comparable(field, job):
    value = job.get(field)
    if field == "tests":
        return _norm_tests(value)
    return value


def diff_jobs(old_map, new_map):
    """Compares two {job_id: job} maps and returns a list of changed jobs:
        [{"id", "company", "role", "changes": [(label, old_display, new_display), ...]}]

    A field is only compared when it exists in BOTH snapshots, so upgrading from an
    older known_jobs.json (which lacks new fields like closes_at) does not trigger a
    flood of false alerts. New and removed jobs are ignored here (handled elsewhere).
    """
    changed = []
    for job_id, new in new_map.items():
        old = old_map.get(job_id)
        if not old:
            continue
        changes = []
        for field, label, display in TRACKED_FIELDS:
            if field not in old or field not in new:
                continue
            if _comparable(field, old) != _comparable(field, new):
                changes.append((label, display(old), display(new)))
        if changes:
            changed.append({
                "id": job_id,
                "company": new.get("company", "Unknown"),
                "role": new.get("role", ""),
                "eligible": new.get("eligible", ""),
                "changes": changes,
            })
    return changed


def is_important(change):
    """Changes worth a louder alert: becoming eligible, or a deadline/test change on a job
    you can still act on."""
    for label, old, new in change["changes"]:
        if label == "Eligibility" and new == "Eligible":
            return True
    return change.get("eligible") == "Eligible" and any(
        label in ("Deadline", "Test rounds") for label, _, _ in change["changes"])


def format_changes(changes, limit=8):
    """Builds a Discord/desktop message body for a list of diff_jobs() results."""
    lines = []
    for c in changes[:limit]:
        lines.append(f"• **{c['company']}** - {c['role']}")
        for label, old, new in c["changes"]:
            lines.append(f"   {label}: {old} → {new}")
    if len(changes) > limit:
        lines.append(f"...and {len(changes) - limit} more.")
    return "\n".join(lines)
