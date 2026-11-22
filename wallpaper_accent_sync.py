import colorsys
import ctypes
import io
import os
import shutil
import struct
import sys
import threading
import tkinter as tk
from tkinter import colorchooser, messagebox
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
# Недокументований виклик, яким Параметри Windows застосовують CursorBaseSize без перезаходу
SPI_SETCURSORBASESIZE = 0x2029
SPIF_SENDCHANGE = 0x02

HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002

COLOR_HIGHLIGHT = 13
COLOR_HOTLIGHT = 26

APP_NAME = "WallpaperAccentSync"
APP_REG_PATH = rf"Software\{APP_NAME}"
BACKUP_REG_PATH = rf"Software\{APP_NAME}\Backup"
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
CURSORS_REG_PATH = r"Control Panel\Cursors"
COLORS_REG_PATH = r"Control Panel\Colors"
ACCESSIBILITY_REG_PATH = r"Software\Microsoft\Accessibility"
ERROR_ALREADY_EXISTS = 183
ORIGINAL_BASE_SIZE_SETTING = "OriginalCursorBaseSize"  # розмір курсора до ручного вибору в програмі

APP_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    APP_NAME,
)
CURSOR_STORAGE_DIR = os.path.join(APP_DATA_DIR, "cursors")

CURSOR_SIZE_PRESETS: tuple[int, ...] = (24, 32, 40, 48, 64)

ALL_CURSOR_ROLES: tuple[str, ...] = (
    "Arrow",
    "Help",
    "AppStarting",
    "Wait",
    "Crosshair",
    "IBeam",
    "NWPen",
    "No",
    "SizeNS",
    "SizeWE",
    "SizeNWSE",
    "SizeNESW",
    "SizeAll",
    "UpArrow",
    "Hand",
    "Pin",
    "Person",
)

FALLBACK_DEFAULT_CURSORS: dict[str, tuple[str, int]] = {
    "": ("Windows Default", winreg.REG_SZ),
    "Arrow": (r"%SystemRoot%\cursors\aero_arrow.cur", winreg.REG_EXPAND_SZ),
    "Help": (r"%SystemRoot%\cursors\aero_helpsel.cur", winreg.REG_EXPAND_SZ),
    "AppStarting": (r"%SystemRoot%\cursors\aero_working.ani", winreg.REG_EXPAND_SZ),
    "Wait": (r"%SystemRoot%\cursors\aero_busy.ani", winreg.REG_EXPAND_SZ),
    "Crosshair": ("", winreg.REG_SZ),
    "IBeam": ("", winreg.REG_SZ),
    "NWPen": (r"%SystemRoot%\cursors\aero_pen.cur", winreg.REG_EXPAND_SZ),
    "No": (r"%SystemRoot%\cursors\aero_unavail.cur", winreg.REG_EXPAND_SZ),
    "SizeNS": (r"%SystemRoot%\cursors\aero_ns.cur", winreg.REG_EXPAND_SZ),
    "SizeWE": (r"%SystemRoot%\cursors\aero_ew.cur", winreg.REG_EXPAND_SZ),
    "SizeNWSE": (r"%SystemRoot%\cursors\aero_nwse.cur", winreg.REG_EXPAND_SZ),
    "SizeNESW": (r"%SystemRoot%\cursors\aero_nesw.cur", winreg.REG_EXPAND_SZ),
    "SizeAll": (r"%SystemRoot%\cursors\aero_move.cur", winreg.REG_EXPAND_SZ),
    "UpArrow": (r"%SystemRoot%\cursors\aero_up.cur", winreg.REG_EXPAND_SZ),
    "Hand": (r"%SystemRoot%\cursors\aero_link.cur", winreg.REG_EXPAND_SZ),
    "Pin": (r"%SystemRoot%\cursors\aero_pin.cur", winreg.REG_EXPAND_SZ),
    "Person": (r"%SystemRoot%\cursors\aero_person.cur", winreg.REG_EXPAND_SZ),
}

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


def _broadcast_worker(area: str) -> None:
    try:
        result = ctypes.c_ulong()
        user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ctypes.c_wchar_p(area),
            SMTO_ABORTIFHUNG,
            1000,
            ctypes.byref(result),
        )
    except Exception:
        pass


def broadcast_settings_change(area: str = "Colors") -> None:
    """Сповіщає вікна про зміну налаштувань у фоні, щоб повільне вікно не вішало інтерфейс."""
    threading.Thread(target=_broadcast_worker, args=(area,), daemon=True).start()


def get_system_cursor_base_size() -> int:
    """Зчитує базовий розмір курсора Windows із реєстру (за замовчуванням 32 px)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CURSORS_REG_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, "CursorBaseSize")
            if isinstance(val, int) and 16 <= val <= 256:
                return val
    except OSError:
        pass
    return 32


def _write_cursor_size_level(px: int) -> None:
    """Повзунок «Розмір вказівника» в Windows: рівень 1..15, де базовий розмір = 32 + 16 * (рівень - 1)."""
    level = max(1, min(15, round((px - 32) / 16) + 1))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ACCESSIBILITY_REG_PATH) as key:
        winreg.SetValueEx(key, "CursorSize", 0, winreg.REG_DWORD, level)


def _apply_base_size_live(px: int) -> None:
    """Просить Windows одразу застосувати новий базовий розмір курсорів."""
    try:
        user32.SystemParametersInfoW(SPI_SETCURSORBASESIZE, 0, ctypes.c_void_p(int(px)), SPIF_UPDATEINIFILE)
    except Exception:
        pass


def set_system_cursor_base_size(px: int) -> None:
    """Windows масштабує курсори за CursorBaseSize, тому ручний розмір записуємо саме сюди
    (і в Accessibility\\CursorSize, який читає сучасний інтерфейс налаштувань Windows)."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CURSORS_REG_PATH) as key:
        winreg.SetValueEx(key, "CursorBaseSize", 0, winreg.REG_DWORD, int(px))
    _write_cursor_size_level(int(px))
    _apply_base_size_live(int(px))


# --- Резервне копіювання початкового стану системи ---

_backup_verified = False
_instance_mutex: int | None = None


def _load_backup() -> dict[str, tuple[Any, int]]:
    """Зчитує всі резервні значення одним проходом: {ім'я: (значення, тип)}."""
    result: dict[str, tuple[Any, int]] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BACKUP_REG_PATH, 0, winreg.KEY_READ) as bkp:
            index = 0
            while True:
                try:
                    name, val, reg_type = winreg.EnumValue(bkp, index)
                except OSError:
                    break
                result[name] = (val, reg_type)
                index += 1
    except OSError:
        pass
    return result


def _backup_value(bkp: Any, src_key: Any, name: str, save_name: str, skip_own: bool = False) -> None:
    """Копіює значення name з src_key у резервний ключ, якщо його там ще немає."""
    missing_name = f"{save_name}_Missing"
    for check_key in (save_name, missing_name):
        try:
            winreg.QueryValueEx(bkp, check_key)
            return
        except FileNotFoundError:
            pass

    try:
        val, reg_type = winreg.QueryValueEx(src_key, name)
    except FileNotFoundError:
        winreg.SetValueEx(bkp, missing_name, 0, winreg.REG_DWORD, 1)
        return
    # Власні файли курсорів програми не є «початковим» станом
    if skip_own and isinstance(val, str) and APP_NAME in val:
        return
    winreg.SetValueEx(bkp, save_name, 0, reg_type, val)


