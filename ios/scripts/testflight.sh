#!/bin/bash
# Closeout for iPhone: archive, then upload to TestFlight (internal testing only).
# Uses the Apple account signed in to Xcode. No passwords, tokens or keys belong here.
set -euo pipefail

IOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ACTION="${1:-help}"
ARCHIVE="${2:-$IOS_DIR/artifacts/testflight/Closeout.xcarchive}"
DERIVED="${CLOSEOUT_DERIVED_DATA_PATH:-$IOS_DIR/build-testflight}"

case "$ACTION" in
  check)
    # Compiles for the simulator. No signing, no Apple account, nothing leaves the Mac.
    xcodebuild -project "$IOS_DIR/Closeout.xcodeproj" -scheme Closeout -configuration Debug \
      -destination 'generic/platform=iOS Simulator' -derivedDataPath "$IOS_DIR/build-check" \
      CODE_SIGNING_ALLOWED=NO build | tail -3
    ;;
  archive)
    if [[ -e "$ARCHIVE" ]]; then
      echo "Archive already exists at $ARCHIVE. Pass a new path; old archives are kept." >&2
      exit 2
    fi
    # -allowProvisioningUpdates lets Xcode register the app id and make the signing profile.
    xcodebuild -project "$IOS_DIR/Closeout.xcodeproj" -scheme Closeout -configuration Release \
      -destination 'generic/platform=iOS' -archivePath "$ARCHIVE" -derivedDataPath "$DERIVED" \
      -allowProvisioningUpdates DEVELOPMENT_TEAM=UX593LWV92 archive
    ;;
  upload)
    if [[ ! -d "$ARCHIVE/Products/Applications/Closeout.app" ]]; then
      echo "No archived app at $ARCHIVE" >&2
      exit 2
    fi
    codesign --verify --deep --strict "$ARCHIVE/Products/Applications/Closeout.app"
    echo "Uploading to App Store Connect for internal TestFlight testing only."
    xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportPath "$ARCHIVE.upload" \
      -exportOptionsPlist "$IOS_DIR/Release/ExportOptions-TestFlight.plist" -allowProvisioningUpdates
    ;;
  *)
    echo 'Usage: bash ios/scripts/testflight.sh check'
    echo '       bash ios/scripts/testflight.sh archive [new-archive-path]'
    echo '       bash ios/scripts/testflight.sh upload  [existing-archive-path]'
    echo 'check   builds for the simulator only.'
    echo 'archive signs a device build with the Apple team signed in to Xcode.'
    echo 'upload  sends that archive to TestFlight; it never publishes to the App Store.'
    ;;
esac
