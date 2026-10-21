#!/bin/bash
# Сборка JMGO Temp без Gradle: aapt2 + javac + d8 + apksigner
set -euo pipefail
cd "$(dirname "$0")"
SDK="$HOME/Library/Android/sdk"
BT="$SDK/build-tools/35.0.0"
PLAT="$SDK/platforms/android-34/android.jar"
rm -rf build && mkdir -p build/res build/classes build/dex
"$BT/aapt2" compile --dir res -o build/res/res.zip
"$BT/aapt2" link -o build/base.apk -I "$PLAT" --manifest AndroidManifest.xml \
  --min-sdk-version 28 --target-sdk-version 28 -A assets build/res/res.zip --java build/gen
javac --release 11 -nowarn -cp "$PLAT" -d build/classes $(find src build/gen -name '*.java')
"$BT/d8" --min-api 28 --lib "$PLAT" --output build/dex $(find build/classes -name '*.class')
cp build/base.apk build/unsigned.apk
(cd build/dex && zip -q -j ../unsigned.apk classes.dex)
"$BT/zipalign" -f -p 4 build/unsigned.apk build/aligned.apk
KS="release.jks"
"$BT/apksigner" sign --ks "$KS" --ks-pass "pass:${JMGO_KS_PASS:?set JMGO_KS_PASS}" --key-pass "pass:${JMGO_KS_PASS}" --ks-key-alias jmgotemp \
  --out build/JMGO-Temp.apk build/aligned.apk
"$BT/apksigner" verify build/JMGO-Temp.apk && ls -la build/JMGO-Temp.apk