def _backup_from_key(bkp: Any, reg_path: str, items: list[tuple[str, str, bool]]) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_READ) as src:
            for name, save_name, skip_own in items:
                _backup_value(bkp, src, name, save_name, skip_own)
    except OSError:
        pass


def ensure_initial_backup() -> None:
    """Зберігає початкові курсори (усі 17 станів, розмір, схему), стиль вказівника та кольори виділення."""
    global _backup_verified
    if _backup_verified:
        return

    cursor_items: list[tuple[str, str, bool]] = [
        (role, "Cursor_Scheme" if role == "" else f"Cursor_{role}", True)
        for role in ("",) + ALL_CURSOR_ROLES
    ]
    cursor_items.append(("CursorBaseSize", "CursorBaseSize", False))
    cursor_items.append(("Scheme Source", "Cursor_SchemeSource", False))

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, BACKUP_REG_PATH, 0, winreg.KEY_READ | winreg.KEY_WRITE
    ) as bkp:
        _backup_from_key(bkp, CURSORS_REG_PATH, cursor_items)
        _backup_from_key(bkp, ACCESSIBILITY_REG_PATH, [("CursorType", "Accessibility_CursorType", False)])
        _backup_from_key(
            bkp,
            COLORS_REG_PATH,
            [("Hilight", "Color_Hilight", False), ("HotTrackingColor", "Color_HotTrackingColor", False)],
        )
        winreg.SetValueEx(bkp, "Initialized", 0, winreg.REG_DWORD, 1)

    _backup_verified = True


def cleanup_cursor_files() -> None:
    try:
        if os.path.isdir(CURSOR_STORAGE_DIR):
            shutil.rmtree(CURSOR_STORAGE_DIR, ignore_errors=True)
    except Exception:
        pass


# --- Генерація статичних (.cur) та анімованих (.ani) курсорів Windows ---

def scale_hotspot(hotspot_32: tuple[int, int], target_size: int) -> tuple[int, int]:
    scale = target_size / 32.0
    return (
        max(0, min(target_size - 1, round(hotspot_32[0] * scale))),
        max(0, min(target_size - 1, round(hotspot_32[1] * scale))),
    )


def _cur_dib(img: Image.Image) -> bytes:
    """Одне зображення курсора у вигляді DIB (BITMAPINFOHEADER + піксели + AND-маска)."""
    img = img.convert("RGBA")
    w, h = img.size

    # DIB зберігається знизу вгору й у порядку BGRA — це робить сам Pillow (швидко, без циклів)
    xor_rows = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM).tobytes("raw", "BGRA")

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
    return bih + xor_rows + and_rows


def build_multi_cur_bytes(images: list[tuple[Image.Image, int, int]]) -> bytes:
    """Кодує набір зображень (img, hotspot_x, hotspot_y) у один файл .cur з кількома розмірами."""
    blobs = [_cur_dib(img) for img, _, _ in images]
    offset = 6 + 16 * len(images)
    entries = bytearray()
    for (img, hx, hy), blob in zip(images, blobs):
        w, h = img.size
        entries.extend(
            struct.pack(
                "<BBBBHHII", w if w < 256 else 0, h if h < 256 else 0, 0, 0, hx, hy, len(blob), offset
            )
        )
        offset += len(blob)
    return struct.pack("<HHH", 0, 2, len(images)) + bytes(entries) + b"".join(blobs)


def build_cur_bytes(img: Image.Image, hotspot_x: int = 0, hotspot_y: int = 0) -> bytes:
    """Кодує RGBA-зображення у бінарний формат курсора Windows (.cur)."""
    return build_multi_cur_bytes([(img, hotspot_x, hotspot_y)])


