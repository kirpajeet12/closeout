import SwiftUI
import WebKit

/// What the shell knows about the page: whether it ever loaded and why it last failed.
final class ShellState: ObservableObject {
    @Published var failure: String? = nil
    @Published var everLoaded = false
}

struct WebShell: UIViewRepresentable {
    let url: URL
    let reloadToken: Int
    @ObservedObject var state: ShellState
    var onSettings: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        // window.print() does nothing inside a web view; route it to the iOS print sheet.
        let printBridge = WKUserScript(
            source: "window.print = function () { window.webkit.messageHandlers.closeout.postMessage({type: 'print'}); };",
            injectionTime: .atDocumentStart, forMainFrameOnly: true)
        config.userContentController.addUserScript(printBridge)
        config.userContentController.add(context.coordinator, name: "closeout")

        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.allowsBackForwardNavigationGestures = true
        web.allowsLinkPreview = false
        web.scrollView.bounces = false
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.isOpaque = false
        web.backgroundColor = UIColor(Color.paper)
        web.scrollView.backgroundColor = UIColor(Color.paper)

        // Two-finger press and hold anywhere = change the server address.
        let press = UILongPressGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.longPress(_:)))
        press.numberOfTouchesRequired = 2
        press.minimumPressDuration = 0.8
        press.delegate = context.coordinator
        web.addGestureRecognizer(press)

        context.coordinator.webView = web
        web.load(URLRequest(url: url))
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        context.coordinator.parent = self
        let co = context.coordinator
        if co.lastToken != reloadToken || co.lastURL != url {
            co.lastToken = reloadToken
            co.lastURL = url
            web.load(URLRequest(url: url))
        }
    }

    static func dismantleUIView(_ web: WKWebView, coordinator: Coordinator) {
        web.configuration.userContentController.removeScriptMessageHandler(forName: "closeout")
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, UIGestureRecognizerDelegate {
        var parent: WebShell
        weak var webView: WKWebView?
        var lastToken: Int
        var lastURL: URL

        init(_ parent: WebShell) {
            self.parent = parent
            lastToken = parent.reloadToken
            lastURL = parent.url
        }

        private func sameServer(_ target: URL) -> Bool {
            target.host?.lowercased() == parent.url.host?.lowercased()
        }

        // MARK: loading

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            parent.state.failure = nil
            parent.state.everLoaded = true
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { fail(error) }
        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { fail(error) }

        private func fail(_ error: Error) {
            let ns = error as NSError
            if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
            parent.state.failure = ns.localizedDescription
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }

        // The local server runs HTTPS with a self-signed certificate (data/tls). Trust it only for
        // the configured server and only when that server is a private-network address.
        func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                     completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
            let space = challenge.protectionSpace
            if space.authenticationMethod == NSURLAuthenticationMethodServerTrust,
               let trust = space.serverTrust,
               space.host.lowercased() == parent.url.host?.lowercased(),
               ServerSettings.isPrivateHost(space.host) {
                completionHandler(.useCredential, URLCredential(trust: trust))
                return
            }
            completionHandler(.performDefaultHandling, nil)
        }

        // Links that leave the server open in Safari; everything else stays in the app.
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let target = navigationAction.request.url else { decisionHandler(.allow); return }
            let scheme = target.scheme?.lowercased() ?? ""
            if scheme == "mailto" || scheme == "tel" || scheme == "sms" {
                UIApplication.shared.open(target); decisionHandler(.cancel); return
            }
            if (scheme == "http" || scheme == "https"), !sameServer(target), navigationAction.navigationType == .linkActivated {
                UIApplication.shared.open(target); decisionHandler(.cancel); return
            }
            decisionHandler(.allow)
        }

        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let target = navigationAction.request.url {
                if sameServer(target) { webView.load(navigationAction.request) } else { UIApplication.shared.open(target) }
            }
            return nil
        }

        // MARK: dialogs the page may open

        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
            present(alert) ?? completionHandler()
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
            present(alert) ?? completionHandler(false)
        }

        private func present(_ alert: UIAlertController) -> Void? {
            guard let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }).first,
                  var top = scene.keyWindow?.rootViewController else { return nil }
            while let next = top.presentedViewController { top = next }
            top.present(alert, animated: true)
            return ()
        }

        // MARK: print bridge

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.name == "closeout",
                  let body = message.body as? [String: Any],
                  body["type"] as? String == "print",
                  let web = webView else { return }
            let printer = UIPrintInteractionController.shared
            let info = UIPrintInfo(dictionary: nil)
            info.outputType = .general
            info.jobName = "Closeout"
            printer.printInfo = info
            printer.printFormatter = web.viewPrintFormatter()
            printer.present(animated: true, completionHandler: nil)
        }

        // MARK: gestures

        @objc func longPress(_ gesture: UILongPressGestureRecognizer) {
            if gesture.state == .began { parent.onSettings() }
        }

        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
    }
}
