import argparse
import colorsys
import json
import os
import re
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from itertools import combinations
from .game import Game, IllegalMove

OUTPUT_LIMIT = 65536


class TournamentCancelled(Exception):
    """Raised when the organizer stops a tournament while a bot is running."""


class BotTimedOut(ValueError):
    """Raised when a bot does not return its move before its clock expires."""


BOT_ICONS = (
    '🐙', '🤖', '🎲', '🐍', '🦊', '🚀', '🐸', '🐼', '🦉', '🐝', '🐬', '🦄',
    '🐢', '🦖', '🦋', '🐳', '🐧', '🐱', '🐯', '🦁', '🌟', '🔮', '🛸', '🍀',
    '⚡', '🎯', '🧩', '🦜', '🐨', '🦈', '🌈', '🎨',
)


def _random_bot_color(rng, used):
    for hue in rng.sample(range(360), 360):
        rgb = colorsys.hls_to_rgb(hue / 360, 0.68, 0.72)
        color = '#%02x%02x%02x' % tuple(round(channel * 255) for channel in rgb)
        if color.lower() not in used:
            return color
    raise ValueError('Too many bots to assign distinct colors')


def load_bots(path):
    path = Path(path).resolve()
    entries = json.loads(path.read_text())
    if not isinstance(entries, list) or len(entries) < 2:
        raise ValueError('Bot manifest must contain at least two bots')
    names = set()
    rng = secrets.SystemRandom()
    used_icons = {bot['icon'] for bot in entries if isinstance(bot, dict) and bot.get('icon')}
    used_colors = {bot['color'].lower() for bot in entries
                   if isinstance(bot, dict) and isinstance(bot.get('color'), str)}
    for index, bot in enumerate(entries):
        if not isinstance(bot.get('name'), str) or not bot['name'] or bot['name'] in names:
            raise ValueError('Each bot needs a unique nonempty name')
        names.add(bot['name'])
        if 'icon' not in bot:
            choices = [icon for icon in BOT_ICONS if icon not in used_icons]
            bot['icon'] = rng.choice(choices or BOT_ICONS)
            used_icons.add(bot['icon'])
        if 'color' not in bot:
            bot['color'] = _random_bot_color(rng, used_colors)
            used_colors.add(bot['color'].lower())
        if not isinstance(bot['icon'], str) or not bot['icon'] or len(bot['icon']) > 32:
            raise ValueError('Each bot icon must be a short nonempty string')
        if not isinstance(bot['color'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', bot['color']):
            raise ValueError('Each bot color must be a six-digit hex color such as #79bcff')
        command = bot.get('command')
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError('command must be a nonempty array of strings')
        bot['cwd'] = str((path.parent / bot.get('cwd', '.')).resolve())
        if not Path(bot['cwd']).is_dir():
            raise ValueError('Bot folder does not exist: ' + bot['cwd'])
        bot['command'] = [sys.executable if x == '{python}' else x for x in command]
    return entries


class BotSession:
    """Line-oriented bot connection; one process can handle every turn in a game."""
    def __init__(self, bot):
        self.bot = bot
        self.proc = None
        self.lock = threading.Lock()
        self.active_turn = None
        self.protocol_error = None
        self.reader_threads = []

    def start(self):
        """Start the bot process before the game clock is charged.

        Starting is idempotent so callers can eagerly launch all bots at the
        beginning of a game while get_move remains safe for one-off callers.
        """
        if self.proc is not None:
            if self.proc.poll() is not None:
                raise ValueError('bot process exited before the game ended')
            return
        self.proc = subprocess.Popen(
            self.bot['command'], cwd=self.bot['cwd'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            start_new_session=True)
        proc = self.proc
        self.reader_threads = [
            threading.Thread(target=self._read_stdout, args=(proc,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(proc,), daemon=True),
        ]
        for thread in self.reader_threads:
            thread.start()

    def _read_stdout(self, proc):
        while True:
            line = proc.stdout.readline(OUTPUT_LIMIT + 1)
            with self.lock:
                if self.proc is not proc:
                    return
                if not line:
                    turn = self.active_turn
                    if turn and turn['response'] is None and turn['error'] is None:
                        turn['error'] = 'bot exited without returning a move'
                        turn['event'].set()
                    return
                turn = self.active_turn
                if len(line) > OUTPUT_LIMIT:
                    message = 'bot output exceeded 64 KiB per stream'
                    if turn and turn['sent']:
                        turn['error'] = message
                        turn['event'].set()
                    else:
                        self.protocol_error = message
                    continue
                if not turn or not turn['sent']:
                    self.protocol_error = 'bot wrote to stdout before receiving a move request'
                elif turn['response'] is not None:
                    turn['error'] = 'bot must return exactly one JSON line per move'
                    turn['event'].set()
                else:
                    turn['response'] = line
                    turn['event'].set()

    def _read_stderr(self, proc):
        while True:
            chunk = proc.stderr.read(4096)
            if not chunk:
                return
            with self.lock:
                if self.proc is not proc:
                    return
                turn = self.active_turn
                if turn and turn['sent']:
                    turn['stderr_bytes'] += len(chunk)
                    if turn['stderr_bytes'] > OUTPUT_LIMIT:
                        turn['error'] = 'bot output exceeded 64 KiB per stream'
                        turn['event'].set()

    def get_move(self, state, timeout, cancel_event=None):
        if self.proc is None:
            self.start()
        elif self.proc.poll() is not None:
            raise ValueError('bot process exited before the game ended')
        with self.lock:
            if self.protocol_error:
                raise ValueError(self.protocol_error)
        turn = {'event': threading.Event(), 'response': None, 'error': None,
                'stderr_bytes': 0, 'sent': False}
        with self.lock:
            self.active_turn = turn
        try:
            payload = (json.dumps(state) + '\n').encode()
            turn['sent'] = True
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
            deadline = time.monotonic() + timeout
            while not turn['event'].wait(0.01):
                if cancel_event is not None and cancel_event.is_set():
                    raise TournamentCancelled()
                if time.monotonic() >= deadline:
                    raise BotTimedOut('move timed out')
                if self.proc.poll() is not None:
                    raise ValueError('bot exited before returning a move')
            if cancel_event is not None and cancel_event.is_set():
                raise TournamentCancelled()
            with self.lock:
                if turn['error']:
                    raise ValueError(turn['error'])
                if self.protocol_error:
                    raise ValueError(self.protocol_error)
                response = turn['response']
            if time.monotonic() > deadline:
                raise BotTimedOut('clock expired')
            if response is None:
                raise ValueError('bot exited without returning a move')
            return json.loads(response.decode())
        except (BrokenPipeError, OSError) as exc:
            raise ValueError('bot process failed: ' + str(exc)) from exc
        finally:
            with self.lock:
                if self.active_turn is turn:
                    self.active_turn = None

    def close(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if proc.poll() is None:
                proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        for thread in self.reader_threads:
            thread.join(timeout=1)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


def get_move(bot, state, timeout):
    """Compatibility helper for one request; games use a persistent BotSession."""
    session = BotSession(bot)
    try:
        return session.get_move(state, timeout)
    finally:
        session.close()


def play(bots, k, clock_seconds, game_id, on_progress=None, cancel_event=None,
         display_delay=0):
    game = Game(k)
    clocks = [float(clock_seconds), float(clock_seconds)]
    frames = []
    players = [b['name'] for b in bots]

    def frame(move=None):
        state = game.state()
        state['clocks'] = list(clocks)
        state['player_info'] = [{'name': b['name'], 'icon': b['icon'], 'color': b['color']} for b in bots]
        if move is not None:
            state['move'] = move
        frames.append(state)
        return state

    sessions = [BotSession(bot) for bot in bots]
    try:
        # Launch every bot before the first turn begins. Process startup is
        # setup time, not time spent thinking on a player's clock.
        for player, session in enumerate(sessions):
            try:
                session.start()
            except (OSError, ValueError) as exc:
                # A launch failure is still a forfeit, but it must not charge
                # either clock because the game has not started yet.
                game.turn = player
                game.forfeit(str(exc))
                break
        frame()
        while game.winner is None:
            if cancel_event is not None and cancel_event.is_set():
                raise TournamentCancelled()
            player = game.turn
            state = game.state()
            state.update(game_id=game_id, ply=len(frames), players=players,
                         clocks=list(clocks))
            active_since = time.time()
            if on_progress:
                on_progress({'players': players, 'frames': list(frames), 'clocks': list(clocks),
                             'active_player': player, 'active_since': active_since,
                             'game_id': game_id, 'player_info': [{'name': b['name'], 'icon': b['icon'], 'color': b['color']} for b in bots]})
            move = None
            started = time.monotonic()
            try:
                if clocks[player] <= 0:
                    raise BotTimedOut('clock expired')
                move = sessions[player].get_move(state, clocks[player], cancel_event)
                elapsed = time.monotonic() - started
                clocks[player] = max(0.0, clocks[player] - elapsed)
                game.apply(move)
            except TournamentCancelled:
                raise
            except BotTimedOut:
                clocks[player] = 0.0
                game.forfeit('time')
            except (OSError, ValueError, IllegalMove) as exc:
                elapsed = time.monotonic() - started
                clocks[player] = max(0.0, clocks[player] - elapsed)
                if 'clock expired' in str(exc) or clocks[player] <= 0:
                    clocks[player] = 0.0
                    game.forfeit('time')
                else:
                    game.forfeit(str(exc))
            frame(move)
            if on_progress:
                on_progress({'players': players, 'frames': list(frames), 'clocks': list(clocks),
                             'active_player': None, 'active_since': None,
                             'game_id': game_id, 'player_info': [{'name': b['name'], 'icon': b['icon'], 'color': b['color']} for b in bots]})
            if display_delay > 0:
                stopped = cancel_event.wait(display_delay) if cancel_event is not None else False
                if stopped and game.winner is None:
                    raise TournamentCancelled()
                if cancel_event is None:
                    time.sleep(display_delay)
    finally:
        for session in sessions:
            session.close()
    return {'players': players, 'winner': bots[game.winner]['name'],
            'reason': game.reason, 'clock_seconds': clock_seconds, 'game_id': game_id,
            'player_info': [{'name': b['name'], 'icon': b['icon'], 'color': b['color']} for b in bots],
            'frames': frames}


def tournament(bots, k=15, clock_seconds=120, on_progress=None, pairing=None,
               game_id_start=1, on_game_complete=None, cancel_event=None,
               display_delay=0):
    if clock_seconds <= 0:
        raise ValueError('clock_seconds must be positive')
    Game(k)
    if pairing is None:
        chosen_bots = bots
        pairs = list(combinations(bots, 2))
    else:
        if len(pairing) != 2 or pairing[0] == pairing[1]:
            raise ValueError('Choose two distinct bots')
        by_name = {b['name']: b for b in bots}
        if any(name not in by_name for name in pairing):
            raise ValueError('Selected bot was not found')
        chosen_bots = [by_name[name] for name in pairing]
        pairs = [(chosen_bots[0], chosen_bots[1])]
    games = []
    scores = {b['name']: 0 for b in chosen_bots}
    for pair_index, (a, b) in enumerate(pairs):
        pair_id = str(game_id_start + len(games))
        for round_number, ordered in enumerate(([a, b], [b, a]), start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise TournamentCancelled()
            result = play(ordered, k, clock_seconds, str(game_id_start + len(games)),
                          on_progress, cancel_event, display_delay)
            result.update(pairing_id=pair_id, round_number=round_number,
                          pairing_number=pair_index + 1, pairing_count=len(pairs),
                          pairing_bots=[a['name'], b['name']])
            games.append(result)
            scores[result['winner']] += 1
            if on_game_complete:
                on_game_complete(result, pair_index, round_number, len(pairs))
    return {'k': k, 'clock_seconds': clock_seconds,
            'bots': [{'name': b['name'], 'icon': b['icon'], 'color': b['color']} for b in chosen_bots],
            'scores': scores, 'games': games}


def main():
    parser = argparse.ArgumentParser(description='No Tipping tournament runner')
    parser.add_argument('--bots', default='bots.json')
    parser.add_argument('--k', type=int, default=15)
    parser.add_argument('--clock', type=float, default=120, help='total seconds per player per game')
    parser.add_argument('--output', default='results.json')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    bots = load_bots(args.bots)
    if args.serve:
        from .server import serve
        serve(bots, args)
    else:
        result = tournament(bots, args.k, args.clock)
        Path(args.output).write_text(json.dumps(result, indent=2))
        print(json.dumps(result['scores'], indent=2))

if __name__ == '__main__':
    main()
