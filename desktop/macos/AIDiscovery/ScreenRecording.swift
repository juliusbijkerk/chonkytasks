import AVFoundation
import Foundation
import ScreenCaptureKit

@available(macOS 15.0, *)
@MainActor
final class ScreenRecording: NSObject, SCContentSharingPickerObserver, SCStreamDelegate, SCRecordingOutputDelegate {
    var onState: ((String, String?) -> Void)?
    var onRecording: ((URL) -> Void)?

    private enum State { case idle, selecting, starting, recording, stopping }

    private let picker = SCContentSharingPicker.shared
    private var state = State.idle
    private var stream: SCStream?
    private var recordingOutput: SCRecordingOutput?
    private var recordingURL: URL?
    private var limitTimer: Timer?
    private var transitionTimeout: DispatchWorkItem?

    func choose(display: Bool) {
        guard state == .idle else { return }
        state = .selecting
        onState?("selecting", nil)
        var configuration = SCContentSharingPickerConfiguration()
        configuration.allowedPickerModes = display ? [.singleDisplay] : [.singleWindow]
        configuration.allowsChangingSelectedContent = false
        picker.configuration = configuration
        picker.maximumStreamCount = 1
        picker.add(self)
        picker.isActive = true
        picker.present(using: display ? .display : .window)
    }

    private func begin(_ filter: SCContentFilter) async {
        guard state == .selecting else { return }
        state = .starting
        onState?("starting", nil)
        setTransitionTimeout("The recording could not start. Try selecting a source again.")
        do {
            let directory = FileManager.default.temporaryDirectory
                .appendingPathComponent("chonkytasks-capture-\(UUID().uuidString)", isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                    attributes: [.posixPermissions: 0o700])
            let timestamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
            let url = directory.appendingPathComponent("Screen recording \(timestamp).mp4")
            recordingURL = url

            let configuration = SCStreamConfiguration()
            let size = filter.contentRect.size
            let scale = min(CGFloat(filter.pointPixelScale), 1920 / max(1, size.width), 1200 / max(1, size.height))
            // H.264 needs even dimensions.
            configuration.width = max(2, Int(size.width * scale) / 2 * 2)
            configuration.height = max(2, Int(size.height * scale) / 2 * 2)
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 10)
            configuration.capturesAudio = false
            configuration.captureMicrophone = false
            configuration.showsCursor = true

            let file = SCRecordingOutputConfiguration()
            file.outputURL = url
            file.outputFileType = .mp4
            file.videoCodecType = .h264
            let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
            let output = SCRecordingOutput(configuration: file, delegate: self)
            self.stream = stream
            recordingOutput = output
            try stream.addRecordingOutput(output)
            try await stream.startCapture()
        } catch {
            if state == .starting {
                fail("Could not start this recording. Choose a display or window again.")
            }
        }
    }

    func stop() {
        guard state == .recording, let stream else { return }
        beginFinishing()
        Task { [weak self] in
            do {
                try await stream.stopCapture()
            } catch {
                guard let self, self.stream === stream else { return }
                self.fail("Could not finish this recording. Try recording again.")
            }
        }
    }

    private func beginFinishing() {
        state = .stopping
        limitTimer?.invalidate()
        onState?("stopping", nil)
        setTransitionTimeout("The recording could not be finalized. Try recording again.")
    }

    private func setTransitionTimeout(_ message: String) {
        transitionTimeout?.cancel()
        let timeout = DispatchWorkItem { [weak self] in self?.fail(message) }
        transitionTimeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 15, execute: timeout)
    }

    private func observeLimits() {
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, let output = self.recordingOutput else { return }
                // Leave headroom below the existing import limits (15 minutes, 500 MiB).
                if output.recordedDuration.seconds >= 14 * 60 + 45
                    || output.recordedFileSize >= 475 * 1024 * 1024 {
                    self.stop()
                }
            }
        }
        limitTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func reset() {
        picker.remove(self)
        picker.isActive = false
        limitTimer?.invalidate()
        limitTimer = nil
        transitionTimeout?.cancel()
        transitionTimeout = nil
        recordingOutput = nil
        stream = nil
        recordingURL = nil
        state = .idle
    }

    private func fail(_ message: String) {
        let stream = self.stream
        let folder = recordingURL?.deletingLastPathComponent()
        reset()
        Task {
            if let stream { try? await stream.stopCapture() }
            if let folder { try? FileManager.default.removeItem(at: folder) }
        }
        onState?("error", message)
    }

    func discard() {
        let folder = recordingURL?.deletingLastPathComponent()
        reset()
        if let folder { try? FileManager.default.removeItem(at: folder) }
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        Task { @MainActor in
            guard self.state == .selecting, stream == nil else { return }
            self.reset()
            self.onState?("idle", "Recording selection canceled.")
        }
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter,
                                          for stream: SCStream?) {
        Task { @MainActor in
            guard stream == nil else { return }
            await self.begin(filter)
        }
    }

    nonisolated func contentSharingPickerStartDidFailWithError(_ error: Error) {
        Task { @MainActor in
            guard self.state == .selecting else { return }
            self.fail("Could not open the macOS source picker. Try again.")
        }
    }

    nonisolated func stream(_ stream: SCStream, didStopWithError error: Error) {
        Task { @MainActor in
            guard self.stream === stream, self.state != .stopping else { return }
            if (error as? SCStreamError)?.code == .userStopped, self.state == .recording {
                // ScreenCaptureKit finalizes its writer when sharing stops from the system menu.
                self.beginFinishing()
            } else {
                self.fail("Screen sharing ended before the recording could finish.")
            }
        }
    }

    nonisolated func recordingOutputDidStartRecording(_ recordingOutput: SCRecordingOutput) {
        Task { @MainActor in
            guard self.recordingOutput === recordingOutput, self.state == .starting else { return }
            self.transitionTimeout?.cancel()
            self.transitionTimeout = nil
            self.state = .recording
            self.onState?("recording", nil)
            self.observeLimits()
        }
    }

    nonisolated func recordingOutput(_ recordingOutput: SCRecordingOutput, didFailWithError error: Error) {
        Task { @MainActor in
            guard self.recordingOutput === recordingOutput else { return }
            self.fail("The recording could not be written. Try recording a shorter clip.")
        }
    }

    nonisolated func recordingOutputDidFinishRecording(_ recordingOutput: SCRecordingOutput) {
        Task { @MainActor in
            guard self.recordingOutput === recordingOutput, let url = self.recordingURL else { return }
            let bytes = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize
            guard let bytes, bytes > 0 else {
                self.fail("The recording had no video. Try recording again.")
                return
            }
            self.reset()
            self.onState?("idle", "Opening your local recording…")
            self.onRecording?(url)
        }
    }
}
