import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

enum EvidenceRedactionError: Error { case redactionFailed }

enum EvidenceRedaction {
    private static let maximumImageSide = 960

    private struct LabelRegion {
        let label: String
        let bounds: CGRect
        let value: CGRect?
    }

    private struct AgreedRegion {
        let accurateIndex: Int
        let fastIndex: Int
        let bounds: CGRect
    }

    static func redact(_ image: CGImage) throws -> Data {
        let accurate = recognitionRequest(.accurate)
        let fast = recognitionRequest(.fast)
        let textRegions = VNDetectTextRectanglesRequest()
        let faces = VNDetectFaceRectanglesRequest()
        let handler = VNImageRequestHandler(cgImage: image, orientation: .up)
        do {
            try handler.perform([accurate, fast, textRegions, faces])
        } catch {
            throw EvidenceRedactionError.redactionFailed
        }
        guard let accurateText = accurate.results, let fastText = fast.results,
              let textRegions = textRegions.results, let faces = faces.results else {
            throw EvidenceRedactionError.redactionFailed
        }

        let scale = outputScale(image)
        let twoPixels = CGSize(width: 2 / (CGFloat(image.width) * scale),
                               height: 2 / (CGFloat(image.height) * scale))
        let accurateLabels = accurateText.map { labelRegion(in: $0, minimumGap: twoPixels.width, confidence: 0.75) }
        // The fast recognizer can report 0.5 even on clean text. Both passes must still agree on the label and its location.
        let fastLabels = fastText.map { labelRegion(in: $0, minimumGap: twoPixels.width, confidence: 0.50, fastPass: true) }
        let agreed = agreedRegions(accurate: accurateLabels, fast: fastLabels, tolerance: twoPixels)
        let accurateIndices = Set(agreed.map(\.accurateIndex))
        let fastIndices = Set(agreed.map(\.fastIndex))
        let accurateBounds = accurateText.map(\.boundingBox)
        let fastBounds = fastText.map(\.boundingBox)
        let masks = textRegions.map(\.boundingBox) + accurateBounds + fastBounds
        let protected = faces.map(\.boundingBox) +
            accurateBounds.indices.filter { !accurateIndices.contains($0) }.map { accurateBounds[$0] } +
            fastBounds.indices.filter { !fastIndices.contains($0) }.map { fastBounds[$0] }
        let values = agreed.compactMap { accurateLabels[$0.accurateIndex]?.value } +
            agreed.compactMap { fastLabels[$0.fastIndex]?.value }
        return try render(image, covering: masks, keeping: agreed.map(\.bounds),
                          protected: protected, values: values)
    }

    private static func recognitionRequest(_ level: VNRequestTextRecognitionLevel) -> VNRecognizeTextRequest {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = level
        request.usesLanguageCorrection = false
        request.automaticallyDetectsLanguage = true
        request.minimumTextHeight = 0.006
        return request
    }

    private static func labelRegion(in observation: VNRecognizedTextObservation, minimumGap: CGFloat,
                                    confidence: VNConfidence, fastPass: Bool = false) -> LabelRegion? {
        guard let candidate = observation.topCandidates(1).first, candidate.confidence >= confidence,
              let match = VisualEvidenceLabels.match(in: candidate.string, fastPass: fastPass),
              let label = horizontalBounds(of: match.labelRange, in: candidate) else { return nil }
        let line = observation.boundingBox
        let bounds = label.intersection(line)
        guard usable(bounds), area(bounds) >= 0.80 * area(label) else { return nil }
        guard let valueRange = match.valueRange else {
            // A standalone label must cover most of the line identified by this pass.
            guard area(bounds) >= 0.70 * area(line) else { return nil }
            return LabelRegion(label: match.label, bounds: bounds, value: nil)
        }
        guard let value = horizontalBounds(of: valueRange, in: candidate),
              value.minX - label.maxX >= minimumGap,
              label.minX - line.minX <= 0.35 * label.width,
              min(label.maxY, value.maxY) - max(label.minY, value.minY) >= 0.55 * min(label.height, value.height)
        else { return nil }

        // Protect the entire remainder of the line, including glyphs beyond the measured value.
        let tail = CGRect(x: value.minX, y: min(line.minY, value.minY),
                          width: max(line.maxX, value.maxX) - value.minX,
                          height: max(line.maxY, value.maxY) - min(line.minY, value.minY))
        return LabelRegion(label: match.label, bounds: bounds, value: tail)
    }