def save_cur_file(img: Image.Image, path: str, hotspot_x: int = 0, hotspot_y: int = 0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(build_cur_bytes(img, hotspot_x=hotspot_x, hotspot_y=hotspot_y))


# Windows сама обирає зображення, найближче до потрібного їй розміру, тож у файл кладемо кілька
CURSOR_IMAGE_SIZES: tuple[int, ...] = (32, 48, 64)


def save_cursor_canvas(
    canvas: Image.Image, path: str, hotspot_32: tuple[int, int], target_size: int
) -> None:
    """Зберігає 128-px полотно як .cur одразу в кількох розмірах (цільовий + стандартні)."""
    images = []
    for s in sorted({target_size, *CURSOR_IMAGE_SIZES}):
        hx, hy = scale_hotspot(hotspot_32, s)
        images.append((canvas.resize((s, s), Image.Resampling.LANCZOS), hx, hy))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(build_multi_cur_bytes(images))


def save_ani_file(
    frames: list[Image.Image],
    path: str,
    hotspot_x: int,
    hotspot_y: int,
    jiffies_per_frame: int = 5,
) -> None:
    """Зберігає серію RGBA-кадрів в анімований курсор Windows (.ani)."""
    n_frames = len(frames)
    icon_chunks = bytearray()
    for frame in frames:
        cur_data = build_cur_bytes(frame, hotspot_x=hotspot_x, hotspot_y=hotspot_y)
        icon_chunks.extend(b"icon")
        icon_chunks.extend(struct.pack("<I", len(cur_data)))
        icon_chunks.extend(cur_data)
        if len(cur_data) % 2 == 1:
            icon_chunks.append(0)

    fram_payload = b"fram" + bytes(icon_chunks)
    list_chunk = b"LIST" + struct.pack("<I", len(fram_payload)) + fram_payload

    anih_data = struct.pack(
        "<9I",
        36,
        n_frames,
        n_frames,
        0,
        0,
        0,
        0,
        jiffies_per_frame,
        1,
    )
    anih_chunk = b"anih" + struct.pack("<I", 36) + anih_data

    riff_payload = b"ACON" + anih_chunk + list_chunk
    ani_bytes = b"RIFF" + struct.pack("<I", len(riff_payload)) + riff_payload

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(ani_bytes)


def _get_cursor_colors(fill_rgb: tuple[int, int, int]) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    is_bright = get_perceived_brightness(fill_rgb) > 0.42
    outline_rgba = (20, 20, 24, 255) if is_bright else (250, 250, 252, 255)
    fill_rgba = (*fill_rgb, 255)
    return fill_rgba, outline_rgba


def _draw_outlined_polygon(
    draw: ImageDraw.ImageDraw,
    pts: list[tuple[int, int]],
    fill_rgba: tuple[int, int, int, int],
    outline_rgba: tuple[int, int, int, int],
    outline_width: int = 7,
) -> None:
    draw.polygon(pts, fill=fill_rgba)
    draw.line(pts + [pts[0]], fill=outline_rgba, width=outline_width, joint="curve")


def _draw_spinner_ring(
    canvas: Image.Image,
    cx: int,
    cy: int,
    outer_r: int,
    inner_r: int,
    angle_deg: float,
    fill_rgba: tuple[int, int, int, int],
    outline_rgba: tuple[int, int, int, int],
) -> None:
    """Малює кільце завантаження з контрастною обводкою та рухомим сектором."""
    ring = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    r_draw = ImageDraw.Draw(ring)

    r_draw.ellipse(
        (cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r),
        fill=outline_rgba,
    )
    track_r = outer_r - 4
    track_fill = (fill_rgba[0], fill_rgba[1], fill_rgba[2], 95)
    r_draw.ellipse(
        (cx - track_r, cy - track_r, cx + track_r, cy + track_r),
        fill=track_fill,
    )
    r_draw.pieslice(
        (cx - track_r, cy - track_r, cx + track_r, cy + track_r),
        start=angle_deg,
        end=angle_deg + 145,
        fill=fill_rgba,
    )
    r_draw.ellipse(
        (cx - inner_r - 4, cy - inner_r - 4, cx + inner_r + 4, cy + inner_r + 4),
        fill=outline_rgba,
    )
    r_draw.ellipse(
        (cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r),
        fill=(0, 0, 0, 0),
    )

    canvas.alpha_composite(ring)


def _render_polygon_cur(
    pts_128: list[tuple[int, int]],
    rgb: tuple[int, int, int],
    hotspot_32: tuple[int, int],
    out_path: str,
    target_size: int = 32,
    outline_width: int = 7,
) -> None:
    fill_rgba, outline_rgba = _get_cursor_colors(rgb)
    canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    _draw_outlined_polygon(draw, pts_128, fill_rgba, outline_rgba, outline_width=outline_width)
    save_cursor_canvas(canvas, out_path, hotspot_32, target_size)


def generate_colored_cursors(rgb: tuple[int, int, int], target_size: int = 32) -> dict[str, str]:
    """Створює повний набір з 17 системних курсорів із заданим акцентним кольором і розміром."""
    size = max(16, min(128, int(target_size)))
    fill_rgba, outline_rgba = _get_cursor_colors(rgb)
    paths: dict[str, str] = {}

    def p(filename: str) -> str:
        return os.path.join(CURSOR_STORAGE_DIR, filename)

    # 1. Arrow (Основний режим — пропорції наближені до стандартного курсора Windows)
    arrow_pts = [(12, 8), (12, 88), (31, 70), (44, 100), (56, 95), (43, 65), (70, 65)]
    paths["Arrow"] = p("accent_arrow.cur")
    _render_polygon_cur(arrow_pts, rgb, (3, 2), paths["Arrow"], target_size=size, outline_width=6)

    # 2. Help (Вибір довідки: стрілка + знак питання)
    paths["Help"] = p("accent_help.cur")
    c_help = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d_help = ImageDraw.Draw(c_help)
    small_arrow = [(10, 8), (10, 76), (26, 61), (37, 87), (47, 83), (36, 57), (59, 57)]
    _draw_outlined_polygon(d_help, small_arrow, fill_rgba, outline_rgba, outline_width=6)
    q_top = [
        (68, 50), (83, 43), (99, 46), (105, 59), (101, 72),
        (89, 79), (89, 85), (78, 85), (78, 72), (90, 65),
        (92, 58), (85, 53), (76, 57), (68, 57),
    ]
    _draw_outlined_polygon(d_help, q_top, fill_rgba, outline_rgba, outline_width=5)
    d_help.ellipse((76, 91, 91, 106), fill=fill_rgba, outline=outline_rgba, width=5)
    hx, hy = scale_hotspot((3, 2), size)
    save_cursor_canvas(c_help, paths["Help"], (3, 2), size)

    # 3. AppStarting (Фоновий режим: стрілка + анімоване кільце завантаження)
    paths["AppStarting"] = p("accent_appstarting.ani")
    app_frames: list[Image.Image] = []
    for step in range(8):
        frame = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        d_f = ImageDraw.Draw(frame)
        _draw_outlined_polygon(d_f, small_arrow, fill_rgba, outline_rgba, outline_width=6)
        _draw_spinner_ring(frame, cx=82, cy=80, outer_r=23, inner_r=11, angle_deg=step * 45, fill_rgba=fill_rgba, outline_rgba=outline_rgba)
        app_frames.append(frame.resize((size, size), Image.Resampling.LANCZOS))
    save_ani_file(app_frames, paths["AppStarting"], hotspot_x=hx, hotspot_y=hy, jiffies_per_frame=5)

    # 4. Wait (Зайнятий / Завантаження: центроване анімоване кільце)
    paths["Wait"] = p("accent_wait.ani")
    wait_frames: list[Image.Image] = []
    for step in range(8):
        frame = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        _draw_spinner_ring(frame, cx=64, cy=64, outer_r=38, inner_r=20, angle_deg=step * 45, fill_rgba=fill_rgba, outline_rgba=outline_rgba)
        wait_frames.append(frame.resize((size, size), Image.Resampling.LANCZOS))
    chx, chy = scale_hotspot((16, 16), size)
    save_ani_file(wait_frames, paths["Wait"], hotspot_x=chx, hotspot_y=chy, jiffies_per_frame=5)

    # 5. Crosshair (Графічне виділення)
    cross_pts = [
        (58, 20), (70, 20), (70, 58), (108, 58), (108, 70), (70, 70),
        (70, 108), (58, 108), (58, 70), (20, 70), (20, 58), (58, 58),
    ]
    paths["Crosshair"] = p("accent_cross.cur")
    _render_polygon_cur(cross_pts, rgb, (16, 16), paths["Crosshair"], target_size=size, outline_width=6)

    # 6. IBeam (Виділення тексту)
    ibeam_pts = [
        (46, 20), (82, 20), (82, 29), (69, 29), (69, 99), (82, 99),
        (82, 108), (46, 108), (46, 99), (59, 99), (59, 29), (46, 29),
    ]
    paths["IBeam"] = p("accent_ibeam.cur")
    _render_polygon_cur(ibeam_pts, rgb, (16, 16), paths["IBeam"], target_size=size, outline_width=5)

    # 7. NWPen (Рукописний ввід)
    pen_pts = [(16, 112), (36, 106), (102, 40), (88, 26), (22, 92)]
    paths["NWPen"] = p("accent_nwpen.cur")
    _render_polygon_cur(pen_pts, rgb, (4, 28), paths["NWPen"], target_size=size, outline_width=6)

    # 8. No (Недоступно: перекреслене кільце)
    paths["No"] = p("accent_no.cur")
    c_no = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    no_ring = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d_no = ImageDraw.Draw(no_ring)
    d_no.ellipse((24, 24, 104, 104), fill=outline_rgba)
    d_no.ellipse((29, 29, 99, 99), fill=fill_rgba)
    d_no.ellipse((40, 40, 88, 88), fill=outline_rgba)
    d_no.ellipse((45, 45, 83, 83), fill=(0, 0, 0, 0))
    c_no.alpha_composite(no_ring)
    slash_pts = [(36, 44), (44, 36), (92, 84), (84, 92)]
    _draw_outlined_polygon(ImageDraw.Draw(c_no), slash_pts, fill_rgba, outline_rgba, outline_width=5)
    save_cursor_canvas(c_no, paths["No"], (16, 16), size)

    # 9. SizeNS (Вертикальна зміна розміру)
    size_ns_pts = [
        (64, 16), (84, 38), (70, 38), (70, 90), (84, 90),
        (64, 112), (44, 90), (58, 90), (58, 38), (44, 38),
    ]
    paths["SizeNS"] = p("accent_sizens.cur")
    _render_polygon_cur(size_ns_pts, rgb, (16, 16), paths["SizeNS"], target_size=size, outline_width=6)

    # 10. SizeWE (Горизонтальна зміна розміру)
    size_we_pts = [
        (16, 64), (38, 44), (38, 58), (90, 58), (90, 44),
        (112, 64), (90, 84), (90, 70), (38, 70), (38, 84),
    ]
    paths["SizeWE"] = p("accent_sizewe.cur")
    _render_polygon_cur(size_we_pts, rgb, (16, 16), paths["SizeWE"], target_size=size, outline_width=6)

    # 11. SizeNWSE (Діагональна зміна розміру 1)
    size_nwse_pts = [
        (26, 26), (58, 30), (48, 40), (88, 80), (98, 70),
        (102, 102), (70, 98), (80, 88), (40, 48), (30, 58),
    ]
    paths["SizeNWSE"] = p("accent_sizenwse.cur")
    _render_polygon_cur(size_nwse_pts, rgb, (16, 16), paths["SizeNWSE"], target_size=size, outline_width=6)

    # 12. SizeNESW (Діагональна зміна розміру 2)
    size_nesw_pts = [
        (102, 26), (70, 30), (80, 40), (40, 80), (30, 70),
        (26, 102), (58, 98), (48, 88), (88, 48), (98, 58),
    ]
    paths["SizeNESW"] = p("accent_sizenesw.cur")
    _render_polygon_cur(size_nesw_pts, rgb, (16, 16), paths["SizeNESW"], target_size=size, outline_width=6)

    # 13. SizeAll (Переміщення)
    size_all_pts = [
        (64, 14), (80, 32), (70, 32), (70, 58), (96, 58), (96, 48),
        (114, 64), (96, 80), (96, 70), (70, 70), (70, 96), (80, 96),
        (64, 114), (48, 96), (58, 96), (58, 70), (32, 70), (32, 80),
        (14, 64), (32, 48), (32, 58), (58, 58), (58, 32), (48, 32),
    ]
    paths["SizeAll"] = p("accent_sizeall.cur")
    _render_polygon_cur(size_all_pts, rgb, (16, 16), paths["SizeAll"], target_size=size, outline_width=5)

    # 14. UpArrow (Альтернативний вибір)
    up_arrow_pts = [(64, 16), (88, 46), (72, 46), (72, 108), (56, 108), (56, 46), (40, 46)]
    paths["UpArrow"] = p("accent_uparrow.cur")
    _render_polygon_cur(up_arrow_pts, rgb, (16, 4), paths["UpArrow"], target_size=size, outline_width=6)

    # 15. Hand (Вибір посилання)
    hand_pts = [
        (42, 10), (52, 10), (52, 42), (64, 42), (64, 46), (76, 46), (76, 51),
        (87, 51), (87, 76), (80, 94), (36, 94), (22, 68), (29, 59), (42, 64),
    ]
    paths["Hand"] = p("accent_hand.cur")
    _render_polygon_cur(hand_pts, rgb, (11, 2), paths["Hand"], target_size=size, outline_width=6)

    # 16. Pin (Вибір розташування)
    paths["Pin"] = p("accent_pin.cur")
    c_pin = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d_pin = ImageDraw.Draw(c_pin)
    _draw_outlined_polygon(d_pin, small_arrow, fill_rgba, outline_rgba, outline_width=6)
    pin_pts = [(85, 48), (100, 55), (103, 71), (85, 104), (67, 71), (70, 55)]
    _draw_outlined_polygon(d_pin, pin_pts, fill_rgba, outline_rgba, outline_width=5)
    d_pin.ellipse((79, 61, 91, 73), fill=outline_rgba)
    save_cursor_canvas(c_pin, paths["Pin"], (3, 2), size)

    # 17. Person (Вибір користувача)
    paths["Person"] = p("accent_person.cur")
    c_per = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d_per = ImageDraw.Draw(c_per)
    _draw_outlined_polygon(d_per, small_arrow, fill_rgba, outline_rgba, outline_width=6)
    shoulders_pts = [(64, 104), (68, 86), (79, 80), (93, 80), (104, 86), (108, 104)]
    _draw_outlined_polygon(d_per, shoulders_pts, fill_rgba, outline_rgba, outline_width=5)
    d_per.ellipse((74, 50, 98, 74), fill=fill_rgba, outline=outline_rgba, width=5)
    save_cursor_canvas(c_per, paths["Person"], (3, 2), size)

    return paths


def apply_cursor_color(rgb: tuple[int, int, int], target_size: int = 32) -> None:
    ensure_initial_backup()
    paths = generate_colored_cursors(rgb, target_size=target_size)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ACCESSIBILITY_REG_PATH) as key:
        winreg.SetValueEx(key, "CursorType", 0, winreg.REG_DWORD, 0)

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CURSORS_REG_PATH) as key:
        for role, cur_path in paths.items():
            winreg.SetValueEx(key, role, 0, winreg.REG_SZ, cur_path)

    user32.SystemParametersInfoW(
        SPI_SETCURSORS, 0, None, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    )


