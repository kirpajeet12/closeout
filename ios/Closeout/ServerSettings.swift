import Foundation
import Combine

/// Where the Closeout server lives. Stored on the phone; nothing else is persisted natively.
final class ServerSettings: ObservableObject {
    static let key = "closeout.serverURL"
    static let example = "https://192.168.1.20:8443"

    @Published var urlString: String {
        didSet { UserDefaults.standard.set(urlString, forKey: Self.key) }
    }

    init() {
        urlString = UserDefaults.standard.string(forKey: Self.key) ?? ""
    }

    var url: URL? { Self.normalise(urlString) }
    var isConfigured: Bool { url != nil }

    /// Accepts "192.168.1.20:8443", "http://mac.local:8765", "https://closeout.example.com/".
    static func normalise(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") { text = "https://" + text }
        guard var parts = URLComponents(string: text), let host = parts.host, !host.isEmpty else { return nil }
        guard let scheme = parts.scheme?.lowercased(), scheme == "http" || scheme == "https" else { return nil }
        parts.scheme = scheme
        parts.query = nil
        parts.fragment = nil
        if parts.path.isEmpty { parts.path = "/" }
        return parts.url
    }

    /// True for addresses that can only be reached on the same network (home / office Wi-Fi).
    /// Only these may use the self-signed certificate the local HTTPS server runs with.
    static func isPrivateHost(_ host: String) -> Bool {
        let h = host.lowercased()
        if h == "localhost" || h == "127.0.0.1" || h.hasSuffix(".local") { return true }
        let octets = h.split(separator: ".").compactMap { Int($0) }
        guard octets.count == 4 else { return false }
        if octets[0] == 10 { return true }
        if octets[0] == 192 && octets[1] == 168 { return true }
        if octets[0] == 172 && (16...31).contains(octets[1]) { return true }
        return false
    }
}
