#!/usr/bin/env python3
"""JMGO Temp (браузерная версия) — read-only монитор JMGO по ADB (macOS / Windows / Linux).

Версия: см. VERSION ниже (нумерация общая с APK JMGO Temp).

Запуск:
    macOS:    python3 jmgo_dash.py [IP:порт]
    Windows:  py jmgo_dash.py [IP:порт]
    Страница откроется сама: http://localhost:8777

Нужны Python 3.9+ и adb (Android platform-tools). adb ищется в PATH или рядом со скриптом
(можно просто положить jmgo_dash.py в распакованную папку platform-tools).
IP:порт необязателен: скрипт возьмет уже подключенный проектор или найдет его в сети сам.

Что показывает:
  * температуру матрицы DMD и лазерных модулей, объектива и мощность вентиляторов — у системного
    сервиса JMGO, так же как приложение JMGO Temp: скрипт на время работы копирует на проектор
    маленький помощник (jmgotemp_probe.dex, ~5 КБ, только чтение), запускает его через adb и сразу
    удаляет файл; помощник сам завершается, когда скрипт закрыт. Если помощник не запустился —
    температуры берутся из logcat hippo_server (там они есть только при подробном журнале прошивки);
  * пороги защиты (снижение яркости / отключение) и цели вентиляторов — берутся прямо
    из конфигурации прошивки (hippo_configs.xml внутри APK com.jmgo.hippo);
  * уровень яркости и ток лазеров R/G/B (из /sys/class/jmgo_dlp/brightness);
  * процессор, вентиляторы, память, накопитель, вывод изображения.

Ничего на проекторе не меняет: только чтение (временный файл помощника удаляется сразу после запуска).
"""
from __future__ import annotations

import argparse
import base64
import collections
import http.server
import json
import math
import os
import random
import re
import shutil
import socketserver
import subprocess
import tempfile
import sys
import threading
import time
import webbrowser
from pathlib import Path

PORT = 8777
SCRIPT_DIR = Path(__file__).resolve().parent


