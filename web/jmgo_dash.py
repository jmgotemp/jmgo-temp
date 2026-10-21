#!/usr/bin/env python3
"""JMGO Temp (браузерная версия) — read-only монитор JMGO по ADB (macOS / Windows / Linux).

Версия: см. VERSION ниже (нумерация общая с APK JMGO Temp).

Запуск:
    macOS:    python3 jmgo_dash.py [IP:порт]
    Windows:  py jmgo_dash.py [IP:порт]
    Страница откроется сама: http://localhost:8777

Нужны Python 3.9+ и adb (Android platform-tools). adb ищется в PATH или рядом со скриптом
(можно просто положить jmgo_dash.py в распакованную папку platform-tools).
IP:порт необязателен: скрипт возьмёт уже подключённый проектор или найдёт его в сети сам.

Что показывает:
  * температуру матрицы DMD и лазерных модулей — их измеряет системный сервис JMGO
    (hippo_server) и раз в 2–10 с пишет в logcat; читаем поток лога, root не нужен;
  * пороги защиты (снижение яркости / отключение) и цели вентиляторов — берутся прямо
    из конфигурации прошивки (hippo_configs.xml внутри APK com.jmgo.hippo);
  * уровень яркости и ток лазеров R/G/B (из /sys/class/jmgo_dlp/brightness);
  * процессор, вентиляторы, память, накопитель, вывод изображения.

Ничего на проекторе не меняет: только чтение.
"""
from __future__ import annotations

import argparse
import collections
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
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


VERSION = "1.1"  # та же нумерация, что у APK JMGO Temp

ADB = find_adb()
TARGET = ""  # IP:порт из командной строки (необязательно)
HISTORY = collections.deque(maxlen=720)  # примерно час при опросе раз в 5 секунд
LATEST: dict = {"error": "Подключение к проектору…"}
LOCK = threading.Lock()

THERMAL_FRESH_S = 40  # если дольше нет строк от hippo — данные считаем устаревшими

# Индексы датчиков в hippo: 7 — DMD, 10/11/12/14 — лазерные модули 1..4
LASER_INDEX = {10: 0, 11: 1, 12: 2, 14: 3}
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
echo "model=$(getprop ro.product.model)"
echo "jmodel=$(getprop ro.boot.jmgo.model)"
echo "name=$(settings get global device_name 2>/dev/null)"
echo "platform=$(getprop ro.board.platform)"
echo "android=$(getprop ro.build.version.release)"
echo "firmware=$(getprop ro.build.display.id)"
echo "incremental=$(getprop ro.build.version.incremental)"
echo "fingerprint=$(getprop ro.build.fingerprint)"
echo "thermal_type=$(val /sys/class/thermal/thermal_zone0/type)"
echo "temp=$(val /sys/class/thermal/thermal_zone0/temp)"
echo "fan=$(val /sys/class/jmgo_fan/tachometer)"
echo "freq=$(val /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq)"
echo "freq_max=$(val /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq)"
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
echo "dlp_version=$(val /sys/class/jmgo_dlp/version)"
echo "dlp_source=$(val /sys/class/jmgo_dlp/source)"
echo "dlp_enable_vrr=$(val /sys/class/jmgo_dlp/enable_vrr)"
echo "dlp_model=$(getprop vendor.jmgo.dlp.model)"
echo "brightness=$(settings get global jmgo_display_brightness 2>/dev/null)"
echo "picture_backlight=$(settings get global picture_backlight 2>/dev/null)"
echo "picture_mode=$(settings get global picture_mode 2>/dev/null)"
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

# Достаём конфиг hippo для своей модели прямо из APK на проекторе (только чтение).
CONFIG_SCRIPT = r'''#!/system/bin/sh
A=$(pm path com.jmgo.hippo 2>/dev/null | head -1 | cut -d: -f2)
M=$(getprop ro.boot.jmgo.model); [ -z "$M" ] && M=$(getprop ro.product.model)
echo "APK=$A"
echo "MODEL=$M"
F="assets/$M/hippo_configs.xml"
if [ -n "$A" ] && unzip -l "$A" 2>/dev/null | grep -q " $F\$"; then
  echo "FILE=$F"
  echo "-----BEGIN-----"
  unzip -p "$A" "$F"
fi
'''


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
        raise RuntimeError("Не найден adb. Положи jmgo_dash.py в папку platform-tools или установи adb.")
    except Exception:
        pass
    if TARGET:
        raise RuntimeError(f"Не удалось подключиться к {TARGET}. Проверь IP:порт в настройках беспроводной отладки.")

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
    raise RuntimeError("Проектор не найден. Включи беспроводную отладку ADB на проекторе и подключи компьютер к той же сети "
                       "(или запусти скрипт с адресом: jmgo_dash.py 192.168.x.x:порт).")