def reset_cursor_to_default() -> None:
    backup = _load_backup()

    # Повертаємо початковий стиль вказівника (білий/чорний/інверсний), а не завжди 0
    saved_type = backup.get("Accessibility_CursorType", (0,))[0]
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, ACCESSIBILITY_REG_PATH) as key:
        winreg.SetValueEx(
            key, "CursorType", 0, winreg.REG_DWORD, saved_type if isinstance(saved_type, int) else 0
        )

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CURSORS_REG_PATH) as cur_key:
        for role in ("",) + ALL_CURSOR_ROLES:
            save_name = "Cursor_Scheme" if role == "" else f"Cursor_{role}"
            saved = backup.get(save_name)
            if saved and isinstance(saved[0], str) and APP_NAME not in saved[0]:
                val, reg_type = saved
            else:
                val, reg_type = FALLBACK_DEFAULT_CURSORS[role]
            winreg.SetValueEx(cur_key, role, 0, reg_type, val)

        # Розмір, який користувач мав до ручного вибору в програмі, актуальніший за початковий backup
        orig_size = load_int_setting(ORIGINAL_BASE_SIZE_SETTING, 0)
        final_base: int | None = None
        if orig_size:
            final_base = orig_size
            save_int_setting(ORIGINAL_BASE_SIZE_SETTING, 0)
        elif backup:
            base = backup.get("CursorBaseSize")
            final_base = base[0] if base and isinstance(base[0], int) else 32
        if final_base is not None:
            winreg.SetValueEx(cur_key, "CursorBaseSize", 0, winreg.REG_DWORD, final_base)

        source = backup.get("Cursor_SchemeSource", (2,))[0]
        winreg.SetValueEx(
            cur_key, "Scheme Source", 0, winreg.REG_DWORD, source if isinstance(source, int) else 2
        )

    if final_base is not None:
        _write_cursor_size_level(final_base)
        _apply_base_size_live(final_base)

    cleanup_cursor_files()
    user32.SystemParametersInfoW(
        SPI_SETCURSORS, 0, None, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    )


# --- Системні кольори виділення, шпалери та налаштування ---

def apply_selection_color(rgb: tuple[int, int, int]) -> None:
    ensure_initial_backup()
    r, g, b = rgb
    color_ref = rgb_to_colorref(rgb)
    border = (max(0, r - 20), max(0, g - 20), max(0, b - 20))
    border_ref = rgb_to_colorref(border)

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, COLORS_REG_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "HotTrackingColor", 0, winreg.REG_SZ, f"{r} {g} {b}")
        winreg.SetValueEx(
            key, "Hilight", 0, winreg.REG_SZ, f"{border[0]} {border[1]} {border[2]}"
        )

    elements = (ctypes.c_int * 2)(COLOR_HIGHLIGHT, COLOR_HOTLIGHT)
    colors = (ctypes.c_int * 2)(border_ref, color_ref)
    user32.SetSysColors(2, elements, colors)
    broadcast_settings_change("Colors")


