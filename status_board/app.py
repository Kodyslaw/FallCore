import sqlite3
import threading
import time
import os
import socket
import json
import tempfile
from flask import Flask, redirect, render_template, request, url_for, jsonify
from monitor import run_checks

basedir = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(basedir, 'database.db')
THEME_PATH = os.path.join(basedir, 'theme.json')
app = Flask(__name__)
theme_lock = threading.RLock()

DEFAULT_THEME = {'mode': 'dark', 'base': 'dark', 'custom': {
    '--bg': '#090e18', '--panel': '#111a29', '--green': '#37e2ad',
    '--text': '#f0f5fc', '--border': '#273347', '--amber': '#ffc36b',
    '--red': '#ff7188', '--blue': '#7daeff'
}}
ALLOWED_COLORS = set(DEFAULT_THEME['custom'])


def validate_theme(data):
    """Weryfikuje schemat i kolory wysłane przez przeglądarkę."""
    if not isinstance(data, dict):
        return None
    mode = data.get('mode')
    base = data.get('base')
    if mode not in ('dark', 'light', 'custom') or base not in ('dark', 'light'):
        return None
    custom = data.get('custom')
    if not isinstance(custom, dict):
        return None
    if set(custom) != ALLOWED_COLORS:
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
    """Zapis atomowy: inne urządzenia nigdy nie odczytają połowy pliku."""
    with theme_lock:
        fd, temp_path = tempfile.mkstemp(prefix='.theme-', suffix='.json', dir=basedir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, THEME_PATH)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
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
        # Migracja istniejącej bazy: pozycja kafelka bez utraty usług.
        cols = {row[1] for row in conn.execute('PRAGMA table_info(services)')}
        if 'display_order' not in cols:
            conn.execute('ALTER TABLE services ADD COLUMN display_order INTEGER')
        conn.execute('UPDATE services SET display_order = id WHERE display_order IS NULL')
        conn.commit()
    finally:
        conn.close()


def background_monitor():
    while True:
        try:
            run_checks()
        except Exception as e:
            print(f'Błąd podczas monitorowania w tle: {e}')
        time.sleep(60)


@app.route('/')
def index():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        services = conn.execute('SELECT * FROM services ORDER BY display_order ASC, id ASC').fetchall()
    finally:
        conn.close()
    response = app.make_response(render_template('index.html', services=services, local_ip=get_local_ip(), theme=get_theme()))
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


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


@app.route('/add', methods=['POST'])
def add_service():
    name = (request.form.get('name') or '').strip()
    url = (request.form.get('url') or '').strip()
    service_type = request.form.get('type', 'http')
    if name and url and service_type in ('http', 'statuspage'):
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                'INSERT INTO services (name, url, type, last_checked) VALUES (?, ?, ?, ?)',
                (name, url, service_type, 'Oczekiwanie na test...')
            )
            conn.commit()
        finally:
            conn.close()
        try:
            run_checks()
        except Exception as e:
            app.logger.warning('Test po dodaniu usługi nie powiódł się: %s', e)
    return redirect(url_for('index'))



@app.route('/api/services/order', methods=['POST'])
def reorder_services():
    """Zapisuje kolejność wszystkich kafelków; nie usuwa ani nie modyfikuje usług."""
    payload = request.get_json(silent=True)
    ids = payload.get('ids') if isinstance(payload, dict) else None
    if not isinstance(ids, list) or any(type(x) is not int or x < 1 for x in ids) or len(ids) != len(set(ids)):
        return jsonify({'error': 'Niepoprawna lista identyfikatorów'}), 400
    conn = sqlite3.connect(DB_PATH, timeout=15)
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
    """Usuwa tylko wskazaną usługę z bazy SQLite."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute('DELETE FROM services WHERE id = ?', (service_id,))
        conn.commit()
    finally:
        conn.close()
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
    threading.Thread(target=background_monitor, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
