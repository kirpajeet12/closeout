import SwiftUI

// Closeout for iPhone. A native shell around the Closeout web app: it owns the icon, the
// launch, camera / location permissions, printing and the "which server" step; the screens
// themselves are served by the Closeout server (closeout/api.py + web/index.html).
@main
struct CloseoutApp: App {
    @StateObject private var settings = ServerSettings()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(settings)
                .tint(Color.ink)
                .preferredColorScheme(.light)
        }
    }
}

struct RootView: View {
    @EnvironmentObject private var settings: ServerSettings

    var body: some View {
        if settings.isConfigured {
            ShellView()
        } else {
            ConnectScreen(canCancel: false) {}
        }
    }
}

// The web app's palette (web/index.html :root), so native screens match the pages.
extension Color {
    static let paper = Color(red: 0.969, green: 0.957, blue: 0.929)   // #f7f4ed
    static let card = Color.white
    static let ink = Color(red: 0.071, green: 0.075, blue: 0.078)     // #121314
    static let ink2 = Color(red: 0.227, green: 0.239, blue: 0.259)    // #3a3d42
    static let mute = Color(red: 0.455, green: 0.471, blue: 0.498)    // #74787f
    static let line = Color(red: 0.902, green: 0.882, blue: 0.839)    // #e6e1d6
    static let hot = Color(red: 0.910, green: 0.349, blue: 0.047)     // #e8590c
}
