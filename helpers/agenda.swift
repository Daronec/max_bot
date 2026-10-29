import Foundation
import EventKit

// Read-only access; EventKit expands recurring events for the requested day.
let todayReminders = CommandLine.arguments.contains("--today-reminders")
let store = EKEventStore()
let now = Date()
let calendar = Calendar.current
let start = calendar.startOfDay(for: now)
let end = calendar.date(byAdding: .day, value: 1, to: start)!
var result: [String: Any] = ["events": [], "reminders": [], "errors": []]
var errors: [String] = []
let iso = ISO8601DateFormatter()
func permission(_ request: (@escaping (Bool, Error?) -> Void) -> Void) -> Bool {
    var granted = false
    let done = DispatchSemaphore(value: 0)
    request { ok, _ in granted = ok; done.signal() }
    return done.wait(timeout: .now() + 45) == .success && granted
}
if !todayReminders {
let eventAccess = permission { callback in store.requestFullAccessToEvents(completion: callback) }
if eventAccess {
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    result["events"] = store.events(matching: predicate).sorted { $0.startDate < $1.startDate }.map { event -> [String: Any] in
        return ["title": event.title ?? "Без названия", "start": iso.string(from: event.startDate),
                "end": iso.string(from: event.endDate), "all_day": event.isAllDay,
                "calendar": event.calendar.title]
    }
} else { errors.append("Календарь: нет разрешения на чтение. Разрешите доступ в настройках macOS.") }
}
let reminderAccess = permission { callback in store.requestFullAccessToReminders(completion: callback) }
if reminderAccess {
    let done = DispatchSemaphore(value: 0)
    var items: [[String: Any]] = []
    let predicate = store.predicateForIncompleteReminders(withDueDateStarting: todayReminders ? start : nil, ending: todayReminders ? end : now, calendars: nil)
    store.fetchReminders(matching: predicate) { reminders in
        items = (reminders ?? []).compactMap { reminder in
            guard !reminder.isCompleted, let components = reminder.dueDateComponents,
                  let due = (components.calendar ?? calendar).date(from: components) else { return nil }
            if todayReminders {
                guard due >= start && due < end else { return nil }
            } else {
                guard due < now else { return nil }
            }
            // A date-only reminder becomes overdue after its day has ended.
            if !todayReminders && components.hour == nil && due >= start { return nil }
            return ["title": reminder.title ?? "Без названия", "due": iso.string(from: due),
                    "all_day": components.hour == nil, "list": reminder.calendar.title]
        }.sorted { ($0["due"] as! String) < ($1["due"] as! String) }
        done.signal()
    }
    if done.wait(timeout: .now() + 45) == .success { result["reminders"] = items }
    else { errors.append("Напоминания: чтение заняло слишком много времени.") }
} else { errors.append("Напоминания: нет разрешения на чтение. Разрешите доступ в настройках macOS.") }
result["errors"] = errors
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
