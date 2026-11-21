import AppKit
import Carbon
import CryptoKit
import EventKit
import Foundation
import Security

struct BridgeError: Error { let code: String }
let store = EKEventStore()
let keyQuery: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: "com.mistralhackathon.aidiscovery.workflows", kSecAttrAccount as String: "mistral"]
func hasCalendar() -> Bool { EKEventStore.authorizationStatus(for: .event).rawValue == 3 }
func calendarStatus() -> String {
    switch EKEventStore.authorizationStatus(for: .event).rawValue {
    case 0: return "not_determined"
    case 1: return "restricted"
    case 2: return "denied"
    case 3: return "authorized"
    default: return "write_only"
    }
}
func permissions() -> [String: Any] {
    ["mail": mailAllowed(), "calendar": hasCalendar(), "calendar_status": calendarStatus(), "timezone": TimeZone.current.identifier]
}
func mailAllowed() -> Bool {
    guard let target = NSAppleEventDescriptor(descriptorType: typeApplicationBundleID, data: Data("com.apple.mail".utf8)) else { return false }
    return AEDeterminePermissionToAutomateTarget(target.aeDesc, typeWildCard, typeWildCard, false) == noErr
}
func mail(_ handler: String, _ arguments: [NSAppleEventDescriptor] = []) throws -> NSAppleEventDescriptor {
    guard let path = Bundle.main.path(forResource: "Mail", ofType: "scpt"),
          let script = NSAppleScript(contentsOf: URL(fileURLWithPath: path), error: nil) else { throw BridgeError(code: "helper_missing") }
    let event = NSAppleEventDescriptor(eventClass: AEEventClass(kASAppleScriptSuite), eventID: AEEventID(kASSubroutineEvent),
        targetDescriptor: nil, returnID: AEReturnID(kAutoGenerateReturnID), transactionID: AETransactionID(kAnyTransactionID))
    event.setParam(NSAppleEventDescriptor(string: handler), forKeyword: AEKeyword(keyASSubroutineName))
    let params = NSAppleEventDescriptor.list()
    for (i, argument) in arguments.enumerated() { params.insert(argument, at: i + 1) }
    event.setParam(params, forKeyword: AEKeyword(keyDirectObject))
    var error: NSDictionary?
    let result = script.executeAppleEvent(event, error: &error)
    if let error {
        let number = error[NSAppleScript.errorNumber] as? Int
        throw BridgeError(code: number == -1743 ? "mail_permission" : number == 5001 ? "inbox_backlog" : number == -1712 ? "mail_timeout" : "mail_error_\(number ?? 0)")
    }
    return result
}
func key(_ operation: String, _ input: [String: Any]) throws -> [String: Any] {
    if operation == "save_key" {
        guard let value = input["value"] as? String, !value.isEmpty, value.count <= 4096 else { throw BridgeError(code: "invalid_input") }
        let data = Data(value.utf8)
        let status = SecItemUpdate(keyQuery as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var query = keyQuery; query[kSecValueData as String] = data
            guard SecItemAdd(query as CFDictionary, nil) == errSecSuccess else { throw BridgeError(code: "keychain_error") }
        } else if status != errSecSuccess { throw BridgeError(code: "keychain_error") }
        return ["saved": true]
    }
    var query = keyQuery
    query[operation == "key_status" ? kSecReturnAttributes as String : kSecReturnData as String] = true
    query[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    if status == errSecItemNotFound { return operation == "key_status" ? ["configured": false] : ["value": ""] }
    guard status == errSecSuccess else { throw BridgeError(code: "keychain_error") }
    if operation == "key_status" { return ["configured": true] }
    return ["value": String(data: result as? Data ?? Data(), encoding: .utf8) ?? ""]
}

func meetingCalendar(_ operation: String, _ input: [String: Any]) throws -> [String: Any] {
    guard hasCalendar() else { throw BridgeError(code: "calendar_permission") }
    let calendars = store.calendars(for: .event).filter(\.allowsContentModifications)
    if operation == "calendar_targets" {
        return ["default_id": store.defaultCalendarForNewEvents?.calendarIdentifier ?? "",
                "calendars": calendars.map { ["id": $0.calendarIdentifier, "title": $0.title] }]
    }
    guard let calendarID = input["calendar_id"] as? String,
          let calendar = calendars.first(where: { $0.calendarIdentifier == calendarID }) else {
        throw BridgeError(code: "invalid_calendar")
    }
    guard let start = input["start"] as? Double, let end = input["end"] as? Double,
          start.isFinite, end.isFinite, end > start, end - start <= 4 * 3600,
          let zone = input["timezone"] as? String, let timeZone = TimeZone(identifier: zone),
          let title = input["title"] as? String, title == "Meeting",
          let token = input["token"] as? String, token.range(of: #"^[a-f0-9]{64}$"#, options: .regularExpression) != nil,
          let marker = URL(string: "chonkytasks://approved-meeting/" + token) else {
        throw BridgeError(code: "invalid_event")
    }
    let begins = Date(timeIntervalSince1970: start), finishes = Date(timeIntervalSince1970: end)
    let events = store.events(matching: store.predicateForEvents(withStart: begins, end: finishes, calendars: nil))
    if let existing = events.first(where: { $0.url == marker }) {
        guard existing.calendar.calendarIdentifier == calendarID, existing.title == title,
              existing.startDate == begins, existing.endDate == finishes else { throw BridgeError(code: "invalid_event") }
        return ["available": true, "event_id": existing.eventIdentifier ?? ""]
    }
    if operation == "find_event" { return [:] }
    guard begins > Date() else { throw BridgeError(code: "invalid_event") }
    let available = !events.contains { event in
        event.status != .canceled && event.availability != .free && event.startDate < finishes && begins < event.endDate
    }
    if operation == "check_event" { return ["available": available] }
    guard available else { throw BridgeError(code: "calendar_conflict") }

    let event = EKEvent(eventStore: store)
    event.title = title
    event.calendar = calendar
    event.startDate = begins
    event.endDate = finishes
    event.timeZone = timeZone
    event.url = marker
    do { try store.save(event, span: .thisEvent, commit: true) }
    catch { throw BridgeError(code: "event_unconfirmed") }
    guard let identifier = event.eventIdentifier, let saved = store.event(withIdentifier: identifier),
          saved.url == marker, saved.startDate == begins, saved.endDate == finishes,
          saved.calendar.calendarIdentifier == calendarID else { throw BridgeError(code: "event_unconfirmed") }
    return ["available": true, "event_id": identifier]
}
func run(_ input: [String: Any]) throws -> [String: Any] {
    let operation = input["operation"] as? String ?? "status"
    if ["save_key", "get_key", "key_status"].contains(operation) { return try key(operation, input) }
    if operation == "status" { return permissions() }
    if ["calendar_targets", "check_event", "create_event", "find_event"].contains(operation) {
        return try meetingCalendar(operation, input)
    }
    if operation == "permissions" {
        let service = input["service"] as? String ?? "both"
        guard ["both", "mail", "calendar"].contains(service) else { throw BridgeError(code: "invalid_input") }
        NSApp.activate(ignoringOtherApps: true)
        if service != "mail" && !hasCalendar() {
            var finished = false
            let completionLock = NSLock()
            let completion: (Bool, Error?) -> Void = { _, _ in
                completionLock.lock(); finished = true; completionLock.unlock()
            }
            if #available(macOS 14, *) { store.requestFullAccessToEvents(completion: completion) }
            else { store.requestAccess(to: .event, completion: completion) }
            let deadline = Date().addingTimeInterval(120)
            while Date() < deadline {
                completionLock.lock(); let done = finished; completionLock.unlock()
                if done { break }
                RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            }
        }
        // Ask TCC explicitly: a harmless name lookup can be answered by Mail
        // without requesting permission for reading messages and creating drafts.
        if service != "calendar", let target = NSAppleEventDescriptor(descriptorType: typeApplicationBundleID, data: Data("com.apple.mail".utf8)) {
            _ = AEDeterminePermissionToAutomateTarget(target.aeDesc, typeWildCard, typeWildCard, true)
        }
        return permissions()
    }
    if operation == "open_settings" {
        guard let pane = input["pane"] as? String, ["Calendars", "Automation"].contains(pane),
              let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_" + pane) else { throw BridgeError(code: "invalid_input") }
        NSWorkspace.shared.open(url)
        return ["opened": true]
    }
    if operation == "busy_times" {
        guard hasCalendar() else { throw BridgeError(code: "calendar_permission") }
        guard let start = input["start"] as? Double, let end = input["end"] as? Double,
              end > start, end - start <= 32 * 86400 else { throw BridgeError(code: "invalid_input") }
        let events = store.events(matching: store.predicateForEvents(withStart: Date(timeIntervalSince1970: start), end: Date(timeIntervalSince1970: end), calendars: nil))
        let busy = events.filter { $0.status != .canceled && $0.availability != .free }.map {
            ["start": $0.startDate.timeIntervalSince1970, "end": $0.endDate.timeIntervalSince1970]
        }
        return ["busy": busy, "timezone": TimeZone.current.identifier]
    }
    guard mailAllowed() else { throw BridgeError(code: "mail_permission") }
    if operation == "inbox_info" {
        let info = try mail("inboxInfo")
        return ["accounts": info.atIndex(1)?.int32Value ?? 0, "messages": info.atIndex(2)?.int32Value ?? 0]
    }
    if operation == "inbox" {
        guard let since = input["since"] as? Double else { throw BridgeError(code: "invalid_input") }
        let rows = try mail("recentMessages", [NSAppleEventDescriptor(date: Date(timeIntervalSince1970: since))])
        var messages: [[String: Any]] = []
        if rows.numberOfItems > 0 {
            for i in 1...rows.numberOfItems {
                guard let row = rows.atIndex(i), let date = row.atIndex(4)?.dateValue else { continue }
                messages.append(["id": row.atIndex(1)?.int32Value ?? 0, "message_id": row.atIndex(2)?.stringValue ?? "",
                    "subject": row.atIndex(3)?.stringValue ?? "", "received_at": date.timeIntervalSince1970])
            }
        }
        return ["messages": messages.sorted { ($0["received_at"] as! Double) < ($1["received_at"] as! Double) }]
    }
    guard let id = input["id"] as? Int32, id > 0 else { throw BridgeError(code: "invalid_input") }
    if operation == "draft_preview" || operation == "send_draft" {
        let snapshot = try mail("draftDetails", [NSAppleEventDescriptor(int32: id)])
        func addresses(_ index: Int) -> [String] {
            guard let list = snapshot.atIndex(index), list.numberOfItems > 0 else { return [] }
            return (1...list.numberOfItems).compactMap { list.atIndex($0)?.stringValue }
        }
        var preview: [String: Any] = ["id": id, "message_id": snapshot.atIndex(2)?.stringValue ?? "",
            "subject": snapshot.atIndex(3)?.stringValue ?? "", "sender": snapshot.atIndex(4)?.stringValue ?? "",
            "to": addresses(5), "cc": addresses(6), "bcc": addresses(7),
            "body": snapshot.atIndex(8)?.stringValue ?? "", "attachments": snapshot.atIndex(9)?.int32Value ?? 0]
        let bytes = try JSONSerialization.data(withJSONObject: preview, options: [.sortedKeys])
        let revision = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        preview["revision"] = revision
        if operation == "draft_preview" { return preview }
        guard input["revision"] as? String == revision else { throw BridgeError(code: "draft_changed") }
        guard let originalID = input["original_id"] as? Int32, originalID > 0,
              let originalMessageID = input["original_message_id"] as? String, !originalMessageID.isEmpty,
              !addresses(5).isEmpty else { throw BridgeError(code: "invalid_input") }
        let accepted = try mail("sendReviewedDraft", [NSAppleEventDescriptor(int32: id),
            NSAppleEventDescriptor(int32: originalID), NSAppleEventDescriptor(string: originalMessageID), snapshot])
        guard accepted.booleanValue else { throw BridgeError(code: "send_unconfirmed") }
        return ["accepted": true]
    }
    if operation == "message" {
        let maximumLength: Int32 = input["full_body"] as? Bool == true ? 60000 : 12000
        let result = try mail("messageContent", [NSAppleEventDescriptor(int32: id), NSAppleEventDescriptor(int32: maximumLength)])
        return ["subject": result.atIndex(1)?.stringValue ?? "", "body": result.atIndex(2)?.stringValue ?? "",
            "message_id": result.atIndex(3)?.stringValue ?? "", "sender": result.atIndex(4)?.stringValue ?? "",
            "body_truncated": result.atIndex(5)?.booleanValue ?? true]
    }
    if operation == "draft" || operation == "recover_draft" {
        guard let text = input["body"] as? String, !text.isEmpty, text.count <= 12000,
              let messageID = input["message_id"] as? String, !messageID.isEmpty else { throw BridgeError(code: "invalid_input") }
        if operation == "recover_draft" {
            guard let since = input["since"] as? Double else { throw BridgeError(code: "invalid_input") }
            let result = try mail("recoverReply", [NSAppleEventDescriptor(int32: id), NSAppleEventDescriptor(string: messageID),
                NSAppleEventDescriptor(string: text), NSAppleEventDescriptor(date: Date(timeIntervalSince1970: since))])
            return ["draft_id": String(result.int32Value)]
        }
        let result = try mail("saveReply", [NSAppleEventDescriptor(int32: id), NSAppleEventDescriptor(string: messageID), NSAppleEventDescriptor(string: text)])
        return ["draft_id": String(result.int32Value)]
    }
    throw BridgeError(code: "invalid_input")
}
do {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard data.count < 100_000, let input = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw BridgeError(code: "invalid_input") }
    _ = NSApplication.shared
    NSApp.setActivationPolicy(.accessory)
    let limit: TimeInterval = input["operation"] as? String == "permissions" ? 160 : 65
    DispatchQueue.global().asyncAfter(deadline: .now() + limit) {
        FileHandle.standardOutput.write(Data("{\"error\":\"native_timeout\"}".utf8))
        exit(1)
    }
    // Enter AppKit's event loop before sending Apple events. LaunchServices
    // applications need their reply port serviced during synchronous scripts.
    DispatchQueue.main.async {
        do {
            let result = try run(input)
            FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]))
            exit(0)
        } catch {
            let code = (error as? BridgeError)?.code ?? "native_error"
            FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: ["error": code]))
            exit(1)
        }
    }
    NSApp.run()
} catch {
    let code = (error as? BridgeError)?.code ?? "native_error"
    FileHandle.standardOutput.write(try! JSONSerialization.data(withJSONObject: ["error": code]))
    exit(1)
}
