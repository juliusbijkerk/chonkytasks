import Foundation

/// Fixed English interface vocabulary for locating original label pixels locally.
enum VisualEvidenceLabels {
    struct Match {
        let label: String
        let labelRange: Range<String.Index>
        let valueRange: Range<String.Index>?
    }

    private static let fields = lookup([
        "From", "Subject", "Body", "Message", "Title", "Date", "Start", "Start time", "Duration",
        "Time zone", "Location", "Task", "Due", "Due date", "Invoice", "Invoice ID", "Invoice number",
        "Invoice #", "Invoice date", "Vendor", "Total", "Invoice total", "Folder", "Order", "Order ID",
        "Order number", "Order #", "Customer", "SKU", "Quantity", "Queue", "Workbook", "Source workbook",
        "Group by", "Column", "Measure", "Filter", "Item", "Status", "Amount", "Rows", "Row count"
    ])

    private static let captions = lookup([
        "Mail", "Inbox", "Calendar", "Tasks", "Notes", "Spreadsheets", "Invoice Desk", "Order Desk",
        "Browser", "Docs", "New event", "Create event", "Event created", "New task", "Add task", "Task added",
        "New note", "Save note", "Note saved", "Create report", "Report created", "Supplier invoice", "File invoice",
        "Invoice filed", "Order request", "New order entry", "Create order draft", "Order recorded", "Cancel",
        "Saved calendar", "Saved tasks", "Saved notes", "No saved items", "Recent work"
    ])

    static func match(in text: String, fastPass: Bool = false) -> Match? {
        guard text.utf8.count <= 2_048,
              let start = text.firstIndex(where: { !$0.isWhitespace }),
              let last = text.lastIndex(where: { !$0.isWhitespace }) else { return nil }
        let end = text.index(after: last)
        let line = start..<end
        let lineKey = key(for: text[line])
        // Fast Vision can insert a space inside this word. Preserve nothing unless Accurate and geometry agree too.
        let wholeLabel = fastPass && lineKey == "invoice f iled" ? "Invoice filed" : wholeLabels[lineKey]
        if let label = wholeLabel {
            return Match(label: label, labelRange: line, valueRange: nil)
        }

        if let separator = text[line].firstIndex(of: ":"),
           let label = fields[key(for: text[start..<separator])] {
            let after = text.index(after: separator)
            let valueStart = text[after..<end].firstIndex(where: { !$0.isWhitespace })
            return Match(label: label, labelRange: start..<after, valueRange: valueStart.map { $0..<end })
        }

        // Allow a field and value separated by whitespace. Do not split longer known captions.
        var field: Match?
        for boundary in text[line].indices where text[boundary].isWhitespace {
            let prefix = text[start..<boundary]
            if prefix.utf8.count > 40 { break }
            let identity = key(for: prefix)
            if captions[identity] != nil { return nil }
            guard let label = fields[identity],
                  let valueStart = text[boundary..<end].firstIndex(where: { !$0.isWhitespace }),
                  let finalLabelCharacter = prefix.lastIndex(where: { !$0.isWhitespace }) else { continue }
            field = Match(label: label, labelRange: start..<text.index(after: finalLabelCharacter),
                          valueRange: valueStart..<end)
        }
        return field
    }

    private static let wholeLabels = captions.merging(fields) { caption, _ in caption }

    private static func key(for text: Substring) -> String {
        let label = text.trimmingCharacters(in: .whitespaces)
        guard !label.isEmpty, label.utf8.count <= 40, label.utf8.allSatisfy({
            (65...90).contains($0) || (97...122).contains($0) || $0 == 32 || $0 == 35
        }) else { return "" }
        return label.lowercased()
    }

    private static func lookup(_ labels: [String]) -> [String: String] {
        Dictionary(uniqueKeysWithValues: labels.map { ($0.lowercased(), $0) })
    }
}
