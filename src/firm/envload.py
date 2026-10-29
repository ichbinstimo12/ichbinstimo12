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


def use_os_trust_store() -> bool:
    """TLS-Pruefung gegen den Zertifikatsspeicher des Betriebssystems statt nur gegen Pythons eigene Liste.

    Antivirus-Programme (HTTPS-Scan) und Firmen-Proxys ersetzen Zertifikate durch ein eigenes, das nur im
    Windows-/macOS-Speicher als vertrauenswuerdig hinterlegt ist. Ohne das scheitert jede Verbindung mit
    CERTIFICATE_VERIFY_FAILED. Die Pruefung bleibt dabei voll aktiv (nichts wird abgeschaltet).
    Abschalten: FIRM_OS_TRUSTSTORE=0."""
    if os.getenv("FIRM_OS_TRUSTSTORE", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        import truststore
        truststore.inject_into_ssl()
        return True
    except Exception:           # not installed / unsupported platform -> Python's default verification
        return False
