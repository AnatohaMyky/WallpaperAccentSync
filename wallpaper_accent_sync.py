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

APP_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    APP_NAME,
)
CURSOR_STORAGE_DIR = os.path.join(APP_DATA_DIR, "cursors")

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


def broadcast_settings_change(area: str = "Colors") -> None:
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


# --- Резервне копіювання початкового стану системи ---

def ensure_initial_backup() -> None:
    """Зберігає початкові курсори (усі 17 станів) та кольори виділення перед зміною."""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, BACKUP_REG_PATH) as bkp:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CURSORS_REG_PATH, 0, winreg.KEY_READ) as cur_key:
                for role in ("",) + ALL_CURSOR_ROLES:
                    save_name = "Cursor_Scheme" if role == "" else f"Cursor_{role}"
                    missing_name = f"{save_name}_Missing"

                    already_saved = False
                    for check_key in (save_name, missing_name):
                        try:
                            winreg.QueryValueEx(bkp, check_key)
                            already_saved = True
                            break
                        except FileNotFoundError:
                            pass
                    if already_saved:
                        continue

                    try:
                        val, reg_type = winreg.QueryValueEx(cur_key, role)
                        if isinstance(val, str) and "WallpaperAccentSync" in val:
                            continue
                        winreg.SetValueEx(bkp, save_name, 0, reg_type, val)
                    except FileNotFoundError:
                        winreg.SetValueEx(bkp, missing_name, 0, winreg.REG_DWORD, 1)
        except OSError:
            pass

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, COLORS_REG_PATH, 0, winreg.KEY_READ) as col_key:
                for name in ("Hilight", "HotTrackingColor"):
                    save_name = f"Color_{name}"
                    try:
                        winreg.QueryValueEx(bkp, save_name)
                        continue
                    except FileNotFoundError:
                        pass

                    try:
                        val, reg_type = winreg.QueryValueEx(col_key, name)
                        winreg.SetValueEx(bkp, save_name, 0, reg_type, val)
                    except FileNotFoundError:
                        pass
        except OSError:
            pass

        winreg.SetValueEx(bkp, "Initialized", 0, winreg.REG_DWORD, 1)


def cleanup_cursor_files() -> None:
    try:
        if os.path.isdir(CURSOR_STORAGE_DIR):
            shutil.rmtree(CURSOR_STORAGE_DIR, ignore_errors=True)
    except Exception:
        pass


# --- Генерація статичних (.cur) та анімованих (.ani) курсорів Windows ---

def build_cur_bytes(img: Image.Image, hotspot_x: int = 0, hotspot_y: int = 0) -> bytes:
    """Кодує 32x32 RGBA-зображення у бінарний формат курсора Windows (.cur)."""
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
    return header + entry + image_data


def save_cur_file(img: Image.Image, path: str, hotspot_x: int = 0, hotspot_y: int = 0) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(build_cur_bytes(img, hotspot_x=hotspot_x, hotspot_y=hotspot_y))


def save_ani_file(
    frames_32: list[Image.Image],
    path: str,
    hotspot_x: int,
    hotspot_y: int,
    jiffies_per_frame: int = 5,
) -> None:
    """Зберігає серію 32x32 RGBA-кадрів в анімований курсор Windows (.ani)."""
    n_frames = len(frames_32)
    icon_chunks = bytearray()
    for frame in frames_32:
        cur_data = build_cur_bytes(frame, hotspot_x=hotspot_x, hotspot_y=hotspot_y)
        icon_chunks.extend(b"icon")
        icon_chunks.extend(struct.pack("<I", len(cur_data)))
        icon_chunks.extend(cur_data)
        if len(cur_data) % 2 == 1:
            icon_chunks.append(0)

    fram_payload = b"fram" + bytes(icon_chunks)
    list_chunk = b"LIST" + struct.pack("<I", len(fram_payload)) + fram_payload

    # anih header: cbSize=36, nFrames, nSteps, w=0, h=0, bpp=0, planes=0, jifRate, flags=1 (AF_ICON)
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

    # Зовнішній контур кільця
    r_draw.ellipse(
        (cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r),
        fill=outline_rgba,
    )
    # Напівпрозора підкладка треку
    track_r = outer_r - 4
    track_fill = (fill_rgba[0], fill_rgba[1], fill_rgba[2], 95)
    r_draw.ellipse(
        (cx - track_r, cy - track_r, cx + track_r, cy + track_r),
        fill=track_fill,
    )
    # Активний сектор обертання
    r_draw.pieslice(
        (cx - track_r, cy - track_r, cx + track_r, cy + track_r),
        start=angle_deg,
        end=angle_deg + 145,
        fill=fill_rgba,
    )
    # Внутрішній контур і прозорий отвір усередині
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
    outline_width: int = 7,
) -> None:
    fill_rgba, outline_rgba = _get_cursor_colors(rgb)
    canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    _draw_outlined_polygon(draw, pts_128, fill_rgba, outline_rgba, outline_width=outline_width)
    final_img = canvas.resize((32, 32), Image.Resampling.LANCZOS)
    save_cur_file(final_img, out_path, hotspot_x=hotspot_32[0], hotspot_y=hotspot_32[1])


