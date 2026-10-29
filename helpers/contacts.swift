import Foundation
import Contacts
import AppKit

var arguments = CommandLine.arguments
var responseURL: URL? = nil
func output(_ value: [String:Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject:value) else { return }
    if let url = responseURL { try? data.write(to:url,options:.atomic) }
    else if let text = String(data:data,encoding:.utf8) { print(text) }
}
func execute() {
let store = CNContactStore()
if arguments.contains("--check") {
    output(["authorization": CNContactStore.authorizationStatus(for:.contacts).rawValue])
    exit(0)
}
let authorizing = arguments.dropFirst().first == "--authorize"
let creating = arguments.dropFirst().first == "--create"
let query = (creating || authorizing) ? "create" : (arguments.dropFirst().first ?? "")
guard !query.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty && query.count <= 200 else {
    output(["error":"Укажите имя контакта (до 200 символов)."]);exit(0)
}
let done = DispatchSemaphore(value:0)
var granted = false
store.requestAccess(for:.contacts) { ok, _ in granted=ok;done.signal() }
guard done.wait(timeout:.now()+45) == .success && granted else {
    output(["error":"Нет доступа к Контактам. Разрешите доступ на Mac: Системные настройки → Конфиденциальность и безопасность → Контакты."]);exit(0)
}
if authorizing { output(["authorization":CNContactStore.authorizationStatus(for:.contacts).rawValue]);exit(0) }
if creating {
    do {
        guard arguments.count == 3,
              let bytes = arguments[2].data(using:.utf8),
              let input = try JSONSerialization.jsonObject(with:bytes) as? [String:String] else {
            output(["error":"Некорректные поля контакта."]);exit(0)
        }
        let given = (input["given_name"] ?? "").trimmingCharacters(in:.whitespacesAndNewlines)
        let family = (input["family_name"] ?? "").trimmingCharacters(in:.whitespacesAndNewlines)
        let phone = (input["phone"] ?? "").trimmingCharacters(in:.whitespacesAndNewlines)
        let email = (input["email"] ?? "").trimmingCharacters(in:.whitespacesAndNewlines)
        guard !given.isEmpty && given.count <= 100 && family.count <= 100 && phone.count <= 80 && email.count <= 254 else {
            output(["error":"Укажите корректное имя, телефон или email."]);exit(0)
        }
        func digits(_ text:String) -> String {
            var value = text.filter { $0.isNumber }
            if value.count == 11 && value.first == "8" { value = "7" + value.dropFirst() }
            return value
        }
        let keys = [CNContactGivenNameKey,CNContactFamilyNameKey,CNContactPhoneNumbersKey,CNContactEmailAddressesKey] as [CNKeyDescriptor]
        let fetch = CNContactFetchRequest(keysToFetch:keys)
        var duplicate = false
        try store.enumerateContacts(with:fetch) { contact, stop in
            let samePhone = !digits(phone).isEmpty && contact.phoneNumbers.contains { digits($0.value.stringValue) == digits(phone) }
            let sameEmail = !email.isEmpty && contact.emailAddresses.contains { ($0.value as String).lowercased() == email.lowercased() }
            let sameName = phone.isEmpty && email.isEmpty && contact.givenName.caseInsensitiveCompare(given) == .orderedSame && contact.familyName.caseInsensitiveCompare(family) == .orderedSame
            if samePhone || sameEmail || sameName { duplicate = true;stop.pointee = true }
        }
        if duplicate { output(["duplicate":true,"name":given + (family.isEmpty ? "" : " " + family)]);exit(0) }
        let contact = CNMutableContact()
        contact.givenName = given
        contact.familyName = family
        if !phone.isEmpty { contact.phoneNumbers = [CNLabeledValue(label:CNLabelPhoneNumberMobile,value:CNPhoneNumber(stringValue:phone))] }
        if !email.isEmpty { contact.emailAddresses = [CNLabeledValue(label:CNLabelHome,value:email as NSString)] }
        let request = CNSaveRequest()
        request.add(contact,toContainerWithIdentifier:nil)
        try store.execute(request)
        output(["created":true,"id":contact.identifier,"name":given + (family.isEmpty ? "" : " " + family),"phone":phone,"email":email])
    } catch { output(["error":"Не удалось сохранить контакт. Проверьте разрешения и доступность адресной книги."]) }
    exit(0)
}
do {
    let keys = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactMiddleNameKey,
                CNContactOrganizationNameKey, CNContactPhoneNumbersKey, CNContactEmailAddressesKey] as [CNKeyDescriptor]
    let request=CNContactFetchRequest(keysToFetch:keys)
    request.predicate=CNContact.predicateForContacts(matchingName:query)
    request.sortOrder = .userDefault
    var items:[[String:Any]]=[]
    var truncated=false
    try store.enumerateContacts(with:request) { contact, stop in
        if items.count >= 20 { truncated=true;stop.pointee=true;return }
        let name=[contact.givenName,contact.middleName,contact.familyName].filter { !$0.isEmpty }.joined(separator:" ")
        items.append(["name":name.isEmpty ? contact.organizationName : name,
                      "phones":contact.phoneNumbers.map { $0.value.stringValue },
                      "emails":contact.emailAddresses.map { $0.value as String }])
    }
    output(["contacts":items,"truncated":truncated])
} catch { output(["error":"Не удалось прочитать контакты. Проверьте разрешения macOS."]) }

}
if let index = arguments.firstIndex(of:"--request"), arguments.count > index+1 {
    let requestURL = URL(fileURLWithPath:arguments[index+1]).standardizedFileURL
    let expected = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("contacts_requests").standardizedFileURL
    guard requestURL.deletingLastPathComponent() == expected,
          let data = try? Data(contentsOf:requestURL),
          let request = try? JSONSerialization.jsonObject(with:data) as? [String:[String]],
          let values = request["args"] else { exit(1) }
    responseURL = requestURL.appendingPathExtension("result.json")
    arguments = [arguments[0]] + values
}
if Bundle.main.bundleURL.pathExtension == "app" {
    if arguments.count == 1 { arguments.append("--authorize") }
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    DispatchQueue.global().async { execute();exit(0) }
    app.run()
} else { execute() }
