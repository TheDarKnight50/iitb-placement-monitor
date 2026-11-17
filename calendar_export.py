"""Writes tests and JAF deadlines to an iCalendar (.ics) file that can be imported into
Google Calendar / Apple Calendar / Outlook so reminders reach your phone too.

Pure logic (stdlib only).
"""
import hashlib
from datetime import datetime, timezone, timedelta


def _escape(text):
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line):
    """RFC 5545: lines longer than 75 octets are folded with CRLF + space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += b
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(events, now=None):
    """events: list of dicts with uid, start (aware datetime), summary, and optional
    end, description, url, alarm_minutes (list of ints)."""
    now = now or datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//iitb-placement-monitor//EN",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:IITB Placements",
    ]
    for ev in events:
        start = ev["start"]
        end = ev.get("end") or start + timedelta(minutes=30)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_escape(ev['uid'])}",
            f"DTSTAMP:{_utc(now)}",
            f"DTSTART:{_utc(start)}",
            f"DTEND:{_utc(end)}",
            f"SUMMARY:{_escape(ev['summary'])}",
        ]
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{_escape(ev['description'])}")
        if ev.get("url"):
            lines.append(f"URL:{ev['url']}")
        for minutes in ev.get("alarm_minutes", []):
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape(ev['summary'])}",
                f"TRIGGER:-PT{int(minutes)}M",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def placement_events(jobs, tests, signed_test_filter=None):
    """Builds calendar events from processed jobs and stored tests.

    * Deadline events for eligible, unsigned JAFs (a 30-min block ending at the deadline).
    * Test events for tests with a parsed time; if signed_test_filter is given, only tests
      for which it returns True are included.
    """
    events = []
    for j in jobs:
        if j.get("eligible") != "Eligible" or j.get("signed") == "Yes" or not j.get("closes_at"):
            continue
        try:
            closes = datetime.fromisoformat(str(j["closes_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if closes.tzinfo is None:
            closes = closes.replace(tzinfo=timezone.utc)
        events.append({
            "uid": f"jaf-deadline-{j.get('id')}@iitb-placement-monitor",
            "start": closes - timedelta(minutes=30),
            "end": closes,
            "summary": f"JAF closes: {j.get('company')} - {j.get('role')}",
            "description": f"CTC: {j.get('ctc', 'N/A')}\nSign before the deadline on the placement portal.",
            "url": "https://campus.placements.iitb.ac.in/applicant/home",
            "alarm_minutes": [180, 60],
        })
    for t in tests:
        if not t.get("test_time"):
            continue
        if signed_test_filter and not signed_test_filter(t):
            continue
        try:
            start = datetime.fromisoformat(t["test_time"])
        except ValueError:
            continue
        events.append({
            # hashlib (not hash()) so the UID is stable across runs and re-imports update in place
            "uid": f"test-{hashlib.sha1(str(t.get('link') or t['title']).encode()).hexdigest()[:16]}@iitb-placement-monitor",
            "start": start,
            "end": start + timedelta(hours=1),
            "summary": f"Test: {t.get('company') or t['title'][:60]}",
            "description": t.get("title", ""),
            "url": t.get("link"),
            "alarm_minutes": [60, 30],
        })
    return events
