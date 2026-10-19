"""Тесты планирования сегментов и расчёта задержек."""

from __future__ import annotations

import unittest
import httpx

from app.downloader.segments import plan_segments, segments_complete, validate_segments
from app.net.backoff import compute_delay, parse_retry_after, RetryPolicy


class TestSegments(unittest.TestCase):
    def test_small_file_is_one_segment(self) -> None:
        segments = plan_segments(1024, segment_size_mb=1)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start, 0)
        self.assertEqual(segments[0].end, 1023)

    def test_big_file_is_split_without_gaps(self) -> None:
        total = 5 * 1024 * 1024
        segments = plan_segments(total, segment_size_mb=1)
        self.assertGreater(len(segments), 1)
        self.assertTrue(validate_segments(segments, total))
        self.assertEqual(sum(seg.total for seg in segments), total)

    def test_odd_size_covers_everything(self) -> None:
        total = 3 * 1024 * 1024 + 12345
        segments = plan_segments(total, segment_size_mb=1)
        self.assertTrue(validate_segments(segments, total))
        self.assertEqual(segments[-1].end, total - 1)

    def test_validate_detects_gap(self) -> None:
        from app.core.models import Segment

        broken = [Segment(0, 0, 99), Segment(1, 200, 299)]
        self.assertFalse(validate_segments(broken, 300))

    def test_complete(self) -> None:
        from app.core.models import Segment

        segments = [Segment(0, 0, 9, received=10)]
        self.assertTrue(segments_complete(segments))


class TestBackoff(unittest.TestCase):
    def test_delay_grows(self) -> None:
        policy = RetryPolicy(base_delay=1.0, jitter=0.0, max_delay=60)
        first = compute_delay(1, None, policy)
        second = compute_delay(2, None, policy)
        third = compute_delay(3, None, policy)
        self.assertAlmostEqual(first, 1.0)
        self.assertAlmostEqual(second, 2.0)
        self.assertAlmostEqual(third, 4.0)

    def test_retry_after_wins(self) -> None:
        policy = RetryPolicy(base_delay=1.0, jitter=0.0)
        self.assertAlmostEqual(compute_delay(1, 7.5, policy), 7.5)

    def test_delay_capped(self) -> None:
        policy = RetryPolicy(base_delay=1.0, jitter=0.0, max_delay=10)
        self.assertLessEqual(compute_delay(20, None, policy), 10)

    def test_parse_retry_after_seconds(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "12"})
        self.assertEqual(parse_retry_after(response), 12.0)

    def test_parse_retry_after_absent(self) -> None:
        response = httpx.Response(429)
        self.assertIsNone(parse_retry_after(response))


if __name__ == "__main__":
    unittest.main()
