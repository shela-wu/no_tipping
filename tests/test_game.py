import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import patch
from notipping.game import Game, IllegalMove
from notipping.runner import (BotSession, TournamentCancelled, get_move,
                              load_bots, play, tournament)

class RulesTests(unittest.TestCase):
    def test_initial_torque_includes_board(self):
        self.assertEqual(Game().torques(), (-6, 6))

    def test_support_positions_allowed(self):
        g = Game(1)
        g.apply({'position': -3, 'weight': 1})
        g.apply({'position': -1, 'weight': 1})
        self.assertIsNone(g.winner)
        self.assertEqual(g.phase, 'remove')
        self.assertEqual(g.turn, 0)
        g.apply({'position': -1})  # first player removes opponent's block
        self.assertIsNone(g.winner)

    def test_occupied_and_invalid_move_do_not_mutate(self):
        g = Game()
        before = g.state()
        for move in [{'position': -4, 'weight': 1}, {'position': True, 'weight': 1}, {'position': 31, 'weight': 1}, {'position': 0, 'weight': 25}]:
            with self.assertRaises(IllegalMove):
                g.apply(move)
            self.assertEqual(g.state(), before)

    def test_zero_torque_is_stable(self):
        g = Game(1)
        g.apply({'position': 5, 'weight': 1})
        self.assertEqual(g.torques()[1], 0)
        self.assertIsNone(g.winner)

    def test_tip_loses(self):
        g = Game()
        g.apply({'position': 30, 'weight': 15})
        self.assertEqual(g.winner, 1)
        self.assertEqual(g.reason, 'tipping')

    def test_initial_block_removable(self):
        g = Game(1)
        g.apply({'position': -3, 'weight': 1})
        g.apply({'position': -1, 'weight': 1})
        g.apply({'position': -4})
        self.assertNotIn(-4, g.board)
        self.assertEqual(g.winner, 1)

    def test_weight_bounds(self):
        for k in [0, -1, True, 1.5]:
            with self.assertRaises(ValueError): Game(k)
        self.assertEqual(Game(25).k, 25)
        self.assertEqual(Game(31).k, 31)
        self.assertEqual(Game(1000).k, 1000)

