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

    def test_many_small_parts_for_speed(self) -> None:
        """60 МБ при частях по 4 МБ — 15 частей (качаются по N одновременно), а не одна:
        Яндекс режет скорость каждого соединения, раньше такой файл шёл в один поток."""
        total = 60 * 1024 * 1024
        segments = plan_segments(total, segment_size_mb=4)
        self.assertEqual(len(segments), 15)
        self.assertTrue(validate_segments(segments, total))

    def test_huge_file_parts_are_capped(self) -> None:
        total = 50 * 1024 ** 3
        segments = plan_segments(total, segment_size_mb=4)
        self.assertLessEqual(len(segments), 512)
        self.assertTrue(validate_segments(segments, total))

    def test_old_defaults_upgraded_but_user_choice_kept(self) -> None:
        from app.core.config import AppConfig, _upgrade_defaults

        old = AppConfig()
        old.max_parallel_segments, old.segment_min_mb, old.config_version = 4, 64, 0
        _upgrade_defaults(old)
        self.assertEqual((old.max_parallel_segments, old.segment_min_mb), (12, 4))
        mine = AppConfig()
        mine.max_parallel_segments, mine.segment_min_mb, mine.config_version = 6, 32, 0
        _upgrade_defaults(mine)
        self.assertEqual((mine.max_parallel_segments, mine.segment_min_mb), (6, 32))
        again = AppConfig()
        again.max_parallel_segments, again.config_version = 4, 2
        _upgrade_defaults(again)
        self.assertEqual(again.max_parallel_segments, 4, "после обновления выбор «4» — уже ваш")

    def test_download_requests_have_own_quota(self) -> None:
        from app.net.ratelimit import RateLimiter

        limiter = RateLimiter(4)
        self.assertEqual(limiter.bucket("yandex").rate, 4)
        self.assertGreaterEqual(limiter.bucket("yandex-download").rate, 12)
        self.assertIsNot(limiter.bucket("yandex"), limiter.bucket("yandex-download"))

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