def generate_colored_cursors(rgb: tuple[int, int, int]) -> dict[str, str]:
    """Створює повний набір з 17 системних курсорів із заданим акцентним кольором."""
    fill_rgba, outline_rgba = _get_cursor_colors(rgb)
    paths: dict[str, str] = {}

    def p(filename: str) -> str:
        return os.path.join(CURSOR_STORAGE_DIR, filename)

    # 1. Arrow (Основний режим)
    arrow_pts = [(12, 8), (12, 104), (36, 82), (52, 118), (66, 112), (50, 76), (82, 76)]
    paths["Arrow"] = p("accent_arrow.cur")
    _render_polygon_cur(arrow_pts, rgb, (3, 2), paths["Arrow"])

    # 2. Help (Вибір довідки: стрілка + знак питання)
    paths["Help"] = p("accent_help.cur")
    c_help = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d_help = ImageDraw.Draw(c_help)
    small_arrow = [(10, 8), (10, 86), (29, 68), (42, 98), (54, 93), (41, 63), (67, 63)]
    _draw_outlined_polygon(d_help, small_arrow, fill_rgba, outline_rgba, outline_width=6)
    # Знак питання праворуч знизу
    q_top = [
        (76, 56), (92, 48), (110, 52), (116, 66), (112, 80),
        (99, 88), (99, 95), (87, 95), (87, 81), (100, 73),
        (102, 65), (95, 60), (85, 64), (76, 64),
    ]
    _draw_outlined_polygon(d_help, q_top, fill_rgba, outline_rgba, outline_width=5)
    d_help.ellipse((85, 101, 101, 117), fill=fill_rgba, outline=outline_rgba, width=5)
    save_cur_file(c_help.resize((32, 32), Image.Resampling.LANCZOS), paths["Help"], 3, 2)

    # 3. AppStarting (Фоновий режим: стрілка + анімоване кільце завантаження)
    paths["AppStarting"] = p("accent_appstarting.ani")
    app_frames: list[Image.Image] = []
    for step in range(8):
        frame = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        d_f = ImageDraw.Draw(frame)
        _draw_outlined_polygon(d_f, small_arrow, fill_rgba, outline_rgba, outline_width=6)
        _draw_spinner_ring(frame, cx=92, cy=90, outer_r=26, inner_r=12, angle_deg=step * 45, fill_rgba=fill_rgba, outline_rgba=outline_rgba)
        app_frames.append(frame.resize((32, 32), Image.Resampling.LANCZOS))
    save_ani_file(app_frames, paths["AppStarting"], hotspot_x=3, hotspot_y=2, jiffies_per_frame=5)

    # 4. Wait (Зайнятий / Завантаження: центроване анімоване кільце)
    paths["Wait"] = p("accent_wait.ani")
    wait_frames: list[Image.Image] = []
    for step in range(8):
        frame = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
        _draw_spinner_ring(frame, cx=64, cy=64, outer_r=44, inner_r=24, angle_deg=step * 45, fill_rgba=fill_rgba, outline_rgba=outline_rgba)
        wait_frames.append(frame.resize((32, 32), Image.Resampling.LANCZOS))
    save_ani_file(wait_frames, paths["Wait"], hotspot_x=16, hotspot_y=16, jiffies_per_frame=5)

    # 5. Crosshair (Графічне виділення)
    cross_pts = [
        (58, 16), (70, 16), (70, 58), (112, 58), (112, 70), (70, 70),
        (70, 112), (58, 112), (58, 70), (16, 70), (16, 58), (58, 58),
    ]
    paths["Crosshair"] = p("accent_cross.cur")
    _render_polygon_cur(cross_pts, rgb, (16, 16), paths["Crosshair"], outline_width=6)

    # 6. IBeam (Виділення тексту)
    ibeam_pts = [
        (44, 16), (84, 16), (84, 26), (69, 26), (69, 102), (84, 102),
        (84, 112), (44, 112), (44, 102), (59, 102), (59, 26), (44, 26),
    ]
    paths["IBeam"] = p("accent_ibeam.cur")
    _render_polygon_cur(ibeam_pts, rgb, (16, 16), paths["IBeam"], outline_width=5)

    # 7. NWPen (Рукописний ввід)
    pen_pts = [(12, 116), (34, 110), (108, 36), (92, 20), (18, 94)]
    paths["NWPen"] = p("accent_nwpen.cur")
    _render_polygon_cur(pen_pts, rgb, (3, 29), paths["NWPen"], outline_width=6)

    # 8. No (Недоступно: перекреслене кільце)
    paths["No"] = p("accent_no.cur")
    c_no = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    no_ring = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d_no = ImageDraw.Draw(no_ring)
    d_no.ellipse((20, 20, 108, 108), fill=outline_rgba)
    d_no.ellipse((25, 25, 103, 103), fill=fill_rgba)
    d_no.ellipse((37, 37, 91, 91), fill=outline_rgba)
    d_no.ellipse((42, 42, 86, 86), fill=(0, 0, 0, 0))
    c_no.alpha_composite(no_ring)
    slash_pts = [(33, 41), (41, 33), (95, 87), (87, 95)]
    _draw_outlined_polygon(ImageDraw.Draw(c_no), slash_pts, fill_rgba, outline_rgba, outline_width=5)
    save_cur_file(c_no.resize((32, 32), Image.Resampling.LANCZOS), paths["No"], 16, 16)

    # 9. SizeNS (Вертикальна зміна розміру)
    size_ns_pts = [
        (64, 12), (86, 36), (71, 36), (71, 92), (86, 92),
        (64, 116), (42, 92), (57, 92), (57, 36), (42, 36),
    ]
    paths["SizeNS"] = p("accent_sizens.cur")
    _render_polygon_cur(size_ns_pts, rgb, (16, 16), paths["SizeNS"], outline_width=6)

    # 10. SizeWE (Горизонтальна зміна розміру)
    size_we_pts = [
        (12, 64), (36, 42), (36, 57), (92, 57), (92, 42),
        (116, 64), (92, 86), (92, 71), (36, 71), (36, 86),
    ]
    paths["SizeWE"] = p("accent_sizewe.cur")
    _render_polygon_cur(size_we_pts, rgb, (16, 16), paths["SizeWE"], outline_width=6)

    # 11. SizeNWSE (Діагональна зміна розміру 1)
    size_nwse_pts = [
        (22, 22), (58, 26), (47, 37), (91, 81), (102, 70),
        (106, 106), (70, 102), (81, 91), (37, 47), (26, 58),
    ]
    paths["SizeNWSE"] = p("accent_sizenwse.cur")
    _render_polygon_cur(size_nwse_pts, rgb, (16, 16), paths["SizeNWSE"], outline_width=6)

    # 12. SizeNESW (Діагональна зміна розміру 2)
    size_nesw_pts = [
        (106, 22), (70, 26), (81, 37), (37, 81), (26, 70),
        (22, 106), (58, 102), (47, 91), (91, 47), (102, 58),
    ]
    paths["SizeNESW"] = p("accent_sizenesw.cur")
    _render_polygon_cur(size_nesw_pts, rgb, (16, 16), paths["SizeNESW"], outline_width=6)

    # 13. SizeAll (Переміщення)
    size_all_pts = [
        (64, 10), (82, 30), (70, 30), (70, 58), (98, 58), (98, 46),
        (118, 64), (98, 82), (98, 70), (70, 70), (70, 98), (82, 98),
        (64, 118), (46, 98), (58, 98), (58, 70), (30, 70), (30, 82),
        (10, 64), (30, 46), (30, 58), (58, 58), (58, 30), (46, 30),
    ]
    paths["SizeAll"] = p("accent_sizeall.cur")
    _render_polygon_cur(size_all_pts, rgb, (16, 16), paths["SizeAll"], outline_width=5)

    # 14. UpArrow (Альтернативний вибір)
    up_arrow_pts = [(64, 12), (92, 46), (73, 46), (73, 114), (55, 114), (55, 46), (36, 46)]
    paths["UpArrow"] = p("accent_uparrow.cur")
    _render_polygon_cur(up_arrow_pts, rgb, (16, 3), paths["UpArrow"], outline_width=6)

    # 15. Hand (Вибір посилання)
    hand_pts = [
        (44, 12), (56, 12), (56, 48), (70, 48), (70, 52), (84, 52), (84, 58),
        (96, 58), (96, 86), (88, 106), (38, 106), (22, 76), (30, 66), (44, 72),
    ]
    paths["Hand"] = p("accent_hand.cur")
    _render_polygon_cur(hand_pts, rgb, (12, 3), paths["Hand"], outline_width=7)

    # 16. Pin (Вибір розташування)
    paths["Pin"] = p("accent_pin.cur")
    c_pin = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d_pin = ImageDraw.Draw(c_pin)
    _draw_outlined_polygon(d_pin, small_arrow, fill_rgba, outline_rgba, outline_width=6)
    pin_pts = [(94, 52), (110, 60), (114, 78), (94, 114), (74, 78), (78, 60)]
    _draw_outlined_polygon(d_pin, pin_pts, fill_rgba, outline_rgba, outline_width=5)
    d_pin.ellipse((87, 67, 101, 81), fill=outline_rgba)
    save_cur_file(c_pin.resize((32, 32), Image.Resampling.LANCZOS), paths["Pin"], 3, 2)

    # 17. Person (Вибір користувача)
    paths["Person"] = p("accent_person.cur")
    c_per = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    d_per = ImageDraw.Draw(c_per)
    _draw_outlined_polygon(d_per, small_arrow, fill_rgba, outline_rgba, outline_width=6)
    shoulders_pts = [(70, 114), (74, 94), (86, 88), (102, 88), (114, 94), (118, 114)]
    _draw_outlined_polygon(d_per, shoulders_pts, fill_rgba, outline_rgba, outline_width=5)
    d_per.ellipse((81, 56, 107, 82), fill=fill_rgba, outline=outline_rgba, width=5)
    save_cur_file(c_per.resize((32, 32), Image.Resampling.LANCZOS), paths["Person"], 3, 2)

    return paths


