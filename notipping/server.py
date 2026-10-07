import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .runner import TournamentCancelled, tournament
from .game import Game


def run_weights(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object with k")
    k = payload.get("k")
    Game(k)  # Enforce a positive integer, including rejecting booleans.
    return k


def serve(bots, args):
    lock = threading.Lock()
    output_path = Path(args.output)

    def restore_result():
        """Keep the latest completed tournament visible across a server restart."""
        try:
            result = json.loads(output_path.read_text())
            roster = [
                {'name': bot['name'], 'icon': bot['icon'], 'color': bot['color']}
                for bot in bots
            ]
            names = {bot['name'] for bot in roster}
            if (not isinstance(result, dict) or not isinstance(result.get('games'), list)
                    or not result['games'] or not isinstance(result.get('scores'), dict)
                    or set(result['scores']) != names or not isinstance(result.get('k'), int)
                    or not isinstance(result.get('clock_seconds'), (int, float))):
                return None
            saved_roster = result.get('bots')
            if isinstance(saved_roster, list) and len(saved_roster) == len(roster):
                saved_by_name = {bot.get('name'): bot for bot in saved_roster
                                 if isinstance(bot, dict)}
                if (set(saved_by_name) == names and all(
                        isinstance(saved_by_name[name].get('icon'), str)
                        and saved_by_name[name]['icon']
                        and len(saved_by_name[name]['icon']) <= 32
                        and isinstance(saved_by_name[name].get('color'), str)
                        and re.fullmatch(r'#[0-9a-fA-F]{6}', saved_by_name[name]['color'])
                        for name in names)):
                    roster = [{'name': bot['name'], 'icon': saved_by_name[bot['name']]['icon'],
                               'color': saved_by_name[bot['name']]['color']} for bot in bots]
                    for bot in bots:
                        saved_bot = saved_by_name[bot['name']]
                        bot['icon'], bot['color'] = saved_bot['icon'], saved_bot['color']
            info_by_name = {bot['name']: bot for bot in roster}
            for game in result['games']:
                if not isinstance(game, dict) or len(game.get('players', [])) != 2:
                    return None
                game['player_info'] = [info_by_name[name] for name in game['players']]
                for frame in game.get('frames', []):
                    frame['player_info'] = game['player_info']
            result['bots'] = roster
            return result
        except (OSError, ValueError, TypeError, KeyError):
            return None

    restored = restore_result()
    status = {'running': False, 'result': restored, 'error': None,
              'k': restored['k'] if restored else args.k,
              'clock_seconds': restored['clock_seconds'] if restored else args.clock,
              'live': None, 'tournament_id': 1, 'next_action': None,
              'announcement': None, 'stop_requested': False, 'cancelled': False,
              'display_delay': 0}
    resume_game = threading.Event()
    cancel_tournament = threading.Event()
    announced_games = set()
    page = (Path(__file__).resolve().parent.parent / 'web' / 'index.html').read_bytes()

    def public_bot(bot):
        return {'name': bot['name'], 'icon': bot['icon'], 'color': bot['color']}

    def save_result_locked():
        output_path.write_text(json.dumps(status['result'], indent=2))

    def publish_game_complete_locked(game, pair_index, round_number, pairing_count):
        next_action = ('round2' if round_number == 1 else
                       'next_pair' if pair_index + 1 < pairing_count else None)
        result = status['result']
        saved_game = next((g for g in result['games']
                           if str(g.get('game_id')) == str(game['game_id'])), None)
        if saved_game is None:
            saved_game = dict(game)
            result['games'].append(saved_game)
            result['scores'][game['winner']] = result['scores'].get(game['winner'], 0) + 1
        saved_game.update(pairing_id=game['pairing_id'], round_number=round_number,
                          pairing_number=pair_index + 1, pairing_count=pairing_count,
                          pairing_bots=game['pairing_bots'])
        pair_names = game['pairing_bots']
        pair_score = {name: 0 for name in pair_names}
        for completed in result['games']:
            if completed.get('pairing_id') == game['pairing_id']:
                winner = completed['winner']
                pair_score[winner] = pair_score.get(winner, 0) + 1
        status.update(live=None, next_action=next_action,
                      announcement={
                          'id': str(game['game_id']),
                          'round_number': round_number,
                          'pairing_number': pair_index + 1,
                          'pairing_count': pairing_count,
                          'pairing_bots': pair_names,
                          'game_players': game['players'],
                          'winner': game['winner'],
                          'reason': game['reason'],
                          'pair_score': pair_score,
                          'overall_scores': dict(result['scores']),
                          'next_action': next_action,
                      })
        if next_action:
            resume_game.clear()
        announced_games.add(str(game['game_id']))
        save_result_locked()

    def progress(update):
        with lock:
            status['live'] = update
            frames = update.get('frames', [])
            if frames and frames[-1].get('winner') is not None:
                last = frames[-1]
                result = status.get('result')
                game_id = update['game_id']
                if result is not None and not any(str(g.get('game_id')) == str(game_id) for g in result['games']):
                    winner_name = update['players'][last['winner']]
                    game = {'game_id': game_id, 'players': update['players'],
                            'player_info': update['player_info'], 'winner': winner_name,
                            'winner_index': last['winner'], 'reason': last.get('reason'),
                            'clock_seconds': status['clock_seconds'], 'frames': frames,
                            'pairing_id': update['pairing_id'],
                            'round_number': update['round_number'],
                            'pairing_number': update['pairing_number'],
                            'pairing_count': update['pairing_count'],
                            'pairing_bots': update['pairing_bots']}
                    result['games'].append(game)
                    result['scores'][winner_name] = result['scores'].get(winner_name, 0) + 1
                    publish_game_complete_locked(
                        game, update['pairing_number'] - 1,
                        update['round_number'], update['pairing_count'])

    def game_complete(game, pair_index, round_number, pairing_count):
        next_action = ('round2' if round_number == 1 else
                       'next_pair' if pair_index + 1 < pairing_count else None)
        with lock:
            if str(game['game_id']) not in announced_games:
                publish_game_complete_locked(game, pair_index, round_number, pairing_count)
        if next_action and not cancel_tournament.is_set():
            resume_game.wait()
        if next_action:
            with lock:
                status['next_action'] = None

    def run(k, clock_seconds, pairing, display_delay):
        try:
            with lock:
                if status['result'] is None:
                    roster = [public_bot(b) for b in bots]
                    status['result'] = {'k': k, 'clock_seconds': clock_seconds, 'bots': roster,
                                        'scores': {bot['name']: 0 for bot in roster}, 'games': []}
                start_game_id = len(status['result']['games']) + 1
            tournament(bots, k, clock_seconds, on_progress=progress, pairing=pairing,
                       game_id_start=start_game_id, on_game_complete=game_complete,
                       cancel_event=cancel_tournament, display_delay=display_delay)
            with lock:
                save_result_locked()
        except TournamentCancelled:
            with lock:
                status.update(cancelled=True, announcement=None, next_action=None)
        except Exception as exc:
            with lock:
                status['error'] = str(exc)
        finally:
            with lock:
                status['running'] = False
                status['live'] = None

    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, body, kind='application/json'):
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/':
                self.respond(200, page, 'text/html; charset=utf-8')
            elif self.path == '/api/status':
                with lock:
                    data = dict(status, players=[public_bot(b) for b in bots], has_games=bool(status['result'] and status['result']['games']))
                    self.respond(200, json.dumps(data).encode())
            else:
                self.respond(404, b'{}')

        def do_POST(self):
            if self.path == '/api/new':
                if self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
                    self.respond(403, b'{"error":"origin rejected"}')
                    return
                with lock:
                    if status['running']:
                        self.respond(409, b'{"error":"wait for the current games to finish"}')
                        return
                    status.update(result=None, error=None, live=None, k=args.k,
                                  clock_seconds=args.clock, next_action=None,
                                  announcement=None, stop_requested=False,
                                  cancelled=False, display_delay=0,
                                  tournament_id=status['tournament_id'] + 1)
                    cancel_tournament.clear()
                    announced_games.clear()
                    try:
                        output_path.unlink()
                    except FileNotFoundError:
                        pass
                self.respond(200, json.dumps({'tournament_id': status['tournament_id']}).encode())
                return
            if self.path == '/api/stop':
                if self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
                    self.respond(403, b'{"error":"origin rejected"}')
                    return
                with lock:
                    if not status['running']:
                        self.respond(409, b'{"error":"there is no running tournament"}')
                        return
                    status.update(stop_requested=True, announcement=None, next_action=None)
                    cancel_tournament.set()
                    resume_game.set()
                self.respond(202, b'{"stopping":true}')
                return
            if self.path == '/api/continue':
                if self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
                    self.respond(403, b'{"error":"origin rejected"}')
                    return
                with lock:
                    if not status['announcement']:
                        self.respond(409, b'{"error":"there is no result announcement to dismiss"}')
                        return
                    if status['next_action'] and not status['running']:
                        self.respond(409, b'{"error":"there is no waiting round"}')
                        return
                    status['announcement'] = None
                    if status['next_action']:
                        status['next_action'] = None
                        resume_game.set()
                self.respond(200, b'{"continued":true}')
                return
            if self.path != '/api/run':
                self.respond(404, b'{}')
                return
            # Browser requests must originate at this local UI.
            if self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
                self.respond(403, b'{"error":"origin rejected"}')
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024:
                    raise ValueError('Expected a JSON body of at most 1024 bytes')
                payload = json.loads(self.rfile.read(length))
                k = run_weights(payload)
                clock_seconds = payload.get('clock_seconds', 120)
                if type(clock_seconds) not in (int, float) or not 1 <= clock_seconds <= 86400:
                    raise ValueError('clock_seconds must be from 1 to 86400')
                display_delay = payload.get('display_delay', 0)
                if type(display_delay) not in (int, float) or display_delay not in (0, 0.25, 0.5, 1, 2):
                    raise ValueError('display_delay must be 0, 0.25, 0.5, 1, or 2 seconds')
                pairing = payload.get('pairing')
                if pairing is not None:
                    if (not isinstance(pairing, list) or len(pairing) != 2 or
                            not all(isinstance(name, str) for name in pairing) or
                            pairing[0] == pairing[1]):
                        raise ValueError('Choose two distinct bots')
                    known_names = {bot['name'] for bot in bots}
                    if any(name not in known_names for name in pairing):
                        raise ValueError('Selected bot was not found')
            except (ValueError, UnicodeError) as exc:
                self.respond(400, json.dumps({'error': str(exc)}).encode())
                return
            with lock:
                if status['running']:
                    self.respond(409, b'{"error":"already running"}')
                    return
                if status['cancelled']:
                    self.respond(409, b'{"error":"Start a new tournament to change settings after stopping"}')
                    return
                existing = status['result']
                if existing is not None and existing['games'] and (existing['k'] != k or existing['clock_seconds'] != clock_seconds):
                    self.respond(409, b'{"error":"Start a new tournament before changing k or clock settings"}')
                    return
                if existing is None:
                    status.update(k=k, clock_seconds=clock_seconds)
                elif not existing['games']:
                    status.update(k=k, clock_seconds=clock_seconds)
                    existing.update(k=k, clock_seconds=clock_seconds)
                cancel_tournament.clear()
                status.update(running=True, error=None, live=None,
                              stop_requested=False, cancelled=False,
                              display_delay=display_delay)
            threading.Thread(target=run,
                             args=(k, clock_seconds, pairing, display_delay),
                             daemon=True).start()
            self.respond(202, b'{"running":true}')

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print('Open http://127.0.0.1:%d (Ctrl+C to stop)' % args.port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
