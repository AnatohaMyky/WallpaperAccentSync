import colorsys
import ctypes
import io
import os
import struct
import sys
import threading
import tkinter as tk
from tkinter import colorchooser
from typing import Any
import winreg
from PIL import Image, ImageDraw, ImageTk
import pystray

# Увімкнення чіткого масштабування інтерфейсу на HiDPI-екранах Windows
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


# --- Константи Win32 та шляхи ---

SPI_GETDESKWALLPAPER = 0x0073
SPI_SETCURSORS = 0x0057
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02

COLOR_HIGHLIGHT = 13
COLOR_HOTLIGHT = 26

APP_NAME = "WallpaperAccentSync"
APP_REG_PATH = rf"Software\{APP_NAME}"
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
CURSORS_REG_PATH = r"Control Panel\Cursors"

CURSOR_STORAGE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    APP_NAME,
    "cursors",
)

user32 = ctypes.windll.user32


def rgb_to_colorref(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return (b << 16) | (g << 8) | r


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def hex_to_rgb(hex_str: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        clean = hex_str.strip().lstrip("#")
        if len(clean) == 6:
            return (int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16))
    except ValueError:
        pass
    return fallback


def get_perceived_brightness(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


# --- Генерація реальних .cur файлів Windows ---

def save_cur_file(img: Image.Image, path: str, hotspot_x: int = 0, hotspot_y: int = 0) -> None:
    """Зберігає RGBA-зображення у бінарний формат курсора Windows (.cur)."""
    img = img.convert("RGBA")
    w, h = img.size

    xor_rows = bytearray()
    pixels = img.load()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            xor_rows.extend((b, g, r, a))

    and_row_size = ((w + 31) // 32) * 4
    and_rows = bytes(and_row_size * h)

    bih = struct.pack(
        "<IiiHHIIiiII",
        40,
        w,
        h * 2,
        1,
        32,
        0,
        len(xor_rows) + len(and_rows),
        0,
        0,
        0,
        0,
    )
    image_data = bih + bytes(xor_rows) + and_rows
    header = struct.pack("<HHH", 0, 2, 1)
    entry = struct.pack(
        "<BBBBHHII",
        w if w < 256 else 0,
        h if h < 256 else 0,
        0,
        0,
        hotspot_x,
        hotspot_y,
        len(image_data),
        6 + 16,
    )

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(header + entry + image_data)


def render_polygon_cursor(
    points_128: list[tuple[int, int]],
    fill_rgb: tuple[int, int, int],
    hotspot_32: tuple[int, int],
    out_path: str,
) -> None:
    """Малює антиаліасинговий курсор у 128x128 і зменшує до 32x32."""
    canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    outline_rgba = (20, 20, 24, 255) if get_perceived_brightness(fill_rgb) > 0.42 else (250, 250, 252, 255)
    fill_rgba = (*fill_rgb, 255)

    draw.polygon(points_128, fill=fill_rgba)
    draw.line(points_128 + [points_128[0]], fill=outline_rgba, width=7, joint="curve")

    final_img = canvas.resize((32, 32), Image.Resampling.LANCZOS)
    save_cur_file(final_img, out_path, hotspot_x=hotspot_32[0], hotspot_y=hotspot_32[1])


def generate_colored_cursors(rgb: tuple[int, int, int]) -> dict[str, str]:
    """Створює набір курсорів із заданим акцентним кольором і повертає шляхи до них."""
    arrow_path = os.path.join(CURSOR_STORAGE_DIR, "accent_arrow.cur")
    hand_path = os.path.join(CURSOR_STORAGE_DIR, "accent_hand.cur")
    cross_path = os.path.join(CURSOR_STORAGE_DIR, "accent_cross.cur")

    arrow_pts = [
        (12, 8),
        (12, 104),
        (36, 82),
        (52, 118),
        (66, 112),
        (50, 76),
        (82, 76),
    ]
    render_polygon_cursor(arrow_pts, rgb, hotspot_32=(3, 2), out_path=arrow_path)

    hand_pts = [
        (44, 12),
        (56, 12),
        (56, 48),
        (70, 48),
        (70, 52),
        (84, 52),
        (84, 58),
        (96, 58),
        (96, 86),
        (88, 106),
        (38, 106),
        (22, 76),
        (30, 66),
        (44, 72),
    ]
    render_polygon_cursor(hand_pts, rgb, hotspot_32=(12, 3), out_path=hand_path)

    cross_pts = [
        (58, 16),
        (70, 16),
        (70, 58),
        (112, 58),
        (112, 70),
        (70, 70),
        (70, 112),
        (58, 112),
        (58, 70),
        (16, 70),
        (16, 58),
        (58, 58),
    ]
    render_polygon_cursor(cross_pts, rgb, hotspot_32=(16, 16), out_path=cross_path)

    return {
        "Arrow": arrow_path,
        "Hand": hand_path,
        "Crosshair": cross_path,
    }


def apply_cursor_color(rgb: tuple[int, int, int]) -> None:
    paths = generate_colored_cursors(rgb)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Accessibility") as key:
        winreg.SetValueEx(key, "CursorType", 0, winreg.REG_DWORD, 0)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CURSORS_REG_PATH) as key:
        for role, cur_path in paths.items():
            winreg.SetValueEx(key, role, 0, winreg.REG_SZ, cur_path)

    user32.SystemParametersInfoW(
        SPI_SETCURSORS, 0, None, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    )


def reset_cursor_to_default() -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Accessibility") as key:
        winreg.SetValueEx(key, "CursorType", 0, winreg.REG_DWORD, 0)

    defaults = {
        "Arrow": (r"%SystemRoot%\cursors\aero_arrow.cur", winreg.REG_EXPAND_SZ),
        "Hand": (r"%SystemRoot%\cursors\aero_link.cur", winreg.REG_EXPAND_SZ),
        "Crosshair": ("", winreg.REG_SZ),
    }
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CURSORS_REG_PATH) as key:
        for role, (val, reg_type) in defaults.items():
            winreg.SetValueEx(key, role, 0, reg_type, val)

    user32.SystemParametersInfoW(
        SPI_SETCURSORS, 0, None, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    )


# --- Системні кольори виділення, шпалери та налаштування ---

def apply_selection_color(rgb: tuple[int, int, int]) -> None:
    r, g, b = rgb
    color_ref = rgb_to_colorref(rgb)
    border = (max(0, r - 20), max(0, g - 20), max(0, b - 20))
    border_ref = rgb_to_colorref(border)

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, r"Control Panel\Colors", 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "HotTrackingColor", 0, winreg.REG_SZ, f"{r} {g} {b}")
        winreg.SetValueEx(
            key, "Hilight", 0, winreg.REG_SZ, f"{border[0]} {border[1]} {border[2]}"
        )

    elements = (ctypes.c_int * 2)(COLOR_HIGHLIGHT, COLOR_HOTLIGHT)
    colors = (ctypes.c_int * 2)(border_ref, color_ref)
    user32.SetSysColors(2, elements, colors)


