"""
PDF Editor Desktop Application
A feature-rich desktop app for viewing and editing PDF files.
"""

import sys
import os
import fitz  # PyMuPDF
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QScrollArea, QLabel, QPushButton, QToolBar, QAction, QFileDialog,
    QSpinBox, QDoubleSpinBox, QColorDialog, QInputDialog, QMessageBox,
    QSplitter, QListWidget, QListWidgetItem, QDockWidget, QComboBox,
    QSlider, QStatusBar, QDialog, QFormLayout, QLineEdit, QDialogButtonBox,
    QFontDialog, QCheckBox, QGroupBox, QTextEdit, QTabWidget, QFrame,
    QSizePolicy, QToolButton, QMenu
)
from PyQt5.QtCore import (
    Qt, QPoint, QRect, QSize, QRectF, pyqtSignal, QPointF, QThread, pyqtSlot
)
from PyQt5.QtGui import (
    QPixmap, QImage, QPainter, QPen, QColor, QBrush, QFont, QIcon,
    QCursor, QPainterPath, QPolygonF, QKeySequence, QTransform
)


# ─── Tool Constants ───────────────────────────────────────────────────────────
TOOL_SELECT   = "select"
TOOL_TEXT     = "text"
TOOL_HIGHLIGHT = "highlight"
TOOL_DRAW     = "draw"
TOOL_RECT     = "rect"
TOOL_ELLIPSE  = "ellipse"
TOOL_LINE     = "line"
TOOL_ERASE    = "erase"


class Annotation:
    """Represents a single annotation on a PDF page."""
    def __init__(self, ann_type, page_num, rect=None, color=None,
                 text="", line_width=2, opacity=1.0):
        self.ann_type  = ann_type   # "text","highlight","rect","ellipse","line","draw"
        self.page_num  = page_num
        self.rect      = rect       # QRectF (PDF coords)
        self.color     = color or QColor(255, 255, 0)
        self.text      = text
        self.line_width = line_width
        self.opacity   = opacity
        self.points    = []         # for freehand draw / line


class UndoStack:
    def __init__(self, max_size=50):
        self._stack = []
        self._index = -1
        self._max   = max_size

    def push(self, state):
        # Discard redo history
        self._stack = self._stack[:self._index + 1]
        self._stack.append(state)
        if len(self._stack) > self._max:
            self._stack.pop(0)
        self._index = len(self._stack) - 1

    def undo(self):
        if self._index > 0:
            self._index -= 1
            return self._stack[self._index]
        return None

    def redo(self):
        if self._index < len(self._stack) - 1:
            self._index += 1
            return self._stack[self._index]
        return None

    def can_undo(self):
        return self._index > 0

    def can_redo(self):
        return self._index < len(self._stack) - 1


