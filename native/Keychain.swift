// Keep this helper's bytes stable across UI releases. It never displays authentication UI.
import Foundation
import Security
import LocalAuthentication

let service = "local.CodexQuota.accounts.v2"
let context = LAContext()
context.interactionNotAllowed = true

func query(_ key: String, serviceName: String = service) -> [String: Any] {
    [kSecClass as String: kSecClassGenericPassword,
     kSecAttrService as String: serviceName,
     kSecAttrAccount as String: key,
     kSecUseAuthenticationContext as String: context,
     kSecUseAuthenticationUI as String: kSecUseAuthenticationUIFail]
}

func code(_ status: OSStatus) -> Int32 {
    if status == errSecSuccess { return 0 }
    if status == errSecItemNotFound { return 10 }
    if status == errSecInteractionNotAllowed || status == errSecAuthFailed || status == errSecUserCanceled { return 11 }
    return 13
}

func put(_ data: Data, key: String, serviceName: String = service) -> OSStatus {
    let q = query(key, serviceName: serviceName)
    let status = SecItemUpdate(q as CFDictionary, [kSecValueData as String: data] as CFDictionary)
    if status != errSecItemNotFound { return status }
    var item = q
    item[kSecValueData as String] = data
    item[kSecAttrLabel as String] = "Codex 额度 · 固定凭据助手"
    return SecItemAdd(item as CFDictionary, nil)
}

func get(_ key: String, serviceName: String = service) -> (OSStatus, Data?) {
    var q = query(key, serviceName: serviceName)
    q[kSecReturnData as String] = true
    q[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(q as CFDictionary, &result)
    return (status, result as? Data)
}

if CommandLine.arguments == [CommandLine.arguments[0], "--version"] {
    print("1")
    exit(0)
}
if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--self-test" {
    let key = UUID().uuidString
    let testService = service + ".self-test"
    let payload = Data("{\"test\":true}".utf8)
    let written = put(payload, key: key, serviceName: testService)
    let (read, returned) = get(key, serviceName: testService)
    let deleted = SecItemDelete(query(key, serviceName: testService) as CFDictionary)
    let result: [String: Any] = ["write": written, "read": read, "delete": deleted, "matches": returned == payload]
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: result))
    exit(written == errSecSuccess && read == errSecSuccess && deleted == errSecSuccess && returned == payload ? 0 : 1)
}
guard CommandLine.arguments.count == 3 else { exit(12) }
let operation = CommandLine.arguments[1], key = CommandLine.arguments[2]
guard key.count == 24, key.allSatisfy({ $0.isHexDigit }) else { exit(12) }
if operation == "--vault-get" {
    let (status, data) = get(key)
    if status == errSecSuccess, let data { FileHandle.standardOutput.write(data) }
    exit(code(status))
}
if operation == "--vault-put" {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard data.count < 100_000, (try? JSONSerialization.jsonObject(with: data)) != nil else { exit(12) }
    exit(code(put(data, key: key)))
}
exit(12)
