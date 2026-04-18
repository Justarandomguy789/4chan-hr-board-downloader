import requests
import os
import json
import time
import sys
import html
import re
import threading
from datetime import datetime
from PIL import Image as PILImage
import pystray
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QSplitter, 
    QFrame, QMessageBox, QDialog, QGridLayout, QScrollArea, QFileDialog
)
from PyQt6.QtCore import Qt, QSize, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QPixmap, QIcon, QFont

# ==================== CONFIG ====================
APP_NAME = "HighResVault"
BOARDS = ["hr"]
DEFAULT_DIR = "4chan_downloads"
STATE_FILE = "download_state.json"
CHECK_INTERVAL = 600  
HEADERS = {"User-Agent": "4chan-Triage-Downloader/4.0"}
ICON_PATH = "4chan.ico" 

# ==================== MODERN STYLESHEET ====================
STYLE_SHEET = """
QMainWindow, QDialog, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
    font-family: 'Segoe UI', sans-serif;
    font-size: 14px;
}
QScrollBar:vertical {
    border: none;
    background-color: #181825;
    width: 10px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background-color: #45475a;
    border-radius: 5px;
}
QPushButton {
    background-color: #313244;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    color: #cdd6f4;
    font-weight: bold;
}
QPushButton:hover { background-color: #45475a; }
QPushButton:disabled { color: #585b70; background-color: #181825; }
QListWidget {
    background-color: #11111b;
    border: 1px solid #313244;
    border-radius: 8px;
    padding: 6px;
}
QListWidget::item { padding: 10px; border-radius: 6px; margin-bottom: 4px; }
QListWidget::item:selected { background-color: #89b4fa; color: #11111b; }
#btn_yes { background-color: #a6e3a1; color: #11111b; font-size: 15px; padding: 12px; }
#btn_skip { background-color: #89b4fa; color: #11111b; font-size: 15px; padding: 12px; }
#btn_no { background-color: #f38ba8; color: #11111b; font-size: 15px; padding: 12px; }
#img_label { background-color: #11111b; border: 1px solid #313244; border-radius: 8px; }
QSplitter::handle { background-color: #313244; width: 2px; }
"""

# ==================== UTILS ====================
def clean_html(raw_html):
    if not raw_html: return ""
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, ' ', raw_html)
    return html.unescape(cleantext).strip()

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()[:100]

# ==================== THREADS ====================
class PreviewFetcher(QThread):
    previews_ready = pyqtSignal(int, list)
    def __init__(self, board, tno):
        super().__init__()
        self.board, self.tno = board, tno
    def run(self):
        try:
            with requests.Session() as session:
                session.headers.update(HEADERS)
                r = session.get(f"https://a.4cdn.org/{self.board}/thread/{self.tno}.json", timeout=5)
                if r.status_code != 200: return
                posts = r.json().get("posts", [])
                images_found = [(p["tim"], p["ext"]) for p in posts if "tim" in p][:4]
                image_data = [None] * len(images_found)
                def fetch_single(index, tim, ext):
                    try:
                        img_r = session.get(f"https://i.4cdn.org/{self.board}/{tim}{ext}", timeout=8)
                        if img_r.status_code == 200: image_data[index] = img_r.content
                    except: pass
                with ThreadPoolExecutor(max_workers=4) as executor:
                    for i, (tim, ext) in enumerate(images_found): executor.submit(fetch_single, i, tim, ext)
                self.previews_ready.emit(self.tno, [img for img in image_data if img])
        except: pass

