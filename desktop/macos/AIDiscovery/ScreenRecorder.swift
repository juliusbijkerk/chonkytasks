import AppKit
import AVFoundation
import ScreenCaptureKit

/// All state is owned by the main queue. ScreenCaptureKit finalizes the MP4
/// before it is offered to the analysis API; existing recordings are untouched.
@available(macOS 15.0, *)
final class ScreenRecorder: NSObject, SCContentSharingPickerObserver, SCRecordingOutputDelegate, SCStreamDelegate {
    private let root: URL
    private var stream: SCStream?
    private var output: SCRecordingOutput?
    private var timer: Timer?
    private var phase = "idle"
    private var startedAt: Date?
    private var descriptor: [String: String] = [:]
    private var message = ""
    var onChange: (([String: Any]) -> Void)?

    private var pendingURL: URL { root.appendingPathComponent("data/pending-recording.json") }
    var isCapturing: Bool { ["starting", "recording", "stopping"].contains(phase) }
    var snapshot: [String: Any] {
        var state: [String: Any] = ["status": phase, "error": message]
        descriptor.forEach { state[$0.key] = $0.value }
        if let startedAt { state["started_at"] = ISO8601DateFormatter().string(from: startedAt) }
        return state
    }

    init(root: URL) {
        self.root = root
        super.init()
        // Recover a finished capture even if the app closed before ingestion.
        // The API also recognizes an already-ingested name if its response was lost.
        if let data = try? Data(contentsOf: pendingURL),
           let saved = try? JSONDecoder().decode([String: String].self, from: data),
           let name = saved["name"], name.range(of: "^[a-f0-9]{32}\\.mp4$", options: .regularExpression) != nil {
            descriptor = saved
            phase = "ready"
        }
        SCContentSharingPicker.shared.add(self)
    }

    func start(context: String) throws {
        guard ["idle", "error"].contains(phase) else {
            throw failure("Finish the current recording before starting another.")
        }
        descriptor = ["context": String(context.prefix(2000))]
        startedAt = nil
        message = ""
        phase = "selecting"
        var configuration = SCContentSharingPickerConfiguration()
        configuration.allowedPickerModes = [.singleDisplay, .singleWindow]
        configuration.allowsChangingSelectedContent = false
        let picker = SCContentSharingPicker.shared
        picker.defaultConfiguration = configuration
        picker.maximumStreamCount = 1
        picker.isActive = true
        publish()
        picker.present()
    }