def apply_cursor_color(rgb: tuple[int, int, int]) -> None:
    ensure_initial_backup()
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

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CURSORS_REG_PATH) as cur_key:
        restored_from_bkp = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BACKUP_REG_PATH, 0, winreg.KEY_READ) as bkp:
                for role in ("",) + ALL_CURSOR_ROLES:
                    save_name = "Cursor_Scheme" if role == "" else f"Cursor_{role}"
                    try:
                        val, reg_type = winreg.QueryValueEx(bkp, save_name)
                        if isinstance(val, str) and "WallpaperAccentSync" not in val:
                            winreg.SetValueEx(cur_key, role, 0, reg_type, val)
                            restored_from_bkp = True
                            continue
                    except FileNotFoundError:
                        pass

                    fb_val, fb_type = FALLBACK_DEFAULT_CURSORS[role]
                    winreg.SetValueEx(cur_key, role, 0, fb_type, fb_val)
        except OSError:
            pass

        if not restored_from_bkp:
            for role, (fb_val, fb_type) in FALLBACK_DEFAULT_CURSORS.items():
                winreg.SetValueEx(cur_key, role, 0, fb_type, fb_val)

        winreg.SetValueEx(cur_key, "Scheme Source", 0, winreg.REG_DWORD, 2)

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


