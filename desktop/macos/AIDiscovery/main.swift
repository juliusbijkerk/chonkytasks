import AppKit
import Darwin
import Foundation
import UniformTypeIdentifiers
import WebKit

// chonkytasks — a local macOS app. It starts the project's Python backend (FastAPI +
// FFmpeg + Mistral via OpenRouter) on an ephemeral loopback port and shows its UI
// in a WKWebView. Recordings dropped on the window or the Dock icon are copied
// into the backend's inbox folder and handed to the page, which starts analysis.

let videoExtensions: Set<String> = ["mp4", "mov", "webm", "mkv", "m4v"]

func isVideoFile(_ url: URL) -> Bool {
    url.isFileURL && videoExtensions.contains(url.pathExtension.lowercased())
}

func jsString(_ value: String) -> String {
    value.replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "'", with: "\\'")
        .replacingOccurrences(of: "\n", with: "\\n")
}

func htmlString(_ value: String) -> String {
    value.replacingOccurrences(of: "&", with: "&amp;")
        .replacingOccurrences(of: "<", with: "&lt;")
        .replacingOccurrences(of: ">", with: "&gt;")
}

enum AppPaths {
    /// Locate the directory that contains app/main.py (the backend root).
    /// Order: CHONKYTASKS_HOME env (with legacy overrides), build path,
    /// then the repo layout when the .app sits in <repo>/dist/.
    static func backendRoot() -> URL? {
        let fm = FileManager.default
        func valid(_ path: String) -> URL? {
            fm.fileExists(atPath: path + "/app/main.py") ? URL(fileURLWithPath: path) : nil
        }
        for key in ["CHONKYTASKS_HOME", "AI_DISCOVERY_HOME", "FLOWLENS_HOME"] {
            if let env = ProcessInfo.processInfo.environment[key], let url = valid(env) {
                return url
            }
        }
        if let recorded = Bundle.main.path(forResource: "backend-root", ofType: "txt"),
           let text = String(data: (try? Data(contentsOf: URL(fileURLWithPath: recorded))) ?? Data(), encoding: .utf8),
           let url = valid(text.trimmingCharacters(in: .whitespacesAndNewlines)) {
            return url
        }
        let bundle = Bundle.main.bundleURL
        return valid(bundle.deletingLastPathComponent().deletingLastPathComponent().path)
    }

    static func pythonExecutable(root: URL) throws -> String {
        let fm = FileManager.default
        let venv = root.appendingPathComponent(".venv/bin/python").path
        if fm.isExecutableFile(atPath: venv) { return venv }
        throw NSError(domain: "AIDiscovery", code: 1, userInfo: [NSLocalizedDescriptionKey:
            "The project's Python environment is missing. Follow the setup in README.md, then reopen chonkytasks."])
    }
}

final class ServerProcess {
    let root: URL
    let port: Int
    private(set) var process: Process?
    private var logHandle: FileHandle?
    var onUnexpectedExit: (() -> Void)?
    private var stopping = false

    static let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/AI Discovery/server.log")

    init?(root: URL) {
        guard let port = ServerProcess.freePort() else { return nil }
        self.root = root
        self.port = port
    }

    var baseURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    static func freePort() -> Int? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.stride)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.stride))
            }
        }
        guard bound == 0 else { return nil }
        var length = socklen_t(MemoryLayout<sockaddr_in>.stride)
        let named = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(fd, $0, &length)
            }
        }
        guard named == 0 else { return nil }
        return Int(addr.sin_port.bigEndian)
    }

    func start() throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: try AppPaths.pythonExecutable(root: root))
        process.arguments = ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "\(port)"]
        process.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        // Finder does not read shell profiles. Include both Homebrew locations
        // so video tools can be found when opening the app from Finder or the Dock.
        environment["PATH"] = ([root.appendingPathComponent(".venv/bin").path,
                                "/opt/homebrew/bin", "/usr/local/bin",
                                environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"]).joined(separator: ":")
        process.environment = environment
        try FileManager.default.createDirectory(at: Self.logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: Self.logURL.path, contents: nil)
        let handle = try FileHandle(forWritingTo: Self.logURL)
        logHandle = handle
        process.standardOutput = handle
        process.standardError = handle
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, !self.stopping else { return }
                self.onUnexpectedExit?()
            }
        }
        try process.run()
        self.process = process
    }

    func waitUntilReady(timeout: TimeInterval = 25, completion: @escaping (Bool) -> Void) {
        let deadline = Date().addingTimeInterval(timeout)
        func attempt() {
            var request = URLRequest(url: baseURL.appendingPathComponent("api/health"))
            request.timeoutInterval = 2
            URLSession.shared.dataTask(with: request) { data, response, error in
                DispatchQueue.main.async {
                    let body = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                    let healthy = error == nil && (response as? HTTPURLResponse)?.statusCode == 200
                        && body?["configured"] is Bool && body?["ffmpeg"] is Bool
                        && self.process?.isRunning == true
                    if healthy {
                        completion(true)
                    } else if Date() < deadline, self.process?.isRunning == true {
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: attempt)
                    } else {
                        completion(false)
                    }
                }
            }.resume()
        }
        attempt()
    }

    func stop() {
        stopping = true
        defer { try? logHandle?.close(); logHandle = nil }
        guard let process, process.isRunning else { return }
        process.terminate()
        let semaphore = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            process.waitUntilExit()
            semaphore.signal()
        }
        if semaphore.wait(timeout: .now() + 3) == .timedOut {
            kill(process.processIdentifier, SIGKILL)
        }
    }
}

