import unittest
from datetime import datetime, timezone

from calendar_export import build_ics, placement_events


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.jobs = [
            {"id": 7, "company": "Acme, Inc.", "role": "SDE; Backend", "ctc": "₹20.00 LPA",
             "eligible": "Eligible", "signed": "No", "closes_at": "2026-11-20T11:30:00Z"},
            {"id": 8, "company": "Signed Co", "role": "Analyst", "ctc": "N/A",
             "eligible": "Eligible", "signed": "Yes", "closes_at": "2026-11-20T11:30:00Z"},
        ]
        self.tests = [
            {"title": "Signed Co online test", "company": "Signed Co", "link": "https://insti.app/blog/1",
             "test_time": "2026-11-21T18:00:00+05:30"},
            {"title": "Other test", "company": "Other", "link": "https://insti.app/blog/2",
             "test_time": "2026-11-21T18:00:00+05:30"},
            {"title": "No time", "link": "https://insti.app/blog/3", "test_time": None},
        ]

    def test_events(self):
        events = placement_events(self.jobs, self.tests, lambda t: t["company"] == "Signed Co")
        summaries = [e["summary"] for e in events]
        self.assertEqual(summaries, ["JAF closes: Acme, Inc. - SDE; Backend", "Test: Signed Co"])

    def test_ics_valid_and_escaped(self):
        events = placement_events(self.jobs, self.tests)
        ics = build_ics(events, now=datetime(2026, 11, 17, tzinfo=timezone.utc))
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(ics.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(ics.count("BEGIN:VEVENT"), 3)
        self.assertIn(r"SUMMARY:JAF closes: Acme\, Inc. - SDE\; Backend", ics)
        self.assertIn("DTEND:20261120T113000Z", ics)
        self.assertIn("DTSTART:20261121T123000Z", ics)  # 18:00 IST
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)

    def test_uids_stable(self):
        a = [e["uid"] for e in placement_events(self.jobs, self.tests)]
        b = [e["uid"] for e in placement_events(self.jobs, self.tests)]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
