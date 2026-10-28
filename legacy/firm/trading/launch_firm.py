import os
import subprocess
import sys
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LAUNCHER")

def launch_company():
    """
    ULTIMATE FIX: Uses a hardcoded path detection to stop the 'trading/trading' bug.
    """
    logger.info("🚀 INITIALIZING QUANT FIRM BOOT SEQUENCE...")

    # 1. Get the absolute path of this current file (launch_firm.py)
    # This is the only way to be 100% sure where we are on the disk.
    script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)

    logger.info(f"Launcher detected its own location at: {script_dir}")

    # 2. Define paths relative to the script's location
    # Since launch_firm.py is in C:\Users\timo\trading\
    # and main.py is in C:\Users\timo\trading\firm\main.py
    # The correct path is just 'firm/main.py' relative to script_dir
    api_path = os.path.join(script_dir, "firm", "dashboard_api.py")
    main_path = os.path.join(script_dir, "firm", "main.py")

    logger.info(f"Targeting API: {api_path}")
    logger.info(f"Targeting Main: {main_path}")

    # VERIFICATION: Check if the files actually exist before trying to run them
    if not os.path.exists(main_path):
        logger.error(f"FILE NOT FOUND: {main_path}")
        # Try one level up just in case
        alt_main = os.path.join(script_dir, "..", "trading", "firm", "main.py")
        if os.path.exists(alt_main):
            main_path = alt_main
            logger.info(f"Found main.py at alternative path: {main_path}")
        else:
            logger.error("Could not find main.py anywhere. Please check your folder structure.")
            return

    # 3. Start the Office Dashboard API
    logger.info("Starting the Office Dashboard API on port 8000...")
    try:
        api_process = subprocess.Popen(
            [sys.executable, api_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
    except Exception as e:
        logger.error(f"Failed to launch the Office: {e}")
        return

    # 4. Start the Trading Engine
    logger.info("Launching the Trading Engine and hiring agents...")
    try:
        # We use the absolute path we just verified
        subprocess.run([sys.executable, main_path])
    except KeyboardInterrupt:
        logger.info("Shutting down the firm...")
    finally:
        api_process.terminate()
        logger.info("Company closed.")

if __name__ == "__main__":
    launch_company()