/// Container view that accepts file drops even where the web view does not forward them.
final class DropView: NSView {
    weak var appDelegate: AppDelegate?

    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes([.fileURL])
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        registerForDraggedTypes([.fileURL])
    }

    private func droppedVideos(_ sender: any NSDraggingInfo) -> [URL] {
        (sender.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: nil) as? [URL])?
            .filter(isVideoFile) ?? []
    }

    override func draggingEntered(_ sender: any NSDraggingInfo) -> NSDragOperation {
        droppedVideos(sender).isEmpty ? [] : .copy
    }

    override func draggingUpdated(_ sender: any NSDraggingInfo) -> NSDragOperation {
        draggingEntered(sender)
    }

    override func performDragOperation(_ sender: any NSDraggingInfo) -> Bool {
        let videos = droppedVideos(sender)
        guard !videos.isEmpty else { return false }
        videos.forEach { appDelegate?.handleDroppedFile($0) }
        return true
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandlerWithReply {
    var window: NSWindow!
    var webView: WKWebView!
    var server: ServerProcess?
    private var pageReady = false
    private var pendingFiles: [URL] = []
    private var recorderStorage: NSObject?

    @available(macOS 15.0, *)
    private func recorder() -> ScreenRecorder? {
        if let recorder = recorderStorage as? ScreenRecorder { return recorder }
        guard let root = server?.root else { return nil }
        let recorder = ScreenRecorder(root: root)
        recorder.onChange = { [weak self] state in
            guard let data = try? JSONSerialization.data(withJSONObject: state),
                  let json = String(data: data, encoding: .utf8) else { return }
            self?.webView?.evaluateJavaScript("window.recordingUI?.nativeState(\(json))")
        }
        recorderStorage = recorder
        return recorder
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        let origin = message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame, origin.protocol == "http", origin.host == "127.0.0.1",
              origin.port == server?.port, let body = message.body as? [String: String] else {
            replyHandler(nil, "Recording is available only from the local app."); return
        }
        guard #available(macOS 15.0, *), let recorder = recorder() else {
            replyHandler(nil, "Direct recording in the Mac app requires macOS 15 or later."); return
        }
        do {
            switch body["action"] {
            case "start": try recorder.start(context: body["context"] ?? "")
            case "stop": recorder.stop()
            case "acknowledge": try recorder.acknowledge(name: body["name"] ?? "")
            case "status": break
            default: replyHandler(nil, "Unknown recording action."); return
            }
            replyHandler(recorder.snapshot, nil)
        } catch { replyHandler(nil, error.localizedDescription) }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        guard let root = AppPaths.backendRoot() else {
            presentFatalError("The project folder could not be found. Keep chonkytasks.app in the project's dist folder, or rebuild it after moving the project. You can also set CHONKYTASKS_HOME to the project folder.")
            return
        }
        guard let server = ServerProcess(root: root) else {
            presentFatalError("Could not reserve a local port.")
            return
        }
        self.server = server
        buildWindow()
        webView.loadHTMLString("<html><body style='font:15px -apple-system;color:#555;text-align:center;padding-top:120px'>Opening chonkytasks…</body></html>", baseURL: nil)
        do {
            try server.start()
        } catch {
            presentFatalError("Could not start the local server: \(error.localizedDescription)")
            return
        }
        server.waitUntilReady { [weak self] ok in
            guard let self else { return }
            if ok {
                server.onUnexpectedExit = { [weak self] in
                    self?.presentFatalError("The local server stopped. Reopen chonkytasks to try again.\nDetails: ~/Library/Logs/AI Discovery/server.log")
                }
                self.webView.load(URLRequest(url: server.baseURL))
            } else {
                self.presentFatalError("The local server could not start.\nDetails: ~/Library/Logs/AI Discovery/server.log\nTo repair the project environment, run .venv/bin/python -m pip install -r requirements.lock.txt from the project folder, then reopen the app.")
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        server?.stop()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if #available(macOS 15.0, *), let recorder = recorderStorage as? ScreenRecorder, recorder.isCapturing {
            window.makeKeyAndOrderFront(nil)
            let alert = NSAlert()
            alert.messageText = "Your recording is still in progress."
            alert.informativeText = "Stop and save it before closing chonkytasks."
            alert.addButton(withTitle: "Stop & analyze")
            alert.addButton(withTitle: "Keep open")
            if alert.runModal() == .alertFirstButtonReturn { recorder.stop() }
            return .terminateCancel
        }
        return .terminateNow
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    /// Files dropped on the Dock icon or opened via "Open With".
    func application(_ application: NSApplication, openFiles files: [String]) {
        files.map(URL.init(fileURLWithPath:)).filter(isVideoFile).forEach(handleDroppedFile)
        application.reply(toOpenOrPrint: .success)
    }

    // MARK: - Window & web view

    private func buildWindow() {
        let rect = NSRect(x: 0, y: 0, width: 1120, height: 780)
        window = NSWindow(contentRect: rect, styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "chonkytasks"
        window.minSize = NSSize(width: 720, height: 540)
        window.center()
        window.delegate = self

        let configuration = WKWebViewConfiguration()
        configuration.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "screenRecording")
        webView = WKWebView(frame: rect, configuration: configuration)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self

        let dropView = DropView(frame: rect)
        dropView.autoresizingMask = [.width, .height]
        dropView.addSubview(webView)
        dropView.appDelegate = self
        window.contentView = dropView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func buildMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "New analysis", action: #selector(newAnalysis), keyEquivalent: "n")
        appMenu.addItem(withTitle: "Open recording…", action: #selector(openRecording), keyEquivalent: "o")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit chonkytasks", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        for (title, action, key) in [("Undo", "undo:", "z"), ("Cut", "cut:", "x"),
                                     ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                                     ("Select All", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        editItem.submenu = editMenu
        mainMenu.addItem(editItem)
        NSApp.mainMenu = mainMenu
    }

    @objc private func newAnalysis() {
        webView?.evaluateJavaScript("window.aiDiscovery && window.aiDiscovery.home()")
    }

    @objc private func openRecording() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.movie, .mpeg4Movie, .quickTimeMovie, .video, .audiovisualContent]
        panel.begin { [weak self] result in
            guard result == .OK else { return }
            panel.urls.filter(isVideoFile).forEach { self?.handleDroppedFile($0) }
        }
    }

    // MARK: - Drag & drop bridge

    func handleDroppedFile(_ url: URL?) {
        guard let url, isVideoFile(url) else { return }
        guard pageReady, let root = server?.root else {
            pendingFiles.append(url)
            return
        }
        let inbox = root.appendingPathComponent("data/jobs/inbox", isDirectory: true)
        try? FileManager.default.createDirectory(at: inbox, withIntermediateDirectories: true)
        let token = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let destination = inbox.appendingPathComponent("\(token).\(url.pathExtension.lowercased())")
        do {
            try? FileManager.default.removeItem(at: destination)
            try FileManager.default.copyItem(at: url, to: destination)
        } catch {
            guard let data = try? Data(contentsOf: url) else { return }
            try? data.write(to: destination)
        }
        webView.evaluateJavaScript(
            "window.aiDiscovery && window.aiDiscovery.dropped('\(destination.lastPathComponent)', '\(jsString(url.lastPathComponent))')")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard webView.url?.host == server?.baseURL.host else { return }
        pageReady = true
        let files = pendingFiles
        pendingFiles.removeAll()
        files.forEach(handleDroppedFile)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        presentFatalError("The window could not load. Reopen chonkytasks to try again.\n\(error.localizedDescription)")
    }

    // MARK: - Navigation policy: local server only

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if let scheme = url.scheme, scheme == "http" || scheme == "https" {
            if url.scheme == server?.baseURL.scheme && url.host == server?.baseURL.host && url.port == server?.baseURL.port {
                decisionHandler(.allow)
            } else {
                decisionHandler(.cancel)
                NSWorkspace.shared.open(url)
            }
        } else if url.scheme == "about" {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // MARK: - Fatal error page

    private func presentFatalError(_ message: String) {
        pageReady = false
        if window == nil { buildWindow() }
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><style>
        body{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;background:#fff;color:#141414;
        max-width:520px;margin:120px auto;line-height:1.6;font-size:15px}
        h1{font-size:20px;margin:0 0 12px}p{white-space:pre-wrap;color:#555}
        </style></head><body><h1>chonkytasks could not start</h1><p>\(htmlString(message))</p></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

// Entry point. main.swift allows top-level code; `appDelegate` stays alive for the
// process lifetime because NSApplication.delegate is weak.
let appDelegate = AppDelegate()
let sharedApp = NSApplication.shared
sharedApp.delegate = appDelegate
sharedApp.setActivationPolicy(.regular)
sharedApp.run()