def adb_shell(serial: str, script: str = REMOTE_SCRIPT, timeout: float = 20) -> str:
    # Скрипт отдаём через stdin (`sh -s`): одинаково работает на macOS и Windows.
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
    m = re.search(r"assets/([^/]+)/", file_name)
    limits["source"] = f"из прошивки этого проектора ({m.group(1) if m else file_name})"
    limits["from_firmware"] = True
    check = _float_rows(xml, "SYSTEM_CHECK_TEMPERATURE_CONFIG")
    sensors = []
    laser_shut, laser_derate = [], []
    for row in check:
        if len(row) < 2:
            continue
        idx, shutdown = int(row[0]), row[1]
        derate = row[2] if len(row) > 2 and row[2] > 0 else None
        sensors.append({"index": idx, "shutdown": shutdown, "derate": derate})
        if idx == DMD_INDEX:
            limits["dmd_shutdown"] = shutdown
        elif idx in LASER_INDEX:
            laser_shut.append(shutdown)
            if derate:
                laser_derate.append(derate)
    if laser_shut:
        limits["laser_shutdown"] = min(laser_shut)
    if laser_derate:
        limits["laser_derate"] = min(laser_derate)
    limits["sensors"] = sensors
    pid = _float_rows(xml, "FAN_PID_CONTROL_CONFIG")
    if pid:
        limits["pid_targets"] = [r[0] for r in pid if r]
        # строка вида {50, 95, 95, 100}: мин = макс — это фиксированные обороты, а не цель PID
        limits["pid_fixed"] = [len(r) > 2 and r[1] >= r[2] for r in pid if r]
    interp = _float_rows(xml, "DISPLAY_BRIGHTNESS_INTERPOLATOR")
    if interp and len(interp[0]) >= 2:
        limits["interp_r"] = [int(v) for v in interp[0]]
    vmax = _float_list(xml, "BRIGHTNESS_VALUE_MAX")
    if vmax:
        limits["value_max"] = [int(v) for v in vmax[:3]]
    # Новые модели (N5S Ultra Max) читают DMD не терморезистором, а через контроллер DLP,
    # и тогда действует свой порог DMD_TEMPERATURE_CHECK_CONFIG (у KD11CN — 71 °C).
    limits["_dmd_mux"] = _float_list(xml, "TEMPERATURE_DMD_CHANNEL_MUX")
    limits["_dmd_dlp_cfg"] = _float_list(xml, "DMD_TEMPERATURE_CHECK_CONFIG")
    return limits


def dlp_version_int(text: str) -> int:
    """'6.0.0' -> 600, как в прошивке (a*100 + b*10 + c)."""
    try:
        p = [int(x) for x in re.findall(r"\d+", text or "")[:3]]
    except ValueError:
        return 0
    return sum(v * k for v, k in zip(p, (100, 10, 1)))


def dmd_by_dlp(mux: list, dlp_version: str) -> bool:
    """Повторяет TemperaturePolicy.isDmdTemperatureByDlp() из hippo."""
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
        raw = adb_shell(serial, CONFIG_SCRIPT, timeout=30)
        head, _, xml = raw.partition("-----BEGIN-----")
        file_name = ""
        for line in head.splitlines():
            if line.startswith("FILE="):
                file_name = line[5:].strip()
        if xml.strip():
            limits = parse_hippo_config(xml, file_name)
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


