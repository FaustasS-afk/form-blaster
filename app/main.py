import sys
import os
import traceback
from pathlib import Path

# ── CRITICAL: Always run from the project directory ───────────────────────────
# This ensures data/, config/, and profiles/ are created INSIDE the project
# and not in whatever folder the user launched python from.
os.chdir(Path(__file__).parent)

# Ensure local imports work correctly when packaged
sys.path.append(os.path.abspath(os.path.dirname(__file__)))


def _install_crash_reporter():
    """
    Without this, an exception raised inside a Qt slot (e.g. Import TXT hitting a
    locked database) propagates out of the event loop and the whole window just
    VANISHES with no message. This makes the traceback visible and persists it.
    """
    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write("\n===== UNHANDLED ERROR =====\n" + text + "\n")
        sys.stderr.flush()
        try:
            with open(Path(__file__).parent / "crash.log", "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 60 + "\n")
                f.write(text)
        except Exception:
            pass
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None, "Unhandled Error",
                    text[-1500:] + "\n\n(also written to crash.log)"
                )
        except Exception:
            pass

    sys.excepthook = hook


_install_crash_reporter()


from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from data.db import init_db
from gui.main_window import MainWindow

def main():
    # Initialize SQLite schema
    init_db()
    
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    # Run the Main window
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
