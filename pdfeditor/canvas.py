"""Холст страницы: отрисованная страница + интерактивные рамки строк.

Координаты сцены совпадают с координатами PDF в пунктах, масштаб
задаётся трансформацией вида. Сама страница рендерится в растр под
текущий масштаб, чтобы текст оставался чётким.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QKeyEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsObject, QGraphicsPixmapItem,
                               QGraphicsProxyWidget, QGraphicsScene, QGraphicsView, QLineEdit)

from .document import PdfDocument
from .fonts import strip_subset

ACCENT = QColor(37, 99, 235)
EDITED = QColor(234, 88, 12)


def pixmap_to_qimage(pix) -> QImage:
    fmt = QImage.Format_RGB888 if pix.n == 3 else QImage.Format_RGBA8888
    return QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()


class LineItem(QGraphicsObject):
    """Рамка одной строки текста. Позиция item = смещение строки (dx, dy)."""

    def __init__(self, view: "PageView", line_id: str):
        super().__init__()
        self.view = view
        self.line_id = line_id
        self._rect = QRectF()
        self._hover = False
        self._snapshot: QImage | None = None
        self.setFlags(QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemIsMovable
                      | QGraphicsItem.ItemIsFocusable)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.IBeamCursor)
        self.sync()

    @property
    def doc(self) -> PdfDocument:
        return self.view.doc

    def sync(self):
        """Подтягивает позицию и размер из модели."""
        edit = self.doc.edit_for(self.line_id)
        r = self.doc.current_bbox(self.line_id)
        self.prepareGeometryChange()
        # рамка хранится в координатах «без смещения», смещение — это pos()
        self._rect = QRectF(r.x0 - edit.dx, r.y0 - edit.dy, r.width, r.height).adjusted(-1.5, -1, 1.5, 1)
        self.setPos(edit.dx, edit.dy)
        self.update()

    def boundingRect(self) -> QRectF:
        return self._rect.adjusted(-1, -1, 1, 1)

    def paint(self, p: QPainter, option, widget=None):
        edited = self.line_id in self.doc.edits
        if self._snapshot is not None:
            p.drawImage(self._rect, self._snapshot)
        if self.isSelected():
            p.setPen(QPen(ACCENT, 0))
            p.setBrush(QColor(37, 99, 235, 28))
            p.drawRect(self._rect)
        elif self._hover:
            p.setPen(QPen(ACCENT.lighter(140), 0, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self._rect)
        elif edited and self.view.show_edits:
            p.setPen(QPen(EDITED, 0, Qt.DotLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(self._rect)

    def hoverEnterEvent(self, e):
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, e):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        if e.button() == Qt.LeftButton:
            self.view.begin_drag()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        self.view.drag_snapshots()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        self.setCursor(Qt.IBeamCursor)
        self.view.end_drag()

    def mouseDoubleClickEvent(self, e):
        self.view.start_editing(self)


class InlineEditor(QLineEdit):
    def __init__(self, on_commit, on_cancel):
        super().__init__()
        self._on_commit, self._on_cancel = on_commit, on_cancel
        self._done = False
        self.setFrame(False)
        self.setStyleSheet("QLineEdit { background: rgba(255,255,255,235); color: black;"
                           " border: 1px solid #2563eb; padding: 0; }")

    def keyPressEvent(self, e: QKeyEvent):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.finish(True)
        elif e.key() == Qt.Key_Escape:
            self.finish(False)
        else:
            super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.finish(True)

    def finish(self, commit: bool):
        if self._done:
            return
        self._done = True
        (self._on_commit if commit else self._on_cancel)(self.text())


class PageView(QGraphicsView):
    linesMoved = Signal(list)                              # [(id, old dx, dy, new dx, dy)]
    lineTextChanged = Signal(str, str)                     # id, новый текст
    selectionInfo = Signal(str)
    zoomChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setBackgroundBrush(QColor(0xE5, 0xE7, 0xEB))
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.doc: PdfDocument | None = None
        self.page_no = 0
        self.zoom = 1.0
        self.show_edits = True
        self._page_item: QGraphicsPixmapItem | None = None
        self._page_image: QImage | None = None
        self._render_scale = 1.0
        self._items: dict[str, LineItem] = {}
        self._editor: QGraphicsProxyWidget | None = None
        self._drag_starts: dict[str, QPointF] = {}
        self._rerender = QTimer(self, singleShot=True, interval=120, timeout=self._render)
        self.scene().selectionChanged.connect(self._on_selection)

    # -- страница -----------------------------------------------------------

    def set_page(self, doc: PdfDocument, page_no: int):
        self.doc, self.page_no = doc, page_no
        self._close_editor()
        scene = self.scene()
        scene.clear()
        self._items.clear()
        rect = doc.page_rect(page_no)
        scene.setSceneRect(QRectF(-20, -20, rect.width + 40, rect.height + 40))
        shadow = scene.addRect(QRectF(2, 3, rect.width, rect.height), QPen(Qt.NoPen), QColor(0, 0, 0, 40))
        shadow.setZValue(-2)
        self._page_item = QGraphicsPixmapItem()
        self._page_item.setZValue(-1)
        self._page_item.setTransformationMode(Qt.SmoothTransformation)
        scene.addItem(self._page_item)
        self._render()
        for line in doc.lines(page_no):
            item = LineItem(self, line.id)
            scene.addItem(item)
            self._items[line.id] = item
        self.selectionInfo.emit("" if doc.is_editable_page(page_no)
                                else "Повёрнутая страница: редактирование пока не поддерживается")

    def refresh(self, line_ids=()):
        for lid in line_ids:
            if lid in self._items:
                self._items[lid].sync()
        self._render()
        self._on_selection()

    def _render(self):
        if self.doc is None:
            return
        scale = self.zoom * self.devicePixelRatioF()
        self._render_scale = scale
        self._page_image = pixmap_to_qimage(self.doc.render_page(self.page_no, scale))
        self._page_item.setPixmap(QPixmap.fromImage(self._page_image))
        self._page_item.setScale(1 / scale)

    def snapshot(self, scene_rect: QRectF) -> QImage | None:
        if self._page_image is None:
            return None
        s = self._render_scale
        return self._page_image.copy(int(scene_rect.x() * s), int(scene_rect.y() * s),
                                     int(scene_rect.width() * s) + 1, int(scene_rect.height() * s) + 1)

    # -- перетаскивание -----------------------------------------------------

    def begin_drag(self):
        self._drag_starts = {it.line_id: QPointF(it.pos()) for it in self.selected_items()}

    def drag_snapshots(self):
        for it in self.selected_items():
            if it._snapshot is None and it.line_id in self._drag_starts:
                it._snapshot = self.snapshot(it.mapRectToScene(it._rect))

    def end_drag(self):
        moves = []
        for lid, start in self._drag_starts.items():
            it = self._items[lid]
            it._snapshot = None
            it.update()
            if (it.pos() - start).manhattanLength() > 0.01:
                moves.append((lid, start.x(), start.y(), it.pos().x(), it.pos().y()))
        self._drag_starts = {}
        if moves:
            self.linesMoved.emit(moves)

    # -- масштаб ------------------------------------------------------------

    def set_zoom(self, zoom: float):
        zoom = max(0.1, min(zoom, 8.0))
        if abs(zoom - self.zoom) < 1e-6:
            return
        self.zoom = zoom
        self.resetTransform()
        self.scale(zoom, zoom)
        self._rerender.start()
        self.zoomChanged.emit(zoom)

    def fit_width(self):
        if self.doc is None:
            return
        avail = self.viewport().width() - 30
        self.set_zoom(avail / (self.doc.page_rect(self.page_no).width + 40))
        self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())

    def wheelEvent(self, e):
        if e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            factor = 1.0015 ** e.angleDelta().y()
            self.set_zoom(self.zoom * factor)
        else:
            super().wheelEvent(e)

    # -- выделение и клавиатура ---------------------------------------------

    def selected_items(self) -> list[LineItem]:
        return [i for i in self.scene().selectedItems() if isinstance(i, LineItem)]

    def _on_selection(self):
        sel = self.selected_items()
        if self.doc is None or len(sel) != 1:
            self.selectionInfo.emit(f"Выделено строк: {len(sel)}" if sel else "")
            return
        line = self.doc.line(sel[0].line_id)
        span = line.main_span
        info = (f"{strip_subset(span.font)} · {span.size:.1f} pt · "
                f"#{span.color:06X} · базовая линия y={line.chars[0].origin[1]:.1f}")
        used = self.doc.font_report.get(line.id)
        if used:
            info += f"  →  запись: {used}"
        self.selectionInfo.emit(info)

    def keyPressEvent(self, e: QKeyEvent):
        sel = self.selected_items()
        if sel and self._editor is None:
            step = 10.0 if e.modifiers() & Qt.ShiftModifier else 1.0
            moves = {Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0),
                     Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step)}
            if e.key() in moves:
                ddx, ddy = moves[e.key()]
                self.linesMoved.emit([(it.line_id, it.pos().x(), it.pos().y(),
                                       it.pos().x() + ddx, it.pos().y() + ddy) for it in sel])
                return
            if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_F2) and len(sel) == 1:
                self.start_editing(sel[0])
                return
            if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                for it in sel:
                    self.lineTextChanged.emit(it.line_id, "")
                return
        super().keyPressEvent(e)

    # -- редактирование текста ----------------------------------------------

    def start_editing(self, item: LineItem):
        self._close_editor()
        line = self.doc.line(item.line_id)
        span = line.main_span
        rect = item.mapRectToScene(item._rect)

        editor = InlineEditor(lambda text: self._commit(item.line_id, text), lambda _: self._close_editor())
        font = QFont(_qt_family(span.font))
        font.setPixelSize(max(1, round(span.size)))  # 1 px сцены = 1 pt PDF
        font.setBold(bool(span.flags & 16))
        font.setItalic(bool(span.flags & 2))
        editor.setFont(font)
        editor.setText(self.doc.current_text(item.line_id))
        proxy = self.scene().addWidget(editor)
        proxy.setZValue(10)
        proxy.setPos(rect.x(), rect.y())
        width = max(rect.width() + span.size * 4, editor.fontMetrics().horizontalAdvance(editor.text()) + span.size * 4)
        editor.resize(int(width), int(rect.height() + 2))
        self._editor = proxy
        editor.setFocus()
        editor.end(False)

    def _commit(self, line_id: str, text: str):
        self._close_editor()
        if text != self.doc.current_text(line_id):
            self.lineTextChanged.emit(line_id, text)

    def _close_editor(self):
        if self._editor is not None:
            proxy, self._editor = self._editor, None
            proxy.hide()
            proxy.deleteLater()  # удаляем после выхода из обработчика событий редактора
            self.setFocus()


def _qt_family(pdf_font: str) -> str:
    """'TimesNewRomanPS-BoldMT' → 'Times New Roman' — только для подсказки Qt."""
    import re
    name = strip_subset(pdf_font)
    name = re.split(r"[-,]", name)[0]
    name = re.sub(r"(PSMT|PS|MT)$", "", name)
    name = re.sub(r"\s+(Regular|Bold|Italic|Oblique)$", "", name)
    if " " not in name:
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return name