class RunnerTests(unittest.TestCase):
    def test_play_starts_all_bots_before_the_first_clock(self):
        with tempfile.TemporaryDirectory() as cwd:
            markers = [Path(cwd) / 'bot-a-started', Path(cwd) / 'bot-b-started']
            bots = []
            for marker in markers:
                bots.append({
                    'name': marker.stem,
                    'cwd': cwd,
                    'icon': '🤖',
                    'color': '#123456',
                    'command': [sys.executable, '-u', '-c',
                                'import json, pathlib, sys, time\n'
                                f'time.sleep(0.05); pathlib.Path({str(marker)!r}).touch()\n'
                                'for line in sys.stdin:\n'
                                ' print(json.dumps({"position": 30, "weight": 15}), flush=True)'],
                })

            started = []
            original_start = BotSession.start

            def record_start(session):
                started.append(session.bot['name'])
                return original_start(session)

            def check_first_progress(update):
                if update['active_player'] is not None:
                    self.assertEqual(set(started), {marker.stem for marker in markers})

            with patch.object(BotSession, 'start', new=record_start):
                play_result = play(
                    bots, k=15, clock_seconds=2, game_id='startup-test',
                    on_progress=check_first_progress)

            self.assertEqual(play_result['reason'], 'tipping')
            self.assertTrue(all(marker.exists() for marker in markers))

    def test_bot_session_keeps_process_memory_between_moves(self):
        with tempfile.TemporaryDirectory() as cwd:
            bot = {
                'cwd': cwd,
                'command': [sys.executable, '-u', '-c',
                            'import json,sys\ncount=0\n'
                            'for line in sys.stdin:\n'
                            ' count += 1\n'
                            ' print(json.dumps({"position":count}), flush=True)'],
            }
            session = BotSession(bot)
            try:
                state = Game(1).state()
                self.assertEqual(session.get_move(state, 2), {'position': 1})
                self.assertEqual(session.get_move(state, 2), {'position': 2})
            finally:
                session.close()

    def test_cancellation_stops_a_waiting_bot_process(self):
        with tempfile.TemporaryDirectory() as cwd:
            session = BotSession({
                'cwd': cwd,
                'command': [sys.executable, '-u', '-c',
                            'import time; time.sleep(30)'],
            })
            cancelled = threading.Event()
            outcome = []

            def request_move():
                try:
                    session.get_move(Game(1).state(), 60, cancelled)
                except TournamentCancelled:
                    outcome.append('cancelled')

            worker = threading.Thread(target=request_move)
            worker.start()
            deadline = time.monotonic() + 2
            while session.proc is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNotNone(session.proc)
            cancelled.set()
            worker.join(timeout=2)
            try:
                self.assertFalse(worker.is_alive())
                self.assertEqual(outcome, ['cancelled'])
            finally:
                session.close()

    def test_double_round_robin(self):
        bots = load_bots(Path(__file__).resolve().parents[1] / 'bots.json')[:2]
        r = tournament(bots, k=2, clock_seconds=3)
        self.assertEqual(len(r['games']), 2)
        self.assertEqual(r['games'][0]['players'], list(reversed(r['games'][1]['players'])))
        self.assertEqual(sum(r['scores'].values()), 2)
        self.assertTrue(all(g['reason'] == 'tipping' for g in r['games']))

    def test_timeout_is_a_loss_and_does_not_block_round_two(self):
        with tempfile.TemporaryDirectory() as cwd:
            bots = [
                {
                    'name': 'Slow', 'cwd': cwd, 'icon': '🐌', 'color': '#123456',
                    'command': [sys.executable, '-u', '-c',
                                'import time; time.sleep(2)'],
                },
                {
                    'name': 'Fast', 'cwd': cwd, 'icon': '⚡', 'color': '#654321',
                    'command': [sys.executable, '-u', '-c',
                                'import json,sys\n'
                                'for line in sys.stdin:\n'
                                ' print(json.dumps({"position":-3,"weight":1}), flush=True)'],
                },
            ]

            result = tournament(bots, k=1, clock_seconds=.05,
                                 pairing=['Slow', 'Fast'])

        self.assertEqual(len(result['games']), 2)
        self.assertEqual([game['reason'] for game in result['games']], ['time', 'time'])
        self.assertEqual([game['winner'] for game in result['games']], ['Fast', 'Fast'])
        self.assertEqual(result['scores'], {'Slow': 0, 'Fast': 2})

    def test_tournament_reports_each_round_before_continuing(self):
        bots = load_bots(Path(__file__).resolve().parents[1] / 'bots.json')[:2]
        callbacks = []

        def fake_play(ordered, k, clock_seconds, game_id, on_progress,
                      cancel_event=None, display_delay=0):
            players = [bot['name'] for bot in ordered]
            return {'players': players, 'winner': players[0], 'reason': 'test',
                    'clock_seconds': clock_seconds, 'game_id': game_id, 'frames': []}

        with patch('notipping.runner.play', side_effect=fake_play):
            result = tournament(bots, k=1, clock_seconds=120,
                                on_game_complete=lambda game, pair_index, round_number, count:
                                callbacks.append((game, pair_index, round_number, count)),
                                pairing=[bots[0]['name'], bots[1]['name']])

        self.assertEqual([entry[2] for entry in callbacks], [1, 2])
        self.assertEqual([entry[1:] for entry in callbacks], [(0, 1, 1), (0, 2, 1)])
        self.assertEqual(callbacks[0][0]['pairing_id'], callbacks[1][0]['pairing_id'])
        self.assertEqual(result['games'][0]['players'], list(reversed(result['games'][1]['players'])))

    def test_tournament_cancellation_prevents_the_next_game(self):
        bots = load_bots(Path(__file__).resolve().parents[1] / 'bots.json')[:2]
        cancelled = threading.Event()
        completed = []
        with self.assertRaises(TournamentCancelled):
            tournament(bots, k=1, clock_seconds=10,
                       pairing=[bots[0]['name'], bots[1]['name']],
                       cancel_event=cancelled,
                       on_game_complete=lambda *args: (completed.append(args), cancelled.set()))
        self.assertEqual(len(completed), 1)

    def test_failed_bots(self):
        with tempfile.TemporaryDirectory() as cwd:
            for code, timeout in [('import time; time.sleep(2)', .05), ('print("invalid")', 1), ('print("x" * 70000)', 1), ('raise SystemExit(2)', 1)]:
                with self.assertRaises(ValueError):
                    get_move({'command': [sys.executable, '-c', code], 'cwd': cwd}, Game().state(), timeout)

if __name__ == '__main__': unittest.main()
