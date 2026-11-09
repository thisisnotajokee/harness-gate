#!/usr/bin/env python3
"""Warstwa komunikatów bramki (polski + angielski).

Mikro-renderer tekstów z `messages.json`, który leży w tym samym katalogu co
ten moduł. Kontrakt dla runnera:

    render(lang, key, **params) -> str
    render_fallback(lang, key, fallback, **params) -> str

Zasady (R-UX):
  * nieznany język  -> angielski (`en`),
  * brak klucza     -> `render` zwraca nazwę klucza, `render_fallback` zwraca `fallback`,
  * czytelny plik   -> nigdy nie rzucamy wyjątku, nigdy nie zwracamy pustego tekstu,
  * brak parametru  -> w tekście pojawia się `?nazwa` (np. `?repo`),
  * zero zależności zewnętrznych (tylko biblioteka standardowa).
"""

import json
import os
import string

# Katalog tego modułu — `messages.json` czytamy względem niego, nie względem CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "messages.json")

# Język domyślny i zarazem awaryjny dla nieznanych kodów języków.
_DEFAULT_LANG = "en"

# Pamięć podręczna wczytanego słownika (klucz: kod języka -> {klucz: tekst}).
_CACHE = None


def _read_tables():
    """Wczytuje `messages.json` i zwraca mapę `{lang: {key: text}}`.

    Każdy błąd (brak pliku, zepsuty JSON, zła struktura) kończy się pustą mapą —
    warstwa wyżej obsłuży to jako "brak klucza", bez wyjątku.
    """
    try:
        with open(_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        langs = data.get("langs")
        if isinstance(langs, dict):
            return langs
    except Exception:
        pass
    return {}


def _tables():
    """Leniwie wczytuje i zapamiętuje słowniki komunikatów."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _read_tables()
    return _CACHE


def _resolve_lang(tables, lang):
    """Zamienia żądany kod języka na istniejący; nieznany -> język domyślny."""
    if isinstance(lang, str) and lang in tables:
        return lang
    return _DEFAULT_LANG if _DEFAULT_LANG in tables else None


class _SafeDict(dict):
    """Słownik parametrów, który dla brakujących nazw zwraca `?nazwa`."""

    def __missing__(self, key):
        return "?" + str(key)


def _fill(template, params):
    """Podstawia parametry do szablonu; brakujące daje `?nazwa`."""
    try:
        return string.Formatter().vformat(template, (), _SafeDict(params or {}))
    except Exception:
        # Zepsuty szablon (np. niedomknięty nawias) — oddajemy go bez zmian,
        # byle nie rzucić wyjątku i nie zwrócić pustego tekstu.
        return template


def _lookup(tables, lang, key):
    """Zwraca tekst dla (język, klucz) albo None, gdy go brak."""
    resolved = _resolve_lang(tables, lang)
    if resolved is None:
        return None
    table = tables.get(resolved)
    if not isinstance(table, dict):
        return None
    text = table.get(key)
    return text if isinstance(text, str) and text != "" else None


def render(lang, key, **params):
    """Zwraca komunikat dla języka i klucza. Nigdy nie rzuca wyjątku.

    Nieznany język -> `en`. Brak klucza -> zwraca samą nazwę klucza.
    """
    tables = _tables()
    try:
        text = _lookup(tables, lang, key)
        if text is None:
            return key
        return _fill(text, params)
    except Exception:
        return key


def render_fallback(lang, key, fallback, **params):
    """Jak `render`, ale przy braku klucza w danym języku zwraca `fallback`.

    Używane, gdy runner chce podstawić techniczne `summary` z checka zamiast
    brakującego komunikatu. Nigdy nie rzuca wyjątku, nigdy nie zwraca pustego.
    """
    tables = _tables()
    try:
        text = _lookup(tables, lang, key)
        if text is None:
            return fallback
        return _fill(text, params)
    except Exception:
        return fallback
