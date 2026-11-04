import sys

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from .mainwindow import MainWindow


class App(QApplication):
    """Принимает файлы из Finder: «Открыть с помощью» и бросок на иконку в Dock.

    macOS передаёт их событием FileOpen, а не аргументами командной строки;
    событие может прийти раньше, чем создано окно.
    """

    def __init__(self, argv):
        super().__init__(argv)
        self.window: MainWindow | None = None
        self.pending: list[str] = []

    def event(self, e):
        if e.type() == QEvent.FileOpen and e.file():
            if self.window is not None:
                self.window.open_with_confirm(e.file())
            else:
                self.pending.append(e.file())
            return True
        return super().event(e)


def main():
    app = App(sys.argv)
    app.setApplicationName("PDF Editor")
    app.setApplicationDisplayName("PDF Editor")
    win = MainWindow()
    app.window = win
    win.show()
    files = [a for a in sys.argv[1:] if a.lower().endswith(".pdf")] + app.pending
    if files:
        win.open(files[-1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
