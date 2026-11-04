from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QSpinBox)

from .canvas import PageView, pixmap_to_qimage
from .document import LineEdit, PdfDocument

THUMB_ZOOM = 0.22
ZOOM_PRESETS = [50, 75, 100, 125, 150, 200, 300, 400]


class EditCommand(QUndoCommand):
    """Одна или несколько правок строк: хранит состояние до и после."""

    def __init__(self, window: "MainWindow", page_no: int, before: dict, after: dict, title: str):
        super().__init__(title)
        self.w, self.page_no = window, page_no
        self.before, self.after = before, after

    def _apply(self, state: dict):
        for lid, edit in state.items():
            self.w.doc.set_edit(lid, LineEdit(edit.text, edit.dx, edit.dy))
        self.w.on_page_changed(self.page_no, list(state))

    def redo(self):
        self._apply(self.after)

    def undo(self):
        self._apply(self.before)


class MainWindow(QMainWindow):
    HINT = "Двойной клик — правка текста · перетаскивание или стрелки (Shift — ×10) — сдвиг · Del — удалить строку"

    def __init__(self):
        super().__init__()
        self.doc: PdfDocument | None = None
        self.page_no = 0
        self.undo = QUndoStack(self)
        self.undo.cleanChanged.connect(lambda _: self._update_title())

        self.view = PageView(self)
        self.view.linesMoved.connect(self._on_lines_moved)
        self.view.lineTextChanged.connect(self._on_text_changed)
        self.view.selectionInfo.connect(lambda s: self.info.setText(s or self.HINT))
        self.view.zoomChanged.connect(self._on_zoom_changed)
        self.setCentralWidget(self.view)

        self.thumbs = QListWidget()
        self.thumbs.setViewMode(QListWidget.IconMode)
        self.thumbs.setFlow(QListWidget.TopToBottom)
        self.thumbs.setWrapping(False)
        self.thumbs.setMovement(QListWidget.Static)
        self.thumbs.setIconSize(QSize(130, 170))
        self.thumbs.setGridSize(QSize(150, 200))
        self.thumbs.setUniformItemSizes(True)
        self.thumbs.setFixedWidth(172)
        self.thumbs.currentRowChanged.connect(self.go_to_page)
        dock = QDockWidget("Страницы", self)
        dock.setWidget(self.thumbs)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.pages_dock = dock
        self._thumb_queue: list[int] = []
        self._thumb_timer = QTimer(self, interval=0, timeout=self._render_next_thumb)
        # QTimer.singleShot(0, callable) роняет PySide 6.12 на macOS — используем объект таймера
        self._fit_timer = QTimer(self, singleShot=True, interval=0, timeout=self.view.fit_width)

        self.info = QLabel()
        self.statusBar().addWidget(self.info, 1)
        self.page_label = QLabel()
        self.statusBar().addPermanentWidget(self.page_label)

        self._build_actions()
        self.setAcceptDrops(True)
        self.resize(1200, 860)
        self._update_title()
        self._update_enabled()

    # -- действия -----------------------------------------------------------

    def _act(self, text, slot, shortcut=None, short=None):
        a = QAction(text, self)
        if short:
            a.setIconText(short)  # компактная подпись для тулбара
        a.setToolTip(text)
        if shortcut:
            a.setShortcut(shortcut)
        a.triggered.connect(slot)
        return a

    def _build_actions(self):
        self.a_open = self._act("Открыть…", self.open_dialog, QKeySequence.Open)
        self.a_save = self._act("Сохранить", self.save, QKeySequence.Save)
        self.a_save_as = self._act("Сохранить как…", self.save_as, QKeySequence.SaveAs)
        self.a_undo = self.undo.createUndoAction(self, "Отменить")
        self.a_undo.setShortcut(QKeySequence.Undo)
        self.a_undo.setIconText("↶")
        self.a_redo = self.undo.createRedoAction(self, "Повторить")
        self.a_redo.setShortcut(QKeySequence.Redo)
        self.a_redo.setIconText("↷")
        self.a_zoom_in = self._act("Увеличить", lambda: self.view.set_zoom(self.view.zoom * 1.25), QKeySequence.ZoomIn, "+")
        self.a_zoom_out = self._act("Уменьшить", lambda: self.view.set_zoom(self.view.zoom / 1.25), QKeySequence.ZoomOut, "−")
        self.a_zoom_100 = self._act("100 %", lambda: self.view.set_zoom(1.0), "Ctrl+0")
        self.a_fit = self._act("По ширине", self.view.fit_width, "Ctrl+9", "↔")
        self.a_prev = self._act("Предыдущая страница", lambda: self.go_to_page(self.page_no - 1), "PgUp", "◀")
        self.a_next = self._act("Следующая страница", lambda: self.go_to_page(self.page_no + 1), "PgDown", "▶")
        self.a_edit = self._act("Редактировать строку", self._edit_selected, "F2")
        self.a_revert = self._act("Вернуть исходный вид строки", self._revert_selected, "Ctrl+Shift+R")
        self.a_marks = self._act("Подсвечивать изменённые строки", self._toggle_marks)
        self.a_marks.setCheckable(True)
        self.a_marks.setChecked(True)

        m = self.menuBar().addMenu("Файл")
        m.addActions([self.a_open, self.a_save, self.a_save_as])
        m = self.menuBar().addMenu("Правка")
        m.addActions([self.a_undo, self.a_redo])
        m.addSeparator()
        m.addActions([self.a_edit, self.a_revert])
        m = self.menuBar().addMenu("Вид")
        m.addActions([self.a_zoom_in, self.a_zoom_out, self.a_zoom_100, self.a_fit])
        m.addSeparator()
        m.addActions([self.a_prev, self.a_next, self.pages_dock.toggleViewAction(), self.a_marks])

        tb = self.addToolBar("Основная")
        tb.setMovable(False)
        tb.addActions([self.a_open, self.a_save])
        tb.addSeparator()
        tb.addActions([self.a_undo, self.a_redo])
        tb.addSeparator()
        tb.addAction(self.a_zoom_out)
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.addItems([f"{z} %" for z in ZOOM_PRESETS])
        self.zoom_box.setMinimumContentsLength(6)
        self.zoom_box.lineEdit().returnPressed.connect(self._zoom_from_box)
        self.zoom_box.textActivated.connect(lambda _: self._zoom_from_box())
        tb.addWidget(self.zoom_box)
        tb.addActions([self.a_zoom_in, self.a_fit])
        tb.addSeparator()
        tb.addAction(self.a_prev)
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(lambda v: self.go_to_page(v - 1))
        tb.addWidget(self.page_spin)
        tb.addAction(self.a_next)

    def _update_enabled(self):
        has = self.doc is not None
        for a in (self.a_save, self.a_save_as, self.a_zoom_in, self.a_zoom_out, self.a_zoom_100,
                  self.a_fit, self.a_prev, self.a_next, self.a_edit, self.a_revert):
            a.setEnabled(has)
        self.zoom_box.setEnabled(has)
        self.page_spin.setEnabled(has)

    def _update_title(self):
        name = os.path.basename(self.doc.path) if self.doc else "PDF Editor"
        dirty = self.doc is not None and not self.undo.isClean()
        self.setWindowTitle(("● " if dirty else "") + name)

    # -- файлы --------------------------------------------------------------

    def open_dialog(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Открыть PDF", "", "PDF (*.pdf)")
        if path:
            self.open(path)

    def open_with_confirm(self, path: str):
        if self._confirm_discard():
            self.open(path)
        self.raise_()
        self.activateWindow()

    def open(self, path: str):
        try:
            doc = PdfDocument(path)
        except Exception as e:
            QMessageBox.critical(self, "Не удалось открыть", f"{path}\n\n{e}")
            return
        self.doc = doc
        self.undo.clear()
        self.page_no = -1
        self.thumbs.blockSignals(True)
        self.thumbs.clear()
        placeholder = QPixmap(self.thumbs.iconSize())
        placeholder.fill(Qt.white)
        for i in range(doc.page_count):
            it = QListWidgetItem(QIcon(placeholder), str(i + 1))
            it.setTextAlignment(Qt.AlignHCenter)
            self.thumbs.addItem(it)
        self.thumbs.blockSignals(False)
        self._thumb_queue = list(range(doc.page_count))
        self._thumb_timer.start()
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(doc.page_count)
        self.page_spin.blockSignals(False)
        self.go_to_page(0)
        self._update_title()
        self._update_enabled()
        self._fit_timer.start()

    def save(self):
        if self.doc:
            self._save_to(self.doc.path)

    def save_as(self):
        if not self.doc:
            return
        base, _ = os.path.splitext(self.doc.path)
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить PDF", base + "_edited.pdf", "PDF (*.pdf)")
        if path:
            self._save_to(path)

    def _save_to(self, path: str):
        tmp = path + ".tmp"
        try:
            self.doc.save(tmp)
            os.replace(tmp, path)
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            QMessageBox.critical(self, "Ошибка сохранения", str(e))
            return
        self.undo.setClean()
        self.statusBar().showMessage(f"Сохранено: {path}", 5000)

    def _confirm_discard(self) -> bool:
        if self.doc is None or self.undo.isClean():
            return True
        r = QMessageBox.question(self, "Несохранённые изменения", "Сохранить изменения перед закрытием?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            self.save()
            return self.undo.isClean()
        return r == QMessageBox.Discard

    def closeEvent(self, e):
        e.accept() if self._confirm_discard() else e.ignore()

    def dragEnterEvent(self, e):
        if any(u.toLocalFile().lower().endswith(".pdf") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.toLocalFile().lower().endswith(".pdf"):
                self.open_with_confirm(u.toLocalFile())
                break

    # -- страницы и миниатюры -----------------------------------------------

    def go_to_page(self, page_no: int):
        if self.doc is None or not (0 <= page_no < self.doc.page_count) or page_no == self.page_no:
            return
        self.page_no = page_no
        self.view.set_page(self.doc, page_no)
        for w, val in ((self.thumbs, page_no), (self.page_spin, page_no + 1)):
            w.blockSignals(True)
            w.setCurrentRow(val) if w is self.thumbs else w.setValue(val)
            w.blockSignals(False)
        self.page_label.setText(f"Страница {page_no + 1} из {self.doc.page_count}")

    def _render_next_thumb(self):
        if not self._thumb_queue or self.doc is None:
            self._thumb_timer.stop()
            return
        self._update_thumb(self._thumb_queue.pop(0))

    def _update_thumb(self, page_no: int):
        img = pixmap_to_qimage(self.doc.render_page(page_no, THUMB_ZOOM * self.devicePixelRatioF()))
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(self.devicePixelRatioF())
        self.thumbs.item(page_no).setIcon(QIcon(pm))

    def on_page_changed(self, page_no: int, line_ids: list[str]):
        if page_no != self.page_no:
            self.go_to_page(page_no)
        self.view.refresh(line_ids)
        self._update_thumb(page_no)

    # -- правки -------------------------------------------------------------

    def _push(self, changes: dict[str, LineEdit], title: str):
        before = {lid: self.doc.edit_for(lid) for lid in changes}
        self.undo.push(EditCommand(self, self.page_no, before, changes, title))

    def _on_lines_moved(self, moves: list):
        changes = {}
        for lid, ox, oy, nx, ny in moves:
            cur = self.doc.edit_for(lid)
            changes[lid] = LineEdit(cur.text, nx, ny)
        self._push(changes, "Перемещение")

    def _on_text_changed(self, line_id: str, text: str):
        cur = self.doc.edit_for(line_id)
        self._push({line_id: LineEdit(text, cur.dx, cur.dy)}, "Удаление строки" if text == "" else "Правка текста")

    def _edit_selected(self):
        sel = self.view.selected_items()
        if len(sel) == 1:
            self.view.start_editing(sel[0])

    def _revert_selected(self):
        sel = self.view.selected_items()
        if sel:
            self._push({it.line_id: LineEdit() for it in sel}, "Возврат к исходному")

    def _toggle_marks(self, on: bool):
        self.view.show_edits = on
        self.view.scene().update()

    # -- масштаб ------------------------------------------------------------

    def _on_zoom_changed(self, zoom: float):
        self.zoom_box.lineEdit().setText(f"{round(zoom * 100)} %")

    def _zoom_from_box(self):
        txt = self.zoom_box.currentText().replace("%", "").strip()
        try:
            self.view.set_zoom(float(txt) / 100)
        except ValueError:
            self._on_zoom_changed(self.view.zoom)
