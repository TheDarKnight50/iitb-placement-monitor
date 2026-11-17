import unittest

from job_changes import diff_jobs, is_important, format_changes


def job(**kw):
    base = {"id": 1, "company": "Acme", "role": "SDE", "ctc": "₹20.00 LPA",
            "salary_key": "INR 1500000 2000000", "eligible": "Eligible",
            "closes_at": "2026-11-20T11:30:00Z", "tests": ["Online Test"]}
    base.update(kw)
    return base


class DiffJobsTest(unittest.TestCase):
    def test_no_changes(self):
        self.assertEqual(diff_jobs({"1": job()}, {"1": job()}), [])

    def test_deadline_extended(self):
        changes = diff_jobs({"1": job()}, {"1": job(closes_at="2026-11-22T11:30:00Z")})
        self.assertEqual(len(changes), 1)
        label, old, new = changes[0]["changes"][0]
        self.assertEqual(label, "Deadline")
        self.assertEqual(old, "20 Nov, 05:00 PM")
        self.assertEqual(new, "22 Nov, 05:00 PM")
        self.assertTrue(is_important(changes[0]))

    def test_became_eligible_is_important(self):
        changes = diff_jobs({"1": job(eligible="Not Eligible")}, {"1": job()})
        self.assertTrue(is_important(changes[0]))

    def test_exchange_rate_noise_ignored(self):
        # Displayed INR string changes but raw salary is the same -> no alert
        old = job(ctc="₹50.00 LPA (USD 60,000)", salary_key="USD 0 60000")
        new = job(ctc="₹50.40 LPA (USD 60,000)", salary_key="USD 0 60000")
        self.assertEqual(diff_jobs({"1": old}, {"1": new}), [])

    def test_ctc_revised(self):
        changes = diff_jobs({"1": job(base_pay="₹15.00 LPA")},
                            {"1": job(base_pay="₹15.00 LPA", ctc="₹25.00 LPA", salary_key="INR 1500000 2500000")})
        self.assertEqual(changes[0]["changes"],
                         [("Pay", "Base ₹15.00 LPA / CTC ₹20.00 LPA", "Base ₹15.00 LPA / CTC ₹25.00 LPA")])
        self.assertFalse(is_important(changes[0]))

    def test_test_order_does_not_matter(self):
        self.assertEqual(diff_jobs({"1": job(tests=["A", "B"])}, {"1": job(tests=["B", "A"])}), [])

    def test_missing_fields_in_old_snapshot_are_skipped(self):
        old = job()
        del old["closes_at"]
        del old["salary_key"]
        self.assertEqual(diff_jobs({"1": old}, {"1": job()}), [])

    def test_new_jobs_ignored(self):
        self.assertEqual(diff_jobs({}, {"1": job()}), [])

    def test_signed_change_reported_but_not_important(self):
        changes = diff_jobs({"1": job(signed="No")}, {"1": job(signed="Yes")})
        self.assertEqual(changes[0]["changes"], [("Signed", "No", "Yes")])
        self.assertFalse(is_important(changes[0]))

    def test_format(self):
        changes = diff_jobs({"1": job()}, {"1": job(eligible="Not Eligible")})
        self.assertIn("Eligibility: Eligible → Not Eligible", format_changes(changes))


if __name__ == "__main__":
    unittest.main()