    private static func horizontalBounds(of range: Range<String.Index>, in candidate: VNRecognizedText) -> CGRect? {
        guard let region = try? candidate.boundingBox(for: range), usable(region.boundingBox),
              abs(region.topRight.y - region.topLeft.y) <= 0.18 * region.boundingBox.height,
              abs(region.bottomRight.y - region.bottomLeft.y) <= 0.18 * region.boundingBox.height else { return nil }
        return region.boundingBox
    }

    private static func agreedRegions(accurate: [LabelRegion?], fast: [LabelRegion?], tolerance: CGSize) -> [AgreedRegion] {
        let accurateIndices = accurate.indices.filter { accurate[$0] != nil }
        let fastIndices = fast.indices.filter { fast[$0] != nil }
        // Dense screens still get full text coverage without an unbounded region matching pass.
        guard accurateIndices.count <= 128, fastIndices.count <= 128 else { return [] }
        let fastByLabel = Dictionary(grouping: fastIndices) { fast[$0]?.label }
        var proposals: [AgreedRegion] = []
        for index in accurateIndices {
            guard let first = accurate[index] else { continue }
            let matches: [AgreedRegion] = fastByLabel[first.label, default: []].compactMap { fastIndex in
                guard let second = fast[fastIndex] else { return nil }
                let overlap = first.bounds.intersection(second.bounds)
                guard usable(overlap), overlap.width >= 0.74 * max(first.bounds.width, second.bounds.width),
                      overlap.height >= 0.64 * max(first.bounds.height, second.bounds.height) else { return nil }
                // Keep glyph edges within two output pixels of agreement and inside at least one label box.
                let window = overlap.insetBy(dx: -tolerance.width, dy: -tolerance.height)
                let firstCrop = window.intersection(first.bounds)
                let secondCrop = window.intersection(second.bounds)
                var bounds = area(firstCrop) >= area(secondCrop) ? firstCrop : secondCrop
                if let valueStart = [first.value?.minX, second.value?.minX].compactMap({ $0 }).min() {
                    bounds.size.width = min(bounds.maxX, valueStart - tolerance.width) - bounds.minX
                }
                guard usable(bounds) else { return nil }
                return AgreedRegion(accurateIndex: index, fastIndex: fastIndex, bounds: bounds)
            }
            if matches.count == 1, let proposal = matches.first { proposals.append(proposal) }
        }
        let fastCounts = Dictionary(grouping: proposals, by: \.fastIndex)
        return proposals.enumerated().compactMap { index, proposal in
            // Even-odd clipping requires disjoint openings. Leave ambiguous overlaps fully painted.
            guard fastCounts[proposal.fastIndex]?.count == 1,
                  !proposals.enumerated().contains(where: { other, region in
                      other != index && usable(region.bounds.intersection(proposal.bounds))
                  }) else { return nil }
            return proposal
        }
    }

    private static func outputScale(_ image: CGImage) -> CGFloat {
        min(1, CGFloat(maximumImageSide) / CGFloat(max(image.width, image.height)))
    }

    private static func usable(_ rect: CGRect) -> Bool {
        [rect.minX, rect.minY, rect.width, rect.height].allSatisfy(\.isFinite) && rect.width > 0 && rect.height > 0
    }

    private static func area(_ rect: CGRect) -> CGFloat { rect.width * rect.height }

