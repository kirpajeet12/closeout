# Closeout for iPhone

A native iPhone app around the Closeout web app. The native part owns the icon, the launch
screen, camera / location / local-network permissions, printing and the "which server" step.
Every other screen is served by the Closeout server (`closeout/api.py` + `web/index.html`),
so the app and the browser always show the same product.

## What is in here

| Path | What it is |
|---|---|
| `Closeout/CloseoutApp.swift` | App entry, root switch (connect screen vs. the app), the palette |
| `Closeout/ServerSettings.swift` | The server address, saved on the phone; address normalising; private-network test |
| `Closeout/WebShell.swift` | The web view: self-signed HTTPS trust for private addresses only, links off the server open in Safari, `window.print()` → iOS print sheet, alerts, two-finger press-and-hold to change the address |
| `Closeout/Screens.swift` | Connect screen, splash, "Can't reach Closeout" screen |
| `Closeout/Info.plist` | Permission texts and the local-networking allowance |
| `Closeout/PrivacyInfo.xcprivacy` | Privacy manifest (photos and precise location, app functionality only, no tracking) |
| `scripts/render-app-icon.swift` | Draws the icon (`swift ios/scripts/render-app-icon.swift`) |
| `scripts/testflight.sh` | `check` (simulator build), `archive`, `upload` |
| `Release/ExportOptions-TestFlight.plist` | Upload settings: internal TestFlight only |

Bundle id `com.closeout.field`, version 0.1.0, iOS 17 and up, iPhone only, portrait.

## Run it on the Mac (simulator)

```bash
bash ios/scripts/testflight.sh check
```

Or open `ios/Closeout.xcodeproj` in Xcode, pick an iPhone simulator at the top, press ▶.
On first launch the app asks for the server address. With the server running on the Mac
(`.venv/bin/python -m uvicorn closeout.api:app --host 0.0.0.0 --port 8765`) the simulator
can use `http://127.0.0.1:8765`.

## Run it on a real phone

The phone must be on the same Wi-Fi as the Mac. Type the address the Mac's server is reachable
at, for example `https://<mac-ip>:8443` (the HTTPS copy, needed for location) or
`http://<mac-ip>:8765`. The self-signed certificate in `data/tls/` is accepted automatically,
but only for private-network addresses; a public server must have a real certificate.

To change the address later: press and hold anywhere with two fingers.

## TestFlight

```bash
bash ios/scripts/testflight.sh archive
bash ios/scripts/testflight.sh upload
```

`archive` signs with the Apple team signed in to Xcode and registers the app id if needed.
`upload` sends the build to App Store Connect for internal TestFlight testing; it never
publishes to the App Store. Before the first upload an app record must exist in App Store
Connect (name, bundle id `com.closeout.field`, SKU); that is done once on appstoreconnect.apple.com.
Build folders and archives are ignored by git.

## Limits

- It is a shell: no server, no app. Offline work is a later feature of the web app itself.
- App Store review would ask for more than a web view (guideline 4.2); TestFlight does not.
