"""Deadline reminders for JAFs you are eligible for but have not signed yet.

Two kinds of alerts:
  * a daily digest (once per day, after DIGEST_HOUR IST) of JAFs closing soon
  * a last-call warning when a JAF closes within WARN_HOURS

Pure logic (stdlib only); scraper.py does the sending and stores the state.
"""
from datetime import datetime, timezone, timedelta

IST_TZ = timezone(timedelta(hours=5, minutes=30))


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def actionable_jobs(jobs, now):
    """Eligible, unsigned jobs whose deadline is still in the future, soonest first.
    Returns [(job, closes_at_datetime)]."""
    out = []
    for j in jobs:
        if j.get("eligible") != "Eligible" or j.get("signed") == "Yes":
            continue
        closes = parse_iso(j.get("closes_at"))
        if closes and closes > now:
            out.append((j, closes))
    return sorted(out, key=lambda pair: pair[1])


def humanize_delta(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def compute_deadline_alerts(jobs, state, now, warn_hours=3, digest_hour=9, digest_window_hours=48):
    """Decides which deadline alerts to send.

    Args:
        jobs: processed job dicts (need eligible, signed, closes_at, id, company, role, ctc)
        state: dict persisted between runs ({"last_digest": "YYYY-MM-DD", "warned": {id: closes_at}})
        now: timezone-aware datetime

    Returns:
        (digest_items, warn_items, new_state) where each item is (job, closes_at, time_left).
        digest_items is None when no digest is due this run.
    """
    state = {"last_digest": state.get("last_digest"), "warned": dict(state.get("warned") or {})}
    jobs_now = actionable_jobs(jobs, now)
    now_ist = now.astimezone(IST_TZ)
    today = now_ist.strftime("%Y-%m-%d")

    digest_items = None
    if now_ist.hour >= digest_hour and state["last_digest"] != today:
        horizon = now + timedelta(hours=digest_window_hours)
        digest_items = [(j, c, c - now) for j, c in jobs_now if c <= horizon]
        state["last_digest"] = today

    warn_items = []
    warn_horizon = now + timedelta(hours=warn_hours)
    for j, closes in jobs_now:
        key = str(j.get("id"))
        # Re-warn if the deadline was moved (warned value no longer matches).
        if closes <= warn_horizon and state["warned"].get(key) != j.get("closes_at"):
            warn_items.append((j, closes, closes - now))
            state["warned"][key] = j.get("closes_at")

    # Forget jobs that are signed, closed or gone so the state file stays small.
    live_ids = {str(j.get("id")) for j, _ in jobs_now}
    state["warned"] = {k: v for k, v in state["warned"].items() if k in live_ids}
    return digest_items, warn_items, state


def format_items(items, limit=15):
    lines = []
    for j, closes, left in items[:limit]:
        when = closes.astimezone(IST_TZ).strftime("%d %b, %I:%M %p")
        lines.append(f"• **{j.get('company')}** - {j.get('role')} | CTC {j.get('ctc', 'N/A')}\n"
                     f"   closes {when} IST (in {humanize_delta(left)})")
    if len(items) > limit:
        lines.append(f"...and {len(items) - limit} more.")
    return "\n".join(lines)
