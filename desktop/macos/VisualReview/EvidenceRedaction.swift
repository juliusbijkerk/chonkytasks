import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

enum EvidenceRedactionError: Error { case redactionFailed }

enum EvidenceRedaction {
    private static let maximumImageSide = 960

    static func redact(_ image: CGImage) throws -> Data {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.automaticallyDetectsLanguage = true
        request.minimumTextHeight = 0.006
        let handler = VNImageRequestHandler(cgImage: image, orientation: .up)
        do {
            try handler.perform([request])
        } catch {
            throw EvidenceRedactionError.redactionFailed
        }
        guard let observations = request.results else { throw EvidenceRedactionError.redactionFailed }

        let masks = try observations.flatMap { observation -> [CGRect] in
            guard let text = observation.topCandidates(1).first else { return [] }
            return try SensitiveEvidence.ranges(in: text.string).map { range in
                guard let region = try? text.boundingBox(for: range), usable(region.boundingBox) else {
                    throw EvidenceRedactionError.redactionFailed
                }
                return region.boundingBox
            }
        }
        return try render(image, covering: masks)
    }

    private static func usable(_ rect: CGRect) -> Bool {
        [rect.minX, rect.minY, rect.width, rect.height].allSatisfy(\.isFinite) && rect.width > 0 && rect.height > 0
    }

    static func render(_ image: CGImage, covering regions: [CGRect]) throws -> Data {
        let scale = min(1, CGFloat(maximumImageSide) / CGFloat(max(image.width, image.height)))
        let width = max(1, Int((CGFloat(image.width) * scale).rounded()))
        let height = max(1, Int((CGFloat(image.height) * scale).rounded()))
        let canvas = CGRect(x: 0, y: 0, width: width, height: height)
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let drawing = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                 bytesPerRow: 0, space: colorSpace, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
            throw EvidenceRedactionError.redactionFailed
        }

        drawing.interpolationQuality = .high
        drawing.draw(image, in: canvas)
        drawing.setFillColor(red: 0, green: 0, blue: 0, alpha: 1)
        for region in regions {
            guard usable(region) else { throw EvidenceRedactionError.redactionFailed }
            let rect = CGRect(x: region.minX * canvas.width, y: region.minY * canvas.height,
                              width: region.width * canvas.width, height: region.height * canvas.height)
            // Pad by one output pixel to cover glyph edges without obscuring adjacent words.
            let padded = rect.insetBy(dx: -1, dy: -1).integral.intersection(canvas)
            guard usable(padded) else { throw EvidenceRedactionError.redactionFailed }
            drawing.fill(padded)
        }
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