def parse_rgb_str(value: Any, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    """Розбирає рядок реєстру виду "0 120 215"; за будь-якої аномалії повертає fallback."""
    try:
        parts = [int(p) for p in str(value).split()]
        if len(parts) == 3 and all(0 <= p <= 255 for p in parts):
            return (parts[0], parts[1], parts[2])
    except ValueError:
        pass
    return fallback


def reset_selection_to_default() -> None:
    backup = _load_backup()
    hi_rgb = parse_rgb_str(backup.get("Color_Hilight", ("",))[0], (0, 120, 215))
    hot_rgb = parse_rgb_str(backup.get("Color_HotTrackingColor", ("",))[0], (0, 102, 204))

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, COLORS_REG_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "HotTrackingColor", 0, winreg.REG_SZ, "{} {} {}".format(*hot_rgb))
        winreg.SetValueEx(key, "Hilight", 0, winreg.REG_SZ, "{} {} {}".format(*hi_rgb))

    elements = (ctypes.c_int * 2)(COLOR_HIGHLIGHT, COLOR_HOTLIGHT)
    colors = (ctypes.c_int * 2)(
        rgb_to_colorref(hi_rgb),
        rgb_to_colorref(hot_rgb),
    )
    user32.SetSysColors(2, elements, colors)
    broadcast_settings_change("Colors")


def delete_registry_tree(root: Any, path: str) -> None:
    """Рекурсивно видаляє гілку реєстру (у модулі winreg немає функції DeleteTree)."""
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            children: list[str] = []
            while True:
                try:
                    children.append(winreg.EnumKey(key, len(children)))
                except OSError:
                    break
    except FileNotFoundError:
        return
    for child in children:
        delete_registry_tree(root, f"{path}\\{child}")
    winreg.DeleteKey(root, path)


def perform_full_uninstall_cleanup() -> None:
    """Повністю відкатує зміни в системі та видаляє всі сліди програми з реєстру й диска."""
    global _backup_verified
    reset_cursor_to_default()
    reset_selection_to_default()
    set_autostart_enabled(False)

    try:
        if os.path.isdir(APP_DATA_DIR):
            shutil.rmtree(APP_DATA_DIR, ignore_errors=True)
    except Exception:
        pass

    try:
        delete_registry_tree(winreg.HKEY_CURRENT_USER, APP_REG_PATH)
    except OSError:
        pass
    _backup_verified = False


def save_active_accent_meta(
    cursor_rgb: tuple[int, int, int], selection_rgb: tuple[int, int, int]
) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_REG_PATH) as key:
        winreg.SetValueEx(key, "AccentHex", 0, winreg.REG_SZ, rgb_to_hex(selection_rgb))
        winreg.SetValueEx(key, "AccentColorRef", 0, winreg.REG_DWORD, rgb_to_colorref(selection_rgb))
        winreg.SetValueEx(key, "CursorHex", 0, winreg.REG_SZ, rgb_to_hex(cursor_rgb))
        winreg.SetValueEx(key, "SelectionHex", 0, winreg.REG_SZ, rgb_to_hex(selection_rgb))


def _read_setting(name: str) -> Any:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APP_REG_PATH, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _write_setting(name: str, reg_type: int, value: Any) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_REG_PATH) as key:
        winreg.SetValueEx(key, name, 0, reg_type, value)


def load_color_setting(name: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    val = _read_setting(name)
    return hex_to_rgb(val, fallback) if isinstance(val, str) else fallback


def load_bool_setting(name: str, default: bool) -> bool:
    val = _read_setting(name)
    return default if val is None else bool(val)


def save_bool_setting(name: str, value: bool) -> None:
    _write_setting(name, winreg.REG_DWORD, int(value))


def load_str_setting(name: str, default: str) -> str:
    val = _read_setting(name)
    return val if isinstance(val, str) else default


def save_str_setting(name: str, value: str) -> None:
    _write_setting(name, winreg.REG_SZ, value)


def load_int_setting(name: str, default: int) -> int:
    val = _read_setting(name)
    return val if isinstance(val, int) else default


def save_int_setting(name: str, value: int) -> None:
    _write_setting(name, winreg.REG_DWORD, int(value))


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
            # --minimized: при автозапуску програма стартує одразу в треї, без вікна
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}" --minimized'
            else:
                pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                exe = pyw if os.path.isfile(pyw) else sys.executable
                cmd = f'"{exe}" "{os.path.abspath(__file__)}" --minimized'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def acquire_single_instance() -> bool:
    """Повертає False, якщо програма вже запущена (іменований mutex Windows)."""
    global _instance_mutex
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        _instance_mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


TRANSCODED_WALLPAPER = os.path.join(
    os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Themes\TranscodedWallpaper",
)


def get_wallpaper_path() -> str | None:
    buf = ctypes.create_unicode_buffer(512)
    if user32.SystemParametersInfoW(SPI_GETDESKWALLPAPER, len(buf), buf, 0) and buf.value:
        if os.path.isfile(buf.value):
            return buf.value
    return TRANSCODED_WALLPAPER if os.path.isfile(TRANSCODED_WALLPAPER) else None


def get_wallpaper_signature() -> tuple[str | None, float]:
    path = get_wallpaper_path()
    mtime = 0.0
    for candidate in (TRANSCODED_WALLPAPER, path):
        if candidate:
            try:
                mtime = max(mtime, os.path.getmtime(candidate))
            except OSError:
                pass
    return path, mtime


# --- Аналіз палітри шпалер ---

# Достатньо для прев'ю (420x146) і палітри (72x72); 4K-шпалери не тягнемо в пам'яті повністю
WALLPAPER_WORK_SIZE = 840


