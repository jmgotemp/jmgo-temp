# JMGO Temp

Temperature monitor for JMGO laser projectors (N5 / N5S / IRIS / N3 series).
Мониторинг температур лазерных проекторов JMGO — [по-русски ниже](#по-русски).

**Download:** [latest release](../../releases/latest) — `JMGO-Temp-<version>.apk` for the projector and
`JMGO-Temp-web-<version>.zip` for a computer.

## What it shows
- DMD, laser modules, lens and CPU temperatures with the firmware's own protection thresholds
  (read from the projector's firmware, per model);
- fan speed (rpm) and duty, the temperature the fans hold;
- brightness level, aperture (N5S Ultra Max / IRIS), AL 2.0;
- 1-hour charts, a compact overlay on top of the film and overheat warnings (APK).

## Two ways to run
- **APK** — install on the projector, open «JMGO Temp». Full-screen dashboard + compact overlay.
  Since 1.5 the app can optionally change the laser temperature target the fans hold
  (resets on reboot unless «Keep after reboot» is on). Since 1.6 the app also checks GitHub for a new version
  (once a day when the screen opens, or by the button) and can download and install it on request;
  it sends nothing about you or the projector. Everything else is read-only.
- **Browser version** — `jmgo_dash.py` (Python 3, no extra packages) on a computer in the same network,
  over wireless ADB: `python3 jmgo_dash.py <projector-IP>:<port>` → http://localhost:8777.
  Windows: `jmgo_dash_win.bat`. Read-only.

Tested live on JMGO N5 Pro (K981CN). Other models are supported through their firmware configs;
see [CHANGELOG.md](CHANGELOG.md) for what was checked where.

## Building the APK
No Gradle: Android SDK build-tools 35.0.0, platform android-34, JDK 11+.

```bash
cd app
keytool -genkeypair -keystore release.jks -alias jmgotemp -keyalg RSA -validity 10000   # your own key
JMGO_KS_PASS=<your password> bash build.sh      # → build/JMGO-Temp.apk
```
Official releases are signed with the author's key; a build signed with another key won't install over them.

## Layout
- `app/` — Android app (Java, WebView UI in `assets/dashboard.html`).
- `web/` — browser version; the page HTML is the same as the app's.
- `probe/` — the small read-only helper the browser version runs on the projector (source of `PROBE_DEX_B64`).

## Support
The app is free. If you want to say thanks: [boosty.to/jmgotemp](https://boosty.to/jmgotemp/donate)
(accepts cards from outside Russia too).

## Disclaimer
Unofficial project, not affiliated with or endorsed by JMGO. JMGO is a trademark of its owner.
The app does not modify the firmware: it reads data from the projector's own services and, only if you
choose, sets the fan temperature target, which the firmware resets on reboot. Provided without warranty
(see [LICENSE](LICENSE)) — use at your own risk.

## License
[GPL-3.0](LICENSE).

---

## По-русски

Приложение для проекторов JMGO: температуры DMD, лазеров, объектива и процессора с порогами защиты
из прошивки этого проектора, обороты вентиляторов, яркость и диафрагма, графики за час,
компактное окно поверх фильма и предупреждения о перегреве.

- **APK** — ставится на проектор. С версии 1.5 может по желанию менять температуру лазеров,
  которую держат вентиляторы (сбрасывается при перезагрузке, если не включено «Сохранять после
  перезагрузки»). С версии 1.6 приложение раз в сутки при открытии (или по кнопке) спрашивает у GitHub, не вышла
  ли новая версия, и по вашей кнопке скачивает и ставит ее; ничего о вас и проекторе не отправляет.
  Остальное только читается.
- **Браузерная версия** — `jmgo_dash.py` на компьютере в той же сети, по беспроводному ADB:
  `python3 jmgo_dash.py IP:порт`, на Windows — `jmgo_dash_win.bat`. Только чтение.

Скачать — в [Releases](../../releases/latest). История изменений — [CHANGELOG.md](CHANGELOG.md).
Поддержать: [boosty.to/jmgotemp](https://boosty.to/jmgotemp/donate).

Неофициальный проект, с JMGO не связан. Прошивку не меняет: читает данные у служб самого проектора и,
только если вы это выберете, ставит температуру для вентиляторов (прошивка сбрасывает ее при перезагрузке).
Без гарантий — используете на свой риск.