class ManageDialog(QDialog):
    def __init__(self, state, state_lock, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Threads")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(800, 500)
        self.state, self.state_lock = state, state_lock
        self.changes_made = False
        layout = QHBoxLayout(self)
        for key, title in [("yes", "Downloading (YES)"), ("no", "Hidden (NO)")]:
            vbox = QVBoxLayout()
            vbox.addWidget(QLabel(title, font=QFont("Segoe UI", 12, QFont.Weight.Bold)))
            lst = QListWidget()
            with self.state_lock:
                for board, tnos in self.state[key].items():
                    for tno in tnos:
                        name = self.state["names"].get(str(tno), f"Thread {tno}")
                        item = QListWidgetItem(f"/{board}/ {name}")
                        item.setData(Qt.ItemDataRole.UserRole, (board, tno))
                        lst.addItem(item)
            vbox.addWidget(lst)
            btn = QPushButton(f"Remove from {key.title()}")
            btn.clicked.connect(lambda chk=False, l=lst, k=key: self.remove_item(l, k))
            vbox.addWidget(btn)
            layout.addLayout(vbox)

    def remove_item(self, list_widget, key):
        row = list_widget.currentRow()
        item = list_widget.takeItem(row)
        if item:
            board, tno = item.data(Qt.ItemDataRole.UserRole)
            with self.state_lock: 
                if tno in self.state[key][board]:
                    self.state[key][board].remove(tno)
            self.changes_made = True

# ==================== MAIN UI ====================
class MainWindow(QMainWindow):
    log_msg = pyqtSignal(str)
    restore_window = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))
            
        self.resize(1200, 800)
        self.state_lock = threading.Lock()
        self.state = self.load_state()
        self.is_running = True
        self.log_msg.connect(self._add_log_ui)
        self.restore_window.connect(self._show_window_ui)
        self.setup_ui()
        self.refresh_catalog()
        self.start_background_downloader()

    def load_state(self):
        default = {"yes": {b: [] for b in BOARDS}, "no": {b: [] for b in BOARDS}, 
                   "names": {}, "download_path": os.path.abspath(DEFAULT_DIR)}
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    saved = json.load(f)
                    for k in ["yes", "no", "names", "download_path"]:
                        if k in saved: default[k] = saved[k]
            except: pass
        return default

    def save_state(self):
        with self.state_lock:
            with open(STATE_FILE, "w") as f: json.dump(self.state, f, indent=2)

    def setup_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        layout = QVBoxLayout(central); layout.setContentsMargins(15, 15, 15, 15)
        top = QHBoxLayout()
        self.btn_refresh = QPushButton("🔄 Refresh"); self.btn_refresh.clicked.connect(self.refresh_catalog)
        self.btn_path = QPushButton("📂 Folder"); self.btn_path.clicked.connect(self.set_download_path)
        self.btn_manage = QPushButton("⚙ Manage"); self.btn_manage.clicked.connect(self.open_manage)
        self.btn_tray = QPushButton("🔽 Tray"); self.btn_tray.clicked.connect(self.minimize_to_tray)
        for b in [self.btn_refresh, self.btn_path, self.btn_manage, self.btn_tray]: top.addWidget(b)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_pane = QFrame(); left_layout = QVBoxLayout(left_pane)
        left_layout.addWidget(QLabel("Pending Threads", font=QFont("Segoe UI", 12, QFont.Weight.Bold)))
        self.thread_list = QListWidget(); self.thread_list.itemClicked.connect(self.on_thread_clicked)
        left_layout.addWidget(self.thread_list); splitter.addWidget(left_pane)

        right_pane = QFrame(); right_layout = QVBoxLayout(right_pane)
        self.lbl_title = QLabel("Select a thread"); self.lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.lbl_title)
        grid_widget = QWidget(); self.grid = QGridLayout(grid_widget)
        self.previews = [QLabel("...") for _ in range(4)]
        for i, lbl in enumerate(self.previews):
            lbl.setFixedSize(300, 300); lbl.setObjectName("img_label"); lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.grid.addWidget(lbl, i//2, i%2)
        right_layout.addWidget(grid_widget, alignment=Qt.AlignmentFlag.AlignCenter)
        
        actions = QHBoxLayout()
        self.btn_yes = QPushButton("✔ YES"); self.btn_yes.setObjectName("btn_yes")
        self.btn_skip = QPushButton("⏭ SKIP"); self.btn_skip.setObjectName("btn_skip")
        self.btn_no = QPushButton("✖ NO"); self.btn_no.setObjectName("btn_no")
        self.btn_yes.clicked.connect(lambda: self.sort_thread("yes"))
        self.btn_skip.clicked.connect(lambda: self.sort_thread("skip"))
        self.btn_no.clicked.connect(lambda: self.sort_thread("no"))
        for b in [self.btn_yes, self.btn_skip, self.btn_no]: actions.addWidget(b); b.setEnabled(False)
        right_layout.addLayout(actions); splitter.addWidget(right_pane)
        splitter.setSizes([350, 850]); layout.addWidget(splitter)
        self.log_widget = QListWidget(); self.log_widget.setMaximumHeight(100); layout.addWidget(self.log_widget)

    def set_download_path(self):
        path = QFileDialog.getExistingDirectory(self, "Select Folder", self.state["download_path"])
        if path:
            with self.state_lock: self.state["download_path"] = path
            self.save_state(); self.log(f"New Path: {path}")

    def refresh_catalog(self):
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("⏳ Checking...")
        self.thread_list.clear()
        
        new_count = 0
        dead_count = 0

        for board in BOARDS:
            try:
                r = requests.get(f"https://a.4cdn.org/{board}/catalog.json", headers=HEADERS, timeout=10)
                if r.status_code != 200: continue
                
                catalog_threads = []
                for page in r.json():
                    for t in page.get("threads", []):
                        catalog_threads.append(t["no"])

                with self.state_lock:
                    # Cleanup: Remove threads from YES/NO that are no longer in the catalog
                    for key in ["yes", "no"]:
                        if board in self.state[key]:
                            original_list = self.state[key][board]
                            # Only keep it if it's still alive in the catalog
                            self.state[key][board] = [tno for tno in original_list if tno in catalog_threads]
                            dead_count += (len(original_list) - len(self.state[key][board]))

                    # Add new threads to UI
                    yes, no = self.state["yes"].get(board, []), self.state["no"].get(board, [])
                
                for page in r.json():
                    for t in page.get("threads", []):
                        if t["no"] not in yes and t["no"] not in no:
                            title = clean_html(t.get("sub") or t.get("com") or "No Title")[:50]
                            with self.state_lock: self.state["names"][str(t["no"])] = title
                            item = QListWidgetItem(f"/{board}/ {title}")
                            item.setData(Qt.ItemDataRole.UserRole, (board, t["no"]))
                            self.thread_list.addItem(item)
                            new_count += 1
            except Exception as e:
                self.log(f"Refresh error: {e}")
        
        self.save_state()
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("🔄 Refresh")
        self.log(f"Refresh done. Found {new_count} new. Cleaned {dead_count} archived.")

    def on_thread_clicked(self, item):
        board, tno = item.data(Qt.ItemDataRole.UserRole)
        self.current_tno = tno
        with self.state_lock: name = self.state["names"].get(str(tno), str(tno))
        self.lbl_title.setText(f"Fetching: {name}")
        for lbl in self.previews: lbl.setText("...")
        self.worker = PreviewFetcher(board, tno)
        self.worker.previews_ready.connect(self.on_previews_ready); self.worker.start()

    def on_previews_ready(self, tno, images):
        if tno != getattr(self, "current_tno", None): return
        for i, lbl in enumerate(self.previews):
            if i < len(images):
                px = QPixmap(); px.loadFromData(images[i])
                lbl.setPixmap(px.scaled(300, 300, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        for b in [self.btn_yes, self.btn_skip, self.btn_no]: b.setEnabled(True)

    def sort_thread(self, action):
        item = self.thread_list.currentItem()
        if not item: return
        board, tno = item.data(Qt.ItemDataRole.UserRole)
        row = self.thread_list.row(item)
        if action in ["yes", "no"]:
            with self.state_lock: self.state[action][board].append(tno)
            self.thread_list.takeItem(row)
            if action == "yes": threading.Thread(target=self.download_instant, args=(board, tno), daemon=True).start()
        elif action == "skip":
            self.thread_list.setCurrentRow((row + 1) % self.thread_list.count() if self.thread_list.count() > 0 else 0)
            self.on_thread_clicked(self.thread_list.currentItem()); return
        self.save_state()
        if self.thread_list.count() > 0:
            self.thread_list.setCurrentRow(row if row < self.thread_list.count() else 0)
            self.on_thread_clicked(self.thread_list.currentItem())
        else: self.lbl_title.setText("Done!"); [l.clear() for l in self.previews]

    def log(self, msg): self.log_msg.emit(msg)
    def _add_log_ui(self, m): self.log_widget.addItem(f"[{datetime.now().strftime('%H:%M:%S')}] {m}"); self.log_widget.scrollToBottom()

    def open_manage(self):
        if ManageDialog(self.state, self.state_lock, self).exec(): pass
        self.save_state(); self.refresh_catalog()

    def download_instant(self, board, tno):
        with requests.Session() as sess:
            sess.headers.update(HEADERS); self._process(board, tno, sess)

    def start_background_downloader(self):
        threading.Thread(target=self.downloader_loop, daemon=True).start()

    def downloader_loop(self):
        with requests.Session() as sess:
            sess.headers.update(HEADERS)
            while self.is_running:
                with self.state_lock: yes_copy = {b: list(tnos) for b, tnos in self.state["yes"].items()}
                for board, tnos in yes_copy.items():
                    for tno in tnos: self._process(board, tno, sess)
                for _ in range(CHECK_INTERVAL):
                    if not self.is_running: return
                    time.sleep(1)

    def _process(self, board, tno, sess):
        try:
            with self.state_lock:
                path, raw_name = self.state["download_path"], self.state["names"].get(str(tno), str(tno))
            safe_name = sanitize_filename(raw_name) or str(tno)
            save_dir = os.path.join(path, safe_name)
            
            r = sess.get(f"https://a.4cdn.org/{board}/thread/{tno}.json", timeout=10)
            
            # If thread is archived or deleted (404), remove it from the list
            if r.status_code == 404:
                self.log(f"Thread dead, removing from queue: {raw_name}")
                with self.state_lock:
                    if tno in self.state["yes"][board]:
                        self.state["yes"][board].remove(tno)
                self.save_state()
                return

            if r.status_code != 200: return
            
            os.makedirs(save_dir, exist_ok=True)
            new_f = 0
            for post in r.json().get("posts", []):
                if "tim" in post:
                    fn = f"{post['tim']}{post['ext']}"
                    fpath = os.path.join(save_dir, fn)
                    if not os.path.exists(fpath):
                        img = sess.get(f"https://i.4cdn.org/{board}/{fn}", timeout=15)
                        if img.status_code == 200:
                            with open(fpath, "wb") as f: f.write(img.content)
                            new_f += 1
            if new_f: self.log(f"Saved {new_f} images: {raw_name}")
        except: pass

    # ==================== UPDATED TRAY LOGIC ====================
    def minimize_to_tray(self):
        self.hide()
        if os.path.exists(ICON_PATH):
            icon_img = PILImage.open(ICON_PATH)
        else:
            icon_img = PILImage.new('RGB', (64, 64), (30, 150, 100))
            
        self.tray = pystray.Icon("4chan", icon_img, APP_NAME, menu=pystray.Menu(
            pystray.MenuItem("Restore", lambda: self.restore_window.emit()), 
            pystray.MenuItem("Exit", self.full_exit)))
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _show_window_ui(self):
        if hasattr(self, 'tray'): 
            self.tray.stop()
        self.show()
        self.showNormal() 
        self.activateWindow() 
        self.raise_() 

    def full_exit(self):
        self.is_running = False
        if hasattr(self, 'tray'): self.tray.stop()
        QApplication.quit()

    def closeEvent(self, e): self.full_exit(); e.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion"); app.setStyleSheet(STYLE_SHEET)
    window = MainWindow(); window.show(); sys.exit(app.exec())
