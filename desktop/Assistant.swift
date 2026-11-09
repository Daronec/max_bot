import Cocoa
import WebKit

final class AssistantApp: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var root: URL!
    var panel: URL!
    var attempts = 0

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let path = Bundle.main.object(forInfoDictionaryKey: "AssistantProjectRoot") as? String,
              let address = Bundle.main.object(forInfoDictionaryKey: "AssistantPanelURL") as? String,
              let url = URL(string: address), url.scheme == "http", url.host == "127.0.0.1" else {
            NSApplication.shared.terminate(nil); return
        }
        root = URL(fileURLWithPath: path)
        panel = url
        let menu = NSMenu()
        let appItem = NSMenuItem(); menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Обновить", action: #selector(reloadPanel), keyEquivalent: "r")
        appMenu.addItem(withTitle: "Открыть в браузере", action: #selector(openBrowser), keyEquivalent: "b")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Завершить", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); menu.addItem(editItem)
        let edit = NSMenu(title: "Правка")
        for (title, action, key) in [("Копировать", "copy:", "c"), ("Вставить", "paste:", "v"), ("Вырезать", "cut:", "x"), ("Выбрать все", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        editItem.submenu = edit
        NSApplication.shared.mainMenu = menu
        webView = WKWebView(frame: .zero)
        webView.navigationDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 850), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "ИИ Ассистент"
        window.contentView = webView
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        webView.loadHTMLString("<html><body style='background:#10141b;color:#ecf0f6;font:20px system-ui;padding:60px'>Подключение к локальному ассистенту…</body></html>", baseURL: nil)
        let process = Process()
        process.executableURL = root.appendingPathComponent(".venv/bin/python")
        process.arguments = [root.appendingPathComponent("launch_server.py").path]
        process.currentDirectoryURL = root
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { showFailure(); return }
        waitForPanel()
    }

    func waitForPanel() {
        var request = URLRequest(url: panel.appendingPathComponent("healthz"))
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { data, response, error in
            DispatchQueue.main.async {
                if error == nil, let http = response as? HTTPURLResponse, http.statusCode == 200,
                   let data = data, let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   json["service"] as? String == "assistant-supervisor" {
                    self.webView.load(URLRequest(url: self.panel)); return
                }
                self.attempts += 1
                if self.attempts < 30 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.waitForPanel() }
                } else { self.showFailure() }
            }
        }.resume()
    }

    func showFailure() {
        webView.loadHTMLString("<html><body style='background:#10141b;color:#ecf0f6;font:18px system-ui;padding:60px'>Не удалось подключиться. Проверьте logs/supervisor.log в проекте. Для повтора используйте меню «Обновить».</body></html>", baseURL: nil)
    }
    @objc func reloadPanel() { attempts = 0; waitForPanel() }
    @objc func openBrowser() { NSWorkspace.shared.open(panel) }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "about" || url.scheme == "http" && url.host == panel.host && url.port == panel.port {
            decisionHandler(.allow)
        } else {
            if navigationAction.navigationType == .linkActivated && ["http", "https"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }
}

let app = NSApplication.shared
let delegate = AssistantApp()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
