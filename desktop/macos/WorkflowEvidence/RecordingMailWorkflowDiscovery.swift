import Foundation

/// Finds visible email-to-output evidence. The candidate carries timestamps but
/// never retains the values from the email or the result.
enum RecordingMailWorkflowDiscovery {
    private enum Workflow: CaseIterable {
        case calendar, task, note

        var kind: ImportedWorkflowKind {
            switch self {
            case .calendar: return .mailToCalendar
            case .task: return .mailToTask
            case .note: return .mailToNote
            }
        }

        var resultMarkers: Set<String> {
            switch self {
            case .calendar: return ["event created", "calendar event created"]
            case .task: return ["task added", "task created"]
            case .note: return ["note saved"]
            }
        }

        var title: String {
            switch self {
            case .calendar: return "Turn an email into a calendar event"
            case .task: return "Turn an email into a follow-up task"
            case .note: return "Save an email as a note"
            }
        }

        var sourceEvidence: String {
            switch self {
            case .calendar: return "Email displayed meeting details"
            case .task: return "Email displayed a task and due date"
            case .note: return "Email displayed a sender, subject and message"
            }
        }

        var resultEvidence: String {
            switch self {
            case .calendar: return "Calendar confirmed an event with matching details"
            case .task: return "Tasks confirmed a follow-up with the same title and due date"
            case .note: return "Notes confirmed a note with the same subject and sender"
            }
        }

        var summaryResult: String {
            switch self {
            case .calendar: return "a calendar event"
            case .task: return "a follow-up task"
            case .note: return "a saved note"
            }
        }


    }

    private enum Field: String {
        case from, subject, title, date, start, duration, location, task, due, body
        case timeZone = "time zone"

        init?(label: String) {
            switch label {
            case "due date": self = .due
            case "timezone": self = .timeZone
            case "message": self = .body
            default: self.init(rawValue: label)
            }
        }
    }

    private struct Fields {
        private var values: [Field: String] = [:]
        private var ambiguous = Set<Field>()

        subscript(_ field: Field) -> String? {
            ambiguous.contains(field) ? nil : values[field]
        }

        init(lines: [RecognizedScreenLine]) {
            for line in lines {
                let parts = line.text.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
                guard let label = parts.first, let field = Field(label: normalize(String(label))) else { continue }
                let inline = parts.count > 1 ? normalize(String(parts[1])) : ""
                let value = inline.isEmpty ? Self.valueBeside(line, in: lines) : inline
                guard let value, !value.isEmpty else { continue }
                if let previous = values[field], previous != value {
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
            }.min { $0.box.minX < $1.box.minX }.map { normalize($0.text) }
        }
    }

    private struct Email: Hashable {
        let from: String
        let subject: String

        init?(_ fields: Fields) {
            guard let from = fields[.from], let subject = fields[.subject] else { return nil }
            self.from = from
            self.subject = subject
        }
    }

    private struct Signature: Hashable {
        let workflow: Workflow
        let matchingValues: [String]

        init?(_ workflow: Workflow, fields: Fields, isSource: Bool) {
            self.workflow = workflow
            switch workflow {
            case .calendar:
                guard let title = fields[.title], let date = fields[.date], validDate(date),
                      let start = fields[.start], validTime(start),
                      let duration = fields[.duration], let minutes = durationMinutes(duration),
                      let zone = fields[.timeZone], let identifier = validZone(zone),
                      let location = fields[.location] else { return nil }
                matchingValues = [title, date, start, String(minutes), identifier, location]
            case .task:
                guard let task = fields[.task], let due = fields[.due], validDate(due) else { return nil }
                matchingValues = [task, due]
            case .note:
                guard let email = Email(fields), !isSource || (fields[.body]?.count ?? 0) >= 3 else { return nil }
                matchingValues = [email.subject, email.from]
            }
        }
    }

    private struct ReadingKey: Hashable {
        let clipID: String
        let signature: Signature
    }

    private struct Proof {
        let source: ImportedReplayCue
        let result: ImportedReplayCue
    }