def reset_selection_to_default() -> None:
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, r"Control Panel\Colors", 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "HotTrackingColor", 0, winreg.REG_SZ, "0 102 204")
        winreg.SetValueEx(key, "Hilight", 0, winreg.REG_SZ, "0 120 215")

    elements = (ctypes.c_int * 2)(COLOR_HIGHLIGHT, COLOR_HOTLIGHT)
    colors = (ctypes.c_int * 2)(
        rgb_to_colorref((0, 120, 215)),
        rgb_to_colorref((0, 102, 204)),
    )
    user32.SetSysColors(2, elements, colors)


def save_active_accent_meta(
    cursor_rgb: tuple[int, int, int], selection_rgb: tuple[int, int, int]
) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_REG_PATH) as key:
        winreg.SetValueEx(key, "AccentHex", 0, winreg.REG_SZ, rgb_to_hex(selection_rgb))
        winreg.SetValueEx(key, "AccentColorRef", 0, winreg.REG_DWORD, rgb_to_colorref(selection_rgb))
        winreg.SetValueEx(key, "CursorHex", 0, winreg.REG_SZ, rgb_to_hex(cursor_rgb))
        winreg.SetValueEx(key, "SelectionHex", 0, winreg.REG_SZ, rgb_to_hex(selection_rgb))


