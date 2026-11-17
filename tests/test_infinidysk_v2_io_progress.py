"""Counters describe measured work, not authorization or a whole-job ETA."""

import io
import unittest

from utils.infinidysk_v2_io_progress import (
    IOProgress,
    ProgressReader,
    ProgressWriter,
    public_io_progress,
)


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    def wall(self):
        return 1000 + self.value


class IOProgressTests(unittest.TestCase):
    def recorder(self, phase="blob_copy", total=5000, **kwargs):
        clock, events = Clock(), []
        measured = IOProgress(
            lambda stage, message, **detail: events.append(
                (stage, detail["io_progress"])
            ),
            phase,
            total=total,
            monotonic=clock.now,
            wall_clock=clock.wall,
            **kwargs,
        )
        return clock, events, measured

    def test_thousands_of_updates_do_not_rewrite_state_per_entry(self):
        checks = []
        _, events, measured = self.recorder(total=10000, check=lambda: checks.append(1))
        for count in range(1, 10001):
            measured.update(count)
        measured.finish()
        self.assertEqual(2, len(events))
        self.assertGreaterEqual(len(checks), 10000)
        self.assertEqual(10000, events[-1][1]["processed"])
        self.assertTrue(events[-1][1]["complete"])

    def test_cancellation_remains_observable_when_clock_is_frozen(self):
        cancelled = False

        def check():
            if cancelled:
                raise RuntimeError("cancelled")

        _, events, measured = self.recorder(check=check)
        measured.add(1)
        cancelled = True
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            measured.add(1)
        self.assertEqual(1, len(events))

    def test_rate_and_remaining_are_measured_for_this_pass(self):
        clock, events, measured = self.recorder()
        for second in range(1, 11):
            clock.value = second
            measured.update(second * 100)
        payload = events[-1][1]
        self.assertEqual(100, payload["rate_per_second"])
        self.assertEqual(40, payload["estimated_remaining_seconds"])
        public = public_io_progress(
            payload, active=True, stage="staging_blobs", now=1010
        )
        self.assertEqual(40, public["estimated_remaining_seconds"])
        for active, stage, now in (
            (False, "staging_blobs", 1010),
            (True, "verifying_blob_staging", 1010),
            (True, "staging_blobs", 1041),
        ):
            value = public_io_progress(payload, active=active, stage=stage, now=now)
            self.assertIsNone(value["estimated_remaining_seconds"])
            self.assertIsNone(value["rate_per_second"])

    def test_unknown_total_can_show_pace_but_not_remaining_time(self):
        clock, events, measured = self.recorder(total=None)
        for second in range(1, 11):
            clock.value = second
            measured.add(100)
        public = public_io_progress(
            events[-1][1],
            active=True,
            stage="staging_blobs",
            now=1010,
        )
        self.assertEqual(100, public["rate_per_second"])
        self.assertIsNone(public["total"])
        self.assertIsNone(public["estimated_remaining_seconds"])

    def test_pause_overrun_completion_and_new_pass_reset_estimates(self):
        clock, events, measured = self.recorder(total=2000)
        for second in range(1, 11):
            clock.value = second
            measured.add(100)
        self.assertIsNotNone(events[-1][1]["estimated_remaining_seconds"])
        clock.value = 50
        measured.add(1)
        self.assertIsNone(events[-1][1]["rate_per_second"])
        clock.value = 51
        measured.update(2001)
        self.assertIsNone(events[-1][1]["estimated_remaining_seconds"])
        measured.finish()
        self.assertIsNone(events[-1][1]["rate_per_second"])
        _, new_events, _ = self.recorder()
        self.assertNotEqual(events[-1][1]["phase_id"], new_events[-1][1]["phase_id"])

    def test_public_payload_rejects_malformed_counts_units_and_private_fields(self):
        _, events, _ = self.recorder()
        value = events[0][1]
        public = public_io_progress(
            dict(value, password="private", raw_path="/private/input"),
            active=True,
            stage="staging_blobs",
            now=1000,
        )
        self.assertNotIn("password", public)
        self.assertNotIn("raw_path", public)
        for field, invalid in (
            ("processed", True),
            ("total", -1),
            ("unit", "rows"),
            ("phase", "private"),
            ("phase_id", "../escape"),
            ("elapsed_seconds", 1.5),
        ):
            with self.subTest(field=field):
                self.assertIsNone(
                    public_io_progress(
                        dict(value, **{field: invalid}),
                        active=True,
                        stage="staging_blobs",
                        now=1000,
                    )
                )

    def test_reader_counts_actual_returned_bytes_and_writer_preserves_result(self):
        _, _, measured = self.recorder(total=3)
        reader = ProgressReader(io.BytesIO(b"abc"), measured)
        self.assertEqual(b"ab", reader.read(2))
        self.assertEqual(b"c", reader.read(2))
        self.assertEqual(b"", reader.read(2))
        self.assertEqual(3, measured.processed)

        class Partial:
            def write(self, data):
                return 2

        _, _, written = self.recorder(total=10)
        self.assertEqual(2, ProgressWriter(Partial(), written).write(b"hello"))
        self.assertEqual(2, written.processed)

    def test_legacy_callback_and_callback_errors_remain_compatible(self):
        events = []
        measured = IOProgress(
            lambda stage, message: events.append(stage), "archive_write"
        )
        measured.finish()
        self.assertEqual(["archiving", "archiving"], events)

        def broken(stage, message, **detail):
            raise TypeError("callback body failed")

        with self.assertRaisesRegex(TypeError, "callback body failed"):
            IOProgress(broken, "archive_write")

    def test_oversized_optional_rate_never_breaks_status_serialization(self):
        _, events, _ = self.recorder()
        payload = dict(
            events[0][1],
            elapsed_seconds=10,
            processed=100,
            rate_per_second=10**400,
            estimated_remaining_seconds=10,
        )
        public = public_io_progress(
            payload, active=True, stage="staging_blobs", now=1000
        )
        self.assertIsNone(public["rate_per_second"])
        self.assertIsNone(public["estimated_remaining_seconds"])