def reset_selection_to_default() -> None:
    hilight_str = "0 120 215"
    hot_str = "0 102 204"

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BACKUP_REG_PATH, 0, winreg.KEY_READ) as bkp:
            try:
                val, _ = winreg.QueryValueEx(bkp, "Color_Hilight")
                if isinstance(val, str) and len(val.split()) == 3:
                    hilight_str = val
            except FileNotFoundError:
                pass
            try:
                val, _ = winreg.QueryValueEx(bkp, "Color_HotTrackingColor")
                if isinstance(val, str) and len(val.split()) == 3:
                    hot_str = val
            except FileNotFoundError:
                pass
    except OSError:
        pass

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, COLORS_REG_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "HotTrackingColor", 0, winreg.REG_SZ, hot_str)
        winreg.SetValueEx(key, "Hilight", 0, winreg.REG_SZ, hilight_str)

    def parse_rgb_str(s: str, fb: tuple[int, int, int]) -> tuple[int, int, int]:
        try:
            parts = [int(p) for p in s.split()]
            if len(parts) == 3:
                return (parts[0], parts[1], parts[2])
        except ValueError:
            pass
        return fb

    hi_rgb = parse_rgb_str(hilight_str, (0, 120, 215))
    hot_rgb = parse_rgb_str(hot_str, (0, 102, 204))

    elements = (ctypes.c_int * 2)(COLOR_HIGHLIGHT, COLOR_HOTLIGHT)
    colors = (ctypes.c_int * 2)(
        rgb_to_colorref(hi_rgb),
        rgb_to_colorref(hot_rgb),
    )
    user32.SetSysColors(2, elements, colors)
    broadcast_settings_change("Colors")


