import SwiftUI

/// The page, plus the two native moments around it: first launch and "server not reachable".
struct ShellView: View {
    @EnvironmentObject private var settings: ServerSettings
    @StateObject private var state = ShellState()
    @State private var reloadToken = 0
    @State private var editing = false

    var body: some View {
        ZStack {
            Color.paper.ignoresSafeArea()
            if let url = settings.url {
                WebShell(url: url, reloadToken: reloadToken, state: state, onSettings: { editing = true })
                if let failure = state.failure {
                    UnreachableView(address: settings.urlString, detail: failure,
                                    retry: { state.failure = nil; reloadToken += 1 },
                                    change: { editing = true })
                        .transition(.opacity)
                } else if !state.everLoaded {
                    SplashView()
                        .transition(.opacity)
                }
            }
        }
        .animation(.easeInOut(duration: 0.25), value: state.failure)
        .animation(.easeInOut(duration: 0.25), value: state.everLoaded)
        .fullScreenCover(isPresented: $editing) {
            ConnectScreen(canCancel: true) {
                editing = false
                state.failure = nil
                reloadToken += 1
            }
            .environmentObject(settings)
        }
    }
}

struct Wordmark: View {
    var body: some View {
        Text("CLOSEOUT")
            .font(.system(size: 12, weight: .heavy))
            .tracking(3)
            .foregroundStyle(Color.ink)
    }
}

/// Shown until the first page has finished loading.
struct SplashView: View {
    var body: some View {
        ZStack {
            Color.paper.ignoresSafeArea()
            VStack(spacing: 18) {
                Wordmark()
                ProgressView().tint(Color.mute)
            }
        }
    }
}

/// First launch, and later via a two-finger press and hold.
struct ConnectScreen: View {
    @EnvironmentObject private var settings: ServerSettings
    var canCancel: Bool
    var onDone: () -> Void

    @State private var draft = ""
    @FocusState private var focused: Bool

    private var target: URL? { ServerSettings.normalise(draft) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Wordmark()
                Spacer()
                if canCancel {
                    Button("Cancel", action: onDone).foregroundStyle(Color.mute)
                }
            }
            .padding(.top, 10)

            Spacer().frame(height: 64)

            Text("Where does\nCloseout run?")
                .font(.system(size: 36, weight: .bold))
                .tracking(-0.6)
                .lineSpacing(2)
                .foregroundStyle(Color.ink)
                .fixedSize(horizontal: false, vertical: true)

            Text("The address the server prints when it starts. On the same Wi-Fi as the Mac it looks like the example.")
                .font(.system(size: 16))
                .foregroundStyle(Color.ink2)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 14)

            VStack(alignment: .leading, spacing: 8) {
                Text("SERVER ADDRESS")
                    .font(.system(size: 11, weight: .semibold))
                    .tracking(1.6)
                    .foregroundStyle(Color.mute)
                TextField(ServerSettings.example, text: $draft)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .font(.system(size: 17, design: .monospaced))
                    .padding(15)
                    .background(Color.card)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(focused ? Color.ink : Color.line, lineWidth: 1))
                    .focused($focused)
                    .submitLabel(.go)
                    .onSubmit(open)
            }
            .padding(.top, 40)

            Button(action: open) {
                Text("Open Closeout")
                    .font(.system(size: 17, weight: .semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .background(target == nil ? Color.mute : Color.ink)
            .foregroundStyle(.white)
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .disabled(target == nil)
            .padding(.top, 18)

            Spacer()

            Text("To change this later, press and hold the screen with two fingers.")
                .font(.footnote)
                .foregroundStyle(Color.mute)
        }
        .padding(.horizontal, 28)
        .padding(.bottom, 24)
        .background(Color.paper.ignoresSafeArea())
        .ignoresSafeArea(.keyboard, edges: .bottom)
        .onAppear {
            draft = settings.urlString
            focused = true
        }
    }

    private func open() {
        guard let url = target else { return }
        settings.urlString = url.absoluteString
        onDone()
    }
}

/// The page could not be loaded. Plain words, the address, two ways out.
struct UnreachableView: View {
    var address: String
    var detail: String
    var retry: () -> Void
    var change: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Wordmark().padding(.top, 10)
            Spacer()
            Circle()
                .fill(Color.hot)
                .frame(width: 12, height: 12)
                .padding(.bottom, 22)
            Text("Can't reach\nCloseout")
                .font(.system(size: 36, weight: .bold))
                .tracking(-0.6)
                .lineSpacing(2)
                .foregroundStyle(Color.ink)
            Text(address)
                .font(.system(size: 15, design: .monospaced))
                .foregroundStyle(Color.ink2)
                .padding(.top, 16)
            Text("Check the phone is on the same Wi-Fi as the Mac and the server is running.")
                .font(.system(size: 16))
                .foregroundStyle(Color.ink2)
                .padding(.top, 10)
            Text(detail)
                .font(.footnote)
                .foregroundStyle(Color.mute)
                .padding(.top, 6)
            Spacer()
            Button(action: retry) {
                Text("Try again")
                    .font(.system(size: 17, weight: .semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .background(Color.ink)
            .foregroundStyle(.white)
            .clipShape(RoundedRectangle(cornerRadius: 14))
            Button(action: change) {
                Text("Change address")
                    .font(.system(size: 17, weight: .semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .foregroundStyle(Color.ink)
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.ink, lineWidth: 1))
            .padding(.top, 10)
        }
        .padding(.horizontal, 28)
        .padding(.bottom, 24)
        .background(Color.paper.ignoresSafeArea())
    }
}