# ─── Page Canvas ─────────────────────────────────────────────────────────────
class PageCanvas(QLabel):
    """Widget that renders a single PDF page and handles annotation drawing."""

    annotation_added = pyqtSignal(object)  # emits Annotation

    def __init__(self, parent=None):
        super().__init__(parent)
        self.page_num      = 0
        self.zoom          = 1.5
        self._page_pixmap  = None   # rendered page (no overlays)
        self.annotations   = []     # List[Annotation] for this page
        self.current_tool  = TOOL_SELECT
        self.pen_color     = QColor(255, 0, 0)
        self.fill_color    = QColor(255, 255, 0, 100)
        self.line_width    = 2
        self.opacity       = 0.5
        self._drawing      = False
        self._start_pt     = QPoint()
        self._cur_pt       = QPoint()
        self._draw_pts     = []
        self._doc          = None

        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)
        self.setAlignment(Qt.AlignTop | Qt.AlignLeft)

    # ── public API ────────────────────────────────────────────────────────
    def set_document(self, doc):
        self._doc = doc

    def render_page(self, page_num, zoom=None):
        if zoom is not None:
            self.zoom = zoom
        self.page_num = page_num
        if self._doc is None:
            return
        page = self._doc[page_num]
        mat  = fitz.Matrix(self.zoom, self.zoom)
        clip = page.rect
        pix  = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        img  = QImage(pix.samples, pix.width, pix.height,
                      pix.stride, QImage.Format_RGB888)
        self._page_pixmap = QPixmap.fromImage(img)
        self._redraw()

    def set_annotations(self, annotations):
        self.annotations = annotations
        self._redraw()

    def set_tool(self, tool):
        self.current_tool = tool
        cursors = {
            TOOL_SELECT:    Qt.ArrowCursor,
            TOOL_TEXT:      Qt.IBeamCursor,
            TOOL_HIGHLIGHT: Qt.CrossCursor,
            TOOL_DRAW:      Qt.CrossCursor,
            TOOL_RECT:      Qt.CrossCursor,
            TOOL_ELLIPSE:   Qt.CrossCursor,
            TOOL_LINE:      Qt.CrossCursor,
            TOOL_ERASE:     Qt.ForbiddenCursor,
        }
        self.setCursor(cursors.get(tool, Qt.ArrowCursor))

    # ── coordinate helpers ────────────────────────────────────────────────
    def _widget_to_pdf(self, pt):
        """Convert widget pixel coords → PDF page coords."""
        if self._doc is None:
            return QPointF(pt)
        page = self._doc[self.page_num]
        pw   = page.rect.width
        ph   = page.rect.height
        if self._page_pixmap:
            sx = pw / self._page_pixmap.width()
            sy = ph / self._page_pixmap.height()
        else:
            sx = sy = 1.0
        return QPointF(pt.x() * sx, pt.y() * sy)

    def _pdf_to_widget(self, pt):
        """Convert PDF page coords → widget pixel coords."""
        if self._doc is None or self._page_pixmap is None:
            return QPointF(pt)
        page = self._doc[self.page_num]
        pw   = page.rect.width
        ph   = page.rect.height
        sx   = self._page_pixmap.width()  / pw
        sy   = self._page_pixmap.height() / ph
        return QPointF(pt.x() * sx, pt.y() * sy)

    # ── mouse events ──────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self._drawing  = True
        self._start_pt = event.pos()
        self._cur_pt   = event.pos()
        self._draw_pts = [event.pos()]

        if self.current_tool == TOOL_ERASE:
            self._erase_at(event.pos())

        if self.current_tool == TOOL_TEXT:
            self._add_text_annotation(event.pos())
            self._drawing = False

    def mouseMoveEvent(self, event):
        if not self._drawing:
            return
        self._cur_pt = event.pos()
        if self.current_tool == TOOL_DRAW:
            self._draw_pts.append(event.pos())
        if self.current_tool == TOOL_ERASE:
            self._erase_at(event.pos())
        self._redraw_with_preview()

    def mouseReleaseEvent(self, event):
        if not self._drawing or event.button() != Qt.LeftButton:
            return
        self._drawing = False

        if self.current_tool in (TOOL_RECT, TOOL_ELLIPSE,
                                 TOOL_HIGHLIGHT, TOOL_LINE, TOOL_DRAW):
            self._finalize_annotation()

    # ── annotation creation ───────────────────────────────────────────────
    def _finalize_annotation(self):
        tool = self.current_tool
        p1   = self._widget_to_pdf(self._start_pt)
        p2   = self._widget_to_pdf(self._cur_pt)
        rect = QRectF(p1, p2).normalized()

        if rect.width() < 2 and rect.height() < 2 and tool != TOOL_LINE:
            return

        if tool == TOOL_HIGHLIGHT:
            color = QColor(255, 255, 0, int(255 * self.opacity))
            ann   = Annotation(tool, self.page_num, rect, color,
                               line_width=0, opacity=self.opacity)
        elif tool in (TOOL_RECT, TOOL_ELLIPSE):
            color = QColor(self.pen_color)
            ann   = Annotation(tool, self.page_num, rect, color,
                               line_width=self.line_width)
            ann.fill_color = QColor(self.fill_color)
        elif tool == TOOL_LINE:
            color = QColor(self.pen_color)
            ann   = Annotation(tool, self.page_num, rect, color,
                               line_width=self.line_width)
            ann.points = [p1, p2]
        elif tool == TOOL_DRAW:
            color = QColor(self.pen_color)
            ann   = Annotation(tool, self.page_num, rect, color,
                               line_width=self.line_width)
            ann.points = [self._widget_to_pdf(p) for p in self._draw_pts]
        else:
            return

        self.annotations.append(ann)
        self.annotation_added.emit(ann)
        self._redraw()

    def _add_text_annotation(self, pos):
        text, ok = QInputDialog.getMultiLineText(
            self, "Add Text", "Enter annotation text:")
        if ok and text.strip():
            pdf_pt = self._widget_to_pdf(pos)
            rect   = QRectF(pdf_pt.x(), pdf_pt.y(),
                            200 / self.zoom, 40 / self.zoom)
            color  = QColor(self.pen_color)
            ann    = Annotation(TOOL_TEXT, self.page_num, rect, color,
                                text=text)
            self.annotations.append(ann)
            self.annotation_added.emit(ann)
            self._redraw()

    def _erase_at(self, pos):
        pdf_pt  = self._widget_to_pdf(pos)
        radius  = 10 / self.zoom
        erase_r = QRectF(pdf_pt.x() - radius, pdf_pt.y() - radius,
                         radius * 2, radius * 2)
        removed = []
        for ann in self.annotations:
            if ann.rect and ann.rect.intersects(erase_r):
                removed.append(ann)
            elif ann.ann_type == TOOL_DRAW:
                for pt in ann.points:
                    if erase_r.contains(pt):
                        removed.append(ann)
                        break
        for ann in removed:
            if ann in self.annotations:
                self.annotations.remove(ann)
        if removed:
            self._redraw()

    # ── painting ──────────────────────────────────────────────────────────
    def _redraw(self):
        if self._page_pixmap is None:
            return
        canvas = QPixmap(self._page_pixmap)
        p      = QPainter(canvas)
        p.setRenderHint(QPainter.Antialiasing)
        self._paint_annotations(p, self.annotations)
        p.end()
        self.setPixmap(canvas)
        self.resize(canvas.size())

    def _redraw_with_preview(self):
        if self._page_pixmap is None:
            return
        canvas = QPixmap(self._page_pixmap)
        p      = QPainter(canvas)
        p.setRenderHint(QPainter.Antialiasing)
        self._paint_annotations(p, self.annotations)
        # Draw live preview
        self._paint_preview(p)
        p.end()
        self.setPixmap(canvas)

    def _paint_annotations(self, painter, annotations):
        if self._doc is None or self._page_pixmap is None:
            return
        page = self._doc[self.page_num]
        pw   = page.rect.width
        ph   = page.rect.height
        sx   = self._page_pixmap.width()  / pw
        sy   = self._page_pixmap.height() / ph

        for ann in annotations:
            if ann.rect is None:
                continue
            r = QRectF(ann.rect.x() * sx, ann.rect.y() * sy,
                       ann.rect.width() * sx, ann.rect.height() * sy)

            if ann.ann_type == TOOL_HIGHLIGHT:
                c = QColor(ann.color)
                c.setAlphaF(ann.opacity)
                painter.fillRect(r, QBrush(c))

            elif ann.ann_type == TOOL_RECT:
                pen = QPen(ann.color, ann.line_width)
                painter.setPen(pen)
                fc  = getattr(ann, "fill_color", QColor(0, 0, 0, 0))
                painter.setBrush(QBrush(fc))
                painter.drawRect(r)
                painter.setBrush(Qt.NoBrush)

            elif ann.ann_type == TOOL_ELLIPSE:
                pen = QPen(ann.color, ann.line_width)
                painter.setPen(pen)
                fc  = getattr(ann, "fill_color", QColor(0, 0, 0, 0))
                painter.setBrush(QBrush(fc))
                painter.drawEllipse(r)
                painter.setBrush(Qt.NoBrush)

            elif ann.ann_type == TOOL_LINE and len(ann.points) == 2:
                pen = QPen(ann.color, ann.line_width)
                painter.setPen(pen)
                p1  = QPointF(ann.points[0].x() * sx, ann.points[0].y() * sy)
                p2  = QPointF(ann.points[1].x() * sx, ann.points[1].y() * sy)
                painter.drawLine(p1, p2)

            elif ann.ann_type == TOOL_DRAW and len(ann.points) > 1:
                pen = QPen(ann.color, ann.line_width)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                pts = [QPointF(pt.x() * sx, pt.y() * sy) for pt in ann.points]
                for i in range(len(pts) - 1):
                    painter.drawLine(pts[i], pts[i + 1])

            elif ann.ann_type == TOOL_TEXT:
                pen = QPen(ann.color)
                painter.setPen(pen)
                font = QFont("Arial", max(8, int(12 * self.zoom)))
                painter.setFont(font)
                bg = QColor(255, 255, 200, 200)
                painter.fillRect(r, bg)
                painter.drawText(r, Qt.TextWordWrap | Qt.AlignTop, ann.text)

    def _paint_preview(self, painter):
        tool = self.current_tool
        if tool == TOOL_HIGHLIGHT:
            c = QColor(255, 255, 0, int(255 * self.opacity))
            r = QRectF(QPoint(
                min(self._start_pt.x(), self._cur_pt.x()),
                min(self._start_pt.y(), self._cur_pt.y())),
                QPoint(
                max(self._start_pt.x(), self._cur_pt.x()),
                max(self._start_pt.y(), self._cur_pt.y())))
            painter.fillRect(r, QBrush(c))

        elif tool == TOOL_RECT:
            pen = QPen(self.pen_color, self.line_width, Qt.DashLine)
            painter.setPen(pen)
            r = QRect(self._start_pt, self._cur_pt).normalized()
            painter.drawRect(r)

        elif tool == TOOL_ELLIPSE:
            pen = QPen(self.pen_color, self.line_width, Qt.DashLine)
            painter.setPen(pen)
            r = QRect(self._start_pt, self._cur_pt).normalized()
            painter.drawEllipse(r)

        elif tool == TOOL_LINE:
            pen = QPen(self.pen_color, self.line_width)
            painter.setPen(pen)
            painter.drawLine(self._start_pt, self._cur_pt)

        elif tool == TOOL_DRAW and len(self._draw_pts) > 1:
            pen = QPen(self.pen_color, self.line_width)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            for i in range(len(self._draw_pts) - 1):
                painter.drawLine(self._draw_pts[i], self._draw_pts[i + 1])


