import AVFoundation
import CoreGraphics
import Foundation

enum RecordingSceneSampling {
    struct Change {
        let seconds: TimeInterval
        let strength: Double
    }

    enum Cue: Hashable {
        case copy, paste, emptyFolder, save
    }

    struct Hint {
        let seconds: TimeInterval
        let cue: Cue
    }

    private static let digestWidth = 64
    private static let digestHeight = 36
    private static let windowLength: TimeInterval = 26
    private static let maximumWindows = 4
    private static let denseStep: TimeInterval = 0.28

    static func scan(_ asset: AVURLAsset, duration: TimeInterval) async throws -> (changes: [Change], inspected: Int) {
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 256, height: 256)
        generator.requestedTimeToleranceBefore = CMTime(seconds: 0.16, preferredTimescale: 600)
        generator.requestedTimeToleranceAfter = CMTime(seconds: 0.16, preferredTimescale: 600)
        let count = Int(min(520, ceil(duration / 0.44)))
        var prior: (seconds: TimeInterval, pixels: [UInt8])?
        var changes: [Change] = []
        var inspected = 0
        for seconds in evenlySpaced(start: 0, end: duration, count: count) {
            try Task.checkCancellation()
            guard let frame = try? await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)),
                  let pixels = digest(frame.image) else { continue }
            let actual = frame.actualTime.seconds
            guard actual.isFinite, actual >= 0 else { continue }
            if let prior {
                guard actual > prior.seconds else { continue }
                let strength = difference(prior.pixels, pixels)
                if strength >= 0.020 {
                    changes.append(Change(seconds: actual, strength: strength))
                }
            }
            prior = (actual, pixels)
            inspected += 1
        }
        try Task.checkCancellation()
        return (changes, inspected)
    }

    static func denseTimes(duration: TimeInterval, changes: [Change], hints: [Hint]) -> [TimeInterval] {
        guard duration > windowLength else { return evenlySpaced(start: 0, end: duration, count: 96) }
        let lastStart = duration - windowLength
        var starts = Set<Int>()
        func propose(_ start: TimeInterval) {
            // Scene anchors are precise enough to compare at half-second resolution.
            starts.insert(Int((min(lastStart, max(0, start)) * 2).rounded(.down)))
        }
        for change in changes { propose(change.seconds - 10) }
        for hint in hints {
            propose(hint.seconds - 6)
            propose(hint.seconds - 13)
            if hint.cue == .copy { propose(hint.seconds - 2) }
        }

        let scored = starts.map { tick -> (start: TimeInterval, score: Double) in
            let start = TimeInterval(tick) / 2
            let end = start + windowLength
            let visual = changes.reduce(0.0) { score, change in
                score + (start...end ~= change.seconds ? min(0.07, change.strength) : 0)
            }
            var marks: [Cue: Set<Int>] = [:]
            for hint in hints where (start + 1)...(end - 2) ~= hint.seconds {
                marks[hint.cue, default: []].insert(Int(hint.seconds / 2))
            }
            let copies = marks[.copy]?.count ?? 0
            let pastes = marks[.paste]?.count ?? 0
            let folders = marks[.emptyFolder]?.count ?? 0
            let saves = marks[.save]?.count ?? 0
            let copyScore = min(1, copies) * 8 + min(3, pastes) * 4
            let fieldScore = min(3, folders) * 2 + min(1, saves) * 4
            return (start, Double(copyScore + fieldScore) + min(3.0, visual))
        }
        let ranked = scored.sorted { lhs, rhs in
            lhs.score == rhs.score ? lhs.start < rhs.start : lhs.score > rhs.score
        }

        var startsToInspect: [TimeInterval] = []
        for entry in ranked where entry.score > 0 {
            if startsToInspect.contains(where: { abs($0 - entry.start) < windowLength * 0.65 }) { continue }
            startsToInspect.append(entry.start)
            if startsToInspect.count == maximumWindows { break }
        }

        // A single lattice deduplicates overlapping windows and gives short-lived UI cues
        // the same coverage regardless of where the change detector located the anchor.
        var ticks = Set<Int>()
        for start in startsToInspect {
            let first = Int(ceil(start / denseStep))
            let last = Int(floor(min(duration - 0.01, start + windowLength) / denseStep))
            guard last >= first else { continue }
            ticks.formUnion(first...last)
        }
        return ticks.sorted().map { TimeInterval($0) * denseStep }
    }

    static func sceneHints(_ frames: [SampledRecordingFrame]) -> [Hint] {
        frames.flatMap { frame -> [Hint] in
            let keys = Set(frame.lines.map(\.key))
            var cues: [Cue] = []
            if keys.contains("copy"), keys.contains("cut"), keys.contains("recent") { cues.append(.copy) }
            if keys.contains("paste"), keys.contains("new folder"), keys.contains("recent") { cues.append(.paste) }
            if keys.contains("recent"), keys.contains("folder is empty") || keys.contains("this folder is empty") {
                cues.append(.emptyFolder)
            }
            if keys.contains("save as"), keys.contains(where: { $0.hasPrefix("name:") }) { cues.append(.save) }
            return cues.map { Hint(seconds: frame.cue.seconds, cue: $0) }
        }
    }

    static func evenlySpaced(start: TimeInterval, end: TimeInterval, count: Int) -> [TimeInterval] {
        guard count > 0 else { return [] }
        let spacing = (end - start) / Double(count)
        return (0..<count).map { start + (Double($0) + 0.5) * spacing }
    }

    private static func digest(_ image: CGImage) -> [UInt8]? {
        var pixels = [UInt8](repeating: 0, count: digestWidth * digestHeight)
        let ok = pixels.withUnsafeMutableBytes { bytes in
            guard let context = CGContext(data: bytes.baseAddress, width: digestWidth, height: digestHeight,
                                          bitsPerComponent: 8, bytesPerRow: digestWidth,
                                          space: CGColorSpaceCreateDeviceGray(),
                                          bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return false }
            context.interpolationQuality = .medium
            context.draw(image, in: CGRect(x: 0, y: 0, width: digestWidth, height: digestHeight))
            return true
        }
        return ok ? pixels : nil
    }

    private static func difference(_ earlier: [UInt8], _ later: [UInt8]) -> Double {
        // A menu can occupy little of the screen. Keep the strongest local tile
        // alongside the mean so that the activity survives downsampling.
        let columns = 8, rows = 6
        let tileWidth = digestWidth / columns, tileHeight = digestHeight / rows
        var tiles = [Int](repeating: 0, count: columns * rows)
        for y in 0..<digestHeight {
            for x in 0..<digestWidth {
                let pixel = y * digestWidth + x
                tiles[(y / tileHeight) * columns + x / tileWidth] += abs(Int(earlier[pixel]) - Int(later[pixel]))
            }
        }
        let peak = Double(tiles.max() ?? 0) / Double(255 * tileWidth * tileHeight)
        let average = Double(tiles.reduce(0, +)) / Double(255 * earlier.count)
        return max(peak * 0.5, average)
    }
}
