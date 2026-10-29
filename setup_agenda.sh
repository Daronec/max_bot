#!/bin/zsh
set -eu
cd "$(dirname "$0")"
/usr/bin/swiftc -module-cache-path /tmp/max-bot-swift-cache -target arm64-apple-macosx14.0 \
  helpers/agenda.swift -o helpers/agenda -framework EventKit \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker helpers/agenda-info.plist
/usr/bin/codesign --force --sign - --identifier local.maxbot.agenda helpers/agenda
