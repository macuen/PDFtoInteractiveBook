import os
import sqlite3
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

try:
    import fitz  # PyMuPDF
    from PIL import Image, ImageTk
except ImportError as exc:
    raise SystemExit(
        "Missing dependency. Run: python -m pip install -r requirements.txt"
    ) from exc


APP_NAME = "PDF Place Reader"


def app_data_dir() -> Path:
    """Return a per-user application data directory on Windows/macOS/Linux."""
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    directory = root / APP_NAME.replace(" ", "")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class ProgressStore:
    """Tiny SQLite database containing one last-read page per PDF path."""
    def __init__(self):
        self.db_path = app_data_dir() / "reading_progress.sqlite3"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS reading_progress (
                pdf_path TEXT PRIMARY KEY,
                page_index INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.connection.commit()

    def get_page(self, pdf_path: str) -> int:
        row = self.connection.execute(
            "SELECT page_index FROM reading_progress WHERE pdf_path = ?",
            (pdf_path,)
        ).fetchone()
        return int(row[0]) if row else 0

    def save_page(self, pdf_path: str, page_index: int):
        self.connection.execute("""
            INSERT INTO reading_progress (pdf_path, page_index, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(pdf_path) DO UPDATE SET
                page_index = excluded.page_index,
                updated_at = CURRENT_TIMESTAMP
        """, (pdf_path, int(page_index)))
        self.connection.commit()

    def close(self):
        self.connection.close()


class PDFPlaceReader:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1000x800")
        self.root.minsize(600, 450)
        self.store = ProgressStore()
        self.document = None
        self.pdf_path = None
        self.page_index = 0
        self.current_photo = None
        self.render_job = None
        self.zoom = 1.35
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)
        self.root.bind("<Left>", lambda _event: self.previous_page())
        self.root.bind("<Right>", lambda _event: self.next_page())
        self.root.bind("<Control-o>", lambda _event: self.open_pdf())
        self.root.bind("<Control-plus>", lambda _event: self.change_zoom(0.15))
        self.root.bind("<Control-minus>", lambda _event: self.change_zoom(-0.15))

    def _build_ui(self):
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="Open PDF", command=self.open_pdf).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Previous", command=self.previous_page).pack(
            side=tk.LEFT, padx=(8, 2)
        )
        ttk.Button(toolbar, text="Next", command=self.next_page).pack(side=tk.LEFT)

        self.page_var = tk.StringVar(value="No PDF open")
        ttk.Label(toolbar, textvariable=self.page_var, width=18).pack(
            side=tk.LEFT, padx=10
        )

        ttk.Label(toolbar, text="Go to:").pack(side=tk.LEFT)
        self.goto_var = tk.StringVar()
        self.goto_entry = ttk.Entry(toolbar, textvariable=self.goto_var, width=6)
        self.goto_entry.pack(side=tk.LEFT, padx=4)
        self.goto_entry.bind("<Return>", lambda _event: self.go_to_page())
        ttk.Button(toolbar, text="Go", command=self.go_to_page).pack(side=tk.LEFT)

        ttk.Button(toolbar, text="−", width=3,
                   command=lambda: self.change_zoom(-0.15)).pack(
                       side=tk.RIGHT, padx=(3, 0)
                   )
        ttk.Button(toolbar, text="+", width=3,
                   command=lambda: self.change_zoom(0.15)).pack(side=tk.RIGHT)
        ttk.Label(toolbar, text="Zoom").pack(side=tk.RIGHT, padx=5)

        self.canvas = tk.Canvas(
            self.root,
            background="#555555",
            highlightthickness=0
        )

        self.v_scroll = ttk.Scrollbar(
            self.root,
            orient=tk.VERTICAL,
            command=self.canvas.yview
        )

        self.h_scroll = ttk.Scrollbar(
            self.root,
            orient=tk.HORIZONTAL,
            command=self.canvas.xview
        )

        self.canvas.configure(
            yscrollcommand=self.v_scroll.set,
            xscrollcommand=self.h_scroll.set
        )

        # Dedicated status bar at the very bottom
        status_bar = ttk.Frame(self.root, padding=(8, 3))
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.status_var = tk.StringVar(
            value="Open a PDF. Your last page is saved automatically."
        )

        ttk.Label(
            status_bar,
            textvariable=self.status_var,
            anchor=tk.W
        ).pack(side=tk.LEFT)

        # PDF viewing area occupies all remaining space
        self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas.bind("<Configure>", self._on_canvas_resize)

    def open_pdf(self):
        selected = filedialog.askopenfilename(
            title="Open PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")]
        )
        if not selected:
            return

        selected = str(Path(selected).expanduser().resolve())
        try:
            new_document = fitz.open(selected)
            if new_document.page_count == 0:
                new_document.close()
                raise ValueError("This PDF contains no pages.")
            if not new_document.is_pdf:
                new_document.close()
                raise ValueError("The selected file is not a valid PDF.")
        except Exception as exc:
            messagebox.showerror("Unable to open PDF", str(exc))
            return

        if self.document is not None:
            self.document.close()

        self.document = new_document
        self.pdf_path = selected
        self.root.title(f"{APP_NAME} — {Path(selected).name}")
        saved_page = self.store.get_page(selected)
        self.page_index = max(0, min(saved_page, self.document.page_count - 1))
        self.render_page()
        self.status_var.set(f"Opened: {Path(selected).name}")

    def render_page(self):
        if self.document is None:
            return

        # Render only the current page; previous page images are not retained.
        page = self.document.load_page(self.page_index)
        matrix = fitz.Matrix(self.zoom, self.zoom)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
        self.current_photo = ImageTk.PhotoImage(image)

        self.canvas.delete("all")
        self.canvas.create_image(12, 12, anchor=tk.NW, image=self.current_photo)
        self.canvas.configure(scrollregion=(0, 0, pixmap.width + 24, pixmap.height + 24))
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

        total = self.document.page_count
        self.page_var.set(f"Page {self.page_index + 1} / {total}")
        self.goto_var.set(str(self.page_index + 1))

        # Commit immediately: closing or crashing after this point won't lose the page.
        self.store.save_page(self.pdf_path, self.page_index)

    def previous_page(self):
        if self.document is not None and self.page_index > 0:
            self.page_index -= 1
            self.render_page()

    def next_page(self):
        if self.document is not None and self.page_index < self.document.page_count - 1:
            self.page_index += 1
            self.render_page()

    def go_to_page(self):
        if self.document is None:
            return
        try:
            requested = int(self.goto_var.get())
        except ValueError:
            messagebox.showwarning("Invalid page", "Enter a whole page number.")
            return
        if not 1 <= requested <= self.document.page_count:
            messagebox.showwarning(
                "Page out of range",
                f"Enter a page from 1 to {self.document.page_count}."
            )
            return
        self.page_index = requested - 1
        self.render_page()

    def change_zoom(self, delta):
        self.zoom = max(0.4, min(3.0, self.zoom + delta))
        if self.document is not None:
            self.render_page()

    def _on_canvas_resize(self, _event):
        # Deliberately do not auto-rerender on resize: avoids repeated large renders.
        pass

    def close_app(self):
        try:
            if self.document is not None:
                self.store.save_page(self.pdf_path, self.page_index)
                self.document.close()
            self.store.close()
        finally:
            self.root.destroy()


def main():
    root = tk.Tk()
    PDFPlaceReader(root)
    root.mainloop()


if __name__ == "__main__":
    main()