# ─── Thumbnail List ───────────────────────────────────────────────────────────
class ThumbnailPanel(QListWidget):
    page_selected = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QSize(100, 140))
        self.setSpacing(6)
        self.setResizeMode(QListWidget.Adjust)
        self.setFixedWidth(140)
        self.itemClicked.connect(self._on_item_clicked)

    def load_document(self, doc):
        self.clear()
        for i, page in enumerate(doc):
            mat = fitz.Matrix(0.3, 0.3)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = QImage(pix.samples, pix.width, pix.height,
                         pix.stride, QImage.Format_RGB888)
            icon = QIcon(QPixmap.fromImage(img))
            item = QListWidgetItem(icon, f"  {i + 1}")
            self.addItem(item)

    def _on_item_clicked(self, item):
        self.page_selected.emit(self.row(item))

    def set_current_page(self, page_num):
        self.setCurrentRow(page_num)


# ─── Add Page Dialog ──────────────────────────────────────────────────────────
class AddPageDialog(QDialog):
    def __init__(self, doc_len, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Blank Page")
        form  = QFormLayout(self)
        self.pos_spin = QSpinBox()
        self.pos_spin.setRange(1, doc_len + 1)
        self.pos_spin.setValue(doc_len + 1)
        self.w_spin   = QDoubleSpinBox()
        self.w_spin.setRange(1, 5000)
        self.w_spin.setValue(595)
        self.w_spin.setSuffix(" pt")
        self.h_spin   = QDoubleSpinBox()
        self.h_spin.setRange(1, 5000)
        self.h_spin.setValue(842)
        self.h_spin.setSuffix(" pt")
        form.addRow("Insert at position:", self.pos_spin)
        form.addRow("Width:",  self.w_spin)
        form.addRow("Height:", self.h_spin)
        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)


