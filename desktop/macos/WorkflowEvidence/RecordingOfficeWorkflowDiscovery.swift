import Foundation

/// Recognizes concrete source-to-result transitions without retaining the
/// document identifiers, balances, customer names, or spreadsheet entries.
enum RecordingOfficeWorkflowDiscovery {
    private enum Workflow: CaseIterable {
        case invoice, report, order

        var kind: ImportedWorkflowKind {
            switch self {
            case .invoice: return .invoiceFiling
            case .report: return .spreadsheetReport
            case .order: return .orderDraft
            }
        }

        var receipt: String {
            switch self {
            case .invoice: return "invoice filed"
            case .report: return "report created"
            case .order: return "order recorded"
            }
        }

        var title: String {
            switch self {
            case .invoice: return "File an invoice"
            case .report: return "Create a filtered spreadsheet report"
            case .order: return "Prepare an order draft"
            }
        }

        var sourceEvidence: String {
            switch self {
            case .invoice: return "Invoice details were visible"
            case .report: return "Workbook showed the filter and matching input rows"
            case .order: return "Order number, customer, item and quantity were visible"
            }
        }

        var resultEvidence: String {
            switch self {
            case .invoice: return "Filing was confirmed with the same invoice details"
            case .report: return "Report showed the same workbook and filter, with matching row count and total"
            case .order: return "An order was recorded with the same details"
            }
        }

    }

    private enum Field: String {
        case invoice, vendor, date, total, order, customer, sku, quantity, workbook, measure, filter, rows
        case group = "group by"

        init?(label: String) {
            switch label {
            case "invoice id", "invoice number", "invoice #": self = .invoice
            case "invoice date": self = .date
            case "order id", "order number", "order #": self = .order
            case "column": self = .group
            case "source workbook": self = .workbook
            case "row count": self = .rows
            default: self.init(rawValue: label)
            }
        }
    }

    private struct Fields {
        private var values: [Field: String] = [:]
        private var ambiguous = Set<Field>()

        subscript(_ field: Field) -> String? {
            display(field).map(normalize)
        }

        func display(_ field: Field) -> String? {
            ambiguous.contains(field) ? nil : values[field]
        }

        init(_ lines: [RecognizedScreenLine]) {
            for line in lines {
                let words = line.text.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
                guard words.count > 1, let label = words.first, let field = Field(label: normalize(String(label))) else { continue }
                let inline = clean(String(words[1]))
                let value = inline.isEmpty ? Self.valueBeside(line, in: lines) : inline
                guard let value, !value.isEmpty, value.count <= 180 else { continue }
                if let prior = values[field], normalize(prior) != normalize(value) {
                    ambiguous.insert(field)
                } else {
                    values[field] = value
                }
            }
        }

        private static func valueBeside(_ label: RecognizedScreenLine, in lines: [RecognizedScreenLine]) -> String? {
            lines.filter {
                let gap = $0.box.minX - label.box.maxX
                return (-0.002...0.15).contains(gap) && abs($0.box.midY - label.box.midY) < 0.014 &&
                    $0.box.minX > label.box.minX + 0.02
            }.min { $0.box.minX < $1.box.minX }.map { clean($0.text) }
        }
    }

    private struct Signature: Equatable {
        let key: String
        let comparisonValues: [String]

        init?(_ workflow: Workflow, fields: Fields, lines: [RecognizedScreenLine], isResult: Bool) {
            switch workflow {
            case .invoice:
                guard let invoice = fields[.invoice], validIdentifier(invoice),
                      let vendor = fields[.vendor], let date = fields[.date], validDate(date),
                      let total = fields[.total], let amount = money(total) else { return nil }
                key = invoice
                comparisonValues = [vendor, date, decimalString(amount.value), amount.currency]
            case .order:
                guard let order = fields[.order], validIdentifier(order),
                      let customer = fields[.customer], let sku = fields[.sku], validIdentifier(sku),
                      let quantity = fields[.quantity], let count = positiveInteger(quantity) else { return nil }
                key = order
                comparisonValues = [customer, sku, String(count)]
            case .report:
                guard let workbook = fields[.workbook], fields[.group] == "status", fields[.measure] == "amount",
                      let filter = fields[.filter] else { return nil }
                let totals: Totals?
                if isResult {
                    if let rows = fields[.rows], let count = positiveInteger(rows),
                       let total = fields[.total], let value = decimal(total) {
                        totals = Totals(count: count, value: value)
                    } else {
                        totals = nil
                    }
                } else {
                    totals = tableTotals(lines, filter: filter)
                }
                guard let totals else { return nil }
                key = workbook
                comparisonValues = [filter, String(totals.count), decimalString(totals.value)]
            }
        }