    static func render(_ image: CGImage, covering regions: [CGRect], keeping openings: [CGRect] = [],
                       protected: [CGRect] = [], values: [CGRect] = []) throws -> Data {
        let scale = outputScale(image)
        let width = max(1, Int((CGFloat(image.width) * scale).rounded()))
        let height = max(1, Int((CGFloat(image.height) * scale).rounded()))
        let canvas = CGRect(x: 0, y: 0, width: width, height: height)
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let drawing = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                 bytesPerRow: 0, space: colorSpace, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
            throw EvidenceRedactionError.redactionFailed
        }

        func pixels(_ region: CGRect) throws -> CGRect {
            guard usable(region) else { throw EvidenceRedactionError.redactionFailed }
            return CGRect(x: region.minX * canvas.width, y: region.minY * canvas.height,
                          width: region.width * canvas.width, height: region.height * canvas.height)
        }

        func mask(_ region: CGRect, value: Bool = false) throws -> CGRect {
            let rect = try pixels(region)
            let margin = value ? 1.5 : max(5, min(24, rect.width * 0.16))
            let padded = rect.insetBy(dx: -margin, dy: -max(5, rect.height * 0.24)).integral.intersection(canvas)
            guard usable(padded) else { throw EvidenceRedactionError.redactionFailed }
            return padded
        }

        drawing.interpolationQuality = .high
        drawing.draw(image, in: canvas)
        drawing.setFillColor(red: 0.16, green: 0.18, blue: 0.21, alpha: 1)
        let keep = try openings.compactMap { opening -> CGRect? in
            let rect = try pixels(opening).intersection(canvas)
            guard usable(rect) else { return nil }
            // Round inward so the clip never exposes any pixel outside the agreed rectangle.
            let safe = CGRect(x: ceil(rect.minX), y: ceil(rect.minY),
                              width: floor(rect.maxX) - ceil(rect.minX), height: floor(rect.maxY) - ceil(rect.minY))
            return safe.width > 0 && safe.height > 0 ? safe : nil
        }
        drawing.saveGState()
        if !keep.isEmpty {
            drawing.addRect(canvas)
            drawing.addRects(keep)
            drawing.clip(using: .evenOdd)
        }
        drawing.fill(try regions.map { try mask($0) })
        drawing.restoreGState()
        drawing.fill(try protected.map { try mask($0) })
        drawing.fill(try values.map { try mask($0, value: true) })
        guard let sanitized = drawing.makeImage() else { throw EvidenceRedactionError.redactionFailed }

        let jpeg = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(jpeg, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw EvidenceRedactionError.redactionFailed
        }
        // Re-encode the new raster, then remove ancillary markers ImageIO may synthesize.
        CGImageDestinationAddImage(destination, sanitized, [kCGImageDestinationLossyCompressionQuality: 0.84] as CFDictionary)
        guard CGImageDestinationFinalize(destination), jpeg.length > 0, jpeg.length <= 512 * 1024 else {
            throw EvidenceRedactionError.redactionFailed
        }
        return try withoutMetadata(Data(referencing: jpeg))
    }

    private static func withoutMetadata(_ jpeg: Data) throws -> Data {
        let bytes = [UInt8](jpeg)
        guard bytes.starts(with: [0xff, 0xd8]), bytes.suffix(2).elementsEqual([0xff, 0xd9]) else {
            throw EvidenceRedactionError.redactionFailed
        }
        var result = Data(bytes.prefix(2))
        var offset = 2
        while offset + 4 <= bytes.count {
            let marker = bytes[offset + 1]
            let length = Int(bytes[offset + 2]) * 256 + Int(bytes[offset + 3])
            let end = offset + 2 + length
            guard bytes[offset] == 0xff, length >= 2, end <= bytes.count else {
                throw EvidenceRedactionError.redactionFailed
            }
            if marker == 0xda {
                result.append(contentsOf: bytes[offset...])
                return result
            }
            // APP and comment markers can contain EXIF, XMP, profiles, thumbnails or other side data.
            if !(0xe0...0xef).contains(marker), marker != 0xfe {
                result.append(contentsOf: bytes[offset..<end])
            }
            offset = end
        }
        throw EvidenceRedactionError.redactionFailed
    }
}