# ─── Main Window ──────────────────────────────────────────────────────────────
class PDFEditorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Editor")
        self.resize(1280, 800)

        self._doc          = None
        self._filepath     = None
        self._current_page = 0
        self._zoom         = 1.5
        self._annotations  = {}  # {page_num: [Annotation, ...]}
        self._undo         = UndoStack()
        self._modified     = False

        self._build_ui()
        self._build_toolbar()
        self._build_status_bar()
        self._connect_signals()
        self._update_ui_state()

    # ── UI construction ───────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Thumbnail panel
        self.thumbnail_panel = ThumbnailPanel()

        # Scroll area for page canvas
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setAlignment(Qt.AlignHCenter)
        self.scroll_area.setStyleSheet("background: #3c3c3c;")

        # Use a stacked container so the canvas is never destroyed
        from PyQt5.QtWidgets import QStackedWidget
        self._stack = QStackedWidget()

        self._placeholder = QLabel("Open a PDF file to begin editing")
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(
            "color: #aaa; font-size: 18px; background: #3c3c3c;")
        self._placeholder.setMinimumSize(600, 400)

        self.page_canvas = PageCanvas()

        self._stack.addWidget(self._placeholder)  # index 0
        self._stack.addWidget(self.page_canvas)   # index 1
        self._stack.setCurrentIndex(0)

        self.scroll_area.setWidget(self._stack)

        # Properties panel (right)
        self.props_panel = self._build_props_panel()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.thumbnail_panel)
        splitter.addWidget(self.scroll_area)
        splitter.addWidget(self.props_panel)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([140, 900, 220])

        main_layout.addWidget(splitter)

    def _build_props_panel(self):
        panel = QWidget()
        panel.setFixedWidth(220)
        panel.setStyleSheet("background: #f5f5f5;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        # Tool options group
        tool_grp = QGroupBox("Tool Options")
        tool_layout = QFormLayout(tool_grp)

        self.color_btn = QPushButton()
        self.color_btn.setFixedHeight(28)
        self._set_button_color(self.color_btn, QColor(255, 0, 0))
        self.color_btn.clicked.connect(self._pick_pen_color)
        tool_layout.addRow("Color:", self.color_btn)

        self.fill_color_btn = QPushButton()
        self.fill_color_btn.setFixedHeight(28)
        self._set_button_color(self.fill_color_btn, QColor(255, 255, 0, 100))
        self.fill_color_btn.clicked.connect(self._pick_fill_color)
        tool_layout.addRow("Fill:", self.fill_color_btn)

        self.line_width_spin = QSpinBox()
        self.line_width_spin.setRange(1, 20)
        self.line_width_spin.setValue(2)
        self.line_width_spin.valueChanged.connect(self._on_line_width_changed)
        tool_layout.addRow("Line width:", self.line_width_spin)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(10, 100)
        self.opacity_slider.setValue(50)
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        tool_layout.addRow("Opacity:", self.opacity_slider)

        layout.addWidget(tool_grp)

        # Page info group
        page_grp = QGroupBox("Page")
        page_layout = QFormLayout(page_grp)
        self.page_info_lbl = QLabel("No document")
        self.page_info_lbl.setWordWrap(True)
        page_layout.addRow(self.page_info_lbl)
        layout.addWidget(page_grp)

        # Zoom group
        zoom_grp = QGroupBox("Zoom")
        zoom_layout = QHBoxLayout(zoom_grp)
        self.zoom_out_btn = QPushButton("−")
        self.zoom_out_btn.setFixedSize(28, 28)
        self.zoom_out_btn.clicked.connect(self._zoom_out)
        self.zoom_lbl = QLabel("150%")
        self.zoom_lbl.setAlignment(Qt.AlignCenter)
        self.zoom_in_btn = QPushButton("+")
        self.zoom_in_btn.setFixedSize(28, 28)
        self.zoom_in_btn.clicked.connect(self._zoom_in)
        zoom_layout.addWidget(self.zoom_out_btn)
        zoom_layout.addWidget(self.zoom_lbl)
        zoom_layout.addWidget(self.zoom_in_btn)
        layout.addWidget(zoom_grp)

        layout.addStretch()
        return panel

    def _build_toolbar(self):
        # ── File toolbar ──────────────────────────────────────────────────
        file_tb = self.addToolBar("File")
        file_tb.setIconSize(QSize(22, 22))
        file_tb.setMovable(False)

        act_open = QAction("Open", self)
        act_open.setShortcut(QKeySequence.Open)
        act_open.triggered.connect(self._open_pdf)
        file_tb.addAction(act_open)

        act_save = QAction("Save", self)
        act_save.setShortcut(QKeySequence.Save)
        act_save.triggered.connect(self._save_pdf)
        file_tb.addAction(act_save)

        act_save_as = QAction("Save As…", self)
        act_save_as.setShortcut(QKeySequence.SaveAs)
        act_save_as.triggered.connect(self._save_pdf_as)
        file_tb.addAction(act_save_as)

        file_tb.addSeparator()

        act_undo = QAction("Undo", self)
        act_undo.setShortcut(QKeySequence.Undo)
        act_undo.triggered.connect(self._undo_action)
        file_tb.addAction(act_undo)

        act_redo = QAction("Redo", self)
        act_redo.setShortcut(QKeySequence.Redo)
        act_redo.triggered.connect(self._redo_action)
        file_tb.addAction(act_redo)

        # ── Navigation toolbar ────────────────────────────────────────────
        nav_tb = self.addToolBar("Navigation")
        nav_tb.setMovable(False)

        act_prev = QAction("◀ Prev", self)
        act_prev.setShortcut(Qt.Key_Left)
        act_prev.triggered.connect(self._prev_page)
        nav_tb.addAction(act_prev)

        self.page_spin = QSpinBox()
        self.page_spin.setFixedWidth(60)
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(self._on_page_spin_changed)
        nav_tb.addWidget(self.page_spin)

        self.total_pages_lbl = QLabel(" / 0")
        nav_tb.addWidget(self.total_pages_lbl)

        act_next = QAction("Next ▶", self)
        act_next.setShortcut(Qt.Key_Right)
        act_next.triggered.connect(self._next_page)
        nav_tb.addAction(act_next)

        nav_tb.addSeparator()

        act_rotate_cw = QAction("Rotate CW", self)
        act_rotate_cw.triggered.connect(lambda: self._rotate_page(90))
        nav_tb.addAction(act_rotate_cw)

        act_rotate_ccw = QAction("Rotate CCW", self)
        act_rotate_ccw.triggered.connect(lambda: self._rotate_page(-90))
        nav_tb.addAction(act_rotate_ccw)

        nav_tb.addSeparator()

        act_add_page = QAction("+ Page", self)
        act_add_page.triggered.connect(self._add_blank_page)
        nav_tb.addAction(act_add_page)

        act_del_page = QAction("− Page", self)
        act_del_page.triggered.connect(self._delete_page)
        nav_tb.addAction(act_del_page)

        # ── Tools toolbar ─────────────────────────────────────────────────
        tools_tb = self.addToolBar("Tools")
        tools_tb.setMovable(False)

        self._tool_actions = {}
        tool_defs = [
            (TOOL_SELECT,    "Select"),
            (TOOL_TEXT,      "Text"),
            (TOOL_HIGHLIGHT, "Highlight"),
            (TOOL_DRAW,      "Draw"),
            (TOOL_RECT,      "Rectangle"),
            (TOOL_ELLIPSE,   "Ellipse"),
            (TOOL_LINE,      "Line"),
            (TOOL_ERASE,     "Erase"),
        ]
        for tool_id, label in tool_defs:
            act = QAction(label, self)
            act.setCheckable(True)
            act.triggered.connect(lambda checked, t=tool_id: self._select_tool(t))
            tools_tb.addAction(act)
            self._tool_actions[tool_id] = act

        self._select_tool(TOOL_SELECT)

        # ── Menu bar ──────────────────────────────────────────────────────
        menubar = self.menuBar()

        file_menu = menubar.addMenu("File")
        file_menu.addAction(act_open)
        file_menu.addAction(act_save)
        file_menu.addAction(act_save_as)
        file_menu.addSeparator()
        act_import = QAction("Insert PDF…", self)
        act_import.triggered.connect(self._insert_pdf)
        file_menu.addAction(act_import)
        file_menu.addSeparator()
        act_export = QAction("Export as Image…", self)
        act_export.triggered.connect(self._export_as_image)
        file_menu.addAction(act_export)
        file_menu.addSeparator()
        act_quit = QAction("Quit", self)
        act_quit.setShortcut(QKeySequence.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        edit_menu = menubar.addMenu("Edit")
        edit_menu.addAction(act_undo)
        edit_menu.addAction(act_redo)
        edit_menu.addSeparator()
        act_clear = QAction("Clear Page Annotations", self)
        act_clear.triggered.connect(self._clear_page_annotations)
        edit_menu.addAction(act_clear)
        act_clear_all = QAction("Clear All Annotations", self)
        act_clear_all.triggered.connect(self._clear_all_annotations)
        edit_menu.addAction(act_clear_all)
        edit_menu.addSeparator()
        act_add_text_field = QAction("Add Text Field…", self)
        act_add_text_field.triggered.connect(self._add_pdf_text)
        edit_menu.addAction(act_add_text_field)
        act_add_watermark = QAction("Add Watermark…", self)
        act_add_watermark.triggered.connect(self._add_watermark)
        edit_menu.addAction(act_add_watermark)

        view_menu = menubar.addMenu("View")
        act_zoom_in  = QAction("Zoom In",  self)
        act_zoom_in.setShortcut(QKeySequence.ZoomIn)
        act_zoom_in.triggered.connect(self._zoom_in)
        view_menu.addAction(act_zoom_in)
        act_zoom_out = QAction("Zoom Out", self)
        act_zoom_out.setShortcut(QKeySequence.ZoomOut)
        act_zoom_out.triggered.connect(self._zoom_out)
        view_menu.addAction(act_zoom_out)
        act_zoom_fit = QAction("Fit Width", self)
        act_zoom_fit.triggered.connect(self._zoom_fit)
        view_menu.addAction(act_zoom_fit)

    def _build_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_lbl = QLabel("Ready – Open a PDF to begin")
        self.status_bar.addWidget(self._status_lbl)

    def _connect_signals(self):
        self.thumbnail_panel.page_selected.connect(self._go_to_page)
        self.page_canvas.annotation_added.connect(self._on_annotation_added)

    # ── tool selection ────────────────────────────────────────────────────
    def _select_tool(self, tool):
        for tid, act in self._tool_actions.items():
            act.setChecked(tid == tool)
        self.page_canvas.set_tool(tool)

    # ── document operations ───────────────────────────────────────────────
    def _open_pdf(self):
        if self._modified:
            if not self._ask_save():
                return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open PDF", "", "PDF Files (*.pdf);;All Files (*)")
        if path:
            self._load_pdf(path)

    def _load_pdf(self, path):
        try:
            doc = fitz.open(path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Cannot open PDF:\n{e}")
            return
        self._doc        = doc
        self._filepath   = path
        self._annotations = {}
        self._current_page = 0
        self._modified   = False

        self.page_canvas.set_document(doc)
        self.thumbnail_panel.load_document(doc)
        self.page_spin.setMaximum(len(doc))
        self.total_pages_lbl.setText(f" / {len(doc)}")
        self._go_to_page(0)
        self.setWindowTitle(f"PDF Editor – {os.path.basename(path)}")
        self._status("Opened: " + path)
        self._update_ui_state()

    def _save_pdf(self):
        if self._doc is None:
            return
        if self._filepath is None:
            self._save_pdf_as()
            return
        self._bake_and_save(self._filepath)

    def _save_pdf_as(self):
        if self._doc is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save PDF As", "", "PDF Files (*.pdf)")
        if path:
            if not path.endswith(".pdf"):
                path += ".pdf"
            self._bake_and_save(path)
            self._filepath = path
            self.setWindowTitle(f"PDF Editor – {os.path.basename(path)}")

    def _bake_and_save(self, path):
        """Flatten all annotations into the PDF and save."""
        try:
            for page_num, anns in self._annotations.items():
                if not anns:
                    continue
                page = self._doc[page_num]
                for ann in anns:
                    self._bake_annotation(page, ann)
            self._doc.save(path, garbage=4, deflate=True)
            self._annotations = {}
            self._modified    = False
            self._status(f"Saved: {path}")
            # Reload to reflect baked annotations
            self._load_pdf(path)
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def _bake_annotation(self, page, ann):
        """Write an Annotation object into the fitz page as a native annotation."""
        r = ann.rect
        if r is None:
            return
        fitz_rect = fitz.Rect(r.x(), r.y(),
                              r.x() + r.width(), r.y() + r.height())
        c = ann.color
        color_f = (c.redF(), c.greenF(), c.blueF())

        if ann.ann_type == TOOL_HIGHLIGHT:
            quads = fitz_rect.get_area()
            page.add_highlight_annot(fitz_rect)

        elif ann.ann_type == TOOL_RECT:
            fa = page.add_rect_annot(fitz_rect)
            fa.set_colors(stroke=color_f)
            fa.set_border(width=ann.line_width)
            fa.update()

        elif ann.ann_type == TOOL_ELLIPSE:
            fa = page.add_circle_annot(fitz_rect)
            fa.set_colors(stroke=color_f)
            fa.set_border(width=ann.line_width)
            fa.update()

        elif ann.ann_type == TOOL_TEXT:
            fa = page.add_freetext_annot(
                fitz_rect, ann.text,
                fontsize=12, text_color=color_f, fill_color=(1, 1, 0.8))
            fa.update()

        elif ann.ann_type == TOOL_DRAW and len(ann.points) > 1:
            pts = [(pt.x(), pt.y()) for pt in ann.points]
            fa  = page.add_ink_annot([pts])
            fa.set_colors(stroke=color_f)
            fa.set_border(width=ann.line_width)
            fa.update()

        elif ann.ann_type == TOOL_LINE and len(ann.points) == 2:
            p1 = ann.points[0]
            p2 = ann.points[1]
            fa = page.add_line_annot(
                fitz.Point(p1.x(), p1.y()),
                fitz.Point(p2.x(), p2.y()))
            fa.set_colors(stroke=color_f)
            fa.set_border(width=ann.line_width)
            fa.update()

    # ── page navigation ───────────────────────────────────────────────────
    def _go_to_page(self, page_num):
        if self._doc is None:
            return
        page_num = max(0, min(page_num, len(self._doc) - 1))
        self._current_page = page_num
        anns = self._annotations.get(page_num, [])
        self.page_canvas.annotations = anns
        self.page_canvas.render_page(page_num, self._zoom)
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(page_num + 1)
        self.page_spin.blockSignals(False)
        self.thumbnail_panel.set_current_page(page_num)
        page = self._doc[page_num]
        w, h  = round(page.rect.width), round(page.rect.height)
        self.page_info_lbl.setText(
            f"Page {page_num + 1} of {len(self._doc)}\n{w} × {h} pt")

    def _prev_page(self):
        self._go_to_page(self._current_page - 1)

    def _next_page(self):
        self._go_to_page(self._current_page + 1)

    def _on_page_spin_changed(self, val):
        self._go_to_page(val - 1)

    # ── zoom ──────────────────────────────────────────────────────────────
    def _zoom_in(self):
        self._zoom = min(5.0, self._zoom + 0.25)
        self._refresh_zoom()

    def _zoom_out(self):
        self._zoom = max(0.25, self._zoom - 0.25)
        self._refresh_zoom()

    def _zoom_fit(self):
        if self._doc is None:
            return
        page = self._doc[self._current_page]
        avail = self.scroll_area.viewport().width() - 20
        self._zoom = avail / page.rect.width
        self._refresh_zoom()

    def _refresh_zoom(self):
        self.zoom_lbl.setText(f"{int(self._zoom * 100)}%")
        self.page_canvas.render_page(self._current_page, self._zoom)

    # ── rotation ──────────────────────────────────────────────────────────
    def _rotate_page(self, angle):
        if self._doc is None:
            return
        page = self._doc[self._current_page]
        page.set_rotation((page.rotation + angle) % 360)
        self._modified = True
        self.thumbnail_panel.load_document(self._doc)
        self._go_to_page(self._current_page)
        self._status(f"Page {self._current_page + 1} rotated {angle}°")

    # ── page management ───────────────────────────────────────────────────
    def _add_blank_page(self):
        if self._doc is None:
            return
        dlg = AddPageDialog(len(self._doc), self)
        if dlg.exec_() == QDialog.Accepted:
            pos = dlg.pos_spin.value() - 1
            w   = dlg.w_spin.value()
            h   = dlg.h_spin.value()
            self._doc.new_page(pos, width=w, height=h)
            self._modified = True
            self.thumbnail_panel.load_document(self._doc)
            self.page_spin.setMaximum(len(self._doc))
            self.total_pages_lbl.setText(f" / {len(self._doc)}")
            self._go_to_page(pos)
            self._status(f"Blank page inserted at position {pos + 1}")

    def _delete_page(self):
        if self._doc is None or len(self._doc) <= 1:
            return
        reply = QMessageBox.question(
            self, "Delete Page",
            f"Delete page {self._current_page + 1}?",
            QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self._doc.delete_page(self._current_page)
            self._annotations.pop(self._current_page, None)
            self._modified = True
            self.thumbnail_panel.load_document(self._doc)
            self.page_spin.setMaximum(len(self._doc))
            self.total_pages_lbl.setText(f" / {len(self._doc)}")
            self._go_to_page(min(self._current_page, len(self._doc) - 1))
            self._status(f"Page deleted")

    # ── insert / export ───────────────────────────────────────────────────
    def _insert_pdf(self):
        if self._doc is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Insert PDF", "", "PDF Files (*.pdf)")
        if not path:
            return
        try:
            other = fitz.open(path)
            insert_pos = self._current_page + 1
            self._doc.insert_pdf(other, start_at=insert_pos)
            other.close()
            self._modified = True
            self.thumbnail_panel.load_document(self._doc)
            self.page_spin.setMaximum(len(self._doc))
            self.total_pages_lbl.setText(f" / {len(self._doc)}")
            self._go_to_page(insert_pos)
            self._status(f"Inserted {os.path.basename(path)} at page {insert_pos + 1}")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _export_as_image(self):
        if self._doc is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Page as Image", "",
            "PNG Files (*.png);;JPEG Files (*.jpg)")
        if not path:
            return
        try:
            page = self._doc[self._current_page]
            mat  = fitz.Matrix(2.0, 2.0)
            pix  = page.get_pixmap(matrix=mat, alpha=False)
            pix.save(path)
            self._status(f"Page exported to {path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    # ── annotation helpers ────────────────────────────────────────────────
    def _on_annotation_added(self, ann):
        pg = self._current_page
        if pg not in self._annotations:
            self._annotations[pg] = []
        # avoid duplicate
        if ann not in self._annotations[pg]:
            self._annotations[pg].append(ann)
        self._modified = True
        self._save_undo_state()

    def _save_undo_state(self):
        import copy
        state = {pg: list(anns)
                 for pg, anns in self._annotations.items()}
        self._undo.push(state)

    def _undo_action(self):
        state = self._undo.undo()
        if state is not None:
            self._annotations = state
            self._go_to_page(self._current_page)

    def _redo_action(self):
        state = self._undo.redo()
        if state is not None:
            self._annotations = state
            self._go_to_page(self._current_page)

    def _clear_page_annotations(self):
        self._annotations.pop(self._current_page, None)
        self.page_canvas.annotations = []
        self.page_canvas._redraw()
        self._modified = True

    def _clear_all_annotations(self):
        self._annotations = {}
        self.page_canvas.annotations = []
        self.page_canvas._redraw()
        self._modified = True

    # ── PDF text / watermark ──────────────────────────────────────────────
    def _add_pdf_text(self):
        """Directly insert text into the PDF page (not as annotation)."""
        if self._doc is None:
            return
        text, ok = QInputDialog.getText(self, "Add Text", "Enter text:")
        if not ok or not text.strip():
            return
        page   = self._doc[self._current_page]
        pw, ph = page.rect.width, page.rect.height
        rect   = fitz.Rect(50, ph / 2 - 20, pw - 50, ph / 2 + 20)
        page.insert_textbox(rect, text, fontsize=16,
                            color=(0, 0, 0), align=fitz.TEXT_ALIGN_CENTER)
        self._modified = True
        self._go_to_page(self._current_page)
        self._status("Text inserted into page")

    def _add_watermark(self):
        if self._doc is None:
            return
        text, ok = QInputDialog.getText(
            self, "Add Watermark", "Watermark text:",
            text="DRAFT")
        if not ok or not text.strip():
            return
        for page in self._doc:
            pw, ph = page.rect.width, page.rect.height
            page.insert_text(
                fitz.Point(pw * 0.15, ph * 0.55),
                text, fontsize=72,
                color=(0.8, 0.8, 0.8), rotate=45)
        self._modified = True
        self._go_to_page(self._current_page)
        self.thumbnail_panel.load_document(self._doc)
        self._status(f"Watermark '{text}' applied to all pages")

    # ── property panel callbacks ──────────────────────────────────────────
    def _pick_pen_color(self):
        c = QColorDialog.getColor(self.page_canvas.pen_color, self)
        if c.isValid():
            self.page_canvas.pen_color = c
            self._set_button_color(self.color_btn, c)

    def _pick_fill_color(self):
        c = QColorDialog.getColor(self.page_canvas.fill_color, self,
                                  options=QColorDialog.ShowAlphaChannel)
        if c.isValid():
            self.page_canvas.fill_color = c
            self._set_button_color(self.fill_color_btn, c)

    def _on_line_width_changed(self, val):
        self.page_canvas.line_width = val

    def _on_opacity_changed(self, val):
        self.page_canvas.opacity = val / 100.0

    @staticmethod
    def _set_button_color(btn, color):
        btn.setStyleSheet(
            f"background-color: {color.name()}; border: 1px solid #888;")

    # ── helpers ───────────────────────────────────────────────────────────
    def _status(self, msg):
        self._status_lbl.setText(msg)

    def _update_ui_state(self):
        has_doc = self._doc is not None
        self._stack.setCurrentIndex(1 if has_doc else 0)

    def _ask_save(self):
        reply = QMessageBox.question(
            self, "Unsaved Changes",
            "There are unsaved changes. Save before opening a new file?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if reply == QMessageBox.Save:
            self._save_pdf()
            return True
        elif reply == QMessageBox.Discard:
            return True
        return False

    def closeEvent(self, event):
        if self._modified:
            if not self._ask_save():
                event.ignore()
                return
        if self._doc:
            self._doc.close()
        event.accept()


# ─── Entry Point ─────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PDF Editor")
    app.setStyle("Fusion")

    window = PDFEditorWindow()
    window.show()

    # Open file from command line if provided
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        window._load_pdf(sys.argv[1])

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