        func matches(_ other: Signature) -> Bool {
            key == other.key && comparisonValues == other.comparisonValues
        }
    }

    private struct Totals: Equatable {
        let count: Int
        let value: Decimal

        init(count: Int, value: Decimal) {
            self.count = count
            self.value = value
        }

        init?(_ amounts: [Decimal]) {
            guard !amounts.isEmpty else { return nil }
            count = amounts.count
            value = amounts.reduce(0, +)
        }
    }

    private struct Key: Hashable {
        let clipID: String
        let workflow: Workflow
        let identifier: String
    }

    private struct Sighting {
        let cue: ImportedReplayCue
        let signature: Signature
    }

    private struct Pair {
        let source: Sighting
        let result: ImportedReplayCue
    }

    static func candidates(in frames: [SampledRecordingFrame]) -> [ImportedWorkflowCandidate] {
        var pending: [Key: Sighting] = [:]
        var completedKeys = Set<Key>()
        var findings: [Workflow: [Pair]] = [:]
        for frame in frames.sorted(by: { $0.cue.seconds < $1.cue.seconds }) {
            let fields = Fields(frame.lines)
            // The status heading is an exact known phrase, allowing for OCR
            // spaces inside glyph runs such as typographic ligatures.
            let labels = Set(frame.lines.map { receiptKey($0.text) })
            let receipts = Workflow.allCases.filter { labels.contains(receiptKey($0.receipt)) }
            if !receipts.isEmpty {
                for workflow in receipts {
                    guard let signature = Signature(workflow, fields: fields, lines: frame.lines, isResult: true) else { continue }
                    let key = Key(clipID: frame.cue.clipID, workflow: workflow, identifier: signature.key)
                    guard !completedKeys.contains(key), let earlier = pending.removeValue(forKey: key),
                          earlier.signature.matches(signature),
                          (0.10...180).contains(frame.cue.seconds - earlier.cue.seconds) else { continue }
                    findings[workflow, default: []].append(Pair(source: earlier, result: frame.cue))
                    completedKeys.insert(key)
                }
            } else {
                for workflow in Workflow.allCases {
                    guard let signature = Signature(workflow, fields: fields, lines: frame.lines, isResult: false) else { continue }
                    let key = Key(clipID: frame.cue.clipID, workflow: workflow, identifier: signature.key)
                    guard !completedKeys.contains(key) else { continue }
                    if let earlier = pending[key], earlier.signature.matches(signature),
                       frame.cue.seconds - earlier.cue.seconds <= 175 { continue }
                    pending[key] = Sighting(cue: frame.cue, signature: signature)
                }
            }
        }
        return Workflow.allCases.compactMap { workflow in
            guard let pairs = findings[workflow], !pairs.isEmpty else { return nil }
            return candidate(workflow, pairs: pairs)
        }
    }

    private static func candidate(_ workflow: Workflow, pairs: [Pair]) -> ImportedWorkflowCandidate {
        let summary = pairs.count == 1 ? "One source was followed by a matching completed result." :
            "\(pairs.count) distinct sources were followed by matching completed results."
        return ImportedWorkflowCandidate(kind: workflow.kind, title: workflow.title, summary: summary,
            examples: pairs.prefix(4).map { pair in
                .init(source: pair.source.cue.seconds, result: pair.result.seconds,
                      source_caption: workflow.sourceEvidence, result_caption: workflow.resultEvidence)
            })
    }