def find_adb() -> str:
    exe = "adb.exe" if os.name == "nt" else "adb"
    candidates = [
        SCRIPT_DIR / exe,
        SCRIPT_DIR / "platform-tools" / exe,
        Path.home() / "platform-tools" / exe,
        Path("/opt/homebrew/bin/adb"),
        Path("/usr/local/bin/adb"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / exe,
        Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / exe,
    ]
    found = shutil.which("adb")
    if found:
        return found
    for c in candidates:
        if str(c) and c.is_file():
            return str(c)
    return "adb"


VERSION = "1.6.1"  # та же нумерация, что у APK JMGO Temp

ADB = find_adb()
TARGET = ""  # IP:порт из командной строки (необязательно)
HISTORY = collections.deque(maxlen=1800)  # примерно час при опросе раз в 2 секунды
LATEST: dict = {"error": "Подключение к проектору…", "error_code": "connecting"}


class DashError(RuntimeError):
    """Ошибка с кодом: текст переводится на странице (RU/EN)."""
    def __init__(self, code: str, message: str, *args: str):
        super().__init__(message)
        self.code, self.args_ = code, list(args)


def _console_ru() -> bool:
    import locale
    lang = os.environ.get("LANG") or os.environ.get("LC_ALL") or ""
    if not lang:
        try:
            lang = locale.getlocale()[0] or ""
        except Exception:
            lang = ""
    return lang.lower().startswith("ru") or lang.startswith("Russian")


CONSOLE_RU = _console_ru()
LOCK = threading.Lock()

THERMAL_FRESH_S = 40  # если дольше нет строк от hippo — данные считаем устаревшими

# Индексы датчиков в hippo: 7 — DMD, 10/11/12/14 — лазерные модули 1..4
LASER_INDEX = {10: 0, 11: 1, 12: 2, 14: 3}
# Коды, у которых название известно точно: N5 — проверено на проекторах, остальные — Google Play / FCC.
MODEL_NAMES = {
    "K981CN": "JMGO N5 Pro / N5S Pro", "K983CN": "JMGO N5S Pro Max", "K931CN": "JMGO N5 Ultra Max", "KD11CN": "JMGO N5S Ultra Max", "KD11AX": "JMGO IRIS Ultra Max",
    "K931AO": "JMGO N3 Ultimate", "S70A": "JMGO N1", "H80A": "JMGO N1 Pro",
    "K92A": "JMGO N1S Infinity", "K93A": "JMGO N1S Pro", "K610AO": "JMGO N1S SE",
    "T91A": "JMGO O2S Ultra", "m7642_JmGO_H79P": "JMGO O1", "m7642_JmGO_T72A": "JMGO O1 Pro",
}
DMD_INDEX = 7

# Запасные пороги (сняты с прошивки N5 Pro K981CN 1.1.31.23), если конфиг прочитать не удалось
DEFAULT_LIMITS = {
    "dmd_shutdown": 55.0,
    "laser_derate": 56.5,
    "laser_shutdown": 57.0,
    "pid_targets": [40.0, 46.0, 51.0],
    "interp_r": [360, 420, 480, 540, 600, 660, 720, 790, 870, 1023],
    "value_max": [1023, 1023, 1023],
    "source": "по умолчанию, как у N5 Pro: для этой модели в прошивке не нашлись",
    "from_firmware": False,
}


REMOTE_SCRIPT = r'''#!/system/bin/sh
val() { cat "$1" 2>/dev/null || true; }
# выключен с пульта (спящий режим): hippo ставит jmgo.system.power.state ≠ 0 (N5 Pro: 3) — ничего не читаем
PS=$(getprop jmgo.system.power.state); SL=$(getprop sys.jmgo.sleep)
echo "power_state=$PS"
if [ "$SL" = 1 ] || { [ -n "$PS" ] && [ "$PS" != 0 ]; }; then echo "asleep=1"; exit 0; fi
echo "model=$(getprop ro.product.model)"
echo "jmodel=$(getprop ro.boot.jmgo.model)"
echo "alias=$(getprop ro.device.alias)"
echo "device=$(getprop ro.product.device)"
echo "name=$(settings get global device_name 2>/dev/null)"
echo "platform=$(getprop ro.board.platform)"
echo "android=$(getprop ro.build.version.release)"
echo "firmware=$(getprop ro.build.display.id)"
echo "incremental=$(getprop ro.build.version.incremental)"
echo "fingerprint=$(getprop ro.build.fingerprint)"
echo "thermal_type=$(val /sys/class/thermal/thermal_zone0/type)"
echo "temp=$(val /sys/class/thermal/thermal_zone0/temp)"
# обороты — не каждый опрос: на N5S Ultra Max / IRIS чтение тахометра перезапускает общий замер,
# и проверка вентиляторов прошивки может получить ложный 0 об/мин (ошибка 4001)
[ "$READ_FAN" = 1 ] && echo "fan=$(val /sys/class/jmgo_fan/tachometer)"
echo "freq=$(val /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq)"
echo "freq_max=$(val /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq)"
echo "freq_cap=$(val /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq)"
echo "freq_min=$(val /sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq)"
echo "jplatform=$(getprop ro.jmgo.platform)"
echo "bw_status=$(getprop sys.jmgo.black_white_status)"
echo "cooling_state=$(val /sys/class/thermal/cooling_device0/cur_state)"
echo "cooling_max=$(val /sys/class/thermal/cooling_device0/max_state)"
echo "cooling_type=$(val /sys/class/thermal/cooling_device0/type)"
for i in 0 1 2 3; do
  echo "trip${i}_type=$(val /sys/class/thermal/thermal_zone0/trip_point_${i}_type)"
  echo "trip${i}_temp=$(val /sys/class/thermal/thermal_zone0/trip_point_${i}_temp)"
done
for z in /sys/class/thermal/thermal_zone*; do
  [ "$z" = "/sys/class/thermal/thermal_zone0" ] && continue
  zname=$(val "$z/type"); ztemp=$(val "$z/temp")
  echo "zone_extra=$(basename "$z")|$zname|$ztemp"
done
echo "dlp_brightness=$(val /sys/class/jmgo_dlp/brightness)"
echo "dlp_display_size=$(val /sys/class/jmgo_dlp/display_size)"
[ "$READ_DLPV" = 1 ] && echo "dlp_version=$(val /sys/class/jmgo_dlp/version)"
echo "dlp_source=$(val /sys/class/jmgo_dlp/source)"
echo "dlp_enable_vrr=$(val /sys/class/jmgo_dlp/enable_vrr)"
echo "dlp_model=$(getprop vendor.jmgo.dlp.model)"
echo "brightness=$(settings get global jmgo_display_brightness 2>/dev/null)"
echo "picture_backlight=$(settings get global picture_backlight 2>/dev/null)"
echo "picture_mode=$(settings get global picture_mode 2>/dev/null)"
echo "dynamic_black=$(settings get system dynamic_black_enabled 2>/dev/null)"
echo "motor_positions=$(settings list system 2>/dev/null | grep '^motor_position_' | tr '\n' ' ')"
# сырые отсчеты встроенного АЦП: по ним hippo считает температуру объектива (TEMPERATURE_LENS_CHANNEL_MUX)
for f in /sys/bus/iio/devices/iio:device*/name; do
  [ "$(cat "$f" 2>/dev/null)" = "1c020a00.saradc" ] || continue
  d=${f%/name}; echo "saradc=$(for i in 0 1 2 3 4 5 6 7 8 9; do cat "$d/in_voltage${i}_raw" 2>/dev/null || echo -; done | tr '\n' ' ')"
done
echo "uptime=$(cut -d. -f1 /proc/uptime)"
echo "loadavg=$(cat /proc/loadavg)"
CPU1=$(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8,$9}' /proc/stat)
sleep 0.2
CPU2=$(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8,$9}' /proc/stat)
echo "cpu_usage=$(awk -v a="$CPU1" -v b="$CPU2" 'BEGIN {split(a,x); split(b,y); ta=0;tb=0; idle=0; for(i=1;i<=8;i++){ta+=x[i];tb+=y[i]} idle=y[4]-x[4]; d=tb-ta; if(d>0) printf "%.0f",(d-idle)*100/d; else print "0"}')"
STORAGE=$(df -k /data/user/0 2>/dev/null | awk 'NR==2 {print $2" "$3" "$4}')
[ -z "$STORAGE" ] && STORAGE=$(df -k /data 2>/dev/null | awk 'NR==2 {print $2" "$3" "$4}')
echo "storage=$STORAGE"
echo "display_info=$(dumpsys display 2>/dev/null | grep -m1 'mBaseDisplayInfo')"
echo "mem_total=$(awk '/MemTotal/{print $2}' /proc/meminfo)"
echo "mem_available=$(awk '/MemAvailable/{print $2}' /proc/meminfo)"
'''

# Достаем конфиг hippo для своей модели прямо из APK на проекторе (только чтение).
# Как сам hippo: общий assets/hippo_configs.xml + assets/<папка>/hippo_configs.xml + уточнения
# -device-/-model-/-hw- (Build.DEVICE, ro.boot.dlpModel, ro.boot.hardware_version). Папка — ro.jmgo.product.device,
# затем ro.product.device и код модели (на N3/N1S папка не совпадает с ro.boot.jmgo.model).
CONFIG_SCRIPT = r'''#!/system/bin/sh
A=$(pm path com.jmgo.hippo 2>/dev/null | head -1 | cut -d: -f2)
echo "APK=$A"
[ -z "$A" ] && exit 0
L=$(unzip -l "$A" 2>/dev/null | awk '{print $4}' | grep '^assets/.*hippo_configs[^/]*\.xml$')
D=""
for c in "$(getprop ro.jmgo.product.device)" "$(getprop ro.product.device)" "$(getprop ro.boot.jmgo.model)" "$(getprop ro.product.model)"; do
  [ -n "$c" ] && echo "$L" | grep -qx "assets/$c/hippo_configs.xml" && { D=$c; break; }
done
echo "DIR=$D"
echo "DEVICE=$(getprop ro.product.device)"
echo "DLPMODEL=$(getprop ro.boot.dlpModel)"
echo "HW=$(getprop ro.boot.hardware_version)"
[ -z "$D" ] && exit 0
for f in assets/hippo_configs.xml $(echo "$L" | grep "^assets/$D/"); do
  echo "-----FILE $f-----"
  unzip -p "$A" "$f"
done
'''

_ELEM = re.compile(r'<(setting(?:-array|-2-dimen-array)?)\s[^>]*?key="([^"]+)"[^>]*?(?:/>|>.*?</\1\s*>)', re.S)


def merge_hippo_configs(raw: str) -> tuple[str, str]:
    """Склеивает файлы из CONFIG_SCRIPT в порядке hippo (XmlParser.parseAssetsConfigXml): общий,
    папка модели, затем -device-/-model-/-hw-, если их токен совпал. Возвращает (xml, «K931CN + hw-6»)."""
    head, _, rest = raw.partition("-----FILE ")
    info = dict(l.split("=", 1) for l in head.splitlines() if "=" in l)
    d = info.get("DIR", "").strip()
    files: dict[str, str] = {}
    for chunk in ("-----FILE " + rest).split("-----FILE ")[1:]:
        name, _, body = chunk.partition("-----\n")
        files[name.strip()] = body
    if not d or f"assets/{d}/hippo_configs.xml" not in files:
        return "", ""
    order = ["assets/hippo_configs.xml", f"assets/{d}/hippo_configs.xml"]
    extra = sorted(n for n in files if n.startswith(f"assets/{d}/hippo_configs") and n not in order)
    used = [d]
    for kind, want in (("-device-", info.get("DEVICE", "")), ("-model-", info.get("DLPMODEL", "")),
                       ("-hw-", info.get("HW", ""))):
        want = want.strip()
        if not want:
            continue
        for n in extra:
            f = n.rsplit("/", 1)[-1]
            if kind in f and want in re.split(r"[-.]", f):
                order.append(n)
                used.append(f[len("hippo_configs-"):-4])
    merged: dict[str, str] = {}
    for n in order:
        body = re.sub(r"<!--.*?-->", "", files.get(n, ""), flags=re.S)
        for m in _ELEM.finditer(body):
            merged[m.group(2)] = m.group(0)
    return "\n".join(merged.values()), " + ".join(used)


def run(cmd: list[str], timeout: float = 15, stdin: str | None = None) -> str:
    # Байты, а не text=True: на Windows текстовый режим превращает \n в \r\n, и sh на проекторе ломается.
    p = subprocess.run(
        cmd, input=stdin.encode("utf-8") if stdin is not None else None,
        capture_output=True, timeout=timeout,
    )
    out = p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    if p.returncode != 0:
        message = (p.stderr.decode("utf-8", "replace") or out).strip()
        raise RuntimeError(message or f"команда завершилась с кодом {p.returncode}")
    return out.strip()


def discover_device() -> str:
    if TARGET:
        try:
            run([ADB, "connect", TARGET], timeout=20)
        except Exception:
            pass
    try:
        devices = run([ADB, "devices"])
        for line in devices.splitlines():
            if "\tdevice" in line:
                serial = line.split()[0]
                if not TARGET or serial == TARGET:
                    return serial
    except FileNotFoundError:
        raise DashError("no_adb", "Не найден adb. Положите jmgo_dash.py в папку platform-tools или установите adb.")
    except Exception:
        pass
    if TARGET:
        raise DashError("connect", f"Не удалось подключиться к {TARGET}. Проверьте IP:порт в настройках беспроводной отладки.", TARGET)

    run([ADB, "start-server"], timeout=20)
    time.sleep(1.5)
    services = run([ADB, "mdns", "services"], timeout=15)
    for line in services.splitlines():
        if "_adb-tls-connect" in line:
            parts = line.split()
            if parts:
                endpoint = parts[-1]
                if re.match(r"^[^:]+:\d+$", endpoint):
                    run([ADB, "connect", endpoint], timeout=20)
                    time.sleep(0.8)
                    return endpoint
    raise DashError("not_found", "Проектор не найден. Включите беспроводную отладку ADB на проекторе и подключите компьютер к той же сети "
                    "(или запустите скрипт с адресом: jmgo_dash.py 192.168.x.x:порт).")


def adb_shell(serial: str, script: str = REMOTE_SCRIPT, timeout: float = 20) -> str:
    # Скрипт отдаем через stdin (`sh -s`): одинаково работает на macOS и Windows.
    return run([ADB, "-s", serial, "shell", "sh", "-s"], timeout=timeout, stdin=script)


# ---------------------------------------------------------------- конфиг прошивки

CONFIG_CACHE: dict[str, dict] = {}


def _float_rows(xml: str, key: str, tag: str = "setting-2-dimen-array") -> list[list[float]]:
    m = re.search(rf'key="{key}"[^>]*>(.*?)</{tag}>', xml, re.S)
    if not m:
        return []
    rows = []
    for item in re.findall(r"<item>(.*?)</item>", m.group(1), re.S):
        nums = re.findall(r"-?\d+(?:\.\d+)?", item)
        if nums:
            rows.append([float(n) for n in nums])
    return rows


def _float_list(xml: str, key: str) -> list[float]:
    m = re.search(rf'key="{key}"\s+value="([^"]*)"', xml)
    if not m:
        return []
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", m.group(1))]


def parse_hippo_config(xml: str, file_name: str) -> dict:
    xml = re.sub(r"<!--.*?-->", "", xml, flags=re.S)  # в комментариях лежат старые/примерные значения
    limits = dict(DEFAULT_LIMITS)
    limits["source"] = f"из прошивки этого проектора ({file_name})"
    limits["config"] = file_name
    limits["from_firmware"] = True
    check = _float_rows(xml, "SYSTEM_CHECK_TEMPERATURE_CONFIG")
    sensors = []
    laser_shut, laser_derate = [], []
    has_dmd = False
    laser_idx: set[int] = set()
    for row in check:
        if len(row) < 2:
            continue
        idx, shutdown = int(row[0]), row[1]
        derate = row[2] if len(row) > 2 and row[2] > 0 else None
        sensors.append({"index": idx, "shutdown": shutdown, "derate": derate})
        if idx == DMD_INDEX:
            limits["dmd_shutdown"] = shutdown
            has_dmd = True
        elif idx in LASER_INDEX:
            laser_idx.add(idx)
            laser_shut.append(shutdown)
            if derate:
                laser_derate.append(derate)
    # нет датчика в самопроверке модели — нет и порога (не подставляем значения N5 Pro)
    if not has_dmd:
        limits.pop("dmd_shutdown", None)
    if laser_shut:
        limits["laser_shutdown"] = min(laser_shut)
    else:
        limits.pop("laser_shutdown", None)
    if laser_derate:
        limits["laser_derate"] = min(laser_derate)
    else:
        limits.pop("laser_derate", None)
    laser_idx.update(int(v) for v in _float_list(xml, "INTELLIGENT_FAN_CONTROL") if int(v) in LASER_INDEX)
    limits["_laser_modules"] = sorted(LASER_INDEX[i] for i in laser_idx)
    limits["sensors"] = sensors
    pid = _float_rows(xml, "FAN_PID_CONTROL_CONFIG")
    if pid:
        limits["pid_targets"] = [r[0] for r in pid if r]
        # строка вида {50, 95, 95, 100}: мин = макс — это фиксированные обороты, а не цель PID
        limits["pid_fixed"] = [len(r) > 2 and r[1] >= r[2] for r in pid if r]
    interp = _float_rows(xml, "DISPLAY_BRIGHTNESS_INTERPOLATOR")
    if interp and len(interp[0]) >= 2:
        limits["interp_r"] = [int(v) for v in interp[0]]
    # фиксированные режимы яркости {mode, look, R, G, B, C}: Overlap, производительность, белый экран…
    limits["mode_currents"] = [[int(r[0])] + [int(v) for v in r[2:5]] for r in _float_rows(xml, "BRIGHTNESS_VALUE")
                               if len(r) >= 5 and r[0] > 0 and r[2] >= 0]
    vmax = _float_list(xml, "BRIGHTNESS_VALUE_MAX")
    if vmax:
        limits["value_max"] = [int(v) for v in vmax[:3]]
    # Новые модели (N5S Ultra Max) читают DMD не терморезистором, а через контроллер DLP,
    # и тогда действует свой порог DMD_TEMPERATURE_CHECK_CONFIG (у KD11CN — 71 °C).
    limits["_dmd_mux"] = _float_list(xml, "TEMPERATURE_DMD_CHANNEL_MUX")
    limits["_dmd_dlp_cfg"] = _float_list(xml, "DMD_TEMPERATURE_CHECK_CONFIG")
    # Диафрагма (N5S Ultra Max): ступени {F, положение мотора}; hippo хранит положение мотора
    # в Settings.System motor_position_<имя>, имя — MOTOR_DEVICE_NAME_CONFIG[DISPLAY_BRIGHTNESS_APERTURE_ID].
    # в общем файле hippo таблица-заглушка с положением -1: у модели без своей таблицы диафрагму не показываем
    limits["_aperture_gears"] = [(r[0], r[1]) for r in _float_rows(xml, "DISPLAY_APERTURE_GEAR_CONFIG") if len(r) >= 2 and r[1] >= 0]
    m = re.search(r'key="MOTOR_DEVICE_NAME_CONFIG"[^>]*>(.*?)</setting-array>', xml, re.S)
    names = re.findall(r"<item>\s*([^<\s]+)\s*</item>", m.group(1)) if m else []
    aid = _float_list(xml, "DISPLAY_BRIGHTNESS_APERTURE_ID")
    if aid and 0 <= int(aid[0]) < len(names):
        limits["_aperture_motor"] = names[int(aid[0])]
    # Объектив: {канал АЦП, R, B, mux, делитель} на встроенном АЦП (SAR_IIO_DEVICE); на N5 Pro/UM/Iris так и настроено
    lens = _float_list(xml, "TEMPERATURE_LENS_CHANNEL_MUX")
    m = re.search(r'key="SAR_IIO_DEVICE"[^>]*value="([^"]*)"', xml)
    if len(lens) >= 5 and lens[0] >= 0 and m and m.group(1):
        limits["_lens_mux"] = lens[:5]
        acc = _float_list(xml, "SAR_ADC_LEVEL_ACCURATE_MODE")
        limits["_adc_max"] = 1024 if acc and acc[0] == 1 else 255
    return limits


def lens_temperature(limits: dict, saradc: str) -> float | None:
    """Как TemperaturePolicy.getSarAdcTemperature в hippo: NTC (R, B) через делитель на 3,3 В."""
    mux = limits.get("_lens_mux")
    if not mux:
        return None
    vals = (saradc or "").split()
    ch, r0, beta, _, div = (int(v) for v in mux)
    try:
        level = int(vals[ch])
    except (IndexError, ValueError):
        return None
    v = level / limits.get("_adc_max", 1024) * 3.3
    if not 0 < v < 3.3:
        return None
    r = div * v / (3.3 - v) / r0
    t = 1 / (math.log(r) / beta + 0.00335402) - 273.15
    return round(t, 1) if -40 < t < 150 else None


def aperture_gear(limits: dict, positions: str) -> float | None:
    """F-число по положению мотора, как BaseDisplayPolicy.getApertureGear в hippo: совпадение или ближайшая ступень.
    Нет таблицы ступеней (N5 Pro) или нет настройки — None, диафрагму не показываем."""
    gears, name = limits.get("_aperture_gears") or [], limits.get("_aperture_motor")
    if not gears or not name:
        return None
    m = re.search(rf"(?:^|\s)motor_position_{re.escape(name)}=(-?\d+)", positions or "")
    if not m:
        return None
    pos = int(m.group(1))
    return min(gears, key=lambda g: abs(g[1] - pos))[0]


def dlp_version_int(text: str) -> int:
    """'6.0.0' -> 600, как в прошивке (a*100 + b*10 + c)."""
    try:
        p = [int(x) for x in re.findall(r"\d+", text or "")[:3]]
    except ValueError:
        return 0
    return sum(v * k for v, k in zip(p, (100, 10, 1)))


def dmd_by_dlp(mux: list, dlp_version: str) -> bool:
    """Повторяет TemperaturePolicy.getDMDTemperature()/isDmdTemperatureByDlp() из hippo.
    sar −2 (Iris Ultra/Ultra Max, hippo 9.4.x): DMD всегда из DLP, без проверки версии."""
    if mux and mux[0] == -2:
        return True
    if not mux or mux[0] < 0:
        return False
    if len(mux) >= 7 and mux[6] > 0:
        ver = dlp_version_int(dlp_version)
        if ver >= mux[6]:
            return True
        if ver > 0:
            return False
    return len(mux) >= 6 and mux[5] == 1


def get_limits(serial: str) -> dict:
    cached = CONFIG_CACHE.get(serial)
    if cached and (cached.get("_ok") or time.time() - cached.get("_ts", 0) < 120):
        return cached
    limits = dict(DEFAULT_LIMITS)
    limits["_ok"] = False
    try:
        raw = adb_shell(serial, CONFIG_SCRIPT, timeout=40)
        xml, cfg_name = merge_hippo_configs(raw)
        if xml.strip():
            limits = parse_hippo_config(xml, cfg_name)
            limits["_ok"] = True
    except Exception as exc:  # noqa: BLE001 — оставляем пороги по умолчанию
        limits["config_error"] = str(exc)
    limits["_ts"] = time.time()
    CONFIG_CACHE[serial] = limits
    return limits


# ---------------------------------------------------------------- поток logcat hippo

THERMAL_LOCK = threading.Lock()
THERMAL: dict = {}

RE_EPOCH = re.compile(r"^\s*(\d{9,11}\.\d+)")
RE_INDEX = re.compile(r"getTemForIntelligentIndex :(\d+) tem :(-?[\d.]+)")
RE_CHECK = re.compile(
    r"SystemCheck:\s+dmdTemperature:(-?[\d.]+) laserTemperature:([-\d. ]+?) cpuTemperature:(-?\d+)-?"
    r".*?autoFanTemperature:(-?[\d.]+) fanStatus:(-?\d+) fanSpeed:([\d-]+?)-? ERROR:(\w+)"
)
RE_PID = re.compile(
    r"FAN_PID mFactoryPidTemperature:(-?[\d.]+), currentTemperature:(-?[\d.]+), "
    r"fanSpeed:\[([\d, ]+)\], overlapLevel:(\d+), mode:(\w+)"
)
RE_SETFAN = re.compile(r"setFanSpeed :\[([\d, ]+)\]")


def _valid(v: float) -> float | None:
    return None if v <= -100 or v > 200 else round(v, 1)


def parse_log_line(line: str) -> None:
    now = time.time()
    m_epoch = RE_EPOCH.match(line)
    dev_ts = float(m_epoch.group(1)) if m_epoch else now
    upd: dict = {}
    m = RE_INDEX.search(line)
    if m:
        idx, val = int(m.group(1)), _valid(float(m.group(2)))
        if idx == DMD_INDEX:
            upd["dmd"] = val
        elif idx in LASER_INDEX:
            upd[f"laser{LASER_INDEX[idx]}"] = val
    m = RE_CHECK.search(line)
    if m:
        upd["dmd"] = _valid(float(m.group(1)))
        for i, v in enumerate(m.group(2).split()[:4]):
            try:
                upd[f"laser{i}"] = _valid(float(v))
            except ValueError:
                pass
        upd["hippo_cpu"] = int(m.group(3))
        upd["fan_status"] = int(m.group(5))
        upd["hippo_error"] = m.group(7).lower() == "true"
        upd["check_ts"] = dev_ts
    m = RE_PID.search(line)
    if m:
        upd["pid_temp"] = _valid(float(m.group(2)))
        upd["fan_duty"] = [int(x) for x in re.findall(r"\d+", m.group(3))]
        upd["overlap_level"] = int(m.group(4))
        upd["fan_mode"] = m.group(5)
    else:
        m = RE_SETFAN.search(line)
        if m:
            upd["fan_duty"] = [int(x) for x in re.findall(r"\d+", m.group(1))]
    if not upd:
        return
    with THERMAL_LOCK:
        THERMAL.update(upd)
        THERMAL["recv_ts"] = now
        THERMAL["dev_ts"] = dev_ts


# Помощник для проектора: собран из probe/webprobe/JmgoTempProbe.java (javac --release 11 + d8 --min-api 28).
# Раз в 2 с печатает JSON: getString(0x105) температуры, getString(0x138) объектив, getArray(0xa7) мощность
# вентиляторов, get(0x70) режим вентиляторов, get(0x334) Overlap. Только чтение, тахометр не трогает.
PROBE_DEX_B64 = """
ZGV4CjAzOQAOKVOejTy5Se4/fYGFYq6g6YJ7gyBnFOHMEwAAcAAAAHhWNBIAAAAAAAAAAPwSAAByAAAAcAAAABsAAAA4AgAAHQAA
AKQCAAAPAAAAAAQAACsAAAB4BAAAAQAAANAFAADcDQAA8AUAADgMAAA7DAAASQwAAFkMAABuDAAAeQwAAIcMAACVDAAAnwwAAKwM
AAC3DAAAvwwAAMIMAADHDAAAygwAANgMAADoDAAA/QwAAAsNAAAZDQAAIw0AADANAAA7DQAAQA0AAEQNAABHDQAAWw0AAF4NAABi
DQAAZg0AAGsNAABvDQAAgA0AAIQNAACJDQAAnw0AALQNAADQDQAA5w0AAPoNAAARDgAANA4AAEkOAABbDgAAfg4AAJIOAACmDgAA
wQ4AANUOAADpDgAABQ8AAAoPAAARDwAAHg8AACwPAAAvDwAAMw8AADcPAAA7DwAAPg8AAEUPAABIDwAATA8AAFAPAABkDwAAeQ8A
AI4PAACWDwAAsQ8AALkPAAC/DwAAyw8AAPsPAAArEAAANBAAAEQQAABXEAAAXBAAAGUQAABtEAAAdxAAAIoQAACSEAAAnhAAAKkQ
AACxEAAAwBAAAM8QAADVEAAA2hAAAN8QAADnEAAA8xAAAPkQAAABEQAABhEAABARAAAZEQAAIBEAAC8RAAA4EQAARBEAAFYRAABf
EQAAZhEAAHMRAAB9EQAAhxEAAI4RAACXEQAAoREAALYRAAC+EQAAwREAAAsAAAANAAAAGAAAAB8AAAAiAAAAIwAAACQAAAAlAAAA
JgAAACcAAAAoAAAAKQAAACoAAAArAAAALAAAAC0AAAAuAAAALwAAADAAAAAxAAAANgAAADoAAAA9AAAAPgAAAD8AAABAAAAAQQAA
AA0AAAABAAAAAAAAABYAAAABAAAA3AsAABcAAAABAAAA5AsAABgAAAACAAAAAAAAABoAAAAEAAAAAAAAABoAAAAFAAAAAAAAAB0A
AAAFAAAA3AsAACAAAAAIAAAA5AsAABwAAAALAAAA7AsAACEAAAAOAAAA9AsAABoAAAAPAAAAAAAAABwAAAAPAAAA7AsAACAAAAAP
AAAA/AsAACAAAAAPAAAA5AsAACEAAAAPAAAABAwAABsAAAAQAAAADAwAABwAAAAQAAAA7AsAAB4AAAAQAAAAFAwAACAAAAAQAAAA
5AsAACEAAAATAAAAHAwAADYAAAAUAAAAAAAAADcAAAAUAAAA7AsAADgAAAAUAAAAFAwAADkAAAAUAAAA5AsAADkAAAAUAAAAJAwA
ADoAAAAVAAAAAAAAADsAAAAVAAAALAwAABoAAAAWAAAAAAAAABoAAAAXAAAAAAAAAAMADwAMAAAAAwABAA4AAAADAAEADwAAAAMA
AQAQAAAAAwABABEAAAADAAEAEgAAAAMAAQATAAAAAwABABQAAAADAAEAFQAAAAMADwAyAAAAAwABADMAAAADAAEANAAAAAMAAQA1
AAAAAwAEAEwAAAARAAcAXgAAAAMAFAAKAAAAAwAGAEUAAAADAAQASQAAAAMACwBPAAAAAwALAFEAAAADAAsAUwAAAAMAGABXAAAA
AwANAGEAAAAEABkAVQAAAAQAGgBqAAAABQAcAEoAAAAFAAUAXQAAAAUAFABiAAAABQAAAGMAAAAFAAoAZAAAAAUABABlAAAABQAU
AGYAAAAFABUAbQAAAAUAFwBuAAAABwAZAEYAAAAHABcAYAAAAAgABwBNAAAACAATAFAAAAAKABcACgAAAAsAAgBfAAAACwAIAGwA
AAAMAAEAWAAAAAwAAQBZAAAADgAUAAoAAAAPAA4ATgAAAA8AGwBoAAAADwALAGwAAAAPAAwAbAAAABAAFAAKAAAAEAAXAAoAAAAQ
AA8ARAAAABAAEABEAAAAEAARAEQAAAAQABIARAAAABAACgBpAAAAEQADAEsAAAASABYAZwAAABMACQBUAAAAAwAAAAEAAAAOAAAA
AAAAABkAAADUEgAAaBIAAKoSAAAFAAAABQACAGgLAABcAAAAGgBDAHEQFQAAAAwAHAEPACQQGAABAAwBGgJSAG4wFgAgAQwAGgFW
ACQQGQABAAwBEgJuMCoAIAEMAB8ABAA4ADUAcQALAAAADAFxAAsAAAAMAhoDSABuIBIAMQASUxIEclQJADAhbhAMAAIAbhAPAAIA
DAA4AAoAAABuEBAAAQBuEBAAAgARACIACgAaA1oAcCAXADAAJwANAG4QEAABAG4QEAACACcAIgAKABoBWwBwIBcAEAAnACkAAAAR
AAEARAAAAAgAAQABAEwABQACAAUAAgB+CwAAKQAAAHEACwAAAAwAcQALAAAADAEaAkcAbiASACAAbiARAEAAYgQNABICclIJADQQ
bhAMAAEAAABuEBAAAAARAQ0DKAYNA24QEAABACcDbhAQAAAAJwMAAAgAAAARAAEAIQAAAAQABQACfwkgHgAeAAUAAQACAAEAjAsA
ADgAAAAaAFwAEmFxIAEAQQAMBG4QCgAEAAwBbhAQAAQAOQEDABEAIgQQABoCPABwICIAJAASAiETNTIRAD0CBwATAywAbiAjADQA
RAMBAm4gJAA0ANgCAgEo7xMBXQBuICMAFAAMBG4QJwAEAAwEEQQNBBEAAwAAADIAAQABAQk2AgABAAIAAQCYCwAAFQAAABIgcSAB
ABAADAFuEA0AAQAKAG4QEAABAHEQHwAAAAwBEQENARoBXAARAQAAAQAAAA8AAQABAQkRAwABAAIAAQCgCwAAGAAAABoAXAASQXEg
AQAhAAwCbhAOAAIADAFuEBAAAgA5AQMAKAVxEAcAAQAMABEADQIRAAMAAAASAAEAAQEJFgcAAQACAAAAqAsAAEcAAAAiABAAGgEA
AHAgIgAQAG4QHgAGAAwGIWESAhMDIgA1Ei8ASQQGAhMFXAAyNB8AM1QDACgbEwMgADU0FABxEBkABAAMAyQQGQADAAwDGgRCAHEg
HQA0AAwDbiAmADAAKAxuICMAQAAoCG4gIwBQAAwDbiAjAEMA2AICASjQbiAjADAADAZuECcABgAMBhEGAAABAAEAAQAAALYLAAAE
AAAAcBAcAAAADgAPAAEAAwACALoLAAAPAQAAGgBwABoBbwAh4j0CGgASAkYODgJxEBgADgAKDhMCPABxIBsA4gAKDhIScSAaAOIA
Cg6B4hYE6AO9QigEDQ4WAtAHYg4NADgOCgBiDg0AchAIAA4ACg45DggAcQACAAAADA5pDg0AcQAoAAAACwQTDgUBcRAFAA4ADA4T
BjgBcRAFAAYADAYTB6cAcRADAAcADAcTCHAAcRAEAAgADAgTCTQDcRAEAAkADAkTCgACcRAEAAoADAoTC2QAcRAEAAsADAsTDBMC
cRAEAAwADAwiDRAAcBAhAA0AbiAmAB0ADA1uMCUATQUMBBoFCQBuICYAVAAMBG4gJgDkAAwOGgQHAG4gJgBOAAwObiAmAG4ADA4a
BAUAbiAmAE4ADA5uICYAfgAMDhoEBgBuICYATgAMDm4gJgCOAAwOGgQIAG4gJgBOAAwObiAmAJ4ADA4aBAIAbiAmAE4ADA5uICYA
rgAMDhoEAwBuICYATgAMDm4gJgC+AAwOGgQBAG4gJgBOAAwObiAmAM4ADA5uICYADgAMDm4QJwAOAAwOKDANDhIEaQQNAHEAKAAA
AAsEcRAgAA4ADA5xEAcADgAMDiIGEABwECEABgBuICYAFgAMBm4wJQBGBQwEGgUEAG4gJgBUAAwEbiAmAOQADA5uICYADgAMDm4Q
JwAOAAwOYgQOAG4gFADkAGIODgBuEBMADgAKDjgOAwAOAHEgKQAyACkAFP8AAAgAAAARAAEAIQAAAKsABAACAQ0eAQnNARYADgES
D9ItiFpaPEstHmcciAJ3hgAlAgAADohaPGk8HjoeADsBAA7hPHgBEw+0ADABAA4BEQ8ANQEADgEWDwBEAQAOeMN4ARYPSHZAAA0A
DgBOAQAOSwEbEgEUD0tpaWlpaWlpAWsSGh48ASsQWpY8AAAAAgAAAAEAAQABAAAADwAAAAEAAAABAAAAAgAAAA4AGQABAAAADgAA
AAIAAAAPABkAAQAAAAAAAAABAAAAAgAAAAIAAAAPABgAAQAAABoAAAAEAAAAAQAFAAUAAQABIgAMLCJhcGVydHVyZSI6AA4sImJy
aWdodG5lc3MiOgATLCJicmlnaHRuZXNzX21vZGUiOgAJLCJlcnJvciI6AAwsImZhbl9kdXR5IjoADCwiZmFuX21vZGUiOgAILCJs
ZW5zIjoACywib3ZlcmxhcCI6AAksInRlbXBzIjoABjxpbml0PgABQwADRExQAAFJAAxJRFhfQVBFUlRVUkUADklEWF9CUklHSFRO
RVNTABNJRFhfQlJJR0hUTkVTU19NT0RFAAxJRFhfRkFOX0RVVFkADElEWF9GQU5fTU9ERQAISURYX0xFTlMAC0lEWF9PVkVSTEFQ
AAlJRFhfVEVNUFMAA0lJSQACSUwAAUoAEkptZ29UZW1wUHJvYmUuamF2YQABTAACTEMAAkxJAANMSUkAAkxKAA9MSm1nb1RlbXBQ
cm9iZTsAAkxMAANMTEwAFExhbmRyb2lkL29zL0lCaW5kZXI7ABNMYW5kcm9pZC9vcy9QYXJjZWw7ABpMZGFsdmlrL2Fubm90YXRp
b24vVGhyb3dzOwAVTGphdmEvaW8vUHJpbnRTdHJlYW07ABFMamF2YS9sYW5nL0NsYXNzOwAVTGphdmEvbGFuZy9FeGNlcHRpb247
ACFMamF2YS9sYW5nL0lsbGVnYWxTdGF0ZUV4Y2VwdGlvbjsAE0xqYXZhL2xhbmcvSW50ZWdlcjsAEExqYXZhL2xhbmcvTWF0aDsA
IUxqYXZhL2xhbmcvTnVtYmVyRm9ybWF0RXhjZXB0aW9uOwASTGphdmEvbGFuZy9PYmplY3Q7ABJMamF2YS9sYW5nL1N0cmluZzsA
GUxqYXZhL2xhbmcvU3RyaW5nQnVpbGRlcjsAEkxqYXZhL2xhbmcvU3lzdGVtOwASTGphdmEvbGFuZy9UaHJlYWQ7ABpMamF2YS9s
YW5nL3JlZmxlY3QvTWV0aG9kOwADU1lTAAVUX0dFVAALVF9HRVRfQVJSQVkADFRfR0VUX1NUUklORwABVgACVkkAAlZKAAJWTAAB
WgAFWklMTEkAAVsAAltDAAJbSQASW0xqYXZhL2xhbmcvQ2xhc3M7ABNbTGphdmEvbGFuZy9PYmplY3Q7ABNbTGphdmEvbGFuZy9T
dHJpbmc7AAZcdSUwNHgAGWFuZHJvaWQub3MuU2VydmljZU1hbmFnZXIABmFwcGVuZAAEY2FsbAAKY2hlY2tFcnJvcgAuY29tLmpt
Z28ubWlkZGxld2FyZS5kbHAuSUptR09EbHBNYW5hZ2VyU2VydmljZQAuY29tLmptZ28ubWlkZGxld2FyZS5zZXJ2aWNlLklKbUdP
U3lzdGVtU2VydmljZQAHY29ubmVjdAAOY3JlYXRlSW50QXJyYXkAEWN1cnJlbnRUaW1lTWlsbGlzAANkbHAAB2Zvck5hbWUABmZv
cm1hdAAIZ2V0QXJyYXkAEWdldERlY2xhcmVkTWV0aG9kAAZnZXRJbnQACmdldFNlcnZpY2UACWdldFN0cmluZwAGaW52b2tlAA1p
c0JpbmRlckFsaXZlAA1qbWdvbWlkZGxlLTAxAARtYWluAANtYXgAA21pbgAGbm9fZGxwAApub19zZXJ2aWNlAARudWxsAAZvYnRh
aW4AA291dAAIcGFyc2VJbnQAB3ByaW50bG4ABXF1b3RlAA1yZWFkRXhjZXB0aW9uAAdyZWFkSW50AApyZWFkU3RyaW5nABByZWFk
U3Ryb25nQmluZGVyAAdyZWN5Y2xlAAVzbGVlcAALdG9DaGFyQXJyYXkACHRvU3RyaW5nAAh0cmFuc2FjdAAFdmFsdWUAB3ZhbHVl
T2YACHdyaXRlSW50ABN3cml0ZUludGVyZmFjZVRva2VuAAZ7InRzIjoAAX0AnAF+fkQ4eyJiYWNrZW5kIjoiZGV4IiwiY29tcGls
YXRpb24tbW9kZSI6ImRlYnVnIiwiaGFzLWNoZWNrc3VtcyI6ZmFsc2UsIm1pbi1hcGkiOjI4LCJzaGEtMSI6ImFiYWFiNDY5YjVl
YmQ0ZGQyYmI5MWJhMGVkNmY0NTI3N2ZhYWU0Y2EiLCJ2ZXJzaW9uIjoiOC42LjItZGV2In0AAgYBaxwBGAkOAAgAABgBGAEYARgB
GAEYARgBGAEYARgBGAEYARgBCACBgASIEgEIzA0BCPALAQjIDgEI1A8BCJwQAQmgEgEI6BANF0ckEwIkAAIEZCSnAARwJDgBJDQD
JAUBF0gEAgQGBAQAAQAAAGASAAAAAAAAAAAAAAMAAAAAAAAAAQAAAMwSAAACAAAAzBIAAAYAAADMEgAAEQAAAAAAAAABAAAAAAAA
AAEAAAByAAAAcAAAAAIAAAAbAAAAOAIAAAMAAAAdAAAApAIAAAQAAAAPAAAAAAQAAAUAAAArAAAAeAQAAAYAAAABAAAA0AUAAAEg
AAAIAAAA8AUAAAMgAAAIAAAAaAsAAAEQAAALAAAA3AsAAAIgAAByAAAAOAwAAAQgAAABAAAAYBIAAAAgAAABAAAAaBIAAAUgAAAB
AAAAqhIAAAMQAAABAAAAzBIAAAYgAAABAAAA1BIAAAAQAAABAAAA/BIAAA==
"""
PROBE_REMOTE = "/data/local/tmp/jmgotemp_probe.dex"
PROBE_CMD = f"CLASSPATH={PROBE_REMOTE} app_process /system/bin JmgoTempProbe 2"


def _num(v) -> float | None:
    try:
        return _valid(float(v))
    except (TypeError, ValueError):
        return None


def parse_probe_line(line: str) -> bool:
    """Строка помощника -> THERMAL. True, если в ней были температуры."""
    try:
        d = json.loads(line)
        t = json.loads(d.get("temps") or "{}") if isinstance(d, dict) else {}
    except ValueError:
        return False
    if not isinstance(t, dict) or not t:
        return False
    upd: dict = {"dmd": _num(t.get("dmd_temperatures")), "lens": _num(d.get("lens")), "source": "probe"}
    for n in range(1, 5):
        if f"laser{n}_temperatures" in t:
            upd[f"laser{n - 1}"] = _num(t[f"laser{n}_temperatures"])
    duty = d.get("fan_duty")
    if isinstance(duty, list) and duty:
        upd["fan_duty"] = [int(x) for x in duty]
    if isinstance(d.get("fan_mode"), int) and d["fan_mode"] >= 0:
        upd["fan_mode"] = "PID" if d["fan_mode"] == 0 else "manual"
    if isinstance(d.get("overlap"), int) and d["overlap"] >= 0:
        upd["overlap_level"] = d["overlap"]
    with THERMAL_LOCK:
        THERMAL.update(upd)
        THERMAL["recv_ts"] = time.time()
        THERMAL["dev_ts"] = (d.get("ts") or 0) / 1000 or time.time()
    return True


class LogWatcher:
    """Источник DMD/лазеров: помощник на проекторе (как приложение), запасной — `adb logcat -s hippo_server:V`."""

    def __init__(self) -> None:
        self.serial = ""
        self.thread: threading.Thread | None = None
        self.stop_evt: threading.Event | None = None
        self.proc: subprocess.Popen | None = None

    def ensure(self, serial: str) -> None:
        if serial == self.serial and self.thread and self.thread.is_alive():
            return
        self.stop()
        with THERMAL_LOCK:
            THERMAL.clear()
        self.serial = serial
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(serial, self.stop_evt), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.stop_evt:
            self.stop_evt.set()
        p = self.proc
        if p and p.poll() is None:
            p.kill()  # помощник на проекторе завершится сам: adb закрыл его вывод
        self.proc = None
        self.serial = ""

    def _run(self, serial: str, stop: threading.Event) -> None:
        fails = 0
        while not stop.is_set():
            if fails < 3:
                fails = 0 if self._run_probe(serial, stop) else fails + 1
            else:
                self._run_logcat(serial, stop)
                fails = 2  # после журнала — еще одна попытка помощника
            stop.wait(3)

    def _run_probe(self, serial: str, stop: threading.Event) -> bool:
        """Копирует помощник, запускает, удаляет файл после первой строки. True — данные шли."""
        got = False
        tmp = ""
        try:
            fd, tmp = tempfile.mkstemp(suffix=".dex")
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode("".join(PROBE_DEX_B64.split())))
            run([ADB, "-s", serial, "push", tmp, PROBE_REMOTE], timeout=20)
            p = subprocess.Popen(
                [ADB, "-s", serial, "shell", PROBE_CMD],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            self.proc = p
            assert p.stdout is not None
            for line in p.stdout:
                if stop.is_set():
                    break
                if parse_probe_line(line) and not got:
                    got = True
                    try:  # помощник уже загружен в память — файл больше не нужен
                        run([ADB, "-s", serial, "shell", "rm", "-f", PROBE_REMOTE], timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
            if p.poll() is None:
                p.kill()
        except Exception:  # noqa: BLE001
            pass
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if not got:
                try:
                    run([ADB, "-s", serial, "shell", "rm", "-f", PROBE_REMOTE], timeout=10)
                except Exception:  # noqa: BLE001
                    pass
        return got

    def _run_logcat(self, serial: str, stop: threading.Event) -> None:
        """Запасной путь (2 мин), потом снова пробуем помощника."""
        if not stop.is_set():
            killer = None
            try:
                p = subprocess.Popen(
                    [ADB, "-s", serial, "logcat", "-v", "epoch", "-s", "hippo_server:V"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
                self.proc = p
                killer = threading.Timer(120, p.kill)
                killer.daemon = True
                killer.start()
                assert p.stdout is not None
                for line in p.stdout:
                    if stop.is_set():
                        break
                    parse_log_line(line)
                if p.poll() is None:
                    p.kill()
            except Exception:  # noqa: BLE001
                pass
            finally:
                if killer:
                    killer.cancel()


WATCHER = LogWatcher()


# ---------------------------------------------------------------- снимок состояния

def parse_dlp_current(raw: str) -> list[int] | None:
    m = re.match(r"R(\d+)G(\d+)B(\d+)", raw or "")
    return [int(m.group(i)) for i in (1, 2, 3)] if m else None


def brightness_level(r: int | None, interp: list[int]) -> tuple[int | None, bool]:
    """Ступень 1–10 по току красного лазера (обратная к DISPLAY_BRIGHTNESS_INTERPOLATOR)."""
    if r is None or not interp or len(interp) < 2:
        return None, False
    if r in interp:
        return interp.index(r) + 1, True
    if r <= interp[0]:
        return 1, False
    if r >= interp[-1]:
        return len(interp), False
    for i in range(len(interp) - 1):
        a, b = interp[i], interp[i + 1]
        if a <= r <= b:
            return int(i + 1 + (r - a) / (b - a) + 0.5), False
    return None, False


def menu_level(pct: int) -> int:
    """0–100 из настройки -> пункт меню 1–10. Ступени хранятся как (N-1)*100/9: 0, 11, 22 … 88, 100."""
    return max(1, min(10, int(pct * 9 / 100 + 0.5) + 1))


FAN_PERIOD = 10.0  # с: как часто читать обороты (± 1 с, чтобы не совпадать с проверкой прошивки раз в 10 с)
POLL_STATE: dict = {"serial": "", "next_fan": 0.0, "fans": [], "dlp_version": ""}


def parse_snapshot(serial: str) -> dict:
    st = POLL_STATE
    if st["serial"] != serial:
        st.update(serial=serial, next_fan=0.0, fans=[], dlp_version="")
    read_fan = time.time() >= st["next_fan"]
    read_dlpv = not st["dlp_version"]
    head = f"READ_FAN={1 if read_fan else 0}\nREAD_DLPV={1 if read_dlpv else 0}\n"
    raw = adb_shell(serial, head + REMOTE_SCRIPT.split("\n", 1)[1])
    data: dict[str, str] = {}
    extra_zone_lines: list[str] = []
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key == "zone_extra":
                extra_zone_lines.append(value.strip())
            else:
                data[key] = value.strip()
    if extra_zone_lines:
        data["zone_extra"] = "\n".join(extra_zone_lines)
    if data.get("asleep") == "1":
        # опрос на паузе: poll() остановит помощник и покажет «спящий режим», после включения все продолжится
        raise DashError("asleep", "Проектор выключен с пульта (спящий режим) — опрос на паузе.")

    def integer(key: str, default: int = 0) -> int:
        try:
            return int(data.get(key, ""))
        except (TypeError, ValueError):
            return default

    trips = {"passive": []}
    for i in range(4):
        temp = integer(f"trip{i}_temp")
        kind = data.get(f"trip{i}_type", "")
        if temp and kind == "passive":
            trips["passive"].append(temp // 1000)
        elif temp and kind:
            trips[kind] = temp // 1000

    if read_fan:
        st["next_fan"] = time.time() + FAN_PERIOD + random.uniform(-1.0, 1.0)
        if "fan" in data:
            new = [int(x) for x in re.findall(r"\d+", data.get("fan", ""))]
            # одиночный «0 об/мин» после > 300 — сбой чтения тахометра: оставляем прошлое значение;
            # 3 нуля подряд (≈ 30 с) — вентилятор правда стоит
            old, zeros = st["fans"], st.setdefault("zeros", {})
            for i, v in enumerate(new):
                prev = old[i] if i < len(old) else None
                if v == 0 and prev and prev > 300 and zeros.get(i, 0) + 1 < 3:
                    zeros[i] = zeros.get(i, 0) + 1
                    new[i] = prev
                elif v != 0:
                    zeros[i] = 0
            st["fans"] = new
    if read_dlpv and data.get("dlp_version"):
        st["dlp_version"] = data["dlp_version"]
    data["dlp_version"] = st["dlp_version"]
    fans = list(st["fans"])
    temp_raw = integer("temp")
    mem_total = integer("mem_total")
    mem_available = integer("mem_available")
    platform = data.get("platform", "")
    model = data.get("jmodel") or data.get("model", "")
    raw_brightness = integer("brightness", -1)
    raw_name = data.get("name", "JMGO")
    extra_zones = []
    for raw_zone in data.get("zone_extra", "").splitlines():
        parts = raw_zone.split("|", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            try:
                extra_zones.append({"name": parts[1], "temp": int(parts[2]) // 1000})
            except ValueError:
                pass
    dlp_size = data.get("dlp_display_size", "").split()
    try:
        dlp_width, dlp_height = int(dlp_size[0]), int(dlp_size[1])
    except (IndexError, ValueError):
        dlp_width = dlp_height = None
    friendly_name = re.sub(r"-\d{4}$", "", raw_name).replace("JMGO-", "JMGO ", 1)
    load_parts = data.get("loadavg", "").split()
    try:
        load_1m = float(load_parts[0])
    except (IndexError, ValueError):
        load_1m = None
    storage_parts = data.get("storage", "").split()
    try:
        storage_total = int(storage_parts[0])
        storage_used = int(storage_parts[1])
        storage_free = int(storage_parts[2])
    except (IndexError, ValueError):
        storage_total = storage_used = storage_free = None
    display_info = data.get("display_info", "")
    display_match = re.search(r"real (\d+) x (\d+)", display_info)
    refresh_match = re.search(r"modes \[\{[^]]*?fps=([0-9.]+)", display_info)

    # китайская прошивка сама задает торговое имя (ro.device.alias): K941CN «N5 Ultra», K951CN «N5 Pro Max»
    alias = data.get("alias", "").strip()
    if alias:
        friendly_name = alias if alias.upper().startswith("JMGO") else "JMGO " + alias
    friendly_name = next((MODEL_NAMES[c] for c in (model, data.get("model", ""), data.get("device", ""))
                          if c in MODEL_NAMES), friendly_name)

    return {
        "serial": serial,
        "name": friendly_name,
        "raw_name": raw_name,
        "model": model,
        "platform": platform,
        "chip": f"MediaTek {data['jplatform'].upper()}" if data.get("jplatform") else "",
        "android": data.get("android", ""),
        "firmware": data.get("firmware", "") or data.get("incremental", ""),
        "firmware_version": data.get("incremental", "") or data.get("firmware", ""),
        "fingerprint": data.get("fingerprint", ""),
        "thermal_type": data.get("thermal_type", ""),
        "temp": temp_raw // 1000 if temp_raw else None,
        "fan1": fans[0] if len(fans) > 0 else None,
        "fan2": fans[1] if len(fans) > 1 else None,
        "freq": integer("freq") // 1000 or None,
        "freq_max": integer("freq_max") // 1000 or None,
        "freq_hw": integer("freq_max") // 1000 or None,
        "freq_cap": integer("freq_cap") // 1000 or None,
        "freq_min": integer("freq_min") // 1000 or None,
        "bw_status": integer("bw_status", -1),
        "cooling_state": integer("cooling_state"),
        "cooling_max": integer("cooling_max"),
        "cooling_type": data.get("cooling_type", ""),
        "trip_hot": trips.get("hot"),
        "trip_passive0": trips["passive"][0] if len(trips["passive"]) > 0 else None,
        "trip_passive1": trips["passive"][1] if len(trips["passive"]) > 1 else None,
        "trip_critical": trips.get("critical"),
        "brightness": data.get("brightness", ""),
        "brightness_raw": raw_brightness,
        "extra_zones": extra_zones,
        "dlp_brightness": data.get("dlp_brightness", ""),
        "dlp_version": data.get("dlp_version", ""),
        "dlp_model": data.get("dlp_model", ""),
        "dlp_source": data.get("dlp_source", ""),
        "dlp_enable_vrr": data.get("dlp_enable_vrr", ""),
        "dlp_width": dlp_width,
        "dlp_height": dlp_height,
        "picture_mode": data.get("picture_mode", ""),
        "dynamic_black": integer("dynamic_black", -1),
        "motor_positions": data.get("motor_positions", ""),
        "saradc": data.get("saradc", ""),
        "uptime": integer("uptime"),
        "load_1m": load_1m,
        "cpu_usage": integer("cpu_usage", -1),
        "storage_total_kb": storage_total,
        "storage_used_kb": storage_used,
        "storage_free_kb": storage_free,
        "output_width": int(display_match.group(1)) if display_match else None,
        "output_height": int(display_match.group(2)) if display_match else None,
        "output_hz": round(float(refresh_match.group(1))) if refresh_match else None,
        "mem_total": mem_total,
        "mem_avail": mem_available,
        "ts": int(time.time()),
    }


SESSION: dict = {"serial": "", "start": 0, "max": {}}


def enrich(snapshot: dict, limits: dict) -> None:
    """Добавляет в снимок данные hippo (DMD/лазеры), пороги, уровень яркости, максимумы."""
    with THERMAL_LOCK:
        th = dict(THERMAL)
    saradc = snapshot.pop("saradc", "")
    age = time.time() - th["recv_ts"] if "recv_ts" in th else None
    fresh = age is not None and age < THERMAL_FRESH_S
    mods = limits.get("_laser_modules") or range(4)  # 3-лазерные модели: лишний модуль не показываем
    lasers = [th.get(f"laser{i}") for i in mods]
    lasers = [v for v in lasers if v is not None]
    thermal = {
        "fresh": fresh,
        "age": round(age) if age is not None else None,
        "dmd": th.get("dmd") if fresh else None,
        "lasers": lasers if fresh else [],
        "laser_max": max(lasers) if fresh and lasers else None,
        # объектив: у помощника — как в приложении; без него — по сырому АЦП (формула hippo)
        "lens": th.get("lens") if fresh and th.get("lens") is not None else lens_temperature(limits, saradc),
        "pid_temp": th.get("pid_temp") if fresh else None,
        "fan_duty": th.get("fan_duty") if fresh else None,
        "fan_mode": th.get("fan_mode"),
        "overlap_level": th.get("overlap_level"),
        "fan_status": th.get("fan_status"),
        "hippo_error": th.get("hippo_error"),
        "source": th.get("source", "logcat" if th else None),  # probe — помощник, logcat — журнал прошивки
    }
    snapshot["thermal"] = thermal
    public_limits = {k: v for k, v in limits.items() if not k.startswith("_")}
    public_limits["dmd_source"] = "ntc"
    cfg = limits.get("_dmd_dlp_cfg") or []
    by_dlp = dmd_by_dlp(limits.get("_dmd_mux") or [], snapshot.get("dlp_version", ""))
    if by_dlp:
        # KD11CN: свой порог DMD_TEMPERATURE_CHECK_CONFIG; KD11AX: его нет, порог уже в строке {7, 71, -1}
        public_limits["dmd_source"] = "dlp"
    if by_dlp and cfg and cfg[0] > 0:
        public_limits["dmd_shutdown"] = cfg[0]
        if len(cfg) > 1 and cfg[1] > 0:
            public_limits["dmd_derate"] = cfg[1]
    snapshot["limits"] = public_limits

    cur = parse_dlp_current(snapshot.get("dlp_brightness", ""))
    vmax = limits.get("value_max") or [1023, 1023, 1023]
    snapshot["laser_current"] = cur
    snapshot["laser_current_pct"] = [round(c * 100 / (vmax[i] or 1023)) for i, c in enumerate(cur)] if cur else None
    # Яркость из меню хранится в настройке (0–100). Фиксированные режимы (Overlap, производительность,
    # белый экран) узнаем по точному совпадению тока лазеров с таблицей BRIGHTNESS_VALUE из прошивки.
    mode = -1
    if cur:
        for row in limits.get("mode_currents", []):
            if all(abs(cur[i] - row[i + 1]) <= 2 for i in range(3)):
                mode = row[0]
                break
    snapshot["brightness_mode"] = mode
    raw = snapshot.get("brightness_raw", -1)
    fixed = mode in (15, 16, 22, 23)
    cur_level = None if fixed else brightness_level(cur[0] if cur else None, limits.get("interp_r", []))[0]
    if raw is not None and raw >= 0:
        snapshot["brightness_pct"] = raw
        snapshot["brightness_menu"] = menu_level(raw)
        snapshot["brightness_exact"] = True
    else:
        snapshot["brightness_menu"] = cur_level
        snapshot["brightness_exact"] = False
    # AL 2.0 (Dynamic Black): прошивка сама пишет флаг; ток лазеров меняется по сцене — показываем ступень по току
    al = snapshot.get("dynamic_black", -1)
    if al is not None and al >= 0:
        snapshot["al_enabled"] = al == 1
    if cur_level is not None and (al == 1 or ((al is None or al < 0) and cur_level != snapshot.get("brightness_menu"))):
        snapshot["brightness_actual"] = cur_level
    ap = aperture_gear(limits, snapshot.pop("motor_positions", ""))
    if ap:
        snapshot["aperture"] = ap

    # частота процессора и троттлинг: система снижает потолок частоты (scaling_max_freq) или включает охлаждение
    hw, cap, fq = snapshot.get("freq_hw"), snapshot.get("freq_cap"), snapshot.get("freq")
    snapshot["freq_pct"] = round(fq * 100 / hw) if fq and hw else None
    # Троттлинг — только когда процессор правда горячий (не дальше 15 °C до порога): на платформе N3 / IRIS
    # потолок частоты и cooling_device0 бывают «ниже нормы» и при 60–70 °C. Потолок сравниваем с наибольшим
    # за сеанс, охлаждение учитываем только процессорное; состояние меняется после 2 опросов подряд.
    if cap and cap > SESSION.get("cap_base", 0):
        SESSION["cap_base"] = cap
    trip = snapshot.get("trip_passive0") or 123
    hot = (snapshot.get("temp") or 0) >= trip - 15
    cpu_cool = "cpu" in (snapshot.get("cooling_type") or "").lower() and (snapshot.get("cooling_state") or 0) > 0
    raw_thr = bool(hot and ((cap and cap < SESSION.get("cap_base", cap)) or cpu_cool))
    if raw_thr != SESSION.get("thr_state", False):
        SESSION["thr_streak"] = SESSION.get("thr_streak", 0) + 1
        if SESSION["thr_streak"] >= 2:
            SESSION["thr_state"], SESSION["thr_streak"] = raw_thr, 0
    else:
        SESSION["thr_streak"] = 0
    thr = SESSION.get("thr_state", False)
    now_s = int(time.time())
    if thr:
        SESSION.setdefault("thr_since", now_s)
        SESSION.setdefault("thr_first", now_s)
    else:
        SESSION.pop("thr_since", None)
    snapshot["throttled"] = thr
    snapshot["throttle_since"] = SESSION.get("thr_since")
    snapshot["throttle_first"] = SESSION.get("thr_first")

    # сбойные «0 об/мин» отфильтрованы при чтении тахометра (parse_snapshot), здесь значения уже из кэша

    # максимумы за сеанс работы дашборда
    if SESSION["serial"] != snapshot["serial"]:
        SESSION.clear()
        SESSION.update(serial=snapshot["serial"], start=int(time.time()), max={})
    mx = SESSION["max"]
    for key, val in (("dmd", thermal["dmd"]), ("laser", thermal["laser_max"]), ("cpu", snapshot.get("temp"))):
        if val is not None and (mx.get(key) is None or val > mx[key]):
            mx[key] = val
    snapshot["session_max"] = dict(mx)
    snapshot["session_start"] = SESSION["start"]
    snapshot["app_version"] = VERSION


def poll() -> None:
    global LATEST
    serial = ""
    while True:
        try:
            if not serial:
                serial = discover_device()
            snapshot = parse_snapshot(serial)
            WATCHER.ensure(serial)
            limits = get_limits(serial)
            enrich(snapshot, limits)
            th = snapshot["thermal"]
            with LOCK:
                LATEST = snapshot
                HISTORY.append({
                    "ts": snapshot["ts"],
                    "temp": snapshot["temp"],
                    "fan1": snapshot["fan1"],
                    "fan2": snapshot["fan2"],
                    "dmd": th["dmd"],
                    "l": th["lasers"],
                    "load": snapshot.get("cpu_usage"),
                    "fpct": snapshot.get("freq_pct"),
                    "thr": snapshot.get("throttled", False),
                })
        except Exception as exc:
            # Wi‑Fi‑соединение adb может «зависнуть»: adb devices показывает device, а shell молчит.
            # Переподключаемся с нуля, чтобы дашборд сам ожил.
            if serial and ":" in serial and isinstance(exc, subprocess.TimeoutExpired):
                for cmd in (["disconnect", serial], ["connect", serial]):
                    try:
                        run([ADB, *cmd], timeout=15)
                    except Exception:
                        pass
            serial = ""
            WATCHER.stop()
            if isinstance(exc, DashError) and exc.code == "asleep":
                # «Максимум за сеанс» начинается заново после выключения с пульта (как график за час)
                SESSION["max"] = {}
                SESSION["start"] = int(time.time())
                SESSION.pop("thr_first", None)
                SESSION.pop("thr_since", None)
                time.sleep(3)  # во сне проверяем раз в ~5 с, как приложение
            if isinstance(exc, subprocess.TimeoutExpired):
                err = {"error": "Проектор не отвечает по ADB (переподключаюсь…). Если не проходит — выключите и включите "
                                "беспроводную отладку на проекторе.", "error_code": "timeout"}
            elif isinstance(exc, DashError):
                err = {"error": str(exc), "error_code": exc.code, "error_args": exc.args_}
            else:
                err = {"error": str(exc), "error_code": "adb"}
            with LOCK:
                LATEST = {**err, "ts": int(time.time())}
                HISTORY.clear()
        time.sleep(2)


HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JMGO Temp</title>
<style>
:root{color-scheme:dark;--bg:#0f1115;--panel:#191c22;--panel2:#20242c;--line:#303641;--text:#f2f4f7;--muted:#a3aab6;--green:#52d273;--yellow:#f2bd55;--red:#f16b68;--blue:#72aaff;--orange:#ff9f43}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:1.75vh/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Roboto,Arial,sans-serif;overflow:hidden}
main{height:100vh;padding:1.6vh 1.8vw 1.4vh;display:flex;flex-direction:column;gap:1.1vh}
h1{font-size:3vh;line-height:1.15;margin:0;font-weight:700}
.muted{color:var(--muted)}
.head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start}
.ttl{display:flex;flex-direction:column;align-items:flex-start;gap:.5vh;min-width:0}.ttl h1{line-height:1}.ttl-top{display:flex;align-items:center;gap:1vw}
.brand{display:inline-flex;align-items:center;gap:.5vw;align-self:center;color:var(--muted);font-size:1.9vh;font-weight:600;white-space:nowrap}.brand svg{height:3.2vh;width:3.2vh;flex:none}
html.narrow .brand b{display:none}
.device{color:var(--muted);font-size:1.5vh;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.device{display:none}
.hdr-right{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.btn{background:var(--panel2);color:var(--text);border:2px solid var(--line);border-radius:10px;padding:.6vh 1vw;font:inherit;font-size:1.6vh;cursor:pointer}
.btn:focus{outline:none;border-color:var(--blue);background:#24304a}.btn.on{border-color:var(--green)}
.status{white-space:nowrap;color:var(--green);font-size:1.5vh}.status.off{color:var(--red)}.is-app .status:not(.off){display:none}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:currentColor;margin-right:7px}
.error,.alert{padding:.8vh 1vw;border-radius:12px;font-size:1.5vh}
.error{background:#321b1e;border:1px solid #7a373d;color:#ffb4b1}
.alert{background:#2e2716;border:1px solid #6b5a2a;color:#ffe2a3}
.metric{background:var(--panel);border:1px solid var(--line);border-radius:1vh;padding:1.1vh 1vw;min-width:0}
.metric .label{color:var(--muted);font-size:1.45vh;margin-bottom:.4vh}
.metric .big{font-size:5vh;line-height:1;font-weight:700;letter-spacing:-.04em}
.metric .value{font-size:2.7vh;font-weight:650}
#brightness{font-size:6.6vh}#brightness.dense{font-size:5vh}
.metric .unit{font-size:.5em;color:var(--muted);font-weight:400;letter-spacing:0}
.metric .hint,.metric-sub{font-size:1.35vh;color:var(--muted);margin-top:.4vh}
.big.warn,.value.warn,b.warn{color:var(--yellow)}.big.hot,.value.hot,b.hot{color:var(--red)}
.thr{color:var(--red);font-weight:600}
.version-line,.zone-line{display:flex;justify-content:space-between;gap:8px;font-size:1.4vh;margin-top:.2vh}
.version-line span,.zone-line span{color:var(--muted)}
.version-line b,.zone-line b{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dmd-grid{display:grid;grid-template-columns:auto auto;justify-content:space-between;column-gap:1vw;align-items:baseline}
.dmd-grid>:nth-child(1){grid-area:1/1}.dmd-grid>:nth-child(2){grid-area:1/2}.dmd-grid>:nth-child(3){grid-area:2/1}.dmd-grid>:nth-child(4){grid-area:2/2}
.metric.xl .big{font-size:6.6vh}.metric.xl .metric-sub{font-size:1.6vh;margin-top:.8vh}.metric.xl .cpu-pair .sep{font-size:4.6vh}
.dmd-grid:not(.has-lens) .lens-x{display:none}
.ultra-extra{display:none}.ultra-extra.visible{display:block}
.cpu-pair{display:flex;align-items:baseline;gap:1vw}.cpu-pair .sep{color:var(--line);font-size:3.6vh;font-weight:300}
.fan-pair{display:flex;gap:8px}.fan-item{flex:1;min-width:0;text-align:center}
.fan-value{font-size:5vh;line-height:1.1;font-weight:650;font-variant-numeric:tabular-nums}.fan-name{font-size:1.25vh;color:var(--muted);white-space:nowrap}
.laser-row{display:flex;gap:6px;margin-top:.6vh}
.laser-chip{flex:1;background:var(--panel2);border-radius:7px;padding:.3vh 0;text-align:center;font-size:1.5vh;font-variant-numeric:tabular-nums}
.laser-chip small{display:block;color:var(--muted);font-size:1.1vh}
.bar{height:.6vh;background:var(--panel2);border-radius:5px;overflow:hidden;margin-top:.7vh}
.bar i{display:block;height:100%;background:var(--green);border-radius:5px;width:0;transition:width .4s,background .4s}.bar i.warn{background:var(--yellow)}.bar i.hot{background:var(--red)}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:.8vw}
.charts{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);grid-template-rows:minmax(0,1fr);gap:.8vw}
.chart{background:var(--panel);border:1px solid var(--line);display:flex;flex-direction:column;min-height:0;padding:1vh 1vw;border-radius:1vh}
.chart-title{font-weight:600;font-size:1.6vh;margin-bottom:.4vh}.chart-sub{font-weight:400;color:var(--muted);font-size:.85em}
.chart canvas{display:block;width:100%;flex:1;min-height:0}
.chart-legend{display:flex;justify-content:center;gap:15px;flex-wrap:wrap;color:var(--muted);font-size:1.3vh;margin-top:.3vh}
.legend-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.infos{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:.8vw}.infos .metric{padding:.9vh .9vw}
.infos .metric .value{font-size:3.4vh}.infos .metric .hint{font-size:1.55vh}
#output{font-size:2.9vh;white-space:nowrap}#output .unit{display:block;font-size:1.55vh;white-space:nowrap}
.prot{display:block}
.threshold{background:var(--panel);border:1px solid var(--line);padding:.7vh .8vw;border-radius:1vh}
.threshold b{display:block;font-size:2vh}.threshold span{display:block;color:var(--muted);font-size:1.25vh;line-height:1.25}
.threshold.hot b{color:var(--yellow)}.threshold.critical b{color:var(--red)}.threshold.info b{color:var(--blue)}
.note{color:var(--muted);font-size:1.3vh;line-height:1.35}
.app-only{display:none}.is-app .app-only{display:inline-block}
.btn.don{color:var(--orange);border-color:#6b4a26}.btn.don:focus{color:var(--orange);border-color:var(--blue)}
.btn .hrt{color:#ff4d5a}
#donate{position:fixed;top:0;right:0;bottom:0;left:0;background:#000000eb;display:none;align-items:center;justify-content:center;z-index:20}#donate.open{display:flex}
.dn-box{background:var(--panel);border:1px solid var(--line);border-radius:2vh;padding:4vh 3vw;display:flex;gap:3vw;align-items:center;max-width:86vw}
#warn,#tgt,#ovs{position:fixed;top:0;right:0;bottom:0;left:0;z-index:20;visibility:hidden;background:transparent;transition:background .18s,visibility 0s .18s}#warn.open,#tgt.open,#ovs.open{visibility:visible;background:#0000005c;transition:background .18s}
.wn-box{position:absolute;top:8vh;right:2vw;background:var(--panel);border:1px solid var(--line);border-radius:1.2vh;padding:1.3vh .9vw 1.2vh;width:max-content;box-shadow:0 1.6vh 5vh #000c;
 transform-origin:var(--ox,90%) 0;opacity:0;transform:translateY(-1vh) scale(.94,.6);transition:opacity .18s,transform .18s cubic-bezier(.2,.8,.3,1)}
#warn.open .wn-box,#tgt.open .wn-box,#ovs.open .wn-box{opacity:1;transform:none}
.wn-box::before{content:'';position:absolute;top:-9px;right:var(--cx,60px);width:16px;height:16px;background:var(--panel);border-left:1px solid var(--line);border-top:1px solid var(--line);transform:rotate(45deg)}
.wn-box p{margin:0 0 1vh;color:var(--muted);font-size:1.45vh;line-height:1.35;width:0;min-width:100%}
.wn-row{display:flex;justify-content:flex-start;align-items:center;gap:.8vw;width:100%;text-align:left;margin:0 0 .5vh;padding:.55vh .7vw;font-size:1.65vh}
.wn-row small{color:var(--muted);font-size:1.4vh;margin-left:.5vw}.wn-row.all{margin-bottom:1vh}
.wn-row.on{border-color:var(--line)}.wn-row:focus{border-color:var(--blue)}
.wn-row b{flex:none;min-width:3.6vw;text-align:center;border-radius:7px;padding:.2vh .5vw;font-size:1.55vh;background:#2b3038;color:var(--muted)}.wn-row.on b{background:#1f3a29;color:var(--green)}
.wi{color:var(--yellow)}#updBtn,#donBtn,.is-app #warnBtn,.is-app #ovBtn{display:inline-flex;align-items:center;gap:.45vw}.btn .wi,.btn .ui,.btn .ic,.btn .hrt{display:inline-flex;align-items:center;line-height:1}.ico{width:1.35em;height:1.35em;display:block;flex:none}
.lhead{display:flex;justify-content:space-between;align-items:center;gap:.5vw}.lhead .label{margin-bottom:0}
.btn.tgt{font-size:1.35vh;padding:.1vh .6vw;border-width:1px;border-radius:7px;color:var(--muted);margin:-.4vh 0 .4vh;white-space:nowrap}.btn.tgt.on{color:var(--orange);border-color:#6b4a26}.btn.tgt:focus{color:var(--text);border-color:var(--blue)}
#tgt .tgt-reset{color:var(--orange);font-weight:600;font-size:1.45vh;display:inline-flex;align-items:center;gap:.35vw}#tgt .tr-ico{width:1.25em;height:1.25em;flex:none}
#tgt .wn-box p,#ovs .wn-box p{font-size:1.3vh;margin:0 0 .6vh}#tgt .tgt-reset{font-size:1.3vh}#tgt .wn-btns,#ovs .wn-btns{margin-top:.6vh;flex-direction:column;align-items:flex-start;gap:.3vh}#tgt .wn-hint,#ovs .wn-hint{font-size:1.25vh}
#tgtList{display:flex;flex-direction:column;gap:.25vh}#tgtList .wn-row{font-size:1.4vh;padding:.3vh .5vw;margin:.5vh 0 0;width:auto}#tgtList .wn-row b{font-size:1.3vh;min-width:2.6vw}
.st-head{display:flex;gap:.5vw;color:var(--muted);font-size:1.25vh;padding:0 0 .2vh}.st-head span:first-child,.st-lbl{width:3.6vw;flex:none;box-sizing:border-box}
.st-lbl{font-variant-numeric:tabular-nums}
.st-row{display:flex;align-items:center;gap:.5vw;padding:.2vh 0;border-radius:6px;border:1px solid transparent}
.st-lbl{color:var(--text);font-size:1.45vh;white-space:nowrap}.st-lbl i{font-style:normal;color:var(--green);font-size:1.1vh;margin-left:.25vw}
.st-val{display:inline-flex;align-items:center;justify-content:space-between;gap:.4vw;flex:none;font-size:1.5vh;padding:.3vh .45vw;font-variant-numeric:tabular-nums}.st-val>span{min-width:5.2em;text-align:center}
.st-val i{font-style:normal;color:var(--muted);font-size:1.2vh}.st-val b{font-weight:600}.st-val small{color:var(--muted);font-size:1.15vh;margin-left:.25vw}.st-val.on b{color:var(--orange)}.st-val.on{border-color:#6b4a26}.st-val:focus{border-color:var(--blue);background:#24304a}
.st-warn{color:var(--orange);font-size:1.15vh;white-space:nowrap;margin-left:.5vw;font-weight:400}.st-val.miss{border-color:#6b4a26}
#tgt .wn-box p.tgt-txt{width:auto;min-width:0;white-space:pre-line}.fan-miss{color:var(--orange)}
.chev{width:1em;height:1em;margin-left:.35vw;flex:none}
.ov-grid{display:grid;grid-template-columns:auto auto;gap:.45vh .6vw;align-items:center}.ov-sec{color:var(--muted);font-size:1.25vh;white-space:nowrap}
.ov-chips{display:flex;flex-wrap:wrap;gap:.3vw}.ov-items{flex-direction:column;flex-wrap:nowrap;gap:.45vh}.ov-items>div{display:flex;gap:.3vw}
.ov-chip{font-size:1.35vh;padding:.3vh .5vw;white-space:nowrap;border-radius:6px;display:inline-flex;align-items:center}.ov-chip.on{color:var(--green);border-color:#2f5d3c}.ov-chip:focus{border-color:var(--blue)}
.ov-chip .ico{width:1.2em;height:1.2em}.ov-sz{height:3.1vh;min-width:2.6vw;justify-content:center;align-items:center;padding:0;line-height:1}
#ovs .ov-on{font-size:1.4vh;padding:.3vh .5vw;margin:0 0 .7vh;width:auto}#ovs .ov-on b{font-size:1.3vh;min-width:2.6vw}
.ov-pv{margin-top:.7vh;padding:.9vh .6vw;border-radius:6px;background:linear-gradient(160deg,#f0c060 0%,#d9703a 30%,#5a8fd0 62%,#1d3350 100%)}
.ov-sc{position:relative;overflow:hidden}.ov-box{position:absolute;left:0;top:0;transform-origin:0 0;transform:scale(.5);border-radius:12px;padding:.8vh 1vw;font-weight:700;font-family:ui-monospace,Menlo,monospace;line-height:1.35;white-space:pre;color:#fff}
.ov-box .g{color:#a3aab6}.ov-box .ok{color:#7be495}.ov-box .w{color:#f2bd55}.ov-box .h{color:#f16b68}.ov-box .b{color:#72aaff}
.tgt-n{display:flex;align-items:center;gap:.3vw;font-size:1.3vh;color:var(--muted);margin-bottom:.4vh}.tgt-n span{margin-right:.3vw}.tgt-n .ov-chip{min-width:2.2vw;justify-content:center}
.tgt-err{color:var(--red)!important;margin:.6vh 0 0!important}.tgt-err:empty{display:none}
.wn-btns{display:flex;align-items:center;justify-content:space-between;gap:1.2vw;margin-top:.9vh}.wn-hint{color:var(--muted);font-size:1.35vh}
.dn-qr{width:36vh;height:36vh;flex:none;background:#fff;border-radius:1vh}.dn-qr svg{width:100%;height:100%;display:block;shape-rendering:crispEdges}
.dn-txt h2{margin:0 0 1.4vh;font-size:3.2vh}.dn-txt p{margin:0 0 1.6vh;color:var(--muted);font-size:2vh;line-height:1.4;max-width:44vw}
.dn-url{font-size:2.8vh;font-weight:600;margin-bottom:2.4vh}.dn-url a{color:var(--text)}
html.narrow,html.narrow body{height:auto}
html.narrow body{overflow:auto;font-size:14px}
html.narrow main{height:auto;padding:16px 18px;gap:12px}
html.narrow h1{font-size:24px}
html.narrow .device,html.narrow .status,html.narrow .btn{font-size:13px}
html.narrow .metric .label,html.narrow .metric .hint,html.narrow .metric-sub,html.narrow .note,html.narrow .chart-legend,html.narrow .threshold span,html.narrow .zone-line,html.narrow .version-line{font-size:12px}
html.narrow .metric .big{font-size:34px}
html.narrow #brightness{font-size:44px}html.narrow .metric.xl .big{font-size:44px}html.narrow .metric.xl .metric-sub{font-size:13px}html.narrow #brightness.dense{font-size:34px}
html.narrow .metric .value{font-size:20px}
html.narrow .fan-value{font-size:30px}
html.narrow .laser-chip{font-size:13px}
html.narrow .laser-chip small{font-size:10px}
html.narrow .chart-title{font-size:14px}
html.narrow .threshold b{font-size:18px}
html.narrow #output{font-size:18px}
html.narrow #output .unit{font-size:12px}
html.narrow .kpis{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
html.narrow .infos{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}
html.narrow .charts,html.narrow .prot{grid-template-columns:1fr}
html.narrow .chart canvas{flex:none;height:260px}
.brand .ver{color:var(--muted);font-weight:500;margin-left:.3vw}
.btn.upd{white-space:nowrap;position:relative}.btn.upd .ui{color:var(--blue)}.btn.upd.new{border-color:#2f5d3c;color:var(--green)}.btn.upd.new .ui{color:var(--green)}.btn.upd:focus{border-color:var(--blue)}
.btn.upd .nb{display:inline-block;width:.9vh;height:.9vh;border-radius:50%;background:var(--green);position:absolute;top:.5vh;right:.45vw;visibility:hidden}.btn.upd.new .nb{visibility:visible}
#upd{position:fixed;top:0;right:0;bottom:0;left:0;z-index:20;visibility:hidden;background:transparent;transition:background .18s,visibility 0s .18s}#upd.open{visibility:visible;background:#0000005c;transition:background .18s}
#upd .wn-box{right:auto;padding-top:2vh;transform-origin:var(--ox,10%) 0}#upd.open .wn-box{opacity:1;transform:none}#upd .wn-box::before{right:auto;left:var(--cx,40px)}
.up-wrap{display:flex;gap:1.3vw;align-items:flex-start}
.up-qr{width:17vh;height:17vh;flex:none;background:#fff;border-radius:.8vh}.up-qr svg{width:100%;height:100%;display:block;shape-rendering:crispEdges}
.up-txt{display:flex;flex-direction:column;gap:.8vh;min-width:22vw}
.up-cur{font-size:1.65vh}.up-cur b{font-size:2.1vh}
.up-st{font-size:1.65vh;color:var(--muted)}.up-st.ok,.up-st.new{color:var(--green)}.up-st.new{font-weight:600}.up-st.err{color:var(--red)}.up-st small{display:block;color:var(--muted);font-weight:400;font-size:1.4vh}
.up-btns{display:flex;gap:.6vw;flex-wrap:wrap}.up-btns .btn{font-size:1.6vh;padding:.55vh .8vw}.up-btns a.btn{text-decoration:none}
.btn.go{border-color:#2f5d3c;color:var(--green)}.btn.go:focus{border-color:var(--blue)}
.up-new{padding:.9vh 1vw;border:1px solid #2f5d3c;border-radius:8px;background:#16201a}.up-new h4{margin:0 0 .5vh;font-size:1.45vh;color:var(--green);font-weight:600}.up-new ul{margin:0;padding-left:1.1vw;font-size:1.45vh;line-height:1.4;color:var(--text)}.up-new li{margin:.2vh 0}.up-new .more{color:var(--muted);font-size:1.3vh;margin-top:.3vh}
.up-note{color:var(--muted);font-size:1.35vh;line-height:1.35}.up-url{font-size:1.35vh;color:var(--muted)}
</style>
</head>
<body><main>
<div class="head"><div class="ttl"><div class="ttl-top"><span class="brand"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="11" width="5" height="9" rx="1" fill="#F07070"/><rect x="9.5" y="4" width="5" height="16" rx="1" fill="#5DD879"/><rect x="16" y="13.5" width="5" height="6.5" rx="1" fill="#6AA8FF"/><rect x="2" y="21.2" width="20" height="1.4" rx=".7" fill="#8A93A3"/></svg><b>JMGO Temp</b><span class="ver" id="hdrVer"></span></span><button id="updBtn" class="btn upd" onclick="openUpd()"><span class="ui"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.34-5.66" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><path d="M20.5 3.5v5h-5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span data-i="updBtn"></span><span class="nb"></span></button></div><h1 id="name">JMGO</h1><span class="device" id="device"></span></div>
<div class="hdr-right">
<button id="ovBtn" class="btn app-only" onclick="openOvs()"></button>
<button id="langBtn" class="btn" onclick="toggleLang()">RU</button>
<button id="warnBtn" class="btn app-only" onclick="openWarn()"><span class="wi"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 22 20.5H2z" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/><path d="M12 9.5v5" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><circle cx="12" cy="17.6" r="1.4" fill="currentColor"/></svg></span><span data-i="warnBtn"></span></button>
<button id="donBtn" class="btn don" onclick="openDonate()"><span class="hrt"><svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-7.5-4.6-9.6-9.3C.9 8.3 3 4.5 6.7 4.5c2.2 0 3.6 1.3 5.3 3.3 1.7-2 3.1-3.3 5.3-3.3 3.7 0 5.8 3.8 4.3 7.2C19.5 16.4 12 21 12 21z" fill="currentColor"/></svg></span><span data-i="donBtn"></span></button>
<div class="status" id="status"></div></div></div>
<div id="error"></div><div id="ovWarn"></div><div id="alert"></div><div id="popDiag"></div>
<section class="kpis">
  <div class="metric xl"><div class="label" data-i="cpuT"></div><div class="cpu-pair"><div class="big" id="temp">—</div><span class="sep" id="loadSep">|</span><div class="big" id="load">—</div></div><div class="metric-sub" id="cpuSub">—</div></div>
  <div class="metric xl"><div class="dmd-grid"><div class="label" data-i="dmdT"></div><div class="label lens-x" data-i="lensT"></div><div class="big" id="dmd">—</div><div class="big lens-x" id="lensVal">—</div></div><div class="metric-sub" id="dmdSub">—</div></div>
  <div class="metric"><div class="lhead"><div class="label" data-i="laserT"></div><button id="tgtBtn" class="btn tgt" style="display:none" onclick="openTgt()"></button></div><div class="big" id="laserMax">—</div><div class="laser-row" id="laserRow"></div><div class="metric-sub" id="laserSub">—</div></div>
  <div class="metric"><div class="label" data-i="fanT"></div><div class="fan-pair"><div class="fan-item"><div class="fan-value" id="fan1">—</div><div class="fan-name" id="fan1n"></div></div><div class="fan-item"><div class="fan-value" id="fan2">—</div><div class="fan-name" id="fan2n"></div></div></div><div class="metric-sub" id="fanSub">—</div></div>
  <div class="metric"><div class="label" data-i="brT"></div><div class="big" id="brightness">—</div><div class="metric-sub" id="brSub">—</div></div>
</section>
<section class="charts">
  <div class="chart"><div class="chart-title"><span data-i="ch1"></span> <span class="chart-sub" data-i="ch1s"></span></div><canvas id="thermalChart"></canvas><div class="chart-legend" id="thermalLegend"></div></div>
  <div class="chart"><div class="chart-title"><span data-i="ch2"></span> <span class="chart-sub" data-i="ch2s"></span></div><canvas id="combinedChart"></canvas><div class="chart-legend" id="combinedLegend"></div></div>
</section>
<section class="infos">
  <div class="metric"><div class="label" data-i="freqT"></div><div class="value" id="freq">—</div><div class="hint" id="freqHint">—</div></div>
  <div class="metric"><div class="label" data-i="memT"></div><div class="value" id="memory">—</div><div class="hint" id="memoryHint">—</div></div>
  <div class="metric"><div class="label" data-i="outT"></div><div class="value" id="output">—</div><div class="hint" id="outputHint">—</div></div>
  <div class="metric"><div class="label" data-i="stT"></div><div class="value" id="storage">—</div><div class="hint" id="storageHint"></div></div>
  <div class="metric"><div class="label" data-i="maxT"></div><div class="zone-line"><span>DMD</span><b id="maxDmd">—</b></div><div class="zone-line"><span data-i="lasers"></span><b id="maxLaser">—</b></div><div class="zone-line"><span data-i="cpu"></span><b id="maxCpu">—</b></div><div class="zone-line"><span data-i="throttling"></span><b id="maxThr">—</b></div></div>
  <div class="metric"><div class="label" data-i="verT"></div><div class="version-line"><span id="appLabel"></span><b id="appVersion">—</b></div><div class="version-line"><span data-i="fw"></span><b id="firmwareVersion">—</b></div><div class="version-line"><span>DLP</span><b id="dlpVersion">—</b></div><div class="version-line"><span data-i="model"></span><b id="modelCode">—</b></div></div>
  <div class="metric ultra-extra" id="extraZoneCard"><div class="label" data-i="extraT"></div><div class="zone-line"><span>GPU</span><b id="gpuTemp">—</b></div><div class="zone-line"><span>APU</span><b id="apuTemp">—</b></div></div>
</section>
<section class="prot"><div class="note" id="protNote"></div></section>
</main>
<div id="warn" onclick="if(event.target===this)closeWarn()"><div class="wn-box"><p data-i="wnText"></p><div id="wnList"></div>
<div class="wn-btns"><button class="btn" id="wnTest" onclick="Android.testWarn()" data-i="wnTest"></button><span class="wn-hint" data-i="wnBack"></span></div></div></div>
<div id="tgt" onclick="if(event.target===this)closeTgt()"><div class="wn-box"><p class="tgt-txt"><span data-i="tgtText"></span><br><span data-i="tgtText2"></span></p><div id="tgtList"></div><p class="tgt-err" id="tgtErr"></p>
<div class="wn-btns"><span class="tgt-reset" id="tgtReset"></span><span class="wn-hint" data-i="wnBack"></span></div></div></div>
<div id="ovs" onclick="if(event.target===this)closeOvs()"><div class="wn-box"><div id="ovsOn"></div>
<div class="ov-grid"><span class="ov-sec" data-i="ovsData"></span><div class="ov-chips ov-items" id="ovItems"></div><span class="ov-sec" data-i="ovsSize"></span><div class="ov-chips" id="ovSize"></div>
<span class="ov-sec" data-i="ovsBg"></span><div class="ov-chips" id="ovBg"></div><span class="ov-sec" data-i="corner"></span><div class="ov-chips" id="ovCorner"></div></div>
<div class="ov-pv"><div class="ov-sc" id="ovSc"><div class="ov-box" id="ovBox"></div></div></div><div class="wn-btns"><span class="wn-hint" data-i="wnBack"></span></div></div></div>
<div id="upd" onclick="if(event.target===this)closeUpd()"><div class="wn-box" id="updBox"><div class="up-wrap"><div class="up-qr"><svg viewBox="0 0 41 41"><path fill="#fff" d="M0 0h41v41h-41z"/><path stroke="#000" d="M4 4.5h7m1 0h3m5 0h3m2 0h2m3 0h7m-33 1h1m5 0h1m1 0h1m2 0h1m4 0h1m3 0h1m5 0h1m5 0h1m-33 1h1m1 0h3m1 0h1m4 0h2m1 0h1m1 0h1m3 0h2m4 0h1m1 0h3m1 0h1m-33 1h1m1 0h3m1 0h1m2 0h2m4 0h1m1 0h1m1 0h4m3 0h1m1 0h3m1 0h1m-33 1h1m1 0h3m1 0h1m2 0h6m1 0h1m2 0h3m1 0h1m2 0h1m1 0h3m1 0h1m-33 1h1m5 0h1m2 0h1m1 0h3m3 0h1m2 0h2m4 0h1m5 0h1m-33 1h7m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h7m-23 1h1m1 0h1m1 0h1m4 0h1m-19 1h1m4 0h5m2 0h3m1 0h1m5 0h1m1 0h1m5 0h2m-31 1h1m1 0h1m2 0h1m3 0h1m1 0h1m2 0h1m1 0h2m1 0h3m4 0h3m-29 1h2m2 0h2m1 0h1m5 0h4m3 0h1m1 0h2m2 0h1m1 0h2m-32 1h5m2 0h2m1 0h1m1 0h1m3 0h1m2 0h1m2 0h1m1 0h8m-32 1h1m2 0h1m1 0h3m1 0h3m3 0h1m1 0h2m5 0h1m1 0h2m4 0h1m-33 1h1m4 0h1m3 0h1m2 0h2m2 0h1m1 0h6m2 0h2m1 0h1m1 0h2m-31 1h1m1 0h3m3 0h1m4 0h4m1 0h1m1 0h1m2 0h2m2 0h3m-32 1h1m1 0h1m1 0h2m1 0h1m1 0h1m2 0h3m1 0h1m1 0h1m1 0h1m1 0h1m2 0h1m1 0h1m1 0h2m1 0h1m-31 1h1m3 0h1m6 0h5m4 0h1m1 0h2m1 0h3m2 0h1m-32 1h3m1 0h1m1 0h1m2 0h2m1 0h1m1 0h1m1 0h1m1 0h2m2 0h1m2 0h1m2 0h2m1 0h1m-32 1h2m2 0h4m2 0h1m1 0h3m1 0h1m1 0h1m2 0h3m2 0h1m1 0h2m1 0h1m-33 1h1m1 0h1m1 0h2m1 0h2m1 0h2m2 0h1m3 0h3m4 0h2m1 0h3m-28 1h5m3 0h1m7 0h1m2 0h1m1 0h1m2 0h3m1 0h1m-32 1h1m1 0h1m6 0h1m1 0h2m3 0h1m1 0h2m2 0h2m3 0h2m1 0h1m-31 1h1m2 0h1m2 0h1m1 0h1m1 0h4m1 0h1m1 0h1m2 0h1m3 0h2m1 0h2m1 0h2m-32 1h1m3 0h1m2 0h2m3 0h2m1 0h1m1 0h1m2 0h1m1 0h1m2 0h2m1 0h1m1 0h1m1 0h1m-33 1h7m3 0h1m1 0h1m1 0h1m1 0h2m1 0h2m2 0h7m-22 1h2m3 0h3m3 0h1m3 0h2m3 0h1m1 0h3m-33 1h7m1 0h6m1 0h1m1 0h1m2 0h5m1 0h1m1 0h1m1 0h2m-32 1h1m5 0h1m2 0h1m1 0h1m1 0h1m2 0h1m1 0h1m4 0h2m3 0h4m-32 1h1m1 0h3m1 0h1m7 0h4m1 0h1m2 0h1m1 0h6m1 0h2m-33 1h1m1 0h3m1 0h1m4 0h1m3 0h7m1 0h3m1 0h3m2 0h1m-33 1h1m1 0h3m1 0h1m3 0h6m2 0h3m2 0h1m1 0h1m1 0h2m2 0h2m-33 1h1m5 0h1m1 0h2m1 0h1m2 0h2m4 0h1m1 0h2m1 0h6m-31 1h7m4 0h1m2 0h1m2 0h3m2 0h4m1 0h1m3 0h1"/></svg></div>
<div class="up-txt"><div class="up-cur" id="upCur"></div><div class="up-st" id="upSt"></div><div class="up-btns" id="upBtns"></div><div class="up-new" id="upNew" style="display:none"></div>
<div class="up-note"><span data-i="upNote1"></span><br><span data-i="upNote2"></span></div></div></div>
<div class="wn-btns"><span class="up-url">github.com/jmgotemp/jmgo-temp/releases</span><span class="wn-hint" data-i="wnBack"></span></div></div></div>
<div id="donate" onclick="if(event.target===this)closeDonate()"><div class="dn-box"><div class="dn-qr"><svg viewBox="0 0 37 37"><path fill="#fff" d="M0 0h37v37h-37z"/><path stroke="#000" d="M4 4.5h7m2 0h1m1 0h1m1 0h1m2 0h1m2 0h1m2 0h7m-29 1h1m5 0h1m3 0h2m1 0h3m1 0h2m1 0h1m1 0h1m5 0h1m-29 1h1m1 0h3m1 0h1m2 0h1m2 0h2m2 0h5m1 0h1m1 0h3m1 0h1m-29 1h1m1 0h3m1 0h1m2 0h1m1 0h1m1 0h3m2 0h1m3 0h1m1 0h3m1 0h1m-29 1h1m1 0h3m1 0h1m3 0h1m2 0h2m1 0h1m1 0h1m3 0h1m1 0h3m1 0h1m-29 1h1m5 0h1m1 0h3m1 0h3m3 0h2m2 0h1m5 0h1m-29 1h7m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h7m-18 1h2m1 0h1m2 0h4m-21 1h1m2 0h1m1 0h2m1 0h1m2 0h2m2 0h2m4 0h1m1 0h1m-23 1h2m4 0h2m1 0h1m1 0h1m1 0h2m4 0h1m1 0h1m2 0h1m2 0h1m-29 1h1m3 0h1m1 0h1m1 0h5m6 0h1m1 0h1m1 0h1m1 0h3m-26 1h2m1 0h1m3 0h1m4 0h2m2 0h2m1 0h1m4 0h2m-28 1h2m3 0h2m3 0h3m3 0h2m1 0h5m1 0h1m1 0h2m-28 1h1m1 0h1m3 0h1m3 0h1m3 0h6m-20 1h1m2 0h1m1 0h1m2 0h2m6 0h2m6 0h4m-29 1h2m2 0h2m3 0h1m1 0h7m6 0h2m1 0h1m-28 1h3m3 0h1m2 0h2m2 0h2m2 0h2m1 0h1m6 0h1m-26 1h2m1 0h1m2 0h1m1 0h2m4 0h2m3 0h3m1 0h1m2 0h1m-29 1h1m1 0h2m2 0h1m1 0h2m1 0h3m2 0h2m3 0h1m1 0h1m3 0h2m-27 1h2m1 0h1m3 0h1m2 0h2m1 0h1m1 0h5m2 0h1m2 0h2m-29 1h1m5 0h3m1 0h1m3 0h1m4 0h6m1 0h1m-19 1h3m2 0h1m2 0h2m1 0h2m3 0h1m1 0h3m-29 1h7m5 0h3m1 0h1m3 0h1m1 0h1m1 0h1m2 0h1m-28 1h1m5 0h1m1 0h1m4 0h1m1 0h1m3 0h2m3 0h3m-27 1h1m1 0h3m1 0h1m3 0h2m2 0h1m1 0h2m2 0h5m3 0h1m-29 1h1m1 0h3m1 0h1m1 0h2m1 0h4m4 0h2m1 0h6m-28 1h1m1 0h3m1 0h1m2 0h1m1 0h3m2 0h1m2 0h3m2 0h3m1 0h1m-29 1h1m5 0h1m3 0h1m2 0h1m2 0h5m2 0h2m2 0h1m-28 1h7m1 0h1m1 0h4m1 0h1m1 0h1m2 0h1m3 0h2m1 0h1"/></svg></div><div class="dn-txt"><h2 data-i="dnTitle"></h2><p data-i="dnText"></p><div class="dn-url" id="dnUrl">boosty.to/jmgotemp</div><button class="btn" id="dnClose" onclick="closeDonate()" data-i="dnClose"></button></div></div></div>
<script>
const IS_APP=typeof Android!=='undefined';
if(IS_APP)document.body.classList.add('is-app');
function setNarrow(){document.documentElement.classList.toggle('narrow',!IS_APP&&(innerHeight<700||innerWidth/innerHeight<4/3))}setNarrow();
const I18N={
ru:{
updBtn:'Проверить обновление',upCur:v=>`У вас <b>JMGO Temp ${v}</b>`,upNew:v=>`Вышла версия ${v}`,upOk:'✓ Это последняя версия',upNever:'Еще не проверялось',
upChecked:t=>`проверено ${t}`,upToday:'сегодня в',upChecking:'Проверяю…',upDl:p=>`Скачивание… ${p} %`,upInst:'Подтвердите установку в окне Android.',
upErr_net:'Нет связи с GitHub. Проверьте интернет.',upErr_dl:'Не удалось скачать. Попробуйте еще раз или скачайте по QR.',upErr_inst:'Установка не прошла. Скачайте по QR.',
upErr_cancel:'Установка отменена.',upErr_perm:'Разрешите JMGO Temp установку приложений и нажмите еще раз.',upErr_perm_none:'Прошивка не дает ставить из приложения — скачайте по QR.',upErr_no_apk:'В релизе нет APK — скачайте по QR.',
upCheck:'Проверить',upNewT:v=>`Что нового в ${v}`,upMore:n=>`… и еще ${n} — на GitHub`,upInstall:'Скачать и установить',upOpen:'Открыть страницу',upNote1:'Ставится поверх, настройки сохраняются.',upNote2:'QR — все версии на GitHub.',
 connecting:'подключение…',connected:'подключен',offline:'нет связи',error:'ошибка: ',subtitle:'Мониторинг проектора',
 updated:'обновлено',uptime:(h,m)=>`работает ${h} ч ${m} мин`,
 ovBtn:on=>`Компактный режим: ${on?'вкл':'выкл'}`,corner:'Угол',
 donBtn:'Поддержать',dnTitle:'Поддержать JMGO Temp',dnClose:'Закрыть',
 dnText:'Приложение бесплатное. Если оно вам пригодилось, можно сказать спасибо на Boosty — разовым донатом или подпиской. Наведите камеру телефона на QR-код.',
 ovWarn:'Нет разрешения на окно поверх других приложений. Выдайте его один раз с компьютера:',
 errApp:'Сервис JMGO не ответил. Это приложение работает только на проекторах JMGO.',
 errDash:'Проверь, что ADB включен и проектор в той же Wi‑Fi сети. Если на компьютере включен VPN — он может перехватывать локальную сеть.',
 cpuT:'Процессор: °C | загрузка',dmdT:'Матрица DMD, °C',laserT:'Лазеры, °C',fanT:'Вентиляторы, об/мин',brT:'Яркость (мощность лазера)',
 load:'загрузка',mhz:'МГц',hz:'Гц',gb:'ГБ',
 cpuThrAt:t=>`сброс частоты с ${t}&nbsp;°C`,
 thrNow:(since,f)=>`⚠ троттлинг с ${since}: потолок ${f} МГц`,
 toOff:(m,l)=>`DMD: до отключения ${m} °C<br>порог отключения ${l} °C`,lensT:'Объектив, °C',toDim:(m,l)=>`до снижения яркости ${m} °C (порог ${l})`,
 stale:s=>`нет свежих данных (${s} с)`,waiting:'жду данные…',module:'модуль',
 fanN:n=>`вент. ${n}`,fanSub:(p,t)=>`работают на ${p}&nbsp;% мощности${t!=null?`<br>автоматика держит лазеры около ${t}&nbsp;°C`:''}`,fanNo:'мощность — нет данных',
 brMax:'Макс.',iris:'диафрагма',current:'ток лазеров',curNo:'ток лазеров недоступен',alNow:n=>`AL 2.0: сейчас ≈${n}/10 по току`,alOn:'AL 2.0 включен',
 e_no_service:'Сервис jmgomiddle-01 не найден — это не проектор JMGO?',e_no_dlp:'DLP-сервис JMGO недоступен.',e_service:'Нет связи с сервисом JMGO.',
 e_connecting:'Подключение к проектору…',e_no_adb:'Не найден adb. Положите jmgo_dash.py в папку platform-tools или установите adb.',
 e_connect:t=>`Не удалось подключиться к ${t}. Проверьте IP:порт в настройках беспроводной отладки.`,
 e_not_found:'Проектор не найден. Включите беспроводную отладку ADB на проекторе и подключите компьютер к той же сети (или запустите скрипт с адресом: jmgo_dash.py 192.168.x.x:порт).',
 e_asleep:'Проектор выключен с пульта (спящий режим). Опрос на паузе — данные появятся после включения.',asleepSt:'спящий режим',
 e_timeout:'Проектор не отвечает по ADB (переподключаюсь…). Если не проходит — выключите и включите беспроводную отладку на проекторе.',e_adb:'Ошибка ADB',
 lasersLim:['лазеры: −ярк. / откл.',(d,o)=>`${d}° / ${o}°`],
 bwWhite:'белый экран',
 modes:{14:'авто (по камере)',15:'Overlap',16:'производительность',20:'автотрапеция',21:'супер-контраст',22:'белый экран: ток повышен',23:'производительность · белый экран'},
 ch1:'Матрица и лазеры',ch1s:'· час · пунктир — пороги',ch2:'Процессор и вентиляторы',ch2s:'· об/мин справа · красное — троттлинг',
 collecting:'Собираю историю…',noTemp:'Нет данных о температуре',
 lOff:v=>`лазеры: откл. ${v}°`,lDim:v=>`лазеры: −яркость ${v}°`,dOff:v=>`DMD: откл. ${v}°`,fTgt:v=>`цель вент. ${v}°`,
 laserModule:n=>`лазерный модуль ${n}`,cpuC:'процессор, °C',loadP:'загрузка, %',freqP:'частота, % от макс.',
 freqT:'Частота процессора',freqHint:(mx,ld)=>`макс. ${mx} МГц${ld!=null?` · загрузка ${ld}%`:''}`,freqFixed:'падает только при перегреве',thrCap:f=>`⚠ троттлинг, потолок ${f} МГц`,
 memT:'Память',memHint:(u,t)=>`${u} из ${t} МБ`,usedP:'% занято',stHint:(u,t)=>`${u} из ${t} ГБ`,outT:'Вывод изображения',hdmi:'HDMI-сигнал',
 stT:'Накопитель',free:'свободно',freeOf:t=>`свободно из ${t} ГБ`,maxT:'Максимум за сеанс',lasers:'Лазеры',cpu:'Процессор',throttling:'Троттлинг',no:'нет',was:t=>`был, ${t}`,
 verT:'Версии',app:'JMGO Temp',dash:'Дашборд',fw:'Прошивка',model:'Модель',extraT:'Доп. датчики',
 pDmd:'DMD — отключение проектора',pDim:'Лазеры — снижение яркости',pOff:'Лазеры — отключение',
 pFan:hi=>`Вентиляторы держат лазеры около этой температуры${hi!=null?` (в ярких режимах — до ${hi} °C)`:''}`,
 warnBtn:'Предупреждения',wnTitle:'Предупреждения поверх фильма',
 wnText:'После выхода из приложения оно раз в 5 с проверяет температуры и при перегреве на 20 с показывает сообщение слева вверху. Если выключить все — после выхода ничего не делает.',
 wnTest:'Пример предупреждения',wnBack:'«Назад» на пульте — закрыть',
 tgtBtn:t=>t?`Изменено: ${t} ▾`:'Изменить °C ▾',tgtText:'Температура, которую держат вентиляторы.',tgtAutoTxt:'Дефолт — как в прошивке.',tgtText2:'Ниже — холоднее и громче,\nвыше — тише и горячее.',tgtReset:'Сбросится после перезагрузки проектора.',tgtKeep:'Сохранится после перезагрузки проектора.',
 tgtTiers:'Строк яркости',tgtHeadB:'Яркость',tgtHeadL:(a,b)=>`Лазеры, ${a}–${b}&nbsp;°C`,tgtMiss:'не достигается',fanMiss:t=>`цель ${t}&nbsp;°C не достигается`,
 ovsShow:'Компактный режим',ovsText:'До 3 строк. Что показывать:',ovsData:'Данные',ovsSize:'Размер',ovsBg:'Фон, %',ovNoBg:'нет',
 ovItems:{dmd:'DMD',las:'Лазеры',cpu:'CPU °C',load:'Загрузка CPU',freq:'Частота CPU',fan:'Вентиляторы',br:'Яркость',hdmi:'HDMI'},tgtBoot:'Сохранять после перезагрузки',
 tgtAuto:'дефолт',popDiag:'⚠ Окно не показалось. Пришлите, пожалуйста, фото этой строки:',tgtFail:'Не удалось:',
 wnItems:[['all','Все предупреждения',''],
  ['w_las','Лазеры','за 1,5 °C до снижения яркости'],
  ['w_dmd','Матрица DMD','за 3 °C до отключения'],
  ['w_cpu','Процессор — перегрев','от 100 °C'],
  ['w_thr','Процессор — троттлинг','прошивка снизила частоту']],
 wnOn:'вкл',wnOff:'выкл',wnPart:'частично',
nFw:c=>`Пороги — из прошивки этого проектора${c?` (${c})`:''}.`,noLim:'в прошивке нет порога для этого датчика',noDmd:'эта модель не отдает температуру DMD',nDef:'⚠ Пороги не нашлись в прошивке — показаны значения N5 Pro.',
 nDlp:'DMD меряет датчик в самой матрице (через DLP), поэтому порог выше.',nNtc:'Датчик DMD стоит рядом с матрицей, зеркала горячее.',
 nPhoto:'Для контроля охлаждения сравнивайте снимок этого экрана после 30–60 мин фильма раз в несколько месяцев: +5 °C или заметно выше обороты — пора обслуживать.',
 aStop:(n,p)=>`Вентилятор ${n}: 0 об/мин${p!=null?` при мощности ${p} %`:''} — похоже, не крутится.`,
 aDmd:(v,l)=>`DMD близко к порогу отключения: ${v} из ${l} °C.`,aLas:(v,l)=>`Лазеры близко к порогу снижения яркости: ${v} из ${l} °C.`,
 aFans:(a,b)=>`Вентиляторы крутятся заметно по-разному (${a} и ${b} об/мин).`,aNoTh:'Пока нет данных о температуре DMD/лазеров — подождите 10–20 с.',
 aThr:(t,f)=>`Процессор троттлит с ${t}: частота ограничена до ${f} МГц.`
},
en:{
updBtn:'Check for updates',upCur:v=>`You have <b>JMGO Temp ${v}</b>`,upNew:v=>`Version ${v} is out`,upOk:'✓ This is the latest version',upNever:'Not checked yet',
upChecked:t=>`checked ${t}`,upToday:'today at',upChecking:'Checking…',upDl:p=>`Downloading… ${p} %`,upInst:'Confirm the install in the Android window.',
upErr_net:'No connection to GitHub. Check the internet.',upErr_dl:'Download failed. Try again or use the QR.',upErr_inst:'Install failed. Download via the QR.',
upErr_cancel:'Install cancelled.',upErr_perm:'Allow JMGO Temp to install apps, then press again.',upErr_perm_none:'The firmware blocks installs from apps — use the QR.',upErr_no_apk:'No APK in the release — use the QR.',
upCheck:'Check',upNewT:v=>`What's new in ${v}`,upMore:n=>`… and ${n} more — on GitHub`,upInstall:'Download and install',upOpen:'Open the page',upNote1:'Installs over the old one, settings are kept.',upNote2:'QR — all versions on GitHub.',
 connecting:'connecting…',connected:'connected',offline:'offline',error:'error: ',subtitle:'Projector monitor',
 updated:'updated',uptime:(h,m)=>`uptime ${h} h ${m} min`,
 ovBtn:on=>`Compact mode: ${on?'on':'off'}`,corner:'Corner',
 donBtn:'Support',dnTitle:'Support JMGO Temp',dnClose:'Close',
 dnText:'The app is free. If you find it useful, you can say thanks on Boosty with a one-time tip or a subscription. Point your phone camera at the QR code.',
 ovWarn:'No permission to draw over other apps. Grant it once from a computer:',
 errApp:'The JMGO service did not respond. This app works only on JMGO projectors.',
 errDash:'Check that ADB is enabled and the projector is on the same Wi‑Fi network. A VPN on the computer may intercept local traffic.',
 cpuT:'CPU: °C | load',dmdT:'DMD chip, °C',laserT:'Lasers, °C',fanT:'Fans, rpm',brT:'Brightness (laser power)',
 load:'load',mhz:'MHz',hz:'Hz',gb:'GB',
 cpuThrAt:t=>`throttles from ${t}&nbsp;°C`,
 thrNow:(since,f)=>`⚠ throttling since ${since}: capped at ${f} MHz`,
 toOff:(m,l)=>`DMD: ${m} °C to shutdown<br>shutdown limit ${l} °C`,lensT:'Lens, °C',toDim:(m,l)=>`${m} °C to dimming (limit ${l})`,
 stale:s=>`no fresh data (${s} s)`,waiting:'waiting for data…',module:'module',
 fanN:n=>`fan ${n}`,fanSub:(p,t)=>`running at ${p}% power${t!=null?`<br>auto control keeps lasers near ${t}&nbsp;°C`:''}`,fanNo:'power — no data',
 brMax:'Max',iris:'aperture',current:'laser current',curNo:'laser current unavailable',alNow:n=>`AL 2.0: now ≈${n}/10 by current`,alOn:'AL 2.0 on',
 e_no_service:'Service jmgomiddle-01 not found — is this a JMGO projector?',e_no_dlp:'JMGO DLP service is unavailable.',e_service:'No connection to the JMGO service.',
 e_connecting:'Connecting to the projector…',e_no_adb:'adb not found. Put jmgo_dash.py into the platform-tools folder or install adb.',
 e_connect:t=>`Could not connect to ${t}. Check IP:port in the wireless debugging settings.`,
 e_not_found:'Projector not found. Enable ADB wireless debugging on the projector and connect the computer to the same network (or start the script with an address: jmgo_dash.py 192.168.x.x:port).',
 e_asleep:'The projector is off (standby). Polling is paused — data will return once it is turned on.',asleepSt:'standby',
 e_timeout:'The projector does not respond over ADB (reconnecting…). If it persists, turn wireless debugging off and on again on the projector.',e_adb:'ADB error',
 lasersLim:['lasers: dim / off',(d,o)=>`${d}° / ${o}°`],
 bwWhite:'white screen',
 modes:{14:'auto (camera)',15:'Overlap',16:'performance',20:'auto keystone',21:'super contrast',22:'white screen: boosted current',23:'performance · white screen'},
 ch1:'DMD and lasers',ch1s:'· hour · dashed = limits',ch2:'CPU and fans',ch2s:'· rpm on the right · red = throttling',
 collecting:'Collecting history…',noTemp:'No temperature data',
 lOff:v=>`lasers: off ${v}°`,lDim:v=>`lasers: dim ${v}°`,dOff:v=>`DMD: off ${v}°`,fTgt:v=>`fan target ${v}°`,
 laserModule:n=>`laser module ${n}`,cpuC:'CPU, °C',loadP:'load, %',freqP:'frequency, % of max',
 freqT:'CPU frequency',freqHint:(mx,ld)=>`max ${mx} MHz${ld!=null?` · load ${ld}%`:''}`,freqFixed:'drops only when overheating',thrCap:f=>`⚠ throttling, capped at ${f} MHz`,
 memT:'Memory',memHint:(u,t)=>`${u} of ${t} MB`,usedP:'% used',stHint:(u,t)=>`${u} of ${t} GB`,outT:'Video output',hdmi:'HDMI signal',
 stT:'Storage',free:'free',freeOf:t=>`free of ${t} GB`,maxT:'Session max',lasers:'Lasers',cpu:'CPU',throttling:'Throttling',no:'no',was:t=>`yes, ${t}`,
 verT:'Versions',app:'JMGO Temp',dash:'Dashboard',fw:'Firmware',model:'Model',extraT:'Extra sensors',
 pDmd:'DMD — projector shutdown',pDim:'Lasers — brightness reduction',pOff:'Lasers — shutdown',
 pFan:hi=>`Fans keep the lasers near this temperature${hi!=null?` (up to ${hi} °C in bright modes)`:''}`,
 warnBtn:'Warnings',wnTitle:'Warnings over the movie',
 wnText:'After you leave the app it checks temperatures every 5 s and, if something overheats, shows a message at the top left for 20 s. With everything off, it does nothing after you leave.',
 wnTest:'Example warning',wnBack:'Back on the remote — close',
 tgtBtn:t=>t?`Changed: ${t} ▾`:'Change °C ▾',tgtText:'Temperature the fans hold.',tgtAutoTxt:'Default is the firmware setting.',tgtText2:'Lower is cooler and louder,\nhigher is quieter and hotter.',tgtReset:'Resets after the projector reboots.',tgtKeep:'Kept after the projector reboots.',
 tgtTiers:'Brightness rows',tgtHeadB:'Brightness',tgtHeadL:(a,b)=>`Lasers, ${a}–${b}&nbsp;°C`,tgtMiss:'not reached',fanMiss:t=>`target ${t}&nbsp;°C not reached`,
 ovsShow:'Compact mode',ovsText:'Up to 3 lines. Show:',ovsData:'Data',ovsSize:'Size',ovsBg:'Background, %',ovNoBg:'none',
 ovItems:{dmd:'DMD',las:'Lasers',cpu:'CPU °C',load:'CPU load',freq:'CPU clock',fan:'Fans',br:'Brightness',hdmi:'HDMI'},tgtBoot:'Keep after reboot',
 tgtAuto:'default',popDiag:'⚠ The window did not appear. Please send a photo of this line:',tgtFail:'Failed:',
 wnItems:[['all','All warnings',''],
  ['w_las','Lasers','1.5 °C before dimming'],
  ['w_dmd','DMD chip','3 °C before shutdown'],
  ['w_cpu','CPU overheating','from 100 °C'],
  ['w_thr','CPU throttling','firmware lowered the frequency']],
 wnOn:'on',wnOff:'off',wnPart:'partly',
nFw:c=>`Limits are read from this projector's firmware${c?` (${c})`:''}.`,noLim:'the firmware sets no limit for this sensor',noDmd:'this model does not report the DMD temperature',nDef:'⚠ Limits not found in the firmware — N5 Pro values are shown.',
 nDlp:'DMD is measured inside the chip (via DLP), hence the higher limit.',nNtc:'The DMD sensor sits next to the chip; the mirrors run hotter.',
 nPhoto:'To track cooling, compare a photo of this screen after 30–60 min of a movie every few months: +5 °C or clearly higher fan speeds means it\'s time for servicing.',
 aStop:(n,p)=>`Fan ${n}: 0 rpm${p!=null?` at ${p}% power`:''} — it seems to be stopped.`,
 aDmd:(v,l)=>`DMD is close to the shutdown limit: ${v} of ${l} °C.`,aLas:(v,l)=>`Lasers are close to the dimming limit: ${v} of ${l} °C.`,
 aFans:(a,b)=>`Fans run at noticeably different speeds (${a} and ${b} rpm).`,aNoTh:'No DMD/laser temperature yet — wait 10–20 s.',
 aThr:(t,f)=>`CPU throttling since ${t}: frequency capped at ${f} MHz.`
}};
const $=id=>document.getElementById(id);
function loadLang(){let l=null;try{l=IS_APP?Android.getLang():localStorage.getItem('jmgo_lang')}catch(e){}
 if(l!=='ru'&&l!=='en')l=(navigator.language||'ru').toLowerCase().startsWith('ru')?'ru':'en';return l}
let LANG=loadLang();
const T=(k,...a)=>{const v=I18N[LANG][k]??I18N.ru[k];return typeof v==='function'?v(...a):v};
let last=null;
function applyStatic(){document.documentElement.lang=LANG;document.querySelectorAll('[data-i]').forEach(e=>e.textContent=T(e.dataset.i));
 $('langBtn').textContent=LANG==='ru'?'RU':'EN';$('appLabel').textContent=T(IS_APP?'app':'dash');$('fan1n').textContent=T('fanN',1);$('fan2n').textContent=T('fanN',2);
 if(!last){$('device').textContent=T('subtitle');$('status').innerHTML='<i class="dot"></i>'+T('connecting')}}
function toggleLang(){LANG=LANG==='ru'?'en':'ru';try{IS_APP?Android.setLang(LANG):localStorage.setItem('jmgo_lang',LANG)}catch(e){}applyStatic();if(last)render(last);updOverlayBtn();renderUpd()}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function set(id,v){$(id).innerHTML=v}
function openDonate(){popOpened();$('donate').classList.add('open');popFocus(()=>$('dnClose'))}
function closeDonate(){$('donate').classList.remove('open');$('donBtn').focus()}
function renderWarn(){let p={};try{p=JSON.parse(Android.warnPrefs())}catch(e){}const f=document.activeElement&&document.activeElement.dataset.k;
 const tr=(last&&last.latest&&last.latest.trip_passive0)||123;
 $('wnList').innerHTML=T('wnItems').map(([k,t,h])=>[k,t,k==='w_cpu'?h.replace('100',Math.min(100,tr-10)):h]).map(([k,t,h])=>{const st=k==='all'?(WKEYS.every(x=>p[x]!==false)?'wnOn':WKEYS.some(x=>p[x]!==false)?'wnPart':'wnOff'):(p[k]!==false?'wnOn':'wnOff');
   return `<button class="btn wn-row${k==='all'?' all':''}${st==='wnOn'?' on':''}" data-k="${k}" onclick="toggleWarn('${k}')"><b>${T(st)}</b><span>${t}${h?`<small>${h}</small>`:''}</span></button>`}).join('');if(f){const b=$('wnList').querySelector(`[data-k="${f}"]`);b&&b.focus()}}
const WKEYS=['w_las','w_dmd','w_cpu','w_thr'];
// «Все»: если включено все — выключить все, иначе (выкл или частично) — включить все
function toggleWarn(k){let p={};try{p=JSON.parse(Android.warnPrefs())}catch(e){}Android.setWarn(k,k==='all'?!WKEYS.every(x=>p[x]!==false):p[k]===false);renderWarn()}
function openWarn(){renderWarn();const r=$('warnBtn').getBoundingClientRect(),b=document.querySelector('.wn-box'),right=Math.max(8,innerWidth-r.right-40);
 b.style.top=(r.bottom+12)+'px';b.style.right=right+'px';const cx=innerWidth-right-(r.left+r.width/2)-8;b.style.setProperty('--cx',cx+'px');b.style.setProperty('--ox',`calc(100% - ${cx+8}px)`);
 popOpened();$('warnBtn').classList.add('on');$('warn').classList.add('open');popFocus(()=>$('wnList').querySelector('.btn'))}
function closeWarn(){$('warn').classList.remove('open');$('warnBtn').classList.remove('on');$('warnBtn').focus()}
function onBack(){if($('upd').classList.contains('open')){closeUpd();return true}if($('donate').classList.contains('open')){closeDonate();return true}if($('warn').classList.contains('open')){closeWarn();return true}if($('tgt').classList.contains('open')){closeTgt();return true}if($('ovs').classList.contains('open')){closeOvs();return true}return false}
// Цель температуры лазеров для вентиляторов (только приложение, N5/N5S/IRIS): список значений и штатная цель — из прошивки модели
function tgtCtl(){return last&&last.latest&&last.latest.fan_ctl}
// с 1.6.1 цель — любой градус в диапазоне модели: ◀ ▶ меняют на 1 °C, в проектор уходит через 1 с после последнего нажатия
let tgtPend=null,tgtTimer=0;
function tgtView(){const fc=tgtCtl();return fc&&tgtPend?Object.assign({},fc,tgtPend):fc}
function tgtRange(fc){const o=fc.opts||[];return [fc.min||Math.min(fc.auto,...o),fc.max||Math.max(fc.auto,...o)]}
// строки в окне: [цели, подпись]. В прошивку всегда уходят четыре цели (lo 1–3, mid 4–6, hi 7–8, top 9–10),
// в объединенные строки — одно значение; показываем значение старшей цели строки
const TGT_ICO='<svg class="ico tr-ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.34-5.66" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><path d="M20.5 3.5v5h-5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const TGT_KEYS=['lo','mid','hi','top'];
const TGT_GROUPS={1:[['lo,mid,hi,top','1–10']],2:[['lo,mid','1–6'],['hi,top','7–10']],3:[['lo','1–3'],['mid','4–6'],['hi,top','7–10']],
 4:[['lo','1–3'],['mid','4–6'],['hi','7–8'],['top','9–10']]};
function tgtGroups(fc){return TGT_GROUPS[fc.tiers]||TGT_GROUPS[3]}
const tgtVals=v=>Object.fromEntries(TGT_KEYS.map(k=>[k,v[k]||0]));
const tgtTop=g=>g.split(',').pop();
function renderTgt(){const fc=tgtView();if(!fc)return;const a=document.activeElement,fr=a&&a.dataset.r;const [mn,mx]=tgtRange(fc);
 const row=([g,l])=>{const v=fc[tgtTop(g)]||0,cur=g.split(',').includes(fc.use),miss=cur&&v&&fc.unreach&&!tgtPend;
  return `<div class="st-row tgt-row"><span class="st-lbl">${l}</span>`+
  `<button class="btn st-val${v?' on':''}${miss?' miss':''}" data-r="${g}" onclick="clickTgt(event,'${g}')"><i>◀</i><span><b>${v||fc.auto}</b>${v?'&nbsp;°C':`<small>${T('tgtAuto')}</small>`}</span><i>▶</i></button>${miss?`<span class="st-warn">${T('tgtMiss')}</span>`:''}</div>`};
 const n=tgtGroups(fc).length;
 $('tgtList').innerHTML=`<div class="tgt-row tgt-n"><span>${T('tgtTiers')}</span>`+[1,2,3,4].map(k=>`<button class="btn ov-chip${k===n?' on':''}" data-r="n${k}" onclick="pickTiers(${k})">${k}</button>`).join('')+'</div>'+
  `<div class="st-head"><span>${T('tgtHeadB')}</span><span>${T('tgtHeadL',mn,mx)}</span></div>`+tgtGroups(fc).map(row).join('')+
  `<div class="tgt-row"><button class="btn wn-row${fc.boot?' on':''}" data-r="boot" onclick="pickBoot()"><b>${T(fc.boot?'wnOn':'wnOff')}</b><span>${T('tgtBoot')}</span></button></div>`;
 $('tgtReset').innerHTML=TGT_ICO+esc(T(fc.boot?'tgtKeep':'tgtReset'));
 if(fr!=null&&$('tgtList').contains(a)===false){const b=$('tgtList').querySelector(`[data-r="${fr}"]`);b&&b.focus()}}
// список раскрывается от кнопки, стрелка указывает на нее (как «Предупреждения»)
function posTgt(){posPop('tgtBtn','#tgt .wn-box',12)}
function posPop(btn,box,gap){const r=$(btn).getBoundingClientRect(),b=document.querySelector(box),right=Math.max(8,innerWidth-r.right-gap);
 b.style.top=(r.bottom+12)+'px';b.style.right=right+'px';const cx=innerWidth-right-(r.left+r.width/2)-8;b.style.setProperty('--cx',cx+'px');b.style.setProperty('--ox',`calc(100% - ${cx+8}px)`)}
function openTgt(){if(!tgtCtl())return;$('tgtErr').textContent='';renderTgt();posTgt();requestAnimationFrame(posTgt);
 popOpened();$('tgt').classList.add('open');const fc=tgtCtl();popFocus(()=>$('tgtList').querySelector(`.st-val[data-r*="${fc.use}"]`)||$('tgtList').querySelector('.btn'))}
function closeTgt(){sendTgt();$('tgt').classList.remove('open');$('tgtBtn').focus()}
function stepTgt(g,d){const fc=tgtView();if(!fc)return;const [mn,mx]=tgtRange(fc);let v=(fc[tgtTop(g)]||fc.auto)+d;v=Math.max(mn,Math.min(mx,v));
 tgtPend=tgtVals(fc);g.split(',').forEach(r=>tgtPend[r]=v===fc.auto?0:v);
 clearTimeout(tgtTimer);tgtTimer=setTimeout(sendTgt,1000);updTgtBtn(tgtCtl())}
// смена числа ступеней: объединенным ступеням достается значение старшей; если что-то поменялось — запись, как после ◀ ▶
function pickTiers(n){const fc=tgtCtl();if(!fc)return;const v=tgtView();fc.tiers=n;try{Android.setFanTiers(n)}catch(e){}
 const p=tgtVals(v),q=Object.assign({},p);tgtGroups(fc).forEach(([g])=>g.split(',').forEach(r=>q[r]=p[tgtTop(g)]));
 if(TGT_KEYS.some(k=>q[k]!==p[k])){tgtPend=q;clearTimeout(tgtTimer);tgtTimer=setTimeout(sendTgt,1000)}
 updTgtBtn(fc);if(!$('tgt').classList.contains('open'))return;renderTgt();const b=$('tgtList').querySelector(`[data-r="n${n}"]`);b&&b.focus()}
// мышь/тачпад: левая половина — ◀, правая — ▶; OK с пульта (detail 0) значение не меняет
function clickTgt(e,r){if(!e.detail)return;const b=e.currentTarget.getBoundingClientRect();stepTgt(r,e.clientX<b.left+b.width/2?-1:1)}
function sendTgt(){clearTimeout(tgtTimer);if(!tgtPend)return;const p=tgtPend,fc=tgtCtl();let res='';
 try{res=Android.setFanTargets(p.lo,p.mid,p.hi,p.top)}catch(e){res=String(e)}
 tgtPend=null;if(res!=='ok'){$('tgtErr').textContent=T('tgtFail')+' '+res;updTgtBtn(fc);return}$('tgtErr').textContent='';
 if(fc){Object.assign(fc,p);const w=fc[fc.use];if(w>0)fc.custom=w;else delete fc.custom;delete fc.unreach}updTgtBtn(fc)}
function pickBoot(){const fc=tgtCtl();if(!fc)return;fc.boot=!fc.boot;try{Android.setFanBoot(fc.boot)}catch(e){}renderTgt()}
function updTgtBtn(fc0){const b=$('tgtBtn');if(!IS_APP||!fc0){b.style.display='none';return}b.style.display='';const fc=tgtView();
 const vs=tgtGroups(fc).map(([g])=>fc[tgtTop(g)]||0),any=vs.some(v=>v);
 // в кнопке не пишем «дефолт» (3 раза не влезает в плитку): незаданная ступень — «—», подписи в окне выбора
 const same=vs.every(v=>v===vs[0]),list=`${vs.map(v=>v||'—').join(' / ')}&nbsp;°C`;
 // 4 разных значения со словом «Изменено» не влезают рядом с подписью плитки — только цифры (оранжевый цвет и так значит «свое»)
 b.innerHTML=any&&!same&&vs.length>3?list+' ▾':T('tgtBtn',!any?'':same?`${vs[0]}&nbsp;°C`:list);b.classList.toggle('on',any);
 if($('tgt').classList.contains('open')){renderTgt();posTgt()}}
// Окно открыли кнопкой OK — то же нажатие (на новых WebView клик приходит при отпускании) не должно
// сработать на кнопке внутри окна: 0,5 с после открытия клики внутри окон игнорируем
let popUntil=0;function popOpened(){popUntil=Date.now()+500;popCheck()}
// Самопроверка: окно открыли, но его не видно (за краем экрана или нулевого размера) — показываем строку
// с версией WebView, чтобы владелец мог прислать фото (на N3 нет adb). Если все в порядке, ничего не выводится.
function popCheck(){setTimeout(()=>{const o=['warn','tgt','ovs','donate','upd'].map($).find(x=>x.classList.contains('open'));if(!o)return;
 const b=o.querySelector('.wn-box,.dn-box').getBoundingClientRect(),c=o.getBoundingClientRect();
 if(b.width>0&&b.height>0&&b.right>0&&b.bottom>0&&b.left<innerWidth&&b.top<innerHeight){$('popDiag').innerHTML='';return}
 const v=(navigator.userAgent.match(/Chrome\/([\d.]+)/)||[])[1]||'?',sup=window.CSS&&CSS.supports?CSS.supports('inset','0'):'?',r=x=>`${x.left|0},${x.top|0} ${x.width|0}×${x.height|0}`;
 $('popDiag').innerHTML=`<div class="alert">${T('popDiag')} WebView ${esc(v)} · inset ${sup} · ${esc(o.id)} ${r(b)} / ${r(c)} · ${innerWidth}×${innerHeight}</div>`},400)}
document.addEventListener('click',e=>{if(Date.now()<popUntil&&e.target.closest&&e.target.closest('#warn .wn-box,#tgt .wn-box,#ovs .wn-box,#donate .dn-box,#upd .wn-box')){e.preventDefault();e.stopPropagation()}},true);
// фокус в только что открытое окно; если WebView еще не успел показать его — повторяем в следующем кадре
function popFocus(f){const b=f();b&&b.focus();if(!b||document.activeElement!==b)requestAnimationFrame(()=>{const c=f();c&&c.focus()})}
if(!IS_APP)$('dnUrl').innerHTML='<a href="https://boosty.to/jmgotemp/donate" target="_blank" rel="noopener">boosty.to/jmgotemp</a>';
const fmt=v=>v==null?'—':(Math.round(v*10)/10).toFixed(1);
const unit=(v,u)=>v==null||v===''?'—':`${esc(v)}<span class="unit"> ${u}</span>`;
const hm=ts=>new Date(ts*1000).toLocaleTimeString(LANG==='ru'?'ru':'en-GB',{hour:'2-digit',minute:'2-digit'});
// Датчики стоят на лазерных модулях, а не на цветах, поэтому цвета линий нейтральные.
const LCOL=['#e9edf3','#5fd3c6','#b8a1ff','#ff7ac8'];
const FIXED_MODES=[15,16,22,23];
function margCls(m){return m==null?'':m<=2?'hot':m<=6?'warn':'ok'}
function resolutionLabel(w,h){if(w>=7680)return '8K';if(w>=3840||h>=2160)return '4K UHD';if(w>=2560||h>=1440)return '2K QHD';if(h>=1080)return 'Full HD';if(h>=720)return 'HD';return ''}
function shortRes(w,h){if(w>=7680)return '8K';if(w>=3840||h>=2160)return '4K';if(w>=2560||h>=1440)return '2K';if(h>=1080)return '1080p';if(h>=720)return '720p';return h+'p'}
function canvasCtx(id){const c=$(id),r=c.getBoundingClientRect(),dpr=devicePixelRatio||1,W=Math.max(420,Math.floor(r.width*dpr)),H=Math.max(120,Math.floor((r.height||190)*dpr));c.width=W;c.height=H;const x=c.getContext('2d');x.clearRect(0,0,W,H);x.font=`${Math.max(11,Math.round(innerHeight*0.013))*dpr}px -apple-system,Roboto,Arial`;return {x,W,H,dpr}}
function timeAxis(x,p,L,R,W,y,dpr){x.fillStyle='#a3aab6';x.textAlign='left';x.fillText(hm(p[0].ts),L,y+13*dpr);x.textAlign='right';x.fillText(hm(p[p.length-1].ts),W-R,y+13*dpr);x.textAlign='left'}
function drawCombined(points){
  const {x,W,H,dpr}=canvasCtx('combinedChart');
  if(points.length<2){x.fillStyle='#a3aab6';x.fillText(T('collecting'),18*dpr,38*dpr);return}
  const fanPeak=Math.max(0,...points.map(p=>Math.max(p.fan1||0,p.fan2||0)));
  const tMax=125,fMax=Math.max(2500,Math.ceil(fanPeak/500)*500);
  const L=Math.ceil(x.measureText('125').width)+12*dpr,R=Math.ceil(x.measureText(String(fMax)).width)+10*dpr,T0=12*dpr,B=20*dpr,pw=W-L-R,ph=H-T0-B;
  const X=i=>L+i/(points.length-1)*pw;
  x.fillStyle='rgba(241,107,104,.18)';points.forEach((p,i)=>{if(p.thr&&i>0)x.fillRect(X(i-1),T0,X(i)-X(i-1)+1,ph)});
  x.strokeStyle='#303641';x.fillStyle='#a3aab6';x.lineWidth=1*dpr;
  [0,25,50,75,100,125].forEach(v=>{const y=T0+(1-v/tMax)*ph;x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.textAlign='right';x.fillText(v,L-6*dpr,y+4*dpr)});
  for(let f=0;f<=fMax;f+=fMax/5){const y=T0+(1-f/fMax)*ph;x.textAlign='left';x.fillText(Math.round(f),W-R+6*dpr,y+4*dpr)}
  timeAxis(x,points,L,R,W,T0+ph,dpr);
  const line=(key,color,scale,w,dash)=>{x.beginPath();let s=false;points.forEach((p,i)=>{const v=p[key];if(v==null||v<0){s=false;return}const py=T0+(1-v/scale)*ph;if(!s){x.moveTo(X(i),py);s=true}else x.lineTo(X(i),py)});x.save();if(dash)x.setLineDash(dash);x.strokeStyle=color;x.lineWidth=w*dpr;x.stroke();x.restore()};
  const has=k=>points.some(p=>p[k]!=null&&p[k]>=0);
  line('fan1','#72aaff',fMax,1.8);line('fan2','#d49aff',fMax,1.8);
  if(has('load'))line('load','#f2bd55',tMax,1.8);
  line('temp','#52d273',tMax,1.8);
  let lg=`<span><i class="legend-dot" style="background:#52d273"></i>${T('cpuC')}</span>`;
  if(has('load'))lg+=`<span><i class="legend-dot" style="background:#f2bd55"></i>${T('loadP')}</span>`;
  lg+=`<span><i class="legend-dot" style="background:#72aaff"></i>${T('fanN',1)}</span><span><i class="legend-dot" style="background:#d49aff"></i>${T('fanN',2)}</span>`;
  set('combinedLegend',lg);
}
function pidTargets(l){return (l.pid_targets||[]).filter((v,i)=>!(l.pid_fixed||[])[i])}
function drawThermal(points,lim){
  const {x,W,H,dpr}=canvasCtx('thermalChart');
  const hasData=points.some(p=>p.dmd!=null||(p.l&&p.l.length));
  if(points.length<2||!hasData){x.fillStyle='#a3aab6';x.fillText(points.length<2?T('collecting'):T('noTemp'),18*dpr,38*dpr);return}
  let vals=[];points.forEach(p=>{if(p.dmd!=null)vals.push(p.dmd);(p.l||[]).forEach(v=>vals.push(v))});
  const top=Math.max(lim.laser_shutdown||57,lim.dmd_shutdown||55,...vals)+3,bot=Math.max(0,Math.floor((Math.min(...vals)-5)/5)*5);
  const pair=lim.laser_shutdown!=null&&lim.laser_derate!=null&&Math.abs(lim.laser_shutdown-lim.laser_derate)<=1.5;
  const L=Math.ceil(x.measureText(String(Math.floor(top/10)*10)).width)+12*dpr,R=12*dpr;
  const T0=12*dpr,B=20*dpr,pw=W-L-R,ph=H-T0-B;
  const Y=v=>T0+(1-(v-bot)/(top-bot))*ph;
  x.strokeStyle='#303641';x.fillStyle='#a3aab6';x.lineWidth=1*dpr;
  for(let v=Math.ceil(bot/10)*10;v<=top;v+=10){const y=Y(v);x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.textAlign='right';x.fillText(v,L-8*dpr,y+4*dpr)}
  timeAxis(x,points,L,R,W,T0+ph,dpr);
  const lbl=[];
  const ref=(v,color,label)=>{if(v==null)return;const y=Y(v);x.save();x.setLineDash([6*dpr,5*dpr]);x.strokeStyle=color;x.lineWidth=1.5*dpr;x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.restore();if(label)lbl.push({y,color,label})};
  if(pair){const ll=T('lasersLim');ref(lim.laser_derate,'#f16b68',ll[0]+' '+ll[1](lim.laser_derate,lim.laser_shutdown))}
  else{ref(lim.laser_shutdown,'#f16b68',T('lOff',lim.laser_shutdown));ref(lim.laser_derate,'#f16b68',T('lDim',lim.laser_derate))}
  ref(lim.dmd_shutdown,'#f2bd55',T('dOff',lim.dmd_shutdown));
  const line=(get,color,w)=>{x.beginPath();let s=false;points.forEach((p,i)=>{const v=get(p);if(v==null){s=false;return}const px=L+i/(points.length-1)*pw,py=Y(v);if(!s){x.moveTo(px,py);s=true}else x.lineTo(px,py)});x.strokeStyle=color;x.lineWidth=w*dpr;x.stroke()};
  const n=Math.max(0,...points.map(p=>(p.l||[]).length));
  for(let i=0;i<n;i++)line(p=>(p.l||[])[i],LCOL[i],1.8);
  line(p=>p.dmd,'#f2bd55',1.8);
  const fh=parseInt(x.font)||14*dpr,boxes=[];
  lbl.sort((a,b)=>a.y-b.y).forEach(o=>{const w=x.measureText(o.label).width+8*dpr,x0=W-R-w;
    const bh=fh+4*dpr,fits=t=>t>=0&&t+bh<=H&&!boxes.some(b=>t<b+bh&&b<t+bh);
    let t=o.y-fh-5*dpr;if(!fits(t)){t=o.y+3*dpr;if(!fits(t))boxes.slice().sort((a,b)=>a-b).forEach(b=>{if(t<b+bh&&b<t+bh)t=b+bh+2*dpr})}boxes.push(t);
    x.fillStyle='rgba(25,28,34,.85)';x.fillRect(x0,t,w,fh+4*dpr);x.fillStyle=o.color;x.textAlign='left';x.fillText(o.label,x0+4*dpr,t+fh)});
  let lg='<span><i class="legend-dot" style="background:#f2bd55"></i>DMD</span>';for(let i=0;i<n;i++)lg+=`<span><i class="legend-dot" style="background:${LCOL[i]}"></i>${T('laserModule',i+1)}</span>`;set('thermalLegend',lg);
}
function renderProtection(d){
  const l=d.limits||{};
  set('protNote',`${l.from_firmware===false?T('nDef')+' ':''}${T('nPhoto')}`);
}
function errText(d){const c=d.error_code;if(c&&I18N.ru['e_'+c]!=null){const t=T('e_'+c,...(d.error_args||[]));return esc(t)+(c==='adb'||c==='service'?'<br><span class="muted">'+esc(d.error)+'</span>':'')}return esc(d.error)}
function render(r){
  const d=r.latest||{};
  if(d.error){const zz=d.error_code==='asleep';$('status').className='status off';$('status').innerHTML='<i class="dot"></i>'+T(zz?'asleepSt':'offline');$('error').innerHTML=`<div class="error">${errText(d)}${zz?'':'<br><span class="muted">'+T(IS_APP?'errApp':'errDash')+'</span>'}</div>`;return}
  $('status').className='status';$('status').innerHTML='<i class="dot"></i>'+T('connected');$('error').innerHTML='';
  $('name').textContent=d.name||'JMGO';
  $('device').textContent=`${T('updated')} ${new Date(d.ts*1000).toLocaleTimeString(LANG==='ru'?'ru':'en-GB')}${d.uptime?' · '+T('uptime',Math.floor(d.uptime/3600),Math.floor(d.uptime%3600/60)):''}`;
  const th=d.thermal||{},lim=d.limits||{};
  const t=d.temp,thr=d.trip_passive0||123;set('temp',t==null?'—':esc(t));$('temp').className='big '+(t>=100?'hot':t>=90?'warn':'');
  const hasLoad=d.cpu_usage!=null&&d.cpu_usage>=0;set('load',hasLoad?`${d.cpu_usage}<span class="unit">%</span>`:'');$('load').style.display=$('loadSep').style.display=hasLoad?'':'none';
  set('cpuSub',`${esc(d.chip||'')}<br>${d.throttled?`<span class="thr">${T('thrNow',d.throttle_since?hm(d.throttle_since):'—',d.freq_cap)}</span>`:T('cpuThrAt',thr)}`);
  const dm=th.dmd,dmM=dm!=null&&lim.dmd_shutdown?lim.dmd_shutdown-dm:null;
  set('dmd',dm==null?'—':fmt(dm));$('dmd').className='big '+margCls(dmM);
  set('dmdSub',dm==null?(lim.from_firmware&&!lim.dmd_shutdown&&th.fresh?T('noDmd'):th.age!=null?T('stale',th.age):T('waiting')):lim.dmd_shutdown?T('toOff',fmt(dmM),lim.dmd_shutdown):T('noLim'));
  set('lensVal',th.lens!=null?fmt(th.lens):'—');$('lensVal').parentNode.classList.toggle('has-lens',th.lens!=null);
  const lm=th.laser_max,lM=lm!=null&&lim.laser_derate?lim.laser_derate-lm:null;
  set('laserMax',lm==null?'—':fmt(lm));$('laserMax').className='big '+margCls(lM);
  set('laserRow',(th.lasers||[]).map((v,i)=>`<div class="laser-chip" style="border-bottom:2px solid ${LCOL[i]}"><small>${T('module')} ${i+1}</small>${fmt(v)}</div>`).join(''));
  set('laserSub',lm==null?'—':lim.laser_derate?T('toDim',fmt(lM),lim.laser_derate):T('noLim'));
  set('fan1',d.fan1??'—');set('fan2',d.fan2??'—');
  const duty=th.fan_duty,tg=pidTargets(lim),fc=d.fan_ctl;const tgt=th.fan_mode!=='PID'?null:fc&&fc.now>0?fc.now:tg.length?tg[Math.min(th.overlap_level||0,tg.length-1)]:null;
  updTgtBtn(fc);
  set('fanSub',duty&&duty.length?T('fanSub',Math.max(...duty),tgt)+(fc&&fc.unreach&&fc.custom?`<br><span class="fan-miss">${T('fanMiss',fc.custom)}</span>`:''):T('fanNo'));
  const mode=d.brightness_mode,fixed=FIXED_MODES.includes(mode);
  let big='—';
  if(fixed)big=T('brMax');
  else if(d.brightness_menu!=null)big=`${d.brightness_exact===false?'≈':''}${d.brightness_menu}<span class="unit"> / 10</span>`;
  set('brightness',big);
  const bs=[];const ml=(T('modes')||{})[mode];const ap=d.aperture>0?`${T('iris')} F${+(+d.aperture).toFixed(1)}`:'';
  if(ml)bs.push(esc(ml)+(ap?' · '+ap:''));else if(ap)bs.push(ap);
  if(!fixed&&d.brightness_actual!=null)bs.push(`<b style="color:var(--blue)">${T('alNow',d.brightness_actual)}</b>`);else if(!fixed&&d.al_enabled)bs.push(T('alOn'));
  if(d.bw_status===2&&mode!==22&&mode!==23)bs.push(T('bwWhite'));
  const pc=d.laser_current_pct;
  bs.push(pc?`${T('current')}: <b style="color:#ff6b6b">R ${pc[0]}%</b> · <b style="color:#5ee08a">G ${pc[1]}%</b> · <b style="color:#6fa8ff">B ${pc[2]}%</b>`:T('curNo'));
  set('brSub',bs.join('<br>'));$('brightness').classList.toggle('dense',bs.length>3);
  set('freq',d.freq?unit(d.freq,T('mhz')):'—');$('freq').className='value'+(d.throttled?' hot':'');
  set('freqHint',d.throttled?`<span class="thr">${T('thrCap',d.freq_cap)}</span>`:(d.freq_min&&d.freq_hw&&d.freq_min>=d.freq_hw?T('freqFixed'):T('freqHint',d.freq_hw||'—',d.cpu_usage>=0?d.cpu_usage:null)));
  const used=d.mem_total&&d.mem_avail?Math.round((d.mem_total-d.mem_avail)/d.mem_total*100):null;
  set('memory',used==null?'—':`${used}<span class="unit"> ${T('usedP')}</span>`);set('memoryHint',used==null?'':T('memHint',Math.round((d.mem_total-d.mem_avail)/1024),Math.round(d.mem_total/1024)));
  const stT=d.storage_total_kb,stU=stT&&d.storage_free_kb!=null?stT-d.storage_free_kb:null;
  set('storage',stU==null?'—':`${Math.round(stU/stT*100)}<span class="unit"> ${T('usedP')}</span>`);set('storageHint',stU==null?'':T('stHint',(stU/1048576).toFixed(1),Math.round(stT/1048576)));
  set('output',d.output_width&&d.output_height?`${resolutionLabel(d.output_width,d.output_height)}<span class="unit"> ${d.output_width}×${d.output_height} · ${d.output_hz||'—'} ${T('hz')}</span>`:'—');
  let oh='';
  if(d.input_rslt){const m=/(\d+)\s*[xX×]\s*(\d+)[^@\d]*(?:@\s*([\d.]+))?/.exec(d.input_rslt);const hz=m&&m[3]?m[3]:d.input_fps;let z=hz?Math.round(+hz):0;if([23,29,47,59,119].includes(z))z++;
    oh=`${T('hdmi')}: ${m?shortRes(+m[1],+m[2]):esc(d.input_rslt)}${z?` · ${z} ${T('hz')}`:''}${d.input_vrr>0?' · VRR':''}`}
  set('outputHint',oh);
  const mx=d.session_max||{};set('maxDmd',mx.dmd!=null?`${fmt(mx.dmd)} °C`:'—');set('maxLaser',mx.laser!=null?`${fmt(mx.laser)} °C`:'—');set('maxCpu',mx.cpu!=null?`${mx.cpu} °C`:'—');
  set('maxThr',d.throttle_first?`<b class="hot">${T('was',hm(d.throttle_first))}</b>`:T('no'));
  set('appVersion',esc(d.app_version||'—'));set('firmwareVersion',esc(d.firmware_version||'—'));
  set('dlpVersion',esc(d.dlp_version||'—'));set('modelCode',esc(d.model||'—'));
  const zones=d.extra_zones||[],gpu=zones.find(z=>/gpu/i.test(z.name)),apu=zones.find(z=>/apu|npu/i.test(z.name));
  set('gpuTemp',gpu?`${gpu.temp} °C`:'—');set('apuTemp',apu?`${apu.temp} °C`:'—');$('extraZoneCard').classList.toggle('visible',Boolean(gpu||apu));
  const al=[];
  if(dmM!=null&&dmM<=6)al.push(T('aDmd',fmt(dm),lim.dmd_shutdown));
  if(lM!=null&&lM<=6)al.push(T('aLas',fmt(lm),lim.laser_derate));
  if(d.throttled)al.push(T('aThr',d.throttle_since?hm(d.throttle_since):'—',d.freq_cap));
  // вентилятор стоит: 0 об/мин при поданной мощности > 30 % (одиночные сбойные нули отфильтрованы еще при опросе)
  const dmax=duty&&duty.length?Math.max(...duty):null;const stop=[d.fan1,d.fan2].map((v,i)=>v===0&&(dmax!=null?dmax>30:Math.max(d.fan1||0,d.fan2||0)>300)?i+1:0).filter(Boolean);
  if(stop.length)stop.forEach(n=>al.push(T('aStop',n,dmax)));
  else if(d.fan1!=null&&d.fan2!=null&&Math.max(d.fan1,d.fan2)>300&&Math.abs(d.fan1-d.fan2)/Math.max(d.fan1,d.fan2)>0.25)al.push(T('aFans',d.fan1,d.fan2));
  if(th.fresh===false&&th.age==null)al.push(T('aNoTh'));
  $('alert').innerHTML=al.length?`<div class="alert">${al.map(esc).join('<br>')}</div>`:'';
  renderProtection(d);
  drawThermal(r.history||[],lim);drawCombined(r.history||[]);
}
const CROT={tr:0,br:90,bl:180,tl:270},CHEV='<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const arrIco=c=>`<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" style="transform:rotate(${CROT[c]||0}deg)"><path d="M7 17 17 7M9 7h8v8" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
function updOverlayBtn(){if(!IS_APP)return;try{const on=Android.overlayOn(),can=Android.canOverlay();const b=$('ovBtn');b.innerHTML=esc(T('ovBtn',on))+CHEV;b.classList.toggle('on',on);
 if($('ovs').classList.contains('open'))renderOvs();
 $('ovWarn').innerHTML=can?'':`<div class="alert">${T('ovWarn')}<br><b>adb shell appops set ${esc(typeof Android.pkg==='function'?Android.pkg():'io.github.jmgotemp')} SYSTEM_ALERT_WINDOW allow</b></div>`}catch(e){}}
// Компактный режим (с 1.6.1): одна кнопка в шапке, в окне — вкл/выкл, что показывать (до 3 строк), размер, фон, угол
const OV_KEYS=['dmd','las','cpu','load','freq','fan','br','hdmi'],OV_SIZE=[8,10,12],OV_BG=[0,40,60,80,100],OV_CORN=['tl','tr','bl','br'];
let OVP=null;
function ovGet(){try{OVP=JSON.parse(Android.ovPrefs())}catch(e){}OVP=Object.assign({items:'dmd,las,cpu,fan,br,hdmi',size:10,bg:80,corner:'tr'},OVP||{});OVP.size=Math.max(8,Math.min(12,+OVP.size));if(OVP.size%2)OVP.size=OVP.size>10?12:10}
function ovSet(k,v){
 if(k==='items'){const s=new Set(OVP.items.split(','));if(s.has(v)){if(s.size<2)return;s.delete(v)}else s.add(v);OVP.items=OV_KEYS.filter(x=>s.has(x)).join(',')}else OVP[k]=v;
 try{Android.setOvPrefs(OVP.items,+OVP.size,+OVP.bg,OVP.corner)}catch(e){}renderOvs()}
function ovToggle(){try{Android.toggleOverlay()}catch(e){}setTimeout(updOverlayBtn,400)}
function ovPreview(){const d=last&&last.latest||{},th=d.thermal||{},lim=d.limits||{},it=new Set(OVP.items.split(',')),ru=LANG==='ru';
 const g=t=>`<span class="g">${t}</span>`,c=(v,l)=>v==null||!l?'':l-v<=2?'h':l-v<=6?'w':'ok',n=v=>v==null?'—':v.toFixed(1)+'°';
 const L1=[],L2=[],L3=[];
 if(it.has('dmd'))L1.push(g('DMD ')+`<span class="${c(th.dmd,lim.dmd_shutdown)}">${n(th.dmd)}</span>`);
 if(it.has('las'))L1.push(g(ru?'Лазер ':'Laser ')+`<span class="${c(th.laser_max,lim.laser_derate)}">${n(th.laser_max)}</span>`);
 const cp=[];if(it.has('cpu')&&d.temp!=null)cp.push(`<span class="${d.temp>=100?'h':d.temp>=90?'w':'ok'}">${d.temp}°</span>`);
 if(it.has('load')&&d.cpu_usage>=0)cp.push(String(d.cpu_usage).padStart(2)+'%');if(it.has('freq')&&d.freq)cp.push(d.freq+g(ru?' МГц':' MHz'));if(cp.length)L1.push(g('CPU ')+cp.join(' '));
 if(d.throttled)L1.push(`<span class="h">⚠ ${ru?'троттлинг':'throttling'} ${d.freq_cap} ${ru?'МГц':'MHz'}</span>`);
 const du=th.fan_duty,rpm=Math.max(d.fan1||-1,d.fan2||-1);
 if(it.has('fan'))L2.push(g(ru?'Вент ':'Fans ')+(rpm>0?String(rpm).padStart(4)+(ru?' об/мин':' rpm'):'—')+(du&&du.length?g(` (${String(Math.max(...du)).padStart(2)}%)`):''));
 if(it.has('br')){const fx=FIXED_MODES.includes(d.brightness_mode);L2.push(g(ru?'Яркость ':'Brightness ')+(fx?(ru?'макс.':'max'):d.brightness_menu!=null?(d.brightness_exact===false?'≈':'')+d.brightness_menu+'/10'+(d.brightness_actual!=null?`<span class="b"> (AL ≈${d.brightness_actual})</span>`:''):'—')+(d.aperture?' F'+d.aperture:''))}
 if(it.has('hdmi')&&d.output_width)L3.push(g(ru?'Вывод ':'Output ')+`${d.output_width>=3840?'4K':d.output_height>=1080?'1080p':d.output_height+'p'} ${d.output_hz||''} ${ru?'Гц':'Hz'}`);
 const box=$('ovBox');box.innerHTML=[L1,L2,L3].filter(l=>l.length).map(l=>l.join('  ')).join('\n')||g('JMGO Temp');
 // drawn at 2x and scaled down: WebView clamps fonts below 8px, which flattened the 12/15/18 difference
 box.style.fontSize=(OVP.size*.1728).toFixed(2)+'vh';box.style.background=`rgba(16,18,22,${OVP.bg/100})`;box.style.textShadow=+OVP.bg?'':'0 0 4px #000,0 0 2px #000';
 const sc=$('ovSc');sc.style.width=Math.ceil(box.offsetWidth/2)+'px';sc.style.height=Math.ceil(box.offsetHeight/2)+'px'}
function renderOvs(){if(!OVP)ovGet();const a=document.activeElement,fk=a&&$('ovs').contains(a)?a.dataset.k:null,fv=a&&a.dataset.v;let on=false;try{on=Android.overlayOn()}catch(e){}
 $('ovsOn').innerHTML=`<button class="btn wn-row ov-on${on?' on':''}" data-k="on" data-v="1" onclick="ovToggle()"><b>${T(on?'wnOn':'wnOff')}</b><span>${T('ovsShow')}</span></button>`;
 const sel=new Set(OVP.items.split(',')),ch=(k,v,html,on,cls)=>`<button class="btn ov-chip${on?' on':''}${cls||''}" data-k="${k}" data-v="${v}" onclick="ovSet('${k}','${v}')">${html}</button>`;
 $('ovItems').innerHTML=[['dmd','las','fan','br'],['cpu','load','freq','hdmi']].map(r=>'<div>'+r.map(k=>ch('items',k,T('ovItems')[k],sel.has(k))).join('')+'</div>').join('');
 $('ovSize').innerHTML=OV_SIZE.map(v=>ch('size',v,`<span style="font-size:2.3vh;display:inline-block;transform:scale(${{8:.5,10:.7,12:1}[v]}) translateY(.1em)">A</span>`,+OVP.size===v,' ov-sz')).join('');
 const bgSel=OV_BG.reduce((a,v)=>Math.abs(v-OVP.bg)<Math.abs(a-OVP.bg)?v:a);
 $('ovBg').innerHTML=OV_BG.map(v=>ch('bg',v,v||T('ovNoBg'),bgSel===v)).join('');
 $('ovCorner').innerHTML=OV_CORN.map(v=>ch('corner',v,arrIco(v),OVP.corner===v)).join('');ovPreview();
 if(fk){const b=$('ovs').querySelector(`[data-k="${fk}"][data-v="${fv}"]`);b&&b.focus()}}
function openOvs(){if(!IS_APP)return;ovGet();renderOvs();posPop('ovBtn','#ovs .wn-box',12);requestAnimationFrame(()=>posPop('ovBtn','#ovs .wn-box',12));
 popOpened();$('ovs').classList.add('open');popFocus(()=>$('ovsOn').querySelector('.btn'))}
function closeOvs(){$('ovs').classList.remove('open');$('ovBtn').focus()}
// кнопки, сгруппированные по видимым строкам (сверху вниз, слева направо)
function visRows(bs){const rows=[];[...bs].map(b=>[b,b.getBoundingClientRect()]).sort((x,y)=>x[1].top-y[1].top||x[1].left-y[1].left)
 .forEach(([b,r])=>{const l=rows[rows.length-1];if(l&&Math.abs(l.top-r.top)<r.height/2)l.bs.push(b);else rows.push({top:r.top,bs:[b]})});return rows.map(r=>r.bs)}
// пульт в окнах с рядами: ◀ ▶ внутри ряда, ▲ ▼ — в соседний ряд к ближайшей по горизонтали кнопке
function navRows(rows,key){let ri=rows.findIndex(r=>r.includes(document.activeElement));if(ri<0){rows[0][0].focus();return}
 const r=rows[ri],ci=r.indexOf(document.activeElement);
 if(key==='ArrowLeft'||key==='ArrowRight'){const b=r[Math.max(0,Math.min(r.length-1,ci+(key==='ArrowRight'?1:-1)))];b.focus();return}
 const nr=rows[Math.max(0,Math.min(rows.length-1,ri+(key==='ArrowDown'?1:-1)))];if(nr===r)return;
 const x=document.activeElement.getBoundingClientRect().left;let best=nr[0];nr.forEach(b=>{if(Math.abs(b.getBoundingClientRect().left-x)<Math.abs(best.getBoundingClientRect().left-x))best=b});best.focus()}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&onBack()){e.preventDefault();return}
  if($('donate').classList.contains('open')&&e.key.startsWith('Arrow')){$('dnClose').focus();e.preventDefault();return}
  if($('tgt').classList.contains('open')&&e.key.startsWith('Arrow')){const a=document.activeElement;
    if(a&&a.classList.contains('st-val')&&(e.key==='ArrowLeft'||e.key==='ArrowRight')){stepTgt(a.dataset.r,e.key==='ArrowRight'?1:-1);e.preventDefault();return}
    const wasN=a&&a.closest('.tgt-n');navRows([...$('tgtList').querySelectorAll('.tgt-row')].map(r=>[...r.querySelectorAll('.btn:not([disabled])')]).filter(r=>r.length),e.key);
    // вверх на выбор числа строк — на выбранную цифру, а не на ближайшую
    if(!wasN&&document.activeElement.closest('.tgt-n')){const on=$('tgtList').querySelector('.tgt-n .on');on&&on.focus()}e.preventDefault();return}
  if($('ovs').classList.contains('open')&&e.key.startsWith('Arrow')){navRows(visRows($('ovs').querySelectorAll('.wn-box .btn')),e.key);e.preventDefault();return}
  const dd=['warn','tgt','upd'].map($).find(x=>x.classList.contains('open'));
  if(dd&&e.key.startsWith('Arrow')){const bs=[...dd.querySelectorAll('.btn')];let i=bs.indexOf(document.activeElement);const n=bs.length;
    i=i<0?0:e.key==='ArrowDown'?Math.min(n-1,i+1):e.key==='ArrowUp'?Math.max(0,i-1):e.key==='ArrowRight'?Math.min(n-1,i+1):Math.max(0,i-1);bs[i].focus();bs[i].scrollIntoView({block:'nearest'});e.preventDefault();return}
  const tb=$('tgtBtn'),inHead=document.activeElement&&document.activeElement.closest('.head');
  if(e.key==='ArrowDown'&&inHead&&tb.offsetParent){tb.focus();e.preventDefault();return}
  if(e.key==='ArrowUp'&&document.activeElement===tb){(IS_APP?$('ovBtn'):$('langBtn')).focus();e.preventDefault();return}
  // с «Изменить °C»: влево — «Проверить обновление» (слева над ней), вправо — «Компактный режим» (над ней)
  if(document.activeElement===tb&&(e.key==='ArrowLeft'||e.key==='ArrowRight')){const t=e.key==='ArrowLeft'?$('updBtn'):(IS_APP?$('ovBtn'):$('langBtn'));
    if(t&&t.offsetParent){t.focus();e.preventDefault();return}}
  if(e.key==='ArrowDown'){window.scrollBy({top:260,behavior:'smooth'});e.preventDefault()}
  else if(e.key==='ArrowUp'){window.scrollBy({top:-260,behavior:'smooth'});e.preventDefault()}
  else if(e.key==='ArrowRight'||e.key==='ArrowLeft'){const bs=[...document.querySelectorAll('.btn')].filter(b=>b.offsetParent&&!b.closest('#warn,#tgt,#ovs,#upd,#donate'));let i=bs.indexOf(document.activeElement);i=i<0?0:(i+(e.key==='ArrowRight'?1:-1)+bs.length)%bs.length;bs[i].focus();e.preventDefault()}
});
// Обновления (с 1.6): APK — Android.updState()/updCheck()/updInstall(), браузер — /upd на локальном сервере.
// Проверка — номер последнего релиза на GitHub; никаких данных не отправляется.
let UPD={},updSig='',updN=0;
function updFetch(force){if(IS_APP){try{UPD=JSON.parse(Android.updState())}catch(e){}renderUpd();return}
 if(!force&&!$('upd').classList.contains('open')&&(updN++%15))return;fetch('/upd',{cache:'no-store'}).then(r=>r.json()).then(j=>{UPD=j;renderUpd()}).catch(()=>{})}
function updWhen(ts){if(!ts)return T('upNever');const d=new Date(ts*1000),n=new Date(),hm=d.toTimeString().slice(0,5);
 return T('upChecked',d.toDateString()===n.toDateString()?T('upToday')+' '+hm:d.toLocaleDateString(LANG==='ru'?'ru-RU':'en-GB',{day:'numeric',month:'short'})+' '+hm)}
function renderUpd(){const u=UPD||{},ph=u.phase||'idle',cur=u.cur||'';$('hdrVer').textContent=cur;
 $('updBtn').classList.toggle('new',!!u.newer);if(!$('upd').classList.contains('open'))return;
 $('upCur').innerHTML=T('upCur',esc(cur));let cls='',st='';
 if(ph==='checking')st=T('upChecking');else if(ph==='download')st=T('upDl',Math.max(0,u.progress|0)),cls='new';else if(ph==='install')st=T('upInst'),cls='new';
 else if(ph==='error'){cls='err';st=esc(T('upErr_'+u.err)||u.err)}
 else if(u.newer){cls='new';st=T('upNew',esc(u.latest))}else if(u.latest){cls='ok';st=T('upOk')+`<small>${updWhen(u.checked)}</small>`}else st=T('upNever');
 $('upSt').className='up-st '+cls;$('upSt').innerHTML=st;renderNotes(u);
 const busy=ph==='checking'||ph==='download'||ph==='install',sig=[busy,u.newer,u.can_install,LANG].join();if(sig===updSig)return;updSig=sig;
 const f=document.activeElement&&document.activeElement.id;let h='';
 if(u.newer&&!busy)h+=u.can_install?`<button class="btn go" id="upGo" onclick="updInstall()">${T('upInstall')}</button>`:`<a class="btn go" id="upGo" href="${esc(u.page)}" target="_blank" rel="noopener">${T('upOpen')}</a>`;
 if(!busy)h+=`<button class="btn" id="upChk" onclick="updCheck()">${T('upCheck')}</button>`;
 const inPop=document.activeElement&&document.activeElement.closest&&document.activeElement.closest('#upd');$('upBtns').innerHTML=h;
 if(!inPop||!document.activeElement.offsetParent){const fb=(f&&$(f))||$('upGo')||$('upChk');fb&&fb.focus()}}
// «Что нового»: из описания релиза на GitHub (английские пункты, пустая строка, русские); до 6 пунктов, язык по интерфейсу
function noteItems(body){const part=String(body||'').split(/\n-{3,}\s*(\n|$)/)[0],gs=part.split(/\n\s*\n/).map(g=>g.split('\n').map(x=>x.trim()).filter(x=>/^[-*•]\s+/.test(x)).map(x=>x.replace(/^[-*•]\s+/,''))).filter(g=>g.length);
 if(!gs.length)return [];return gs.length>1&&LANG==='ru'?gs[1]:gs[0]}
function renderNotes(u){const el=$('upNew'),it=u.newer?noteItems(u.notes):[];if(!it.length){el.style.display='none';el.innerHTML='';return}
 const sh=it.slice(0,6);el.innerHTML=`<h4>${esc(T('upNewT',u.latest))}</h4><ul>${sh.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`+(it.length>6?`<div class="more">${esc(T('upMore',it.length-6))}</div>`:'');el.style.display=''}
function updCheck(){if(IS_APP){try{Android.updCheck()}catch(e){}}else fetch('/upd?check=1',{cache:'no-store'}).catch(()=>{});UPD.phase='checking';UPD.err='';renderUpd();setTimeout(()=>updFetch(true),1500)}
function updInstall(){let r='';try{r=Android.updInstall()}catch(e){r='inst'}if(r!=='ok'){UPD.phase='error';UPD.err=r==='busy'?'':r}updFetch(true)}
// окно раскрывается вниз от кнопки (она слева в шапке), стрелка указывает на нее
function openUpd(){updSig='';const r=$('updBtn').getBoundingClientRect(),b=$('updBox'),left=Math.max(8,r.left-24);
 b.style.top=(r.bottom+12)+'px';b.style.left=left+'px';const cx=r.left+r.width/2-left-8;b.style.setProperty('--cx',cx+'px');b.style.setProperty('--ox',(cx+8)+'px');
 popOpened();$('updBtn').classList.add('on');$('upd').classList.add('open');updFetch(true);renderUpd();popFocus(()=>$('upGo')||$('upChk'))}
function closeUpd(){$('upd').classList.remove('open');$('updBtn').classList.remove('on');$('updBtn').focus()}
async function tick(){
  try{
    if(IS_APP)last=JSON.parse(Android.api());
    else last=await (await fetch('/api',{cache:'no-store'})).json();
    render(last);updOverlayBtn();updFetch();
  }catch(e){$('status').className='status off';$('status').innerHTML='<i class="dot"></i>'+T('error')+esc(e)}
}
applyStatic();updOverlayBtn();
(IS_APP?$('ovBtn'):$('langBtn')).focus();
addEventListener('resize',()=>{setNarrow();last&&render(last)});tick();setInterval(tick,IS_APP?1000:2000);
</script></body></html>
'''


# ---------------------------------------------------------------- обновления (с 1.6)
# Только номер последнего релиза с GitHub: при запуске и по кнопке «Проверить». Ничего не отправляется.
UPD_API = "https://api.github.com/repos/jmgotemp/jmgo-temp/releases/latest"
UPD_PAGE = "https://github.com/jmgotemp/jmgo-temp/releases/latest"
UPD: dict = {"cur": VERSION, "latest": "", "newer": False, "checked": 0, "phase": "idle", "err": "",
             "progress": -1, "page": UPD_PAGE, "can_install": False, "notes": ""}


def _ver_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v.lstrip("vV")))


def _http_get(url: str) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "JMGO-Temp", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8")
    except Exception as exc:
        # Python с python.org на macOS часто без корневых сертификатов: пробуем certifi, затем системный curl
        if "CERTIFICATE" not in str(exc).upper():
            raise
        try:
            import ssl
            import certifi  # type: ignore
            ctx = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                return r.read().decode("utf-8")
        except ImportError:
            pass
        curl = shutil.which("curl")
        if not curl:
            raise
        return subprocess.run([curl, "-fsSL", "-m", "10", "-H", "User-Agent: JMGO-Temp", url],
                              capture_output=True, text=True, check=True).stdout


def check_update(announce: bool = False) -> None:
    UPD.update(phase="checking", err="")
    try:
        rel = json.loads(_http_get(UPD_API))
        tag = rel.get("tag_name", "").lstrip("vV")
        UPD.update(latest=tag, newer=bool(tag) and _ver_tuple(tag) > _ver_tuple(VERSION),
                   checked=int(time.time()), phase="idle", notes=rel.get("body") or "")
        if announce and UPD["newer"]:
            print((f"Вышла версия {tag}: {UPD_PAGE}" if CONSOLE_RU else f"Version {tag} is out: {UPD_PAGE}"), flush=True)
    except Exception:
        UPD.update(phase="error", err="net")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        with LOCK:
            payload = {"latest": dict(LATEST), "history": list(HISTORY)}
        if self.path.startswith("/upd"):
            if "check=1" in self.path and UPD.get("phase") != "checking":
                threading.Thread(target=check_update, daemon=True).start()
                UPD["phase"] = "checking"
            body = json.dumps(UPD, ensure_ascii=False).encode()
            content_type = "application/json; charset=utf-8"
        elif self.path.startswith("/api"):
            body = json.dumps(payload, ensure_ascii=False).encode()
            content_type = "application/json; charset=utf-8"
        else:
            body = HTML.encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    R = CONSOLE_RU
    ap = argparse.ArgumentParser(description=f"JMGO Temp {VERSION}: " + ("монитор температур JMGO по ADB (только чтение)." if R
                                 else "JMGO projector temperature monitor over ADB (read-only)."))
    ap.add_argument("--version", action="version", version=f"JMGO Temp (jmgo_dash.py) {VERSION}")
    ap.add_argument("target", nargs="?", default="", help="IP:порт беспроводной отладки (необязательно)" if R
                    else "wireless debugging IP:port (optional)")
    ap.add_argument("--port", type=int, default=PORT, help="порт веб-страницы (по умолчанию 8777)" if R
                    else "web page port (default 8777)")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически" if R
                    else "do not open the browser automatically")
    args = ap.parse_args()
    TARGET = args.target.strip()
    PORT = args.port
    print(f"JMGO Temp {VERSION} " + ("(браузерная версия)" if R else "(browser version)"), flush=True)
    print(f"adb: {ADB}", flush=True)
    threading.Thread(target=poll, daemon=True).start()
    threading.Thread(target=check_update, args=(True,), daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    try:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"Порт {PORT} занят — возможно, дашборд уже запущен. Откройте http://localhost:{PORT} "
                 f"или запустите с другим портом: --port 8778" if R else
                 f"Port {PORT} is busy — the dashboard may already be running. Open http://localhost:{PORT} "
                 f"or start with another port: --port 8778")
    with server:
        url = f"http://localhost:{PORT}"
        print((f"Дашборд: {url}   (Ctrl+C — стоп)" if R else f"Dashboard: {url}   (Ctrl+C to stop)"), flush=True)
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            WATCHER.stop()
