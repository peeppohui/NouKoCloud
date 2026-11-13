"""Свободное место и CSV-отчёт очереди."""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from app.core.diskspace import GIB, check_space
from app.core.models import CloudFile, DownloadJob, EntryKind, JobState, ProviderId
from app.downloader.report import CSV_COLUMNS, default_report_path, write_report


def _file(name: str, size: int | None = 100) -> CloudFile:
    return CloudFile(
        provider=ProviderId.YANDEX,
        file_id=name,
        path=f"Съёмка/{name}",
        name=name,
        kind=EntryKind.FILE,
        size=size or 0,
    )


class TestDiskSpace(unittest.TestCase):
    def test_reserve_is_added_to_known_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("app.core.diskspace.shutil.disk_usage") as usage:
            usage.return_value = mock.Mock(free=3 * GIB)
            result = check_space(Path(tmp), [_file("a.mov", 2 * GIB)])
            self.assertTrue(result.ok)
            self.assertEqual(result.required_bytes, 3 * GIB)

    def test_shortage_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("app.core.diskspace.shutil.disk_usage") as usage:
            usage.return_value = mock.Mock(free=GIB // 2)
            result = check_space(Path(tmp), [_file("a.mov", 2 * GIB)])
            self.assertFalse(result.ok)
            self.assertEqual(result.missing_bytes, 2 * GIB + GIB // 2)

    def test_unknown_size_still_requires_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("app.core.diskspace.shutil.disk_usage") as usage:
            usage.return_value = mock.Mock(free=GIB // 2)
            result = check_space(Path(tmp), [_file("doc", 0)])
            self.assertFalse(result.ok)
            self.assertEqual(result.unknown_count, 1)
            self.assertEqual(result.required_bytes, GIB)

    def test_probe_leaves_no_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "new" / "folder"
            check_space(target, [])
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])


class TestReport(unittest.TestCase):
    def _job(self, name: str, state: JobState) -> DownloadJob:
        job = DownloadJob(file=_file(name, 5), dest_dir=Path("C:/Downloads"))
        job.state = state
        job.created_at = 100.0
        job.finished_at = 130.0
        job.total_bytes = 5
        job.downloaded_bytes = 5
        return job

    def test_csv_is_excel_friendly_and_escaped(self) -> None:
        jobs = [
            self._job('клип; "дубль".mov', JobState.DONE),
            self._job("ошибка.mov", JobState.FAILED),
        ]
        jobs[0].file.md5 = "abc"
        jobs[0].md5_actual = "abc"
        jobs[1].error = "token=secret; сбой"
        with tempfile.TemporaryDirectory() as tmp:
            path = write_report(jobs, Path(tmp) / "report.csv")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"secret", raw)
            self.assertIn(b"<redacted>", raw)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle, delimiter=";"))
        self.assertEqual(tuple(rows[0]), CSV_COLUMNS)
        self.assertEqual(rows[1][0], 'клип; "дубль".mov')
        self.assertEqual(rows[1][8], "Готово")
        self.assertEqual(rows[1][13], datetime.fromtimestamp(130).isoformat(timespec="seconds"))
        self.assertEqual(rows[1][14], "30.0")

    def test_thousand_jobs_are_written(self) -> None:
        jobs = [self._job(f"клип_{i:04d}.mov", JobState.DONE) for i in range(1000)]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_report(jobs, Path(tmp) / "report.csv")
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle, delimiter=";"))
        self.assertEqual(len(rows), 1001)
        self.assertEqual(rows[1][0], "клип_0000.mov")
        self.assertEqual(rows[1000][0], "клип_0999.mov")

    def test_default_name_has_timestamp(self) -> None:
        path = default_report_path(Path("C:/out"), datetime(2026, 11, 1, 12, 34, 56))
        self.assertEqual(path.name, "NoukoCloud-report-20261101-123456.csv")


if __name__ == "__main__":
    unittest.main()