class LogWatcher:
    """Держит открытым `adb logcat -s hippo_server:V` и разбирает строки на лету."""

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
            p.kill()
        self.proc = None
        self.serial = ""

    def _run(self, serial: str, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                p = subprocess.Popen(
                    [ADB, "-s", serial, "logcat", "-v", "epoch", "-s", "hippo_server:V"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                )
                self.proc = p
                assert p.stdout is not None
                for line in p.stdout:
                    if stop.is_set():
                        break
                    parse_log_line(line)
                if p.poll() is None:
                    p.kill()
            except Exception:  # noqa: BLE001
                pass
            stop.wait(3)


WATCHER = LogWatcher()


# ---------------------------------------------------------------- снимок состояния

def parse_dlp_current(raw: str) -> list[int] | None:
    m = re.match(r"R(\d+)G(\d+)B(\d+)", raw or "")
    return [int(m.group(i)) for i in (1, 2, 3)] if m else None


def brightness_level(r: int | None, interp: list[int]) -> tuple[int | None, bool]:
    """Уровень 1–10 в меню по току красного лазера (таблица DISPLAY_BRIGHTNESS_INTERPOLATOR)."""
    if r is None or not interp:
        return None, False
    if r in interp:
        return interp.index(r) + 1, True
    nearest = min(range(len(interp)), key=lambda i: abs(interp[i] - r))
    return nearest + 1, False


def parse_snapshot(serial: str) -> dict:
    raw = adb_shell(serial)
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

    fans = [int(x) for x in re.findall(r"\d+", data.get("fan", ""))]
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

    model_names = {
        "K981CN": "JMGO N5 Pro",
        "KD11CN": "JMGO N5S Ultra Max",
    }
    friendly_name = model_names.get(model, friendly_name)

    return {
        "serial": serial,
        "name": friendly_name,
        "raw_name": raw_name,
        "model": model,
        "platform": platform,
        "chip": "MediaTek MT5877" if platform.lower() == "mt5877" else platform or "не определён",
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
    age = time.time() - th["recv_ts"] if "recv_ts" in th else None
    fresh = age is not None and age < THERMAL_FRESH_S
    lasers = [th.get(f"laser{i}") for i in range(4)]
    lasers = [v for v in lasers if v is not None]
    thermal = {
        "fresh": fresh,
        "age": round(age) if age is not None else None,
        "dmd": th.get("dmd") if fresh else None,
        "lasers": lasers if fresh else [],
        "laser_max": max(lasers) if fresh and lasers else None,
        "pid_temp": th.get("pid_temp") if fresh else None,
        "fan_duty": th.get("fan_duty") if fresh else None,
        "fan_mode": th.get("fan_mode"),
        "overlap_level": th.get("overlap_level"),
        "fan_status": th.get("fan_status"),
        "hippo_error": th.get("hippo_error"),
    }
    snapshot["thermal"] = thermal
    public_limits = {k: v for k, v in limits.items() if not k.startswith("_")}
    public_limits["dmd_source"] = "ntc"
    cfg = limits.get("_dmd_dlp_cfg") or []
    if cfg and cfg[0] > 0 and dmd_by_dlp(limits.get("_dmd_mux") or [], snapshot.get("dlp_version", "")):
        public_limits["dmd_shutdown"] = cfg[0]
        public_limits["dmd_source"] = "dlp"
        if len(cfg) > 1 and cfg[1] > 0:
            public_limits["dmd_derate"] = cfg[1]
    snapshot["limits"] = public_limits

    cur = parse_dlp_current(snapshot.get("dlp_brightness", ""))
    level, exact = brightness_level(cur[0] if cur else None, limits.get("interp_r", []))
    if level is None and snapshot.get("brightness_raw", -1) >= 0:
        level, exact = max(1, min(10, (snapshot["brightness_raw"] + 5) // 10)), False
    vmax = limits.get("value_max") or [1023, 1023, 1023]
    snapshot["laser_current"] = cur
    snapshot["laser_current_pct"] = [round(c * 100 / (vmax[i] or 1023)) for i, c in enumerate(cur)] if cur else None
    snapshot["brightness_menu"] = level
    snapshot["brightness_exact"] = exact

    # максимумы за сеанс работы дашборда
    if SESSION["serial"] != snapshot["serial"]:
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
            msg = ("Проектор не отвечает по ADB (переподключаюсь…). Если не проходит — выключи и включи "
                   "беспроводную отладку на проекторе.") if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
            with LOCK:
                LATEST = {"error": msg, "ts": int(time.time())}
                HISTORY.clear()
        time.sleep(5)


HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>JMGO — монитор</title>
<style>
:root{color-scheme:dark;--bg:#0f1115;--panel:#191c22;--panel2:#20242c;--line:#303641;--text:#f2f4f7;--muted:#a3aab6;--green:#52d273;--yellow:#f2bd55;--red:#f16b68;--blue:#72aaff;--orange:#ff9f43}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Arial,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px 22px 34px}h1{font-size:25px;line-height:1.15;margin:0;font-weight:700}h2{font-size:16px;margin:0 0 10px}.muted{color:var(--muted)}
.head{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;margin-bottom:14px}.device{margin-top:6px;color:var(--muted)}.status{white-space:nowrap;color:var(--green);font-size:13px;padding-top:5px}.status.demo{color:var(--yellow)}.status.off{color:var(--red)}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:currentColor;margin-right:7px}
.summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.metric{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:13px 14px;min-width:0;min-height:106px}.metric .label{color:var(--muted);font-size:12px;margin-bottom:7px}.metric .value{font-size:25px;font-weight:650}.metric .big{font-size:42px;line-height:1;font-weight:700;letter-spacing:-.04em}.metric .big span{font-size:22px;color:var(--muted);letter-spacing:0}.metric .unit{font-size:13px;color:var(--muted);font-weight:400}.metric .hint,.metric-sub{font-size:11px;color:var(--muted);margin-top:5px}
.big.warn,.value.warn{color:var(--yellow)}.big.hot,.value.hot{color:var(--red)}.big.ok{color:var(--text)}
.version-line,.zone-line{display:flex;justify-content:space-between;gap:8px;font-size:12px;margin-top:3px}.version-line span,.zone-line span{color:var(--muted)}.version-line b,.zone-line b{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ultra-extra{display:none;align-self:start;min-height:0;padding:11px 12px}.ultra-extra.visible{display:block}
.fan-pair{display:flex;gap:8px}.fan-item{flex:1;min-width:0;text-align:center}.fan-value{font-size:25px;font-weight:650;font-variant-numeric:tabular-nums}.fan-name{font-size:11px;color:var(--muted);margin-top:2px;white-space:nowrap}
.laser-row{display:flex;gap:6px;margin-top:8px}.laser-chip{flex:1;background:var(--panel2);border-radius:7px;padding:4px 0;text-align:center;font-size:13px;font-variant-numeric:tabular-nums}.laser-chip small{display:block;color:var(--muted);font-size:10px}
.bar{height:6px;background:var(--panel2);border-radius:5px;overflow:hidden;margin-top:9px}.bar i{display:block;height:100%;background:var(--green);border-radius:5px;width:0;transition:width .4s,background .4s}.bar i.warn{background:var(--yellow)}.bar i.hot{background:var(--red)}
#output{white-space:nowrap;font-size:20px}#output .unit{font-size:11px}
.section{margin-top:15px}.chart{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px 14px}.chart-title{font-weight:600;margin-bottom:2px}.chart-sub{font-size:11px;color:var(--muted);margin-bottom:7px}.chart canvas{display:block;width:100%;height:190px}.chart-legend{display:flex;justify-content:center;gap:15px;flex-wrap:wrap;color:var(--muted);font-size:11px;margin-top:2px}.legend-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.protection{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.threshold{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 13px}.threshold b{display:block;font-size:20px;margin-bottom:3px}.threshold span{display:block;color:var(--muted);font-size:12px}.threshold.hot b{color:var(--yellow)}.threshold.critical b{color:var(--red)}.threshold.info b{color:var(--blue)}
.note{color:var(--muted);font-size:12px;margin-top:10px;line-height:1.5}.error{background:#321b1e;border:1px solid #7a373d;color:#ffb4b1;padding:13px 15px;border-radius:12px;margin-bottom:16px}.alert{background:#2e2716;border:1px solid #6b5a2a;color:#ffe2a3;padding:11px 14px;border-radius:12px;margin-bottom:12px;font-size:13px}
@media(max-width:720px){main{padding:22px 14px 32px}.head{display:block}.status{padding-top:12px}.protection{grid-template-columns:1fr 1fr}.summary{grid-template-columns:1fr 1fr;gap:9px}.metric{padding:14px}.metric .value{font-size:23px}.chart canvas{height:220px}}
.hdr-right{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.btn{background:var(--panel2);color:var(--text);border:2px solid var(--line);border-radius:10px;padding:8px 14px;font:inherit;font-size:14px;cursor:pointer}.btn:focus{outline:none;border-color:var(--blue);background:#24304a}.btn.on{border-color:var(--green)}html,body{height:100%}body{font-size:1.75vh;overflow:hidden}
main{max-width:none;height:100vh;padding:1.6vh 1.8vw 1.4vh;display:flex;flex-direction:column;gap:1.1vh}
.head{margin:0;align-items:center}.ttl{display:flex;align-items:baseline;gap:1.2vw}h1{font-size:3vh}.device{margin:0;font-size:1.5vh}
.btn{padding:.6vh 1vw;font-size:1.6vh}.status{font-size:1.5vh;padding:0}
.error,.alert{margin:0;padding:.8vh 1vw;font-size:1.5vh}
.metric{min-height:0;padding:1.1vh 1vw;border-radius:1vh}.metric .label{font-size:1.45vh;margin-bottom:.4vh}
.metric .big{font-size:5vh}.metric .value{font-size:2.7vh}.metric .hint,.metric-sub{font-size:1.35vh;margin-top:.4vh}
.fan-value{font-size:3.6vh}.fan-name{font-size:1.25vh}.laser-row{margin-top:.6vh}.laser-chip{font-size:1.5vh;padding:.3vh 0}.laser-chip small{font-size:1.1vh}
.bar{margin-top:.7vh;height:.6vh}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:.8vw}
.charts{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);grid-template-rows:minmax(0,1fr);gap:.8vw}
.chart{display:flex;flex-direction:column;min-height:0;padding:1vh 1vw;border-radius:1vh}
.chart-title{font-size:1.6vh;margin-bottom:.4vh}.chart-title .chart-sub{display:inline;font-weight:400;margin:0}
.chart canvas{flex:1;min-height:0;height:auto}.chart-legend{font-size:1.3vh;margin-top:.3vh}
.infos{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:.8vw}.infos .metric{padding:.9vh .9vw}
#output{font-size:2.4vh}.zone-line,.version-line{font-size:1.4vh;margin-top:.2vh}
.prot{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:.8vw;align-items:center;flex:0 0 auto}.protection{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.6vw}
.threshold{padding:.7vh .8vw;border-radius:1vh}.threshold b{font-size:2vh;margin:0}.threshold span{font-size:1.25vh;line-height:1.25}
.note{margin:0;font-size:1.3vh;line-height:1.35;align-self:center;flex:1}
.ultra-extra{padding:.9vh .9vw}
#output .unit{display:block;font-size:1.4vh;white-space:nowrap}
@media (max-height:700px),(max-aspect-ratio:4/3){
html,body{height:auto}body{overflow:auto;font-size:14px}main{height:auto;padding:16px 18px;gap:12px}
h1{font-size:24px}.device,.status{font-size:13px}.btn{font-size:14px}
.metric .label{font-size:12px}.metric .big{font-size:34px}.metric .value{font-size:20px}.metric .hint,.metric-sub,.note,.chart-legend,.threshold span{font-size:12px}
.fan-value{font-size:26px}.laser-chip{font-size:13px}.chart-title{font-size:14px}.threshold b{font-size:18px}#output{font-size:18px}#output .unit{font-size:12px}.zone-line,.version-line{font-size:12px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}.infos{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}
.charts,.prot{grid-template-columns:1fr}.chart canvas{flex:none;height:260px}.protection{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}}
main{max-width:none}
</style>
</head>
<body><main>
<div class="head"><div class="ttl"><h1 id="name">JMGO</h1><span class="device" id="device">Мониторинг проектора</span></div><div class="hdr-right"><div class="status" id="status"><i class="dot"></i>подключение…</div></div></div>
<div id="error"></div><div id="alert"></div>
<section class="kpis">
  <div class="metric"><div class="label">Процессор, °C</div><div class="big" id="temp">—</div><div class="metric-sub" id="tempExplain">—</div></div>
  <div class="metric"><div class="label">Матрица DMD, °C</div><div class="big" id="dmd">—</div><div class="metric-sub" id="dmdSub">датчик у матрицы</div><div class="bar"><i id="dmdBar"></i></div></div>
  <div class="metric"><div class="label">Лазеры, °C (макс.)</div><div class="big" id="laserMax">—</div><div class="laser-row" id="laserRow"></div><div class="metric-sub" id="laserSub">—</div></div>
  <div class="metric"><div class="label">Вентиляторы, об/мин</div><div class="fan-pair"><div class="fan-item"><div class="fan-value" id="fan1">—</div><div class="fan-name">вент. 1</div></div><div class="fan-item"><div class="fan-value" id="fan2">—</div><div class="fan-name">вент. 2</div></div></div><div class="metric-sub" id="fanSub">—</div></div>
  <div class="metric"><div class="label">Яркость (мощность лазера)</div><div class="big" id="brightness">—</div><div class="metric-sub" id="currentSub">ток R/G/B</div></div>
</section>
<section class="charts">
  <div class="chart"><div class="chart-title">Матрица и лазеры <span class="chart-sub">· последний час · пунктир — пороги защиты</span></div><canvas id="thermalChart"></canvas><div class="chart-legend" id="thermalLegend"></div></div>
  <div class="chart"><div class="chart-title">Процессор и вентиляторы <span class="chart-sub">· °C слева, об/мин справа</span></div><canvas id="combinedChart"></canvas>
  <div class="chart-legend"><span><i class="legend-dot" style="background:#52d273"></i>процессор</span><span><i class="legend-dot" style="background:#72aaff"></i>вент. 1</span><span><i class="legend-dot" style="background:#d49aff"></i>вент. 2</span></div></div>
</section>
<section class="infos">
  <div class="metric"><div class="label">Процессор</div><div class="value" id="freq">—</div><div class="hint" id="cpuHint">Текущая частота</div></div>
  <div class="metric"><div class="label">Память</div><div class="value" id="memory">—</div><div class="hint" id="memoryHint">—</div></div>
  <div class="metric"><div class="label">Вывод изображения</div><div class="value" id="output">—</div><div class="hint" id="outputHint">Разрешение · частота</div></div>
  <div class="metric"><div class="label">Накопитель</div><div class="value" id="storage">—</div><div class="hint">свободно</div></div>
  <div class="metric"><div class="label">Максимум за сеанс</div><div class="zone-line"><span>DMD</span><b id="maxDmd">—</b></div><div class="zone-line"><span>Лазеры</span><b id="maxLaser">—</b></div><div class="zone-line"><span>Процессор</span><b id="maxCpu">—</b></div><div class="hint" id="maxSince" style="display:none">—</div></div>
  <div class="metric" id="dlpCard"><div class="label">Версии</div><div class="version-line"><span>Дашборд</span><b id="appVersion">—</b></div><div class="version-line"><span>Прошивка</span><b id="firmwareVersion">—</b></div><div class="version-line"><span>DLP</span><b id="dlpVersion">—</b></div><div class="version-line"><span>Модель</span><b id="modelCode">—</b></div></div>
  <div class="metric ultra-extra" id="extraZoneCard"><div class="label">Доп. датчики</div><div class="zone-line"><span>GPU</span><b id="gpuTemp">—</b></div><div class="zone-line"><span>APU</span><b id="apuTemp">—</b></div></div>
</section>
<section class="prot"><div class="protection" id="protection"></div><div class="note" id="protNote"></div></section>
</main>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function value(v,unit=''){return v==null||v===''?'—':`${esc(v)}${unit?`<span class="unit"> ${unit}</span>`:''}`}
function set(id,v){$(id).innerHTML=v}
const fmt=v=>v==null?'—':(Math.round(v*10)/10).toFixed(1);
// Датчики стоят на лазерных модулях, а не на цветах: каждый модуль светит R+G+B,
// поэтому цвета линий нейтральные, без привязки к красному/зелёному/синему.
const LCOL=['#e9edf3','#5fd3c6','#b8a1ff','#c9a46b'];
function margCls(m){return m==null?'':m<=2?'hot':m<=6?'warn':'ok'}
function status(d){
  const s=$('status'); s.className=d.demo?'status demo':'status'; s.innerHTML=d.demo?'<i class="dot"></i>демо':'<i class="dot"></i>подключен';
  $('error').innerHTML='';
}
function setExtraZones(zones){
 const gpu=zones?.find(z=>/gpu/i.test(z.name)),apu=zones?.find(z=>/apu|npu/i.test(z.name));
 set('gpuTemp',gpu?`${gpu.temp} °C`:'—');set('apuTemp',apu?`${apu.temp} °C`:'—');
 return Boolean(gpu||apu);
}
function resolutionLabel(w,h){
 if(w===3840&&h===2160)return '4K UHD';if(w===2560&&h===1440)return '2K QHD';if(w===1920&&h===1080)return 'Full HD';if(w===1280&&h===720)return 'HD';return '';
}
function canvasCtx(id){const c=$(id),rect=c.getBoundingClientRect(),dpr=devicePixelRatio||1,W=Math.max(420,Math.floor(rect.width*dpr)),H=Math.max(120,Math.floor((rect.height||190)*dpr));c.width=W;c.height=H;const x=c.getContext('2d');x.clearRect(0,0,W,H);x.font=`${Math.max(11,Math.round(innerHeight*0.013))*dpr}px -apple-system,Arial`;return {x,W,H,dpr}}
function timeAxis(x,points,L,R,W,axisBottom,dpr){x.fillStyle='#a3aab6';x.textAlign='left';x.fillText(new Date(points[0].ts*1000).toLocaleTimeString('ru',{hour:'2-digit',minute:'2-digit'}),L,axisBottom+13*dpr);x.textAlign='right';x.fillText(new Date(points[points.length-1].ts*1000).toLocaleTimeString('ru',{hour:'2-digit',minute:'2-digit'}),W-R,axisBottom+13*dpr);x.textAlign='left'}
function drawCombined(points){
  const {x,W,H,dpr}=canvasCtx('combinedChart');
  if(points.length<2){x.fillStyle='#a3aab6';x.fillText('Собираю историю…',18*dpr,38*dpr);return}
  const L=58*dpr,R=58*dpr,T=14*dpr,B=20*dpr,pw=W-L-R,ph=H-T-B;
  const tempTicks=[25,50,75,100,125],fanTicks=[500,1000,1500,2000,2500],tempMax=125,fanMax=2500;
  x.strokeStyle='#303641';x.fillStyle='#a3aab6';x.lineWidth=1*dpr;
  tempTicks.forEach(tv=>{const y=T+(1-tv/tempMax)*ph;x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.textAlign='right';x.fillText(tv,L-8*dpr,y+4*dpr)});
  fanTicks.forEach(fv=>{const y=T+(1-fv/fanMax)*ph;x.textAlign='left';x.fillText(fv,W-R+8*dpr,y+4*dpr)});
  timeAxis(x,points,L,R,W,T+ph,dpr);
  const line=(key,color,scale)=>{x.beginPath();let started=false;points.forEach((p,i)=>{if(p[key]==null)return;const px=L+i/(points.length-1)*pw,py=T+(1-p[key]/scale)*ph;if(!started){x.moveTo(px,py);started=true}else x.lineTo(px,py)});x.strokeStyle=color;x.lineWidth=2.5*dpr;x.stroke()};
  line('temp','#52d273',tempMax);line('fan1','#72aaff',fanMax);line('fan2','#d49aff',fanMax);
}
function drawThermal(points,lim){
  const {x,W,H,dpr}=canvasCtx('thermalChart');
  const hasData=points.some(p=>p.dmd!=null||(p.l&&p.l.length));
  if(points.length<2||!hasData){x.fillStyle='#a3aab6';x.fillText(points.length<2?'Собираю историю…':'Нет данных от hippo_server (см. подсказку выше)',18*dpr,38*dpr);return}
  const L=46*dpr,R=150*dpr,T=12*dpr,B=20*dpr,pw=W-L-R,ph=H-T-B;
  let vals=[];points.forEach(p=>{if(p.dmd!=null)vals.push(p.dmd);(p.l||[]).forEach(v=>vals.push(v))});
  const top=Math.max(lim.laser_shutdown||57,lim.dmd_shutdown||55,...vals)+3, bot=Math.max(0,Math.floor((Math.min(...vals)-5)/5)*5);
  const Y=v=>T+(1-(v-bot)/(top-bot))*ph;
  x.strokeStyle='#303641';x.fillStyle='#a3aab6';x.lineWidth=1*dpr;
  for(let tv=Math.ceil(bot/10)*10;tv<=top;tv+=10){const y=Y(tv);x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.textAlign='right';x.fillText(tv,L-8*dpr,y+4*dpr)}
  timeAxis(x,points,L,R,W,T+ph,dpr);
  const lbl=[];
  const ref=(v,color,label)=>{if(v==null)return;const y=Y(v);x.save();x.setLineDash([6*dpr,5*dpr]);x.strokeStyle=color;x.lineWidth=1.5*dpr;x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.restore();lbl.push({y,color,label})};
  ref(lim.laser_shutdown,'#f16b68',`лазеры: откл. ${lim.laser_shutdown}°`);
  ref(lim.laser_derate,'#ff9f43',`лазеры: −яркость ${lim.laser_derate}°`);
  ref(lim.dmd_shutdown,'#f2bd55',`DMD: откл. ${lim.dmd_shutdown}°`);
  if(lim.pid_targets&&lim.pid_targets.length)ref(lim.pid_targets[0],'#8b93a1',`цель вент. ${lim.pid_targets[0]}°`);
  // подписи порогов разводим по вертикали, чтобы не налезали друг на друга
  lbl.sort((a,b)=>a.y-b.y);const gap=15*dpr;let prev=-1e9;
  lbl.forEach(o=>{o.ty=Math.max(o.y+4*dpr,prev+gap);prev=o.ty});
  const over=prev-(T+ph+4*dpr);if(over>0)lbl.forEach(o=>o.ty-=over);
  lbl.forEach(o=>{x.fillStyle=o.color;x.textAlign='left';x.fillText(o.label,W-R+6*dpr,o.ty);if(Math.abs(o.ty-4*dpr-o.y)>2*dpr){x.strokeStyle=o.color;x.lineWidth=1*dpr;x.beginPath();x.moveTo(W-R,o.y);x.lineTo(W-R+4*dpr,o.ty-4*dpr);x.stroke()}});
  const line=(get,color,w)=>{x.beginPath();let s=false;points.forEach((p,i)=>{const v=get(p);if(v==null)return;const px=L+i/(points.length-1)*pw,py=Y(v);if(!s){x.moveTo(px,py);s=true}else x.lineTo(px,py)});x.strokeStyle=color;x.lineWidth=w*dpr;x.stroke()};
  const n=Math.max(0,...points.map(p=>(p.l||[]).length));
  for(let i=0;i<n;i++)line(p=>(p.l||[])[i],LCOL[i],1.8);
  line(p=>p.dmd,'#f2bd55',2.8);
  let lg='<span><i class="legend-dot" style="background:#f2bd55"></i>DMD</span>';for(let i=0;i<n;i++)lg+=`<span><i class="legend-dot" style="background:${LCOL[i]}"></i>лазерный модуль ${i+1}</span>`;set('thermalLegend',lg);
}
function renderProtection(d){
  const l=d.limits||{},t=[];
  t.push(`<div class="threshold critical"><b>${l.dmd_shutdown??'—'} °C</b><span>DMD — отключение проектора</span></div>`);
  t.push(`<div class="threshold hot"><b>${l.laser_derate??'—'} °C</b><span>Лазеры — снижение яркости</span></div>`);
  t.push(`<div class="threshold critical"><b>${l.laser_shutdown??'—'} °C</b><span>Лазеры — отключение</span></div>`);
  const pt=(l.pid_targets||[]).filter((v,i)=>!(l.pid_fixed||[])[i]);t.push(`<div class="threshold info"><b>${pt.length?pt[0]+' °C':'—'}</b><span>Вентиляторы держат лазеры около этой температуры${pt.length>1?` (в ярких режимах — до ${pt[pt.length-1]} °C)`:''}</span></div>`);
  set('protection',t.join(''));
  set('protNote',`${l.from_firmware?'Пороги — из прошивки этого проектора.':'⚠ Пороги не нашлись в прошивке — показаны значения N5 Pro.'} ${l.dmd_source==='dlp'?'Температуру DMD отдаёт контроллер DLP (датчик в самой матрице), поэтому и порог выше.':'Датчик DMD стоит рядом с матрицей, сами зеркала горячее.'} Чтобы следить за охлаждением, сфотографируйте экран после 30–60 мин фильма и сравните через несколько месяцев в тех же условиях: +5 °C и больше или заметно выше обороты — пора обслуживать.`);
}
function render(r){
  const d0=r.latest;
  const demo=new URLSearchParams(location.search).get('demo')==='ultra';
  const d=demo?{...d0,demo:true,name:'JMGO N5S Ultra Max (демо)',model:'KD11CN',chip:'MediaTek MT9681',temp:72,brightness_menu:10,brightness_exact:true,laser_current:[1023,875,850],laser_current_pct:[100,86,83],fan1:1180,fan2:1215,freq:2200,cpu_usage:34,mem_total:4194304,mem_avail:1900000,storage_free_kb:90000000,output_width:3840,output_height:2160,output_hz:120,firmware_version:'1.1.31.25',dlp_version:'8.2.0',
    thermal:{fresh:true,age:2,dmd:43.5,lasers:[47.1,48.0,48.6,47.7],laser_max:48.6,pid_temp:48.6,fan_duty:[62,62],fan_mode:'PID',overlap_level:0,hippo_error:false},
    limits:{dmd_shutdown:71,dmd_source:'dlp',laser_derate:58,laser_shutdown:58.5,pid_targets:[48,52,50],pid_fixed:[false,false,true],source:'демо'},session_max:{dmd:44.1,laser:49.2,cpu:74}}:d0;
  if(d.error&&!d.demo){$('status').className='status off';$('status').innerHTML='<i class="dot"></i>нет связи';$('error').innerHTML=`<div class="error">${esc(d.error)}<br><span class="muted">Проверь, что ADB включён и проектор в той же Wi‑Fi сети. Если на компьютере включён VPN — он может перехватывать локальную сеть.</span></div>`;return}
  status(d);$('name').textContent=d.name||'JMGO';$('device').textContent=`обновлено ${new Date(d.ts*1000).toLocaleTimeString('ru')}${d.uptime?` · работает ${Math.floor(d.uptime/3600)} ч ${Math.floor(d.uptime%3600/60)} мин`:''}`;
  const th=d.thermal||{},lim=d.limits||{};
  // DMD
  const dm=th.dmd, dmMarg=dm!=null&&lim.dmd_shutdown?lim.dmd_shutdown-dm:null;
  set('dmd',dm==null?'—':fmt(dm));$('dmd').className='big '+margCls(dmMarg);
  set('dmdSub',dm==null?(th.age!=null?`нет свежих данных (${th.age} с)`:'жду строки от hippo_server…'):`до отключения ${fmt(dmMarg)} °C (порог ${lim.dmd_shutdown})`);
  const bar=$('dmdBar');if(dm!=null&&lim.dmd_shutdown){const p=Math.max(0,Math.min(100,(dm-20)/(lim.dmd_shutdown-20)*100));bar.style.width=p+'%';bar.className=margCls(dmMarg)==='ok'?'':margCls(dmMarg)}else bar.style.width='0';
  // лазеры
  const lm=th.laser_max, lMarg=lm!=null&&lim.laser_derate?lim.laser_derate-lm:null;
  set('laserMax',lm==null?'—':fmt(lm));$('laserMax').className='big '+margCls(lMarg);
  set('laserRow',(th.lasers||[]).map((v,i)=>`<div class="laser-chip" style="border-bottom:2px solid ${LCOL[i]}"><small>модуль ${i+1}</small>${fmt(v)}</div>`).join(''));
  set('laserSub',lm==null?'—':`до снижения яркости ${fmt(lMarg)} °C (порог ${lim.laser_derate})`);
  // яркость
  set('brightness',d.brightness_menu==null?'—':`${d.brightness_exact?'':'≈'}${d.brightness_menu}<span class="unit"> / 10</span>`);
  const pc=d.laser_current_pct;set('currentSub',pc?`ток лазеров: <b style="color:#ff6b6b">R ${pc[0]}%</b> · <b style="color:#5ee08a">G ${pc[1]}%</b> · <b style="color:#6fa8ff">B ${pc[2]}%</b>${d.brightness_exact?'':'<br>меняется на лету (динамика/AL)'}`:'ток лазеров недоступен');
  // вентиляторы
  set('fan1',d.fan1??'—');set('fan2',d.fan2??'—');
  const duty=th.fan_duty;const tgt=(lim.pid_targets||[])[th.overlap_level||0];
  set('fanSub',duty?`работают на ${Math.max(...duty)} % мощности${th.fan_mode==='PID'&&tgt!=null?` · автоматика держит лазеры около ${tgt} °C`:''}`:'мощность — нет данных');
  // процессор
  const t=d.temp, thr=d.trip_passive0||123;set('temp',value(t));$('temp').className='big '+(t>=100?'hot':t>=90?'warn':'');
  set('tempExplain',`${esc(d.chip||'')} · сброс частоты с ${thr}&nbsp;°C · на оптику не влияет`);
  set('freq',value(d.freq,'МГц'));set('cpuHint',d.cpu_usage>=0?`Загрузка ${d.cpu_usage}% · текущая частота`:'Текущая частота');
  const used=d.mem_total&&d.mem_avail?Math.round((d.mem_total-d.mem_avail)/d.mem_total*100):null;set('memory',used==null?'—':`${used}<span class="unit"> %</span>`);set('memoryHint',used==null?'':`${Math.round((d.mem_total-d.mem_avail)/1024)} МБ из ${Math.round(d.mem_total/1024)} МБ занято`);
  const freeGb=d.storage_free_kb==null?null:(d.storage_free_kb/1024/1024);set('storage',freeGb==null?'—':`${freeGb.toFixed(1)} ГБ`);
  set('output',d.output_width&&d.output_height?`${resolutionLabel(d.output_width,d.output_height)}<span class="unit"> ${d.output_width}×${d.output_height} · ${d.output_hz||'—'} Гц</span>`:'—');set('outputHint',d.input_rslt?(()=>{const m=/(\d+)\s*[xX×]\s*(\d+)[^@\d]*(?:@\s*([\d.]+))?/.exec(d.input_rslt);const hz=m&&m[3]?m[3]:d.input_fps;let z=hz?Math.round(+hz):0;if([23,29,47,59,119].includes(z))z++;const w=m?+m[1]:0,hh=m?+m[2]:0;const r=!m?esc(d.input_rslt):w>=7680?'8K':(w>=3840||hh>=2160)?'4K':(w>=2560||hh>=1440)?'2K':hh>=1080?'1080p':hh>=720?'720p':hh+'p';return 'HDMI-сигнал: '+r+(z?' · '+z+' Гц':'')+(d.input_vrr>0?' · VRR':'')})():'Разрешение · частота (вывод проектора)');
  // максимумы
  const mx=d.session_max||{};set('maxDmd',mx.dmd!=null?`${fmt(mx.dmd)} °C`:'—');set('maxLaser',mx.laser!=null?`${fmt(mx.laser)} °C`:'—');set('maxCpu',mx.cpu!=null?`${mx.cpu} °C`:'—');
  set('maxSince',d.session_start?`с ${new Date(d.session_start*1000).toLocaleTimeString('ru',{hour:'2-digit',minute:'2-digit'})} (запуск дашборда)`:'');
  const extraVisible=setExtraZones(d.extra_zones);$('extraZoneCard').classList.toggle('visible',extraVisible);$('dlpCard').classList.toggle('visible',true);
  set('appVersion',esc(d.app_version||'—'));set('firmwareVersion',esc(d.firmware_version||d.firmware||'—'));set('dlpVersion',esc([d.dlp_version,d.dlp_model].filter(Boolean).join(' · ')||'—'));set('modelCode',esc(d.model||'—'));
  // предупреждения
  const al=[];
  if(th.hippo_error)al.push('hippo_server сообщает ERROR:true — проверь проектор.');
  if(th.fan_status===0)al.push('fanStatus = 0: система считает, что вентилятор не работает.');
  if(dmMarg!=null&&dmMarg<=6)al.push(`DMD близко к порогу отключения: ${fmt(dm)} из ${lim.dmd_shutdown} °C.`);
  if(lMarg!=null&&lMarg<=6)al.push(`Лазеры близко к порогу снижения яркости: ${fmt(lm)} из ${lim.laser_derate} °C.`);
  if(d.fan1!=null&&d.fan2!=null&&Math.max(d.fan1,d.fan2)>300&&Math.abs(d.fan1-d.fan2)/Math.max(d.fan1,d.fan2)>0.25)al.push(`Вентиляторы крутятся заметно по-разному (${d.fan1} и ${d.fan2} об/мин).`);
  if(th.fresh===false&&th.age==null)al.push('Пока нет строк температуры от hippo_server — подожди 10–20 с. Если не появятся, на этой прошивке лог может быть отключён.');
  $('alert').innerHTML=al.length?`<div class="alert">${al.map(esc).join('<br>')}</div>`:'';
  renderProtection(d);
  drawThermal(r.history,lim);drawCombined(r.history);
}

let last=null;
async function tick(){try{last=await (await fetch('/api',{cache:'no-store'})).json();render(last)}catch(e){$('status').className='status off';$('status').innerHTML='<i class="dot"></i>ошибка страницы'}}
addEventListener('resize',()=>last&&render(last));tick();setInterval(tick,5000);
</script></body></html>'''


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        with LOCK:
            payload = {"latest": dict(LATEST), "history": list(HISTORY)}
        if self.path.startswith("/api"):
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
    ap = argparse.ArgumentParser(description=f"JMGO Temp {VERSION}: монитор температур JMGO по ADB (только чтение).")
    ap.add_argument("--version", action="version", version=f"JMGO Temp (jmgo_dash.py) {VERSION}")
    ap.add_argument("target", nargs="?", default="", help="IP:порт беспроводной отладки (необязательно)")
    ap.add_argument("--port", type=int, default=PORT, help="порт веб-страницы (по умолчанию 8777)")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")
    args = ap.parse_args()
    TARGET = args.target.strip()
    PORT = args.port
    print(f"JMGO Temp {VERSION} (браузерная версия)", flush=True)
    print(f"adb: {ADB}", flush=True)
    threading.Thread(target=poll, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    try:
        server = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"Порт {PORT} занят — возможно, дашборд уже запущен. Открой http://localhost:{PORT} "
                 f"или запусти с другим портом: --port 8778")
    with server:
        url = f"http://localhost:{PORT}"
        print(f"Дашборд: {url}   (Ctrl+C — стоп)", flush=True)
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            WATCHER.stop()
