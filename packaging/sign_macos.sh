#!/usr/bin/env bash
# Sign, notarise and staple the macOS app bundle (Developer ID, hardened runtime).
#
# Placeholder hook: a no-op (exit 0, with a message) unless MACOS_SIGN_IDENTITY is set.
# Notarisation runs only when its credentials are set as well; without them the app is signed
# but not notarised (Gatekeeper then still warns on other Macs).
#
# Environment (CI: repository secrets):
#   MACOS_SIGN_IDENTITY        "Developer ID Application: Name (TEAMID)"
#   MACOS_CERT_P12_BASE64      optional: the certificate + key as base64 .p12 (CI imports it into
#   MACOS_CERT_PASSWORD          a temporary keychain); omit when the identity is in your keychain
#   notarytool credentials, either an App Store Connect API key:
#   NOTARY_KEY_ID, NOTARY_ISSUER_ID, NOTARY_KEY_P8_BASE64
#   or an Apple ID with an app-specific password:
#   APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD
#
# Usage: packaging/sign_macos.sh [packaging/build/dist/mlaser.app]
set -euo pipefail

APP="${1:-packaging/build/dist/mlaser.app}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENTITLEMENTS="$HERE/entitlements.plist"

if [[ -z "${MACOS_SIGN_IDENTITY:-}" ]]; then
  echo "sign_macos: signing skipped (MACOS_SIGN_IDENTITY not set). The app is unsigned (ad-hoc)."
  exit 0
fi
[[ -d "$APP" ]] || { echo "sign_macos: $APP not found" >&2; exit 1; }

cleanup() { [[ -n "${KEYCHAIN:-}" ]] && security delete-keychain "$KEYCHAIN" 2>/dev/null || true; rm -rf "${TMPD:-}"; }
trap cleanup EXIT
TMPD="$(mktemp -d)"

if [[ -n "${MACOS_CERT_P12_BASE64:-}" ]]; then
  KEYCHAIN="$TMPD/mlaser-signing.keychain-db"
  KCPASS="$(uuidgen)"
  echo "$MACOS_CERT_P12_BASE64" | base64 --decode > "$TMPD/cert.p12"
  security create-keychain -p "$KCPASS" "$KEYCHAIN"
  security set-keychain-settings -lut 3600 "$KEYCHAIN"
  security unlock-keychain -p "$KCPASS" "$KEYCHAIN"
  security import "$TMPD/cert.p12" -k "$KEYCHAIN" -P "${MACOS_CERT_PASSWORD:-}" -T /usr/bin/codesign
  security set-key-partition-list -S apple-tool:,apple: -s -k "$KCPASS" "$KEYCHAIN" >/dev/null
  security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')
fi

sign() {
  codesign --force --timestamp --options runtime --entitlements "$ENTITLEMENTS" \
           --sign "$MACOS_SIGN_IDENTITY" "$@"
}

echo "sign_macos: signing nested code in $APP (inside out)"
# every Mach-O file (dylibs, .so extension modules, Qt framework binaries, helper executables)
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then sign "$f"; fi
done < <(find "$APP/Contents" -type f \( -perm -u+x -o -name "*.dylib" -o -name "*.so" \) -print0)
# frameworks as a whole (Qt*.framework), deepest first
while IFS= read -r -d '' fw; do sign "$fw"; done \
  < <(find "$APP/Contents" -type d -name "*.framework" -print0 | sort -rz)
sign "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

if [[ -n "${NOTARY_KEY_ID:-}" && -n "${NOTARY_ISSUER_ID:-}" && -n "${NOTARY_KEY_P8_BASE64:-}" ]]; then
  echo "$NOTARY_KEY_P8_BASE64" | base64 --decode > "$TMPD/AuthKey.p8"
  NOTARY_ARGS=(--key "$TMPD/AuthKey.p8" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER_ID")
elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
  NOTARY_ARGS=(--apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD")
else
  echo "sign_macos: signed; notarisation skipped (no notarytool credentials)."
  exit 0
fi

echo "sign_macos: notarising"
ditto -c -k --keepParent "$APP" "$TMPD/mlaser-notarize.zip"
xcrun notarytool submit "$TMPD/mlaser-notarize.zip" "${NOTARY_ARGS[@]}" --wait --timeout 30m
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose=2 "$APP" || true
echo "sign_macos: done"
