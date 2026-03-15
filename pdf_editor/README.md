# PDF Editor Desktop App

A feature-rich desktop PDF editor built with Python, PyQt5, and PyMuPDF.

## Features

- **View PDFs** – smooth rendering with zoom in/out and fit-width
- **Annotate** – highlight text, draw freehand, add rectangles, ellipses, and lines
- **Add text** – place text annotations anywhere on a page, or insert text directly into the PDF
- **Erase** – remove individual annotations with the eraser tool
- **Page management** – add blank pages, delete pages, rotate pages (CW/CCW)
- **Insert PDF** – merge another PDF into the current document
- **Watermark** – apply a diagonal watermark to all pages
- **Export** – export any page as a PNG or JPEG image
- **Undo / Redo** – full undo/redo stack for annotation edits
- **Save / Save As** – flatten annotations and save as a standard PDF
- **Thumbnail panel** – quick navigation via page thumbnails
- **Properties panel** – choose pen color, fill color, line width, and opacity

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Launch the editor
python main.py

# Open a specific file on startup
python main.py path/to/document.pdf
```

## Tool Reference

| Tool       | Description                                  |
|------------|----------------------------------------------|
| Select     | Default pointer – no annotation added        |
| Text       | Click to place a text annotation             |
| Highlight  | Drag to highlight a rectangular region       |
| Draw       | Freehand pen drawing                         |
| Rectangle  | Drag to draw a rectangle                     |
| Ellipse    | Drag to draw an ellipse                      |
| Line       | Drag to draw a straight line                 |
| Erase      | Click/drag to remove annotations             |

## Keyboard Shortcuts

| Shortcut        | Action        |
|-----------------|---------------|
| Ctrl+O          | Open PDF      |
| Ctrl+S          | Save          |
| Ctrl+Shift+S    | Save As       |
| Ctrl+Z          | Undo          |
| Ctrl+Y          | Redo          |
| Ctrl++          | Zoom in       |
| Ctrl+-          | Zoom out      |
| ← / →           | Prev/Next page|

## Dependencies

- [PyMuPDF](https://pymupdf.readthedocs.io/) – PDF rendering and editing
- [PyQt5](https://riverbankcomputing.com/software/pyqt/) – GUI framework