    private static func tableTotals(_ lines: [RecognizedScreenLine], filter: String) -> Totals? {
        let headers = lines.filter { normalize($0.text) == "status" }
        let amountHeaders = lines.filter { normalize($0.text) == "amount" }
        var values: [Totals] = []
        for status in headers {
            for amount in amountHeaders where amount.box.midX > status.box.midX + 0.035 &&
                abs(amount.box.midY - status.box.midY) < 0.018 {
                let statuses = lines.filter {
                    normalize($0.text) == filter && abs($0.box.minX - status.box.minX) < 0.07 &&
                        (0.012...0.46).contains(status.box.midY - $0.box.midY)
                }
                let matches = statuses.compactMap { entry in
                    lines.filter {
                        abs($0.box.midY - entry.box.midY) < 0.015 &&
                            abs($0.box.midX - amount.box.midX) < 0.12 &&
                            $0.box.minX > entry.box.maxX + 0.014
                    }.compactMap { decimal(normalize($0.text)) }.first
                }
                if matches.count == statuses.count, let totals = Totals(matches) { values.append(totals) }
            }
        }

        // Some text recognition revisions return the headings and rows as
        // combined lines, so they may not expose individually boxed cells.
        let headerText = lines.filter {
            let cells = normalize($0.text).replacingOccurrences(of: "|", with: " ")
                .split(whereSeparator: \.isWhitespace)
            return Array(cells.suffix(2)) == ["status", "amount"]
        }
        for header in headerText {
            let matches = lines.filter {
                (0.012...0.46).contains(header.box.midY - $0.box.midY) &&
                    abs(header.box.minX - $0.box.minX) < 0.06
            }.compactMap { row -> Decimal? in
                var tokens = normalize(row.text).replacingOccurrences(of: "|", with: " ")
                    .split(whereSeparator: \.isWhitespace)
                guard let amount = tokens.popLast(), let parsed = decimal(String(amount)) else { return nil }
                let statusText = tokens.joined(separator: " ")
                guard statusText == filter || statusText.hasSuffix(" " + filter) else { return nil }
                return parsed
            }
            if let totals = Totals(matches) { values.append(totals) }
        }
        guard let first = values.first, values.allSatisfy({ $0 == first }) else { return nil }
        return first
    }

    private static func receiptKey(_ value: String) -> String {
        normalize(value).trimmingCharacters(in: CharacterSet(charactersIn: ".!"))
            .filter { !$0.isWhitespace }
    }

    private static func clean(_ value: String) -> String {
        value.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    private static func normalize(_ value: String) -> String {
        clean(value).folding(options: [.caseInsensitive, .widthInsensitive], locale: Locale(identifier: "en_US_POSIX"))
    }

    private static func validIdentifier(_ value: String) -> Bool {
        value.range(of: #"^[a-z0-9][a-z0-9._/-]{0,79}$"#, options: .regularExpression) != nil
    }

    private static func positiveInteger(_ value: String) -> Int? {
        guard value.range(of: #"^[0-9]{1,7}$"#, options: .regularExpression) != nil,
              let number = Int(value), (1...1_000_000).contains(number) else { return nil }
        return number
    }

    private static func decimal(_ value: String) -> Decimal? {
        guard value.range(of: #"^-?(?:[0-9]{1,12}|[0-9]{1,3}(?:,[0-9]{3}){1,3})(?:\.[0-9]{1,4})?$"#,
                          options: .regularExpression) != nil else { return nil }
        return Decimal(string: value.replacingOccurrences(of: ",", with: ""), locale: Locale(identifier: "en_US_POSIX"))
    }

    private static func money(_ value: String) -> (value: Decimal, currency: String)? {
        let parts = value.split(whereSeparator: \.isWhitespace)
        guard parts.count == 2, let amount = decimal(String(parts[0])), amount >= 0,
              Locale.commonISOCurrencyCodes.contains(parts[1].uppercased()) else { return nil }
        return (amount, parts[1].uppercased())
    }

    private static func decimalString(_ value: Decimal) -> String {
        NSDecimalNumber(decimal: value).stringValue
    }

    private static func validDate(_ value: String) -> Bool {
        guard value.range(of: #"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"#, options: .regularExpression) != nil else { return false }
        let components = value.split(separator: "-").compactMap { Int($0) }
        guard components.count == 3 else { return false }
        let expected = DateComponents(year: components[0], month: components[1], day: components[2])
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .gmt
        guard let date = calendar.date(from: expected) else { return false }
        let actual = calendar.dateComponents([.year, .month, .day], from: date)
        return expected.year == actual.year && expected.month == actual.month && expected.day == actual.day
    }

}