    static func candidates(in frames: [SampledRecordingFrame]) -> [ImportedWorkflowCandidate] {
        var pending: [ReadingKey: [Email: SampledRecordingFrame]] = [:]
        var matched: [Workflow: [Proof]] = [:]
        var matchedSignatures = Set<Signature>()
        for frame in frames.sorted(by: { $0.cue.seconds < $1.cue.seconds }) {
            let labels = Set(frame.lines.map { normalize($0.text).trimmingCharacters(in: CharacterSet(charactersIn: ".!")) })
            let results = Workflow.allCases.filter { !labels.isDisjoint(with: $0.resultMarkers) }
            let fields = Fields(lines: frame.lines)
            if !results.isEmpty {
                for workflow in results {
                    guard let signature = Signature(workflow, fields: fields, isSource: false),
                          !matchedSignatures.contains(signature) else { continue }
                    let key = ReadingKey(clipID: frame.cue.clipID, signature: signature)
                    let sources = pending[key]?.values.filter { earlier in
                        (0.10...120).contains(frame.cue.seconds - earlier.cue.seconds)
                    } ?? []
                    guard sources.count == 1, let source = sources.first else { continue }
                    matched[workflow, default: []].append(Proof(source: source.cue, result: frame.cue))
                    matchedSignatures.insert(signature)
                    pending.removeValue(forKey: key)
                }
            } else if let email = Email(fields) {
                for workflow in Workflow.allCases {
                    guard let signature = Signature(workflow, fields: fields, isSource: true),
                          !matchedSignatures.contains(signature) else { continue }
                    let key = ReadingKey(clipID: frame.cue.clipID, signature: signature)
                    // Keep the earliest matching screen within the active transition
                    // window so the replay starts with a readable source frame.
                    if let earlier = pending[key]?[email], frame.cue.seconds - earlier.cue.seconds <= 115 { continue }
                    pending[key, default: [:]][email] = frame
                }
            }
        }
        return Workflow.allCases.compactMap { workflow in
            guard let proofs = matched[workflow], !proofs.isEmpty else { return nil }
            return candidate(workflow, proofs: Array(proofs.prefix(4)))
        }
    }

    private static func candidate(_ workflow: Workflow, proofs: [Proof]) -> ImportedWorkflowCandidate {
        let singular = "One email was followed by \(workflow.summaryResult) with matching details."
        let plural = "\(proofs.count) separate email-to-result matches were visible."
        return ImportedWorkflowCandidate(kind: workflow.kind, title: workflow.title,
            summary: proofs.count == 1 ? singular : plural,
            examples: proofs.map { proof in
                .init(source: proof.source.seconds, result: proof.result.seconds,
                      source_caption: workflow.sourceEvidence, result_caption: workflow.resultEvidence)
            })
    }


    private static func normalize(_ value: String) -> String {
        value.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            .folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive],
                     locale: Locale(identifier: "en_US_POSIX"))
    }

    private static func validDate(_ value: String) -> Bool {
        guard value.range(of: #"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"#, options: .regularExpression) != nil else { return false }
        let components = value.split(separator: "-").compactMap { Int($0) }
        guard components.count == 3 else { return false }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let input = DateComponents(year: components[0], month: components[1], day: components[2])
        guard let date = calendar.date(from: input) else { return false }
        let check = calendar.dateComponents([.year, .month, .day], from: date)
        return input.year == check.year && input.month == check.month && input.day == check.day
    }

    private static func validTime(_ value: String) -> Bool {
        value.range(of: #"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"#, options: .regularExpression) != nil
    }

    private static func durationMinutes(_ value: String) -> Int? {
        let digits = value.prefix(while: \.isNumber)
        guard let count = Int(digits), (1...1440).contains(count) else { return nil }
        switch value.dropFirst(digits.count).trimmingCharacters(in: .whitespaces) {
        case "", "m", "min", "mins", "minute", "minutes": return count
        case "h", "hr", "hrs", "hour", "hours": return count <= 24 ? count * 60 : nil
        default: return nil
        }
    }

    private static func validZone(_ value: String) -> String? {
        if let zone = TimeZone(abbreviation: value.uppercased()) { return zone.identifier }
        return TimeZone.knownTimeZoneIdentifiers.first { $0.lowercased() == value }
    }
}
