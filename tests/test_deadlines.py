import unittest
from datetime import datetime, timezone, timedelta

from deadlines import compute_deadline_alerts, humanize_delta, format_items

UTC = timezone.utc


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# 10:00 IST on 17 Nov 2026
NOW = datetime(2026, 11, 17, 4, 30, tzinfo=UTC)


def job(id, hours, eligible="Eligible", signed="No"):
    return {"id": id, "company": f"Co{id}", "role": "SDE", "ctc": "₹20.00 LPA",
            "eligible": eligible, "signed": signed, "closes_at": iso(NOW + timedelta(hours=hours))}


class DeadlineTest(unittest.TestCase):
    def test_digest_once_per_day(self):
        jobs = [job(1, 10), job(2, 30), job(3, 100)]
        digest, _, state = compute_deadline_alerts(jobs, {}, NOW)
        self.assertEqual([j["id"] for j, _, _ in digest], [1, 2])  # 48h window
        digest2, _, _ = compute_deadline_alerts(jobs, state, NOW + timedelta(hours=1))
        self.assertIsNone(digest2)
        digest3, _, _ = compute_deadline_alerts(jobs, state, NOW + timedelta(days=1))
        self.assertIsNotNone(digest3)

    def test_no_digest_before_digest_hour(self):
        early = datetime(2026, 11, 17, 1, 0, tzinfo=UTC)  # 06:30 IST
        digest, _, _ = compute_deadline_alerts([job(1, 5)], {}, early, digest_hour=9)
        self.assertIsNone(digest)

    def test_excludes_signed_ineligible_and_closed(self):
        jobs = [job(1, 2, signed="Yes"), job(2, 2, eligible="Not Eligible"), job(3, -1)]
        digest, warnings, _ = compute_deadline_alerts(jobs, {}, NOW)
        self.assertEqual(digest, [])
        self.assertEqual(warnings, [])

    def test_warning_once_and_rewarn_on_extension(self):
        jobs = [job(1, 2)]
        _, warnings, state = compute_deadline_alerts(jobs, {}, NOW)
        self.assertEqual(len(warnings), 1)
        _, warnings, state = compute_deadline_alerts(jobs, state, NOW + timedelta(minutes=15))
        self.assertEqual(warnings, [])
        # Deadline extended by 1 hour -> new closes_at -> warn again (still within 3h)
        extended = [job(1, 3)]
        _, warnings, _ = compute_deadline_alerts(extended, state, NOW + timedelta(minutes=30))
        self.assertEqual(len(warnings), 1)

    def test_state_pruned(self):
        state = {"warned": {"99": "x"}}
        _, _, new_state = compute_deadline_alerts([job(1, 50)], state, NOW)
        self.assertNotIn("99", new_state["warned"])

    def test_humanize(self):
        self.assertEqual(humanize_delta(timedelta(minutes=45)), "45 min")
        self.assertEqual(humanize_delta(timedelta(hours=2, minutes=5)), "2h 5m")
        self.assertEqual(humanize_delta(timedelta(days=1, hours=3)), "1d 3h")

    def test_format_items(self):
        digest, _, _ = compute_deadline_alerts([job(1, 10)], {}, NOW)
        text = format_items(digest)
        self.assertIn("Co1", text)
        self.assertIn("IST", text)


if __name__ == "__main__":
    unittest.main()
