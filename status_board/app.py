import sqlite3
import threading
import time
from flask import Flask, redirect, render_template, request, url_for
from monitor import run_checks

app = Flask(__name__)
DB_NAME = "database.db"

def init_db():
    """Inicjalizacja bazy danych SQLite i utworzenie tabeli serwisów"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            type TEXT NOT NULL,         -- 'http' lub 'statuspage'
            status TEXT DEFAULT 'Nieznany', -- 'UP', 'DOWN', itp.
            response_time REAL DEFAULT 0,  -- Czas odpowiedzi w ms
            last_checked TEXT          -- Data i czas ostatniego sprawdzenia
        )
    """)
    conn.commit()
    conn.close()

def background_monitor():
    """Wątek działający w tle, który cyklicznie odpytuje serwisy co 60 sekund"""
    while True:
        try:
            run_checks()
        except Exception as e:
            print(f"Błąd podczas monitorowania w tle: {e}")
        time.sleep(60)

@app.route("/")
def index():
    """Główny pulpit - wyświetla monitory z bazy danych"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    services = cursor.execute("SELECT * FROM services").fetchall()
    conn.close()
    return render_template("index.html", services=services)

@app.route("/add", methods=["POST"])
def add_service():
    """Dodawanie nowego serwisu do monitorowania"""
    name = request.form.get("name")
    url = request.form.get("url")
    service_type = request.form.get("type", "http")

    if name and url:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO services (name, url, type, last_checked) VALUES (?, ?, ?, ?)",
            (name, url, service_type, "Oczekiwanie na test..."),
        )
        conn.commit()
        conn.close()
        
        # Opcjonalnie: możemy od razu wykonać test dla nowo dodanego serwisu, 
        # żeby użytkownik od razu widział status bez czekania na pętlę w tle.
        try:
            run_checks()
        except Exception:
            pass

    return redirect(url_for("index"))

if __name__ == "__main__":
    init_db()
    
    # Uruchomienie monitora w osobnym wątku (daemon=True oznacza, że zamknie się razem z aplikacją)
    monitor_thread = threading.Thread(target=background_monitor, daemon=True)
    monitor_thread.start()
    
    # Uruchomienie serwera Flask w sieci lokalnej
    app.run(host="0.0.0.0", port=5000, debug=True)

# to do: 
# 1.inicjalizacja bazy danych sqlite i dodanie kilku serwisów dla testu, aby później podmienić to pod jakąś dynamiczną bazę
# 2. funkcja sprawdzająca statusy serwisów w tle
# 3. funkcja przekazująca dane statusu do index.html
# 4. funkcja aktualizująca 'ulubione' serwisy
# 5. dodanie formularza konfiguracyjnego w widoku wertykalnym (smartfon) 