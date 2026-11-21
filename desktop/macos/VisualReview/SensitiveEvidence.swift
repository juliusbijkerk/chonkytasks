import Foundation

/// Locates sensitive values inside recognized lines without altering surrounding text.
enum SensitiveEvidence {
    private struct Rule {
        let regex: NSRegularExpression
        let value: Int
        let validate: (Substring) -> Bool

        init(_ pattern: String, value: Int = 0, options: NSRegularExpression.Options = [],
             validate: @escaping (Substring) -> Bool = { _ in true }) {
            regex = try! NSRegularExpression(pattern: pattern, options: options)
            self.value = value
            self.validate = validate
        }
    }

    private static let rules = [
        Rule(#"(?<![\w.%+\-])[A-Z0-9._%+\-]+@[A-Z0-9\-]+(?:\.[A-Z0-9\-]+)*\.[A-Z]{2,63}(?![\w\-])"#, options: .caseInsensitive),
        Rule(#"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .-]\d{3,4}[ .-]\d{3,4}(?![\w])"#),
        Rule(#"(?<!\w)\d{3}-\d{2}-\d{4}(?!\w)"#),
        Rule(#"(?<!\w)[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D](?!\w)"#),
        Rule(#"(?<![\w.])[0-9](?:[ -]?[0-9]){12,18}(?![\w.]|[ -][0-9])"#, validate: cardNumber),
        Rule(#"(?<![\w-])(?:sk-(?:or-v1-|proj-)?[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{27,}|github_pat_[A-Za-z0-9_]{27,}|xox[baprs]-[A-Za-z0-9-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[A-Za-z0-9_\-]{35}|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{10,})(?![\w-])"#),
        Rule(#"\bBearer\s+([A-Za-z0-9_.+/=\-]{12,})"#, value: 1, options: .caseInsensitive),
        Rule(#"\b(?:password|passwd|passphrase|passcode|api[ -]?key|secret(?: key)?|access token|refresh token|private key|verification code|security code|PIN|CVV|CVC)\s*[:=]\s*(\"[^\"]+\"|'[^']+'|[^\s;|]+)"#, value: 1, options: .caseInsensitive),
        Rule(#"\b(?:card number|credit card number|bank account(?: number)?|account number|sort code|routing number|iban|ssn|social security(?: number)?|national insurance(?: number)?|passport number|phone(?: number)?|mobile number|date of birth|dob|home address|legal name)\s*[:=]\s*(\S[^;|]*?)(?=\s+[A-Za-z][A-Za-z ]{0,24}\s*[:=]|[;|]|$)"#, value: 1, options: .caseInsensitive),
    ]

    static func ranges(in line: String) -> [Range<String.Index>] {
        let bounds = NSRange(line.startIndex..<line.endIndex, in: line)
        let matches = rules.flatMap { rule in
            rule.regex.matches(in: line, range: bounds).compactMap { match -> Range<String.Index>? in
                guard let range = Range(match.range(at: rule.value), in: line),
                      let last = line[range].lastIndex(where: { !$0.isWhitespace }),
                      rule.validate(line[range]) else { return nil }
                return range.lowerBound..<line.index(after: last)
            }
        }.sorted { $0.lowerBound < $1.lowerBound }

        var merged: [Range<String.Index>] = []
        for range in matches {
            if let previous = merged.last, range.lowerBound <= previous.upperBound {
                merged[merged.count - 1] = previous.lowerBound..<max(range.upperBound, previous.upperBound)
            } else {
                merged.append(range)
            }
        }
        return merged
    }

    private static func cardNumber(_ span: Substring) -> Bool {
        let digits = span.compactMap(\.wholeNumberValue)
        guard (13...19).contains(digits.count), Set(digits).count > 1 else { return false }
        let total = digits.reversed().enumerated().reduce(0) { sum, entry in
            let (index, digit) = entry
            let value = index.isMultiple(of: 2) ? digit : digit * 2
            return sum + (value > 9 ? value - 9 : value)
        }
        return total.isMultiple(of: 10)
    }
}