    func stop() {
        guard phase == "recording", let stream else { return }
        let captureName = descriptor["name"]
        phase = "stopping"
        timer?.invalidate(); timer = nil
        publish()
        Task { @MainActor in
            do {
                try await stream.stopCapture()
                DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
                    guard let self, self.descriptor["name"] == captureName, self.phase == "stopping" else { return }
                    self.fail("The recording is taking too long to finish saving. Please try again.")
                }
            }
            catch { self.fail("The recording could not be stopped: \(error.localizedDescription)") }
            // Completion is delivered by recordingOutputDidFinishRecording,
            // after ScreenCaptureKit has finished writing the file.
        }
    }

    func acknowledge(name: String) throws {
        guard phase == "ready", descriptor["name"] == name else { return }
        if FileManager.default.fileExists(atPath: pendingURL.path) {
            try FileManager.default.removeItem(at: pendingURL)
        }
        phase = "idle"; descriptor = [:]; startedAt = nil; message = ""
        publish()
    }

    private func publish() { onChange?(snapshot) }
    private func failure(_ description: String) -> NSError {
        NSError(domain: "chonkytasks.ScreenRecording", code: 1, userInfo: [NSLocalizedDescriptionKey: description])
    }
    private func releaseCapture() {
        timer?.invalidate(); timer = nil
        SCContentSharingPicker.shared.isActive = false
        stream = nil; output = nil
    }
    private func fail(_ description: String) {
        let previous = stream
        releaseCapture()
        phase = "error"; message = description
        publish()
        if let previous { Task { try? await previous.stopCapture() } }
    }

    @MainActor private func begin(filter: SCContentFilter) async {
        guard phase == "selecting" else { return }
        phase = "starting"; publish()
        do {
            let inbox = root.appendingPathComponent("data/jobs/inbox", isDirectory: true)
            try FileManager.default.createDirectory(at: inbox, withIntermediateDirectories: true)
            let name = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased() + ".mp4"
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyy-MM-dd HH.mm.ss"
            descriptor["name"] = name
            descriptor["filename"] = "Recording \(formatter.string(from: Date())).mp4"

            let configuration = SCStreamConfiguration()
            let rect = filter.contentRect
            let pixels = CGFloat(filter.pointPixelScale)
            let width = max(2, rect.width * pixels), height = max(2, rect.height * pixels)
            let scale = min(1, 1920 / max(width, height))
            configuration.width = max(2, Int(width * scale) / 2 * 2)
            configuration.height = max(2, Int(height * scale) / 2 * 2)
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 15)
            configuration.showsCursor = true
            configuration.capturesAudio = false
            configuration.captureMicrophone = false
            configuration.queueDepth = 5
            let recording = SCRecordingOutputConfiguration()
            recording.outputURL = inbox.appendingPathComponent(name)
            recording.outputFileType = .mp4
            recording.videoCodecType = .h264
            let output = SCRecordingOutput(configuration: recording, delegate: self)
            let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
            self.output = output; self.stream = stream
            try stream.addRecordingOutput(output)
            try await stream.startCapture()
        } catch {
            fail("Could not start recording. \(error.localizedDescription)")
        }
    }

    func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter, for stream: SCStream?) {
        Task { @MainActor in await self.begin(filter: filter) }
    }
    func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        DispatchQueue.main.async {
            guard self.phase == "selecting" else { return }
            self.phase = "idle"; self.descriptor = [:]
            picker.isActive = false
            self.publish()
        }
    }
    func contentSharingPickerStartDidFailWithError(_ error: Error) {
        DispatchQueue.main.async { self.fail("Could not open the screen picker. \(error.localizedDescription)") }
    }
    func recordingOutputDidStartRecording(_ recordingOutput: SCRecordingOutput) {
        DispatchQueue.main.async {
            guard self.output === recordingOutput else { return }
            self.phase = "recording"; self.startedAt = Date()
            self.publish()
            self.timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                guard let self, self.phase == "recording", let output = self.output else { return }
                // Leave room for final frames and the MP4 header under API limits.
                if output.recordedDuration.seconds >= 895 || Date().timeIntervalSince(self.startedAt ?? Date()) >= 895 || output.recordedFileSize >= 450 * 1024 * 1024 {
                    self.stop()
                }
            }
        }
    }
    func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) {
        DispatchQueue.main.async {
            guard self.output === recordingOutput, let name = self.descriptor["name"] else { return }
            do {
                let file = self.root.appendingPathComponent("data/jobs/inbox/\(name)")
                let size = try file.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
                guard size > 0 else { throw self.failure("No video was recorded. Start again and record for a few seconds.") }
                try JSONEncoder().encode(self.descriptor).write(to: self.pendingURL, options: .atomic)
                self.releaseCapture()
                self.phase = "ready"; self.message = ""
                self.publish()
            } catch { self.fail("Could not finish saving the recording. \(error.localizedDescription)") }
        }
    }
    func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: Error) {
        DispatchQueue.main.async {
            guard self.output === recordingOutput else { return }
            self.fail("Recording stopped before the video was saved. \(error.localizedDescription)")
        }
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        DispatchQueue.main.async {
            guard self.stream === stream else { return }
            self.phase = "stopping"; self.timer?.invalidate(); self.timer = nil
            self.publish()
            // Stopping sharing from the system menu also finalizes the output.
            DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
                guard let self, self.stream === stream, self.phase == "stopping" else { return }
                self.fail("Screen sharing ended before the recording was saved. \(error.localizedDescription)")
            }
        }
    }
}