def perform_full_uninstall_cleanup() -> None:
    """Повністю відкатує зміни в системі та видаляє всі сліди програми з реєстру й диска."""
    reset_cursor_to_default()
    reset_selection_to_default()
    set_autostart_enabled(False)

    try:
        if os.path.isdir(APP_DATA_DIR):
            shutil.rmtree(APP_DATA_DIR, ignore_errors=True)
    except Exception:
        pass

    try:
        winreg.DeleteTree(winreg.HKEY_CURRENT_USER, APP_REG_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        pass


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
        ensure_initial_backup()

        self.root = tk.Tk()
        self.root.title("Wallpaper Accent Sync")
        self.root.configure(bg="#1C1C1E")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self._auto_apply_val: bool = load_bool_setting("AutoApply", True)
        self._enable_cursor_val: bool = load_bool_setting("EnableCursor", True)
        self._enable_selection_val: bool = load_bool_setting("EnableSelection", True)
        self._separate_colors_val: bool = load_bool_setting("SeparateColors", False)
        self._revert_on_exit_val: bool = load_bool_setting("RevertOnExit", False)

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

        self._build_ui()
        self._refresh_status_area()
        self._fit_and_center_window()

        if self.is_default_state and not self._auto_apply_val:
            self.sync_from_wallpaper(force_apply=False)
        elif self._auto_apply_val:
            self.sync_from_wallpaper(force_apply=True)
        else:
            self.sync_from_wallpaper(force_apply=False)
            self._apply_current_targets()

        self.root.after(0, self._start_tray)
        self.root.after(1500, self._poll_wallpaper_changes)

    def _start_tray(self) -> None:
        self.tray_icon = self._create_tray_icon()
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

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
            pystray.MenuItem(
                "Повернути стандартний вигляд",
                lambda: self.root.after(0, self.reset_defaults),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Змінювати курсор",
                lambda: self.root.after(0, self._toggle_cursor_from_tray),
                checked=lambda _: self._enable_cursor_val,
            ),
            pystray.MenuItem(
                "Змінювати виділення",
                lambda: self.root.after(0, self._toggle_selection_from_tray),
                checked=lambda _: self._enable_selection_val,
            ),
            pystray.MenuItem(
                "Окремі кольори (курсор / виділення)",
                lambda: self.root.after(0, self._toggle_separate_from_tray),
                checked=lambda _: self._separate_colors_val,
            ),
            pystray.MenuItem(
                "Автопідбір зі шпалер",
                lambda: self.root.after(0, self._toggle_auto_apply_from_tray),
                checked=lambda _: self._auto_apply_val,
            ),
            pystray.MenuItem(
                "Відкат до стандартного при виході",
                lambda: self.root.after(0, self._toggle_revert_on_exit_from_tray),
                checked=lambda _: self._revert_on_exit_val,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Видалити налаштування з системи",
                lambda: self.root.after(0, self.uninstall_and_quit),
            ),
            pystray.MenuItem(
                "Вихід",
                lambda: self.root.after(0, lambda: self.quit_app(restore_before_quit=self._revert_on_exit_val)),
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
        self._apply_current_targets()

    def _on_separate_colors_toggled(self) -> None:
        is_sep = self.separate_colors.get()
        self._separate_colors_val = is_sep
        save_bool_setting("SeparateColors", is_sep)

        if not is_sep:
            self.selection_color = self.cursor_color
            self._apply_current_targets()
        elif self._auto_apply_val and len(self.palette) >= 2:
            self.selection_color = self.palette[1]
            self._apply_current_targets()

        self._refresh_status_area()
        self._render_palette_cards()
        self._update_tray_appearance()
        self._fit_and_center_window()

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

    def _apply_current_targets(self) -> None:
        self.is_default_state = False
        save_bool_setting("IsDefaultState", False)

        if self._enable_cursor_val:
            apply_cursor_color(self.cursor_color)
        else:
            reset_cursor_to_default()

        if self._enable_selection_val:
            apply_selection_color(self.selection_color)
        else:
            reset_selection_to_default()

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

    def _poll_wallpaper_changes(self) -> None:
        sig = get_wallpaper_signature()
        if sig != self.last_signature and sig[0] is not None:
            self.sync_from_wallpaper(force_apply=self._auto_apply_val)
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
            thumb.thumbnail((420, 146), Image.Resampling.LANCZOS)
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

        perform_full_uninstall_cleanup()
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
        if should_restore:
            reset_cursor_to_default()
            reset_selection_to_default()
        if self.tray_icon is not None:
            self.tray_icon.stop()
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

    WallpaperAccentApp().run()