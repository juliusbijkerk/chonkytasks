import AVFoundation
import CoreGraphics
import Foundation
import Vision

struct RecognizedScreenLine {
    let text: String
    let box: CGRect

    var key: String { text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
}

struct ImportedReplayCue {
    let clipID = "recording"
    let seconds: TimeInterval
}

struct SampledRecordingFrame {
    let cue: ImportedReplayCue
    let lines: [RecognizedScreenLine]
}

enum ImportedWorkflowKind: String, Encodable {
    case mailToCalendar, mailToTask, mailToNote, invoiceFiling, spreadsheetReport, orderDraft
}

struct ImportedWorkflowCandidate: Encodable {
    struct Example: Encodable {
        let source: Double
        let result: Double
        let source_caption: String
        let result_caption: String
    }

    let kind: ImportedWorkflowKind
    let title: String
    let summary: String
    let examples: [Example]
}

enum ScanError: Error { case unreadable }

func inspectRecording(_ url: URL) async throws -> [ImportedWorkflowCandidate] {
    let asset = AVURLAsset(url: url, options: [
        AVURLAssetReferenceRestrictionsKey: AVAssetReferenceRestrictions.forbidAll.rawValue
    ])
    let duration = try await asset.load(.duration).seconds
    guard duration.isFinite, (0...900).contains(duration), duration > 0 else { throw ScanError.unreadable }
    let generator = AVAssetImageGenerator(asset: asset)
    generator.appliesPreferredTrackTransform = true
    generator.maximumSize = CGSize(width: 2560, height: 2560)
    generator.requestedTimeToleranceBefore = CMTime(seconds: 0.04, preferredTimescale: 600)
    generator.requestedTimeToleranceAfter = generator.requestedTimeToleranceBefore
    var frames: [SampledRecordingFrame] = []
    var inspected = 0
    var times = Set<CMTime>()

    func sample(_ timestamps: [TimeInterval]) async {
        for time in timestamps {
            guard let decoded = try? await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)),
                  decoded.actualTime.seconds.isFinite, decoded.actualTime.seconds >= 0,
                  times.insert(decoded.actualTime).inserted else { continue }
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .fast
            request.usesLanguageCorrection = false
            do { try VNImageRequestHandler(cgImage: decoded.image).perform([request]) }
            catch { continue }
            inspected += 1
            let lines = (request.results ?? []).prefix(160).compactMap { result -> RecognizedScreenLine? in
                guard let text = result.topCandidates(1).first?.string, !text.isEmpty else { return nil }
                return RecognizedScreenLine(text: String(text.prefix(250)), box: result.boundingBox)
            }
            if !lines.isEmpty {
                frames.append(.init(cue: .init(seconds: decoded.actualTime.seconds), lines: lines))
            }
        }
    }

    let count = duration <= 90 ? Int(min(96, ceil(duration / 0.28))) :
        Int(max(min(duration <= 600 ? 184 : 100, ceil(duration / 1.65)), min(280, ceil(duration / 2.7))))
    await sample(RecordingSceneSampling.evenlySpaced(start: 0, end: duration, count: count))
    if duration > 90 {
        let scene = try await RecordingSceneSampling.scan(asset, duration: duration)
        await sample(RecordingSceneSampling.denseTimes(duration: duration, changes: scene.changes,
                                                       hints: RecordingSceneSampling.sceneHints(frames)))
    }
    guard inspected > 0 else { throw ScanError.unreadable }
    return RecordingMailWorkflowDiscovery.candidates(in: frames) + RecordingOfficeWorkflowDiscovery.candidates(in: frames)
}

do {
    guard CommandLine.arguments.count == 2 else { throw ScanError.unreadable }
    let findings = try await inspectRecording(URL(fileURLWithPath: CommandLine.arguments[1]))
    // Cross the helper boundary with fixed labels and timestamps only.
    FileHandle.standardOutput.write(try JSONEncoder().encode(findings))
} catch {
    FileHandle.standardError.write(Data("Local workflow evidence could not be checked.\n".utf8))
    exit(1)
}
