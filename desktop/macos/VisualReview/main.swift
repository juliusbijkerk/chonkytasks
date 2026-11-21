import Foundation
import ImageIO

func prepareReview() throws {
    let args = CommandLine.arguments.dropFirst()
    guard (3...74).contains(args.count) else { throw EvidenceRedactionError.redactionFailed }
    let source = URL(fileURLWithPath: args[args.startIndex], isDirectory: true)
    let destination = URL(fileURLWithPath: args[args.startIndex + 1], isDirectory: true)
    let ids = args.dropFirst(2)
    guard source != destination, Set(ids).count == ids.count else { throw EvidenceRedactionError.redactionFailed }
    for id in ids {
        try autoreleasepool {
            guard id.range(of: #"^f[0-9]{4}$"#, options: .regularExpression) != nil,
                  let file = CGImageSourceCreateWithURL(source.appendingPathComponent(id + ".jpg") as CFURL, nil),
                  let image = CGImageSourceCreateImageAtIndex(file, 0, nil) else {
                throw EvidenceRedactionError.redactionFailed
            }
            let bytes = try EvidenceRedaction.redact(image)
            try bytes.write(to: destination.appendingPathComponent(id + ".jpg"), options: .withoutOverwriting)
        }
    }
}

do {
    try prepareReview()
} catch {
    // Never log original paths or recognized text.
    FileHandle.standardError.write(Data("Privacy review could not be prepared.\n".utf8))
    exit(1)
}
