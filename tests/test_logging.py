"""Журнал ошибок: в errors.log видно, где и почему что-то сломалось."""

from __future__ import annotations

import logging
import threading
import unittest

from app.core import logging_setup


class TestErrorsLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        logging_setup.setup_logging()
        cls.path = logging_setup.errors_log_path()

    def _flush(self) -> str:
        for handler in logging.getLogger().handlers:
            handler.flush()
        return self.path.read_text(encoding="utf-8")

    def test_error_has_place_and_traceback(self) -> None:
        try:
            {}["нет такого ключа"]
        except KeyError:
            logging.getLogger("app.test").warning("Проверка журнала ошибок", exc_info=True)
        text = self._flush()
        self.assertIn("Проверка журнала ошибок", text)
        self.assertIn("test_logging.py", text, "видно файл и строку")
        self.assertIn("Traceback", text)
        self.assertIn("KeyError", text)

    def test_info_does_not_go_to_errors(self) -> None:
        logging.getLogger("app.test").info("обычное сообщение-12345")
        self.assertNotIn("обычное сообщение-12345", self._flush())

    def test_crash_in_thread_is_logged(self) -> None:
        def boom() -> None:
            raise RuntimeError("упало в потоке-777")

        thread = threading.Thread(target=boom, name="проверка")
        thread.start()
        thread.join()
        text = self._flush()
        self.assertIn("Необработанная ошибка в потоке проверка", text)
        self.assertIn("упало в потоке-777", text)


if __name__ == "__main__":
    unittest.main()