def load_color_setting(name: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_REG_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, name)
            if isinstance(val, str):
                return hex_to_rgb(val, fallback)
    except OSError:
        pass
    return fallback


def load_bool_setting(name: str, default: bool) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_REG_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, name)
            return bool(val)
    except OSError:
        return default


def save_bool_setting(name: str, value: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_REG_PATH) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val)
    except OSError:
        return False


def set_autostart_enabled(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH) as key:
        if enabled:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                exe = pyw if os.path.isfile(pyw) else sys.executable
                cmd = f'"{exe}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def get_wallpaper_path() -> str | None:
    buf = ctypes.create_unicode_buffer(512)
    if user32.SystemParametersInfoW(SPI_GETDESKWALLPAPER, len(buf), buf, 0) and buf.value:
        if os.path.isfile(buf.value):
            return buf.value

    transcoded = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Themes\TranscodedWallpaper",
    )
    return transcoded if os.path.isfile(transcoded) else None


def get_wallpaper_signature() -> tuple[str | None, float]:
    path = get_wallpaper_path()
    transcoded = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Themes\TranscodedWallpaper",
    )
    mtime = 0.0
    for candidate in (transcoded, path):
        if candidate and os.path.isfile(candidate):
            try:
                mtime = max(mtime, os.path.getmtime(candidate))
            except OSError:
                pass
    return path, mtime


# --- Аналіз палітри шпалер ---

def load_wallpaper_image(image_path: str) -> Image.Image:
    with open(image_path, "rb") as f:
        data = f.read()
    return Image.open(io.BytesIO(data)).convert("RGB")


def extract_wallpaper_palette(img: Image.Image, max_colors: int = 6) -> list[tuple[int, int, int]]:
    scaled = img.resize((72, 72), Image.Resampling.BILINEAR)
    raw_pixels = scaled.get_flattened_data() if hasattr(scaled, "get_flattened_data") else scaled.getdata()
    pixels = list(raw_pixels)

    num_buckets = 36
    weights = [0.0] * num_buckets
    sum_r = [0.0] * num_buckets
    sum_g = [0.0] * num_buckets
    sum_b = [0.0] * num_buckets

    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if v < 0.18 or s < 0.20:
            continue

        weight = (s ** 2.0) * (1.0 - abs(v - 0.65))
        if weight <= 0:
            continue

        bucket = int(h * 360.0 / 10.0) % num_buckets
        weights[bucket] += weight
        sum_r[bucket] += r * weight
        sum_g[bucket] += g * weight
        sum_b[bucket] += b * weight

    candidates: list[tuple[int, float, tuple[int, int, int]]] = []
    for i in range(num_buckets):
        if weights[i] > 0.001:
            avg_rgb = (
                max(0, min(255, round(sum_r[i] / weights[i]))),
                max(0, min(255, round(sum_g[i] / weights[i]))),
                max(0, min(255, round(sum_b[i] / weights[i]))),
            )
            candidates.append((i, weights[i], avg_rgb))

    candidates.sort(key=lambda item: item[1], reverse=True)

    def circular_dist(a: int, b: int) -> int:
        diff = abs(a - b)
        return min(diff, num_buckets - diff)

    selected: list[tuple[int, float, tuple[int, int, int]]] = []

    for cand in candidates:
        if len(selected) >= max_colors:
            break
        if all(circular_dist(s[0], cand[0]) >= 3 for s in selected):
            selected.append(cand)

    for cand in candidates:
        if len(selected) >= max_colors:
            break
        if all(s[0] != cand[0] for s in selected):
            selected.append(cand)

    result = [item[2] for item in selected]

    fallbacks = [
        (0, 153, 255),
        (255, 136, 0),
        (0, 200, 150),
        (168, 100, 255),
        (235, 75, 110),
        (210, 210, 215),
    ]
    for fb in fallbacks:
        if len(result) >= max_colors:
            break
        if fb not in result:
            result.append(fb)

    return result


