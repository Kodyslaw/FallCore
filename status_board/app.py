import sqlite3
import threading
import time
from flask import Flask, render_template, request, redirect, url_for
import requests

app = Flask (__name__)
DB_NAME = 'database.db'

# to do: 
# 1.inicjalizacja bazy danych sqlite i dodanie kilku serwisów dla testu, aby później podmienić to pod jakąś dynamiczną bazę
# 2. funkcja sprawdzająca statusy serwisów w tle
# 3. funkcja przekazująca dane statusu do index.html
# 4. funkcja aktualizująca 'ulubione' serwisy
# 5. dodanie formularza konfiguracyjnego w widoku wertykalnym (smartfon) 