def load_wallpaper_image(image_path: str) -> Image.Image:
    """Завантажує шпалери одразу в зменшеному вигляді (для JPEG — через draft-декодування)."""
    with open(image_path, "rb") as f:
        data = f.read()
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (WALLPAPER_WORK_SIZE, WALLPAPER_WORK_SIZE))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((WALLPAPER_WORK_SIZE, WALLPAPER_WORK_SIZE), Image.Resampling.BILINEAR)
    img.load()
    return img


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
    def __init__(self, start_hidden: bool = False) -> None:
        ensure_initial_backup()

        self.root = tk.Tk()
        if start_hidden:
            self.root.withdraw()
        self.root.title("Wallpaper Accent Sync")
        self.root.configure(bg="#1C1C1E")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self._auto_apply_val: bool = load_bool_setting("AutoApply", True)
        self._enable_cursor_val: bool = load_bool_setting("EnableCursor", True)
        self._enable_selection_val: bool = load_bool_setting("EnableSelection", True)
        self._separate_colors_val: bool = load_bool_setting("SeparateColors", False)
        self._revert_on_exit_val: bool = load_bool_setting("RevertOnExit", False)

        # Налаштування розміру курсора ("auto" або "manual")
        saved_mode = load_str_setting("CursorSizeMode", "auto")
        self._cursor_size_mode: str = saved_mode if saved_mode in ("auto", "manual") else "auto"
        saved_custom = load_int_setting("CursorCustomSize", 32)
        self._cursor_custom_size: int = saved_custom if saved_custom in CURSOR_SIZE_PRESETS else 32
        self._last_system_cursor_size: int = get_system_cursor_base_size()

        self.auto_apply = tk.BooleanVar(value=self._auto_apply_val)
        self.enable_cursor = tk.BooleanVar(value=self._enable_cursor_val)
        self.enable_selection = tk.BooleanVar(value=self._enable_selection_val)
        self.separate_colors = tk.BooleanVar(value=self._separate_colors_val)
        self.revert_on_exit = tk.BooleanVar(value=self._revert_on_exit_val)
        self.autostart = tk.BooleanVar(value=is_autostart_enabled())

        default_blue = (0, 120, 215)
        self.cursor_color: tuple[int, int, int] = load_color_setting("CursorHex", default_blue)
        self.selection_color: tuple[int, int, int] = load_color_setting("SelectionHex", default_blue)
        if not self._separate_colors_val:
            self.selection_color = self.cursor_color

        self.active_target: str = "cursor"
        self.is_default_state: bool = load_bool_setting("IsDefaultState", False)

        self.palette: list[tuple[int, int, int]] = []
        self.last_signature: tuple[str | None, float] = (None, 0.0)
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.tray_icon: Any = None
        self.size_buttons: dict[str, tk.Button] = {}

        self._shutting_down = False
        self._fit_pending = False
        self._sync_failures = 0
        self._poll_job: str | None = None
        self._tray_thread: threading.Thread | None = None
        # Кеш застосованого стану: не перегенеровуємо курсори й не пишемо в реєстр без потреби.
        # True / None означають «стан невідомий», тож перший виклик гарантовано синхронізує систему.
        self._cursor_custom_active = True
        self._selection_custom_active = True
        self._cursor_key: tuple[tuple[int, int, int], int] | None = None
        self._selection_key: tuple[int, int, int] | None = None

        self._build_ui()
        # Висота вікна залежить від вмісту, який з'являється пізніше (картки палітри, режими статусу),
        # тож підганяємо вікно щоразу, коли ці блоки змінюють розмір
        for widget in (self.cards_frame, self.status_container):
            widget.bind("<Configure>", lambda _: self._schedule_fit())
        self._refresh_size_buttons()
        self._refresh_status_area()
        self._fit_and_center_window()

        if self.is_default_state and not self._auto_apply_val:
            self.sync_from_wallpaper(force_apply=False)
        elif self._auto_apply_val:
            self.sync_from_wallpaper(force_apply=True)
        else:
            self.sync_from_wallpaper(force_apply=False)
            self._apply_current_targets()

        self._fit_and_center_window()  # тепер, коли палітра вже відображена, висота остаточна
        self.root.after(0, self._start_tray)
        self._poll_job = self.root.after(1500, self._poll_system_changes)

    def _get_effective_cursor_size(self) -> int:
        if self._cursor_size_mode == "auto":
            return get_system_cursor_base_size()
        return self._cursor_custom_size

    def _post(self, fn) -> None:
        """Безпечно ставить виклик у чергу головного потоку Tk (для колбеків трея)."""
        try:
            self.root.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _start_tray(self) -> None:
        self.tray_icon = self._create_tray_icon()
        self._tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self._tray_thread.start()

    def _build_ui(self) -> None:
        bg_main = "#1C1C1E"
        fg_text = "#F0F0F2"
        fg_muted = "#B8B8BE"

        preview_container = tk.Frame(
            self.root,
            width=424,
            height=150,
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

        # --- Віджети для роздільного режиму ---
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

        # --- Панель вибору розміру курсора ---
        size_row = tk.Frame(self.root, bg=bg_main)
        size_row.pack(fill="x", padx=16, pady=(2, 8))

        tk.Label(
            size_row,
            text="Розмір курсора:",
            bg=bg_main,
            fg=fg_muted,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 8))

        self.size_buttons["auto"] = self._make_size_pill_button(
            size_row,
            text="Авто",
            command=lambda: self._select_cursor_size("auto", self._cursor_custom_size),
        )
        self.size_buttons["auto"].pack(side="left", padx=(0, 4))

        for px in CURSOR_SIZE_PRESETS:
            key = str(px)
            self.size_buttons[key] = self._make_size_pill_button(
                size_row,
                text=f"{px} px",
                command=lambda sz=px: self._select_cursor_size("manual", sz),
            )
            self.size_buttons[key].pack(side="left", padx=(0, 4))

        # Блок налаштувань
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
            "Відкатувати до стандартного вигляду при виході",
            self.revert_on_exit,
            self._on_revert_on_exit_toggled,
        ).pack(anchor="w", pady=1)

        self._make_checkbox(
            opts_frame,
            "Запускати разом із Windows (автозавантаження)",
            self.autostart,
            self._on_autostart_toggled,
        ).pack(anchor="w", pady=1)

        # Нижня панель кнопок
        btn_row1 = tk.Frame(self.root, bg=bg_main)
        btn_row1.pack(fill="x", padx=16, pady=(2, 6))

        self._make_button(btn_row1, "Свій колір...", self.pick_custom_color, width=14).pack(side="left")
        self._make_button(btn_row1, "Оновити", lambda: self.sync_from_wallpaper(force_apply=False), width=11).pack(
            side="left", padx=(6, 0)
        )
        self._make_button(btn_row1, "Стандартний вигляд", self.reset_defaults, width=21).pack(
            side="right"
        )

        btn_row2 = tk.Frame(self.root, bg=bg_main)
        btn_row2.pack(fill="x", padx=16, pady=(0, 16))

        self._make_button(
            btn_row2,
            "Видалити налаштування з системи",
            self.uninstall_and_quit,
            width=31,
            bg="#3A2428",
            hover_bg="#522E35",
        ).pack(side="left")
        self._make_button(btn_row2, "У трей", self.hide_window, width=14).pack(side="right")

    @staticmethod
    def _make_size_pill_button(parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#26262A",
            fg="#B8B8BE",
            activebackground="#3E3E46",
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            padx=7,
            pady=3,
            font=("Segoe UI", 8),
            cursor="hand2",
        )

    def _refresh_size_buttons(self) -> None:
        active_key = "auto" if self._cursor_size_mode == "auto" else str(self._cursor_custom_size)
        for key, btn in self.size_buttons.items():
            is_active = key == active_key
            btn.configure(
                bg="#3E3E48" if is_active else "#26262A",
                fg="#FFFFFF" if is_active else "#B8B8BE",
                font=("Segoe UI", 8, "bold" if is_active else "normal"),
            )

    def _select_cursor_size(self, mode: str, custom_px: int) -> None:
        self._cursor_size_mode = mode
        self._cursor_custom_size = custom_px
        save_str_setting("CursorSizeMode", mode)
        save_int_setting("CursorCustomSize", custom_px)
        self._refresh_size_buttons()

        if self._enable_cursor_val and not self.is_default_state:
            self._apply_cursor()

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
    def _make_button(
        parent: tk.Widget,
        text: str,
        command,
        width: int,
        bg: str = "#2E2E34",
        hover_bg: str = "#3E3E46",
    ) -> tk.Button:
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=bg,
            fg="#F0F0F2",
            activebackground=hover_bg,
            activeforeground="#FFFFFF",
            relief="flat",
            bd=0,
            pady=5,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        btn.bind("<Enter>", lambda _: btn.configure(bg=hover_bg))
        btn.bind("<Leave>", lambda _: btn.configure(bg=bg))
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

    def _schedule_fit(self) -> None:
        if not self._fit_pending:
            self._fit_pending = True
            self.root.after_idle(self._fit_window)

    def _fit_window(self) -> None:
        """Підганяє розмір вікна під вміст, не рухаючи його по екрану."""
        self._fit_pending = False
        try:
            self.root.update_idletasks()
            w = max(456, self.root.winfo_reqwidth())
            h = self.root.winfo_reqheight()
            if (w, h) != (self.root.winfo_width(), self.root.winfo_height()):
                self.root.geometry(f"{w}x{h}")
        except tk.TclError:
            pass

    def _create_tray_icon(self) -> Any:
        menu = pystray.Menu(
            pystray.MenuItem(
                "Відкрити палітру кольорів",
                lambda: self._post(self.toggle_window),
                default=True,
            ),
            pystray.MenuItem(
                "Оновити зі шпалер",
                lambda: self._post(lambda: self.sync_from_wallpaper(force_apply=True)),
            ),
            pystray.MenuItem(
                "Повернути стандартний вигляд",
                lambda: self._post(self.reset_defaults),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Змінювати курсор",
                lambda: self._post(self._toggle_cursor_from_tray),
                checked=lambda _: self._enable_cursor_val,
            ),
            pystray.MenuItem(
                "Змінювати виділення",
                lambda: self._post(self._toggle_selection_from_tray),
                checked=lambda _: self._enable_selection_val,
            ),
            pystray.MenuItem(
                "Окремі кольори (курсор / виділення)",
                lambda: self._post(self._toggle_separate_from_tray),
                checked=lambda _: self._separate_colors_val,
            ),
            pystray.MenuItem(
                "Автопідбір зі шпалер",
                lambda: self._post(self._toggle_auto_apply_from_tray),
                checked=lambda _: self._auto_apply_val,
            ),
            pystray.MenuItem(
                "Відкат до стандартного при виході",
                lambda: self._post(self._toggle_revert_on_exit_from_tray),
                checked=lambda _: self._revert_on_exit_val,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Видалити налаштування з системи",
                lambda: self._post(self.uninstall_and_quit),
            ),
            pystray.MenuItem(
                "Вихід",
                lambda: self._post(lambda: self.quit_app(restore_before_quit=self._revert_on_exit_val)),
            ),
        )
        return pystray.Icon(
            APP_NAME,
            create_tray_icon_image(self.cursor_color, self.selection_color),
            "Wallpaper Accent Sync",
            menu,
        )

    def _toggle_cursor_from_tray(self) -> None:
        self.enable_cursor.set(not self._enable_cursor_val)
        self._on_targets_toggled()

    def _toggle_selection_from_tray(self) -> None:
        self.enable_selection.set(not self._enable_selection_val)
        self._on_targets_toggled()

    def _toggle_separate_from_tray(self) -> None:
        self.separate_colors.set(not self._separate_colors_val)
        self._on_separate_colors_toggled()

    def _toggle_auto_apply_from_tray(self) -> None:
        self.auto_apply.set(not self._auto_apply_val)
        self._on_auto_apply_toggled()

    def _toggle_revert_on_exit_from_tray(self) -> None:
        self.revert_on_exit.set(not self._revert_on_exit_val)
        self._on_revert_on_exit_toggled()

    def _set_active_target(self, target: str) -> None:
        self.active_target = target
        self._refresh_status_area()

    def _on_targets_toggled(self) -> None:
        self._enable_cursor_val = self.enable_cursor.get()
        self._enable_selection_val = self.enable_selection.get()
        save_bool_setting("EnableCursor", self._enable_cursor_val)
        save_bool_setting("EnableSelection", self._enable_selection_val)
        # У «стандартному вигляді» перемикачі лише запам'ятовуються — інакше вони б мовчки
        # замінили рідні курсори Windows синіми
        if not self.is_default_state:
            self._apply_current_targets()

    def _on_separate_colors_toggled(self) -> None:
        is_sep = self.separate_colors.get()
        self._separate_colors_val = is_sep
        save_bool_setting("SeparateColors", is_sep)

        changed = False
        if not is_sep:
            self.selection_color = self.cursor_color
            changed = True
        elif self._auto_apply_val and len(self.palette) >= 2:
            self.selection_color = self.palette[1]
            changed = True
        if changed and not self.is_default_state:
            self._apply_current_targets()

        self._refresh_status_area()
        self._render_palette_cards()
        self._update_tray_appearance()

    def _on_auto_apply_toggled(self) -> None:
        enabled = self.auto_apply.get()
        self._auto_apply_val = enabled
        save_bool_setting("AutoApply", enabled)
        if enabled and self.palette:
            self._apply_palette_automatically()

    def _on_revert_on_exit_toggled(self) -> None:
        self._revert_on_exit_val = self.revert_on_exit.get()
        save_bool_setting("RevertOnExit", self._revert_on_exit_val)

    def _on_autostart_toggled(self) -> None:
        set_autostart_enabled(self.autostart.get())

    def _sync_base_size(self) -> None:
        """У ручному режимі виставляє CursorBaseSize; в «Авто» повертає розмір користувача."""
        current = get_system_cursor_base_size()
        original = load_int_setting(ORIGINAL_BASE_SIZE_SETTING, 0)
        if self._cursor_size_mode == "manual":
            if not original:
                save_int_setting(ORIGINAL_BASE_SIZE_SETTING, current)
            target = self._cursor_custom_size
        elif original:
            target = original
            save_int_setting(ORIGINAL_BASE_SIZE_SETTING, 0)
        else:
            return
        if target != current:
            set_system_cursor_base_size(target)
        self._last_system_cursor_size = target

    def _apply_cursor(self) -> None:
        self._sync_base_size()
        size = self._get_effective_cursor_size()
        key = (self.cursor_color, size)
        if key == self._cursor_key:
            return
        apply_cursor_color(self.cursor_color, target_size=size)
        self._cursor_key = key
        self._cursor_custom_active = True

    def _restore_cursor(self) -> None:
        if self._cursor_custom_active:
            reset_cursor_to_default()
            self._cursor_custom_active = False
            self._cursor_key = None

    def _apply_selection(self) -> None:
        if self.selection_color == self._selection_key:
            return
        apply_selection_color(self.selection_color)
        self._selection_key = self.selection_color
        self._selection_custom_active = True

    def _restore_selection(self) -> None:
        if self._selection_custom_active:
            reset_selection_to_default()
            self._selection_custom_active = False
            self._selection_key = None

    def _apply_current_targets(self) -> None:
        if self.is_default_state:
            self.is_default_state = False
            save_bool_setting("IsDefaultState", False)

        if self._enable_cursor_val:
            self._apply_cursor()
        else:
            self._restore_cursor()

        if self._enable_selection_val:
            self._apply_selection()
        else:
            self._restore_selection()

        save_active_accent_meta(self.cursor_color, self.selection_color)

    def _apply_palette_automatically(self) -> None:
        if not self.palette:
            return
        self.cursor_color = self.palette[0]
        if self._separate_colors_val and len(self.palette) >= 2:
            self.selection_color = self.palette[1]
        else:
            self.selection_color = self.palette[0]

        self._apply_current_targets()
        self._refresh_status_area()
        self._render_palette_cards()
        self._update_tray_appearance()

    def _poll_system_changes(self) -> None:
        self._poll_job = None
        if self._shutting_down:
            return
        try:
            sig = get_wallpaper_signature()
            if sig[0] is not None and sig != self.last_signature:
                self.sync_from_wallpaper(force_apply=self._auto_apply_val, sig=sig)

            # Відстежуємо зміну системного розміру курсора в режимі "Авто"
            sys_size = get_system_cursor_base_size()
            if sys_size != self._last_system_cursor_size:
                self._last_system_cursor_size = sys_size
                if self._enable_cursor_val and not self.is_default_state:
                    if self._cursor_size_mode == "auto":
                        self._apply_cursor()
                    elif sys_size != self._cursor_custom_size:
                        # Розмір змінили в налаштуваннях Windows: запам'ятовуємо для «Авто»,
                        # а ручний розмір програми повертаємо
                        save_int_setting(ORIGINAL_BASE_SIZE_SETTING, sys_size)
                        self._cursor_key = None
                        self._apply_cursor()
        except Exception:
            pass
        finally:
            # Одна помилка не повинна назавжди зупиняти відстеження
            if not self._shutting_down:
                try:
                    self._poll_job = self.root.after(1500, self._poll_system_changes)
                except tk.TclError:
                    pass

    def sync_from_wallpaper(
        self, force_apply: bool, sig: tuple[str | None, float] | None = None
    ) -> None:
        sig = sig or get_wallpaper_signature()
        path = sig[0]
        if not path:
            return

        try:
            img = load_wallpaper_image(path)
        except Exception:
            # Файл може бути недописаний у момент зміни шпалер — пробуємо ще, але не вічно
            self._sync_failures += 1
            if self._sync_failures >= 3:
                self._sync_failures = 0
                self.last_signature = sig
            return
        self._sync_failures = 0
        self.last_signature = sig

        try:
            thumb = img.copy()
            thumb.thumbnail((420, 146), Image.Resampling.LANCZOS)
            self.preview_photo = ImageTk.PhotoImage(thumb)
            self.preview_label.configure(image=self.preview_photo, text="")

            self.palette = extract_wallpaper_palette(img, max_colors=6)

            if force_apply and self.palette:
                # Явне/автоматичне оновлення за шпалерами — застосовуємо наново без урахування кешу
                self._cursor_key = None
                self._selection_key = None
                self._apply_palette_automatically()
            else:
                self._refresh_status_area()
                self._render_palette_cards()
                self._update_tray_appearance()
        except Exception:
            pass

    def select_color(self, rgb: tuple[int, int, int]) -> None:
        if self._separate_colors_val:
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
        """Повертає стандартний вигляд Windows і ставить автопідбір на паузу."""
        default_rgb = (0, 120, 215)
        self.cursor_color = default_rgb
        self.selection_color = default_rgb
        self.is_default_state = True

        if self._auto_apply_val:
            self._auto_apply_val = False
            self.auto_apply.set(False)
            save_bool_setting("AutoApply", False)

        reset_cursor_to_default()
        reset_selection_to_default()
        self._cursor_custom_active = self._selection_custom_active = False
        self._cursor_key = self._selection_key = None
        save_active_accent_meta(default_rgb, default_rgb)
        save_bool_setting("IsDefaultState", True)

        self._refresh_status_area()
        self._render_palette_cards()
        if self.tray_icon is not None:
            self.tray_icon.icon = create_tray_icon_image(default_rgb, default_rgb)
            self.tray_icon.title = "Wallpaper Accent: Стандартний вигляд"

    def uninstall_and_quit(self) -> None:
        """Повністю відкатує зміни та видаляє дані програми перед видаленням .exe."""
        self.show_window()
        confirmed = messagebox.askyesno(
            "Видалення налаштувань із системи",
            "Повернути стандартні курсори й кольори виділення Windows, прибрати програму з автозавантаження та видалити всі її файли й записи з реєстру?",
            parent=self.root,
        )
        if not confirmed:
            return

        # Зупиняємо опитування, інакше воно могло б знову записати налаштування вже після очищення
        self._shutting_down = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        try:
            perform_full_uninstall_cleanup()
        except Exception as exc:
            self._shutting_down = False
            self._poll_job = self.root.after(1500, self._poll_system_changes)
            messagebox.showerror(
                "Помилка", f"Не вдалося повністю видалити налаштування:\n{exc}", parent=self.root
            )
            return
        messagebox.showinfo(
            "Готово",
            "Усі системні налаштування повернуто до стандартних, автозапуск і тимчасові файли очищено.\n\nЗараз програма закриється — після цього файл програми можна спокійно видалити.",
            parent=self.root,
        )
        self.quit_app(restore_before_quit=False)

    def pick_custom_color(self) -> None:
        initial = (
            self.selection_color
            if (self._separate_colors_val and self.active_target == "selection")
            else self.cursor_color
        )
        target_name = (
            "для виділення"
            if (self._separate_colors_val and self.active_target == "selection")
            else ("для курсора" if self._separate_colors_val else "акцентний колір")
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
        if self.is_default_state:
            self.split_status_frame.pack_forget()
            self.unified_status_frame.pack(fill="x")
            self.unified_badge.configure(bg="#0078D7")
            self.unified_label.configure(
                text="Стандартний вигляд Windows відновлено (автопідбір на паузі)"
            )
            return

        if self._separate_colors_val:
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
        if self.tray_icon is None:
            return
        if self.is_default_state:
            self.tray_icon.icon = create_tray_icon_image((0, 120, 215), (0, 120, 215))
            self.tray_icon.title = "Wallpaper Accent: Стандартний вигляд"
            return

        self.tray_icon.icon = create_tray_icon_image(self.cursor_color, self.selection_color)
        if self._separate_colors_val and self.cursor_color != self.selection_color:
            self.tray_icon.title = (
                f"Курсор: {rgb_to_hex(self.cursor_color)} | Виділення: {rgb_to_hex(self.selection_color)}"
            )
        else:
            self.tray_icon.title = f"Wallpaper Accent: {rgb_to_hex(self.cursor_color)}"

    def _render_palette_cards(self) -> None:
        for child in self.cards_frame.winfo_children():
            child.destroy()

        is_sep = self._separate_colors_val

        for idx, rgb in enumerate(self.palette):
            is_cur = (not self.is_default_state) and (rgb == self.cursor_color)
            is_sel = (not self.is_default_state) and (rgb == self.selection_color)
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

    def quit_app(self, restore_before_quit: bool | None = None) -> None:
        should_restore = self._revert_on_exit_val if restore_before_quit is None else restore_before_quit
        self._shutting_down = True
        if should_restore:
            try:
                reset_cursor_to_default()
                reset_selection_to_default()
            except Exception:
                pass
        if self.tray_icon is not None:
            self.tray_icon.stop()
            # Дочікуємося зняття іконки, інакше в треї залишається «привид» до наведення миші
            if self._tray_thread is not None:
                self._tray_thread.join(timeout=1.5)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    args = {arg.lower() for arg in sys.argv[1:]}
    if "--uninstall" in args:
        perform_full_uninstall_cleanup()
        sys.exit(0)
    if "--reset" in args:
        reset_cursor_to_default()
        reset_selection_to_default()
        save_bool_setting("AutoApply", False)
        save_bool_setting("IsDefaultState", True)
        sys.exit(0)

    # Другий екземпляр створив би другу іконку в треї й конфліктував би з першим
    if not acquire_single_instance():
        sys.exit(0)

    WallpaperAccentApp(start_hidden="--minimized" in args).run()