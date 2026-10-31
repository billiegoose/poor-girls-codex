from __future__ import annotations

import unittest
from unittest import mock

import chatgpt_web as web


class StalledReloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.interface = mock.Mock()
        self.interface.latest_user_message.return_value = ('user-1', 'Prompt')
        self.interface.rate_limit_remaining.return_value = 0.0
        self.session = web.WebSession('a', 'Cats', mock.Mock(), routing_session_id='root')
        self.watcher = web.WebSessionWatcher(
            self.interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(return_value={'ok': True}),
            fenced_result=lambda result: 'RESULT',
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.0,
            clock=lambda: self.now,
        )
        self.watcher.sessions = {'a': self.session}
        self.watcher.refresh_sessions = mock.Mock()
        self.watcher.scan_session = mock.Mock(return_value=False)
        self.watcher.recover_rejected_submission = mock.Mock(return_value=False)
        self.watcher.deliver_session = mock.Mock(return_value=False)

    def step_at(self, now: float) -> bool:
        self.now = now
        with mock.patch('builtins.print'):
            return self.watcher.step()

    def test_first_reload_at_60_seconds_then_jittered_five_to_fifteen_minutes(self) -> None:
        with mock.patch.object(web.random, 'uniform', side_effect=[300.0, 900.0, 300.0]) as jitter:
            for now in (0.0, 59.0, 60.0, 359.0, 360.0, 1259.0, 1260.0):
                self.step_at(now)
        self.assertEqual(self.interface.reload_session.call_args_list, [
            mock.call(self.session), mock.call(self.session), mock.call(self.session),
        ])
        self.assertEqual(jitter.call_args_list, [
            mock.call(300.0, 900.0), mock.call(300.0, 900.0), mock.call(300.0, 900.0),
        ])
        self.assertEqual(self.session.stall_started_at, 0.0)
        self.assertEqual(self.session.next_stall_reload_at, 1560.0)

    def test_hours_without_progress_keep_reloading_without_stopping_pgc(self) -> None:
        with mock.patch.object(web.random, 'uniform', return_value=900.0):
            for now in (0.0, 60.0, 960.0, 1860.0, 2760.0, 3660.0, 7200.0):
                self.step_at(now)
        self.assertEqual(self.interface.reload_session.call_count, 6)
        self.assertEqual(self.session.stall_started_at, 0.0)
        self.assertEqual(self.session.next_stall_reload_at, 8100.0)

    def test_reload_failure_is_retried_after_jitter_not_each_tick(self) -> None:
        self.interface.reload_session.side_effect = RuntimeError('site unavailable')
        with mock.patch.object(web.random, 'uniform', return_value=300.0):
            for now in (0.0, 60.0, 61.0, 359.0, 360.0):
                self.step_at(now)
        self.assertEqual(self.interface.reload_session.call_count, 2)
        self.assertEqual(self.session.next_stall_reload_at, 660.0)

    def test_forward_progress_resets_first_reload_and_hour_cutoff(self) -> None:
        self.step_at(0.0)
        self.watcher.note_forward_progress(self.session, 50.0)
        with mock.patch.object(web.random, 'uniform', return_value=300.0):
            self.step_at(60.0)
            self.step_at(109.0)
            self.interface.reload_session.assert_not_called()
            self.step_at(110.0)
        self.interface.reload_session.assert_called_once_with(self.session)
        self.assertEqual(self.session.stall_started_at, 50.0)

    def test_detected_terminal_never_triggers_stall_recovery(self) -> None:
        self.step_at(0.0)
        self.watcher.note_terminal_response(self.session)
        self.step_at(3600.0)
        self.assertTrue(self.session.stall_terminal)
        self.interface.reload_session.assert_not_called()
        self.assertIsNone(self.session.stall_started_at)

    def test_new_user_turn_rearms_watchdog_after_terminal(self) -> None:
        self.step_at(0.0)
        self.watcher.note_terminal_response(self.session)
        self.interface.latest_user_message.return_value = ('user-2', 'Next prompt')
        with mock.patch.object(web.random, 'uniform', return_value=300.0):
            self.step_at(3600.0)
            self.step_at(3659.0)
            self.interface.reload_session.assert_not_called()
            self.step_at(3660.0)
        self.interface.reload_session.assert_called_once_with(self.session)
        self.assertFalse(self.session.stall_terminal)

    def test_each_conversation_maintains_its_own_deadline(self) -> None:
        other = web.WebSession('b', 'Dogs', mock.Mock(), routing_session_id='root::child')
        self.watcher.sessions['b'] = other
        self.step_at(0.0)
        self.watcher.note_forward_progress(other, 30.0)
        with mock.patch.object(web.random, 'uniform', return_value=300.0):
            self.step_at(60.0)
            self.interface.reload_session.assert_called_once_with(self.session)
            self.step_at(90.0)
        self.assertEqual(self.interface.reload_session.call_args_list, [
            mock.call(self.session), mock.call(other),
        ])

    def test_unattached_inactive_page_is_not_reloaded(self) -> None:
        self.session.routing_session_id = None
        for now in (0.0, 60.0, 3600.0):
            self.step_at(now)
        self.interface.reload_session.assert_not_called()

    def test_actual_delivery_updates_progress_timestamp(self) -> None:
        self.step_at(0.0)
        self.session.pending_responses = [web.PendingResponse('RESULT', 'FALLBACK')]
        self.now = 50.0
        self.assertTrue(web.WebSessionWatcher.deliver_session(self.watcher, self.session, 0.0))
        self.assertEqual(self.session.stall_started_at, 50.0)
        self.assertEqual(self.session.next_stall_reload_at, 110.0)

    def test_ten_minute_tool_call_does_not_cause_immediate_reload(self) -> None:
        self.interface.valid_request.return_value = (
            '{}', {'session': 'root', 'tool': 'status'}, 'tool-fingerprint',
        )
        self.interface.submit.side_effect = RuntimeError('send button unavailable')
        self.watcher.scan_session = lambda session, now: web.WebSessionWatcher.scan_session(
            self.watcher, session, now
        )
        self.watcher.deliver_session = lambda session, now: web.WebSessionWatcher.deliver_session(
            self.watcher, session, now
        )

        def finish_long_tool(*args, **kwargs):
            self.now = 600.0
            return {'ok': True}

        self.watcher.execute_request.side_effect = finish_long_tool
        self.step_at(0.0)  # Discover the tool call.
        self.step_at(0.0)  # Execute; virtual clock advances by ten minutes.
        self.watcher.execute_request.assert_called_once()
        self.assertEqual(self.session.stall_started_at, 600.0)
        self.assertEqual(self.session.delivery_failure_started_at, 600.0)
        self.assertEqual(self.session.next_delivery_at, 600.25)
        self.interface.reload_session.assert_not_called()

        with mock.patch.object(web.random, 'uniform', return_value=300.0):
            self.step_at(659.0)
            self.interface.reload_session.assert_not_called()
            self.step_at(660.0)
        self.interface.reload_session.assert_called_once_with(self.session)

    def test_long_successful_submission_resets_watchdog_on_completion(self) -> None:
        self.session.pending_responses = [web.PendingResponse('RESULT', 'FALLBACK')]
        self.watcher.deliver_session = lambda session, now: web.WebSessionWatcher.deliver_session(
            self.watcher, session, now
        )

        def finish_long_submission(*args, **kwargs):
            self.now = 600.0

        self.interface.submit.side_effect = finish_long_submission
        self.step_at(0.0)
        self.assertEqual(self.session.stall_started_at, 600.0)
        self.interface.reload_session.assert_not_called()
        with mock.patch.object(web.random, 'uniform', return_value=300.0):
            self.step_at(659.0)
            self.interface.reload_session.assert_not_called()
            self.step_at(660.0)
        self.interface.reload_session.assert_called_once_with(self.session)


if __name__ == '__main__':
    unittest.main()
