import hashlib
import ipaddress
import json
import re
import os
import socket
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlsplit, urljoin
from datetime import datetime
from flask import Flask, jsonify, redirect, render_template, request, url_for, send_file
from monitor import run_checks
import requests

basedir = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(basedir, 'database.db')
THEME_PATH = os.path.join(basedir, 'theme.json')
app = Flask(__name__)
theme_lock = threading.RLock()
ICON_CACHE_DIR = os.path.join(basedir, 'favicon_cache')
ICON_CACHE_SECONDS = 7 * 86400
ICON_FAILURE_SECONDS = 10 * 60
ICON_MAX_BYTES = 256 * 1024
_icon_locks = {}
_icon_locks_guard = threading.Lock()

ICON_ALIASES = {
    'githubstatus.com': 'github.com',
    'www.githubstatus.com': 'github.com',
    'discordstatus.com': 'discord.com',
    'www.discordstatus.com': 'discord.com',
    'vercel-status.com': 'vercel.com',
    'www.vercel-status.com': 'vercel.com',
    'cloudflarestatus.com': 'cloudflare.com',
    'www.cloudflarestatus.com': 'cloudflare.com',
    'status.atlassian.com': 'atlassian.com',
}

def public_icon_domain(value):
    if not isinstance(value, str):
        return None
    domain = value.strip().rstrip('.').lower()
    if not domain or len(domain) > 253 or any(ch.isspace() for ch in domain):
        return None
    try:
        domain = domain.encode('idna').decode('ascii')
        ipaddress.ip_address(domain)
        return None  # dla adresu IP pozostaw inicjał kafelka
    except UnicodeError:
        return None
    except ValueError:
        pass
    labels = domain.split('.')
    if len(labels) < 2 or len(labels[-1]) < 2:
        return None
    if labels[-1] in ('local', 'localhost', 'lan', 'internal', 'home', 'test', 'invalid', 'example'):
        return None
    if any(len(label) > 63 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', label) for label in labels):
        return None
    return domain


def normalize_icon_domain(value):
    """Pusty wpis = ikona automatyczna; niepusty = publiczna domena zastępcza."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return ''
    if '://' in value:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme not in ('http', 'https') or parsed.username is not None or
                    parsed.password is not None or parsed.port is not None or
                    parsed.path not in ('', '/') or parsed.query or parsed.fragment):
                return None
            value = parsed.hostname or ''
        except ValueError:
            return None
    return public_icon_domain(value)


def service_icon_domain(service):
    custom = service.get('icon_domain', '')
    if custom:
        return public_icon_domain(custom)
    try:
        host = urlsplit(service['url']).hostname
    except ValueError:
        return None
    if host:
        host = ICON_ALIASES.get(host.lower(), host)
    return public_icon_domain(host)


def icon_url_for_service(service):
    domain = service_icon_domain(service)
    if not domain:
        return ''
    # Nowy adres zdjęcia po zmianie URL / domeny ikony, bez resetowania HTML.
    version = hashlib.sha256(domain.encode('ascii')).hexdigest()[:12]
    return url_for('service_icon', service_id=service['id'], v=version)


def icon_lock_for(domain):
    with _icon_locks_guard:
        return _icon_locks.setdefault(domain, threading.Lock())


def icon_mimetype(header):
    """Akceptuje wyłącznie znane formaty rastrowe, bez SVG i HTML."""
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if header.startswith(b'\x00\x00\x01\x00'):
        return 'image/x-icon'
    if header.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return 'image/webp'
    return None


def fetch_favicon(source_url, allowed_domains, params=None):
    """Pobieranie z bezpiecznymi przekierowaniami tylko do dostawcy ikon."""
    url = source_url
    for step in range(5):
        with requests.get(url, params=params, timeout=(3, 5),
                          stream=True, allow_redirects=False) as response:
            if 300 <= response.status_code < 400:
                location = response.headers.get('Location')
                if not location:
                    raise ValueError('Przekierowanie bez adresu Location')
                next_url = urljoin(response.url, location)
                parsed = urlsplit(next_url)
                hostname = (parsed.hostname or '').lower()
                if (parsed.scheme != 'https' or parsed.username is not None or
                        parsed.password is not None or parsed.port not in (None, 443) or
                        not any(hostname == base or hostname.endswith('.' + base)
                                for base in allowed_domains)):
                    raise ValueError('Przekierowanie do niedozwolonej domeny')
                url = next_url
                params = None
                continue
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(8192):
                data.extend(chunk)
                if len(data) > ICON_MAX_BYTES:
                    raise ValueError('Za duży plik ikony')
            blob = bytes(data)
            if not icon_mimetype(blob[:16]):
                raise ValueError('Serwer nie zwrócił PNG/ICO/JPEG/WEBP')
            return blob
    raise ValueError('Zbyt wiele przekierowań')


def get_cached_icon(domain):
    """Cache na 7 dni; po niepowodzeniu ponowna próba za 10 minut.

    Dane domeny trafiają wyłącznie do usług ikon (nie do hostów wskazanych
    przez monitorowany URL). Stare obrazy są dostępne przy problemach z siecią.
    """
    key = hashlib.sha256(domain.encode('ascii')).hexdigest()
    target = os.path.join(ICON_CACHE_DIR, key + '.img')
    legacy_target = os.path.join(ICON_CACHE_DIR, key + '.png')
    failure = os.path.join(ICON_CACHE_DIR, key + '.failed-v2')
    now = time.time()
    for cached in (target, legacy_target):
        if os.path.isfile(cached) and now - os.path.getmtime(cached) < ICON_CACHE_SECONDS:
            return cached

    with icon_lock_for(domain):
        now = time.time()
        for cached in (target, legacy_target):
            if os.path.isfile(cached) and now - os.path.getmtime(cached) < ICON_CACHE_SECONDS:
                return cached
        fallback = target if os.path.isfile(target) else (legacy_target if os.path.isfile(legacy_target) else None)
        if os.path.isfile(failure) and now - os.path.getmtime(failure) < ICON_FAILURE_SECONDS:
            return fallback

        providers = (
            ('Google', 'https://www.google.com/s2/favicons',
             ('google.com', 'gstatic.com', 'googleusercontent.com'), {'domain': domain, 'sz': 64}),
            ('DuckDuckGo', 'https://icons.duckduckgo.com/ip3/' + domain + '.ico',
             ('duckduckgo.com',), None),
        )
        errors = []
        for name, url, hosts, params in providers:
            try:
                blob = fetch_favicon(url, hosts, params)
                os.makedirs(ICON_CACHE_DIR, exist_ok=True)
                fd, temp = tempfile.mkstemp(prefix='.favicon-', suffix='.img', dir=ICON_CACHE_DIR)
                try:
                    with os.fdopen(fd, 'wb') as f:
                        f.write(blob)
                    os.replace(temp, target)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
                if os.path.isfile(failure):
                    os.unlink(failure)
                return target
            except (requests.RequestException, OSError, ValueError) as exc:
                errors.append(f'{name}: {exc}')

        app.logger.warning('Nie pobrano ikony %s (%s)', domain, '; '.join(errors))
        try:
            os.makedirs(ICON_CACHE_DIR, exist_ok=True)
            with open(failure, 'wb') as f:
                f.write(b'')
        except OSError:
            pass
        return fallback


@app.route('/api/services/<int:service_id>/icon', methods=['GET'])
def service_icon(service_id):
    with db_connect() as conn:
        row = conn.execute('SELECT url, icon_domain FROM services WHERE id=?', (service_id,)).fetchone()
    if row is None:
        return jsonify({'error': 'Nie znaleziono usługi o tym ID'}), 404
    domain = service_icon_domain(dict(row))
    if domain is None:
        return jsonify({'error': 'Brak publicznej domeny ikony (np. lokalny adres IP)'}), 404
    icon = get_cached_icon(domain)
    if not icon:
        response = jsonify({'error': 'Nie udało się pobrać ikony; sprawdź terminal Flaska'})
        response.status_code = 404
        response.headers['Cache-Control'] = 'no-store'
        return response
    try:
        with open(icon, 'rb') as f:
            mimetype = icon_mimetype(f.read(16))
    except OSError:
        mimetype = None
    if not mimetype:
        return jsonify({'error': 'Uszkodzony plik ikony w cache'}), 404
    response = send_file(icon, mimetype=mimetype, conditional=True)
    response.headers['Cache-Control'] = 'public, max-age=86400'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

monitor_wakeup = threading.Event()
CHECK_INTERVAL = 30
HISTORY_DAYS = 30
HISTORY_PERIODS = {"24h": (86400, 1800), "7d": (7 * 86400, 4 * 3600), "30d": (30 * 86400, 12 * 3600)}

DEFAULT_THEME = {'mode': 'dark', 'base': 'dark', 'custom': {
    '--bg': '#090e18', '--panel': '#111a29', '--green': '#37e2ad',
    '--text': '#f0f5fc', '--border': '#273347', '--amber': '#ffc36b',
    '--red': '#ff7188', '--blue': '#7daeff'
}}
ALLOWED_COLORS = set(DEFAULT_THEME['custom'])


def validate_theme(data):
    if not isinstance(data, dict):
        return None
    mode = data.get('mode')
    base = data.get('base')
    if mode not in ('dark', 'light', 'custom') or base not in ('dark', 'light'):
        return None
    custom = data.get('custom')
    if not isinstance(custom, dict) or set(custom) != ALLOWED_COLORS:
        return None
    if any(not isinstance(v, str) or len(v) != 7 or v[0] != '#' or
           any(ch not in '0123456789abcdefABCDEF' for ch in v[1:])
           for v in custom.values()):
        return None
    return {'mode': mode, 'base': base, 'custom': custom}


def get_theme():
    with theme_lock:
        try:
            with open(THEME_PATH, 'r', encoding='utf-8') as f:
                data = validate_theme(json.load(f))
                return data if data else json.loads(json.dumps(DEFAULT_THEME))
        except (OSError, ValueError, TypeError):
            return json.loads(json.dumps(DEFAULT_THEME))


def set_theme(data):
    with theme_lock:
        fd, temp_path = tempfile.mkstemp(prefix='.theme-', suffix='.json', dir=basedir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, THEME_PATH)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                type TEXT NOT NULL,
                status TEXT DEFAULT 'Nieznany',
                response_time REAL DEFAULT 0,
                last_checked TEXT
            )
        ''')
        cols = {row[1] for row in conn.execute('PRAGMA table_info(services)')}
        if 'display_order' not in cols:
            conn.execute('ALTER TABLE services ADD COLUMN display_order INTEGER')
        conn.execute('UPDATE services SET display_order = id WHERE display_order IS NULL')
        if 'icon_domain' not in cols:
            conn.execute("ALTER TABLE services ADD COLUMN icon_domain TEXT NOT NULL DEFAULT ''")
        conn.execute('''
            CREATE TABLE IF NOT EXISTS service_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL,
                checked_at INTEGER NOT NULL,
                status TEXT NOT NULL,
                health TEXT NOT NULL,
                response_time INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(service_id) REFERENCES services(id) ON DELETE CASCADE
            )
        ''')
        conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_checks_service_time
            ON service_checks(service_id, checked_at)
        ''')


def health_of(status):
    if status in ('UP', 'operational'):
        return 'up'
    if status == 'DOWN' or (isinstance(status, str) and status.startswith('HTTP ')):
        return 'down'
    return 'other'


def prune_history():
    # Retencja: zachowujemy ostatnie 30 dni pomiarów, by nie zapełnić karty SD.
    cutoff = int(time.time()) - HISTORY_DAYS * 86400
    with db_connect() as conn:
        conn.execute('DELETE FROM service_checks WHERE checked_at < ?', (cutoff,))


def background_monitor():
    last_prune = 0
    while True:
        start = time.monotonic()
        try:
            if time.monotonic() - last_prune > 3600:
                prune_history()
                last_prune = time.monotonic()
            run_checks()
        except Exception:
            app.logger.exception('Błąd podczas monitorowania w tle')
        # Kontynuuj co 30 s, ale pozwól na natychmiastowy test po dodaniu usługi.
        monitor_wakeup.wait(max(0, CHECK_INTERVAL - (time.monotonic() - start)))
        monitor_wakeup.clear()


@app.route('/')
def index():
    with db_connect() as conn:
        rows = conn.execute(
            'SELECT * FROM services ORDER BY display_order ASC, id ASC'
        ).fetchall()
    services = []
    for row in rows:
        service = dict(row)
        service['icon_url'] = icon_url_for_service(service)
        services.append(service)
    response = app.make_response(render_template(
        'index.html', services=services, local_ip=get_local_ip(), theme=get_theme()
    ))
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


@app.route('/api/services', methods=['GET'])
def services_api():
    """Żywe dane do kafelków, bez przeładowywania strony i formularza."""
    with db_connect() as conn:
        services = conn.execute('''
            SELECT id, name, url, type, status, response_time, last_checked,
                   display_order, icon_domain FROM services ORDER BY display_order ASC, id ASC
        ''').fetchall()
    items = []
    for s in services:
        item = dict(s)
        item['health'] = health_of(s['status'])
        item['icon_url'] = icon_url_for_service(item)
        items.append(item)
    response = jsonify({'services': items, 'theme': get_theme()})
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


@app.route('/api/services/check', methods=['POST'])
def request_check():
    """Wymusza nową serię testów w wątku monitorującym."""
    monitor_wakeup.set()
    return jsonify({'ok': True, 'message': 'Zlecono sprawdzenie usług'})


@app.route('/api/theme', methods=['GET', 'POST'])
def theme_api():
    if request.method == 'GET':
        response = jsonify(get_theme())
        response.headers['Cache-Control'] = 'no-store'
        return response
    data = validate_theme(request.get_json(silent=True))
    if data is None:
        return jsonify({'error': 'Nieprawidłowe ustawienia motywu'}), 400
    try:
        set_theme(data)
    except OSError:
        app.logger.exception('Nie udało się zapisać motywu')
        return jsonify({'error': 'Nie udało się zapisać ustawień na serwerze'}), 500
    response = jsonify(data)
    response.headers['Cache-Control'] = 'no-store'
    return response


def normalize_service_url(value):
    """Uzupełnia https:// dla krótkich adresów, np. youtube.com.

    Pozostawia jawnie podane http:// (np. dla lokalnych urządzeń).
    """
    raw = (value or '').strip()
    if (not raw or len(raw) > 2048 or
            any(ch.isspace() or ord(ch) < 32 for ch in raw)):
        return None

    if '://' not in raw:
        raw = 'https://' + raw

    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in ('http', 'https'):
            return None
        if not parsed.netloc or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port
        return raw
    except ValueError:
        return None


@app.route('/add', methods=['POST'])
def add_service():
    name = (request.form.get('name') or '').strip()
    url = normalize_service_url(request.form.get('url'))
    service_type = request.form.get('type', 'http')
    if name and len(name) <= 120 and url and service_type in ('http', 'statuspage'):
        with db_connect() as conn:
            next_order = conn.execute('SELECT COALESCE(MAX(display_order), 0) + 1 FROM services').fetchone()[0]
            conn.execute('''
                INSERT INTO services (name, url, type, status, last_checked, display_order)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, url, service_type, 'Nieznany', 'Oczekiwanie na test...', next_order))
        monitor_wakeup.set()
    else:
        return 'Niepoprawny adres serwisu lub brak wymaganych danych.', 400
    return redirect(url_for('index'))


@app.route('/api/services/<int:service_id>/edit', methods=['POST'])
def edit_service(service_id):
    "Edycja bez zmiany identyfikatora lub kolejności kafelka."
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Oczekiwano danych JSON'}), 400
    name = data.get('name')
    raw_url = data.get('url')
    service_type = data.get('type')
    if not isinstance(name, str) or not isinstance(raw_url, str) or not isinstance(service_type, str):
        return jsonify({'error': 'Niepoprawny format danych'}), 400
    name = name.strip()
    url = normalize_service_url(raw_url)
    icon_domain = normalize_icon_domain(data.get('icon_domain', ''))
    if not name or len(name) > 120 or not url or service_type not in ('http', 'statuspage'):
        return jsonify({'error': 'Podaj poprawną nazwę, adres i rodzaj testu'}), 400
    if icon_domain is None:
        return jsonify({'error': 'Podaj samą domenę ikony, np. github.com, albo zostaw pole puste'}), 400
    with db_connect() as conn:
        old = conn.execute('SELECT url, type, icon_domain FROM services WHERE id = ?', (service_id,)).fetchone()
        if old is None:
            return jsonify({'error': 'Nie znaleziono usługi'}), 404
        endpoint_changed = old['url'] != url or old['type'] != service_type
        if endpoint_changed:
            # Dane poprzedniego endpointu nie mogą zafałszować statystyk nowego.
            conn.execute('DELETE FROM service_checks WHERE service_id = ?', (service_id,))
            conn.execute('''UPDATE services
                         SET name=?, url=?, type=?, icon_domain=?, status='Nieznany',
                             response_time=0, last_checked='Oczekiwanie na test...'
                         WHERE id=?''', (name, url, service_type, icon_domain, service_id))
        else:
            conn.execute('UPDATE services SET name=?, icon_domain=? WHERE id=?', (name, icon_domain, service_id))
    if endpoint_changed:
        monitor_wakeup.set()
    return jsonify({'ok': True, 'history_reset': endpoint_changed,
                    'icon_changed': old['icon_domain'] != icon_domain})


@app.route('/api/services/<int:service_id>/history', methods=['GET'])
def service_history(service_id):
    "Agregaty i awarie z okresu, obliczone po stronie Flask."
    period = request.args.get('period', '24h')
    if period not in HISTORY_PERIODS:
        return jsonify({'error': 'Niepoprawny okres'}), 400
    duration, bucket_seconds = HISTORY_PERIODS[period]
    now = int(time.time())
    start = now - duration
    with db_connect() as conn:
        service = conn.execute('SELECT id, name FROM services WHERE id=?', (service_id,)).fetchone()
        if service is None:
            return jsonify({'error': 'Nie znaleziono usługi'}), 404
        rows = conn.execute('''
            SELECT checked_at, health, status, response_time
            FROM service_checks
            WHERE service_id=? AND checked_at>=?
            ORDER BY checked_at ASC, id ASC
        ''', (service_id, start)).fetchall()

    total = len(rows)
    up_count = 0
    healthy_latency_sum = 0
    buckets = {}
    incidents = []
    current_down = None
    for row in rows:
        ts = row['checked_at']
        health = row['health']
        bucket = min((ts - start) // bucket_seconds, (duration - 1) // bucket_seconds)
        summary = buckets.setdefault(bucket, {'count': 0, 'up': 0, 'down': 0, 'warn': 0, 'latency_sum': 0})
        summary['count'] += 1
        if health == 'up':
            up_count += 1
            healthy_latency_sum += row['response_time']
            summary['up'] += 1
            summary['latency_sum'] += row['response_time']
        elif health == 'down':
            summary['down'] += 1
        else:
            summary['warn'] += 1
        if health == 'down' and current_down is None:
            current_down = {'started_at': ts, 'ended_at': None, 'status': row['status']}
        elif health != 'down' and current_down is not None:
            current_down['ended_at'] = ts
            incidents.append(current_down)
            current_down = None
    if current_down is not None:
        incidents.append(current_down)
    points = []
    bucket_count = duration // bucket_seconds
    for idx in range(bucket_count):
        item = buckets.get(idx)
        date = datetime.fromtimestamp(start + idx * bucket_seconds)
        label = date.strftime('%H:%M') if period == '24h' else date.strftime('%d.%m %H:%M' if period == '7d' else '%d.%m')
        points.append({
            'label': label,
            'availability': round(item['up'] * 100 / item['count'], 1) if item else None,
            'response_ms': round(item['latency_sum'] / item['up']) if item and item['up'] else None,
            'samples': item['count'] if item else 0,
            'state': ('down' if item['down'] else 'other' if item['warn'] else 'up') if item else 'none'
        })
    def fmt(ts):
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts is not None else None
    last_incidents = list(reversed(incidents[-10:]))
    for item in last_incidents:
        item['started_at'] = fmt(item['started_at'])
        item['ended_at'] = fmt(item['ended_at'])
    response = jsonify({
        'id': service_id, 'name': service['name'], 'period': period,
        'measurements': total,
        'uptime_percent': round(up_count * 100 / total, 2) if total else None,
        'average_response_ms': round(healthy_latency_sum / up_count) if up_count else None,
        'outage_count': len(incidents),
        'incidents': last_incidents,
        'points': points,
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/services/order', methods=['POST'])
def reorder_services():
    payload = request.get_json(silent=True)
    ids = payload.get('ids') if isinstance(payload, dict) else None
    if not isinstance(ids, list) or any(type(x) is not int or x < 1 for x in ids) or len(ids) != len(set(ids)):
        return jsonify({'error': 'Niepoprawna lista identyfikatorów'}), 400
    conn = db_connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = {row[0] for row in conn.execute('SELECT id FROM services')}
        if set(ids) != existing:
            conn.rollback()
            return jsonify({'error': 'Lista usług się zmieniła. Odśwież stronę.'}), 409
        conn.executemany('UPDATE services SET display_order = ? WHERE id = ?',
                         [(position, service_id) for position, service_id in enumerate(ids, 1)])
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        app.logger.exception('Nie udało się zapisać kolejności')
        return jsonify({'error': 'Błąd zapisu kolejności'}), 500
    finally:
        conn.close()
    response = jsonify({'ok': True})
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/delete/<int:service_id>', methods=['POST'])
def delete_service(service_id):
    with db_connect() as conn:
        conn.execute('DELETE FROM services WHERE id = ?', (service_id,))
    return redirect(url_for('index'))


def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'


if __name__ == '__main__':
    init_db()
    threading.Thread(target=background_monitor, daemon=True, name='status-monitor').start()
    app.run(host='0.0.0.0', port=5000, debug=False)