def create_tray_icon_image(
    cursor_rgb: tuple[int, int, int], selection_rgb: tuple[int, int, int]
) -> Image.Image:
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if cursor_rgb == selection_rgb:
        draw.ellipse((2, 2, 29, 29), fill=(*cursor_rgb, 255))
    else:
        draw.pieslice((2, 2, 29, 29), start=90, end=270, fill=(*cursor_rgb, 255))
        draw.pieslice((2, 2, 29, 29), start=270, end=90, fill=(*selection_rgb, 255))
    draw.ellipse((2, 2, 29, 29), outline=(255, 255, 255, 255), width=3)
    return img


# --- Головне вікно та системний трей ---

class WallpaperAccentApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Wallpaper Accent Sync")
        self.root.configure(bg="#1C1C1E")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.auto_apply = tk.BooleanVar(value=load_bool_setting("AutoApply", True))
        self.enable_cursor = tk.BooleanVar(value=load_bool_setting("EnableCursor", True))
        self.enable_selection = tk.BooleanVar(value=load_bool_setting("EnableSelection", True))
        self.separate_colors = tk.BooleanVar(value=load_bool_setting("SeparateColors", False))
        self.autostart = tk.BooleanVar(value=is_autostart_enabled())

        default_blue = (0, 120, 215)
        self.cursor_color: tuple[int, int, int] = load_color_setting("CursorHex", default_blue)
        self.selection_color: tuple[int, int, int] = load_color_setting("SelectionHex", default_blue)
        if not self.separate_colors.get():
            self.selection_color = self.cursor_color

        self.active_target: str = "cursor"

        self.palette: list[tuple[int, int, int]] = []
        self.last_signature: tuple[str | None, float] = (None, 0.0)
        self.preview_photo: ImageTk.PhotoImage | None = None

        self._build_ui()
        self._refresh_status_area()
        self._fit_and_center_window()

        self.tray_icon = self._create_tray_icon()
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

        if self.auto_apply.get():
            self.sync_from_wallpaper(force_apply=True)
        else:
            self.sync_from_wallpaper(force_apply=False)
            self._apply_current_targets()

        self.root.after(1500, self._poll_wallpaper_changes)

    def _build_ui(self) -> None:
        bg_main = "#1C1C1E"
        fg_text = "#F0F0F2"
        fg_muted = "#B8B8BE"

        preview_container = tk.Frame(
            self.root,
            width=424,
            height=155,
            bg="#121214",
            highlightbackground="#3A3A40",
            highlightthickness=1,
        )
        preview_container.pack(padx=16, pady=(16, 10))
        preview_container.pack_propagate(False)

        self.preview_label = tk.Label(preview_container, bg="#121214", text="Немає прев'ю", fg=fg_muted)
        self.preview_label.pack(expand=True, fill="both")

        tk.Label(
            self.root,
            text="Підібрані кольори зі шпалер (натисни, щоб застосувати):",
            bg=bg_main,
            fg=fg_muted,
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 6))

        self.cards_frame = tk.Frame(self.root, bg=bg_main)
        self.cards_frame.pack(fill="x", padx=16)

        self.status_container = tk.Frame(self.root, bg=bg_main)
        self.status_container.pack(fill="x", padx=16, pady=(12, 8))

        # --- Віджети для спільного режиму ---
        self.unified_status_frame = tk.Frame(self.status_container, bg=bg_main)
        self.unified_badge = tk.Frame(
            self.unified_status_frame,
            width=20,
            height=20,
            bg="#0078D7",
            highlightbackground="#FFFFFF",
            highlightthickness=1,
        )
        self.unified_badge.pack(side="left")

        self.unified_label = tk.Label(
            self.unified_status_frame,
            text="Активний акцент: #0078D7",
            bg=bg_main,
            fg=fg_text,
            font=("Segoe UI", 9, "bold"),
        )
        self.unified_label.pack(side="left", padx=(8, 0))

        # --- Віджети для роздільного режиму (кнопки вибору цілі) ---
        self.split_status_frame = tk.Frame(self.status_container, bg=bg_main)

        self.cursor_target_card, self.cursor_badge, self.cursor_target_lbl = self._create_target_pill(
            self.split_status_frame,
            title="Курсор",
            on_click=lambda: self._set_active_target("cursor"),
        )
        self.cursor_target_card.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.selection_target_card, self.selection_badge, self.selection_target_lbl = self._create_target_pill(
            self.split_status_frame,
            title="Виділення",
            on_click=lambda: self._set_active_target("selection"),
        )
        self.selection_target_card.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Блок налаштувань (перемикачі)
        opts_frame = tk.Frame(self.root, bg=bg_main)
        opts_frame.pack(fill="x", padx=13, pady=(0, 10))

        self._make_checkbox(
            opts_frame,
            "Змінювати колір курсора",
            self.enable_cursor,
            self._on_targets_toggled,
        ).pack(anchor="w", pady=1)

        self._make_checkbox(
            opts_frame,
            "Змінювати колір виділення (Win32)",
            self.enable_selection,
            self._on_targets_toggled,
        ).pack(anchor="w", pady=1)

        self._make_checkbox(
            opts_frame,
            "Окремий колір для курсора та виділення",
            self.separate_colors,
            self._on_separate_colors_toggled,
        ).pack(anchor="w", pady=1)

        self._make_checkbox(
            opts_frame,
            "Автоматично ставити кольори при зміні шпалер",
            self.auto_apply,
            self._on_auto_apply_toggled,
        ).pack(anchor="w", pady=1)

        self._make_checkbox(
            opts_frame,
            "Запускати разом із Windows (автозавантаження)",
            self.autostart,
            self._on_autostart_toggled,
        ).pack(anchor="w", pady=1)

        # Нижня панель кнопок
        btn_frame = tk.Frame(self.root, bg=bg_main)
        btn_frame.pack(fill="x", padx=16, pady=(2, 16))

        self._make_button(btn_frame, "Свій колір...", self.pick_custom_color, width=13).pack(side="left")
        self._make_button(btn_frame, "Оновити", lambda: self.sync_from_wallpaper(force_apply=False), width=10).pack(
            side="left", padx=(6, 0)
        )
        self._make_button(btn_frame, "Скинути", self.reset_defaults, width=9).pack(side="left", padx=(6, 0))
        self._make_button(btn_frame, "У трей", self.hide_window, width=9).pack(side="right")

    def _create_target_pill(
        self, parent: tk.Widget, title: str, on_click
    ) -> tuple[tk.Frame, tk.Frame, tk.Label]:
        pill = tk.Frame(
            parent,
            bg="#26262A",
            highlightbackground="#3A3A40",
            highlightthickness=1,
            padx=8,
            pady=5,
            cursor="hand2",
        )
        badge = tk.Frame(
            pill,
            width=16,
            height=16,
            bg="#0078D7",
            highlightbackground="#FFFFFF",
            highlightthickness=1,
            cursor="hand2",
        )
        badge.pack(side="left")

        lbl = tk.Label(
            pill,
            text=f"{title}: #0078D7",
            bg="#26262A",
            fg="#F0F0F2",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
        )
        lbl.pack(side="left", padx=(7, 0))

        for w in (pill, badge, lbl):
            w.bind("<Button-1>", lambda _: on_click())

        return pill, badge, lbl

    @staticmethod
    def _make_checkbox(parent: tk.Widget, text: str, variable: tk.BooleanVar, command) -> tk.Checkbutton:
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            command=command,
            bg="#1C1C1E",
            fg="#D7D7DC",
            selectcolor="#2E2E34",
            activebackground="#1C1C1E",
            activeforeground="#F0F0F2",
            font=("Segoe UI", 9),
            cursor="hand2",
        )

    @staticmethod
    def _make_button(parent: tk.Widget, text: str, command, width: int) -> tk.Button:
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg="#2E2E34",
            fg="#F0F0F2",
            activebackground="#3E3E46",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            pady=5,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        btn.bind("<Enter>", lambda _: btn.configure(bg="#3E3E46"))
        btn.bind("<Leave>", lambda _: btn.configure(bg="#2E2E34"))
        return btn

    def _fit_and_center_window(self) -> None:
        self.root.update_idletasks()
        w = max(456, self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _create_tray_icon(self) -> Any:
        menu = pystray.Menu(
            pystray.MenuItem(
                "Відкрити палітру кольорів",
                lambda: self.root.after(0, self.toggle_window),
                default=True,
            ),
            pystray.MenuItem(
                "Оновити зі шпалер",
                lambda: self.root.after(0, lambda: self.sync_from_wallpaper(force_apply=True)),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Змінювати курсор",
                lambda: self.root.after(0, self._toggle_cursor_from_tray),
                checked=lambda _: self.enable_cursor.get(),
            ),
            pystray.MenuItem(
                "Змінювати виділення",
                lambda: self.root.after(0, self._toggle_selection_from_tray),
                checked=lambda _: self.enable_selection.get(),
            ),
            pystray.MenuItem(
                "Окремі кольори (курсор / виділення)",
                lambda: self.root.after(0, self._toggle_separate_from_tray),
                checked=lambda _: self.separate_colors.get(),
            ),
            pystray.MenuItem(
                "Автопідбір зі шпалер",
                lambda: self.root.after(0, self._toggle_auto_apply_from_tray),
                checked=lambda _: self.auto_apply.get(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Скинути до стандартних",
                lambda: self.root.after(0, self.reset_defaults),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Вихід",
                lambda: self.root.after(0, self.quit_app),
            ),
        )
        return pystray.Icon(
            APP_NAME,
            create_tray_icon_image(self.cursor_color, self.selection_color),
            "Wallpaper Accent Sync",
            menu,
        )

    def _toggle_cursor_from_tray(self) -> None:
        self.enable_cursor.set(not self.enable_cursor.get())
        self._on_targets_toggled()

    def _toggle_selection_from_tray(self) -> None:
        self.enable_selection.set(not self.enable_selection.get())
        self._on_targets_toggled()

    def _toggle_separate_from_tray(self) -> None:
        self.separate_colors.set(not self.separate_colors.get())
        self._on_separate_colors_toggled()

    def _toggle_auto_apply_from_tray(self) -> None:
        self.auto_apply.set(not self.auto_apply.get())
        self._on_auto_apply_toggled()

    def _set_active_target(self, target: str) -> None:
        self.active_target = target
        self._refresh_status_area()

    def _on_targets_toggled(self) -> None:
        save_bool_setting("EnableCursor", self.enable_cursor.get())
        save_bool_setting("EnableSelection", self.enable_selection.get())
        self._apply_current_targets()

    def _on_separate_colors_toggled(self) -> None:
        is_sep = self.separate_colors.get()
        save_bool_setting("SeparateColors", is_sep)

        if not is_sep:
            self.selection_color = self.cursor_color
            self._apply_current_targets()
        elif self.auto_apply.get() and len(self.palette) >= 2:
            self.selection_color = self.palette[1]
            self._apply_current_targets()

        self._refresh_status_area()
        self._render_palette_cards()
        self._update_tray_appearance()
        self._fit_and_center_window()

    def _on_auto_apply_toggled(self) -> None:
        enabled = self.auto_apply.get()
        save_bool_setting("AutoApply", enabled)
        if enabled and self.palette:
            self._apply_palette_automatically()

    def _on_autostart_toggled(self) -> None:
        set_autostart_enabled(self.autostart.get())

    def _apply_current_targets(self) -> None:
        if self.enable_cursor.get():
            apply_cursor_color(self.cursor_color)
        else:
            reset_cursor_to_default()

        if self.enable_selection.get():
            apply_selection_color(self.selection_color)
        else:
            reset_selection_to_default()

        save_active_accent_meta(self.cursor_color, self.selection_color)

    def _apply_palette_automatically(self) -> None:
        if not self.palette:
            return
        self.cursor_color = self.palette[0]
        if self.separate_colors.get() and len(self.palette) >= 2:
            self.selection_color = self.palette[1]
        else:
            self.selection_color = self.palette[0]

        self._apply_current_targets()
        self._refresh_status_area()
        self._render_palette_cards()
        self._update_tray_appearance()

    def _poll_wallpaper_changes(self) -> None:
        sig = get_wallpaper_signature()
        if sig != self.last_signature and sig[0] is not None:
            self.sync_from_wallpaper(force_apply=self.auto_apply.get())
        self.root.after(1500, self._poll_wallpaper_changes)

    def sync_from_wallpaper(self, force_apply: bool) -> None:
        sig = get_wallpaper_signature()
        path = sig[0]
        if not path:
            return

        try:
            img = load_wallpaper_image(path)
            self.last_signature = sig

            thumb = img.copy()
            thumb.thumbnail((420, 151), Image.Resampling.LANCZOS)
            self.preview_photo = ImageTk.PhotoImage(thumb)
            self.preview_label.configure(image=self.preview_photo, text="")

            self.palette = extract_wallpaper_palette(img, max_colors=6)

            if force_apply and self.palette:
                self._apply_palette_automatically()
            else:
                self._refresh_status_area()
                self._render_palette_cards()
                self._update_tray_appearance()
        except Exception:
            pass

    def select_color(self, rgb: tuple[int, int, int]) -> None:
        if self.separate_colors.get():
            if self.active_target == "cursor":
                self.cursor_color = rgb
            else:
                self.selection_color = rgb
        else:
            self.cursor_color = rgb
            self.selection_color = rgb

        self._apply_current_targets()
        self._refresh_status_area()
        self._render_palette_cards()
        self._update_tray_appearance()

    def reset_defaults(self) -> None:
        default_rgb = (0, 120, 215)
        self.cursor_color = default_rgb
        self.selection_color = default_rgb
        reset_cursor_to_default()
        reset_selection_to_default()
        save_active_accent_meta(default_rgb, default_rgb)

        self._refresh_status_area()
        self._render_palette_cards()
        if hasattr(self, "tray_icon"):
            self.tray_icon.icon = create_tray_icon_image(default_rgb, default_rgb)
            self.tray_icon.title = "Wallpaper Accent: Стандартний"

    def pick_custom_color(self) -> None:
        initial = (
            self.selection_color
            if (self.separate_colors.get() and self.active_target == "selection")
            else self.cursor_color
        )
        target_name = (
            "для виділення"
            if (self.separate_colors.get() and self.active_target == "selection")
            else ("для курсора" if self.separate_colors.get() else "акцентний колір")
        )
        chosen = colorchooser.askcolor(
            color=rgb_to_hex(initial),
            title=f"Обери колір {target_name}",
            parent=self.root,
        )
        if chosen and chosen[0]:
            r, g, b = (int(round(c)) for c in chosen[0])
            self.select_color((r, g, b))

    def _refresh_status_area(self) -> None:
        if self.separate_colors.get():
            self.unified_status_frame.pack_forget()
            self.split_status_frame.pack(fill="x")

            cur_hex = rgb_to_hex(self.cursor_color)
            sel_hex = rgb_to_hex(self.selection_color)

            self.cursor_badge.configure(bg=cur_hex)
            self.cursor_target_lbl.configure(text=f"Курсор: {cur_hex}")

            self.selection_badge.configure(bg=sel_hex)
            self.selection_target_lbl.configure(text=f"Виділення: {sel_hex}")

            cur_active = self.active_target == "cursor"
            self.cursor_target_card.configure(
                bg="#34343A" if cur_active else "#242428",
                highlightbackground="#FFFFFF" if cur_active else "#3A3A40",
            )
            self.cursor_target_lbl.configure(bg="#34343A" if cur_active else "#242428")

            self.selection_target_card.configure(
                bg="#34343A" if not cur_active else "#242428",
                highlightbackground="#FFFFFF" if not cur_active else "#3A3A40",
            )
            self.selection_target_lbl.configure(bg="#34343A" if not cur_active else "#242428")
        else:
            self.split_status_frame.pack_forget()
            self.unified_status_frame.pack(fill="x")

            hex_code = rgb_to_hex(self.cursor_color)
            self.unified_badge.configure(bg=hex_code)
            self.unified_label.configure(
                text=f"Активний акцент: {hex_code}  (RGB: {self.cursor_color[0]}, {self.cursor_color[1]}, {self.cursor_color[2]})"
            )

    def _update_tray_appearance(self) -> None:
        if not hasattr(self, "tray_icon"):
            return
        self.tray_icon.icon = create_tray_icon_image(self.cursor_color, self.selection_color)
        if self.separate_colors.get() and self.cursor_color != self.selection_color:
            self.tray_icon.title = (
                f"Курсор: {rgb_to_hex(self.cursor_color)} | Виділення: {rgb_to_hex(self.selection_color)}"
            )
        else:
            self.tray_icon.title = f"Wallpaper Accent: {rgb_to_hex(self.cursor_color)}"

    def _render_palette_cards(self) -> None:
        for child in self.cards_frame.winfo_children():
            child.destroy()

        is_sep = self.separate_colors.get()

        for idx, rgb in enumerate(self.palette):
            is_cur = rgb == self.cursor_color
            is_sel = rgb == self.selection_color
            is_selected = (is_cur or is_sel) if is_sep else is_cur
            hex_code = rgb_to_hex(rgb)

            card = tk.Frame(
                self.cards_frame,
                bg="#34343A" if is_selected else "#26262A",
                highlightbackground="#FFFFFF" if is_selected else "#3A3A40",
                highlightthickness=1,
                padx=6,
                pady=6,
                cursor="hand2",
            )
            card.pack(side="left", padx=(0 if idx == 0 else 6, 0))

            swatch = tk.Frame(
                card,
                width=50,
                height=42,
                bg=hex_code,
                highlightbackground="#1C1C1E" if is_selected else "#4A4A52",
                highlightthickness=1,
                cursor="hand2",
            )
            swatch.pack()
            swatch.pack_propagate(False)

            if is_sep and (is_cur or is_sel):
                badge_text = "К+В" if (is_cur and is_sel) else ("К" if is_cur else "В")
                tag = tk.Label(
                    swatch,
                    text=badge_text,
                    bg="#1C1C1E",
                    fg="#FFFFFF",
                    font=("Segoe UI", 7, "bold"),
                    padx=3,
                    pady=0,
                    cursor="hand2",
                )
                tag.place(relx=1.0, rely=0.0, anchor="ne")
                tag.bind("<Button-1>", lambda _, c=rgb: self.select_color(c))

            lbl = tk.Label(
                card,
                text=hex_code,
                bg=card["bg"],
                fg="#FFFFFF" if is_selected else "#B8B8BE",
                font=("Consolas", 8, "bold" if is_selected else "normal"),
                cursor="hand2",
            )
            lbl.pack(pady=(5, 0))

            for widget in (card, swatch, lbl):
                widget.bind("<Button-1>", lambda _, c=rgb: self.select_color(c))

    def toggle_window(self) -> None:
        if self.root.state() == "withdrawn" or not self.root.winfo_viewable():
            self.show_window()
        else:
            self.hide_window()

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self) -> None:
        self.root.withdraw()

    def quit_app(self) -> None:
        self.tray_icon.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    WallpaperAccentApp().run()