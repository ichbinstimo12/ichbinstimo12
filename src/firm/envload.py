"""Einziger Ort, an dem .env gelesen wird.

Der Supervisor startet die Prozesse mit gefilterten Umgebungen (der Agenten-Prozess ohne Broker-Schluessel) und setzt
FIRM_NO_DOTENV=1. Dann darf ein Prozess die Schluessel auch nicht nachtraeglich aus der .env-Datei laden.
"""
import os


def load_env() -> None:
    if os.getenv("FIRM_NO_DOTENV") == "1":
        return
    from dotenv import load_dotenv
    load_dotenv()
