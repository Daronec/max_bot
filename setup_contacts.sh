#!/bin/zsh
set -eu
cd "$(dirname "$0")"
mkdir -p 'helpers/MAX Bot Contacts.app/Contents/MacOS'
cp helpers/contacts-info.plist 'helpers/MAX Bot Contacts.app/Contents/Info.plist'
/usr/bin/swiftc -module-cache-path /tmp/max-bot-swift-cache -target arm64-apple-macosx14.0 helpers/contacts.swift -o 'helpers/MAX Bot Contacts.app/Contents/MacOS/contacts' -framework Contacts -framework AppKit
/usr/bin/codesign --force --sign - --identifier local.maxbot.contacts 'helpers/MAX Bot Contacts.app'
